"""Honest AC digital twin driven by the room's real sensor boundary.

The twin never mutates a SensorFrame and never presents modeled values as
measurements. It is a first-order resistance/capacitance room model with an
additional first-order cooling response, so cooling ramps up and decays.

The appliance itself is a named machine rather than an anonymous "the AC":
the default profile is an LG 1.5-ton DUAL Inverter split (Q19 class), and its
published numbers drive both the electrical estimate and the cooling rate. A
DUAL Inverter does not cycle on and off, it modulates, so the model runs three
powered bands - full rated pull-down, a proportional taper, and a low-power
hold - and reports the electricity each band actually draws.

Long-run contract, at any horizon and any tick up to ``max_step_s``:

* Powered at a reachable setpoint the room converges inside the hour, then
  holds within 0.3 °C indefinitely at the inverter floor.
* The settled temperature comes from the hold trim, not from the tick rate.
* Off is exactly zero watts from that instant: residual cooling decays inside
  the coil's own stored cold rather than at the compressor's rated capacity, and
  the room rebounds first-order to the sensed baseline without passing it.
* While cooling, the model never reads warmer than the sensed baseline nor
  more than ``max_sensor_delta_c`` below it.
* Watt-hours are the trapezoid of reported draw, an unobserved gap re-anchors
  and is billed nothing, and a restart resumes the same trajectory.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import asdict, dataclass, replace
from typing import Optional


PROVENANCE = "digital_twin_inverter_v2"

# Inverter modulation bands, measured as the gap between the modeled room
# temperature and the requested setpoint.
PULL_DOWN_GAP_C = 0.75
HOLD_GAP_C = 0.25
PHASE_OFF = "off"
PHASE_PULL_DOWN = "pull_down"
PHASE_TAPER = "taper"
PHASE_HOLD = "hold"

# A running compressor owns the room, so the unconditioned sensor baseline is
# only a weak reminder while power is on. Without this the anchor fights the
# compressor and a long run stalls well above its setpoint.
POWERED_ANCHOR_WEIGHT = 0.3

# Inside the hold band the machine has spare capacity, so its own controller
# spends that spare capacity closing the last fraction of a degree instead of
# freezing the room wherever the previous tick happened to land. The trim is a
# time constant, which keeps the settled temperature independent of how often
# the control loop happens to call step().
HOLD_TRIM_TAU_H = 0.25

# "This target is out of reach" needs a sustained stall, not one flat tick.
ADVISORY_GAP_C = 0.5
ADVISORY_RATE_C_H = -0.05
ADVISORY_HOLD_S = 900.0
ADVISORY_TEMPLATE = ("AC is at full capacity — the room may not reach "
                     "{setpoint_c:.1f}°C. Consider a higher target.")


@dataclass(frozen=True)
class AcProfile:
    """Published data for one specific appliance, not a generic AC."""

    name: str = "LG 1.5T dual-inverter"
    # Tons is how an air conditioner is actually sold and argued about, so the
    # card states it rather than making a reader convert 5 kW in their head.
    # Derived, not typed twice: 1 ton of refrigeration is 3.517 kW, so the 5.0 kW
    # nameplate below IS 1.42 T — quoting "1.5T" from the model name while the
    # physics ran on 5.0 kW would be two different appliances on one card.
    rated_cooling_kw: float = 5.0
    rated_power_w: float = 1450.0
    min_power_w: float = 430.0
    iseer: float = 5.2

    KW_PER_TON = 3.517

    @property
    def tons(self) -> float:
        return self.rated_cooling_kw / self.KW_PER_TON

    @property
    def label(self) -> str:
        """What this appliance is, in the units a person buys it in."""
        return f"{self.name} · {self.tons:.2f} ton · {self.rated_cooling_kw:.1f} kW"

    @classmethod
    def from_env(cls) -> "AcProfile":
        return cls(
            name=os.environ.get("BREEZEIQ_AC_MODEL_NAME",
                                "LG 1.5T dual-inverter"),
            # Either say the capacity in kW, or say it in tons and let this
            # convert — never both, so the card and the physics cannot disagree.
            # The default stays the named appliance's real 5.0 kW nameplate,
            # which is 1.42 refrigeration tons; "1.5T" on an Indian split is
            # marketing tonnage, and quoting it while modelling 5.0 kW would put
            # two different machines on one card.
            rated_cooling_kw=float(os.environ.get(
                "BREEZEIQ_AC_COOLING_KW",
                str(float(os.environ["BREEZEIQ_AC_TONS"]) * cls.KW_PER_TON)
                if os.environ.get("BREEZEIQ_AC_TONS") else "5.0")),
            rated_power_w=float(os.environ.get("BREEZEIQ_AC_RATED_W", "1450")),
            min_power_w=float(os.environ.get("BREEZEIQ_AC_MIN_W", "430")),
        ).validated()

    def validated(self) -> "AcProfile":
        if not str(self.name).strip():
            raise ValueError("ac profile name is required")
        if not 0.5 <= self.rated_cooling_kw <= 20:
            raise ValueError("rated_cooling_kw must be from 0.5 to 20")
        if not 100 <= self.rated_power_w <= 10000:
            raise ValueError("rated_power_w must be from 100 to 10000")
        if not 0 < self.min_power_w <= self.rated_power_w:
            raise ValueError("min_power_w must be positive and at most rated")
        if not 1 <= self.iseer <= 12:
            raise ValueError("iseer must be from 1 to 12")
        return replace(self, name=str(self.name).strip())

    def modulation(self, gap_c: float, power: bool) -> tuple[float, str]:
        """Electrical draw and modulation band for a distance to setpoint."""
        if not power:
            return 0.0, PHASE_OFF
        if gap_c > PULL_DOWN_GAP_C:
            return self.rated_power_w, PHASE_PULL_DOWN
        if gap_c > HOLD_GAP_C:
            share = (gap_c - HOLD_GAP_C) / (PULL_DOWN_GAP_C - HOLD_GAP_C)
            headroom = self.rated_power_w - self.min_power_w
            return self.min_power_w + headroom * share, PHASE_TAPER
        return self.min_power_w, PHASE_HOLD

    def phase_for_watts(self, watts: float, power: bool) -> str:
        """Recover the band from a persisted draw so restarts keep context."""
        if not power or watts <= 0:
            return PHASE_OFF
        if watts >= self.rated_power_w:
            return PHASE_PULL_DOWN
        return PHASE_HOLD if watts <= self.min_power_w else PHASE_TAPER


@dataclass(frozen=True)
class RoomGeometry:
    """The actual room, so the appliance's behaviour is derived rather than tuned.

    A 1.5-ton machine does not cool every room at the same rate; it cools THIS
    one at a rate set by how much air and fabric it has to chill. Stating the
    dimensions once lets the pull-down rate, the reachable depth and the
    dehumidification all fall out of the same three numbers — and lets the same
    model describe a different room by changing them.
    """
    width_m: float = 3.6
    depth_m: float = 3.6
    height_m: float = 3.0

    @property
    def floor_area_m2(self) -> float:
        return self.width_m * self.depth_m

    @property
    def volume_m3(self) -> float:
        return self.floor_area_m2 * self.height_m

    @property
    def capacity_j_per_c(self) -> float:
        """Effective thermal mass — the air AND the fabric that follows it.

        Air alone is about 1.2 kg/m³ × 1005 J/kg·K, roughly 46 kJ/°C for this
        room, which would let a 5 kW machine drop it 100 °C in a minute. Real
        rooms do not behave that way because plaster, floor slab and furniture
        exchange heat with the air on the same timescale, and it is that combined
        mass an AC actually has to move.

        EFFECTIVE_CAPACITY_J_PER_M3_C is stated per cubic metre so the figure
        scales with the room instead of being re-tuned for each one. Its value is
        set to reproduce this project's own long-standing 10 °C/h pull-down at
        rated capacity for the 3.6 × 3.6 × 3.0 m reference room — so the change
        is a re-derivation of a number that was already there, not a new guess.
        """
        return self.volume_m3 * EFFECTIVE_CAPACITY_J_PER_M3_C

    def validated(self) -> "RoomGeometry":
        for name, value in (("width_m", self.width_m), ("depth_m", self.depth_m),
                            ("height_m", self.height_m)):
            if not 0.5 <= float(value) <= 30.0:
                raise ValueError(f"{name} must be from 0.5 to 30 metres")
        return self


# Air plus the wall, floor and furniture mass that participates on an hour
# timescale. Calibrated so the reference room reproduces 10 °C/h at 5 kW:
#   C = 5000 W / (10 °C/h / 3600) = 1.8 MJ/°C over 38.88 m³
EFFECTIVE_CAPACITY_J_PER_M3_C = 46_300.0

# What fraction of an air conditioner's capacity does sensible cooling — the part
# that lowers temperature — with the remainder condensing moisture out of the air.
# 0.75 is typical for a residential split in a humid climate, and it is why a room
# under AC feels drier as well as cooler. That matters here rather than being a
# detail: humidity is a first-class PMV input, so the latent quarter of the
# machine's output shows up as real comfort the temperature alone does not explain.
SENSIBLE_HEAT_RATIO = 0.75

# Where a running air conditioner settles the relative humidity of a room, and how
# quickly. The coil is far below dew point, so it condenses continuously and RH
# falls toward this figure regardless of where it started; switch the machine off
# and moisture returns from outside and from the occupants. Latent response is
# slower than sensible, which is why a room feels cool before it feels dry.
AC_SETTLED_RH = 45.0
RH_PULL_TAU_MIN = 20.0
RH_RECOVER_TAU_MIN = 45.0

# Cold stored in the indoor coil and its refrigerant charge — the only cooling
# left in the room once the switch opens, because the compressor and the blower
# share that plug. Roughly 3.5 kg of aluminium and copper (0.9 kJ/kg·K) plus a
# 0.4 kg charge (1.6 kJ/kg·K), subcooled about 12 K below the room:
# (3.5 x 0.9 + 0.4 x 1.6) x 12 ~ 45 kJ, rounded up so the residual is if
# anything overstated rather than argued away.
COIL_STORED_J = 60_000.0


@dataclass(frozen=True)
class TwinConfig:
    room_tau_h: float = 5.0
    sensor_anchor_tau_h: float = 2.0
    # Degrees per hour the profile removes at rated draw. It has to match the
    # room the other constants describe: room_tau_h of 5 h with this envelope
    # is a bedroom of roughly 0.5 kWh/°C, so a 5 kW machine moves it 10 °C/h.
    # Anything much lower makes the same room unholdable at its own setpoint.
    # None means DERIVE from the room and the appliance, which is the normal
    # case. A number here is an explicit override and wins — stated as a
    # separate value rather than a different default, so nothing can silently
    # disagree with the geometry about how fast this machine cools.
    cooling_rate_c_h: Optional[float] = None
    cooling_ramp_min: float = 6.0
    cooling_decay_min: float = 12.0
    default_setpoint_c: float = 24.0
    max_step_s: float = 120.0
    reset_gap_s: float = 300.0
    # A backstop, not the reachability limit. The limit is now derived from the
    # appliance against the room (`reachable_depth_c`); this only bounds how far a
    # modelled number may travel from the thermometer when the derived figure is
    # itself implausible, and it is deliberately generous enough that the ordinary
    # cool-reach-setpoint-stop cycle never touches it.
    max_sensor_delta_c: float = 15.0
    room: RoomGeometry = RoomGeometry()
    profile: AcProfile = AcProfile()

    @property
    def ua_w_per_c(self) -> float:
        """Envelope conductance, from the room's own mass and time constant.

        tau = C / UA is the definition of a first-order thermal system, so the
        two numbers this config already carried imply the third. Deriving it
        keeps the envelope, the pull-down rate and the reachable depth
        consistent with one another instead of being three constants that can
        drift apart.
        """
        return self.room.capacity_j_per_c / (self.room_tau_h * 3600.0)

    @property
    def effective_cooling_rate_c_h(self) -> float:
        """The override if one was given, else the room's own physics."""
        if self.cooling_rate_c_h is not None:
            return float(self.cooling_rate_c_h)
        return self.derived_cooling_rate_c_h

    @property
    def derived_cooling_rate_c_h(self) -> float:
        """How fast THIS machine cools THIS room, at rated capacity.

        Watts over thermal mass. A 1.5-ton unit empties a small bedroom quickly
        and a hall slowly, and that difference is the whole reason this is
        computed rather than configured.
        """
        # Only the SENSIBLE share lowers temperature; the rest condenses water
        # out of the air. That constant was defined and documented directly
        # above and then referenced nowhere, so this rate — and reachable_depth_c
        # with it — claimed the whole nameplate for cooling and ran ~33% fast.
        #
        # Measured against the board over six hours, powered runs had the model
        # falling 6.2-6.8 C/h while the room itself moved -0.9 to +0.9 C/h. Some
        # of that gap is unavoidable and honest: there is no appliance on the
        # switch, so the room cannot follow a model of one. The part that was
        # ours to fix is this.
        watts = self.profile.rated_cooling_kw * 1000.0 * SENSIBLE_HEAT_RATIO
        return watts / self.room.capacity_j_per_c * 3600.0

    @property
    def reachable_depth_c(self) -> float:
        """How far below ambient this machine can actually hold this room.

        Equilibrium is where the compressor's output equals the heat leaking in:
        Q_rated = UA · ΔT, so ΔT_max = Q_rated / UA. For the reference room that
        is far more than any sensible setpoint asks for, which is the physically
        correct answer — a 5 kW machine is not what stops a 13 m² room reaching
        24 °C. Enlarge the room and UA rises with its mass, the reachable depth
        falls, and the model becomes capacity-limited on its own.

        This replaces a flat 6 °C clamp that stalled a 33 °C room at 27 °C and
        raised an out-of-reach advisory for a target the appliance could plainly
        have met.
        """
        watts = self.profile.rated_cooling_kw * 1000.0 * SENSIBLE_HEAT_RATIO
        return watts / max(1e-6, self.ua_w_per_c)

    @property
    def coast_rate_c_h(self) -> float:
        """Cooling still available from a switched-OFF appliance.

        An open switch stops the compressor and the blower together, so what is
        left is `COIL_STORED_J` against this room's thermal mass, spread over
        the decay constant the residual takes to give it up. For the reference
        room that is 0.03 °C of cooling at 0.17 °C/h — two orders of magnitude
        under the compressor's own rate, which is the point: an unpowered
        appliance cools a room by hundredths of a degree, not by degrees.
        """
        stored_c = COIL_STORED_J / self.room.capacity_j_per_c
        return stored_c / (self.cooling_decay_min / 60.0)

    @classmethod
    def from_env(cls) -> "TwinConfig":
        return cls(
            room_tau_h=float(os.environ.get("BREEZEIQ_TWIN_ROOM_TAU_H", "5")),
            sensor_anchor_tau_h=float(os.environ.get(
                "BREEZEIQ_TWIN_SENSOR_ANCHOR_TAU_H", "2")),
            # Absent means derive. Only an operator who has actually measured
            # their machine should be pinning this.
            cooling_rate_c_h=(
                float(os.environ["BREEZEIQ_TWIN_COOLING_C_PER_H"])
                if "BREEZEIQ_TWIN_COOLING_C_PER_H" in os.environ else None),
            cooling_ramp_min=float(os.environ.get(
                "BREEZEIQ_TWIN_RAMP_MIN", "6")),
            cooling_decay_min=float(os.environ.get(
                "BREEZEIQ_TWIN_DECAY_MIN", "12")),
            default_setpoint_c=float(os.environ.get(
                "BREEZEIQ_TWIN_SETPOINT_C", "24")),
            profile=AcProfile.from_env(),
        ).validated()

    def validated(self) -> "TwinConfig":
        if not 0.25 <= self.room_tau_h <= 48:
            raise ValueError("room_tau_h must be from 0.25 to 48")
        if not 0.25 <= self.sensor_anchor_tau_h <= 48:
            raise ValueError("sensor_anchor_tau_h must be from 0.25 to 48")
        if self.cooling_rate_c_h is not None and not (
                0.1 <= self.cooling_rate_c_h <= 20):
            raise ValueError("cooling_rate_c_h must be from 0.1 to 20")
        self.room.validated()
        if not 0.5 <= self.cooling_ramp_min <= 60:
            raise ValueError("cooling_ramp_min must be from 0.5 to 60")
        if not 0.5 <= self.cooling_decay_min <= 120:
            raise ValueError("cooling_decay_min must be from 0.5 to 120")
        if not 16 <= self.default_setpoint_c <= 30:
            raise ValueError("default_setpoint_c must be from 16 to 30")
        if self.max_step_s <= 0 or self.reset_gap_s < self.max_step_s:
            raise ValueError("digital twin time bounds are invalid")
        if not 1 <= self.max_sensor_delta_c <= 15:
            raise ValueError("max_sensor_delta_c must be from 1 to 15")
        return replace(self, profile=self.profile.validated())


