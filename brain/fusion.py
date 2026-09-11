"""Conservative PIR, camera, and light-sensor fusion.

No I/O lives here.  Inputs are relative evidence, not lux measurements:

* ``ldr_index`` is the calibrated 0..1 window-facing divider signal.
* ``camera_luma`` is mean frame luminance, also normalized to 0..1.

The two channels observe different things.  Agreement permits an automatic
light decision.  Strong disagreement holds the current state and asks the
dashboard to show the conflict.  This prevents a loose LDR or camera
auto-exposure from toggling the room light.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


DARK_MAX = 0.25
BRIGHT_MIN = 0.65
DISAGREE_AT = 0.45

# Turning absolute lux into the same 0..1 daylight index the divider produces.
#
# Log rather than linear, because the eye is: 50 lx and 250 lx are a dim room
# and a working one, while 40,000 lx and 40,200 lx are the same noon. A linear
# map would put every indoor reading in the bottom 1% of the scale and make
# DARK_MAX unreachable in a room that is genuinely dark to a person.
#
# The anchors are the two judgements the index has to support: below LUX_DARK
# somebody would reach for a lamp, and at LUX_BRIGHT the window is carrying the
# room. They are deliberately the boundaries of ordinary indoor light, not of
# the sensor's range, since nothing above "the room is well lit" changes any
# decision here.
LUX_DARK = 30.0
LUX_BRIGHT = 3000.0

# What the occupant wants the room to feel like, not what the sensors see.
#   bright  — well lit; switch on before the room merely stops being dark
#   minimal — just enough to get things done; only a genuinely dark room
#   dark    — never switch the light on automatically
LIGHT_PREFERENCES = ("bright", "minimal", "dark")
DEFAULT_LIGHT_PREFERENCE = "minimal"

# The "bright" preference acts inside the wide "normal" band. A fused value can
# sit anywhere from DARK_MAX to BRIGHT_MIN and still be called normal, and the
# lower half of that band is dusk — readable, but not what somebody who asked
# for a well-lit room means. This threshold sits above DARK_MAX (so it only ever
# adds to what "minimal" already does) and below BRIGHT_MIN (so it can never
# fight the self-illumination hold), splitting normal at its midpoint.
COMFORTABLE_MIN = 0.45


@dataclass(frozen=True)
class LightEvidence:
    value: Optional[float]
    state: str                     # dark | normal | bright | unavailable
    confidence: str                # high | medium | low | unavailable
    sources: tuple[str, ...]
    disagreement: bool = False


def _clamp(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return max(0.0, min(1.0, float(value)))


def lux_index(lux: Optional[float]) -> Optional[float]:
    """Absolute lux onto the same 0..1 scale the divider is calibrated to.

    None in, None out: a sensor that did not answer is not a dark room, and
    collapsing the two is precisely how a disconnected part reads as midnight.
    """
    if lux is None or lux < 0:
        return None
    import math
    if lux <= LUX_DARK:
        return 0.0
    span = math.log(LUX_BRIGHT / LUX_DARK)
    return max(0.0, min(1.0, math.log(lux / LUX_DARK) / span))


def fuse_light(ldr_index: Optional[float], camera_luma: Optional[float]) -> LightEvidence:
    """Fuse relative window and room light without inventing physical units."""
    ldr, camera = _clamp(ldr_index), _clamp(camera_luma)
    if ldr is None and camera is None:
        return LightEvidence(None, "unavailable", "unavailable", ())

    if ldr is not None and camera is not None:
        disagreement = abs(ldr - camera) >= DISAGREE_AT
        # Camera sees the occupant's visual environment directly; the LDR adds
        # daylight context.  Keep camera slightly dominant, but refuse auto
        # switching when they strongly disagree.
        value = 0.6 * camera + 0.4 * ldr
        confidence = "low" if disagreement else "high"
        sources = ("ldr", "camera")
    else:
        value = camera if camera is not None else ldr
        disagreement = False
        confidence = "medium"
        sources = ("camera",) if camera is not None else ("ldr",)

    state = "dark" if value <= DARK_MAX else "bright" if value >= BRIGHT_MIN else "normal"
    return LightEvidence(round(value, 4), state, confidence, sources, disagreement)


def wants_auto_on(preference: str, evidence: LightEvidence) -> bool:
    """Would this preference switch the light on for this evidence?

    Only ever answers "switch on". Nothing here can switch a light off, so a
    preference can add automatic help but never take a lit room away from
    somebody who lit it by hand.
    """
    if preference == "dark":
        return False
    if evidence.state == "dark":
        return True
    return (preference == "bright" and evidence.state == "normal"
            and evidence.value is not None
            and evidence.value < COMFORTABLE_MIN)


def decide_light(occupied: bool, current_on: bool, evidence: LightEvidence,
                 *, preference: str = DEFAULT_LIGHT_PREFERENCE,
                 asleep: bool = False, confirmed: bool = True
                 ) -> tuple[bool, str]:
    """Safe lighting policy with hysteresis, disagreement hold, and preference.

    Camera and LDR brightness may both include the controlled lamp itself.
    They can help turn a dark room on, but a bright reading after switch-on
    cannot prove daylight.  While occupied, keep the lamp on until a saved
    pre-light baseline or a lamp-isolated daylight sensor is available.

    The occupant's preference only ever widens or closes the automatic switch-on
    branch.  An empty room still goes dark on every preference, and a sleeping
    occupant is never woken by a light being fixed for them.
    """
    if not occupied:
        return False, "empty room"
    if evidence.value is None:
        return current_on, "hold: light evidence unavailable"
    if evidence.disagreement:
        return current_on, "hold: LDR and camera disagree"
    if asleep:
        return current_on, "hold: someone is asleep"
    if preference not in LIGHT_PREFERENCES:
        preference = DEFAULT_LIGHT_PREFERENCE
    if not current_on and wants_auto_on(preference, evidence):
        # A STREAK, NOT AN INSTANT — the same discipline the ladder applies to
        # comfort and the camera applies to people, and the one decision in this
        # system that was missing it.
        #
        # Measured on the board: `light_state` changed on 29 % of consecutive
        # ticks. The room's lighting was not changing every ninety seconds; the
        # LDR was flapping bright -> dark -> normal between adjacent samples, and
        # a single spurious "dark" was enough to switch the lamp on. That is how
        # the ceiling light came on three times in one night.
        #
        # Only the switch-ON is gated. Switching a lamp off for an empty room
        # stays immediate, because the cost of being wrong runs the other way.
        if not confirmed:
            return current_on, f"hold: {evidence.state} not confirmed yet"
        return True, f"{evidence.state} occupied room, {preference} preference"
    if current_on and evidence.state == "bright":
        return True, "hold: brightness may include room light"
    if not current_on and preference == "dark":
        return False, "hold: automatic light is off by preference"
    return current_on, f"hold: room light {evidence.state}"
