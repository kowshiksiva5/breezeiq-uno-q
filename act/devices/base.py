"""Device abstraction: intent in, honest result out."""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .journal import ActuationJournal


class DeviceKind(str, Enum):
    """What the ladder wants to do, not what the vendor calls it."""
    LIGHT = "light"
    FAN = "fan"
    SWITCH = "switch"          # a plug: AC, heater, anything mains
    COVER = "cover"            # blinds motor


@dataclass
class Device:
    """A thing we can command. `key` is how the ladder refers to it."""
    key: str                          # "ac", "fan", "tubelight"
    kind: DeviceKind
    backend: str                      # "homeassistant" | "atomberg" | "mock"
    address: str                      # entity_id, device_id — backend's business
    label: str = ""
    # Hardware protection lives with the device, not the caller: a compressor
    # must not short-cycle regardless of which layer asks it to.
    min_off_seconds: int = 0
    min_command_interval_seconds: float = 1.0
    failure_retry_seconds: float = 60.0
    _last_off_at: float = field(default=0.0, repr=False)
    _last_state: Optional[bool] = field(default=None, repr=False)


@dataclass
class CommandResult:
    ok: bool
    device: str
    detail: str = ""
    skipped: bool = False             # refused on purpose, e.g. min-off-time
    requested: object = None
    acknowledged: Optional[bool] = None
    reported: object = None
    actor: str = "automation"
    reason: str = ""
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: float = field(default_factory=time.time)
    outcome: str = ""

    def __post_init__(self) -> None:
        if self.acknowledged is None and not self.skipped:
            self.acknowledged = bool(self.ok)
        if not self.outcome:
            if self.skipped:
                self.outcome = "skipped"
            elif self.acknowledged:
                self.outcome = "acknowledged"
            else:
                self.outcome = "failed"

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict:
        return {"command_id": self.command_id, "at": self.at,
                "device": self.device, "requested": self.requested,
                "acknowledged": self.acknowledged, "reported": self.reported,
                "actor": self.actor, "reason": self.reason,
                "outcome": self.outcome, "ok": self.ok,
                "skipped": self.skipped, "detail": self.detail}


@dataclass
class ReportedState:
    """Independent readback.  `available=False` is unknown, never off."""
    device: str
    available: bool
    state: object = None
    source: str = "unknown"
    detail: str = ""
    at: float = field(default_factory=time.time)


class DeviceBackend(ABC):
    """Base class for a vendor integration."""

    name = "unnamed"

    def __init__(self, **kwargs):
        self.config = kwargs

    def start(self) -> "DeviceBackend":
        return self

    def commands_exhausted_until(self, device: "Device") -> Optional[float]:
        """Epoch second after which commands might work again, or None.

        A backend that CANNOT send right now, and knows roughly when that
        changes, says so here and the registry stops asking. Default None means
        "no idea, keep the existing retry behaviour" — so no other backend has
        to care. Added because the vendor budget resets at local midnight while
        `failure_retry_seconds` is 60, so a spent fan budget produced ~1,440
        futile attempts a day (7,399 journalled on the live unit) and buried
        the room's real history under things that never happened.
        """
        return None

    @abstractmethod
    def set_power(self, device: Device, on: bool) -> CommandResult:
        """Turn a device on or off."""

    def set_speed(self, device: Device, speed: int) -> CommandResult:
        """Speed is the fan's own scale, 0 is off. Callers with policy levels
        translate before they get here. Backends that cannot vary speed fall
        back to on/off, which is honest and keeps the FAN rung usable."""
        return self.set_power(device, speed > 0)

    def set_position(self, device: Device, percent: int) -> CommandResult:
        return CommandResult(False, device.key, "backend has no cover support")

    def get_state(self, device: Device) -> Optional[bool]:
        """None = unknown. Used to detect a human overriding us."""
        return None

    def read_state(self, device: Device) -> ReportedState:
        state = self.get_state(device)
        return ReportedState(device.key, state is not None, state,
                             self.name, "reported by backend" if state is not None
                             else "reported state unavailable")

    def health(self) -> dict:
        return {"backend": self.name, "ok": True}


