"""Command the board's own actuators, over the link the telemetry rides.

Every other backend in this package talks to something on the network. This one
talks *down* — to the STM32 on the same board, over the Router serial the
sensor reader is already draining. The blinds motor has no cloud, no API and
no LAN presence; the only way to it is that line.

One backend serves it, and every target the sketch grows later: the target name
comes from ``device.address`` (``SRV1``, …) and the acknowledged position is
filed under ``device.key``, so two covers on one link would keep separate
readbacks and never share a position.

Two rules follow from the transport being shared:

* The reader owns the connection, so this backend never dials the board. It
  asks ``reader.router.command_link()`` for the write handle. No link means a
  clean failure, not a second competing peer on the UART.
* A DC motor cannot report its own position — there is no feedback wire.
  Readback is therefore the last position the MCU *acknowledged*, labelled as
  exactly that, and never presented as an independent observation.
"""
from __future__ import annotations

import time
from typing import Optional

from .base import (CommandResult, Device, DeviceBackend, DeviceKind,
                   ReportedState, register_backend)

ACK_TIMEOUT_S = 2.0
OPEN, CLOSE = "OPEN", "CLOSE"
NO_ACK_DETAIL = "MCU did not acknowledge — motor path not live"

DISPLAY_TARGET = "DISP"
DISPLAY_TIMEOUT_S = 0.5
OCCUPANCY_MODES = ("EMPTY", "OCCUPIED", "CROWDED", "ASLEEP")
PEOPLE_MAX = 9


class TransportUnavailable(RuntimeError):
    """No write handle to the board exists in this process."""


class _NoTransport:
    """Stand-in when the sensor reader is not running beside us."""

    def request(self, line: str, timeout: float) -> Optional[str]:
        raise TransportUnavailable(
            "no reader is connected to the board, so no command channel exists")

    def health(self) -> dict:
        return {"connected": False, "transport": "unavailable"}


def _shared_link():
    """The sensor reader's write handle, or a stand-in that fails cleanly."""
    try:
        from reader.router import command_link
    except ImportError:
        return _NoTransport()
    return command_link()


def display(mode: int, people: int, *, transport=None) -> bool:
    """Push the occupancy glyph and people count to the board's LED matrix.

    Fire-and-forget and deliberately outside the device registry: the matrix is
    a status light, not an actuator, so a failed push must never appear in the
    actuation journal or hold up a comfort tick. Returns whether the MCU
    acknowledged; raises nothing, ever.

    ``mode`` indexes OCCUPANCY_MODES (0 EMPTY, 1 OCCUPIED, 2 CROWDED,
    3 ASLEEP); ``people`` is clamped to 0..PEOPLE_MAX because the bar has that
    many pixels and a larger number would silently draw the same row.

    It travels as ``CMD DISP <mode> <people>`` on the shared command link
    rather than the sketch's dedicated ``breezeiq/display`` RPC, because the
    link routes every call through ``breezeiq/cmd`` and the serial path parses
    the same grammar. One line reaches the board over both transports; the
    firmware runs one parser behind both doors.
    """
    try:
        mode, people = int(mode), int(people)
    except (TypeError, ValueError):
        return False
    if not 0 <= mode < len(OCCUPANCY_MODES):
        return False
    people = max(0, min(PEOPLE_MAX, people))
    link = transport if transport is not None else _shared_link()
    try:
        reply = link.request(
            f"CMD {DISPLAY_TARGET} {mode} {people}", DISPLAY_TIMEOUT_S)
    except Exception:
        # A board that is not there is not a reason to stop deciding.
        return False
    return bool(reply and reply.startswith(f"ACK {DISPLAY_TARGET} "))


