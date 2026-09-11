"""Look an hour ahead, then spend the cheapest rung that holds ISO comfort.

The ladder in `brain.py` is reactive: it measures PMV now and climbs when the
room has been uncomfortable for a measured stretch. That is safe and it is
honest, but it can only ever answer "is this room uncomfortable yet?". It cannot
answer "will 32 W of fan still be holding this room in forty minutes, or am I
about to need 1450 W of compressor?" — and that second question is where the
energy is.

Horizon answers it by projection. Every tick it rolls the room forward one hour
under each rung the ladder itself would consider, prices the electricity, and
keeps the cheapest option that never leaves the comfort band on the way. If no
option holds, or the models disagree with each other, it says nothing at all and
the reactive ladder decides exactly as it always has. Silence is a valid answer
here, and it is always the safe one.

WHAT IT MAY AND MAY NOT DO
    * may spend SHADE, VENT and FAN — free, free, and 8-32 W
    * may shorten the streak the compressor waits for, to a floor of one tick
    * may NEVER switch the compressor on itself

  That last line is the one to defend. A projection is a claim about a room that
  does not exist yet; 1450 W should answer to a thermometer, not to a claim. So
  the compressor still waits for measured discomfort, and all Horizon can do is
  stop it waiting longer than the evidence warrants.

COMFORT IS ISO 7730 CATEGORY B, BOTH HALVES
    `comfort.comfort()` returns PMV and throws PPD away. PMV alone is an
    approximation of the standard; the standard's actual acceptability test is a
    PMV band AND a PPD ceiling. Horizon therefore calls `pmv_fanger()` directly
    and checks both. This matters more here than in the reactive ladder, because
    an optimiser told to minimise energy subject to a comfort constraint will
    always settle against the loosest edge of whatever constraint it is given —
    so the constraint has to be the real one.

TWO MODELS, COMPOSED — and the reason that is not a workaround
    A room under an AC is two things happening at once: an envelope warming or
    cooling on its own, and a compressor pulling against it. `project_envelope`
    is the first; `digital_twin.advance` is the second, and it is the *same*
    function the live twin steps once per tick, so the room the planner optimises
    against is the room the controller will actually produce.

    The envelope carries no AC term on purpose. On this rig the DHT22 measures a
    room the compressor is not holding, which makes the sensor a genuine
    no-AC baseline and the twin's output a genuine delta on top of it. The
    composition is exact rather than approximate, and the seam is visible.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Optional

import digital_twin as dt
from comfort import (AWAY, BLINDS_SHADE_FRACTION, FAN_VELOCITY,
                     OCCUPANT_MET_CLO, SOLAR_MRT_GAIN_C, WINDOW_DRAFT_MS,
                     AWAKE, pmv_fanger)

# ── ISO 7730 Category B ─────────────────────────────────────────────────
# The band and its PPD ceiling, together, because the standard defines
# acceptability as both. Category B is ISO's "acceptable" tier and it is already
# where the reactive ladder's own PMV_HOT sits, so adopting it here changes the
# horizon Horizon plans over, not the comfort the room targets.
#
# This constant is the energy/comfort dial. Tightening it to Category A (±0.2,
# PPD ≤6) buys comfort and spends electricity; loosening to C (±0.7, ≤15) does
# the reverse. It is named, and it is one number, so that trade is a decision
# somebody made rather than a side effect of how a cost function got weighted.
ISO_CATEGORY_B_PMV = 0.5
ISO_CATEGORY_B_PPD = 10.0

# ── the horizon ─────────────────────────────────────────────────────────
# One hour, in 5-minute steps. Coarse deliberately: the room's time constant is
# hours, so a finer step would cost CPU to resolve detail the physics does not
# contain. Twelve steps of arithmetic per candidate is nothing next to the
# camera inference already running on the other side of this board.
HORIZON_MINUTES = 60.0
HORIZON_STEP_MIN = 5.0

# ── the electricity ─────────────────────────────────────────────────────
# Fan draw by ladder level. Imported rather than retyped: `energy.py` already
# owns this table for the savings estimator, and a third copy of it in this file
# is a third thing to forget to update. `test_horizon.py` pins the two against
# each other. AC draw is NOT here — it comes from the appliance profile in
# `digital_twin`, phased the way an inverter actually modulates.
FAN_WATTS = {0: 0.0, 1: 8.0, 2: 18.0, 3: 32.0}

# How far apart the AR forecast and the envelope projection may be, in °C, before
# Horizon stops trusting either of them. A sustained gap means something is
# happening in this room that neither model knows about — a door opened, the sun
# came round a corner the model has no term for, a sensor is drifting — and the
# right response to "my model is wrong" is to stop planning with it, not to plan
# harder. The reactive ladder is measurement-driven and still correct here.
DISAGREEMENT_LIMIT_C = 1.5


def projection_config(config: Optional[dt.TwinConfig] = None) -> dt.TwinConfig:
    """The twin's config, retuned for a forecast instead of a measurement.

    `sensor_anchor_tau_h` exists so the live model cannot wander away from the
    thermometer: it is a correction toward an observation. A projection has no
    observation to correct toward — its baseline IS the envelope model's own
    output — so the slow 2 h anchor becomes a lag on nothing, and the projected
    room ends up cooler than the envelope it is supposed to be tracking.

    That error runs the wrong way. It under-predicts how warm the room gets, so
    every candidate looks more comfortable than it will be, and the layer would
    recommend a fan for a room that is about to need a compressor. Anchoring at
    the fast limit instead makes the projected room converge to
    "envelope minus whatever the AC removed", which is the composition this
    module claims to compute. The AC arithmetic is untouched, and
    POWERED_ANCHOR_WEIGHT still loosens the anchor while the compressor owns the
    room.
    """
    base = config or dt.TwinConfig()
    # AND max_step_s must admit the projection's own step. It defaults to
    # 120 s — a guard against one live tick integrating a huge elapsed time —
    # and `advance` applies it with min(), so a 300 s projection step was
    # silently truncated to 120 s. The envelope advanced a full hour while the
    # modelled room chased it through 24 minutes of dynamics, leaving every
    # candidate up to 2.6 C more comfortable than it will be. Exactly the
    # optimism this function was written to remove, reintroduced one line
    # below the comment saying so.
    step_s = HORIZON_STEP_MIN * 60.0
    return replace(base, sensor_anchor_tau_h=0.25,
                   max_step_s=step_s,
                   reset_gap_s=max(base.reset_gap_s, step_s)).validated()


@dataclass(frozen=True)
class Envelope:
    """How this room drifts with the compressor off, per hour.

    Defaults are the engineering estimates from `room_model.py` — the same
    lumped-RC numbers the simulator has always used. `calibrate.py` replaces the
    ones the logged data can actually identify and leaves the rest alone, so a
    board with no history still projects something physically sensible instead of
    refusing to plan.
    """
    tau_h: float = 5.4                  # conduction time constant
    solar_c_per_h: float = 5.2          # at full sun, unshaded
    shade_block: float = 0.82           # blinds block this fraction
    vent_c_per_h_per_c: float = 0.45    # per °C of indoor-outdoor delta
    occupant_c_per_h: float = 0.45      # a body's sensible gain, awake
    provenance: str = "prior"           # "prior" | "fitted" | "mixed"


@dataclass(frozen=True)
class Exogenous:
    """What the room is subjected to over the horizon, and cannot change."""
    outdoor_c: float
    outdoor_rh: float
    solar: float
    indoor_rh: float
    occupant: str


@dataclass(frozen=True)
class Projection:
    """One candidate rolled forward: what it costs and whether it holds."""
    plan: object                        # the Plan this projects
    watt_hours: float
    worst_pmv: float
    worst_ppd: float
    end_c: float
    holds: bool                         # never left Category B on the way


@dataclass(frozen=True)
class Advisory:
    """What Horizon recommends, or why it declined."""
    plan: Optional[object]
    reason: str
    watt_hours: Optional[float] = None
    worst_ppd: Optional[float] = None
    cheaper_by_wh: Optional[float] = None
    provenance: str = "prior"
    # What this advisory predicts the room will BE at the end of the horizon,
    # and how that is expected to feel. Recorded on every advisory including
    # the declines, because a layer that only reports predictions when it also
    # wants to act can never be scored on the hours it stayed silent — and
    # those are exactly the hours it has to earn trust across.
    predicted_c: Optional[float] = None
    predicted_pmv: Optional[float] = None
    predicted_for: Optional[float] = None      # unix time the prediction is ABOUT


def project_envelope(baseline_c: float, exog: Exogenous, plan,
                     env: Envelope, dt_h: float) -> float:
    """Advance the no-AC room one step. Pure arithmetic, no AC term.

    The lumped-RC form the simulator has always used, minus the compressor:
    conduction toward outdoor, solar gain through whatever the blinds are not
    blocking, a body's heat if somebody is in the room, and ventilation losses
    when the window is open and outdoor air is genuinely cooler.
    """
    conduction = (exog.outdoor_c - baseline_c) / env.tau_h
    shade = env.shade_block if getattr(plan, "blinds_shut", False) else 0.0
    solar = env.solar_c_per_h * exog.solar * (1.0 - shade)
    body = env.occupant_c_per_h if exog.occupant != AWAY else 0.0
    vent = 0.0
    if getattr(plan, "window", False) and exog.outdoor_c < baseline_c:
        vent = env.vent_c_per_h_per_c * (baseline_c - exog.outdoor_c)
    return baseline_c + dt_h * (conduction + solar + body - vent)


def ppd_for(pmv: float) -> float:
    """ISO 7730's PPD for a PMV. A pure function of one number, which is why the
    band and the ceiling are two statements of the same constraint — and why
    deriving PPD from a SHIFTED PMV is the only coherent thing to do once the
    occupant's dial or a sleeping occupant has moved the scale."""
    return 100.0 - 95.0 * math.exp(-0.03353 * pmv ** 4 - 0.2179 * pmv ** 2)