@dataclass(frozen=True)
class AcStep:
    """One advance of the response model. Pure: no clock, no database, no state.

    Extracted so the same arithmetic serves two callers that must never
    disagree: `AcDigitalTwin.step()`, which advances the live room once per tick
    and persists the result, and the planner's rollout, which advances a
    hypothetical room a dozen times to ask "what would an hour of this cost?".
    A second copy of these equations would drift from the first, and then the
    number the planner optimised against would not be the number the room went
    on to produce.
    """
    modeled_c: float
    cooling_effect: float
    estimated_watts: float
    interval_wh: float
    phase: str
    net_rate_c_h: float
    modeled_rh: Optional[float] = None
    # The two halves of `net_rate_c_h`, kept apart so a caller can say WHY the
    # room moved: the envelope pulling it back toward the measured baseline,
    # and the appliance pulling it toward the setpoint.
    drift_c_h: float = 0.0
    cooling_c_h: float = 0.0
    # The coldest value this step was allowed to reach, so a caller can tell a
    # model that stalled from one that was bounded by the room's reachability.
    floor_c: float = 0.0


def advance(*, modeled_c: float, cooling_effect: float, previous_watts: float,
            baseline_c: float, outdoor_c: Optional[float], power: bool,
            setpoint_c: float, dt_s: float, config: "TwinConfig",
            profile: AcProfile, modeled_rh: Optional[float] = None,
            baseline_rh: Optional[float] = None) -> AcStep:
    """Advance the modelled room by `dt_s` under one power state.

    `baseline_c` is the counterfactual no-AC room: what the temperature would be
    with the compressor off. Live, that is the DHT22 reading — the sensor is
    measuring a room the AC is not currently holding, so it is the honest
    baseline and the model is a delta on top of it. In a forward projection it is
    instead the envelope model's own prediction, because holding this hour's
    starting temperature fixed for an hour would describe a room with no sun, no
    outdoor swing and nobody in it.

    That substitution is the whole reason this function takes a baseline rather
    than reading a sensor: the AC response is identical either way, and only the
    thing it is responding to changes.
    """
    dt_s = max(0.0, min(dt_s, config.max_step_s))
    dt_h = dt_s / 3600.0
    watts, phase = profile.modulation(modeled_c - setpoint_c, power)

    # ``cooling_effect`` is the compressor's normalized thermal output. It
    # chases the current modulation level while powered and decays to zero once
    # power stops, which keeps residual cooling alive on an interval that draws
    # no electricity at all.
    effect_tau_s = 60.0 * (config.cooling_ramp_min if power
                           else config.cooling_decay_min)
    target_effect = watts / profile.rated_power_w if power else 0.0
    effect = target_effect + (cooling_effect - target_effect) * (
        math.exp(-dt_s / effect_tau_s))

    # Outdoor temperature changes how quickly the room tends back toward the
    # baseline, but cannot become a second equilibrium that leaves the model
    # permanently colder or hotter than that baseline.
    outdoor_gradient = abs(outdoor_c - baseline_c) if outdoor_c is not None else 0.0
    envelope_factor = 1.0 + min(outdoor_gradient / 12.0, 0.75)
    free_exchange = ((baseline_c - modeled_c)
                     / config.room_tau_h * envelope_factor)
    anchor_weight = POWERED_ANCHOR_WEIGHT if power else 1.0
    sensor_correction = anchor_weight * (
        baseline_c - modeled_c) / config.sensor_anchor_tau_h
    drift = free_exchange + sensor_correction
    # Watts over this room's thermal mass, not a configured constant. The same
    # machine empties a bedroom quickly and a hall slowly, and the projection has
    # to know the difference or it prices an hour of cooling for the wrong room.
    capacity = config.effective_cooling_rate_c_h * effect
    if not power:
        # A DEAD APPLIANCE MAY NOT OUT-COOL ITS OWN COIL.
        #
        # `cooling_effect` decays over `cooling_decay_min` once the switch
        # opens, and it was spending the COMPRESSOR's rated capacity the whole
        # way down: switching off mid pull-down went on to cool the reference
        # room another 0.9 °C with no power reaching the machine. What is
        # actually left is the cold stored in the coil, which `coast_rate_c_h`
        # measures against this room's mass — hundredths of a degree.
        capacity = min(capacity, config.coast_rate_c_h)
    # Holding at target means matching the room's heat gain plus whatever closes
    # the remaining error, bounded by what the machine can deliver at its floor.
    # Never negative heating: below setpoint the compressor simply stops taking
    # heat out and the room drifts back up on its own.
    hold_trim = drift + (modeled_c - setpoint_c) / HOLD_TRIM_TAU_H
    cooling = (_clamp(hold_trim, 0.0, capacity)
               if phase == PHASE_HOLD else capacity)
    net_rate_c_h = drift - cooling
    projected = modeled_c + dt_h * net_rate_c_h
    # How deep this machine can hold this room, from the appliance against the
    # envelope rather than a flat constant. For a small room the physics says the
    # setpoint is comfortably reachable, which is why the old 6 °C clamp stalling
    # a 33 °C room at 27 °C was wrong; enlarge the room and the same expression
    # becomes the binding limit on its own. `max_sensor_delta_c` survives only as
    # a backstop against an implausible derived figure.
    depth = min(config.reachable_depth_c, config.max_sensor_delta_c)
    # The absolute 10/40 °C sanity bounds yield to the measured baseline: the
    # sensor's own plausible range runs 5..55, and a model forbidden to sit AT
    # a measured 45 °C (or 6 °C) room with the machine off would teleport away
    # from the very thermometer it anchors to.
    floor_c = max(min(10.0, baseline_c), baseline_c - depth)
    projected = _clamp(projected, floor_c,
                       min(max(40.0, baseline_c),
                           baseline_c + config.max_sensor_delta_c))

    # ── moisture ────────────────────────────────────────────────────────
    # A coil below dew point condenses whether or not anybody asked it to, so a
    # room under AC gets drier as well as cooler, and stops being dry once the
    # machine stops. Modelled because RH is a first-class PMV term: the latent
    # quarter of the machine's output is comfort the temperature alone does not
    # account for, and leaving it out would make the compressor look worse than
    # it is. Dehumidification tracks the cooling EFFECT, not merely the power, so
    # residual moisture removal decays with the coil rather than stopping dead.
    rh_out = modeled_rh
    if rh_out is not None:
        if power and effect > 0.01:
            tau_s = 60.0 * RH_PULL_TAU_MIN / max(0.05, effect)
            target = AC_SETTLED_RH
        elif baseline_rh is not None:
            tau_s = 60.0 * RH_RECOVER_TAU_MIN
            target = baseline_rh
        else:
            tau_s, target = None, None
        if tau_s:
            rh_out = target + (rh_out - target) * math.exp(-dt_s / tau_s)
            rh_out = _clamp(rh_out, 15.0, 100.0)

    # Draw slides between bands, so the interval is a trapezoid rather than a
    # flat rate. An unpowered interval stays exactly zero: residual cooling must
    # never become phantom electrical usage.
    interval_wh = 0.0 if watts <= 0 else (
        (previous_watts + watts) / 2.0 * dt_s / 3600.0)

    return AcStep(
        modeled_c=projected, cooling_effect=effect, estimated_watts=watts,
        interval_wh=interval_wh, phase=phase, modeled_rh=rh_out,
        drift_c_h=drift, cooling_c_h=cooling, floor_c=floor_c,
        net_rate_c_h=((projected - modeled_c) / dt_h if dt_h > 0 else 0.0))


