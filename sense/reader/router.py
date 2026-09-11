"""Read the MCU from the board itself, over Arduino Router RPC.

App Lab mounts ``/run/arduino-router.sock`` into its Python container. The
STM32 exposes ``breezeiq/read`` through ``Bridge.provide_safe`` and Python calls
it through ``arduino.app_utils.Bridge``. The monitor TCP port is board-host
loopback-only and cannot be reached from an App Lab container.

Same parsing as `board.py`; only the transport differs. When Bridge RPC lands
it becomes a third source and neither of these changes.

The link is bidirectional, so this module also owns the *write* side: the
motor on the MCU is commanded down the same stream the telemetry rides up.
See ``RouterCommandLink`` for why that write side lives here and not in the
device backend that uses it.
"""
from __future__ import annotations

import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .base import (SensorFrame, SensorSource, decode_flag, decode_lux,
                   register_source)

HOST, PORT = "127.0.0.1", 7500
STALE_AFTER_S = 8.0
ADC_MAX = 4095
ADC_RAIL_MARGIN = 15
LDR_RAIL_WINDOW_S = 12.0

TELEMETRY_PREFIX = "HW "
REPLY_PREFIXES = ("ACK ", "ERR ")
COMMAND_TIMEOUT_S = 2.0
BRIDGE_COMMAND_METHOD = "breezeiq/cmd"


class McuLinkError(RuntimeError):
    """The command could not be handed to the MCU at all."""


class RouterCommandLink:
    """Write handle for the board serial, shared with the reader thread.

    The reader owns the only connection to the board. A backend that opened its
    own would give the MCU two competing serial peers on one UART, so commands
    are written through the reader's socket under a lock and the reply is
    picked off the same stream the reader is already draining.

    Telemetry and replies cannot be confused: telemetry lines start with
    ``HW``, replies with ``ACK``/``ERR``. Each side ignores the other's shape,
    so adding commands cannot change how a sensor frame is parsed.
    """

    def __init__(self):
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._replies: deque[tuple[int, str]] = deque(maxlen=32)
        self._seq = 0
        self._conn: Optional[socket.socket] = None
        self._bridge = None
        self._transport = "not connected"

    # ── reader-thread side ──────────────────────────────────────────────
    def attach_socket(self, conn: socket.socket) -> None:
        with self._cv:
            self._conn, self._bridge = conn, None
            self._transport = "router monitor tcp"

    def attach_bridge(self, bridge) -> None:
        with self._cv:
            self._conn, self._bridge = None, bridge
            self._transport = f"bridge rpc {BRIDGE_COMMAND_METHOD}"

    def detach(self) -> None:
        with self._cv:
            self._conn, self._bridge = None, None
            self._transport = "not connected"

    def offer(self, text: str) -> None:
        """Hand a non-telemetry line to whoever is waiting on a reply."""
        if not text.startswith(REPLY_PREFIXES):
            return
        with self._cv:
            self._seq += 1
            self._replies.append((self._seq, text))
            self._cv.notify_all()

    # ── command side ────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        with self._cv:
            return self._conn is not None or self._bridge is not None

    def request(self, line: str, timeout: float = COMMAND_TIMEOUT_S) -> Optional[str]:
        """Send one command line, return the MCU's reply, or None on timeout.

        Raises McuLinkError when the line could not be sent at all — an
        unreachable board and a silent board are different faults and the
        caller reports them differently.
        """
        text = line.strip()
        parts = text.split()
        target = parts[1] if len(parts) > 1 else ""
        with self._cv:
            # Claim the cursor before sending: a reply that lands between the
            # write and the wait must still be seen by this caller.
            cursor = self._seq
            conn, bridge = self._conn, self._bridge
        if bridge is not None:
            return self._request_bridge(bridge, text, target)
        if conn is None:
            raise McuLinkError("router link is not connected")
        try:
            with self._write_lock:
                conn.sendall((text + "\n").encode())
        except OSError as exc:
            raise McuLinkError(f"{type(exc).__name__}: {exc}") from exc
        return self._await_reply(cursor, target, timeout)

    def _await_reply(self, cursor: int, target: str,
                     timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                for seq, reply in self._replies:
                    if seq > cursor and self._addresses(reply, target):
                        return reply
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)

    @staticmethod
    def _addresses(reply: str, target: str) -> bool:
        parts = reply.split()
        return len(parts) > 1 and (not target or parts[1] == target)

    def _request_bridge(self, bridge, text: str, target: str) -> Optional[str]:
        """In a container the monitor port is unreachable; RPC carries it."""
        try:
            payload = bridge.call(BRIDGE_COMMAND_METHOD, text)
        except Exception as exc:
            raise McuLinkError(f"Bridge {type(exc).__name__}: {exc}") from exc
        if isinstance(payload, bytes):
            payload = payload.decode(errors="replace")
        reply = str(payload or "").strip()
        return reply if self._addresses(reply, target) else None

    def health(self) -> dict:
        with self._cv:
            return {"connected": self._conn is not None or self._bridge is not None,
                    "transport": self._transport,
                    "last_reply": self._replies[-1][1] if self._replies else None}


