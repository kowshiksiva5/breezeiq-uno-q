"""STM32 over USB serial — the rig as it stands today.

The MCU emits one `HW key=value ...` line per second. This parses it. When the
Bridge RPC replaces the cable, only this file is rewritten; SensorFrame and
everything above it stay untouched. That is the point of the seam.
"""
from __future__ import annotations

import glob
import threading
import time
from collections import deque
from typing import Optional

from .base import (SensorFrame, SensorSource, decode_flag, decode_lux,
                   register_source)

try:
    import serial                                  # pyserial
except ImportError:                                # board has no pyserial; Bridge later
    serial = None

BAUD = 115200
STALE_AFTER_S = 8.0
ADC_MAX = 4095
ADC_RAIL_MARGIN = 15
LDR_RAIL_WINDOW_S = 12.0


def find_port() -> Optional[str]:
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    return ports[0] if ports else None


@register_source("stm32-serial")
class SerialSensorSource(SensorSource):
    """Reads in a background thread so a slow or absent board never blocks the
    control loop — the loop takes whatever the last line said."""

    def __init__(self, port: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self.port = port or find_port()
        self._lock = threading.Lock()
        self._fields: dict = {}
        self._at = 0.0
        self._ldr_recent = deque(maxlen=30)
        self._conn = None
        self._error: Optional[str] = None

    def start(self) -> "SerialSensorSource":
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self) -> None:
        while True:
            if serial is None:
                self._error = "pyserial not installed"
                time.sleep(10)
                continue
            try:
                self.port = self.port or find_port()
                if not self.port:
                    self._error = "no board found"
                    time.sleep(3)
                    continue
                with serial.Serial(self.port, BAUD, timeout=3) as conn:
                    self._conn, self._error = conn, None
                    while True:
                        line = conn.readline().decode(errors="replace").strip()
                        if line.startswith("HW "):
                            self._ingest(line)
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
                self._conn, self.port = None, None
                time.sleep(3)

    def _ingest(self, line: str) -> None:
        fields = {}
        for token in line.split()[1:]:
            if "=" in token:
                k, _, v = token.partition("=")
                fields[k] = v
        now = time.time()
        with self._lock:
            self._fields, self._at = fields, now
            try:
                self._ldr_recent.append((now, int(fields["ldr"])))
            except (KeyError, ValueError):
                pass

    def send(self, ch: str) -> bool:
        """Actuator commands share the reader's connection — a second handle
        would race it for incoming bytes."""
        conn = self._conn
        if conn is None:
            return False
        try:
            conn.write(ch.encode()[:1])
            conn.flush()
            return True
        except Exception:
            return False

    def _read_raw(self) -> SensorFrame:
        with self._lock:
            fields, at, err = dict(self._fields), self._at, self._error
            ldr_recent = list(self._ldr_recent)
        if not fields:
            return SensorFrame(fault=err or "no data from the board yet")
        if time.time() - at > STALE_AFTER_S:
            return SensorFrame(fault=f"board silent for {int(time.time()-at)}s")

        def num(key, cast=float):
            try:
                return cast(fields[key])
            except (KeyError, ValueError):
                return None

        light = num("ldr", int)
        dark, sun = num("ldr_min", int), num("ldr_max", int)
        solar = None
        # Only report a solar index once the calibration span is real; a
        # half-built divider would otherwise produce confident nonsense.
        recent = [value for seen_at, value in ldr_recent
                  if at - seen_at <= LDR_RAIL_WINDOW_S]
        rail_to_rail = (any(value <= ADC_RAIL_MARGIN for value in recent)
                        and any(value >= ADC_MAX - ADC_RAIL_MARGIN
                                for value in recent))
        current_driven = (light is not None
                          and ADC_RAIL_MARGIN < light < ADC_MAX - ADC_RAIL_MARGIN)
        if (None not in (light, dark, sun) and sun - dark > 500
                and current_driven and not rail_to_rail):
            solar = max(0.0, min(1.0, (light - dark) / (sun - dark)))

        indoor_ok = fields.get("s_in") == "OK"
        outdoor_ok = fields.get("s_out") == "OK"
        return SensorFrame(
            indoor_c=num("in_c") if indoor_ok else None,
            indoor_raw_c=num("in_raw_c") if indoor_ok else None,
            outdoor_raw_c=num("out_raw_c") if outdoor_ok else None,
            indoor_rh=num("in_rh") if indoor_ok else None,
            outdoor_c=num("out_c") if outdoor_ok else None,
            outdoor_rh=num("out_rh") if outdoor_ok else None,
            light_raw=light, solar_index=solar,
            air_raw=num("mq", int),
            lux=decode_lux(num("lux")),
            radar_presence=decode_flag(num("radar", int)),
            radar_edges=num("radar_edges", int),
            radar_held_s=(None if num("radar_held_ms") is None
                          else num("radar_held_ms") / 1000.0),
            motor_dir=num("motor_dir", int), motor_speed=num("motor_speed", int),
            motor_left_s=(None if num("motor_left_ms") is None
                          else num("motor_left_ms") / 1000.0),
            failsafe_active=decode_flag(num("failsafe", int)),
            failsafe_episodes=num("failsafe_n", int),
            fw_build=num("fw_build", int),
            ldr_min=num("ldr_min", int), ldr_max=num("ldr_max", int),
            lux_addr=num("lux_addr", int), lux_bus=num("lux_bus", int),
            i2c_devices=num("i2c_n", int),
            i2c_bus0=num("i2c_b0", int), i2c_bus1=num("i2c_b1", int),
            i2c_bus2=num("i2c_b2", int),
            sda_pullup=decode_flag(num("sda_pu", int)),
            scl_pullup=decode_flag(num("scl_pu", int)),
            sda_level=num("sda_lvl", int), scl_level=num("scl_lvl", int),
            occupied=(num("pir", int) == 1) if num("pir", int) is not None else None,
            at=at,
        )

    def health(self) -> dict:
        h = super().health()
        h.update(port=self.port, link_error=self._error)
        return h
