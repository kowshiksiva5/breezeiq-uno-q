"""Board-local manual-control ingress owned by the control-loop process.

The dashboard never imports vendor adapters and never receives credentials.
It sends one bounded JSON request over a Unix socket.  This server validates it
and routes it through DeviceRegistry, so compressor protection, rate limits,
dry-run gating, durable override authority, and command audit cannot be bypassed.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Optional

from .base import DeviceRegistry


MAX_REQUEST_BYTES = 8192
DEFAULT_SOCKET = "/run/breezeiq/control.sock"
DEVICE_ALIASES = {"light": "tubelight"}

DEFAULT_TTL_MIN = 30
MAX_TTL_MIN = 720

# ``ttl_min: null`` means "hold this until I return the device to automatic".
# The override journal stores expires_at NOT NULL and every active-override
# query filters on it, so a real NULL would hide the override from the very
# dashboard that has to show it. A year is longer than any appliance request
# stays meaningful, and it still expires, so a forgotten manual state cannot
# outlive the board.
UNTIL_AUTO_TTL_MIN = 365 * 24 * 60

# A dashboard tap carries no typed reason. The journal still needs one, so the
# gesture itself is recorded rather than an invented explanation.
DEFAULT_REASON = "dashboard tap"


class ControlSocketServer:
    def __init__(self, registry: DeviceRegistry, path: Optional[str] = None):
        self.registry = registry
        self.path = Path(path or os.environ.get("BREEZEIQ_COMMAND_SOCKET",
                                               DEFAULT_SOCKET))
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None

    def start(self) -> "ControlSocketServer":
        if self._thread and self._thread.is_alive():
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() or self.path.is_socket():
                self.path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(self.path))
            os.chmod(self.path, 0o660)
            sock.listen(8)
            sock.settimeout(1.0)
            self._socket = sock
            self._thread = threading.Thread(target=self._serve,
                                            name="breezeiq-control-socket",
                                            daemon=True)
            self._thread.start()
            self.error = None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        return self

    def _serve(self) -> None:
        assert self._socket is not None
        while self._socket is not None:
            try:
                conn, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                try:
                    raw = conn.recv(MAX_REQUEST_BYTES + 1)
                    if len(raw) > MAX_REQUEST_BYTES:
                        reply = self._error("request_too_large", "request exceeds 8 KiB")
                    else:
                        reply = self.handle(json.loads(raw or b"{}"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    reply = self._error("invalid_json", "request must be valid JSON")
                except Exception as exc:
                    reply = self._error("internal_error", type(exc).__name__)
                try:
                    conn.sendall(json.dumps(reply, separators=(",", ":")).encode())
                except OSError:
                    pass

    @staticmethod
    def _error(outcome: str, detail: str) -> dict:
        return {"ok": False, "skipped": True, "acknowledged": None,
                "reported": None, "outcome": outcome, "detail": detail}

    def handle(self, body: object) -> dict:
        if not isinstance(body, dict):
            return self._error("invalid_request", "JSON body must be an object")
        if body.get("confirm") is not True:
            return self._error("confirmation_required", "confirm must be true")

        requested_device = str(body.get("device", ""))
        known = {d.key for d in self.registry.devices()}
        device = (requested_device if requested_device in known
                  else DEVICE_ALIASES.get(requested_device, requested_device))
        if device not in known:
            return self._error("invalid_device", "unknown device")
        action = body.get("action")
        operator = str(body.get("operator") or "local-dashboard")[:80]
        reason = str(body.get("reason") or DEFAULT_REASON)[:240]
        if "ttl_min" in body and body["ttl_min"] is None:
            ttl_min = float(UNTIL_AUTO_TTL_MIN)
        else:
            try:
                ttl_min = float(body.get("ttl_min", DEFAULT_TTL_MIN))
            except (TypeError, ValueError):
                return self._error("invalid_ttl", "ttl_min must be numeric")
            if not 1 <= ttl_min <= MAX_TTL_MIN:
                return self._error(
                    "invalid_ttl", f"ttl_min must be between 1 and {MAX_TTL_MIN}")
        ttl_seconds = int(ttl_min * 60)

        if action == "auto":
            cleared = self.registry.return_to_auto(
                device, operator=operator, reason=reason)
            return {"ok": True, "device": device, "action": "auto",
                    "mode": "automatic", "cleared": cleared,
                    "acknowledged": True, "reported": None,
                    "outcome": "override_cleared"}
        if action == "power":
            if not isinstance(body.get("value"), bool):
                return self._error("invalid_value", "power value must be boolean")
            result = self.registry.manual_power(
                device, body["value"], operator=operator, reason=reason,
                ttl_seconds=ttl_seconds)
            return result.as_dict()
        if action == "speed":
            value = body.get("value")
            # 0 is off; 1..FAN_SPEED_MAX is the ceiling fan's own speed scale.
            # Read from the environment rather than hardcoded, because this was
            # the fourth place declaring the scale and they disagreed: the socket
            # accepted 6 while the fan has five speeds.
            top = int(os.environ.get("BREEZEIQ_FAN_SPEED_MAX", "5"))
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 <= value <= top):
                return self._error("invalid_value",
                                   f"speed value must be integer 0..{top}")
            if self.registry.get(device).kind.value != "fan":
                return self._error("invalid_action", "speed is supported only for fan")
            result = self.registry.manual_speed(
                device, value, operator=operator, reason=reason,
                ttl_seconds=ttl_seconds)
            return result.as_dict()
        if action == "bench_motor":
            # Bring-up only: the raw motor verbs, for proving a driver's
            # wiring before the ladder is ever allowed to use it. Bounds are
            # enforced on the MCU, which owns the motor; these checks only
            # keep obvious nonsense off the wire.
            command = body.get("command")
            if command not in ("OPEN", "CLOSE", "STOP", "BRAKE"):
                return self._error("invalid_value",
                                   "command must be OPEN, CLOSE, STOP or BRAKE")
            speed, ms = body.get("speed"), body.get("ms")
            if speed is not None and (isinstance(speed, bool)
                                      or not isinstance(speed, int)
                                      or not 0 <= speed <= 100):
                return self._error("invalid_value", "speed must be an integer 0..100")
            if ms is not None and (isinstance(ms, bool)
                                   or not isinstance(ms, int)
                                   or not 1 <= ms <= 15000):
                return self._error("invalid_value", "ms must be an integer 1..15000")
            if self.registry.get(device).kind.value != "cover":
                return self._error("invalid_action",
                                   "bench_motor is supported only for cover devices")
            result = self.registry.bench_motor(
                device, command, speed, ms, operator=operator, reason=reason)
            return result.as_dict()
        return self._error("invalid_action",
                           "action must be power, speed, bench_motor, or auto")

    def health(self) -> dict:
        return {"ok": bool(self._thread and self._thread.is_alive()),
                "path": str(self.path), "error": self.error}

    def close(self) -> None:
        sock, self._socket = self._socket, None
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        try:
            if self.path.exists() or self.path.is_socket():
                self.path.unlink()
        except OSError:
            pass