_LINK = RouterCommandLink()


def command_link() -> RouterCommandLink:
    """The process-wide write handle for the board serial.

    A module singleton because the board is a singleton: whichever reader is
    running attaches its connection here, and a device backend asks for it by
    calling this instead of dialling the board itself.
    """
    return _LINK


@register_source("router")
class RouterSensorSource(SensorSource):
    def __init__(self, host: str = HOST, port: int = PORT,
                 link: Optional[RouterCommandLink] = None, **kwargs):
        super().__init__(**kwargs)
        self.host, self.port = host, port
        self.link = link or command_link()
        self._lock = threading.Lock()
        self._fields: dict = {}
        self._at = 0.0
        self._ldr_recent = deque(maxlen=30)
        self._error: Optional[str] = None
        self._bridge = None
        if Path("/run/arduino-router.sock").exists():
            try:
                from arduino.app_utils import Bridge
                self._bridge = Bridge
            except ImportError:
                pass

    def start(self) -> "RouterSensorSource":
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self) -> None:
        if self._bridge is not None:
            self._reader_bridge()
            return
        while True:
            conn = None
            try:
                conn = socket.create_connection((self.host, self.port), timeout=8)
                conn.settimeout(15)
                self._error = None
                self.link.attach_socket(conn)
                buf = b""
                while True:
                    chunk = conn.recv(1024)
                    if not chunk:
                        raise ConnectionError("router closed the stream")
                    buf += chunk
                    # Keep the tail; a partial line must not be parsed as data.
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode(errors="replace").strip()
                        if text.startswith(TELEMETRY_PREFIX):
                            self._ingest(text)
                        else:
                            self.link.offer(text)
                    if len(buf) > 8192:
                        buf = buf[-1024:]
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {exc}"
            finally:
                # A dropped socket that is never closed becomes a second peer
                # on the board's UART once this loop reconnects.
                self.link.detach()
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
            time.sleep(3)

    def _reader_bridge(self) -> None:
        self.link.attach_bridge(self._bridge)
        while True:
            try:
                payload = self._bridge.call("breezeiq/read")
                if isinstance(payload, bytes):
                    payload = payload.decode(errors="replace")
                text = str(payload).strip()
                if text.startswith("HW "):
                    self._ingest(text)
                    self._error = None
                elif not text:
                    self._error = "MCU sensor snapshot is not ready"
                else:
                    self._error = "MCU returned an invalid sensor snapshot"
            except Exception as exc:
                self._error = f"Bridge {type(exc).__name__}: {exc}"
            time.sleep(1)

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

    def _read_raw(self) -> SensorFrame:
        with self._lock:
            fields, at, err = dict(self._fields), self._at, self._error
            ldr_recent = list(self._ldr_recent)
        if not fields:
            return SensorFrame(fault=err or "no data from the router yet")
        if time.time() - at > STALE_AFTER_S:
            return SensorFrame(fault=f"MCU silent for {int(time.time()-at)}s")

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

        pir = num("pir", int)
        indoor_ok = fields.get("s_in") == "OK"
        outdoor_ok = fields.get("s_out") == "OK"
        return SensorFrame(
            indoor_c=num("in_c") if indoor_ok else None,
            indoor_raw_c=num("in_raw_c") if indoor_ok else None,
            outdoor_raw_c=num("out_raw_c") if outdoor_ok else None,
            indoor_rh=num("in_rh") if indoor_ok else None,
            outdoor_c=num("out_c") if outdoor_ok else None,
            outdoor_rh=num("out_rh") if outdoor_ok else None,
            light_raw=light, solar_index=solar, air_raw=num("mq", int),
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
            occupied=(pir == 1) if pir is not None else None,
            at=at,
        )

    def health(self) -> dict:
        h = super().health()
        transport = ("rpc /run/arduino-router.sock" if self._bridge is not None
                     else f"legacy monitor tcp {self.host}:{self.port}")
        # I²C discovery belongs in health, not in a tick column: it describes
        # the wiring, not the room, and it is the difference between a bus with
        # nothing on it and a part sitting at the address nobody probed.
        with self._lock:
            fields = dict(self._fields)
        addr = fields.get("lux_addr")
        h.update(transport=transport, link_error=self._error,
                 command_link=self.link.health(),
                 bh1750_address=(f"0x{int(addr):02X}" if addr not in (None, "0")
                                 and str(addr).isdigit() and int(addr) else None),
                 i2c_devices=fields.get("i2c_n"))
        return h
