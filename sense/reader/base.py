"""Sensor abstraction: the contract every source must satisfy."""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional


@dataclass
class SensorFrame:
    """One validated observation of the room.

    `valid` is the important field. A source may return a frame with readings
    it does not trust; the control loop must never actuate on those. Keeping
    the reason attached means the dashboard can explain itself instead of
    silently showing a stale number.
    """
    indoor_c: Optional[float] = None
    # Pre-offset, straight off the part. The board applies TEMP_OFFSET_*_C
    # before Linux sees anything, so without these the correction cannot be
    # undone and a recalibration would have to rewrite history.
    indoor_raw_c: Optional[float] = None
    outdoor_raw_c: Optional[float] = None
    indoor_rh: Optional[float] = None
    outdoor_c: Optional[float] = None
    outdoor_rh: Optional[float] = None
    light_raw: Optional[int] = None          # ADC counts, source-specific
    solar_index: Optional[float] = None      # 0..1, calibrated
    air_raw: Optional[int] = None            # MQ-135 counts — logged, never gates
    lux: Optional[float] = None              # BH1750, absolute — None means no ACK
    radar_presence: Optional[bool] = None    # LD2410C OUT — holds on a still person
    # A level alone cannot separate a stuck pin from a genuinely occupied
    # room; only time can. Zero edges over hours is a fault, not presence.
    radar_edges: Optional[int] = None        # OUT transitions since MCU boot
    radar_held_s: Optional[float] = None     # seconds the current level has stood
    motor_dir: Optional[int] = None          # +1 open, -1 close, 0 idle — commanded, not sensed
    motor_speed: Optional[int] = None        # 0..255 last asked of the L298's enable
    motor_left_s: Optional[float] = None     # seconds left of the commanded window, 0 = idle
    failsafe_active: Optional[bool] = None   # is the silence-park engaged right now
    failsafe_episodes: Optional[int] = None  # how many times it has fired since boot
    fw_build: Optional[int] = None           # firmware build reporting all of this
    ldr_min: Optional[int] = None            # session calibration bounds behind the
    ldr_max: Optional[int] = None            # solar index's >500-count span guard
    lux_addr: Optional[int] = None           # I²C address that answered, 0 = none
    lux_bus: Optional[int] = None            # which of the 3 buses answered (0-2)
    i2c_devices: Optional[int] = None        # parts ACKing on the bus, -1 = unscanned
    i2c_bus0: Optional[int] = None           # per-bus ACK counts from the boot scan:
    i2c_bus1: Optional[int] = None           # separates "alone on every bus" from
    i2c_bus2: Optional[int] = None           # "its bus has other parts too"
    sda_pullup: Optional[bool] = None         # read before I2C claims the pins
    scl_pullup: Optional[bool] = None
    sda_level: Optional[int] = None           # the same pins read via the ADC
    scl_level: Optional[int] = None
    occupied: Optional[bool] = None
    valid: bool = False
    fault: str = ""
    at: float = field(default_factory=time.time)
    source: str = ""

    def age_s(self) -> float:
        return time.time() - self.at

    def as_dict(self) -> dict:
        return asdict(self)


# ── shared field decoding ───────────────────────────────────────────────
# Both readers parse the same HW line, and the ADC_RAIL_MARGIN comment in each
# already warns what happens when they drift apart. These two live here so
# there is one answer rather than two that agree by luck.

def decode_lux(value: Optional[float]) -> Optional[float]:
    """The firmware sends -1 when the BH1750 does not ACK. That is a missing
    sensor, not a dark room, and the two must not collapse into 0.0."""
    return None if value is None or value < 0 else value


def decode_flag(value: Optional[int]) -> Optional[bool]:
    """A digital level, or None when the key is absent — which is what an
    older firmware looks like, and must not read as 'nobody is there'."""
    return None if value is None else value == 1


# ── plausibility, applied by every source ───────────────────────────────
# A single sensor reading 2 C off passes every range and rate check ever
# written. Two sensors disagreeing is the only cheap way to catch it, which is
# why both DHT22s were bought.
TEMP_MIN_C, TEMP_MAX_C = 5.0, 55.0
MAX_STEP_C = 4.0                 # per sample; a real room cannot do this
MAX_DISAGREE_C = 25.0            # indoor vs outdoor, sanity only


def validate(frame: SensorFrame, previous: Optional[SensorFrame]) -> SensorFrame:
    """Stamp `valid`/`fault`. Never raises — a bad frame is data, not an error."""
    if frame.indoor_c is None or frame.indoor_rh is None:
        frame.valid = False
        frame.fault = frame.fault or "no indoor reading"
        return frame
    if not (TEMP_MIN_C <= frame.indoor_c <= TEMP_MAX_C):
        frame.valid, frame.fault = False, f"indoor {frame.indoor_c}C out of range"
        return frame
    if not (0.0 <= frame.indoor_rh <= 100.0):
        frame.valid, frame.fault = False, f"indoor RH {frame.indoor_rh}% out of range"
        return frame
    if previous and previous.valid and previous.indoor_c is not None:
        if abs(frame.indoor_c - previous.indoor_c) > MAX_STEP_C:
            frame.valid, frame.fault = False, "indoor temperature stepped implausibly"
            return frame
    if frame.outdoor_c is not None:
        if abs(frame.indoor_c - frame.outdoor_c) > MAX_DISAGREE_C:
            frame.valid, frame.fault = False, "the two sensors disagree wildly"
            return frame
    frame.valid, frame.fault = True, ""
    return frame


class SensorSource(ABC):
    """Base class for anything that can produce a SensorFrame."""

    name = "unnamed"

    def __init__(self, **kwargs):
        self._previous: Optional[SensorFrame] = None
        self._last_good: Optional[SensorFrame] = None
        self.config = kwargs

    @abstractmethod
    def _read_raw(self) -> SensorFrame:
        """Produce an unvalidated frame. Subclasses implement only this."""

    def start(self) -> "SensorSource":
        """Optional hook for sources needing a background reader."""
        return self

    def read(self) -> SensorFrame:
        """Validated read. Holds the last good frame when the current one fails,
        so a single dropped sample does not stall the controller."""
        try:
            frame = self._read_raw()
        except Exception as exc:                       # a source must never crash the loop
            frame = SensorFrame(fault=f"{type(exc).__name__}: {exc}")
        frame.source = self.name
        frame = validate(frame, self._previous)
        self._previous = frame
        if frame.valid:
            self._last_good = frame
        return frame

    def last_good(self) -> Optional[SensorFrame]:
        return self._last_good

    def health(self) -> dict:
        f = self._previous
        return {"source": self.name, "valid": bool(f and f.valid),
                "fault": f.fault if f else "no reading yet",
                "age_s": round(f.age_s(), 1) if f else None}


# ── registry ────────────────────────────────────────────────────────────
_SOURCES: dict[str, Callable[..., SensorSource]] = {}


def register_source(name: str):
    def deco(cls):
        cls.name = name
        _SOURCES[name] = cls
        return cls
    return deco


def build_source(name: str, **kwargs) -> SensorSource:
    if name not in _SOURCES:
        raise KeyError(f"unknown sensor source {name!r}; have {sorted(_SOURCES)}")
    return _SOURCES[name](**kwargs)


def available_sources() -> list[str]:
    return sorted(_SOURCES)
