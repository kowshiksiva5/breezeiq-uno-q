"""Declare the room's devices here. This is the only file you edit to add one.

Credentials come from the environment, never from this file — it is committed.
    HA_URL, HA_TOKEN
    ATOMBERG_API_KEY, ATOMBERG_REFRESH_TOKEN, ATOMBERG_FAN_ID
"""
from __future__ import annotations

import os

from .base import Device, DeviceKind, DeviceRegistry, build_backend
from .journal import ActuationJournal

def devices_from_env() -> list[Device]:
    """Build the physical registry after the board-local environment loads."""
    ac_present = os.environ.get(
        "BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1"
    return [
        Device(key="tubelight", kind=DeviceKind.LIGHT, backend="homeassistant",
               address=os.environ.get("HA_LIGHT_ENTITY", ""),
               label="Wipro tubelight",
               # The lamp illuminates the very sensors that judge the room's
               # light, so any regression in the fusion hold recreates a
               # lamp-on -> "bright" -> lamp-off -> "dark" -> lamp-on cycle
               # (observed live in the command journal before the current
               # fusion code). The registry's turn-on rate limit makes that
               # failure class physically slow regardless of policy bugs:
               # at most one ON per 5 minutes, matching the ladder's dwell.
               min_command_interval_seconds=300),

        Device(key="ac", kind=DeviceKind.SWITCH, backend="homeassistant",
               address=os.environ.get("HA_AC_ENTITY", "") if ac_present else "",
               label=("AC via Wipro plug" if ac_present else
                      "AC response, physical switch absent"),
               # A compressor cut and restarted inside 3 minutes can stall and
               # burn out. Enforced by the registry for every caller.
               min_off_seconds=180, min_command_interval_seconds=5),

        Device(key="fan", kind=DeviceKind.FAN, backend="atomberg",
               address=os.environ.get("ATOMBERG_FAN_ID", ""),
               label="Atomberg ceiling fan", failure_retry_seconds=300),

        # The board's own actuator. Its address is an MCU-side target name,
        # not a network identity: the command is `CMD SRV1 OPEN` down the same
        # serial link the sensor telemetry comes up. A DC motor reversed
        # faster than it can travel just wastes the run, so the ladder's
        # shading decisions are spaced by the registry, not by the sketch.
        Device(key="blinds", kind=DeviceKind.COVER, backend="mcu",
               address=os.environ.get("BREEZEIQ_BLINDS_TARGET", "SRV1"),
               label="Blinds motor", min_command_interval_seconds=10),

        # The VENT rung's device key. No actuator is fitted to the window —
        # the firmware rejects its old SRV2 target — but the key stays
        # registered because the plan layer still carries a window field;
        # removing it belongs to the vent-rung disable, not a label pass.
        Device(key="window", kind=DeviceKind.COVER, backend="mcu",
               address=os.environ.get("BREEZEIQ_WINDOW_TARGET", "SRV2"),
               label="Window (no actuator fitted)",
               min_command_interval_seconds=10),
    ]


def build_registry(use_mock: bool = False, *, live_enabled: bool = False,
                   journal_path: str | None = None) -> DeviceRegistry:
    """Assemble the registry. Unconfigured backends still load — they report
    their own health honestly rather than crashing the control loop."""
    db = journal_path or os.environ.get("BREEZEIQ_DB", "telemetry.sqlite3")
    registry = DeviceRegistry(live_enabled=live_enabled,
                              journal=ActuationJournal(db))
    devices = devices_from_env()

    if use_mock:
        registry.add_backend(build_backend("mock"))
        for d in devices:
            registry.add_device(Device(
                key=d.key, kind=d.kind, backend="mock", address=d.address,
                label=d.label, min_off_seconds=d.min_off_seconds,
                min_command_interval_seconds=d.min_command_interval_seconds,
                failure_retry_seconds=d.failure_retry_seconds))
        return registry

    registry.add_backend(build_backend(
        "homeassistant",
        base_url=os.environ.get("HA_URL", "http://127.0.0.1:8123"),
        token=os.environ.get("HA_TOKEN", "")))
    state_dir = os.environ.get("BREEZEIQ_STATE_DIR", ".breezeiq-state")
    registry.add_backend(build_backend(
        "atomberg",
        api_key=os.environ.get("ATOMBERG_API_KEY", ""),
        refresh_token=os.environ.get("ATOMBERG_REFRESH_TOKEN", ""),
        cloud_state_reads=os.environ.get("ATOMBERG_CLOUD_STATE_READS", "0") == "1",
        cloud_state_min_interval_s=int(os.environ.get(
            "ATOMBERG_CLOUD_STATE_MIN_INTERVAL_S", "3600")),
        budget_path=os.environ.get(
            "ATOMBERG_BUDGET_FILE",
            os.path.join(state_dir, "atomberg-quota.json")),
        lan_snapshot_path=os.environ.get(
            "ATOMBERG_LAN_SNAPSHOT",
            os.path.join(state_dir, "atomberg-lan.json"))))
    # No credentials and no address: the board's own motor is reached through
    # the sensor reader's link, which this backend borrows at first command.
    registry.add_backend(build_backend("mcu"))

    for d in devices:
        registry.add_device(d)
    return registry
