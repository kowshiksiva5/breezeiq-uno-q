# ruff: noqa: E402
"""BreezeIQ control loop — sensors in, real devices out.

Deliberately the simplest thing that closes the loop. Tuning knobs are all at
the top, in one block, so they can be changed without reading the code.

    python3 control.py --dry-run          # decide and print, touch nothing
    python3 control.py --live             # actually command the devices
    python3 control.py --live --once      # a single tick, for testing

The ladder is NOT modified here. This file only translates: SensorFrame in,
brain.Reading out, brain.Plan in, device commands out. Policy stays isolated
from transports and device protocols.
"""
from __future__ import annotations

import os
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in ("brain", "sense", "act"):
    _d = os.path.join(_ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)


import argparse
import json
import threading
import time
from dataclasses import asdict, replace
from typing import Optional

from collections import deque

from comfort import AWAY, comfort
from digital_twin import AcDigitalTwin
from energy import EnergyCollector, EnergyMonitor
import forecast as fc
from fusion import (DEFAULT_LIGHT_PREFERENCE, LIGHT_PREFERENCES, decide_light,
                    fuse_light, lux_index, wants_auto_on)
import horizon as hz
from devices.config import build_registry
from devices.control_socket import ControlSocketServer
from devices.mcu import OCCUPANCY_MODES, display
from brain import (MODE_ASLEEP, VETOABLE, Memory, Plan, Reading,
                   _climb_one_rung, _comfort_offset, decide, gates,
                   mode as room_mode, policy_for)
from reader import build_source
from telemetry import Telemetry
# cv2 is imported lazily inside the counter, so this stays safe on a board
# with no OpenCV installed.
from vision import policy
from vision.counter import build_counter

# ── TUNING ──────────────────────────────────────────────────────────────
TICK_SECONDS = 30.0          # how often we re-decide. Sensors are ~1 Hz; the
                             # room's thermal constant is hours. 30 s is already
                             # far faster than the room can change.
# The NOMINAL tick, used only as the first tick's assumed elapsed time and as the
# sleep interval. Every duration the controller reasons about is measured from the
# clock instead — see `Controller._elapsed_min`. Nothing downstream may assume
# ticks are this far apart.
DT_MIN = TICK_SECONDS / 60.0

# Ceiling on the elapsed time a single tick may charge to the hysteresis timers.
# The wall clock can jump — NTP stepping on a board whose RTC drifts, a laptop
# resuming from suspend, a container paused under load — and an unclamped delta
# would credit dwell and the hot/cold streaks with room history nobody observed,
# firing a rung on evidence that does not exist. Ten minutes is far longer than
# any legitimate gap between ticks and far shorter than the jumps worth fearing.
MAX_ELAPSED_MIN = 10.0

# ── cadence ─────────────────────────────────────────────────────────────
# The loop no longer sleeps a fixed interval. It waits on an event that a watcher
# thread sets the moment something worth re-deciding on happens, and otherwise
# wakes on this floor. The room's thermal constant is hours, so the floor exists
# to keep the timers ticking and the dashboard fresh, not to chase the physics.
#
# What the floor is NOT: a safety interval. The compressor's 180 s minimum-off
# lives in the act layer (`act/devices/config.py`), below the brain and enforced
# per device against SQLite, so no decision rate here can short-cycle it.
TICK_FLOOR_S = 10.0

# What is worth waking for. PIR is the one that matters: somebody walking into a
# room is the only genuinely fast event in a building, and it is the case where
# latency is felt. A temperature step this large is either a door opening or a
# sensor problem, and both deserve a fresh decision rather than a wait.
WAKE_STEP_C = 0.5

# The hours in which a person in this room is presumed to be asleep rather
# than out. Used to decide that motion means "still here" rather than "up and
# about", which is what keeps the lamp from switching itself on at 3am.
NIGHT_FROM_HOUR = 23
NIGHT_UNTIL_HOUR = 7


def _is_night(at: Optional[float] = None) -> bool:
    """Local-clock night. Injectable so the tests do not need a 3am CI run."""
    hour = time.localtime(at).tm_hour if at else time.localtime().tm_hour
    return hour >= NIGHT_FROM_HOUR or hour < NIGHT_UNTIL_HOUR


# Occupancy: the PIR reports motion NOW, but a person reading in a chair is
# still present. Hold "occupied" for this long after the last motion, or the
# AC will switch off on someone sitting still.
OCCUPANCY_HOLD_MIN = 10.0

# How long a present person must be completely motionless before the room
# calls them asleep. The PIR gives up on a still person within its own pulse,
# so this is really "how long the radar has held presence while the PIR saw
# nothing" — the gap between the two parts is the whole signal.
#
# Twenty minutes is chosen against the false positive that matters: reading,
# desk work and watching something all produce small movements well inside it,
# while actual sleep does not. Being wrong is survivable in both directions —
# a missed sleeper keeps a slightly cooler room, a false sleeper gets a fan
# capped at medium and a lamp left alone — which is why this is a threshold
# and not a vote.
SLEEP_STILL_S = 20.0 * 60.0

# How long the camera must keep seeing somebody before a quiet-PIR AWAY is
# promoted to AWAKE. A single frame is not evidence: on the board the counter
# flapped in runs of `p=1 x1` — one lone tick inside 43 otherwise empty ones —
# and each flap toggled the ceiling light, 18 tubelight commands in 24 h of an
# empty room. 60 s of agreement is nothing against a room whose thermal constant
# is hours, and the AWAY path cannot switch anything off in the meantime because
# `confirm_absence` needs a count of zero, which a flap is not.
#
# SECONDS, not ticks. This used to be `PEOPLE_PROMOTE_TICKS = 2`, which meant
# 60 s only while the loop ran at a fixed 30 s; on an event-driven loop the same
# two ticks can be 10 s apart, shrinking a debounce that exists precisely to
# outlast a flap. Wall-clock is the only form that keeps its meaning.
PEOPLE_PROMOTE_S = 60.0

# How long the fused light evidence must keep asking for the lamp before it
# gets it. The LDR on this board changes its fused verdict on 29 % of
# consecutive ticks, so a single sample is not evidence of anything — two
# minutes of agreement is. Long enough to outlast the flapping, short enough
# that walking into a dark room does not feel broken, and it only ever delays
# switching a lamp ON.
LIGHT_CONFIRM_S = 120.0

# The absence gate's light-independent path: how many occupancy holds of PIR
# silence, alongside a healthy camera counting zero, confirm an empty room.
ABSENCE_QUIET_HOLDS = 3

# The degraded path, for a board whose camera is not merely quiet but GONE.
#
# `confirm_absence` requires a healthy camera before it will believe a room is
# empty, which is right when there is a camera to be healthy: two sensors with
# different blind spots, and a count of zero never evicts an occupant on its own.
# But when the camera is offline the gate can never pass, the presence hold
# latches, and an empty room keeps its fan and compressor running forever — the
# exact behaviour the occupancy work exists to prevent. Observed live: the USB-C
# port latched to device role, and the room could no longer switch itself off.
#
# So when there is NO camera at all, PIR silence alone may confirm absence, at a
# much longer threshold than the two-sensor path would need. This is a genuine
# weakening — a person who sits perfectly still for this long is switched off on —
# and it is bounded three ways: it applies only while the camera is absent, it
# needs six occupancy holds rather than three, and every use is recorded as
# `degraded` so the log says which rule emptied the room.
ABSENCE_PIR_ONLY_HOLDS = 6

# A sensor that reads nothing for this long is broken, not quiet, and the board
# should say so once rather than every tick. Five minutes is long past any single
# dropped I2C read or camera reconnect. Seconds rather than the old ten-tick
# count, for the same reason as PEOPLE_PROMOTE_S: a tick count is not a duration
# once the cadence can change.
SENSOR_FAIL_S = 300.0

# Camera people counter. App Lab's local detection Brick is the board default.
# OpenCV and FOMO remain explicit development and evaluation backends.
# A missing camera, a missing model or a revoked permission must degrade to
# "no count", never take the control loop down with it.
COUNTER = os.environ.get("BREEZEIQ_COUNTER", "app-lab")
CAMERA = os.environ.get("BREEZEIQ_CAMERA", "0")
VISION_STALE_S = float(os.environ.get("BREEZEIQ_VISION_STALE_S", "900"))
# A bus-powered USB camera browns out and re-enumerates under a new device
# number. Both knobs exist because this is a physical fault with a physical
# recovery time, not a value a model can infer.
CAMERA_REOPEN_MIN = float(os.environ.get("BREEZEIQ_CAMERA_REOPEN_MIN", "2.0"))
CAMERA_DEAD_S = float(os.environ.get("BREEZEIQ_CAMERA_DEAD_S", "180"))

