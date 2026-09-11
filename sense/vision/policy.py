"""When to look at the camera.

Running inference continuously wastes CPU on a room that has not changed. PIR is
free and always on, so **PIR is the trigger and the camera is the counter.**

    every 5 min                        capture      baseline drift
    PIR fires after a quiet stretch    immediate    empty -> filled
    PIR silent 10 min while occupied   immediate    filled -> empty
    count changed last time            re-check     settle a doorway frame

Why each rule earns its place:

- **Baseline** catches someone who entered without tripping PIR — walked in
  behind another person, or sat in a blind spot.
- **PIR edge** is the arrival case. Zero latency; PIR interrupts, nothing polls.
- **PIR silence** is the departure case, and the important one. PIR cannot tell
  "left the room" from "sitting very still" — exactly the failure that would cut
  the AC on someone still sitting there. The camera settles it.
- **Re-check** because a person mid-doorway is one frame of nonsense.

Pure decision logic, same discipline as `brain.py`: no camera, no clock, no I/O.
Time is passed in, so this is testable without waiting five minutes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

BASELINE_INTERVAL_MIN = 5.0      # routine poll
PIR_QUIET_MIN = 10.0             # PIR silence that means "probably left"
RECHECK_AFTER_CHANGE_MIN = 1.0   # settle a count that just moved
MIN_GAP_MIN = 0.25               # never fire twice inside 15 s

ARRIVAL = "arrival"
DEPARTURE = "departure"
BASELINE = "baseline"
RECHECK = "recheck"


@dataclass
class VisionState:
    """Everything the policy needs to remember between ticks."""
    last_capture_min: Optional[float] = None
    last_pir_min: Optional[float] = None
    last_count: Optional[int] = None
    count_changed: bool = False
    pir_was_quiet: bool = True       # so the first motion reads as an arrival


@dataclass
class Decision:
    capture: bool
    reason: str = ""
    state: VisionState = field(default_factory=VisionState)


def should_capture(now_min: float, pir_active: bool,
                   state: VisionState) -> Decision:
    """Decide whether to run inference this tick.

    now_min    monotonic minutes; only differences matter
    pir_active did PIR see motion since the last tick
    """
    s = replace(state)
    if pir_active:
        s.last_pir_min = now_min

    since_capture = (None if s.last_capture_min is None
                     else now_min - s.last_capture_min)

    # A cold start must look immediately — we know nothing about the room.
    if since_capture is None:
        return Decision(True, "first look", _stamp(s, now_min))

    # Rate limit outranks every trigger below. Without it a flickering PIR
    # would pin the CPU at 100 %.
    if since_capture < MIN_GAP_MIN:
        return Decision(False, "rate limited", s)

    # Arrival: motion after a quiet stretch. The edge is what matters, not the
    # motion itself — a continuously occupied room must not retrigger forever.
    if pir_active and s.pir_was_quiet:
        s.pir_was_quiet = False
        return Decision(True, ARRIVAL, _stamp(s, now_min))

    if pir_active:
        s.pir_was_quiet = False

    # Departure: PIR has gone quiet while we still believe someone is here.
    if (s.last_count or 0) > 0 and s.last_pir_min is not None:
        if now_min - s.last_pir_min >= PIR_QUIET_MIN:
            s.pir_was_quiet = True
            return Decision(True, DEPARTURE, _stamp(s, now_min))

    # Re-check a count that just moved, before anything acts on it.
    if s.count_changed and since_capture >= RECHECK_AFTER_CHANGE_MIN:
        return Decision(True, RECHECK, _stamp(s, now_min))

    if since_capture >= BASELINE_INTERVAL_MIN:
        return Decision(True, BASELINE, _stamp(s, now_min))

    # Mark PIR quiet once the window has elapsed, so the *next* motion counts
    # as a fresh arrival rather than being swallowed as ongoing presence.
    if s.last_pir_min is not None and now_min - s.last_pir_min >= PIR_QUIET_MIN:
        s.pir_was_quiet = True

    return Decision(False, "holding", s)


def _stamp(s: VisionState, now_min: float) -> VisionState:
    s.last_capture_min = now_min
    return s


def record_count(state: VisionState, count: Optional[int]) -> VisionState:
    """Fold a fresh count back into the state. `count_changed` is what arms the
    re-check, so a doorway frame gets confirmed before it reaches the ladder."""
    s = replace(state)
    if count is None:
        return s                       # a failed count must not clear knowledge
    s.count_changed = (s.last_count is not None and count != s.last_count)
    s.last_count = count
    return s
