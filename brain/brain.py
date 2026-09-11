"""THE PRODUCT. Pure function. No I/O. No hardware.

decide(): (Reading, Plan, Memory) -> (Plan, Memory)

Implements §5.2 (gates) and §5.3 (the ladder) exactly as specified:
graded climb/withdraw, hysteresis gates, step-change escape hatches for
PMV beyond ±1.2, a hair-trigger AC cutoff, and revoke-on-gate-false so a
rung never latches past the condition that justified it.

Two things sit either side of the ladder and never inside it: the occupant's
setpoint dial, which shifts the PMV scale before any threshold is compared
(`_preference_offset`), and the occupancy mode, whose every consequence is
declared in one table (`MODE_POLICY`) rather than scattered through branches.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from comfort import comfort, clamp, ASLEEP, AWAY

FAN_OFF, FAN_LOW, FAN_MED, FAN_HIGH = 0, 1, 2, 3

# ── the occupant's veto ─────────────────────────────────────────────────
# A standing "do not use this", distinct from the dashboard's manual override in
# the way a rule differs from an interruption: an override is a tap that expires,
# this is an instruction that persists until withdrawn. "Never open my window" —
# rain, street noise, allergies, a cat. "Not the compressor tonight."
#
# It is deliberately NOT a comfort input. The dial (`comfort_c`) says how warm you
# like the room and the ladder argues with it; a veto says which machinery it may
# argue with, and the ladder does not get an opinion about that.
#
# THE VETO SURVIVES THE ±1.2 PMV ESCAPE. The escape exists for somebody walking
# into a room that has baked all afternoon, and firing every rung at once is right
# — except for the rungs they have forbidden. A system that overrides a standing
# instruction because it disagrees is a system you stop trusting, and being right
# about the temperature does not buy that back. What it must do instead is SAY it
# cannot hold comfort, which `_vetoed_note` puts in the reason string.
VETOABLE = ("fan", "ac", "window", "blinds", "light")


def _vetoed_note(vetoed) -> str:
    """The clause appended to a reason when a veto shaped the outcome."""
    named = ", ".join(sorted(vetoed))
    return f"held back by your preference: no {named}"

DWELL_MIN = 5.0
# A rung the PLANNER spent serves the SAME dwell as one the ladder spent, and
# that is a correction, not an oversight. The first version of this gave the
# planner a shorter 1-minute floor, reasoning that a projection already knows what
# its own action does and so has nothing to wait to observe. True as far as it
# goes — and it broke something else. There is one dwell counter, so shortening it
# for the planner also licensed the REACTIVE ladder to climb again four minutes
# early, and the reactive ladder is the only thing that can reach the compressor.
# Measured over a simulated year: winter spent 10 extra compressor-minutes and
# 70% more electricity, with the planner never once asking for the AC.
#
# The room does not know who spent the rung. The reason to wait is that a rung and
# its own effect chase each other, and that is a fact about the room. So: one
# dwell, one duration. Costs the planner nothing real — it may act once per five
# minutes against a room whose time constant is five hours.
MILD_AC_OFF_MIN = 3.0
HOT_TRIGGER_MIN = 1.0
COLD_TRIGGER_FAST_MIN = 5.0
COLD_TRIGGER_SLOW_MIN = 20.0
PMV_STEP_HOT = 1.2
PMV_STEP_COLD = -1.2
PMV_HOT = 0.5
PMV_COLD = -0.2
PMV_MILD = 0.0
PMV_COLD_HARVEST = -0.4
PMV_COLD_FAST = -0.5

ECO_BAND_LOW = 25.0
ECO_BAND_HIGH = 29.0
PRECOOL_INDOOR_MIN = 27.0
# Two windows, not one, because the two rungs cost wildly different amounts.
#
# Shade and ventilation are free, so an empty room may use them as soon as a
# return is expected — an hour of blocked sun is an hour the compressor never has
# to undo. The compressor itself waits until the last quarter hour, because
# cooling an empty room for an hour to be ready at the end of it spends roughly
# four times the electricity for the same arrival. The pull-down rate makes that
# affordable: this room drops about 10 °C/h at rated capacity, so fifteen minutes
# is a 2.5 °C head start, and the +1.2 PMV escape covers whatever is left.
PRECOOL_PASSIVE_DUE_HOME_MIN = 60.0
PRECOOL_DUE_HOME_MIN = 15.0
PASSIVE_BAKE_INDOOR = 29.0
RELEASE_WINDOW_INDOOR = 25.0

# ── early warning (forecast.py) ─────────────────────────────────────────
# A room still inside the comfort band but climbing fast gets ONE free rung —
# but only a free one. Shading before the sun lands costs nothing and buys
# minutes of lead; starting a compressor on a forecast would be acting on a
# guess. Same dwell discipline as every other rung, so it cannot chatter.
# °C/hour, not °C/tick. The number is the same physical rate the per-tick form
# described at the old fixed 30 s cadence (0.05 C/tick x 120 ticks/hour), but it
# now survives a cadence change: an event-driven loop can tick 5 s or 30 s apart,
# and a per-tick threshold would silently re-tune itself every time it did.
# Expressed as a FRACTION OF WHAT FULL SUN DOES TO THIS ROOM, not as a fixed
# rate. The old 6.0 C/h was a mechanical conversion of a per-tick constant and
# it silently encoded one climate: with this room's 5.2 C/h solar prior it
# needed roughly full sun to trip, so in a milder place, or a room with a small
# or shaded window, the branch was unreachable and the free rung never spent.
#
# As a fraction it means the same thing everywhere — "most of the warming this
# particular room can experience is happening right now" — and it follows the
# solar term calibrate.py fits from real data instead of a guess made once.
TREND_EARLY_SOLAR_FRACTION = 0.85


# room_model imports from this module, so the prior is restated rather than
# imported. Pinned against it by test, which is the same discipline the
# AWAY/AWAKE string constants get.
SOLAR_GAIN_PRIOR_C_PER_H = 5.2


def trend_early_c_per_h(env=None) -> float:
    """The rate that counts as 'climbing fast' for THIS room.

    Takes the fitted solar term when the caller has an envelope, and the
    engineering prior when it does not.
    """
    solar = getattr(env, "solar_c_per_h", None) or SOLAR_GAIN_PRIOR_C_PER_H
    return TREND_EARLY_SOLAR_FRACTION * solar
TREND_EARLY_PMV_FLOOR = 0.15      # already drifting warm, not merely warm-ish
# Above this the room is genuinely getting warmer, so standing a cooling rung
# down would hand the saving straight back. A tenth of a degree an hour is
# sensor noise; anything more is a direction.
WITHDRAW_TREND_FLOOR_C_PER_H = 0.1
MULTI_PERSON_PMV_FLOOR = 0.25      # small warm drift plus verified extra load
PEOPLE_CONFIDENCE_MIN = 0.50

# ── the occupant's dial (Reading.comfort_c) ─────────────────────────────
# The setpoint is expressed as a PMV offset, not as a second thermostat: the
# ladder keeps ONE comfort scale and one set of thresholds, and the dial slides
# the room along it.  COMFORT_REF_C must stay equal to the twin's default
# setpoint, or an untouched dial would silently re-tune every threshold.
COMFORT_REF_C = 24.0
# A dial is a preference, not an override.  Clamping the shift keeps 16 °C from
# manufacturing a step change that bypasses the ±1.2 escape hatches, and 30 °C
# from suppressing one.
PREFERENCE_MAX_PMV = 0.8

# ── occupancy mode ──────────────────────────────────────────────────────
MODE_EMPTY = "EMPTY"
MODE_OCCUPIED = "OCCUPIED"
MODE_CROWDED = "CROWDED"
MODE_ASLEEP = "ASLEEP"

# A sleeping occupant cannot ask the fan to slow down, so the ladder must not
# put a full draft over a bed.  MED is the top fan rung while asleep — the
# compressor rung stays reachable, it is only reached one rung earlier.
SLEEP_FAN_CAP = FAN_MED

# A sleeping body tolerates a warmer room than PMV admits.  Fanger is validated
# on awake, seated, self-regulating people: someone who can shed a layer or move.
# Asleep, the thermoregulatory set point drops, the occupant is under bedding
# they will adjust without waking, and the discomfort PMV reports is not the
# discomfort that wakes anyone.  So the whole comfort band slides +0.15 PMV warm
# — under half a fan rung (a rung is 0.38 PMV at 27 °C) and about 0.5 °C of room
# temperature.  Enough that a borderline-warm night is left alone instead of
# being solved with machinery; far too small to strand a genuinely hot one, which
# still walks the ladder to the compressor.  The draft cap above is what actually
# protects the sleeper, and it is untouched by this.
SLEEP_BAND_RELAX_PMV = 0.15

# More bodies, more heat: a seated adult is roughly 70 W of sensible gain, so a
# second verified person about doubles the internal load and the room reaches the
# same discomfort in about half the time.  The trigger halves to match.  0.5 min
# is exactly one tick at the 30 s loop rate — the smallest shortening the clock
# can express, and the thresholds themselves are untouched: a crowded room still
# has to be measurably hot, for a measured stretch, before anything is spent.
CROWDED_HOT_TRIGGER_MIN = 0.5


@dataclass(frozen=True)
class ModePolicy:
    """Every consequence of who is in the room, in one row.

    The ladder has ONE comfort scale and ONE set of thresholds; occupancy is
    allowed to move exactly these four knobs and nothing else.  Anything not in
    this table behaves identically in all four rooms — which is the property
    that makes per-mode behaviour reviewable instead of merely present.
    """
    climbs_the_ladder: bool   # False -> handled by _decide_away, never climbs
    fan_ceiling: int          # the highest fan rung this room may hold
    band_offset_pmv: float    # comfort band slides this far warm, in PMV
    hot_trigger_min: float    # minutes above PMV_HOT before a rung is spent


MODE_POLICY = {
    # EMPTY never reaches the drift ladder, so its trigger is inert; it is stated
    # rather than omitted so the row reads as a complete answer to "and then?".
    MODE_EMPTY:    ModePolicy(False, FAN_OFF, 0.0, HOT_TRIGGER_MIN),
    MODE_OCCUPIED: ModePolicy(True, FAN_HIGH, 0.0, HOT_TRIGGER_MIN),
    MODE_CROWDED:  ModePolicy(True, FAN_HIGH, 0.0, CROWDED_HOT_TRIGGER_MIN),
    MODE_ASLEEP:   ModePolicy(True, SLEEP_FAN_CAP, SLEEP_BAND_RELAX_PMV,
                              HOT_TRIGGER_MIN),
}

SUN_GATE_SHUT = 0.06
SUN_GATE_OPEN = 0.12
HUMID_OUTDOOR_RH = 75.0
HUMID_INDOOR_DELTA = 3.0
VENT_DELTA_OPEN = 0.3
VENT_DELTA_SHUT = 1.0


@dataclass
class Reading:
    indoor: float
    indoor_rh: float
    outdoor: float
    outdoor_rh: float
    solar: float                      # 0..1 index
    occupant: str                     # AWAY | AWAKE | ASLEEP
    due_home_min: Optional[float] = None
    # Early-warning only, computed upstream in forecast.py, in °C per HOUR.
    # Optional so existing callers keep working untouched; None means
    # "no forecast available" and the ladder behaves exactly as it always has.
    trend_c_per_h: Optional[float] = None
    people: Optional[int] = None      # camera count; PIR still sets `occupant`
    people_confidence: Optional[float] = None
    # The occupant's setpoint dial, in °C — the same persisted, TTL-bounded,
    # audited value the dashboard writes. None means "no preference expressed",
    # and the ladder behaves exactly as it did before the dial existed.
    comfort_c: Optional[float] = None


@dataclass
class Plan:
    fan: int = FAN_OFF
    window: bool = False
    blinds_shut: bool = False
    ac: bool = False
    light: bool = False
    reason: str = "init"
    # The room this plan was chosen for, in one word, for telemetry and the
    # payload. decide() always overwrites it; the default is the fail-safe
    # reading of an unknown room, because "occupied" is the state that keeps
    # people comfortable.
    mode: str = MODE_OCCUPIED


@dataclass
class Memory:
    dwell: float = 0.0
    hot_min: float = 0.0
    cold_min: float = 0.0
    mild_min: float = 0.0


def actuators_changed(a: Plan, b: Plan) -> bool:
    """True if any physical actuator differs — ignores `reason`, which
    carries the live PMV and so changes every tick even when nothing
    actually moved (§6: this is what an "event" should count)."""
    return (a.fan, a.window, a.blinds_shut, a.ac, a.light) != \
           (b.fan, b.window, b.blinds_shut, b.ac, b.light)


def mode(reading: Reading) -> str:
    """EMPTY | ASLEEP | CROWDED | OCCUPIED — what kind of room this is.

    Pure, and deliberately ordered: the PIR/camera occupant state decides first,
    because "nobody here" and "somebody asleep" are the two states that change
    what the ladder is allowed to do.  A crowd is a property of an awake room,
    and the count is only believed at the same confidence the multi-person lead
    demands — an uncertain second body is not a crowd.
    """
    if reading.occupant == AWAY:
        return MODE_EMPTY
    if reading.occupant == ASLEEP:
        return MODE_ASLEEP
    if (reading.people is not None and reading.people >= 2
            and reading.people_confidence is not None
            and reading.people_confidence >= PEOPLE_CONFIDENCE_MIN):
        return MODE_CROWDED
    return MODE_OCCUPIED


def policy_for(mode_name: str) -> ModePolicy:
    """The row of MODE_POLICY governing this room, fail-safe on an unknown name.

    An unrecognised mode falls back to OCCUPIED for the same reason `Plan.mode`
    defaults to it: of the four rooms, "somebody is here and awake" is the one
    whose behaviour is safe to apply to a room you have misread.
    """
    return MODE_POLICY.get(mode_name, MODE_POLICY[MODE_OCCUPIED])


def _preference_offset(reading: Reading, plan: Plan) -> float:
    """How much warmer the occupant's dial reads than the reference, in PMV.

    Two Fanger solves over the same room, differing only in temperature: one at
    the requested setpoint, one at COMFORT_REF_C.  Their difference is what the
    dial is worth on the comfort scale under this humidity, sun and air movement
    — which is exactly the amount the whole scale should slide, once, before any
    threshold is compared.  A cooler setpoint returns a negative offset, so
    `pmv - offset` reads warmer and the ladder climbs sooner.
    """
    if reading.comfort_c is None:
        return 0.0
    room = (reading.indoor_rh, reading.solar, plan.fan, plan.window,
            plan.blinds_shut, reading.occupant)
    offset = comfort(reading.comfort_c, *room) - comfort(COMFORT_REF_C, *room)
    return clamp(offset, -PREFERENCE_MAX_PMV, PREFERENCE_MAX_PMV)


def _comfort_offset(reading: Reading, plan: Plan) -> float:
    """How much warmer this room's occupant is content to be, in PMV.

    Two things say that, and they say it in the same units and the same
    direction: the dial ("I like it warmer than 24") and the mode ("this one is
    asleep"). They compose by addition — a warm dial and a sleeping occupant are
    two reasons to wait, and both should count — and the SUM is clamped, not each
    part.  That clamp is the load-bearing line: preference and mode together can
    slide the scale by at most PREFERENCE_MAX_PMV, so no combination of them can
    manufacture a ±1.2 step change that the room does not justify, or suppress
    one it does.  Both escape hatches stay exactly where physics put them.
    """
    total = _preference_offset(reading, plan) + policy_for(plan.mode).band_offset_pmv
    return clamp(total, -PREFERENCE_MAX_PMV, PREFERENCE_MAX_PMV)


def _cap_fan_to_mode(plan: Plan) -> Plan:
    """The one comfort rule that outranks the ladder's own choice.

    A HIGH draft is how you wake someone up, and a sleeping occupant cannot ask
    the fan to slow down — so ASLEEP holds the fan at MED. EMPTY holds it at OFF,
    which `_decide_away` has already done; applying the ceiling here too means no
    future branch can leave a fan running in a room with nobody in it. The
    compressor is untouched by all of this: a hot night still gets cooled.
    """
    ceiling = policy_for(plan.mode).fan_ceiling
    if plan.fan <= ceiling:
        return plan
    return replace(plan, fan=ceiling,
                   reason=f"{plan.reason} | {plan.mode.lower()}: "
                          f"fan capped at {ceiling}")


def gates(reading: Reading, plan: Plan) -> dict:
    sun_threshold = SUN_GATE_SHUT if plan.blinds_shut else SUN_GATE_OPEN
    sun = reading.solar > sun_threshold
    humid = (reading.outdoor_rh > HUMID_OUTDOOR_RH and
             (reading.indoor - reading.outdoor) < HUMID_INDOOR_DELTA)
    vent_delta = VENT_DELTA_OPEN if plan.window else VENT_DELTA_SHUT
    vent = (reading.outdoor < reading.indoor - vent_delta) and not humid
    return {"sun": sun, "vent": vent, "humid": humid}


def _step_streaks(pmv: float, memory: Memory, dt_min: float) -> Memory:
    hot_min = memory.hot_min + dt_min if pmv > PMV_HOT else 0.0
    cold_min = memory.cold_min + dt_min if pmv < PMV_COLD else 0.0
    mild_min = memory.mild_min + dt_min if pmv < PMV_MILD else 0.0
    return replace(memory, hot_min=hot_min, cold_min=cold_min, mild_min=mild_min)


def _decide_away(reading: Reading, plan: Plan, gate: dict,
                 vetoed=frozenset()) -> Plan:
    """An empty room gets passive rungs and nothing else.

    Fan straight to the EMPTY ceiling (off — a fan cools a person, not a room,
    so running one for nobody is pure waste), compressor off unless somebody is
    actually due back, and the blinds/window rungs still have to pass the same
    gates as in an occupied room: shade only when the sun is on the glass, vent
    only when outdoor air is genuinely cooler.  A hot empty room is allowed to
    stay hot; it just is not allowed to bake.
    """
    p = replace(plan, fan=policy_for(plan.mode).fan_ceiling)

    due = reading.due_home_min
    warm = reading.indoor > PRECOOL_INDOOR_MIN
    passive_precool = (due is not None and warm and
                       due <= PRECOOL_PASSIVE_DUE_HOME_MIN)
    compressor_precool = (due is not None and warm and
                          due <= PRECOOL_DUE_HOME_MIN)

    if passive_precool:
        # Free rungs, an hour out. Shade an hour before somebody walks in and the
        # compressor has an hour less heat to remove when they do.
        if gate["vent"] and "window" not in vetoed:
            p.window = True
        if gate["sun"] and "blinds" not in vetoed:
            p.blinds_shut = True
        p.reason = f"pre-cool passively: due home in {due:.0f} min"
    if compressor_precool and "ac" not in vetoed:
        p.ac = True
        p.reason = f"pre-cool: due home in {due:.0f} min"
    elif not passive_precool:
        p.ac = False
        p.reason = f"away, eco band {ECO_BAND_LOW:.0f}-{ECO_BAND_HIGH:.0f} C"
    else:
        p.ac = False

    if p.window and not gate["vent"]:
        p.window = False
    if p.blinds_shut and not gate["sun"]:
        p.blinds_shut = False

    if reading.indoor > PASSIVE_BAKE_INDOOR:
        if gate["sun"] and "blinds" not in vetoed:
            p.blinds_shut = True
        elif gate["vent"] and "window" not in vetoed:
            p.window = True
        p.reason = "away, passive rung against bake"
    elif reading.indoor < RELEASE_WINDOW_INDOOR:
        p.window = False

    return p


def _climb_one_rung(reading: Reading, plan: Plan, gate: dict,
                    vetoed=frozenset()) -> Plan:
    """Spend the cheapest rung whose gate holds and which the occupant allows.

    A vetoed rung is SKIPPED, not stalled on. Forbid the fan and the ladder reads
    SHADE -> VENT -> AC; forbid the compressor too and it spends what it can and
    stops. Stalling instead would leave a hot room sitting on a rung it was never
    going to be allowed to climb past.
    """
    p = replace(plan)
    # The climb has to know the mode's fan ceiling (`_cap_fan_to_mode`), or every
    # rung above it would be spent and immediately thrown away, and the
    # compressor rung would never be reached at all.
    fan_ceiling = policy_for(p.mode).fan_ceiling
    if gate["sun"] and not p.blinds_shut and "blinds" not in vetoed:
        p.blinds_shut = True
        p.reason = "climb: blinds shut (free)"
    elif gate["vent"] and not p.window and "window" not in vetoed:
        p.window = True
        p.reason = "climb: window open (free)"
    elif p.fan < fan_ceiling and "fan" not in vetoed:
        p.fan += 1
        p.reason = f"climb: fan -> {p.fan}"
    elif "ac" not in vetoed:
        p.ac = True
        p.reason = "climb: AC on (last resort)"
    else:
        # Everything that could help is forbidden. Say so rather than reporting
        # a climb that did not happen — this is the case where the occupant needs
        # to know the room cannot be held, not to wonder why it is warm.
        p.reason = f"cannot climb: {_vetoed_note(vetoed)}"
    return p


def _reclimb_floor(reading: Reading) -> float:
    """The lowest PMV at which SOMETHING would climb a rung again.

    Not PMV_HOT alone. The multi-person lead climbs from MULTI_PERSON_PMV_FLOOR,
    which is half of it, so a withdrawal judged against the hot line was handing
    the rung straight back to a rule with a lower bar — traced as a cooling room
    hunting fan 1-0-1-0 for two degrees while two people sat in it.
    """
    floors = [PMV_HOT]
    if (reading.people is not None and reading.people >= 2
            and reading.people_confidence is not None
            and reading.people_confidence >= PEOPLE_CONFIDENCE_MIN):
        floors.append(MULTI_PERSON_PMV_FLOOR)
    return min(floors)


def _would_leave_band(reading: Reading, plan: Plan, fan: int) -> bool:
    """Would dropping to `fan` put this room straight back where something
    climbs again?

    `comfort` is pure, so the answer is available before the rung is spent
    rather than one dwell later.
    """
    candidate = comfort(reading.indoor, reading.indoor_rh, reading.solar,
                        fan, plan.window, plan.blinds_shut, reading.occupant)
    pmv = clamp(candidate - _comfort_offset(reading, plan), -3.0, 3.0)
    return pmv > _reclimb_floor(reading)


def _withdraw_one_rung(reading: Reading, plan: Plan, pmv: float, gate: dict) -> Plan:
    p = replace(plan)
    if p.fan > FAN_OFF:
        # DO NOT REMOVE A RUNG THE ROOM IMMEDIATELY NEEDS BACK. A fan cools by
        # moving air, so taking it away raises PMV — and if that lands back over
        # the hot line the next tick climbs straight to the rung just removed.
        # Traced on a slowly warming room: fan hunted 1-0-1-0-1 between 25.8 and
        # 26.3 C, one command per flip. That is not just untidy; the vendor
        # allows 90 commands a day, so hunting is what spends the budget the
        # dashboard then has to apologise for.
        #
        # Withdrawing is meant to bank a saving, and a rung that comes back
        # within the dwell saves nothing. So the fan steps down only when the
        # room still holds without it.
        if _would_leave_band(reading, p, p.fan - 1):
            p.reason = f"holding fan {p.fan}: the room does not hold without it"
            return p
        # AND NOT INTO A WARMING ROOM. Removing a rung can be legal this instant
        # and wrong a minute later: the room is on its way up, so the saving is
        # handed straight back and the fan is commanded twice for nothing.
        #
        # Deliberately NOT trend_early_c_per_h(): that is the "climbing fast"
        # bar (~1.3 C/h) for acting EARLY, and a room warming at 0.6 C/h sailed
        # under it while hunting 1-0-1-0-1. Standing down is a bet that the room
        # holds, so ANY real warming loses the bet — this is a noise floor, not
        # a rate that has to be impressive.
        if (reading.trend_c_per_h is not None
                and reading.trend_c_per_h >= WITHDRAW_TREND_FLOOR_C_PER_H):
            p.reason = (f"holding fan {p.fan}: room still warming "
                        f"{reading.trend_c_per_h:+.1f} C/h")
            return p
        p.fan -= 1
        p.reason = f"withdraw: fan -> {p.fan}"
    elif p.window:
        p.window = False
        p.reason = "withdraw: window shut"
    elif p.blinds_shut and gate["sun"] and pmv < PMV_COLD_HARVEST:
        p.blinds_shut = False
        p.reason = "withdraw: blinds open (harvest sun)"
    return p


def decide(reading: Reading, plan: Plan, memory: Memory, dt_min: float = 0.5,
           advisory=None, vetoed=frozenset()):
    """Pure: (Reading, Plan, Memory) -> (Plan, Memory).

    The ladder itself is `_ladder`. This wrapper does the two things that must
    hold on EVERY branch of it: stamp the occupancy mode on the plan before the
    ladder runs (so each rung, and the telemetry row, reports the room it was
    chosen for), and hold the fan to that mode's ceiling after it has chosen.

    Per-mode contract — the whole of it, and nothing but (see MODE_POLICY):

      mode      fan ceiling   band shift   hot trigger   compressor
      EMPTY     OFF           --           --            pre-cool only
      OCCUPIED  HIGH          0.00 PMV     1.0 min       after the streak
      CROWDED   HIGH          0.00 PMV     0.5 min       after the streak
      ASLEEP    MED          +0.15 PMV     1.0 min       after the streak

    EMPTY leaves the ladder entirely (`_decide_away`: fan off, eco band, passive
    rungs only, still gated). CROWDED additionally spends one cheap rung on a
    verified count, and never the compressor — a count is not a measurement.
    Every other rule — gates, hysteresis, revocation, dwell, the ±1.2 step-change
    escapes, the mild-AC cutoff — is byte-for-byte identical in all four rooms.

    `advisory` is `horizon.Advisory` or None, computed upstream because the
    projection needs the response model's state and the fitted envelope, neither
    of which belongs in a pure function. It arrives as one more input, exactly as
    the forecast trend does. None — no projection, an infeasible room, or models
    that disagree — and this is the same reactive ladder it has always been.
    """
    plan, memory = _ladder(
        reading, replace(plan, mode=mode(reading)), memory, dt_min, advisory,
        frozenset(vetoed))
    return _cap_fan_to_mode(plan), memory


def _touches_vetoed(proposed: Plan, current: Plan, vetoed) -> bool:
    """Would this plan newly engage something the occupant has forbidden?

    Compares against the CURRENT plan rather than against off, so a device
    already running — switched on by hand before the veto was set — is not
    treated as the planner's doing.
    """
    if not vetoed:
        return False
    for name, now, then in (
            ("fan", current.fan, proposed.fan),
            ("ac", current.ac, proposed.ac),
            ("window", current.window, proposed.window),
            ("blinds", current.blinds_shut, proposed.blinds_shut),
            ("light", current.light, proposed.light)):
        # `then > now`, not `then and then != now`. The fan is multi-valued, so
        # the old test treated 2 -> 1 as engaging the device and threw the whole
        # advisory away — losing any free rung bundled with it. A veto forbids
        # asking MORE of something, never less.
        if name in vetoed and int(then) > int(now):
            return True
    return False


def _apply_advisory(plan: Plan, memory: Memory, advisory,
                    vetoed=frozenset()) -> Optional[tuple]:
    """Spend the rung the projection recommends, or return None to fall through.

    Refuses in three cases, each of which is a way a planner could otherwise
    quietly outrank something it must not:

      * no advisory, or one that declined — the normal case, and the safe one.
      * the advisory wants the compressor. `horizon` already refuses to produce
        this and its own tests pin that, so reaching here would mean a future
        change had broken the boundary. Checked anyway: the cost of being wrong
        is 1450 W started on a room that does not exist yet, and a second line of
        defence is cheap insurance against a one-line mistake upstream.
      * dwell has not expired — an actuator floor, not a comfort rule.
    """
    if advisory is None or getattr(advisory, "plan", None) is None:
        return None
    if memory.dwell > 0.0:
        return None
    proposed = advisory.plan
    if bool(getattr(proposed, "ac", False)) and not plan.ac:
        return None
    # Belt and braces on the veto too. Horizon does not generate candidates
    # that use a forbidden device, so arriving here means an upstream change
    # broke that — and a veto that only holds when the planner remembers it
    # is not a veto.
    if _touches_vetoed(proposed, plan, vetoed):
        return None
    if not actuators_changed(proposed, plan):
        return None
    # RESET THE STREAKS, and this line is load-bearing. A streak means "the room
    # has stayed uncomfortable for N minutes DESPITE what we are doing about it",
    # and the planner has just changed what we are doing. Carrying the old count
    # forward makes the ladder escalate on discomfort that was measured under the
    # previous remedy — it double-counts the same minutes.
    #
    # The consequence when this was missing, found by simulating a year rather
    # than by any unit test: the planner spends the cheap rungs early, which is
    # its whole purpose, so a room at its ASLEEP fan ceiling has nothing left when
    # the un-reset streak fires and `_climb_one_rung` goes straight to the
    # compressor. On a winter night the reactive ladder instead spent ten more
    # minutes walking fan 0 -> 1 -> 2, and the room cooled on its own before it
    # ever arrived. Patience had value, and pre-spending discarded it: 10 extra
    # compressor-minutes and 70% more electricity for the day.
    return (replace(proposed, mode=plan.mode,
                    reason=getattr(advisory, "reason", "horizon")),
            replace(memory, dwell=DWELL_MIN, hot_min=0.0, cold_min=0.0))


def _ladder(reading: Reading, plan: Plan, memory: Memory, dt_min: float,
            advisory=None, vetoed=frozenset()):
    policy = policy_for(plan.mode)
    memory = replace(memory, dwell=max(0.0, memory.dwell - dt_min))
    gate = gates(reading, plan)
    pmv = comfort(reading.indoor, reading.indoor_rh, reading.solar,
                  plan.fan, plan.window, plan.blinds_shut, reading.occupant)
    # The dial and the mode slide the scale ONCE, here, before a single threshold
    # is read. Every comparison below — streaks, step changes, floors, harvest —
    # then speaks this room's comfort rather than the textbook's.
    offset = _comfort_offset(reading, plan)
    if offset:
        pmv = clamp(pmv - offset, -3.0, 3.0)
    memory = _step_streaks(pmv, memory, dt_min)

    # ── EMPTY ROOM ──────────────────────────────────────────────────────
    # FIRST, and it has to be. Below this line live the ±1.2 step-change escapes,
    # which fire every rung at once on a hot reading — correct for a person who
    # just walked into a baking room, catastrophic for a room with nobody in it,
    # where "baking" is the normal afternoon state and the answer is shade, not
    # 1500 W. Nothing that switches machinery on may be read before this branch.
    if not policy.climbs_the_ladder:
        return _decide_away(reading, plan, gate, vetoed), memory

    # ── OCCUPIED ────────────────────────────────────────────────────────
    plan = replace(plan)

    if plan.ac and memory.mild_min >= MILD_AC_OFF_MIN:
        plan.ac = False
        plan.reason = "AC off: comfortable for 3 min straight"
        return plan, memory

    # REVOKE — a rung is only allowed to exist while its gate still holds.
    if plan.window and not gate["vent"]:
        plan.window = False
    if plan.blinds_shut and not gate["sun"]:
        plan.blinds_shut = False

    # STEP CHANGE — someone walked into an already-hot/cold room.
    if pmv > PMV_STEP_HOT:
        # Every rung at once — except the ones the occupant has forbidden. This
        # is where the veto is worth the most and costs the most: the room is
        # genuinely uncomfortable and we are declining to fix it the fastest way
        # because we were told not to. Overriding here would make the veto a
        # suggestion, and a suggestion is not worth having.
        if gate["sun"] and "blinds" not in vetoed:
            plan.blinds_shut = True
        if gate["vent"] and "window" not in vetoed:
            plan.window = True
        if "fan" not in vetoed:
            plan.fan = FAN_HIGH
        if "ac" not in vetoed:
            plan.ac = True
        plan.reason = f"step change: PMV {pmv:+.2f} > {PMV_STEP_HOT}, all rungs at once"
        if vetoed:
            plan.reason += f" | {_vetoed_note(vetoed)}"
        return plan, memory

    if pmv < PMV_STEP_COLD:
        plan.ac = False
        plan.fan = FAN_OFF
        plan.window = False
        plan.blinds_shut = False
        plan.reason = f"step change: PMV {pmv:+.2f} < {PMV_STEP_COLD}, withdraw all at once"
        return plan, memory

    # ── THE PLANNER ─────────────────────────────────────────────────────
    # Placed here, and the position is the whole design. Above this line are the
    # reactive safety nets — the empty-room branch, the hair-trigger AC cutoff,
    # gate revocation, and the ±1.2 step-change escapes. Those must outrank a
    # projection: somebody who just walked into a room that has been baking all
    # afternoon needs every rung this instant, and a planner reasoning about the
    # next hour would spend one cheap rung and call it handled.
    #
    # Below this line is the slow reactive climb — one rung per 5 minutes, after a
    # measured streak. The planner outranks THAT, because it is strictly better
    # informed: it knows what its own action does to the next hour, where the
    # climb has to spend a rung and wait to find out.
    #
    # So: measured emergencies first, foresight second, patience last.

    # MULTI-PERSON LEAD — count does not change PMV: PMV describes one
    # person's environment.  It does tell us that extra heat is entering the
    # room.  When already drifting warm, advance exactly one cheap rung.  AC
    # remains measurement-driven and can never start from a count alone.
    #
    # ABOVE THE PLANNER, and that is a correction rather than a preference.
    # There is one dwell counter and exactly one branch may spend a rung on the
    # tick it reaches zero. The planner was read first, so on almost every such
    # tick it took the slot and set dwell back to five minutes; this branch
    # additionally requires dwell == 0 and therefore never saw an open window.
    # Measured over 20,902 logged ticks before the move: 200 of them had a
    # verified count of two or more AND a PMV above the floor AND no hot streak
    # running — every stated precondition — and the branch fired on none. After
    # the move, 200 such ticks and 30 firings.
    #
    # WHAT THE CEILING ACTUALLY IS, because the raw count means nothing without
    # it. Exactly one branch may spend a rung per dwell, so this one can win at
    # most dt/DWELL_MIN of the ticks it is eligible for. Replayed against a room
    # parked in the lead's own window it fires on 10.4% of eligible ticks at a
    # 0.5 min dt with NO planner present at all — the ceiling to the decimal — so
    # the planner is not what holds it down. What the planner changes is how many
    # ticks are ELIGIBLE (241 down to 11 across a ten-hour replay), because it
    # acts and the room stops sitting in the window. Fewer chances to act on a
    # room somebody already fixed is the system working.
    #
    # AND dt IS NOT 0.5 ON THE BOARD. The loop is event-driven: 40 consecutive
    # live ticks measured a median gap of 9.4 s, not 30. That puts the ceiling
    # near 3%, so 30 of 200 is roughly five times more than the floor permits —
    # which is not a branch doing well, it is a counting error. See below.
    #
    # And COUNT FIRINGS BY A CHANGE OF REASON, never by matching the reason
    # string per tick. A tick that spends nothing returns the plan untouched,
    # reason and all, so one firing reads as up to DWELL_MIN/dt consecutive ones:
    # in the 120-tick scenario `test_lead_fires_across_repeated_ticks_with_a_live_planner`
    # replays, the string match counts 20 where the truth is 2. At the board's
    # real 9.4 s cadence the inflation is DWELL_MIN/dt ~ 32x, which would make
    # the 30 above roughly ONE actual firing. Re-derive it from reason CHANGES
    # before concluding anything about this branch either way.
    #
    # It belongs above for the same reason the reactive nets do: it is driven by
    # MEASUREMENT (a verified count, a measured PMV), it is capped at a single
    # rung, and it can never start the compressor. A projection reasoning about
    # the next hour is the better guide to what the room will need; it is not the
    # better guide to two people who are in the room now. The early-warning
    # branch below is deliberately NOT moved — that one is a forecast, and
    # against a forecast the planner really is better informed.
    multi_person = (reading.people is not None and reading.people >= 2 and
                    reading.people_confidence is not None and
                    reading.people_confidence >= PEOPLE_CONFIDENCE_MIN)
    if (multi_person and pmv >= MULTI_PERSON_PMV_FLOOR and
            memory.hot_min < policy.hot_trigger_min and memory.dwell == 0.0):
        candidate = _climb_one_rung(reading, plan, gate, vetoed)
        # `candidate.ac and not plan.ac` — did the CLIMB reach for the
        # compressor, not is the compressor on. Testing the inherited value
        # meant a crowded warm room already under AC never got its free rung.
        if not (candidate.ac and not plan.ac) and actuators_changed(
                candidate, plan):
            candidate.reason = (f"multi-person lead: {reading.people} people, "
                                f"PMV {pmv:+.2f} | {candidate.reason}")
            return candidate, replace(memory, dwell=DWELL_MIN)

    spent = _apply_advisory(plan, memory, advisory, vetoed)
    if spent is not None:
        return spent

    # EARLY WARNING — the room is comfortable but heading out of band.
    # Acts one dwell before hot_min would have fired, and only on rungs that
    # cost nothing: blinds, and venting when outdoor air is already cooler.
    # The fan and the compressor still wait for the PMV streak, because a
    # forecast is not a measurement and 1500 W should never run on a guess.
    if (reading.trend_c_per_h is not None
            and reading.trend_c_per_h >= trend_early_c_per_h()
            and pmv >= TREND_EARLY_PMV_FLOOR
            and memory.hot_min < policy.hot_trigger_min   # not yet the ladder's job
            and memory.dwell == 0.0):
        if not plan.blinds_shut and gate["sun"] and "blinds" not in vetoed:
            plan = replace(plan, blinds_shut=True,
                           reason=f"early: heating {reading.trend_c_per_h:+.1f} C/h, "
                                  f"shade before PMV leaves the band")
            return plan, replace(memory, dwell=DWELL_MIN)
        if not plan.window and gate["vent"] and "window" not in vetoed:
            plan = replace(plan, window=True,
                           reason=f"early: heating {reading.trend_c_per_h:+.1f} C/h, "
                                  f"vent while outdoor is still cooler")
            return plan, replace(memory, dwell=DWELL_MIN)

    # NORMAL DRIFT — cheapest fix first, rate-limited to one rung / dwell.
    # The streak the room must serve before ANY rung is spent is the mode's, so a
    # crowded room reacts a tick sooner than a single occupant. What it does not
    # get is a cheaper rung order or a shortcut to the compressor: the ladder
    # below is the same ladder, climbed in the same order, one rung per dwell.
    if memory.hot_min >= policy.hot_trigger_min and memory.dwell == 0.0:
        plan = _climb_one_rung(reading, plan, gate, vetoed)
        memory = replace(memory, dwell=DWELL_MIN)
    else:
        cold_trigger = COLD_TRIGGER_FAST_MIN if pmv < PMV_COLD_FAST else COLD_TRIGGER_SLOW_MIN
        if memory.cold_min >= cold_trigger and memory.dwell == 0.0:
            plan = _withdraw_one_rung(reading, plan, pmv, gate)
            memory = replace(memory, dwell=DWELL_MIN)

    return plan, memory