# Light: only act when the room is genuinely dark AND someone is there.
LIGHT_ON_BELOW_SOLAR = 0.15

# The occupant's stored lighting preference. The dashboard writes it, this loop
# reads it, and `fusion` owns what each value means.
LIGHT_PREFERENCE_KEY = "light_preference"

# The occupant's standing veto, one stored preference per device: "allow_ac",
# "allow_window" and so on. Absent or anything other than a recognised "no"
# means allowed, so a board with no stored preferences behaves exactly as it
# always has, and a corrupt value fails OPEN rather than silently disabling
# the machinery somebody depends on.
VETO_PREFIX = "allow_"
VETO_DENIES = ("no", "false", "0", "off", "never", "deny")

# Fused light states in which a camera can be believed when it reports an empty
# room. `fusion.fuse_light` also emits "dark" and "unavailable"; neither is
# evidence that the lens could have seen anybody.
LIGHT_ENOUGH_TO_SEE = ("normal", "bright")

# Fan: the ladder plans in policy levels 0-3 (off, gentle, working, full),
# while the ceiling fan itself runs a 0-6 scale. Translating here — at the one
# place policy becomes a device command — keeps the ladder free of vendor
# detail and lets the journal, the reconcile check and the dashboard all speak
# the same number the fan reports back.
# Level 3 is "full", and full must be a speed the fan HAS. It mapped to 6 on a
# five-speed fan, so the top rung of the ladder addressed nothing: the journal
# has thousands of 0/2/4 commands and not a single 6.
FAN_LEVEL_TO_VENDOR_SPEED = {0: 0, 1: 2, 2: 4, 3: 5}

# The LED matrix glyph for each ladder mode. The values are indexes into the
# firmware's own glyph table, so the ORDER is the sketch's and not ours —
# renumbering here would light the wrong face on the board. Building the map
# from `OCCUPANCY_MODES` rather than retyping it means the two cannot drift.
DISPLAY_MODES = {name: index for index, name in enumerate(OCCUPANCY_MODES)}
# ─────────────────────────────────────────────────────────────────────────


def _veto_blocks(key: str, kind: str, value, vetoed) -> bool:
    """Would this command engage a device the occupant has forbidden?

    `blinds` is the awkward one: its command contract is open=True, so SHADE
    is `power=False`. Vetoing the blinds must block the SHUT command, not the
    open one — hence the inversion here rather than a blanket truthiness test.
    """
    device = "blinds" if key == "blinds" else (
        "light" if key == "tubelight" else key)
    if device not in vetoed:
        return False
    engaging = (not bool(value)) if key == "blinds" else bool(value)
    return engaging


def _json_detail(payload: dict) -> str:
    """Compact JSON for an event row. Never raises: a detail that will not
    serialise must not stop the transition being recorded at all."""
    import json
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          default=str)
    except Exception:
        return json.dumps({"unserializable": str(payload)})


class OccupancyTracker:
    """PIR motion -> the AWAY/AWAKE/ASLEEP the ladder expects.

    Kept out of the ladder on purpose: this is sensor interpretation, not a
    control decision, and mixing the two is how a pure function stops being one.
    """

    def __init__(self, hold_min: float = OCCUPANCY_HOLD_MIN):
        self.hold_s = hold_min * 60.0
        self._last_motion: Optional[float] = None
        # Kept apart from _last_motion on purpose — see update(). idle_s must
        # stay a PIR figure because confirm_absence is denominated in it.
        self._last_presence: Optional[float] = None
        # Whether a radar has EVER reported. Sleep is read from the room when
        # one is fitted and falls back to the clock when one is not, so the
        # tracker has to know which world it is in. Latching on first sight
        # rather than per-tick keeps a momentarily clear radar from silently
        # reverting the whole policy to the night window mid-evening.
        self._radar_seen = False

    def restore(self, last_motion_at: Optional[float]) -> bool:
        """Restore a durable PIR hold after a normal process restart."""
        try:
            value = float(last_motion_at)
        except (TypeError, ValueError):
            return False
        # Ignore corrupt/future evidence. Old valid evidence is safe to retain;
        # update() applies the normal hold and day/night policy to its age.
        if value <= 0 or value > time.time() + 60:
            return False
        self._last_motion = value
        return True

    @property
    def idle_s(self) -> Optional[float]:
        """Seconds since the PIR last fired, or None when it never has.

        Published because absence is decided elsewhere and needs the raw age,
        not the AWAY/AWAKE/ASLEEP word this tracker collapses it into.
        """
        if self._last_motion is None:
            return None
        return max(0.0, time.time() - self._last_motion)

    def update(self, motion: Optional[bool],
               radar: Optional[bool] = None,
               at: Optional[float] = None) -> str:
        """Radar is a presence source here and deliberately nowhere else.

        The PIR's own timestamp stays PIR-only, because `idle_s` feeds
        `confirm_absence`, and the whole reason that gate exists is a fan found
        running 6/6 in an empty room. A radar that mistakes fan blades for a
        person would refresh the timer forever and bring that back — Hi-Link's
        manual says outright that the part sees a fan, which is why the PIR was
        kept beside it rather than replaced.

        So the asymmetry is the point, and it runs the safe way: radar can say
        somebody is STILL HERE, keeping a person who is sitting perfectly still
        comfortable, but it cannot keep the room from being released as empty.
        Granting comfort on a false positive costs a few watts; withholding the
        release costs a fan running all day in an empty room.
        """
        # `at` drives both the elapsed maths and the night window, so a test
        # can place this tracker at any hour without waiting for one. The
        # module already committed to that with _is_night(at) — "injectable so
        # the tests do not need a 3am CI run" — but update() never passed one,
        # which left the whole suite quietly passing by day and failing by
        # night. It was failing on a clean checkout when this was written.
        now = time.time() if at is None else at
        if radar is not None:
            self._radar_seen = True
        if motion:
            self._last_motion = now
        if radar:
            self._last_presence = now
        # The later of the two decides how occupied the room looks, while only
        # the PIR's own figure is published as idle_s.
        latest = max([t for t in (self._last_motion, self._last_presence)
                      if t is not None], default=None)
        if latest is None:
            return "AWAY"
        idle = now - latest

        # ── ASLEEP is now observed, not assumed from the hour ──────────────
        # It used to be decided by the clock alone, checked before everything
        # else: at 23:01 anyone still in the room was "asleep", got the fan
        # capped and the band relaxed, whether they were in bed or at a desk.
        # That ordering was itself a fix for the board switching the lamp on
        # three times in one night, so the guard it provides has to survive —
        # but the room can be read directly now, and reading beats guessing.
        #
        # Somebody present and completely motionless is asleep. The radar is
        # what makes that observable: it holds presence for a still person
        # exactly where the PIR gives up, so "radar still sees them AND the
        # PIR has been quiet a long time" is the signature, and the camera has
        # already vetoed the radar upstream if it can see the room is empty.
        #
        # The clock is kept as the last resort and nothing more. With no radar
        # fitted, or none reporting, there is no way to tell a sleeper from an
        # empty chair, and the old night rule is still the safest answer — a
        # lamp held is cheaper than a lamp switched on over someone sleeping.
        # Stillness is measured on the PIR's own clock, never the combined
        # one: the radar refreshes presence every tick it sees somebody, so
        # `idle` never grows for exactly the person this is trying to detect.
        # The signal is the GAP between the two parts — radar still holding
        # while the PIR has seen nothing for a long time — which is also why
        # current radar presence is required. Without that second condition an
        # empty room at 3am reads as asleep purely because nothing has moved,
        # and a room conditioned for nobody is the failure this whole area
        # exists to prevent.
        # A PIR that has NEVER fired proves nothing about stillness — it is
        # equally the signature of an unplugged one — so sleep requires having
        # watched motion stop, not merely an absence of it. Without this a
        # radar-only room reads as asleep the instant somebody walks in.
        pir_idle = (None if self._last_motion is None
                    else now - self._last_motion)
        radar_present = (self._last_presence is not None
                         and now - self._last_presence < self.hold_s)
        if (self._radar_seen and radar_present
                and pir_idle is not None and pir_idle >= SLEEP_STILL_S):
            return "ASLEEP"
        if not self._radar_seen and _is_night(at):
            return "ASLEEP"
        if idle < self.hold_s:
            return "AWAKE"
        # Daytime quiet is somebody gone. A night with a working radar reaches
        # here too, and correctly: the radar is not seeing anybody, so the room
        # is empty rather than asleep.
        return "AWAY"


