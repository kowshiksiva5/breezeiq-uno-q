"""Layer 2 — device control.

One job: turn an intent ("fan to speed 3") into whatever a specific vendor
wants, and report honestly whether it worked. The ladder expresses intent and
never learns a protocol.

Adding a vendor = one new file subclassing DeviceBackend + @register_backend.
No other file changes. That is the whole design goal.
"""
from .base import (Device, DeviceBackend, DeviceKind, CommandResult, ReportedState,
                   register_backend, build_backend, available_backends,
                   DeviceRegistry)
from .journal import ActuationJournal
from .control_socket import ControlSocketServer
from . import assistant, atomberg, mcu, simulated  # noqa: F401  (self-registering)

__all__ = ["Device", "DeviceBackend", "DeviceKind", "CommandResult", "ReportedState",
           "register_backend", "build_backend", "available_backends",
           "DeviceRegistry", "ActuationJournal", "ControlSocketServer"]
