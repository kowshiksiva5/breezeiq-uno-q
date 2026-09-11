"""Bridge host-network Atomberg beacons into App Lab's board-backed state."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .atomberg_lan import AtombergUdpListener


def write_snapshot(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    state_dir = Path(os.environ.get(
        "BREEZEIQ_STATE_DIR",
        "/home/arduino/ArduinoApps/breezeiq/state"))
    target = Path(os.environ.get(
        "ATOMBERG_LAN_SNAPSHOT", state_dir / "atomberg-lan.json"))
    listener = AtombergUdpListener().start()
    while True:
        write_snapshot(target, listener.snapshot())
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