def _load_ar_weights():
    """Fitted weights if `forecast fit --write` has run, else EWMA defaults."""
    import json
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ar_weights.json")
    try:
        d = json.load(open(f))
        return d["weights"], d.get("intercept", 0.0)
    except Exception:
        return None, 0.0                      # None => forecast.py uses EWMA


def frame_to_reading(frame, occupant: str, trend: Optional[float] = None,
                    people: Optional[int] = None,
                    people_confidence: Optional[float] = None,
                    effective_indoor_c: Optional[float] = None,
                    effective_indoor_rh: Optional[float] = None,
                    comfort_c: Optional[float] = None) -> Optional[Reading]:
    """SensorFrame -> ladder.Reading. Returns None when we must not decide."""
    if not frame.valid or frame.indoor_c is None:
        return None
    indoor = (effective_indoor_c if effective_indoor_c is not None
              else frame.indoor_c)
    return Reading(
        indoor=indoor,
        # The AC-adjusted humidity when the response model has one, because a
        # running coil dries the room and RH is a first-class PMV term. Using
        # the raw sensor here would price the compressor's comfort at only its
        # sensible share and make it look worse than it is.
        indoor_rh=(effective_indoor_rh if effective_indoor_rh is not None
                   else frame.indoor_rh if frame.indoor_rh is not None
                   else 50.0),
        # No outdoor sensor yet? Assume outdoor equals indoor, which makes the
        # VENT gate fail closed. Guessing "cooler outside" would open a window
        # on a hunch.
        outdoor=frame.outdoor_c if frame.outdoor_c is not None else indoor,
        outdoor_rh=frame.outdoor_rh if frame.outdoor_rh is not None else 50.0,
        solar=frame.solar_index if frame.solar_index is not None else 0.0,
        occupant=occupant,
        trend_c_per_h=trend,
        people=people,
        people_confidence=people_confidence,
        comfort_c=comfort_c,
    )


def requested_setpoint_c(twin) -> Optional[float]:
    """The occupant's setpoint dial, or None when nobody has expressed one.

    Read off the AC response model's own state, which is where the persisted,
    TTL-bounded, audited `ac_twin_control.setpoint_c` lands each tick — so the
    number the ladder leans on is exactly the number the dashboard shows, and
    there is no second setpoint store to keep in sync. A model that is disabled
    or failed reports no setpoint, and the ladder keeps its default band.
    """
    try:
        return float(twin["setpoint_c"])
    except (TypeError, KeyError, ValueError):
        return None


def confirm_absence(people: Optional[int], vision_status: dict,
                    light_evidence, pir_idle_s: Optional[float] = None) -> bool:
    """Is the camera actually telling us the room is empty?

    A zero-count vote carries confidence 0.0 BY CONSTRUCTION — the vote's
    strength is the median score of the frames that saw somebody, and an empty
    room has none of those. So a per-box confidence floor here can never be
    satisfied: it held the room "occupied" forever and left the devices running
    in an empty room, which is the exact opposite of the headline behaviour.

    What a zero count needs is not a score, it is proof the camera COULD have
    seen a person: a healthy camera, and light to see by. The light test is
    written as the states a lens can work in rather than as "not dark", so that
    absent, unknown or future evidence fails closed — with neither the LDR nor
    camera luminance reporting we cannot say the room was lit, and retaining the
    current physical state is the cheap failure.

    `occupant == AWAY` already implies the PIR has been quiet for 10 min, and
    the tracker emits ASLEEP rather than AWAY at night, so this gate is never
    what stands between a sleeping person and their fan.

    The light test alone is not enough on this board. The LDR reads raw 0 and
    fuses to "unavailable" on 54% of ticks, so on more than half of them the
    lit-room path can never be satisfied and the presence hold latches: a fan
    was found reporting 6/6 in an empty room that automation refused to switch
    off. The second path is deliberately independent of the light chain — a
    healthy camera counting zero WHILE the PIR has been silent for
    ABSENCE_QUIET_HOLDS times the occupancy hold. That is 30 min of two
    different sensors agreeing on an empty room, far past any sitting-still,
    reading, or napping scenario the 10 min hold was written for (the live
    board saw 14 h of PIR quiet), and the camera has to have agreed on zero the
    whole time — one seen person resets it instantly.
    """
    health = vision_status.get("health")
    if health != "ok":
        # NO CAMERA AT ALL is a different situation from a camera that sees
        # nobody, and the two-sensor rule cannot distinguish them: both fail this
        # check, so a board whose camera is unplugged can never empty its room.
        # Seen live — the USB-C port latched to device role and the presence hold
        # latched with it. Where there is no second sensor to agree with, a much
        # longer PIR silence stands in for one.
        if health == "offline" and _quiet_for(pir_idle_s, ABSENCE_PIR_ONLY_HOLDS):
            return True
        return False
    if people != 0:
        return False
    if getattr(light_evidence, "state", None) in LIGHT_ENOUGH_TO_SEE:
        return True
    return _quiet_for(pir_idle_s, ABSENCE_QUIET_HOLDS)


def trust_radar(radar: Optional[bool], camera_saw_empty: bool) -> Optional[bool]:
    """Should the radar be allowed to say somebody is here?

    Holding a room AWAKE is stronger than it looks. `absence_confirmed` only
    releases an AWAY plan for application, so a room that never says AWAY never
    proposes switching anything off and the absence gate is never consulted. A
    radar fooled by fan blades — which Hi-Link's manual says happens — would
    therefore keep the fan running forever, which is the exact incident that
    gate was written for.

    A healthy camera counting zero is the one thing that outranks it, mirroring
    `confirm_absence`'s own two-sensor rule: neither part evicts an occupant
    alone, and neither holds a room alone either. Everything else leaves the
    radar trusted, because an unread camera must never evict a person who is
    simply sitting still.
    """
    if radar and camera_saw_empty:
        return False
    return radar


def _quiet_for(pir_idle_s: Optional[float], holds: float) -> bool:
    """Has the PIR been silent for `holds` occupancy holds? None is never quiet."""
    return (pir_idle_s is not None
            and pir_idle_s >= holds * OCCUPANCY_HOLD_MIN * 60.0)