@dataclass(frozen=True)
class TwinState:
    at: float
    modeled_c: Optional[float]
    cooling_effect: float
    sensed_indoor_c: Optional[float]
    sensed_outdoor_c: Optional[float]
    power: bool
    setpoint_c: float
    mode: str
    status: str
    estimated_watts: float
    interval_wh: Optional[float]
    phase: str = PHASE_OFF
    advisory: Optional[str] = None
    stall_since: Optional[float] = None
    ac_model: Optional[str] = None
    provenance: str = PROVENANCE
    # What the coil did to the room's moisture, and the sensor value it is
    # returning toward once the machine stops.
    modeled_rh: Optional[float] = None
    sensed_indoor_rh: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TwinExplanation:
    """Why the modelled room is the number it is, measured kept apart from modelled.

    No air conditioner is wired to the switch, so every temperature this module
    reports is arithmetic rather than observation, and the card that shows it has
    to be auditable on sight: the readings and switch state that went in, the
    rate that was applied, the value that came out. Hence separate sections
    instead of one flat blob — `measured` holds only what a device reported,
    `inputs` what the model was asked to run on, `modelled` only what this
    module computed, and `step` the arithmetic between them.

    `measured` values are Optional and stay None when the sensor was not worth
    believing, so a reader sees the channel as unavailable rather than as a zero.
    `step` is None on any tick that did not advance the model — a first tick, an
    unobserved gap, a sensor outage — for the same reason.
    """

    measured: dict
    inputs: dict
    step: Optional[dict]
    modelled: dict

    def as_dict(self) -> dict:
        return asdict(self)