def comfort_with_ppd(indoor_c: float, rh: float, solar: float, plan,
                     occupant: str) -> tuple[float, float]:
    """PMV **and** PPD for a room under a plan.

    Deliberately not `comfort.comfort()`, which computes PPD inside
    `pmv_fanger` and then discards it (`pmv, _ = ...`). Horizon needs both,
    because ISO 7730 acceptability is a band and a ceiling. The inputs are
    assembled exactly as `comfort()` assembles them, and a test pins the PMV
    halves of the two against each other so they cannot drift apart.
    """
    velocity = FAN_VELOCITY[getattr(plan, "fan", 0)] + (
        WINDOW_DRAFT_MS if getattr(plan, "window", False) else 0.0)
    exposure = ((1.0 - BLINDS_SHADE_FRACTION)
                if getattr(plan, "blinds_shut", False) else 1.0)
    mrt = indoor_c + SOLAR_MRT_GAIN_C * solar * exposure
    met, clo = OCCUPANT_MET_CLO.get(occupant, OCCUPANT_MET_CLO[AWAKE])
    return pmv_fanger(indoor_c, mrt, velocity, rh, met, clo)


def roll_out(plan, *, start_c: float, baseline_c: float, exog: Exogenous,
             env: Envelope, setpoint_c: float, cooling_effect: float = 0.0,
             watts: float = 0.0, band_offset_pmv: float = 0.0,
             config: Optional[dt.TwinConfig] = None,
             profile: Optional[dt.AcProfile] = None) -> Projection:
    """Roll one candidate forward over the horizon.

    `start_c` is the room as the controller currently understands it (the twin's
    modelled temperature); `baseline_c` is the same room with the compressor
    off. They differ whenever the AC has been running, and both have to advance:
    the envelope on its own physics, the modelled room chasing it.

    `band_offset_pmv` slides the comfort scale exactly as `_comfort_offset()`
    does in the ladder — the occupant's dial and a sleeping occupant's relaxed
    band. Without it, "hold Category B" would quietly overrule a preference the
    user deliberately expressed.
    """
    config = projection_config(config)
    profile = profile or config.profile
    steps = max(1, int(round(HORIZON_MINUTES / HORIZON_STEP_MIN)))
    dt_h = HORIZON_STEP_MIN / 60.0
    dt_s = HORIZON_STEP_MIN * 60.0

    modeled, base, effect, draw = start_c, baseline_c, cooling_effect, watts
    total_wh = 0.0
    worst_pmv, worst_ppd = None, 0.0
    ac_on = bool(getattr(plan, "ac", False))

    for _ in range(steps):
        base = project_envelope(base, exog, plan, env, dt_h)
        step = dt.advance(
            modeled_c=modeled, cooling_effect=effect, previous_watts=draw,
            baseline_c=base, outdoor_c=exog.outdoor_c, power=ac_on,
            setpoint_c=setpoint_c, dt_s=dt_s, config=config, profile=profile)
        modeled, effect, draw = step.modeled_c, step.cooling_effect, step.estimated_watts

        # The fan and the lamp draw for the whole step; the compressor's own
        # trapezoid comes from the profile, which knows how an inverter phases.
        total_wh += FAN_WATTS.get(int(getattr(plan, "fan", 0)), 0.0) * dt_h
        total_wh += step.interval_wh

        pmv, _ = comfort_with_ppd(modeled, exog.indoor_rh, exog.solar,
                                  plan, exog.occupant)
        # Shift FIRST, then derive PPD from the shifted value. Capturing ppd
        # before the offset paired a relaxed band with an unrelaxed ceiling,
        # and since PPD <= 10 is exactly |PMV| <= 0.5, the ceiling silently
        # re-imposed the band the offset had just relaxed — so a sleeping
        # occupant and a warm dial made the planner decline MORE often, the
        # opposite of what both are for.
        pmv -= band_offset_pmv
        ppd = ppd_for(pmv)
        if worst_pmv is None or abs(pmv) > abs(worst_pmv):
            worst_pmv, worst_ppd = pmv, ppd

    holds = (worst_pmv is not None
             and abs(worst_pmv) <= ISO_CATEGORY_B_PMV
             and worst_ppd <= ISO_CATEGORY_B_PPD)
    return Projection(plan=plan, watt_hours=total_wh, worst_pmv=worst_pmv or 0.0,
                      worst_ppd=worst_ppd, end_c=modeled, holds=holds)


