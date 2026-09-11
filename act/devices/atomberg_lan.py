"""Passive LAN listener for Atomberg fans.

Why this exists: commands cost API quota (100/day, hard vendor limit) but the
fans broadcast on the LAN for free. Without this, asking "is the fan still
there / did someone change it by hand?" every 30 s would exhaust the day's
budget before lunch. With it, a comfortable room costs ZERO API calls.

Observed on this network: the fan sends `<device_id>_<series>` to UDP 5625
every few seconds. Richer models send hex-encoded JSON with actual state, so
both shapes are parsed.
"""
from __future__ import annotations

import json
import socket
import threading
import time
from typing import Optional

UDP_PORT = 5625
OFFLINE_AFTER_S = 90.0          # several missed beacons, not one


class AtombergUdpListener:
    """One socket, one thread, shared by every Atomberg device."""

    def __init__(self, port: int = UDP_PORT):
        self.port = port
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}      # device_id -> last beacon
        self._error: Optional[str] = None
        self._started = False

    def start(self) -> "AtombergUdpListener":
        if self._started:
            return self
        self._started = True
        threading.Thread(target=self._listen, daemon=True).start()
        return self

    def _listen(self) -> None:
        while True:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", self.port))
                sock.settimeout(5)
                self._error = None
                while True:
                    try:
                        data, addr = sock.recvfrom(4096)
                    except socket.timeout:
                        continue
                    self._ingest(data, addr[0])
            except Exception as exc:
                # Port already held by another listener, or no permission.
                # Never fatal: the backend just falls back to cloud state.
                self._error = f"{type(exc).__name__}: {exc}"
                time.sleep(10)
            finally:
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass

    def _ingest(self, data: bytes, ip: str) -> None:
        raw = data.decode(errors="replace").strip()
        # Some fans prefix a PROXY header carrying the real sender IP.
        if raw.startswith("PROXY TCP4"):
            parts = raw.split()
            if len(parts) >= 3:
                ip = parts[2]
            raw = raw.split("\r\n", 1)[-1].strip()

        device_id, state = None, {}
        try:                                     # richer models: hex-encoded JSON
            decoded = json.loads(bytes.fromhex(raw).decode())
            device_id = decoded.get("device_id") or decoded.get("deviceId")
            state = decoded
        except Exception:
            if "_" in raw:                       # plain beacon: <device_id>_<series>
                device_id = raw.split("_")[0]
        if not device_id:
            return

        with self._lock:
            entry = self._devices.setdefault(device_id, {"beacons": 0})
            entry.update(ip=ip, last_seen=time.time(), raw=raw[:80])
            entry["beacons"] += 1
            if state:
                entry["state"] = state

    # ── read side ───────────────────────────────────────────────────────
    def seen(self, device_id: str) -> Optional[dict]:
        with self._lock:
            return dict(self._devices.get(device_id, {})) or None

    def is_online(self, device_id: str) -> Optional[bool]:
        entry = self.seen(device_id)
        if not entry or "last_seen" not in entry:
            return None                          # unknown, not "offline"
        return (time.time() - entry["last_seen"]) < OFFLINE_AFTER_S

    def local_state(self, device_id: str) -> Optional[bool]:
        """Power state from a beacon, when the model publishes one. None means
        the beacon carries identity only — ask the cloud if you truly need it."""
        entry = self.seen(device_id)
        state = (entry or {}).get("state") or {}
        for key in ("power", "is_on", "state"):
            if key in state:
                return bool(state[key])
        return None

    def snapshot(self) -> dict:
        """Serializable, privacy-bounded presence for an App Lab bridge."""
        with self._lock:
            devices = {}
            for device_id, entry in self._devices.items():
                item = {
                    "ip": entry.get("ip"),
                    "beacons": entry.get("beacons"),
                    "age_s": round(time.time() - entry["last_seen"], 1)
                    if "last_seen" in entry else None,
                }
                state = entry.get("state") or {}
                for key in ("power", "is_on", "state"):
                    if key in state:
                        item["power"] = bool(state[key])
                        break
                devices[device_id] = item
            return {"at": time.time(), "port": self.port,
                    "error": self._error, "devices": devices}

    def health(self) -> dict:
        return self.snapshot()