@register_backend("mcu")
class McuBackend(DeviceBackend):
    """Named-position cover control on the board's own motor."""

    def __init__(self, transport=None, timeout: float = ACK_TIMEOUT_S, **kwargs):
        super().__init__(**kwargs)
        self._transport = transport
        self.timeout = float(timeout)
        self._acknowledged: dict[str, str] = {}
        self._acknowledged_at: dict[str, float] = {}

    @property
    def transport(self):
        # Resolved late: the reader may start after the registry is built.
        if self._transport is None:
            self._transport = _shared_link()
        return self._transport

    def set_power(self, device: Device, on: bool) -> CommandResult:
        """True opens the cover, False closes it — the dashboard's contract."""
        return self._move(device, OPEN if on else CLOSE)

    def set_position(self, device: Device, percent: int) -> CommandResult:
        """The cover has two trustworthy end states, so percent picks one.

        Intermediate positions are deliberately not offered: with no feedback
        wire a part-travel stop cannot be verified, and nothing upstairs asks
        for a partial blind.
        """
        return self._move(device, OPEN if int(percent) >= 50 else CLOSE)

    def bench_motor(self, device: Device, command: str,
                    speed: int | None = None,
                    ms: int | None = None) -> CommandResult:
        """Raw motor verbs for hardware bring-up, not for the control ladder.

        The ladder only ever wants OPEN or CLOSE at full travel, which
        ``set_power`` already gives it. This exists so the bench can exercise
        the axes the ladder never touches — part-speed, a timed run, coast
        versus brake — because a driver that has never been asked for those
        has never really been tested. Duration and speed are bounded on the
        MCU, not here, so a bad value is refused by the part that owns the
        motor rather than by whatever happened to call it.
        """
        argv = [command]
        if speed is not None:
            argv.append(str(int(speed)))
            if ms is not None:
                argv.append(str(int(ms)))
        return self._move(device, " ".join(argv))

    def _move(self, device: Device, position: str) -> CommandResult:
        if device.kind is not DeviceKind.COVER:
            return CommandResult(False, device.key,
                                 "mcu backend drives cover devices only",
                                 acknowledged=False)
        line = f"CMD {device.address} {position}"
        try:
            reply = self.transport.request(line, self.timeout)
        except Exception as exc:
            # A dead link is a device failure, never a control-loop crash.
            return CommandResult(False, device.key,
                                 f"MCU link unavailable: {exc}",
                                 acknowledged=False)
        if reply is None:
            return CommandResult(False, device.key, NO_ACK_DETAIL,
                                 acknowledged=False)
        return self._interpret(device, position, reply.strip())

    def _interpret(self, device: Device, position: str,
                   reply: str) -> CommandResult:
        parts = reply.split()
        verb = parts[0] if parts else ""
        # Two covers share this link, so a reply is only evidence about the
        # target it names. The command link filters by target as well, but that
        # is transport behaviour; the layer that turns a reply into a truth
        # claim has to check for itself, or the window's ACK settles a blinds
        # command and the readback quietly describes the wrong cover.
        if len(parts) > 1 and parts[1] != device.address:
            return CommandResult(
                False, device.key,
                f"MCU replied about {parts[1]}, not {device.address}",
                acknowledged=False)
        if verb == "ERR":
            why = " ".join(parts[2:]) or "no reason given"
            return CommandResult(False, device.key, f"MCU refused: {why}",
                                 acknowledged=False)
        if verb == "ACK" and len(parts) > 2:
            reached = parts[2]
            self._acknowledged[device.key] = reached
            self._acknowledged_at[device.key] = time.time()
            # The firmware echoes the verb alone; a command may now carry
            # speed and duration after it. Compare the verb, or every
            # parameterised move reads as a mismatch and fails a move that
            # actually happened.
            wanted = position.split()[0] if position else position
            if reached == wanted:
                return CommandResult(True, device.key,
                                     f"MCU acknowledged {position}",
                                     acknowledged=True)
            # The MCU moved somewhere else; record the truth, fail the intent.
            return CommandResult(False, device.key,
                                 f"MCU acknowledged {reached}, not {wanted}",
                                 acknowledged=False)
        return CommandResult(False, device.key,
                             f"unreadable MCU reply: {reply!r}",
                             acknowledged=False)

    def get_state(self, device: Device) -> Optional[bool]:
        position = self._acknowledged.get(device.key)
        return None if position is None else position == OPEN

    def read_state(self, device: Device) -> ReportedState:
        """What the MCU last said it was doing, never dressed up as a sighting.

        The card upstairs needs something to show, and the only fact this
        backend owns is the firmware's own ACK. It is reported as exactly that
        — commanded, acknowledged, unverified — because a DC motor has no feedback
        wire and inventing a position would be the one lie this project cannot
        afford. Before the first ACK there is nothing to report and the state
        stays unavailable.
        """
        position = self._acknowledged.get(device.key)
        if position is None:
            return ReportedState(device.key, False, source=self.name,
                                 detail="no position acknowledged since start")
        age = int(time.time() - self._acknowledged_at.get(device.key, 0.0))
        return ReportedState(
            device.key, True, {"power": position == OPEN, "position": position},
            self.name,
            f"commanded {position}, acknowledged by MCU {age}s ago "
            "(no position feedback — a DC motor has no feedback wire)",
            at=self._acknowledged_at.get(device.key, time.time()))

    def health(self) -> dict:
        try:
            link = self.transport.health()
        except Exception as exc:
            link = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"backend": self.name, "ok": bool(link.get("connected")),
                "link": link, "acknowledged": dict(self._acknowledged)}