# ── registry ────────────────────────────────────────────────────────────
_BACKENDS: dict[str, Callable[..., DeviceBackend]] = {}


def register_backend(name: str):
    def deco(cls):
        cls.name = name
        _BACKENDS[name] = cls
        return cls
    return deco


def build_backend(name: str, **kwargs) -> DeviceBackend:
    if name not in _BACKENDS:
        raise KeyError(f"unknown backend {name!r}; have {sorted(_BACKENDS)}")
    return _BACKENDS[name](**kwargs)


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


class DeviceRegistry:
    """Holds the devices and routes each command to the right backend.

    This is the only object the control loop touches. Swapping a device from
    Tuya to Atomberg is a config change here, not a code change upstairs.
    """

    def __init__(self, *, live_enabled: bool = False,
                 journal: Optional[ActuationJournal] = None):
        self._devices: dict[str, Device] = {}
        self._backends: dict[str, DeviceBackend] = {}
        self.log: list[CommandResult] = []
        self.live_enabled = bool(live_enabled)
        self.journal = journal or ActuationJournal()

    def add_backend(self, backend: DeviceBackend) -> "DeviceRegistry":
        self._backends[backend.name] = backend.start()
        return self

    def add_device(self, device: Device) -> "DeviceRegistry":
        self._devices[device.key] = device
        return self

    def get(self, key: str) -> Optional[Device]:
        return self._devices.get(key)

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def backend(self, name: str) -> Optional[DeviceBackend]:
        """Expose a configured backend for read-only auxiliary collectors."""
        return self._backends.get(name)

    def _result(self, key: str, detail: str, *, ok: bool = False,
                skipped: bool = False, outcome: str = "",
                requested: object = None) -> CommandResult:
        return CommandResult(ok, key, detail, skipped=skipped, outcome=outcome,
                             requested=requested)

    def _dispatch(self, key: str, fn_name: str, *args,
                  actor: str = "automation", reason: str = "",
                  override_id: Optional[str] = None,
                  requested: object = None) -> CommandResult:
        # The single choke point between a decision and a real appliance. Seven
        # guards run below, and the ORDER is the contract — each one assumes
        # every guard above it already passed, so reordering them changes
        # behaviour even though no individual check changes.
        #
        #   1. journal fail-closed   — an unwritable journal refuses every
        #      command rather than acting unrecorded. It is first because the
        #      guards after it read their state (overrides, last-off time,
        #      backoff) from that same journal: a journal we cannot trust to
        #      write is a journal we cannot trust to read.
        #   2. device / backend / address rejection — three "does this even
        #      exist" checks that resolve the request to something callable.
        #      They are `rejected`, not `skipped`: nothing was held back, the
        #      configuration is simply wrong.
        #   3. override precedence   — a human's manual override outranks the
        #      ladder. Only `actor == "automation"` yields; a human command
        #      passes through, which is how an override gets replaced. Placed
        #      above the safety guards so an override still cannot defeat them.
        #   4. compressor min-off    — refuses an ON that would restart a
        #      compressor inside its minimum off-time. Hardware protection, so
        #      it sits above the merely-economic guards and applies to humans
        #      and automation alike.
        #   5. retry backoff         — a command that was NOT acknowledged is
        #      not retried at loop rate. Keyed on the identical `requested`
        #      value, so a genuinely new intent is never suppressed by the
        #      failure of the previous one.
        #   6. rate limit            — collapses repeated ON/adjust traffic.
        #      Deliberately does not gate OFF (`turning_on`): stopping a device
        #      must never be rate limited.
        #   7. dry-run boundary      — the last gate before the backend call.
        #      Last on purpose, so a dry run exercises and journals every guard
        #      above it and reports exactly what a live run would have done.
        #
        # Guards 3-7 return ok=True with skipped=True. That distinction matters
        # upstream: "we deliberately did not act" is a success, not a fault, and
        # only genuine failures should light the health surface.
        #
        # POST-COMMAND READBACK CONTRACT (below the try/except): a backend
        # accepting a command is not evidence the appliance moved, so every
        # non-skipped command takes one independent `read_state`. That readback
        # never rewrites `acknowledged` — protocol acceptance and observed state
        # are recorded as two separate facts, and a read that fails or is
        # unsupported stays an explicit None rather than being coerced into a
        # boolean. Guard state (`last_off_at`, `last_command_at`,
        # `last_requested`, `last_acknowledged`) is committed here and only
        # here, which is why skipped commands leave the guards untouched: a
        # command that never reached the device must not restart its timers.
        if not self.journal.ok or self.journal.error:
            return self._result(
                key, "actuation journal unavailable; command refused",
                skipped=True, outcome="journal_unavailable",
                requested=requested)
        device = self._devices.get(key)
        if device is None:
            return self._record(self._result(key, "no such device", outcome="rejected",
                                             requested=requested),
                                fn_name, actor, reason, override_id)
        backend = self._backends.get(device.backend)
        if backend is None:
            return self._record(self._result(
                key, f"backend {device.backend!r} not configured", outcome="rejected",
                requested=requested),
                fn_name, actor, reason, override_id)
        if not device.address:
            return self._record(self._result(
                key, "device address/entity is not configured", outcome="rejected",
                requested=requested),
                fn_name, actor, reason, override_id)

        active = self.journal.active_override(key)
        if actor == "automation" and active:
            result = self._result(
                key,
                f"manual override active until {time.strftime('%H:%M:%S', time.localtime(active['expires_at']))}",
                ok=True, skipped=True, outcome="manual_override")
            result.requested = requested
            return self._record(result, fn_name, actor, reason,
                                active["override_id"])

        guard = self.journal.guard(key)
        now = time.time()
        if (fn_name == "set_power" and bool(args[0]) and device.min_off_seconds
                and guard["last_off_at"]):
            waited = now - guard["last_off_at"]
            if waited < device.min_off_seconds:
                left = max(1, int(device.min_off_seconds - waited))
                result = self._result(
                    key, f"held off {left}s more to protect the compressor",
                    ok=True, skipped=True, outcome="safety_hold")
                result.requested = requested
                return self._record(result, fn_name, actor, reason, override_id)

        turning_on = fn_name != "set_power" or bool(args[0])
        # A person is not a control loop. The ON throttle exists so an
        # oscillating ladder cannot cycle a relay every tick; a human who
        # pressed a button has already decided, and making them wait out a
        # timer they cannot see reads as a dead button. Observed: OFF answered
        # instantly while ON came back "rate limited for 275.1s", so the lamp
        # could be switched off and then not back on for five minutes.
        #
        # Only the ON throttle is bypassed. The compressor's own minimum-off
        # protection is enforced separately, below this, and is not a comfort
        # policy — it protects hardware and applies to everyone.
        by_hand = actor != "automation"
        # Ask the backend whether sending is possible at all before treating
        # this as an ordinary failure to retry in a minute. A person still gets
        # through: they may have topped the budget up, or be about to learn
        # from the refusal itself.
        blocked_until = None
        if not by_hand:
            try:
                blocked_until = backend.commands_exhausted_until(device)
            except Exception:
                blocked_until = None
        if blocked_until and now < blocked_until:
            mins = max(1, int((blocked_until - now) / 60))
            result = self._result(
                key, f"cannot send for another {mins} min; not retrying until then",
                ok=True, skipped=True, outcome="quota_hold")
            result.requested = requested
            return self._record(result, fn_name, actor, reason, override_id)
        gap = now - guard["last_command_at"]
        if (guard["last_acknowledged"] is False
                and guard["last_requested"] == requested
                and gap < device.failure_retry_seconds):
            result = self._result(
                key, f"failure backoff for {device.failure_retry_seconds - gap:.1f}s",
                ok=True, skipped=True, outcome="retry_backoff")
            result.requested = requested
            return self._record(result, fn_name, actor, reason, override_id)
        if (turning_on and not by_hand and guard["last_command_at"]
                and gap < device.min_command_interval_seconds):
            result = self._result(
                key, f"rate limited for {device.min_command_interval_seconds - gap:.1f}s",
                ok=True, skipped=True, outcome="rate_limited")
            result.requested = requested
            return self._record(result, fn_name, actor, reason, override_id)

        if not self.live_enabled:
            result = self._result(key, "dry-run boundary: no device call made",
                                  ok=True, skipped=True, outcome="dry_run")
            result.requested = requested
            return self._record(result, fn_name, actor, reason, override_id)

        try:
            result = getattr(backend, fn_name)(device, *args)
        except Exception as exc:
            result = CommandResult(False, key, f"{type(exc).__name__}: {exc}")
        result.requested = requested
        result.actor = actor
        result.reason = reason
        if not result.skipped:
            # Protocol acceptance is not proof that the appliance changed.
            # Capture one independent readback without rewriting the command's
            # acknowledged outcome. An unavailable read remains explicit None.
            try:
                reported = backend.read_state(device)
            except Exception as exc:
                reported = ReportedState(
                    key, False, source=backend.name,
                    detail=f"post-command read failed: {type(exc).__name__}")
            result.reported = reported.state if reported.available else None
            self.journal.record_reported(
                device=key, source=reported.source,
                available=reported.available, state=reported.state,
                detail=reported.detail, at=reported.at)
            self.journal.update_guard(
                key, last_off_at=(now if (result.ok and fn_name == "set_power"
                                          and not bool(args[0])) else None),
                last_command_at=now, last_requested=requested,
                last_acknowledged=result.acknowledged)
        return self._record(result, fn_name, actor, reason, override_id)

    def set_power(self, key: str, on: bool, *, actor: str = "automation",
                  reason: str = "") -> CommandResult:
        return self._dispatch(key, "set_power", bool(on), actor=actor,
                              reason=reason, requested={"power": bool(on)})

    def set_speed(self, key: str, speed: int, *, actor: str = "automation",
                  reason: str = "") -> CommandResult:
        return self._dispatch(key, "set_speed", int(speed), actor=actor,
                              reason=reason, requested={"speed": int(speed)})

    def set_position(self, key: str, percent: int, *, actor: str = "automation",
                     reason: str = "") -> CommandResult:
        return self._dispatch(key, "set_position", int(percent), actor=actor,
                              reason=reason, requested={"position": int(percent)})

    def manual_power(self, key: str, on: bool, *, operator: str, reason: str,
                     ttl_seconds: int = 1800) -> CommandResult:
        device = self._devices.get(key)
        if device is None or not device.address:
            return self._dispatch(key, "set_power", bool(on), actor=operator,
                                  reason=reason, requested={"power": bool(on)})
        oid = self.journal.create_override(key, "power", bool(on), operator,
                                           reason, ttl_seconds)
        result = self._dispatch(key, "set_power", bool(on), actor=operator,
                                reason=reason, override_id=oid,
                                requested={"power": bool(on)})
        if result.acknowledged is not True or result.skipped:
            self.journal.clear_override(
                key, operator, f"manual command {result.outcome}; authority not activated")
        return result

    def manual_speed(self, key: str, speed: int, *, operator: str, reason: str,
                     ttl_seconds: int = 1800) -> CommandResult:
        device = self._devices.get(key)
        if device is None or not device.address:
            return self._dispatch(key, "set_speed", int(speed), actor=operator,
                                  reason=reason, requested={"speed": int(speed)})
        if device.kind is not DeviceKind.FAN:
            return self._record(self._result(
                key, "speed is supported only for fan devices",
                outcome="rejected", requested={"speed": int(speed)}),
                "set_speed", operator, reason)
        oid = self.journal.create_override(key, "speed", int(speed), operator,
                                           reason, ttl_seconds)
        result = self._dispatch(key, "set_speed", int(speed), actor=operator,
                                reason=reason, override_id=oid,
                                requested={"speed": int(speed)})
        if result.acknowledged is not True or result.skipped:
            self.journal.clear_override(
                key, operator, f"manual command {result.outcome}; authority not activated")
        return result

    def bench_motor(self, key: str, command: str, speed=None, ms=None, *,
                    operator: str, reason: str) -> CommandResult:
        """Bring-up path to the motor's raw verbs — direction, speed, timed run.

        This used to create no override at all, on the theory that a bench
        pulse is somebody proving a wire, not a standing instruction. That
        was wrong in practice: the comfort ladder re-asserts its own idea of
        the blinds position on every tick, `actor="automation"`, no override
        needed on ITS side either — so a bare bench command and the ladder's
        next tick land on the same physical motor within a second of each
        other, and the ladder wins the race almost every time. A person at
        the bench watching nothing move was not a wiring fault; it was this.
        So a bench drive takes the override after all, but scoped to the
        drive itself rather than left standing: the TTL is the requested run
        time plus a small buffer, not the manual-control default of 30
        minutes. STOP and BRAKE clear it outright, so pressing Stop hands the
        device straight back to automatic control rather than waiting out a
        timer.
        """
        device = self._devices.get(key)
        if device is None:
            return self._record(self._result(
                key, "unknown device", outcome="rejected"),
                "bench_motor", operator, reason)

        if command in ("STOP", "BRAKE"):
            self.journal.clear_override(key, operator, "bench motor halted")
            return self._dispatch(key, "bench_motor", command, speed, ms,
                                  actor=operator, reason=reason,
                                  requested={"command": command})

        # +5s buffer over the run itself: enough for the request's own
        # round trip and the firmware's own coast-out, not enough to linger.
        ttl = max(5, int((ms or 1000) / 1000) + 5)
        oid = self.journal.create_override(key, "bench_motor", command,
                                           operator, reason, ttl)
        result = self._dispatch(key, "bench_motor", command, speed, ms,
                                actor=operator, reason=reason, override_id=oid,
                                requested={"command": command, "speed": speed,
                                           "ms": ms})
        if result.acknowledged is not True or result.skipped:
            self.journal.clear_override(
                key, operator, f"bench command {result.outcome}; authority not activated")
        return result

    def return_to_auto(self, key: str, *, operator: str,
                       reason: str = "return to automatic") -> int:
        return self.journal.clear_override(key, operator, reason)

    def active_overrides(self) -> list[dict]:
        return self.journal.active_overrides()

    def needs_reconcile(self, key: str, requested: object) -> bool:
        """True when automatic intent has not been acknowledged.

        The control plan may stay unchanged while a manual override expires.
        Comparing only successive plans would then leave the appliance at its
        old manual value forever. Durable requested state closes that hole and
        also triggers a retry after a failed transport acknowledgement.
        """
        if self.journal.active_override(key):
            return False
        guard = self.journal.guard(key)
        return (guard["last_requested"] != requested
                or guard["last_acknowledged"] is not True)

    def state_of(self, key: str) -> Optional[bool]:
        state = self.read_state(key)
        if not state.available:
            return None
        if isinstance(state.state, dict) and "power" in state.state:
            return bool(state.state["power"])
        return bool(state.state)

    def read_state(self, key: str) -> ReportedState:
        device = self._devices.get(key)
        backend = self._backends.get(device.backend) if device else None
        if not device:
            return ReportedState(key, False, source="registry", detail="no such device")
        if not backend:
            return ReportedState(key, False, source="registry",
                                 detail=f"backend {device.backend!r} not configured")
        try:
            reported = backend.read_state(device)
        except Exception as exc:
            reported = ReportedState(key, False, source=backend.name,
                                     detail=f"{type(exc).__name__}: {exc}")
        self.journal.record_reported(device=key, source=reported.source,
                                     available=reported.available,
                                     state=reported.state, detail=reported.detail,
                                     at=reported.at)
        return reported

    def _record(self, result: CommandResult, action: str = "unknown",
                actor: str = "automation", reason: str = "",
                override_id: Optional[str] = None) -> CommandResult:
        result.actor = actor
        result.reason = reason
        self.log.insert(0, result)
        del self.log[40:]
        self.journal.record_command(
            command_id=result.command_id, at=result.at, device=result.device,
            action=action, requested=result.requested, actor=actor, reason=reason,
            override_id=override_id, acknowledged=result.acknowledged,
            reported=result.reported, outcome=result.outcome, detail=result.detail)
        return result

    def health(self) -> dict:
        return {
            "backends": {n: b.health() for n, b in self._backends.items()},
            "devices": [{"key": d.key, "kind": d.kind.value, "backend": d.backend,
                         "address": d.address, "label": d.label} for d in self.devices()],
            "live_enabled": self.live_enabled,
            "journal": self.journal.health(),
            "manual_overrides": self.active_overrides(),
        }