class AcDigitalTwin:
    """Persistent thermal scenario engine owned by the UNO Q control loop."""

    def __init__(self, telemetry, config: Optional[TwinConfig] = None):
        self.telemetry = telemetry
        self.config = config or TwinConfig.from_env()
        self.profile = self.config.profile
        self.state = self._restore()
        # Published for the workbench, not persisted: it describes one tick, and
        # a restored state has no tick behind it to describe.
        self.last_explanation: Optional[TwinExplanation] = None

    def _restore(self) -> Optional[TwinState]:
        row = self.telemetry.ac_twin_state()
        if not row:
            return None
        try:
            at = float(row["at"])
            watts = float(row.get("estimated_watts") or 0.0)
            power = bool(row["power"])
            advisory = row.get("advisory") or None
            return TwinState(
                at=at, modeled_c=_optional_float(row["modeled_c"]),
                cooling_effect=_clamp(float(row["cooling_effect"]), 0.0, 1.0),
                sensed_indoor_c=_optional_float(row["sensed_indoor_c"]),
                sensed_outdoor_c=_optional_float(row["sensed_outdoor_c"]),
                power=power, setpoint_c=float(row["setpoint_c"]),
                mode=str(row["mode"]), status=str(row["status"]),
                estimated_watts=watts, interval_wh=None,
                phase=self.profile.phase_for_watts(watts, power),
                advisory=advisory,
                # A restart must not reopen the debounce window: an advisory
                # that was already earned stays earned until conditions change.
                stall_since=None if advisory is None else at - ADVISORY_HOLD_S,
                ac_model=(row.get("ac_model") or self.profile.name),
                provenance=str(row.get("provenance") or PROVENANCE),
                # A room that was dried stays dried across a restart. Losing
                # this would snap the modelled RH back to the raw sensor and
                # hand the PMV solve a humidity the room does not have.
                modeled_rh=_optional_float(row.get("modeled_rh")),
                sensed_indoor_rh=_optional_float(row.get("sensed_indoor_rh")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def step(self, indoor_c: Optional[float], outdoor_c: Optional[float],
             automatic_power: bool, *, at: Optional[float] = None,
             indoor_rh: Optional[float] = None) -> TwinState:
        now = float(at if at is not None else time.time())
        if self.state is not None and now < self.state.at:
            # The persisted timeline is monotonic. A clock that steps backwards
            # (NTP, an out-of-order tick) must not rewind `state.at`: the next
            # forward tick would measure its dt from the rewound stamp and bill
            # a span that was already billed. The tick still runs on current
            # sensors and switches — it just advances no time.
            now = self.state.at
        indoor = _plausible_temperature(indoor_c)
        outdoor = _plausible_temperature(outdoor_c)
        control = self.telemetry.ac_twin_control(now)
        manual = bool(control and control.get("mode") == "manual")
        power = bool(control.get("power")) if manual else bool(automatic_power)
        setpoint = float(control.get("setpoint_c")) if manual else (
            self.state.setpoint_c if self.state else self.config.default_setpoint_c)
        setpoint = _clamp(setpoint, 16.0, 30.0)
        mode = "manual" if manual else "automatic"

        previous = self.state
        if indoor is None:
            modeled = previous.modeled_c if previous else None
            effect = previous.cooling_effect if previous else 0.0
            # Without a sensor the band cannot be recomputed, so the machine is
            # reported as it was last seen instead of being invented.
            watts = (previous.estimated_watts if previous is not None
                     else self.profile.rated_power_w) if power else 0.0
            state = TwinState(
                at=now, modeled_c=modeled, cooling_effect=effect,
                sensed_indoor_c=None, sensed_outdoor_c=outdoor, power=power,
                setpoint_c=setpoint, mode=mode, status="sensor_hold",
                estimated_watts=round(watts, 2), interval_wh=None,
                phase=self.profile.phase_for_watts(watts, power),
                advisory=(previous.advisory if previous else None),
                stall_since=(previous.stall_since if previous else None),
                ac_model=self.profile.name,
                # A blind temperature tick must not forget the room was dried:
                # snapping modeled_rh back to the raw sensor would hand the PMV
                # solve a humidity the room does not have. The RH channel may
                # still be reporting, so the measurement is kept as given.
                modeled_rh=(previous.modeled_rh if previous else None),
                sensed_indoor_rh=indoor_rh,
            )
            return self._commit(state, switch_power=automatic_power)

        if previous is None or previous.modeled_c is None:
            return self._commit(
                self._anchor(now, indoor, outdoor, power, setpoint, mode,
                             "initialized", interval_wh=0.0,
                             indoor_rh=indoor_rh),
                switch_power=automatic_power)

        raw_dt = max(0.0, now - previous.at)
        if raw_dt > self.config.reset_gap_s:
            return self._commit(
                self._anchor(now, indoor, outdoor, power, setpoint, mode,
                             "reset_after_gap", interval_wh=None,
                             indoor_rh=indoor_rh),
                switch_power=automatic_power)

        # THE MODEL IS COLDER THAN ANYTHING SWITCHED OFF COULD HAVE MADE IT.
        #
        # Cooling drives the room TOWARD the setpoint and stops there, so a
        # rebound starts at roughly the setpoint and rises. A model sitting far
        # BELOW the setpoint with the compressor off is therefore not a rebound in
        # progress — no mechanism in the model can produce it, and the anchor will
        # take hours to walk it back while every decision in between is made on a
        # temperature the room does not have.
        #
        # Observed: one -2.1 C sensor glitch clamped the model to 12.9 C against a
        # 25.5 C room, and it was still 10 C out an hour later. The upstream guard
        # now stops that reading arriving at all; this is the recovery for whatever
        # finds a way through next.
        #
        # Deliberately keyed on the SETPOINT, not on the gap to the sensor. Gap
        # alone would fire on a legitimate rebound — which is exactly the gradual
        # return to ambient the model exists to show — and snapping that away
        # would delete the behaviour rather than protect it. And powered is
        # excluded outright: a running compressor is SUPPOSED to hold the room far
        # from its unconditioned baseline.
        if (not power and previous.modeled_c is not None
                and previous.modeled_c < setpoint - RESYNC_BELOW_SETPOINT_C):
            return self._commit(
                self._anchor(now, indoor, outdoor, power, setpoint, mode,
                             "resynced_to_sensor", interval_wh=None,
                             indoor_rh=indoor_rh),
                switch_power=automatic_power)

        # One shared implementation with the planner's rollout — see `advance`.
        # Live, the counterfactual no-AC baseline IS the DHT22 reading: the
        # sensor is measuring a room the compressor is not holding.
        step = advance(
            modeled_c=previous.modeled_c, cooling_effect=previous.cooling_effect,
            previous_watts=previous.estimated_watts, baseline_c=indoor,
            outdoor_c=outdoor, power=power, setpoint_c=setpoint,
            dt_s=min(raw_dt, self.config.max_step_s),
            config=self.config, profile=self.profile,
            # Humidity carries forward from the model's own last value, and
            # falls back to the sensor on the first tick or after a gap —
            # the same anchoring discipline the temperature uses.
            modeled_rh=(previous.modeled_rh
                        if previous.modeled_rh is not None else indoor_rh),
            baseline_rh=indoor_rh)
        modeled, effect = step.modeled_c, step.cooling_effect
        watts, phase, interval_wh = (
            step.estimated_watts, step.phase, step.interval_wh)
        # The realized rate is what the room actually did, clamp included. A
        # model pinned at its honesty limit is stalled even when the unclamped
        # physics still claims it is cooling hard.
        realized_rate_c_h = step.net_rate_c_h

        advisory, stall_since = self._advisory(
            previous, power=power, gap_c=modeled - setpoint,
            rate_c_h=realized_rate_c_h, setpoint_c=setpoint, now=now)
        if advisory and previous.advisory is None:
            self._log_advisory(now, advisory, setpoint, modeled, indoor, watts)

        if phase in (PHASE_PULL_DOWN, PHASE_TAPER):
            status = "cooling"
        elif phase == PHASE_HOLD:
            status = "holding"
        elif effect > 0.02 and realized_rate_c_h < 0.0:
            # A falling model is NOT evidence of residual cooling. With the coil
            # spent, a room whose measured baseline sits below the model falls
            # for free, and reporting that as the AC's residual credits an
            # unpowered appliance with the envelope's work. The coil's own
            # decay is the only thing that earns this label; everything else
            # returning to the sensor is a rebound, in whichever direction.
            #
            # AND THE COIL HAS TO BE WINNING. `effect` stays above this
            # threshold for the whole cooling_decay_min window, but the coil
            # coasts at 0.167 C/h against a drift many times that — so the room
            # is usually already WARMING while the term decays. Measured after
            # switch-off: fourteen consecutive minutes of 23.29 -> 23.69 C, every
            # tick reported as `residual_cooling`. A warming room described as
            # cooling is the same lie in the other direction, so the net rate has
            # to agree before the label is used.
            status = "residual_cooling"
        elif abs(modeled - indoor) > 0.05:
            status = "rebounding"
        else:
            status = "holding"
        state = TwinState(
            at=now, modeled_c=round(modeled, 4),
            cooling_effect=round(effect, 6), sensed_indoor_c=indoor,
            sensed_outdoor_c=outdoor, power=power, setpoint_c=setpoint,
            mode=mode, status=status, estimated_watts=round(watts, 2),
            interval_wh=round(interval_wh, 6), phase=phase,
            advisory=advisory, stall_since=stall_since,
            ac_model=self.profile.name,
            modeled_rh=(round(step.modeled_rh, 2)
                        if step.modeled_rh is not None else None),
            sensed_indoor_rh=indoor_rh,
        )
        return self._commit(state, switch_power=automatic_power, step=step,
                            previous=previous, dt_s=min(
                                raw_dt, self.config.max_step_s))

    def _anchor(self, now: float, indoor: float, outdoor: Optional[float],
                power: bool, setpoint: float, mode: str, status: str, *,
                interval_wh: Optional[float],
                indoor_rh: Optional[float] = None) -> TwinState:
        """Re-anchor the model on the sensor without inventing history."""
        watts, phase = self.profile.modulation(indoor - setpoint, power)
        return TwinState(
            at=now, modeled_c=indoor, cooling_effect=0.0,
            sensed_indoor_c=indoor, sensed_outdoor_c=outdoor, power=power,
            setpoint_c=setpoint, mode=mode, status=status,
            estimated_watts=round(watts, 2), interval_wh=interval_wh,
            phase=phase, advisory=None, stall_since=None,
            ac_model=self.profile.name,
            # Anchoring means model = sensor, humidity included. Dropping the
            # measurement here would persist a row whose RH reads unavailable
            # while the sensor was in fact reporting one.
            modeled_rh=indoor_rh, sensed_indoor_rh=indoor_rh)

    def _advisory(self, previous: TwinState, *, power: bool, gap_c: float,
                  rate_c_h: float, setpoint_c: float,
                  now: float) -> tuple[Optional[str], Optional[float]]:
        """Debounced judgement that a requested setpoint is out of reach.

        The streak lives in TwinState rather than a module global, so parallel
        rooms cannot share a clock and a restart cannot silently reset it.
        """
        stalled = (power and gap_c > ADVISORY_GAP_C
                   and rate_c_h >= ADVISORY_RATE_C_H)
        if not stalled:
            return None, None
        since = previous.stall_since if previous.stall_since is not None else now
        if now - since < ADVISORY_HOLD_S:
            return None, since
        return ADVISORY_TEMPLATE.format(setpoint_c=setpoint_c), since

    def _log_advisory(self, at: float, advisory: str, setpoint: float,
                      modeled: float, indoor: float, watts: float) -> None:
        """One audit row per advisory episode, not one per tick."""
        self.telemetry.ac_twin_event(
            "advisory",
            {"ac_model": self.profile.name, "setpoint_c": setpoint,
             "modeled_c": round(modeled, 4), "sensed_indoor_c": indoor,
             "estimated_watts": round(watts, 2),
             "rated_cooling_kw": self.profile.rated_cooling_kw},
            operator="digital_twin", reason=advisory, provenance=PROVENANCE,
            at=at)

    def _commit(self, state: TwinState, *, switch_power: bool,
                step: Optional[AcStep] = None,
                previous: Optional[TwinState] = None,
                dt_s: Optional[float] = None) -> TwinState:
        self.telemetry.ac_twin_save(state.as_dict())
        self.last_explanation = self._explain(
            state, switch_power=switch_power, step=step, previous=previous,
            dt_s=dt_s)
        self.state = state
        return state

    def _explain(self, state: TwinState, *, switch_power: bool,
                 step: Optional[AcStep], previous: Optional[TwinState],
                 dt_s: Optional[float]) -> TwinExplanation:
        """Everything R5 needs to show that the AC card is arithmetic, not a mood."""
        config = self.config
        advanced = (step is not None and previous is not None
                    and previous.modeled_c is not None)
        return TwinExplanation(
            measured={"indoor_c": state.sensed_indoor_c,
                      "indoor_rh": state.sensed_indoor_rh,
                      "outdoor_c": state.sensed_outdoor_c},
            # `switch_power` is what the caller reported for the real switch;
            # `power` is what the model ran on, which a live manual override
            # replaces. Both, so a disagreement between them is visible.
            inputs={"power": state.power, "switch_power": bool(switch_power),
                    "setpoint_c": state.setpoint_c, "mode": state.mode,
                    "ac_model": state.ac_model,
                    # Tons and kW beside the stored identity, because that is
                    # how an air conditioner is sold and argued about and a
                    # reader should not convert 5 kW in their head. DERIVED, not
                    # stored: ac_model on the state row is the appliance
                    # identity, and a persisted row should not carry a formatted
                    # display string.
                    "ac_label": config.profile.label,
                    "ac_tons": round(config.profile.tons, 2),
                    "dt_s": dt_s,
                    "cooling_rate_c_h": round(
                        config.effective_cooling_rate_c_h, 4),
                    "coast_rate_c_h": round(config.coast_rate_c_h, 4),
                    "ua_w_per_c": round(config.ua_w_per_c, 3),
                    "reachable_depth_c": round(config.reachable_depth_c, 3)},
            step=None if not advanced else {
                "from_c": round(previous.modeled_c, 4),
                "drift_c_h": round(step.drift_c_h, 4),
                "cooling_c_h": round(step.cooling_c_h, 4),
                "net_rate_c_h": round(step.net_rate_c_h, 4),
                "applied_c": round(state.modeled_c - previous.modeled_c, 4),
                "floor_c": round(step.floor_c, 3)},
            modelled={"room_c": state.modeled_c, "room_rh": state.modeled_rh,
                      "phase": state.phase, "status": state.status,
                      "cooling_effect": state.cooling_effect,
                      "estimated_watts": state.estimated_watts,
                      "interval_wh": state.interval_wh})


def _optional_float(value) -> Optional[float]:
    return None if value is None else float(value)


# What a sensor on THIS rig can legitimately report. The old -20..60 window was
# wide enough to wave through a DHT22 glitch of -2.1 C, which then became the
# model's baseline and dragged a 25.5 C room down to a reported 12.9 C. The
# reader already rejects anything outside 5..55 (`sense/reader/base.py`); this
# matches it rather than being independently generous, because two layers
# disagreeing about what is plausible is how a bad value finds a way through.
#
# Deliberately NOT the same constant imported from the reader: the twin is a pure
# module with no sense/ dependency, and coupling them to share a number would
# cost more than the duplication. `test_digital_twin.py` pins them equal instead.
# How far below its own setpoint an UNPOWERED model may sit before it is
# treated as lost rather than rebounding. Cooling stops at the setpoint, so a
# rebound begins there and rises; a couple of degrees allows for the overshoot
# of a single step without catching any legitimate return to ambient.
RESYNC_BELOW_SETPOINT_C = 2.0

PLAUSIBLE_MIN_C = 5.0
PLAUSIBLE_MAX_C = 55.0


def _plausible_temperature(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if (math.isfinite(parsed)
                      and PLAUSIBLE_MIN_C <= parsed <= PLAUSIBLE_MAX_C) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
