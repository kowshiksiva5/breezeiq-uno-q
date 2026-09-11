"""ISO 7730 PMV comfort model, met/clo by occupant state, MRT estimate.

Pure math, no I/O. `pmv_fanger` is the standard Fanger iterative solve
(ASHRAE 55 / ISO 7730) — the same equations pythermalcomfort implements,
written out directly so this module has no dependency-version surface.
"""

from __future__ import annotations

import math
from typing import Tuple

# --- occupant state -------------------------------------------------------

# UPPERCASE, and it matters. `OccupancyTracker.update()` emits "AWAY"/"AWAKE"/
# "ASLEEP", the telemetry `tick.occupant` column stores those, and the dashboard
# and energy code compare against them. These constants used to be lowercase, so
# `brain.decide()`'s `reading.occupant == AWAY` never matched a real reading: the
# empty-room branch never executed on the board, the fan and compressor were free
# to run in an empty room, and `OCCUPANT_MET_CLO.get()` always fell through to the
# AWAKE default so ASLEEP never applied either. Keep these equal to what
# `OccupancyTracker` emits — `test_occupancy_strings_match_brain` pins it.
AWAY = "AWAY"
AWAKE = "AWAKE"
ASLEEP = "ASLEEP"

# met (metabolic rate) and clo (clothing insulation) by occupant state.
# asleep values per spec (§5.1); awake is light indoor clothing, seated.
OCCUPANT_MET_CLO = {
    AWAKE: (1.1, 0.5),    # seated, light activity (typing/reading) — not 1.0 "quiet seated"
    ASLEEP: (0.8, 1.2),   # reduced metabolic rate, plus bedding
}

# fan speed -> air velocity at the body, m/s. Desk/ceiling fan estimates;
# window adds 0.14 m/s of cross-draft when open, independent of fan speed.
# Calibrated for the Atomberg CEILING fan named in the BOM, at seated position.
# A desk fan would be roughly half these. This is the single most important
# calibration constant in the project: it decides how much comfort 32 W of fan
# buys, and therefore how often the 1500 W compressor has to start.
# MEASURE IT on the real rig (smoke/tissue trace + stopwatch, or an anemometer)
# before quoting energy figures as anything but simulated.
FAN_VELOCITY = {
    0: 0.08,   # OFF — residual room air movement, not truly still
    1: 0.53,   # LOW
    2: 0.83,   # MED
    3: 1.13,   # HIGH  (ASHRAE 55 allows up to 1.2 m/s with occupant control)
}

WINDOW_DRAFT_MS = 0.14
SOLAR_MRT_GAIN_C = 3.0     # °C of MRT added per unit solar index, unshaded
BLINDS_SHADE_FRACTION = 0.82


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def pmv_fanger(ta: float, tr: float, vel: float, rh: float, met: float, clo: float, wme: float = 0.0) -> Tuple[float, float]:
    """Fanger PMV/PPD. ta, tr in °C; vel in m/s; rh in %; met in met units; clo in clo units."""
    pa = rh * 10 * math.exp(16.6536 - 4030.183 / (ta + 235))

    icl = 0.155 * clo
    m = met * 58.15
    w = wme * 58.15
    mw = m - w

    fcl = 1 + 1.29 * icl if icl <= 0.078 else 1.05 + 0.645 * icl

    hcf = 12.1 * math.sqrt(max(vel, 0.0))
    taa = ta + 273.0
    tra = tr + 273.0
    tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1)

    p1 = icl * fcl
    p2 = p1 * 3.96
    p3 = p1 * 100.0
    p4 = p1 * taa
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100.0) ** 4

    xn = tcla / 100.0
    xf = xn
    eps = 0.00015
    for _ in range(150):
        xf = (xf + xn) / 2.0
        hcn = 2.38 * abs(100.0 * xf - taa) ** 0.25
        hc = hcf if hcf > hcn else hcn
        xn = (p5 + p4 * hc - p2 * xf ** 4) / (100.0 + p3 * hc)
        if abs(xn - xf) <= eps:
            break

    tcl = 100.0 * xn - 273.0

    hl1 = 3.05 * 0.001 * (5733 - 6.99 * mw - pa)
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0.0
    hl3 = 1.7 * 0.00001 * m * (5867 - pa)
    hl4 = 0.0014 * m * (34 - ta)
    hl5 = 3.96 * fcl * (xn ** 4 - (tra / 100.0) ** 4)
    hl6 = fcl * hc * (tcl - ta)

    ts = 0.303 * math.exp(-0.036 * m) + 0.028
    pmv = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)
    ppd = 100.0 - 95.0 * math.exp(-0.03353 * pmv ** 4 - 0.2179 * pmv ** 2)
    return pmv, ppd


def comfort(indoor: float, rh: float, solar: float, fan: int, window: bool,
            blinds_shut: bool, occupant: str) -> float:
    """PMV(indoor, rh, solar, fan, window, blinds, occupant) -> clamped PMV in [-3, 3]."""
    velocity = FAN_VELOCITY[fan] + (WINDOW_DRAFT_MS if window else 0.0)
    exposure = (1 - BLINDS_SHADE_FRACTION) if blinds_shut else 1.0
    mrt = indoor + SOLAR_MRT_GAIN_C * solar * exposure

    met_clo = OCCUPANT_MET_CLO.get(occupant, OCCUPANT_MET_CLO[AWAKE])
    met, clo = met_clo

    pmv, _ = pmv_fanger(indoor, mrt, velocity, rh, met, clo)
    return clamp(pmv, -3.0, 3.0)
