"""Atomberg backend — the fan, via Atomberg's official developer API.

Why this is a first-class backend rather than "just use HA":
the FAN rung is the whole thesis of BreezeIQ (32 W buying the comfort 1500 W of
compressor otherwise would). It should not be one community integration away
from failing. Talking to the vendor API directly means the most important rung
has no third-party dependency.

Auth model (from developer.atomberg-iot.com):
  api_key + refresh_token  ->  short-lived access_token  ->  commands
The access token expires, so it is fetched lazily and re-fetched on a 401.

Stdlib only, same as every other backend, so it runs on the board unchanged.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.parse
from pathlib import Path
import threading
from typing import Optional

import fcntl

from .atomberg_lan import OFFLINE_AFTER_S, AtombergUdpListener
from .base import (CommandResult, Device, DeviceBackend, DeviceKind,
                   ReportedState, register_backend)

class ApiError(Exception):
    def __init__(self, status: int, body: str):
        # Vendor bodies are deliberately not copied into logs. Some gateways
        # echo request context, which could include account identifiers.
        super().__init__(f"HTTP {status}")
        self.status = status


BASE = "https://api.developer.atomberg-iot.com"
TIMEOUT_S = 8

# HARD VENDOR LIMIT, from the published docs: 100 API calls per DAY, 5 per
# second. Our control loop ticks every 30 s — polling would burn the daily
# quota before lunch. So: never poll, only send on an actual state change, and
# refuse politely when the budget is gone rather than getting silently banned.
DAILY_CALL_BUDGET = 90            # leave headroom for manual testing
MIN_SECONDS_BETWEEN_CALLS = 0.25  # respect the 5/sec throttle


class PersistentDailyBudget:
    """Process-safe vendor-call budget that survives service restarts."""

    def __init__(self, path: str, limit: int = DAILY_CALL_BUDGET):
        self.path = Path(path)
        self.limit = limit
        self.error: Optional[str] = None
        self._thread_lock = threading.Lock()

    @staticmethod
    def _fresh(day: str) -> dict:
        return {"day": day, "calls": 0, "by_kind": {}, "last_call_at": 0.0}

    def _read(self) -> dict:
        day = time.strftime("%Y-%m-%d")
        if not self.path.exists():
            return self._fresh(day)
        try:
            data = json.loads(self.path.read_text())
            if data.get("day") != day:
                return self._fresh(day)
            if not isinstance(data.get("calls"), int) or data["calls"] < 0:
                raise ValueError("invalid call counter")
            data.setdefault("by_kind", {})
            data.setdefault("last_call_at", 0.0)
            return data
        except Exception as exc:
            # Unknown usage must fail closed. Resetting a corrupt counter could
            # silently exceed the vendor's daily cap after every restart.
            raise RuntimeError(f"quota ledger unreadable: {type(exc).__name__}")

    def take(self, kind: str) -> tuple[bool, str]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            with self._thread_lock, lock_path.open("a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                data = self._read()
                if data["calls"] >= self.limit:
                    return False, f"daily quota spent ({data['calls']}/{self.limit})"
                gap = time.time() - float(data.get("last_call_at", 0))
                if gap < MIN_SECONDS_BETWEEN_CALLS:
                    time.sleep(MIN_SECONDS_BETWEEN_CALLS - gap)
                data["calls"] += 1
                data["by_kind"][kind] = int(data["by_kind"].get(kind, 0)) + 1
                data["last_call_at"] = time.time()
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True))
                os.replace(tmp, self.path)
                self.error = None
                return True, ""
        except Exception as exc:
            self.error = str(exc)
            return False, self.error

    def snapshot(self) -> dict:
        try:
            data = self._read()
            return {"calls_today": data["calls"], "budget": self.limit,
                    "by_kind": dict(data.get("by_kind", {})), "error": self.error}
        except Exception as exc:
            self.error = str(exc)
            return {"calls_today": None, "budget": self.limit,
                    "by_kind": {}, "error": self.error}


@register_backend("atomberg")
class AtombergBackend(DeviceBackend):
    def __init__(self, api_key: str = "", refresh_token: str = "",
                 base_url: str = BASE, use_udp: bool = True,
                 cloud_state_reads: bool = False,
                 cloud_state_min_interval_s: int = 3600,
                 budget_path: str = ".breezeiq-state/atomberg-quota.json",
                 lan_snapshot_path: str = ".breezeiq-state/atomberg-lan.json",
                 **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key
        self.refresh_token = refresh_token
        self.base_url = base_url.rstrip("/")
        self._access_token: Optional[str] = None
        self._token_at = 0.0
        self._last_error: Optional[str] = None
        self.cloud_state_reads = bool(cloud_state_reads)
        self.cloud_state_min_interval_s = max(
            300, int(cloud_state_min_interval_s))
        # Keep the complete normalized readback.  Power alone cannot describe
        # a running fan, and Atomberg exposes the last recorded 1..6 speed in
        # the same response.  This is appliance-reported state, not a physical
        # tachometer measurement.
        self._cloud_state: Optional[dict] = None
        self._cloud_state_at = 0.0
        self.budget_ledger = PersistentDailyBudget(budget_path)
        self.lan_snapshot_path = Path(lan_snapshot_path)
        # Free LAN presence. Commands still cost quota; knowing the fan is
        # alive no longer does.
        self.udp = AtombergUdpListener() if use_udp else None

    def _shared_lan(self) -> dict:
        """Read host-network presence bridged into App Lab's /app mount."""
        try:
            payload = json.loads(self.lan_snapshot_path.read_text())
            written_at = float(payload.get("at", 0))
            file_age = max(0.0, time.time() - written_at)
            devices = {}
            for device_id, raw in (payload.get("devices") or {}).items():
                item = dict(raw) if isinstance(raw, dict) else {}
                beacon_age = item.get("age_s")
                if not isinstance(beacon_age, (int, float)):
                    continue
                item["age_s"] = round(float(beacon_age) + file_age, 1)
                if item["age_s"] < OFFLINE_AFTER_S:
                    devices[str(device_id)] = item
            return {"path": str(self.lan_snapshot_path), "age_s": round(file_age, 1),
                    "error": payload.get("error"), "devices": devices}
        except FileNotFoundError:
            return {"path": str(self.lan_snapshot_path), "age_s": None,
                    "error": "host beacon bridge has not written a snapshot",
                    "devices": {}}
        except Exception as exc:
            return {"path": str(self.lan_snapshot_path), "age_s": None,
                    "error": f"snapshot unreadable: {type(exc).__name__}",
                    "devices": {}}

    def start(self) -> "AtombergBackend":
        if self.udp:
            self.udp.start()
        return self

    # ── auth ────────────────────────────────────────────────────────────
    def _request(self, path: str, headers: dict, payload: Optional[dict] = None):
        """http.client, NOT urllib.

        urllib.request calls .capitalize() on every header name, turning
        'x-api-key' into 'X-api-key'. Atomberg's gateway accepts 'x-api-key'
        and 'X-API-Key' but returns 500 {"message":null} for the mangled form —
        which looks exactly like bad credentials and is not. Measured 2026-08-10.
        http.client sends header names byte-for-byte as given.
        """
        host = urllib.parse.urlparse(self.base_url).netloc
        body = json.dumps(payload) if payload is not None else None
        conn = http.client.HTTPSConnection(host, timeout=TIMEOUT_S)
        try:
            conn.request("POST" if payload is not None else "GET",
                         path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode()
            if resp.status >= 400:
                raise ApiError(resp.status, raw)
            return json.loads(raw) if raw else {}
        finally:
            conn.close()

    def _fetch_access_token(self) -> Optional[str]:
        """Exchange the long-lived refresh token for a short-lived access token."""
        if not (self.api_key and self.refresh_token):
            self._last_error = "api_key / refresh_token not configured"
            return None
        allowed, why = self.budget_ledger.take("token")
        if not allowed:
            self._last_error = why
            return None
        try:
            data = self._request("/v1/get_access_token", {
                "Authorization": f"Bearer {self.refresh_token}",
                "x-api-key": self.api_key,
            })
            token = (data.get("message") or {}).get("access_token") or data.get("access_token")
            if token:
                self._access_token, self._token_at = token, time.time()
                self._last_error = None
            else:
                self._last_error = "access-token response did not contain a token"
            return token
        except Exception as exc:
            self._last_error = f"token fetch failed: {exc}"
            return None

    def _auth_headers(self) -> Optional[dict]:
        # Refresh a little early rather than discover expiry mid-command.
        if not self._access_token or time.time() - self._token_at > 20 * 60:
            if not self._fetch_access_token():
                return None
        return {"Authorization": f"Bearer {self._access_token}",
                "x-api-key": self.api_key,
                "Content-Type": "application/json"}

    # ── commands ────────────────────────────────────────────────────────
    def _send(self, device: Device, command: dict) -> CommandResult:
        if not device.address:
            return CommandResult(False, device.key, "Atomberg fan ID is not configured",
                                 outcome="rejected")
        headers = self._auth_headers()
        if headers is None:
            return CommandResult(False, device.key, self._last_error or "not authenticated")
        allowed, why = self.budget_ledger.take("command")
        if not allowed:
            return CommandResult(False, device.key, why, skipped=True,
                                 outcome="quota_hold")
        payload = {"device_id": device.address, "command": command}
        try:
            data = self._request("/v1/send_command", headers, payload)
            ok = bool(data.get("status", "").lower() == "success" or data.get("message"))
            if ok:
                # Force the registry's independent post-command readback to
                # query the appliance instead of returning an hourly cache.
                self._cloud_state_at = 0.0
            return CommandResult(ok, device.key,
                                 "Atomberg accepted command" if ok
                                 else "Atomberg did not acknowledge command",
                                 requested=command, acknowledged=ok,
                                 reported=None)
        except ApiError as e:
            if e.status == 401:                    # token aged out mid-flight
                self._access_token = None
                return CommandResult(False, device.key, "token expired, will retry")
            return CommandResult(False, device.key, str(e))
        except Exception as exc:
            return CommandResult(False, device.key, f"unreachable: {exc}")

    def set_power(self, device: Device, on: bool) -> CommandResult:
        return self._send(device, {"power": bool(on)})

    def set_speed(self, device: Device, speed: int) -> CommandResult:
        """Speed is the fan's own scale: 0 is off, 1..6 are the vendor speeds.

        This backend no longer rescales. A caller with its own ladder levels
        translates before it gets here, so the value that is journaled, read
        back and shown on the dashboard is the same number the fan reports.
        """
        if device.kind is not DeviceKind.FAN:
            return self.set_power(device, speed > 0)
        if speed <= 0:
            return self.set_power(device, False)
        return self._send(device, {"power": True, "speed": min(6, int(speed))})

    # Extra documented commands, free to expose since the transport exists.
    def set_led(self, device: Device, on: bool) -> CommandResult:
        return self._send(device, {"led": bool(on)})

    def set_sleep(self, device: Device, on: bool) -> CommandResult:
        return self._send(device, {"sleep": bool(on)})

    def nudge_speed(self, device: Device, delta: int) -> CommandResult:
        """speedDelta accepts -5..+5 — cheaper than a full state read when we
        only want one step, which matters under a 100-call/day quota."""
        return self._send(device, {"speedDelta": max(-5, min(5, delta))})

    def get_state(self, device: Device) -> Optional[bool]:
        state = self.read_state(device)
        if not state.available:
            return None
        if isinstance(state.state, dict):
            return state.state.get("power")
        return bool(state.state)

    @staticmethod
    def _normalized_cloud_state(raw_state: dict) -> dict:
        """Return the small, non-identifying state used by the control plane."""
        raw_power = raw_state.get("power")
        power = (raw_power if isinstance(raw_power, bool)
                 else str(raw_power).lower() == "true")
        state = {"power": power}
        raw_speed = raw_state.get("last_recorded_speed", raw_state.get("speed"))
        try:
            speed = int(raw_speed)
        except (TypeError, ValueError):
            speed = None
        if speed is not None and 0 <= speed <= 6:
            state["speed"] = speed
            state["speed_scale_max"] = 5
        return state

    def commands_exhausted_until(self, device) -> "float | None":
        """Local midnight when today's budget is gone, else None.

        The ledger keys on the calendar day, so a spent budget is spent until
        the date rolls over — not for `failure_retry_seconds`. Saying so stops
        the loop asking every minute and stops each refusal becoming a row in
        the room's history.
        """
        try:
            spent = self.budget_ledger.snapshot()
        except Exception:
            return None                      # unknown is not "exhausted"
        if int(spent.get("calls_today") or 0) < int(spent.get("budget") or 0):
            return None
        now = time.localtime()
        midnight = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                                23, 59, 59, 0, 0, now.tm_isdst)) + 1
        return midnight

    def read_state(self, device: Device) -> ReportedState:
        """LAN readback first. Cloud fallback requires explicit opt-in."""
        if self.udp:
            local = self.udp.local_state(device.address)
            if local is not None:
                return ReportedState(device.key, True, {"power": bool(local)},
                                     "atomberg-lan", "state from UDP beacon")
        shared = self._shared_lan().get("devices", {}).get(device.address)
        if shared is not None:
            if isinstance(shared.get("power"), bool):
                return ReportedState(
                    device.key, True, {"power": shared["power"]},
                    "atomberg-lan-bridge", "state from board-host UDP beacon",
                    at=time.time() - float(shared.get("age_s", 0)))
            if not self.cloud_state_reads:
                return ReportedState(
                    device.key, False, source="atomberg-lan-bridge",
                    detail="fan is online; beacon carries no power or speed state")
        if self.udp and self.udp.is_online(device.address) is False:
            if not self.cloud_state_reads:
                return ReportedState(device.key, False, source="atomberg-lan",
                                     detail="fan beacon is stale")
        if not self.cloud_state_reads:
            return ReportedState(
                device.key, False, source="atomberg-lan",
                detail="LAN beacon has no state; cloud readback disabled")
        cache_age = time.time() - self._cloud_state_at
        if (self._cloud_state is not None
                and cache_age < self.cloud_state_min_interval_s):
            cached = (dict(self._cloud_state)
                      if isinstance(self._cloud_state, dict)
                      else {"power": bool(self._cloud_state)})
            return ReportedState(
                device.key, True, cached,
                "atomberg-cache",
                f"cached API state; captured {cache_age:.0f}s ago",
                at=self._cloud_state_at)
        headers = self._auth_headers()
        if headers is None:
            return ReportedState(device.key, False, source=self.name,
                                 detail=self._last_error or "not authenticated")
        allowed, why = self.budget_ledger.take("state")
        if not allowed:
            return ReportedState(device.key, False, source=self.name, detail=why)
        try:
            data = self._request(f"/v1/get_device_state?device_id={device.address}", headers)
            states = (data.get("message") or {}).get("device_state") or []
            if states:
                state = self._normalized_cloud_state(states[0])
                self._cloud_state = state
                self._cloud_state_at = time.time()
                return ReportedState(
                    device.key, True, state, self.name,
                    "state from Atomberg API; speed is reported, not tachometer measured")
            return ReportedState(device.key, False, source=self.name,
                                 detail="state response was empty")
        except Exception as exc:
            return ReportedState(device.key, False, source=self.name,
                                 detail=f"state read failed: {type(exc).__name__}")

    def devices_online(self) -> list[dict]:
        """List what the account can see — the fastest way to find device_id."""
        headers = self._auth_headers()
        if headers is None:
            return []
        allowed, why = self.budget_ledger.take("devices")
        if not allowed:
            self._last_error = why
            return []
        try:
            data = self._request("/v1/get_list_of_devices", headers)
            return (data.get("message") or {}).get("devices_list", [])
        except Exception:
            return []

    def health(self) -> dict:
        configured = bool(self.api_key and self.refresh_token)
        budget = self.budget_ledger.snapshot()
        info = {"backend": self.name,
                "ok": configured and self._last_error is None and not budget["error"],
                "configured": configured, "error": self._last_error,
                "cloud_state_reads": self.cloud_state_reads,
                "cloud_state_min_interval_s": self.cloud_state_min_interval_s,
                **budget}
        if self.udp:
            lan = self.udp.health()
            shared = self._shared_lan()
            for device_id, entry in shared["devices"].items():
                current = lan["devices"].get(device_id)
                if current is None or entry["age_s"] < current.get("age_s", 1e9):
                    lan["devices"][device_id] = entry
            lan["bridge"] = {key: shared[key] for key in ("path", "age_s", "error")}
            info["lan"] = lan
        return info
