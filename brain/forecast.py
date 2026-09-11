"""Short-horizon temperature forecast — AR(p) on the room's own history.

    T_hat[t+1] = w0 + w1·T[t] + w2·T[t-1] + ... + wp·T[t-p+1]

No outdoor temp, no solar, no actuator state. That is the appeal: it needs
nothing the MCU is not already sampling at 1 Hz, and it captures the *net*
effect of everything — AC, sun, occupancy, an open door — without modelling
each cause separately.

Kept out of `brain.py` on purpose. `decide()` stays a pure function of
(Reading, Plan, Memory); the forecast arrives as one more field on Reading,
exactly as `comfort.py` is kept separate from the ladder.

WHAT THIS CANNOT DO — worth stating because it is the reason `room_model.py`
still exists. This model is blind to cause. It can say the room is heating; it
cannot say whether that is the sun coming round, a door opening, or the AC
having silently failed. Those are identical to a pure history model. The
grey-box physics model can separate them because it has terms for each.

Units: the trend this module reports is **°C per hour**, a physical rate. It used
to be °C per tick, which stopped meaning anything once the control loop became
event-driven — a threshold in per-tick units silently re-tunes itself whenever the
cadence changes, which is exactly the class of bug that hides until it chatters.

They are not competitors:
  * AR       cheap, fast, good lead time on the next tick or two
  * physics  trusted for the multi-hour forecast, and for explaining *why*
  * residual a large sustained disagreement between the two is the cheapest
             anomaly detector we have — "something is happening in this room
             that neither model knows about" (see `disagreement()`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# ── defaults ────────────────────────────────────────────────────────────
# EWMA is the running-today default. Refit with least_squares_weights() once
# the telemetry has a few days in it — the fitted weights are strictly better
# than a guessed decay rate.
DEFAULT_LAGS = 5
DEFAULT_ALPHA = 0.45          # larger = more reactive, smoother when smaller

# The cadence the per-tick constants below were calibrated against. Trend is
# reported in °C/hour — a physical rate that means the same thing whatever the
# loop interval is — and this is only used to convert the historical per-tick
# thresholds into that unit, so the numbers stay comparable to what shipped.
NOMINAL_TICK_S = 30.0

# PD gains for the decision score. Deliberately small: this is an early-warning
# nudge on top of the PMV ladder, never a second controller.
KP = 1.0
# Lead time, in hours, that the derivative term buys. Trend is °C/hour, so this
# is 3 minutes of lead expressed in the same unit — the same lead the per-tick
# form bought at 6 ticks × 30 s.
KD_HOURS = 0.05

# Below this the trend is noise, not a trend. DHT22 quantises at 0.1 °C, and at
# the nominal cadence anything under ~0.02 °C/tick was quantisation flicker;
# that same floor is 2.4 °C/hour.
TREND_DEADBAND_C_PER_H = 0.02 * 3600.0 / NOMINAL_TICK_S


@dataclass(frozen=True)
class Forecast:
    t_hat: Optional[float] = None      # predicted next-tick temperature
    trend: float = 0.0                 # °C per HOUR, weighted slope
    score: float = 0.0                 # PD decision score
    lags_used: int = 0

    @property
    def rising(self) -> bool:
        return self.trend > TREND_DEADBAND_C_PER_H

    @property
    def falling(self) -> bool:
        return self.trend < -TREND_DEADBAND_C_PER_H

    @property
    def verdict(self) -> str:
        if self.rising:
            return "heating"
        if self.falling:
            return "cooling"
        return "steady"


# ── weights ─────────────────────────────────────────────────────────────
def uniform_weights(lags: int = DEFAULT_LAGS) -> list[float]:
    """Simple moving average. A baseline and nothing more — it smooths noise
    but has zero predictive lead on a trend."""
    return [1.0 / lags] * lags


def ewma_weights(lags: int = DEFAULT_LAGS, alpha: float = DEFAULT_ALPHA) -> list[float]:
    """Exponential decay, normalised to sum to 1. One tunable parameter."""
    raw = [alpha * (1 - alpha) ** i for i in range(lags)]
    total = sum(raw)
    return [w / total for w in raw]


def least_squares_weights(series: Sequence[float], lags: int = DEFAULT_LAGS
                          ) -> tuple[list[float], float]:
    """Fit w1..wp and the intercept w0 by regressing T[t+1] on its own lags.

    Returns (weights, intercept). Strictly better than guessing a decay rate.

    Note the fitted weights may NOT decay monotonically with lag. If the room
    has real thermal inertia the weight on T[t-2] can legitimately exceed the
    weight on T[t-1], because the response to a change surfaces a tick or two
    later. Let the data say so rather than assuming exponential decay is right.
    """
    import numpy as np
    s = np.asarray([float(x) for x in series], dtype=float)
    if s.size < lags + 2:
        raise ValueError(f"need at least {lags + 2} samples, got {s.size}")
    rows = s.size - lags
    X = np.empty((rows, lags + 1))
    for i in range(rows):
        for j in range(lags):
            X[i, j] = s[i + lags - 1 - j]              # column j = lag j, newest first
    X[:, lags] = 1.0                                   # intercept column
    y = s[lags:]
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return [float(c) for c in coef[:lags]], float(coef[lags])


# ── prediction ──────────────────────────────────────────────────────────
def predict_temp(history: Sequence[float], weights: Optional[Sequence[float]] = None,
                 intercept: float = 0.0) -> Optional[float]:
    """T_hat for the next tick. `history` is oldest-first; the most recent
    reading is history[-1]. Returns None when there is not enough history —
    the caller must treat that as "no forecast", never as zero."""
    if not history:
        return None
    w = list(weights) if weights is not None else ewma_weights()
    recent = list(history)[-len(w):][::-1]             # newest first
    if len(recent) < 2:
        return None
    used = w[:len(recent)]
    total = sum(used) or 1.0
    # Renormalise: a short history uses fewer weights than the set provides,
    # and un-normalised weights would bias the prediction toward zero.
    return intercept + sum(wi * ti for wi, ti in zip(used, recent)) / total


def weighted_trend(history: Sequence[float],
                   weights: Optional[Sequence[float]] = None,
                   times_s: Optional[Sequence[float]] = None) -> float:
    """Weighted least-squares slope of recent temperature, **°C per hour**.

    Differences rather than levels: a room at a steady 30 °C and a room passing
    through 30 °C on its way up need different actions, and only the slope
    separates them.

    °C/hour, not °C/tick, because the loop no longer runs on a fixed interval —
    "per tick" stops being a unit the moment ticks can be 5 s or 30 s apart, and
    a threshold in those units silently re-tunes itself every time the cadence
    changes. `times_s` carries the real sample timestamps (seconds, any epoch;
    only differences matter). Omit it and samples are assumed NOMINAL_TICK_S
    apart, which is what every caller before variable cadence relied on.
    """
    h = list(history)
    if len(h) < 2:
        return 0.0
    n = min(len(h), len(weights) if weights is not None else DEFAULT_LAGS + 1)
    win = h[-n:]
    w = list(weights) if weights is not None else ewma_weights(n)
    w = w[:n][::-1]                                    # oldest-first, to match win

    # The time axis, in hours relative to the window's first sample. Regressing
    # on real elapsed time rather than sample index is what makes the slope a
    # physical rate: three samples 5 s apart and three 30 s apart describe very
    # different rooms, and the index form cannot tell them apart.
    if times_s is not None and len(times_s) >= len(h):
        ts = [float(t) for t in list(times_s)[-n:]]
        x = [(t - ts[0]) / 3600.0 for t in ts]
    else:
        x = [i * NOMINAL_TICK_S / 3600.0 for i in range(n)]

    # Weighted least-squares slope, NOT a weighted mean of first differences.
    # The mean-of-diffs form over-weights the newest sample's sign, so a sensor
    # flickering 26.0 / 26.1 / 26.0 on its 0.1 C quantisation step reads as a
    # real trend. Fitting a line through the window cancels that.
    sw = sum(w) or 1.0
    t_bar = sum(wi * xi for xi, wi in zip(x, w)) / sw
    y_bar = sum(wi * y for wi, y in zip(w, win)) / sw
    num = sum(wi * (xi - t_bar) * (y - y_bar) for xi, wi, y in zip(x, w, win))
    den = sum(wi * (xi - t_bar) ** 2 for xi, wi in zip(x, w))
    return num / den if den else 0.0


def decide_score(indoor: float, target: float, trend: float,
                 kp: float = KP, kd_hours: float = KD_HOURS) -> float:
    """D = kp·error + kd·trend — a PD controller in disguise.

    Positive means "hot, or heading there". The kd term is what buys lead time:
    a room still comfortable but climbing fast scores high enough to act before
    the PMV streak counter would have fired. `trend` is °C/hour and `kd_hours`
    is a lead time in hours, so their product is the °C the room is expected to
    move over that lead — the same quantity in any cadence."""
    return kp * (indoor - target) + kd_hours * trend


def forecast(history: Sequence[float], target: float = 26.0,
             weights: Optional[Sequence[float]] = None,
             intercept: float = 0.0,
             times_s: Optional[Sequence[float]] = None) -> Forecast:
    """Everything above, in one call. Safe on short or empty history."""
    h = [float(x) for x in history if x is not None]
    if len(h) < 2:
        return Forecast(lags_used=len(h))
    t_hat = predict_temp(h, weights, intercept)
    tr = weighted_trend(h, weights, times_s=times_s)
    if abs(tr) < TREND_DEADBAND_C_PER_H:               # quantisation, not signal
        tr = 0.0
    return Forecast(t_hat=t_hat, trend=tr,
                    score=decide_score(h[-1], target, tr),
                    lags_used=min(len(h), len(weights) if weights else DEFAULT_LAGS))


# ── the cheap anomaly detector ──────────────────────────────────────────
def disagreement(ar_t_hat: Optional[float], physics_t_hat: Optional[float]
                 ) -> Optional[float]:
    """Signed gap between the two models, °C.

    A large sustained value means something is happening that neither model
    accounts for: physics says stable while history says rising is the classic
    signature of a failed AC, an open door, or a sensor drifting. One sample
    means nothing — the caller must require it to persist."""
    if ar_t_hat is None or physics_t_hat is None:
        return None
    return ar_t_hat - physics_t_hat
