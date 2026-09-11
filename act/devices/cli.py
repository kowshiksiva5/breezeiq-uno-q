"""Read-only integration audit and explicitly gated manual control.

Examples:
    python3 -m devices.cli health
    python3 -m devices.cli automations
    python3 -m devices.cli state ac
    python3 -m devices.cli numeric sensor.ac_power
    python3 -m devices.cli --live --operator dashboard --reason "too warm" on ac

Mutation needs both ``--live`` and ``BREEZEIQ_SAFETY_VALIDATED=1``.  A typo or
fresh checkout therefore cannot switch a real appliance.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os

from .config import build_registry


MUTATING = {"on", "off", "speed", "return-auto"}


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="allow a real command after the safety validation gate")
    ap.add_argument("--operator", default="cli")
    ap.add_argument("--reason", default="manual CLI request")
    ap.add_argument("--ttl", type=int, default=1800,
                    help="manual override duration in seconds")
    ap.add_argument("command", choices=["health", "list", "entities",
                                         "automations", "state", "numeric",
                                         "on", "off", "speed", "return-auto"])
    ap.add_argument("args", nargs="*")
    return ap


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    mutating = args.command in MUTATING
    live = mutating and args.live
    if mutating and not args.live:
        print(json.dumps({"ok": False, "outcome": "dry_run",
                          "detail": "refused: add --live after reviewing the command"}))
        return 2
    if live and os.environ.get("BREEZEIQ_SAFETY_VALIDATED", "0") != "1":
        print(json.dumps({"ok": False, "outcome": "safety_gate",
                          "detail": "refused: BREEZEIQ_SAFETY_VALIDATED is not 1"}))
        return 2

    reg = build_registry(live_enabled=live)
    cmd, rest = args.command, args.args
    try:
        if cmd == "health":
            print(json.dumps(reg.health(), indent=2))
        elif cmd == "list":
            for d in reg.devices():
                print(f"  {d.key:<12} {d.kind.value:<7} {d.backend:<14} "
                      f"{d.address or '(unset)'}")
        elif cmd in ("entities", "automations", "numeric"):
            backend = reg.backend("homeassistant")
            if cmd == "entities":
                for entity in backend.entities(rest[0] if rest else ""):
                    print(" ", entity)
            elif cmd == "automations":
                print(json.dumps(backend.automation_states(), indent=2))
            else:
                if not rest:
                    raise ValueError("numeric requires an entity_id")
                print(json.dumps(asdict(backend.read_numeric_entity(rest[0])), indent=2))
        elif cmd == "state":
            if not rest:
                raise ValueError("state requires a device key")
            print(json.dumps(asdict(reg.read_state(rest[0])), indent=2))
        elif cmd in ("on", "off"):
            if not rest:
                raise ValueError(f"{cmd} requires a device key")
            result = reg.manual_power(rest[0], cmd == "on", operator=args.operator,
                                      reason=args.reason, ttl_seconds=args.ttl)
            print(json.dumps(result.as_dict(), indent=2))
            return 0 if result.ok and not result.skipped else 3
        elif cmd == "speed":
            if len(rest) != 2:
                raise ValueError("speed requires DEVICE and 0..6")
            speed = int(rest[1])
            if speed not in range(7):
                raise ValueError("speed must be 0..6")
            result = reg.manual_speed(rest[0], speed, operator=args.operator,
                                      reason=args.reason, ttl_seconds=args.ttl)
            print(json.dumps(result.as_dict(), indent=2))
            return 0 if result.ok and not result.skipped else 3
        elif cmd == "return-auto":
            if not rest:
                raise ValueError("return-auto requires a device key")
            cleared = reg.return_to_auto(rest[0], operator=args.operator,
                                         reason=args.reason)
            print(json.dumps({"ok": True, "device": rest[0],
                              "cleared": cleared, "mode": "automatic"}))
    except (ValueError, IndexError) as exc:
        print(json.dumps({"ok": False, "outcome": "invalid_request",
                          "detail": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
