"""In-memory backend. Lets the whole control loop be tested with no devices,
and keeps the 13 ladder invariants runnable on a laptop."""
from __future__ import annotations

from typing import Optional

from .base import (CommandResult, Device, DeviceBackend, ReportedState,
                   register_backend)


@register_backend("mock")
class MockBackend(DeviceBackend):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.state: dict[str, object] = {}

    def set_power(self, device: Device, on: bool) -> CommandResult:
        self.state[device.key] = on
        return CommandResult(True, device.key, f"mock -> {'on' if on else 'off'}")

    def set_speed(self, device: Device, speed: int) -> CommandResult:
        self.state[device.key] = speed
        return CommandResult(True, device.key, f"mock -> speed {speed}")

    def set_position(self, device: Device, percent: int) -> CommandResult:
        self.state[device.key] = percent
        return CommandResult(True, device.key, f"mock -> {percent}%")

    def get_state(self, device: Device) -> Optional[bool]:
        v = self.state.get(device.key)
        return bool(v) if v is not None else None

    def read_state(self, device: Device) -> ReportedState:
        v = self.state.get(device.key)
        return ReportedState(device.key, v is not None, v, self.name,
                             "simulated state" if v is not None else "unset")