def candidates(reading, plan, gate: dict, climb: Callable,
               fan_ceiling: int = 3, vetoed=frozenset()) -> list:
    """Every rung combination the gates permit — a SEARCH SPACE, not a climb path.

    The first version of this walked `_climb_one_rung` repeatedly, on the
    reasoning that inheriting the ladder's own SHADE -> VENT -> FAN order for free
    meant Horizon could never invent an action the ladder would not produce. The
    order came along, but so did a defect: a climb only ever goes UP, so a room
    already on FAN could never be offered the free VENT rung sitting below it.

    Found by simulating a winter night. The reactive ladder had the window open
    and the room falling; the planner sat on FAN_MED with the window shut and the
    room rising, reporting "already on the cheapest rung that holds" — true only
    of the candidates it had bothered to generate. It was paying 18 W to hold a
    room that 0 W of open window was already holding better, and the room warmed
    until the ladder reached for the compressor.

    So: enumerate. Four fan levels times the free rungs their gates allow is at
    most sixteen rollouts, which is nothing, and it makes "cheapest sufficient"
    a true statement instead of an artefact of the walk order.

    Gate discipline is unchanged and it is why `climb` is still accepted: a rung
    is only ever OFFERED when its own gate holds, exactly as `_climb_one_rung`
    would. Revocation of a rung whose gate has failed happens upstream in the
    ladder, before the planner is consulted.

    The compressor is not in the space. Not filtered out afterwards — never
    generated, so it is never one comparison away from being chosen.
    """
    # A forbidden device is never OFFERED, so the planner optimises inside
    # what it is actually allowed to do. Filtering only at the end would let
    # it keep picking an option it cannot have and silently lose its best
    # permitted one to a comparison it was never going to win.
    shade_options = {bool(getattr(plan, "blinds_shut", False))}
    if gate.get("sun") and "blinds" not in vetoed:
        shade_options.add(True)
    vent_options = {bool(getattr(plan, "window", False))}
    if gate.get("vent") and "window" not in vetoed:
        vent_options.add(True)

    # The incumbent fan level is always offered, even when it sits ABOVE the
    # mode's ceiling — which is exactly what happens on the tick an occupant
    # falls asleep with the fan on HIGH. Without it "change nothing" is absent
    # from the list, `now` becomes a fabricated 0 W baseline, and the advisory
    # can report "already on the cheapest rung that holds" while running 32 W.
    current_fan = max(0, int(getattr(plan, "fan", 0)))
    ceiling = (current_fan if "fan" in vetoed
               else max(current_fan, max(0, int(fan_ceiling))))
    out = []
    for blinds in sorted(shade_options):
        for window in sorted(vent_options):
            for fan in range(ceiling + 1):
                out.append(replace(plan, fan=fan, window=window,
                                   blinds_shut=blinds, ac=False))
    # The plan as it stands goes first, so `roll_out`'s caller can compare
    # everything against "change nothing" without searching for it.
    current = replace(plan, ac=False)
    out.sort(key=lambda p: (p.fan, p.window, p.blinds_shut) != (
        current.fan, current.window, current.blinds_shut))
    return out