class DeviceMonitor:
    """Persist read-only appliance evidence without blocking comfort ticks."""

    def __init__(self, registry, telemetry: Telemetry,
                 sample_seconds: float = TICK_SECONDS):
        self.registry = registry
        self.telemetry = telemetry
        self.sample_seconds = max(10.0, sample_seconds)
        self.latest: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "DeviceMonitor":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="device-monitor", daemon=True)
            self._thread.start()
        return self

    def collect(self) -> dict:
        backend_health = self.registry.health().get("backends", {})
        fan_lan = (backend_health.get("atomberg", {}).get("lan", {})
                   .get("devices", {}))
        devices = []
        acceptable = True
        for device in self.registry.devices():
            if device.key == "ac" and not device.address:
                devices.append({
                    "device": device.key, "state": "response-only",
                    "available": False, "source": "ac-conditioning",
                    "reported": None,
                    "detail": "physical AC switch is not configured",
                    "lan_present": False, "excluded": True,
                })
                continue
            reported = self.registry.read_state(device.key)
            present = device.key == "fan" and device.address in fan_lan
            state = ("reported" if reported.available else
                     "presence-only" if present else "unavailable")
            acceptable = acceptable and state != "unavailable"
            devices.append({
                "device": device.key, "state": state,
                # What the registry calls this thing. The dashboard has cards
                # for the appliances it was written around; carrying kind and
                # label lets a device added to the registry later get a tile
                # that says what it is, without a second list to keep in sync.
                "kind": getattr(getattr(device, "kind", None), "value", None),
                "label": getattr(device, "label", "") or device.key,
                "available": reported.available, "source": reported.source,
                "reported": reported.state, "detail": reported.detail,
                "lan_present": present,
            })
        backend_ok = all(item.get("ok") for item in backend_health.values())
        result = {
            "ok": backend_ok and acceptable,
            "state": "ok" if backend_ok and acceptable else "degraded",
            "devices": devices, "backends": backend_health,
        }
        self.telemetry.system_health("devices", result["state"], result)
        self.latest = result
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.collect()
            except Exception as exc:
                self.latest = {"ok": False, "state": "failed",
                               "detail": f"{type(exc).__name__}: {exc}"}
                self.telemetry.system_health("devices", "failed", self.latest)
            self._stop.wait(self.sample_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


class Controller:
    def __init__(self, live: bool, source_name: str = "router",
                 automatic_actuation: Optional[bool] = None):
        self.live = live
        self.automatic_actuation = (
            live if automatic_actuation is None
            else live and bool(automatic_actuation))
        self.source = build_source(source_name).start()
        # Real read-only backends remain active in dry-run. Mock devices require
        # an explicit development flag, so a safe boot never labels simulated
        # device state as the room's actual state.
        use_mock = os.environ.get("BREEZEIQ_USE_MOCK_DEVICES", "0") == "1"
        self.registry = build_registry(use_mock=use_mock, live_enabled=live)
        self.command_server: Optional[ControlSocketServer] = None
        self.occupancy = OccupancyTracker()
        self.plan, self.memory = Plan(), Memory()
        # Started unconditionally, even in dry-run: the Edge Impulse clock is
        # the longest lead time in the project and nothing should delay it.
        self.telemetry = Telemetry(os.environ.get("BREEZEIQ_DB", "telemetry.sqlite3"))
        self.occupancy.restore(self.telemetry.latest_pir_motion_at())
        try:
            self.ac_twin: Optional[AcDigitalTwin] = AcDigitalTwin(self.telemetry)
        except Exception as exc:
            self.ac_twin = None
            self.telemetry.system_health(
                "ac_conditioning", "failed",
                {"error": f"{type(exc).__name__}: {exc}"})
        self.energy_monitor = EnergyMonitor(EnergyCollector(
            self.telemetry, self.registry.backend("homeassistant"))).start()
        self.device_monitor = DeviceMonitor(
            self.registry, self.telemetry).start()
        # A restart must not change the room. `Plan()` defaults every device to
        # off, and the first tick sees `previous is None`, so every device is
        # "changed" and gets commanded to that default — which is how a deploy
        # switched the light off nine times in one afternoon, each one logged
        # with reason "init".
        #
        # So the boot plan is the room as it actually is, read back from the
        # devices themselves. Then the first tick compares against reality and
        # commands only what genuinely differs. Same principle as
        # `occupancy.restore()` above: state that outlived the process is
        # recovered, not assumed.
        self.plan = self._adopt_room_state(self.plan)
        self.last_applied: Optional[Plan] = self.plan
        self.ticks = 0
        # Wall-clock of the previous tick. Every duration this controller reasons
        # about is measured against it rather than counted in ticks, because the
        # loop is no longer guaranteed to run on a fixed interval. None means
        # "first tick", which is charged the nominal interval.
        self.last_tick_at: Optional[float] = None
        # Monotonic minutes handed to the capture policy, accumulated from the
        # same measured elapsed time everything else uses. Not wall-clock: only
        # differences matter to the policy, and accumulating the clamped delta
        # means a clock jump cannot make it skip a baseline capture.
        self.clock_min = 0.0
        # Ring of recent (timestamp, indoor temperature) pairs for the AR
        # forecast. Timestamped, not bare values: `weighted_trend` regresses on
        # real elapsed time, and an unevenly-sampled window read as evenly spaced
        # reports a slope the room does not have.
        self.history: deque = deque(maxlen=fc.DEFAULT_LAGS + 1)
        # Refit from this room's own telemetry; until then
        # EWMA carries it. None here means "use the built-in default".
        self.ar_weights, self.ar_intercept = _load_ar_weights()
        self.last_forecast: Optional[fc.Forecast] = None
        self.counter = self._open_counter()
        self._camera_open_min: Optional[float] = None
        self.vision = policy.VisionState()
        self.last_vision_frame = None
        self.light_evidence = fuse_light(None, None)
        # How long, in seconds, the camera has kept seeing somebody without a
        # break. Lives on the controller because it is sensor interpretation
        # across time, which the ladder — a pure function of one reading — has
        # nowhere to keep.
        self.people_seen_s = 0.0
        # Last tick's camera verdict, consumed by the radar gate at the top of
        # the next one. See the gate for why a tick of lag is not a problem.
        self._camera_saw_empty = False
        self._holds_released = False
        # How long the light evidence has continuously wanted the lamp on.
        self.light_wants_on_s = 0.0
        # The last glyph the LED matrix acknowledged, so an unchanged one is
        # never re-sent down a Bridge RPC that costs a round trip.
        self.last_display: Optional[tuple] = None
        # The predictive layer's envelope. Engineering priors until
        # `calibrate.py` has enough logged variety to identify a coefficient, so
        # a board with no history still projects something physically sensible
        # rather than refusing to plan.
        self.envelope = self._load_envelope()
        self.last_advisory: Optional[hz.Advisory] = None
        # Set by the change watcher when something worth re-deciding on happens.
        # The loop waits on this with TICK_FLOOR_S as a timeout, so an arrival is
        # acted on at once and an unchanging room costs one cheap tick.
        # The last occupancy edge this process recorded, so a transition is
        # written once when it happens rather than re-asserted every tick.
        self._last_edge: Optional[tuple] = None
        self.wake = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._watch_stop = threading.Event()
        self._watch_seen: Optional[tuple] = None
        # component -> seconds bad so far, and component -> "already said so",
        # so a dead sensor writes one health row per episode, not one per tick.
        self._sensor_streaks: dict = {}
        self._sensor_failed: dict = {}

    def start_command_server(self) -> ControlSocketServer:
        """Expose manual commands through this controller's guarded registry.

        Only the long-running process calls this.  Tests and one-shot dry runs
        stay socket-free, and the dashboard never creates a competing device
        registry or receives vendor credentials.
        """
        if self.command_server is None:
            self.command_server = ControlSocketServer(self.registry).start()
        return self.command_server

    def _open_counter(self):
        """The people counter, or None. No camera, no model, no OS permission —
        all three degrade to "no count". The comfort loop predates the camera and
        must keep running without it."""
        if not COUNTER:
            return None
        try:
            cam = int(CAMERA) if CAMERA.isdigit() else CAMERA
            return build_counter(COUNTER, camera=cam).open()
        except Exception as exc:
            print(f"  camera disabled ({type(exc).__name__}): {exc}", file=sys.stderr)
            return None

    def _reopen_camera(self, now_min: float) -> bool:
        """Reopen a camera that was absent at startup or has dropped off the bus.

        A USB camera that browns out re-enumerates under a NEW device number, so
        the handle taken at startup is dead for good and no amount of reading it
        recovers. Opening once meant the first dropout disabled the camera for
        the rest of the session — the loop kept running on PIR alone and said so
        only in a log line nobody was watching.

        Slow timer: a failed open probes the device, which is not free, and a
        camera that is genuinely gone should not be probed every tick.
        """
        # None means "opened this session, clock not yet taken". Anchoring on the
        # first call rather than 0.0 matters because now_min is monotonic, not
        # epoch: against 0.0 the very first tick would look infinitely overdue
        # and tear down a camera that had just come up.
        if self._camera_open_min is None:
            self._camera_open_min = now_min
            return self.counter is not None
        if now_min - self._camera_open_min < CAMERA_REOPEN_MIN:
            return self.counter is not None
        self._camera_open_min = now_min
        if self.counter is not None:
            try:
                self.counter.close()
            except Exception:
                pass
            self.counter = None
        self.counter = self._open_counter()
        if self.counter is not None:
            print("  camera reopened", file=sys.stderr)
        return self.counter is not None

    def _look(self, pir: Optional[bool], now_min: float) -> Optional[int]:
        """Return a recent valid count; expose stale/failure separately.

        `now_min` is real monotonic minutes, which is what `should_capture` has
        always asked for in its own docstring. It used to be handed
        `self.ticks * DT_MIN` — a tick counter wearing a clock's clothes — so
        every interval in the capture policy (the 5 min baseline, the 10 min PIR
        quiet window, the 15 s rate limit) silently scaled with the cadence.
        """
        if self.counter is None and not self._reopen_camera(now_min):
            return None
        previous_capture = self.vision.last_capture_min
        d = policy.should_capture(now_min, bool(pir), self.vision)
        self.vision = d.state
        # The App Lab Brick infers continuously, so reading its vote is free and
        # the policy's 5 min baseline would only add occupancy latency. Backends
        # that pay per inference still obey the policy exactly as before.
        if d.capture or self.counter.CONTINUOUS:
            observed = self.counter.count()
            self.last_vision_frame = observed
            if observed.valid:
                self.vision = policy.record_count(self.vision, observed.count)
            else:
                # A look that failed is not a look. Restoring the timestamp makes
                # the next tick retry instead of waiting out the 5 min baseline —
                # otherwise the startup race between this loop and the detector
                # connecting leaves the camera reported dead for five minutes
                # after every restart, which is most of a demo.
                self.vision = replace(self.vision, last_capture_min=previous_capture)
        good = self.counter.last_good()
        age = None if good is None else good.age_s()
        # The Brick infers continuously, so minutes without a fresh detection is
        # a device that went away, not a quiet room. Reopening is rate-limited
        # inside _reopen_camera, so this can be checked every tick for free.
        if age is None or age > CAMERA_DEAD_S:
            self._reopen_camera(now_min)
        if good is None or age > VISION_STALE_S:
            return None
        return good.count

    def vetoed_devices(self) -> frozenset:
        """Which devices the occupant has forbidden, re-read every tick.

        Re-read rather than cached for the same reason the light preference is:
        the dashboard writes it from another process, and a preference that only
        took effect after a restart would not feel like a preference at all.

        Fails OPEN. A locked database or an unreadable value leaves everything
        allowed, because the failure mode of a wrongly-applied veto is a room
        nobody can cool.
        """
        try:
            stored = self.telemetry.preferences()
        except Exception:
            return frozenset()
        out = set()
        for device in VETOABLE:
            value = str(stored.get(f"{VETO_PREFIX}{device}", "")).strip().lower()
            if value in VETO_DENIES:
                out.add(device)
        return frozenset(out)

    def light_preference(self) -> str:
        """The occupant's stored lighting preference, re-read every tick.

        The dashboard writes this from another process, so caching it would
        make a tap take effect only after a restart. A preference is also a
        comfort nicety: a locked database or an unrecognised stored value falls
        back to the policy default rather than stopping a comfort tick.
        """
        try:
            stored = self.telemetry.preference(LIGHT_PREFERENCE_KEY)
        except Exception:
            stored = None
        return stored if stored in LIGHT_PREFERENCES else DEFAULT_LIGHT_PREFERENCE

    def _vision_status(self) -> dict:
        good = self.counter.last_good() if self.counter is not None else None
        latest = self.last_vision_frame
        age = good.age_s() if good else None
        return {
            "health": "offline" if self.counter is None else
                      "degraded" if latest is not None and not latest.valid else
                      "stale" if age is not None and age > VISION_STALE_S else
                      "ok" if good is not None else "starting",
            "source": getattr(self.counter, "name", None),
            "count": good.count if good and age <= VISION_STALE_S else None,
            "confidence": good.confidence if good else None,
            "last_valid_at": good.at if good else None,
            "age_s": round(age, 1) if age is not None else None,
            "luminance": good.luminance if good else None,
            "fault": latest.fault if latest is not None and not latest.valid else "",
            "privacy": "frames discarded; no runtime image storage",
        }

    def _show(self, mode: str, people: Optional[int]) -> None:
        """Put the room's own state on the board's LED matrix.

        A status light, not an actuator: it stays out of the device registry
        and the actuation journal, and a board that does not answer is never a
        reason to stop deciding. The push only goes out when the glyph would
        actually change, and the new one is remembered only once the MCU
        acknowledges it — a push that never landed has to be retried, not
        cached as if the matrix were already showing it.
        """
        index = DISPLAY_MODES.get(mode)
        if index is None:
            return
        frame = (index, max(0, int(people or 0)))
        if frame == self.last_display:
            return
        if display(*frame):
            self.last_display = frame

    def _sensor_episode(self, component: str, bad: bool, detail: dict,
                        elapsed_s: float) -> None:
        """One health row when a sensor dies, one while it is working.

        A sensor that reports nothing is indistinguishable from a quiet one for
        a tick or two, so the row is only written after SENSOR_FAIL_S of
        continuous silence — and only once, because a row every tick buries the
        moment it broke under thousands of identical rows a day.

        Accumulates elapsed SECONDS rather than counting ticks: at a variable
        cadence a tick count says nothing about how long the sensor has actually
        been quiet, and "broken" is a statement about duration.

        Health is ASSERTED while the sensor works, not only announced on the
        recovery edge. `_sensor_failed` lives in this process; the stored row
        was written by whatever process ran last. A camera that failed, then
        came back while the app was restarting, leaves no edge to announce — so
        the Workbench read `camera failed` for as long as the board stayed up
        while the Occupancy card counted people off that same camera.
        `Telemetry.system_health` dedupes, so asserting costs one row per
        episode plus the HEALTH_HEARTBEAT_S heartbeat every other component
        already gets, and a stale row can no longer outlive its writer.
        """
        held = self._sensor_streaks.get(component, 0.0) + elapsed_s if bad else 0.0
        self._sensor_streaks[component] = held
        failed = self._sensor_failed.get(component, False)
        if bad and held >= SENSOR_FAIL_S and not failed:
            self._sensor_failed[component] = True
            self.telemetry.system_health(
                component, "failed", {**detail, "quiet_s": round(held, 1)})
        elif not bad:
            self._sensor_failed[component] = False
            self.telemetry.system_health(component, "ok", detail)

    def _watch_sensors(self, frame, vision_status: dict,
                       elapsed_s: float) -> None:
        """Raise a fault for a sensor that has stopped reporting entirely.

        A dead LDR reads a plausible-looking raw 0 forever, and until this ran
        nothing said so: the light fusion simply degraded to "unavailable" and
        the room went on holding its devices. An invalid frame is skipped
        rather than counted — that is the router's fault, already reported, and
        counting it would blame the sensor for a transport gap.
        """
        # health() is the reader's own description of the link; a source that
        # does not implement it is not a bug, it is a simulated one.
        try:
            reader_health = self.source.health() or {}
        except Exception:
            reader_health = {}
        if frame.valid:
            self._sensor_episode(
                "light_sensor",
                frame.solar_index is None and getattr(frame, "lux", None) is None,
                {"solar_index": frame.solar_index,
                 "light_raw": getattr(frame, "light_raw", None),
                 # The I²C part reports its own presence, which the divider
                 # never could. Carried here so "no lux" says whether the
                 # sensor is absent from the bus or merely quiet.
                 "lux": getattr(frame, "lux", None),
                 "bh1750_address": reader_health.get("bh1750_address"),
                 "i2c_devices": reader_health.get("i2c_devices"),
                 "sda_pullup": getattr(frame, "sda_pullup", None),
                 "scl_pullup": getattr(frame, "scl_pullup", None),
                 "sda_level": getattr(frame, "sda_level", None),
                 "scl_level": getattr(frame, "scl_level", None),
                 "detail": "no usable light index"},
                elapsed_s)
        health = vision_status.get("health")
        self._sensor_episode(
            "camera", health != "ok",
            {"health": health, "source": vision_status.get("source"),
             "fault": vision_status.get("fault"),
             "detail": "camera has not returned a usable count"},
            elapsed_s)

    def _load_envelope(self) -> hz.Envelope:
        """The newest accepted fit, or the engineering priors.

        Priors are a working default, not a placeholder: `room_model`'s lumped-RC
        numbers describe a real bedroom, so a board with no history still projects
        something physically sensible on its first boot instead of refusing to
        plan. `calibrate.py` only ever replaces the coefficients this room's own
        logged data can actually identify, and `provenance` carries which — so a
        projection can never be read as better-founded than it is.
        """
        try:
            row = self.telemetry.latest_envelope_fit()
        except Exception:
            row = None
        if not row:
            return hz.Envelope()
        try:
            return hz.Envelope(
                tau_h=float(row["tau_h"]),
                solar_c_per_h=float(row["solar_c_per_h"]),
                shade_block=float(row["shade_block"]),
                vent_c_per_h_per_c=float(row["vent_c_per_h_per_c"]),
                occupant_c_per_h=float(row["occupant_c_per_h"]),
                provenance=str(row.get("provenance") or "fitted"))
        except (KeyError, TypeError, ValueError) as exc:
            # A malformed row must not stop the room working. Priors carry it and
            # the fault is recorded rather than silently swallowed.
            self.telemetry.system_health(
                "horizon", "degraded",
                {"error": f"unusable envelope_fit row: {exc}",
                 "using": "engineering priors"})
            return hz.Envelope()

    # ── cadence ─────────────────────────────────────────────────────────
    def start_change_watcher(self) -> "Controller":
        """Watch for the events worth interrupting a wait for.

        Same shape as DeviceMonitor: a daemon thread, an Event to stop it, and
        `Event.wait` rather than `sleep` so shutdown is immediate. It reads the
        source once a second, which costs nothing — `SensorSource.read()` returns
        already-buffered state and performs no I/O of its own.
        """
        if self._watcher is None:
            self._watcher = threading.Thread(
                target=self._watch, name="change-watcher", daemon=True)
            self._watcher.start()
        return self

    def _watch(self) -> None:
        while not self._watch_stop.is_set():
            try:
                frame = self.source.read()
                seen = (bool(frame.occupied), frame.indoor_c)
                before = self._watch_seen
                self._watch_seen = seen
                if before is not None and self._material(before, seen):
                    self.wake.set()
            except Exception:
                # A watcher that cannot read is not a reason to stop deciding;
                # the floor still fires and the tick reports the fault itself.
                pass
            self._watch_stop.wait(1.0)

    @staticmethod
    def _material(before: tuple, now: tuple) -> bool:
        """Is this change worth a fresh decision right now?"""
        was_motion, was_c = before
        motion, indoor_c = now
        if motion and not was_motion:
            return True                      # somebody arrived
        if was_c is not None and indoor_c is not None:
            return abs(indoor_c - was_c) >= WAKE_STEP_C
        return False

    def wait_for_next_tick(self) -> str:
        """Block until something happens or the floor expires.

        Both entrypoints call this instead of sleeping, so the App Lab thread and
        the standalone CLI cannot drift into different cadences.
        """
        woken = self.wake.wait(TICK_FLOOR_S)
        self.wake.clear()
        return "event" if woken else "floor"

    def stop_change_watcher(self) -> None:
        self._watch_stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=2)

    def _record_edge(self, occupant: str, people: Optional[int],
                     vision_status: dict, absence_confirmed: bool,
                     at: float) -> Optional[str]:
        """Write the moment the room filled or emptied, with its evidence.

        Until this ran, a transition existed nowhere discrete — `Telemetry.event`
        was fully built and had no callers anywhere in the project, so the only
        way to answer "when did they leave?" was to pull every tick row and diff
        consecutive `occupant` values. The edge is the interesting instant and the
        one a demo points at; it deserves its own row.

        The evidence is recorded alongside it because the two sensors disagree in
        useful ways. "Camera saw them, PIR was quiet" and "PIR fired, camera
        offline" are different arrivals, and which one happened is exactly what
        you want to know when a decision later looks wrong.
        """
        present = occupant != AWAY
        edge = (present, bool(people))
        if self._last_edge is None:
            self._last_edge = edge
            return None
        was_present, _ = self._last_edge
        self._last_edge = edge
        if present == was_present:
            return None

        kind = "occupancy_arrival" if present else "occupancy_departure"
        self.telemetry.event(kind, _json_detail({
            "occupant": occupant,
            "people": people,
            "camera_health": vision_status.get("health"),
            "camera_confidence": vision_status.get("confidence"),
            "pir_idle_s": (round(self.occupancy.idle_s, 1)
                           if self.occupancy.idle_s is not None else None),
            # Only meaningful on a departure, and it is the load-bearing fact
            # there: an unconfirmed absence holds physical state rather than
            # switching a room off on an inference.
            "absence_confirmed": absence_confirmed if not present else None,
            "at": at,
        }))
        return kind

    def _release_holds_when_empty(self, absence_confirmed: bool,
                                  occupant: str) -> None:
        """Drop every manual hold once the room is confirmed empty.

        A hold is a person saying "leave this alone for now", and it stops
        being true the moment there is no person. Left standing it outlives the
        room: a fan pinned to 4 by hand at lunchtime keeps running all
        afternoon, because an override suppresses reconciliation entirely and
        the empty-room branch never gets to switch anything off.

        Gated on `absence_confirmed`, not on the AWAY word, so it needs the
        same two-sensor proof that anything else destructive does — a quiet PIR
        alone has never been enough to call this room empty and is not enough
        here either.

        Vetoes are untouched. Those live in user_preference, not the override
        journal, and they are standing settings rather than temporary holds:
        "never use the compressor for me" does not expire because somebody
        left the room. Only fires on the edge into empty, so a room that stays
        empty does not re-clear nothing every tick.
        """
        if not (absence_confirmed and occupant == "AWAY"):
            self._holds_released = False
            return
        if getattr(self, "_holds_released", False):
            return
        self._holds_released = True
        cleared = []
        for key in VETOABLE:
            try:
                if self.registry.return_to_auto(
                        key, operator="automation",
                        reason="room confirmed empty"):
                    cleared.append(key)
            except Exception:
                continue
        if cleared:
            self.telemetry.event("holds_released_empty_room",
                                 json.dumps(sorted(cleared)))

    def _adopt_room_state(self, plan: Plan) -> Plan:
        """The room as the devices report it, for the boot plan.

        Only fields a device actually answers for are taken. A device that is
        unreachable, or a backend that is not configured, leaves its field at
        the `Plan()` default — which is the safe reading of an unknown device
        and the behaviour this had before. Nothing here commands anything; it
        only decides what the first tick will consider "unchanged".

        `state_of` returns None for unavailable, which is why each field is
        tested against None rather than truthiness: an unreachable lamp and a
        lamp that is off are different facts, and only the second one may be
        adopted as the room's state.
        """
        fields: dict = {}
        for key, field in (("ac", "ac"), ("tubelight", "light")):
            try:
                state = self.registry.state_of(key)
            except Exception:
                continue
            if state is not None:
                fields[field] = bool(state)
        # The covers report the cover contract, which the plan stores inverted
        # for the blind and straight for the window — the same asymmetry
        # `apply()` documents. Reading them back has to undo it the same way.
        try:
            blinds = self.registry.state_of("blinds")
            if blinds is not None:
                fields["blinds_shut"] = not bool(blinds)
        except Exception:
            pass
        try:
            window = self.registry.state_of("window")
            if window is not None:
                fields["window"] = bool(window)
        except Exception:
            pass
        try:
            fan = self.registry.read_state("fan")
            speed = (fan.state or {}).get("speed") if fan.available else None
            if speed is not None:
                vendor_to_level = {v: k for k, v
                                   in FAN_LEVEL_TO_VENDOR_SPEED.items()}
                # An unrecognised vendor speed is real evidence the fan is
                # running, so it rounds to the nearest level we model rather
                # than silently reading as off.
                fields["fan"] = vendor_to_level.get(
                    int(speed),
                    min(FAN_LEVEL_TO_VENDOR_SPEED,
                        key=lambda lvl: abs(FAN_LEVEL_TO_VENDOR_SPEED[lvl]
                                            - int(speed))))
        except Exception:
            pass
        if not fields:
            return plan
        adopted = replace(plan, **fields, reason="adopted the room as found")
        self.telemetry.event("boot_adopted_room", json.dumps(fields, sort_keys=True))
        return adopted

    def _advise(self, reading: Reading, twin, forecast,
                vetoed=frozenset()) -> Optional[hz.Advisory]:
        """Ask the predictive layer what an hour of each rung would cost.

        Kept out of `decide()` because the projection needs the response model's
        state and the fitted envelope, neither of which belongs inside a pure
        function. Any failure here degrades to "no advisory", which is exactly
        the reactive ladder this project shipped with — a planner that cannot
        plan must never be a planner that stops the room working.
        """
        try:
            m = room_mode(reading)
            staged = replace(self.plan, mode=m)
            baseline = (twin or {}).get("sensed_indoor_c")
            return hz.advise(
                reading, staged, gates(reading, staged),
                climb=_climb_one_rung,
                start_c=reading.indoor,
                baseline_c=baseline if baseline is not None else reading.indoor,
                setpoint_c=(twin or {}).get("setpoint_c") or 24.0,
                env=self.envelope,
                cooling_effect=(twin or {}).get("cooling_effect") or 0.0,
                watts=(twin or {}).get("estimated_watts") or 0.0,
                band_offset_pmv=_comfort_offset(reading, staged),
                # The mode's own ceiling, so the planner cannot offer a rung the
                # ladder would immediately cap away — a sleeping room must never
                # be handed a full draft, and a projection priced on FAN_HIGH for
                # a bed is priced on an action that will not happen.
                fan_ceiling=policy_for(m).fan_ceiling,
                vetoed=vetoed,
                ar_t_hat=getattr(forecast, "t_hat", None))
        except Exception as exc:
            self.telemetry.system_health(
                "horizon", "failed", {"error": f"{type(exc).__name__}: {exc}"})
            return None

    def _elapsed_min(self, now: float) -> float:
        """Real minutes since the previous tick.

        Every duration downstream — dwell, the hot/cold streaks, the camera
        debounce, the sensor-silence timer — is charged against this rather than
        a constant, so the loop can be woken by an event at 3 s or idle for 30 s
        and the hysteresis still measures the same physical time. The first tick
        has no predecessor and is charged the nominal interval.

        Clamped: a clock that jumped (NTP step, suspend/resume) must not credit
        the streaks with hours of room history that was never observed.
        """
        if self.last_tick_at is None:
            return DT_MIN
        delta = (now - self.last_tick_at) / 60.0
        return min(max(delta, 0.0), MAX_ELAPSED_MIN)

    def tick(self, at: Optional[float] = None) -> dict:
        """One decision. `at` overrides the clock, exactly as
        `AcDigitalTwin.step(at=...)` does — now that durations are measured
        rather than counted, a test that calls tick() twice in a microsecond
        would otherwise observe no elapsed time at all and no timer would ever
        advance. Production passes nothing and gets the wall clock."""
        now = float(at if at is not None else time.time())
        dt_min = self._elapsed_min(now)
        elapsed_s = dt_min * 60.0
        self.last_tick_at = now
        # Monotonic minutes for the capture policy. Derived by accumulating the
        # same measured elapsed time the rest of the tick uses, so the policy's
        # clock cannot drift away from the controller's.
        self.clock_min += dt_min

        frame = self.source.read()
        # Radar is silenced by a camera that positively saw an empty room.
        #
        # Without this the part can hold a room AWAKE on its own, and holding
        # AWAKE is stronger than it looks: `absence_confirmed` only releases an
        # AWAY plan for application, so a room that never says AWAY never even
        # proposes switching anything off. A radar fooled by fan blades — which
        # Hi-Link's manual says happens — would keep the fan running forever,
        # which is the exact incident the absence gate was written for.
        #
        # A healthy camera counting zero is the one piece of evidence that
        # outranks it, mirroring `confirm_absence`'s own two-sensor rule. It is
        # last tick's verdict because the camera is read further down this one;
        # against a ten-minute occupancy hold a single tick of lag changes
        # nothing, and the fallback is the safe direction anyway — an unread
        # camera leaves the radar trusted.
        occupant = self.occupancy.update(
            frame.occupied,
            radar=trust_radar(getattr(frame, "radar_presence", None),
                              self._camera_saw_empty))

        # The AC-conditioned temperature is the controller's room state. Raw
        # DHT22 evidence remains untouched in `frame` and is stored separately.
        # Automatic policy intent takes effect on the following tick, matching
        # a real actuator/sensor loop; a manual AC request is read immediately.
        # A physical AC must drive the response model from independent HA
        # readback. This keeps manual control, automatic intent, modeled room
        # response, and the next comfort decision on the same power state.
        # AN INVALID FRAME MUST NOT REACH THE RESPONSE MODEL.
        #
        # `frame_to_reading` returns None for an invalid frame and the tick ends
        # before `decide()` — that is the documented promise, and it holds. But
        # the twin was stepped ABOVE that check, so a bad reading could not drive
        # an actuator directly and could still poison the model that does: the
        # twin's output becomes `effective_indoor`, which is the temperature the
        # next tick's comfort decision is made on.
        #
        # It happened. The DHT22 emitted -2.1 C for one sample. The reader
        # correctly marked the frame invalid (TEMP_MIN_C is 5.0), the tick
        # correctly refused to decide on it — and the twin had already taken it
        # as the room's baseline, clamped the model to -2.1 + max_sensor_delta_c,
        # and reported a 12.9 C room. It then spent hours crawling back at the
        # rebound time constant while the real room sat at 25.5 C.
        #
        # So the model is stepped with the sensor only when the sensor is worth
        # believing. On an invalid frame it advances on its own dynamics instead,
        # which is exactly what it is for.
        ac_power = self._physical_ac_power(self.plan.ac)
        believable = frame.valid
        twin = self._step_twin(
            frame.indoor_c if believable else None,
            frame.outdoor_c if believable else None, ac_power,
            indoor_rh=frame.indoor_rh if believable else None)
        effective_indoor = (
            twin.get("modeled_c") if isinstance(twin, dict)
            and twin.get("modeled_c") is not None else frame.indoor_c)

        # Forecast BEFORE deciding, from history that excludes this reading —
        # predicting the sample you already hold is not a forecast. The window
        # carries its own timestamps so the slope is a real rate (°C/hour) even
        # when the samples are unevenly spaced.
        f = fc.forecast([v for _, v in self.history], weights=self.ar_weights,
                        intercept=self.ar_intercept,
                        times_s=[t for t, _ in self.history])
        self.last_forecast = f
        if frame.valid and effective_indoor is not None:
            self.history.append((now, effective_indoor))

        people = self._look(frame.occupied, self.clock_min)
        vision_status = self._vision_status()
        self._camera_saw_empty = (vision_status.get("health") == "ok"
                                  and people == 0)
        self._watch_sensors(frame, vision_status, elapsed_s)
        # A seen person outranks a quiet PIR: someone reading in a chair goes
        # AWAY on the tracker but is plainly still in the room. The reverse does
        # NOT hold — one fixed lens has blind spots, so a count of 0 never
        # evicts an occupant. Two sensors, different blind spots, fail-safe.
        #
        # It takes PEOPLE_PROMOTE_S of unbroken agreement, though: a lone frame
        # is a flap, and acting on one toggled the ceiling light all night. A real
        # arrival is not delayed by this — a person walking in trips the PIR,
        # and the tracker returns AWAKE before this line is reached.
        self.people_seen_s = self.people_seen_s + elapsed_s if people else 0.0
        if occupant == "AWAY" and self.people_seen_s >= PEOPLE_PROMOTE_S:
            occupant = "AWAKE"

        reading = frame_to_reading(
            frame, occupant, trend=f.trend or None, people=people,
            people_confidence=vision_status.get("confidence"),
            effective_indoor_c=effective_indoor,
            effective_indoor_rh=(twin.get("modeled_rh")
                                 if isinstance(twin, dict) else None),
            comfort_c=requested_setpoint_c(twin))

        if reading is None:
            return {"ok": False, "why": frame.fault or "no valid reading",
                    "occupant": occupant, "digital_twin": twin}

        vetoed = self.vetoed_devices()
        advisory = self._advise(reading, twin, f, vetoed)
        self.last_advisory = advisory
        new_plan, self.memory = decide(reading, self.plan, self.memory, dt_min,
                                       advisory=advisory, vetoed=vetoed)
        # Calibrated lux wins over the divider when the part answers. Both
        # describe daylight on the glass, but one needs a span learned from
        # having seen its own extremes and the other is absolute from the
        # first reading — and the divider has been an open circuit once and an
        # intermittent joint twice. The LDR stays as the fallback rather than
        # being removed, so a BH1750 that stops answering degrades to the old
        # behaviour instead of to nothing.
        daylight = lux_index(getattr(frame, "lux", None))
        if daylight is None:
            daylight = frame.solar_index
        self.light_evidence = fuse_light(daylight,
                                         vision_status.get("luminance"))
        preference = self.light_preference()
        self.light_wants_on_s = (
            self.light_wants_on_s + elapsed_s
            if wants_auto_on(preference, self.light_evidence) else 0.0)
        light_on, light_reason = decide_light(
            occupant != "AWAY", new_plan.light, self.light_evidence,
            preference=preference,
            asleep=new_plan.mode == MODE_ASLEEP,
            confirmed=self.light_wants_on_s >= LIGHT_CONFIRM_S)
        # A vetoed lamp is never switched ON automatically. Switching one OFF
        # for an empty room stays allowed: the veto forbids the machinery
        # acting, not the room going dark when nobody is in it.
        if "light" in vetoed and light_on and not new_plan.light:
            light_on, light_reason = new_plan.light, "held: light vetoed"
        if light_on != new_plan.light:
            new_plan = replace(new_plan, light=light_on,
                               reason=f"{new_plan.reason} | light: {light_reason}")
        self._show(new_plan.mode, people)
        pmv = comfort(reading.indoor, reading.indoor_rh, reading.solar,
                      new_plan.fan, new_plan.window, new_plan.blinds_shut,
                      reading.occupant)

        # Per-device reconciliation handles three cases without quota-burning
        # polling: new policy intent, failed acknowledgement, and an expired
        # manual override whose automatic intent did not otherwise change.
        absence_confirmed = confirm_absence(
            people, vision_status, self.light_evidence,
            pir_idle_s=self.occupancy.idle_s)
        results = self.apply(
            new_plan, reading, previous=self.last_applied,
            absence_confirmed=absence_confirmed, vetoed=vetoed)
        self.last_applied = new_plan

        self._release_holds_when_empty(absence_confirmed, occupant)
        edge = self._record_edge(occupant, people, vision_status,
                                 absence_confirmed, now)
        if advisory is not None:
            self.telemetry.horizon_decision(
                acted=advisory.plan is not None, reason=advisory.reason,
                horizon_min=hz.HORIZON_MINUTES,
                band=f"ISO 7730 Category B (PMV +/-{hz.ISO_CATEGORY_B_PMV}, "
                     f"PPD <={hz.ISO_CATEGORY_B_PPD}%)",
                watt_hours=advisory.watt_hours, worst_ppd=advisory.worst_ppd,
                cheaper_by_wh=advisory.cheaper_by_wh,
                chosen={"fan": new_plan.fan, "window": new_plan.window,
                        "blinds_shut": new_plan.blinds_shut, "ac": new_plan.ac},
                provenance=advisory.provenance, at=now)

        light_payload = asdict(self.light_evidence)
        self.telemetry.write(
            frame, pmv, new_plan, occupant, people,
            vision=vision_status, light_evidence=light_payload,
            pir_last_motion_at=self.occupancy._last_motion)
        self.plan = new_plan
        self.ticks += 1
        return {"ok": True, "reading": reading, "pmv": pmv, "plan": new_plan,
                "mode": new_plan.mode,
                "forecast": {"t_hat": f.t_hat, "trend": f.trend,
                             "verdict": f.verdict, "score": f.score},
                "horizon": {
                    "acted": advisory is not None and advisory.plan is not None,
                    "reason": advisory.reason if advisory else "no projection",
                    "watt_hours": advisory.watt_hours if advisory else None,
                    "worst_ppd": advisory.worst_ppd if advisory else None,
                    "cheaper_by_wh": (advisory.cheaper_by_wh
                                      if advisory else None),
                    "provenance": (advisory.provenance if advisory
                                   else self.envelope.provenance),
                    "band": "ISO 7730 Category B",
                },
                "vision": vision_status,
                "digital_twin": twin,
                "light_evidence": light_payload,
                "occupant": occupant, "results": results, "frame": frame,
                "people": people, "logged": self.telemetry.rows,
                "edge": edge, "absence_confirmed": absence_confirmed}

    def _step_twin(self, indoor_c, outdoor_c, automatic_power: bool,
                   indoor_rh=None):
        if self.ac_twin is None:
            return None
        try:
            state = self.ac_twin.step(
                indoor_c, outdoor_c, automatic_power, at=time.time(),
                indoor_rh=indoor_rh)
            self.telemetry.system_health(
                "ac_conditioning", "ok",
                {"status": state.status, "mode": state.mode,
                 "provenance": state.provenance})
            return state.as_dict()
        except Exception as exc:
            detail = {"error": f"{type(exc).__name__}: {exc}"}
            self.telemetry.system_health("ac_conditioning", "failed", detail)
            return {"status": "failed", **detail}

    def _physical_ac_power(self, fallback: bool) -> bool:
        """Use independently reported AC power when a physical switch exists."""
        if os.environ.get("BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") != "1":
            return bool(fallback)
        latest = getattr(getattr(self, "device_monitor", None), "latest", None)
        for item in (latest or {}).get("devices", []):
            if item.get("device") != "ac" or not item.get("available"):
                continue
            reported = item.get("reported")
            if isinstance(reported, dict) and isinstance(
                    reported.get("power"), bool):
                return reported["power"]
        return bool(fallback)

    def apply(self, plan: Plan, reading: Reading,
              previous: Optional[Plan] = None,
              absence_confirmed: bool = False,
              vetoed=frozenset()) -> list:
        """Ladder intent -> guarded registry commands."""
        # A quiet PIR cannot prove an empty room. If the camera is unavailable,
        # retain current physical states instead of switching equipment off on
        # an absence inference. Manual commands remain available independently.
        if (self.automatic_actuation and reading.occupant == "AWAY"
                and not absence_confirmed):
            return ["[presence-hold] physical states retained; absence unconfirmed"]
        wants = [
            ("fan", "speed", plan.fan,
             previous is None or previous.fan != plan.fan),
            ("ac", "power", plan.ac,
             previous is None or previous.ac != plan.ac),
            ("tubelight", "power", plan.light,
             previous is None or previous.light != plan.light),
            # Cover contract is open=true / close=false, and blinds_shut=True
            # means SHADE — so the inversion is load-bearing: passing
            # plan.blinds_shut directly would open the blind into full sun.
            ("blinds", "power", not plan.blinds_shut,
             previous is None or previous.blinds_shut != plan.blinds_shut),
            # The window takes the same cover contract with no inversion, and
            # the asymmetry is in the field names, not the hardware: the plan
            # names the blind by what it does when true (shut) and the window
            # by what it does when true (open). Inverting this one would vent
            # the room exactly when the ladder wanted it sealed.
            ("window", "power", plan.window,
             previous is None or previous.window != plan.window),
        ]
        out = []
        for key, kind, value, changed in wants:
            # THE BACKSTOP. The ladder and the planner both already refuse to
            # propose a forbidden device; this is the one place that cannot be
            # reasoned around, and it is here because a veto enforced only by
            # the things that remember it is not a veto. Only blocks turning
            # something ON — a vetoed device may always be switched off.
            if _veto_blocks(key, kind, value, vetoed):
                out.append(f"[vetoed] {key} left alone by your preference")
                continue
            if kind == "speed":
                value = FAN_LEVEL_TO_VENDOR_SPEED.get(int(value), 0)
            requested = {kind: int(value) if kind == "speed" else bool(value)}
            if not changed and not self.registry.needs_reconcile(key, requested):
                continue
            if not self.automatic_actuation:
                mode = "observe-only" if self.live else "dry-run"
                out.append(f"[{mode}] {key} -> {value}")
                continue
            if kind == "speed":
                out.append(self.registry.set_speed(
                    key, int(value), reason=plan.reason))
            else:
                out.append(self.registry.set_power(
                    key, bool(value), reason=plan.reason))
        return out


