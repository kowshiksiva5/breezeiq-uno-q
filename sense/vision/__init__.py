"""Layer 1b — vision occupancy.

PIR says whether anyone is there. This says how many, and settles the case PIR
cannot: a person sitting perfectly still looks identical to an empty room.

    counter.py   how many people        (yolo | app-lab | mock | opencv | fomo)
    policy.py    when to look           PIR-triggered, 5-min baseline
    cli.py       test and tune it       ./tools/run.sh vision ...

Frames are counted and discarded. Runtime telemetry contains evidence, not
images.
"""
from .counter import (CountFrame, PersonCounter, build_counter,
                      available_counters, register_counter)
from .policy import (VisionState, Decision, should_capture, record_count,
                     ARRIVAL, DEPARTURE, BASELINE, RECHECK)

__all__ = ["CountFrame", "PersonCounter", "build_counter", "available_counters",
           "register_counter", "VisionState", "Decision", "should_capture",
           "record_count", "ARRIVAL", "DEPARTURE", "BASELINE", "RECHECK"]