def advise(reading, plan, gate: dict, *, climb: Callable,
           start_c: float, baseline_c: float, setpoint_c: float,
           env: Optional[Envelope] = None,
           cooling_effect: float = 0.0, watts: float = 0.0,
           band_offset_pmv: float = 0.0, fan_ceiling: int = 3,
           vetoed=frozenset(),
           ar_t_hat: Optional[float] = None,
           at: Optional[float] = None) -> Advisory:
    """The recommendation, or a stated reason for declining to make one.

    Declines — and a decline is a normal, safe outcome, not a failure — when:
      * the room is empty. `_decide_away` owns empty rooms, and it is already
        correct: a fan cools a person, not a room.
      * the AR forecast and the envelope projection disagree beyond
        DISAGREEMENT_LIMIT_C. Something is going on that neither model has a
        term for, and planning with a wrong model is worse than not planning.
      * nothing holds Category B for the whole hour. Then this is a job for the
        measured ladder and, if it comes to it, the compressor.
    """
    env = env or Envelope()

    # PREDICT FIRST, DECIDE SECOND. Rolling the plan already in force forward
    # is what this layer claims to be able to do, and that claim is testable on
    # every hour -- including the empty ones and the ones it declines to act
    # on. Scoring only the hours it chose to act would grade it on the cases it
    # already liked, which is how a model looks accurate and is not.
    held = roll_out(plan, start_c=start_c, baseline_c=baseline_c,
                    exog=_exog(reading), env=env, setpoint_c=setpoint_c,
                    cooling_effect=cooling_effect, watts=watts,
                    band_offset_pmv=band_offset_pmv)
    forecast = {"predicted_c": held.end_c, "predicted_pmv": held.worst_pmv,
                "predicted_for": (at or 0.0) + HORIZON_MINUTES * 60.0}

    if getattr(reading, "occupant", None) == AWAY:
        return Advisory(None, "empty room: passive rungs only",
                        provenance=env.provenance, **forecast)

    if ar_t_hat is not None:
        # COMPARE LIKE WITH LIKE. `ar_t_hat` forecasts the room the loop decides
        # on, which is the twin's MODELLED temperature (loop._tick feeds
        # `effective_indoor` into both the forecast history and reading.indoor).
        # This projected from `baseline_c` — the MEASURED, no-AC room — so the
        # two sides described different rooms and the "disagreement" was just
        # the twin's model-to-sensor delta wearing a different name.
        #
        # Read off the board: modelled 19.2, sensed 25.1, and the planner
        # deferring "models disagree by 6.4 C" 264 times in two hours while the
        # AC held a 19 C setpoint. Structurally guaranteed, not a bad forecast:
        # any running AC silenced the planner for as long as it ran.
        #
        # Starting from `start_c` puts both sides on the room the forecast
        # actually predicts. The residual is the AC term this projection omits
        # by design, bounded by one step: 5 min at 7.5 C/h is ~0.6 C, inside
        # DISAGREEMENT_LIMIT_C, so a genuine model conflict still trips it.
        one_step = project_envelope(
            start_c, _exog(reading), plan, env, HORIZON_STEP_MIN / 60.0)
        gap = dt._clamp(abs(ar_t_hat - one_step), 0.0, 99.0)
        if gap > DISAGREEMENT_LIMIT_C:
            return Advisory(
                None, f"models disagree by {gap:.1f} C; deferring to measurement",
                provenance=env.provenance, **forecast)

    exog = _exog(reading)
    rolled = [
        roll_out(c, start_c=start_c, baseline_c=baseline_c, exog=exog, env=env,
                 setpoint_c=setpoint_c, cooling_effect=cooling_effect,
                 watts=watts, band_offset_pmv=band_offset_pmv)
        for c in candidates(reading, plan, gate, climb, fan_ceiling=fan_ceiling,
                            vetoed=vetoed)
    ]
    feasible = [p for p in rolled if p.holds]
    if not feasible:
        return Advisory(
            None, "no rung holds Category B for the hour; measured ladder decides",
            provenance=env.provenance, **forecast)

    now = rolled[0]                      # candidates() puts "change nothing" first
    # Ties go to the incumbent. Without this a plan costing the same as the
    # current one could still be "chosen", and the room would swap between equally
    # priced rungs every time the projection wobbled.
    best = min(feasible, key=lambda p: (p.watt_hours, p.plan is not now.plan))
    if not _differs(best.plan, now.plan):
        return Advisory(None, "already on the cheapest rung that holds",
                        watt_hours=now.watt_hours, worst_ppd=now.worst_ppd,
                        provenance=env.provenance, **forecast)

    saved = now.watt_hours - best.watt_hours if now.holds else None
    cheaper = (f", {saved:.0f} Wh cheaper" if saved and saved > 0.5 else "")
    return Advisory(
        best.plan,
        # No module name in the sentence. "horizon:" is what this file is called,
        # not something a person in the room knows or needs to; and since the
        # console now renders this reason as the most prominent line on its first
        # screen, the internal vocabulary would be the most visible words in the
        # product. Says what it did and what it bought, in that order.
        f"holds comfort for the next {HORIZON_MINUTES / 60:.0f} h "
        f"at {best.watt_hours:.0f} Wh, {best.worst_ppd:.0f}% dissatisfied"
        f"{cheaper}",
        watt_hours=best.watt_hours, worst_ppd=best.worst_ppd,
        cheaper_by_wh=saved, provenance=env.provenance,
        # The ACTING advisory predicts the room it is steering towards, not the
        # one it is leaving. Scoring it against the held-plan forecast would
        # grade it on a room it deliberately chose not to have.
        predicted_c=best.end_c, predicted_pmv=best.worst_pmv,
        predicted_for=forecast["predicted_for"])


def _differs(a, b) -> bool:
    """Do two plans command different hardware? Ignores `reason`, which carries
    the live PMV and so changes every tick even when nothing moved."""
    return (a.fan, a.window, a.blinds_shut, a.ac) != (
        b.fan, b.window, b.blinds_shut, b.ac)


def _exog(reading) -> Exogenous:
    """Reading -> the room's exogenous inputs, with the ladder's own fallbacks."""
    return Exogenous(
        outdoor_c=getattr(reading, "outdoor", None) if getattr(
            reading, "outdoor", None) is not None else reading.indoor,
        outdoor_rh=getattr(reading, "outdoor_rh", 50.0),
        solar=getattr(reading, "solar", 0.0) or 0.0,
        indoor_rh=getattr(reading, "indoor_rh", 50.0),
        occupant=getattr(reading, "occupant", AWAKE))