def render(state: dict) -> str:
    if not state["ok"]:
        return f"  -- holding: {state['why']}  (occupant {state['occupant']})"
    r, p = state["reading"], state["plan"]
    rungs = " ".join(n for n, on in
                     [("SHADE", p.blinds_shut), ("VENT", p.window),
                      ("FAN", p.fan > 0), ("AC", p.ac)] if on) or "all off"
    lines = [
        f"  in {r.indoor:5.1f}C {r.indoor_rh:4.1f}%  out {r.outdoor:5.1f}C  "
        f"sun {r.solar:.2f}  {r.occupant:<7} PMV {state['pmv']:+.2f}"
        + (f"  people {state['people']}" if state.get("people") is not None else ""),
        f"  rungs: {rungs:<24} fan={p.fan} ac={'on' if p.ac else 'off'}",
        f"  why:   {p.reason}",
    ]
    for res in state["results"]:
        lines.append(f"         {res}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BreezeIQ control loop")
    ap.add_argument("--live", action="store_true",
                    help="actually command devices (default: dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="decide only, touch nothing")
    ap.add_argument("--once", action="store_true", help="a single tick then exit")
    ap.add_argument("--source", default=os.environ.get("BREEZEIQ_SOURCE", "router"))
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks")
    args = ap.parse_args(argv)

    live = args.live and not args.dry_run
    if live and os.environ.get("BREEZEIQ_SAFETY_VALIDATED", "0") != "1":
        print("refused: --live requires BREEZEIQ_SAFETY_VALIDATED=1", file=sys.stderr)
        return 2
    c = Controller(live, args.source)
    if not args.once:
        command = c.start_command_server()
        if command.error:
            print(f"  manual control unavailable: {command.error}")
        c.start_change_watcher()

    print(f"BreezeIQ control loop — {'LIVE, commanding real devices' if live else 'DRY RUN, nothing is touched'}")
    print(f"  source: {args.source}   wake: on change, floor {TICK_FLOOR_S:.0f}s"
          f"   occupancy hold: {OCCUPANCY_HOLD_MIN:.0f} min")
    if live:
        for name, h in c.registry.health()["backends"].items():
            print(f"  backend {name}: {'ok' if h.get('ok') else h.get('error')}")
    print()

    woke = "start"
    try:
        while True:
            state = c.tick()
            print(f"[tick {c.ticks}] {time.strftime('%H:%M:%S')}  ({woke})")
            print(render(state))
            print()
            if args.once or (args.ticks and c.ticks >= args.ticks):
                break
            woke = c.wait_for_next_tick()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        c.stop_change_watcher()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
