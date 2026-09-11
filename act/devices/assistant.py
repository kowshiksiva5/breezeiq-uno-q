"""Home Assistant backend — anything HA speaks, we speak.

Covers the Wipro/Tuya devices today and every future integration for free.
Stdlib only: the board's Debian has no pip without a sudo password, and this
must run there unchanged.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from .base import (CommandResult, Device, DeviceBackend, DeviceKind,
                   ReportedState, register_backend)

TIMEOUT_S = 6


@register_backend("homeassistant")
class HomeAssistantBackend(DeviceBackend):
    def __init__(self, base_url: str = "http://127.0.0.1:8123",
                 token: str = "", **kwargs):
        super().__init__(**kwargs)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._last_error: Optional[str] = None

    # ── transport ───────────────────────────────────────────────────────
    def _call(self, path: str, payload: Optional[dict] = None):
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"},
            method="POST" if payload is not None else "GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}

    def _service(self, domain: str, service: str, data: dict):
        return self._call(f"/api/services/{domain}/{service}", data)

    def _configured(self) -> bool:
        return bool(self.base_url and self.token)

    def _unconfigured(self, device: Device) -> Optional[CommandResult]:
        if not device.address:
            return CommandResult(False, device.key, "Home Assistant entity is not configured",
                                 outcome="rejected")
        if not self.token:
            return CommandResult(False, device.key, "Home Assistant token is not configured",
                                 outcome="rejected")
        return None

    # ── commands ────────────────────────────────────────────────────────
    def set_power(self, device: Device, on: bool) -> CommandResult:
        if blocked := self._unconfigured(device):
            return blocked
        domain = device.address.split(".")[0]      # light.x -> light
        try:
            self._service(domain, "turn_on" if on else "turn_off",
                          {"entity_id": device.address})
            self._last_error = None
            return CommandResult(True, device.key,
                                 f"Home Assistant accepted {'on' if on else 'off'}",
                                 requested={"power": bool(on)},
                                 acknowledged=True, reported=None)
        except urllib.error.HTTPError as e:
            self._last_error = f"HTTP {e.code}"
            return CommandResult(False, device.key,
                                 "token rejected" if e.code == 401 else f"HTTP {e.code}")
        except Exception as exc:
            self._last_error = str(exc)
            return CommandResult(False, device.key, f"HA unreachable: {exc}")

    def set_speed(self, device: Device, speed: int) -> CommandResult:
        """HA fans take a percentage. Speed arrives on the fan's own 0..6
        scale, matching the registry contract, so scale it proportionally."""
        if device.kind is not DeviceKind.FAN:
            return self.set_power(device, speed > 0)
        if speed <= 0:
            return self.set_power(device, False)
        if blocked := self._unconfigured(device):
            return blocked
        pct = round(min(6, int(speed)) / 6 * 100)
        try:
            self._service("fan", "turn_on",
                          {"entity_id": device.address, "percentage": pct})
            return CommandResult(True, device.key,
                                 f"Home Assistant accepted {pct}%",
                                 requested={"speed": speed, "percentage": pct},
                                 acknowledged=True, reported=None)
        except Exception as exc:
            return CommandResult(False, device.key, f"HA unreachable: {exc}")

    def set_position(self, device: Device, percent: int) -> CommandResult:
        if blocked := self._unconfigured(device):
            return blocked
        try:
            self._service("cover", "set_cover_position",
                          {"entity_id": device.address, "position": percent})
            return CommandResult(True, device.key,
                                 f"Home Assistant accepted position {percent}%",
                                 requested={"position": percent},
                                 acknowledged=True, reported=None)
        except Exception as exc:
            return CommandResult(False, device.key, f"HA unreachable: {exc}")

    def get_state(self, device: Device) -> Optional[bool]:
        """Used to notice a human has taken over from the app."""
        try:
            data = self._call(f"/api/states/{device.address}")
            return data.get("state") == "on"
        except Exception:
            return None

    def read_state(self, device: Device) -> ReportedState:
        """Read entity state independently after a command.

        Numeric measurements retain Home Assistant's own unit, device class,
        and state class.  A raw number without a declared unit stays unitless;
        this adapter never guesses watts, watt-hours, temperature, or energy.
        """
        if not device.address:
            return ReportedState(device.key, False, source=self.name,
                                 detail="entity is not configured")
        if not self.token:
            return ReportedState(device.key, False, source=self.name,
                                 detail="token is not configured")
        try:
            data = self._call(f"/api/states/{device.address}")
            raw = data.get("state")
            attrs = data.get("attributes") or {}
            # `unavailable` and `unknown` are Home Assistant's own way of saying
            # it cannot tell us — an integration that is down, an entity that has
            # never reported. A 200 on the API is not the same as an answer, and
            # treating one as "available" is how the room page came to print
            # "Running: Air conditioner" for a socket whose state was literally
            # `unavailable`. Unknown must read as unknown.
            if raw in ("unavailable", "unknown"):
                self._last_error = None
                return ReportedState(
                    device.key, False, {"raw_state": raw}, self.name,
                    f"Home Assistant reports the entity as {raw}")
            state: dict = {"raw_state": raw}
            if raw in ("on", "off"):
                state["power"] = raw == "on"
            try:
                state["value"] = float(raw)
            except (TypeError, ValueError):
                pass
            for key in ("unit_of_measurement", "device_class", "state_class"):
                if key in attrs:
                    state[key] = attrs[key]
            # Preserve HA's source timestamp. The energy collector rejects
            # stale or replayed readings instead of integrating poll time.
            if data.get("last_updated"):
                state["last_updated"] = data["last_updated"]
            if data.get("last_changed"):
                state["last_changed"] = data["last_changed"]
            # Percentage is a reported fan attribute.  Keep vendor scale and
            # name explicit so it cannot be confused with a 0..6 fan speed.
            if isinstance(attrs.get("percentage"), (int, float)):
                state["percentage"] = attrs["percentage"]
            self._last_error = None
            return ReportedState(device.key, True, state, self.name,
                                 "Home Assistant entity state")
        except urllib.error.HTTPError as exc:
            self._last_error = f"HTTP {exc.code}"
            detail = "token rejected" if exc.code == 401 else f"HTTP {exc.code}"
            return ReportedState(device.key, False, source=self.name, detail=detail)
        except Exception as exc:
            self._last_error = str(exc)
            return ReportedState(device.key, False, source=self.name,
                                 detail=f"Home Assistant unreachable: {exc}")

    def read_numeric_entity(self, entity_id: str) -> ReportedState:
        """Read power/energy/temperature entities without assigning a unit."""
        probe = Device(entity_id, DeviceKind.SWITCH, self.name, entity_id,
                       label=entity_id)
        state = self.read_state(probe)
        state.device = entity_id
        if state.available and not isinstance(state.state, dict):
            return ReportedState(entity_id, False, source=self.name,
                                 detail="entity did not return structured state")
        if state.available and "value" not in state.state:
            return ReportedState(entity_id, False, state.state, self.name,
                                 "entity state is non-numeric")
        return state

    def automation_states(self) -> list[dict]:
        """Read-only audit for competing HA policy.  Enabled rows need review."""
        if not self.token:
            return []
        try:
            states = self._call("/api/states")
            return [{"entity_id": s.get("entity_id"), "state": s.get("state")}
                    for s in states
                    if str(s.get("entity_id", "")).startswith("automation.")]
        except Exception:
            return []

    def measurement_entities(self) -> list[dict]:
        """Read-only discovery for explicitly unit-declared power and energy."""
        if not self.token:
            return []
        try:
            states = self._call("/api/states")
            found = []
            for item in states:
                attrs = item.get("attributes") or {}
                device_class = attrs.get("device_class")
                unit = attrs.get("unit_of_measurement")
                if device_class not in {"power", "energy"} and unit not in {
                        "W", "kW", "Wh", "kWh"}:
                    continue
                found.append({
                    "entity_id": item.get("entity_id"),
                    "state": item.get("state"),
                    "unit_of_measurement": unit,
                    "device_class": device_class,
                    "state_class": attrs.get("state_class"),
                    "friendly_name": attrs.get("friendly_name"),
                    "last_updated": item.get("last_updated"),
                })
            return sorted(found, key=lambda row: row["entity_id"] or "")
        except Exception:
            return []

    # ── introspection, so setup mistakes are obvious ────────────────────
    def entities(self, prefix: str = "") -> list[str]:
        try:
            states = self._call("/api/states")
            return sorted(s["entity_id"] for s in states
                          if s["entity_id"].startswith(prefix))
        except Exception:
            return []

    def health(self) -> dict:
        if not self._configured():
            return {"backend": self.name, "ok": False, "configured": False,
                    "url": self.base_url, "error": "HA_TOKEN is not configured"}
        try:
            self._call("/api/")
            return {"backend": self.name, "ok": True, "configured": True,
                    "url": self.base_url}
        except Exception as exc:
            return {"backend": self.name, "ok": False, "configured": True,
                    "url": self.base_url,
                    "error": self._last_error or str(exc)}
