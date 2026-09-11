"""BreezeIQ board-hosted operator console.

The dashboard is intentionally a read model over board-local evidence. It does
not start a simulator, run a second comfort controller, or instantiate a live
device registry. Manual requests cross an explicitly enabled Unix-socket
boundary owned by the control process. Missing data is rendered as missing.

    ./tools/run.sh console           # http://127.0.0.1:8000

Set ``BREEZEIQ_HOST=0.0.0.0`` in the board service to reach it from the room
LAN. Live manual control additionally requires both an enabled safety gate and
the control-process socket. No appliance call is made from this process.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from digital_twin import (PROVENANCE as TWIN_PROVENANCE, TwinConfig,
                          advance as twin_advance)
from energy import estimate_ac_first_savings, summarize_intervals
from fusion import DEFAULT_LIGHT_PREFERENCE, LIGHT_PREFERENCES
from horizon import (HORIZON_MINUTES, ISO_CATEGORY_B_PMV,
                     ISO_CATEGORY_B_PPD)
from telemetry import Telemetry


_ROOT = Path(__file__).resolve().parent.parent
for _part in ("brain", "sense", "act"):
    _path = str(_ROOT / _part)
    if _path not in sys.path:
        sys.path.insert(0, _path)


DEFAULT_PORT = int(os.environ.get("BREEZEIQ_PORT", "8000"))
ABOUT_PAGE = Path(__file__).with_name("about.html").read_text()
STALE_AFTER_S = max(30, int(os.environ.get("BREEZEIQ_STALE_AFTER_S", "120")))
MAX_BODY_BYTES = 4096
# 24 h of 30 s ticks. The history tab is where a night is read back, and 180
# samples was 90 minutes of it. `_decimate` keeps the payload honest at this
# length by thinning only the part of the window nobody scrubs sample by sample.
HISTORY_LIMIT = 2880
HISTORY_FULL_RATE_S = 6 * 3600        # every sample inside this, every other one before
# How fresh an independent readback has to be to contradict automation. Shared
# with the page, which greys a card out at the same age.
DEVICE_STATE_FRESH_S = 300
DEVICE_KEYS = {"ac": "ac", "fan": "fan", "light": "tubelight"}
POWER_DEVICES = {"ac", "light"}
FAN_SPEED_MAX = int(os.environ.get("BREEZEIQ_FAN_SPEED_MAX", "5"))
# The ceiling fan's own scale, 0 is off. FIVE, not six: the journal shows
# speeds 0, 2 and 4 commanded thousands of times and 6 not once, because the
# ladder's "full" rung mapped to a speed this fan does not have. Env-settable
# because the next fan may count differently, and a scale is a property of the
# appliance rather than of this code.

# The two sysfs roots `_camera_link` reads. Named here rather than inlined so a
# test can point them at a fake tree: the console's camera diagnosis branches on
# what they produce, and it has twice sent a reader to re-cable the wrong thing.
# A branch that gives directions deserves a check that it gives the right ones.
USB_DEVICES = Path("/sys/bus/usb/devices")
VIDEO4LINUX = Path("/sys/class/video4linux")


def _is_downstream(name: str) -> bool:
    """Something somebody plugged in, rather than the controller's own furniture.

    Root hubs are `usb1`, `usb2`; interfaces carry a colon (`1-1.2:1.0`). What is
    left — `1-1`, `1-1.2` — is the hub and whatever hangs off it.
    """
    return not name.startswith("usb") and ":" not in name

# A device the registry gained after this page shipped — a window cover, say.
# The page can only pass a plausible key; the control process validates it
# against the live registry and refuses anything it does not own.
GENERIC_DEVICE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

# Stored occupant preferences. Names are shared with the control loop, which
# reads the same rows; ``fusion`` owns what a lighting value means.
LIGHT_PREFERENCE_KEY = "light_preference"
MANUAL_TTL_PREFERENCE_KEY = "manual_ttl_min"
DEFAULT_MANUAL_TTL_MIN = 30
MANUAL_TTL_CHOICES = (5, 15, 30, 60, 120, 240)
MAX_MANUAL_TTL_MIN = max(MANUAL_TTL_CHOICES)
# "Until I return to automatic": stored as a word rather than a huge number so
# the intent survives any future change to how long "no expiry" is in minutes.
UNTIL_AUTO = "until_auto"
# The standing per-device permission the control loop reads every tick. Spelled
# and defaulted here exactly as `loop.vetoed_devices` reads it: the row is an
# ALLOW, absent means allowed, and only these words deny. Duplicated rather than
# imported because dashboard.py deliberately imports nothing from the loop — but
# a mismatch would mean the page shows a veto the room does not obey, so the
# values are pinned against the loop's own constants in the tests.
VETO_PREFIX = "allow_"
VETO_DENIES = ("no", "false", "0", "off", "never", "deny")
VETOABLE = ("fan", "ac", "window", "blinds", "light")
# The last resort when a power tap has to name a temperature and nothing —
# neither a stored request nor a model state — has ever named one. Taken from
# the model's own default rather than typed here again, so the number this page
# writes is the number the control loop would have used anyway.
DEFAULT_SETPOINT_C = TwinConfig.default_setpoint_c


def _is_denied(value) -> bool:
    return str(value if value is not None else "").strip().lower() in VETO_DENIES
DEFAULT_TAP_REASON = "dashboard tap"

# ── the hour-ahead plan, rendered for a person ──────────────────────────
# The comfort band is stored PMV-native because that is what the standard is
# written in, and PMV is not a temperature anybody has a feel for. Rendering it
# in degrees needs one sensitivity: at still air and 50 %RH this room's own
# `pmv_fanger` moves 0.461 PMV to 0.133 PMV between 24 °C and 25 °C, so a degree
# is worth about a third of a PMV point. `test_dashboard.py` pins this against
# `comfort.pmv_fanger` rather than trusting the textbook figure.
#
# The degrees are DERIVED from the PMV constant, never typed beside it. Retuning
# the category to A or C in `horizon.py` therefore moves the number the occupant
# reads with it, instead of leaving a stale 1.5 on the page.
PMV_PER_DEGREE_C = 0.33
COMFORT_BAND_C = round(ISO_CATEGORY_B_PMV / PMV_PER_DEGREE_C, 1)
COMFORT_BAND_LABEL = (
    f"ISO 7730 Category B · PMV ±{ISO_CATEGORY_B_PMV} · "
    f"PPD ≤ {ISO_CATEGORY_B_PPD:.0f}%")
# Enough rows to show that declines are the common case without turning the card
# into a log viewer. The board writes one row per 30 s tick.
PLAN_DECISION_LIMIT = 12
# Which coefficients the projection is standing on. A projection running on
# engineering estimates must not read like one identified from this room, so the
# sentence says which it is in words rather than leaving a token to interpret.
PLAN_BASIS_WORDS = {
    "fitted": "Fitted to this room",
    "mixed": "Partly fitted to this room",
    "prior": "Engineering estimates",
    "unavailable": "Unavailable",
}
PLAN_BASIS_DETAIL = {
    "fitted": ("Envelope coefficients were identified from this room's own "
               "logged history."),
    "mixed": ("Some envelope coefficients were identified from this room's own "
              "history; the rest are still engineering estimates."),
    "prior": ("Engineering estimates only. This room's history has not yet "
              "identified coefficients of its own."),
    "unavailable": "No projection coefficients are stored on this board yet.",
}
# One method note for the whole surface, because a caveat repeated on every card
# stops being read. It also states the boundary the Energy tab depends on:
# nothing projected here is counted as metered anywhere.
PLAN_METHOD_NOTE = (
    "Each tick the board rolls the room forward over the horizon under every "
    "rung the comfort ladder would consider, prices the electricity, and keeps "
    "the cheapest rung that never leaves the band on the way. It may spend "
    "shade, ventilation and the fan; it may never start the compressor, which "
    "still answers to a measured room. Watt-hours on this tab are projected "
    "from those models. Metered energy is reported separately on Energy.")


def _now() -> float:
    return time.time()


def _age(at) -> float | None:
    try:
        return round(max(0.0, _now() - float(at)), 1)
    except (TypeError, ValueError):
        return None


def _read_line(path: Path) -> str | None:
    """First line of a sysfs attribute, or None. Reading sysfs races removal:
    the file can vanish between glob and read when a device drops off the bus,
    which is the very condition this is used to detect."""
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _json_value(value):
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _comfort_state(pmv) -> str:
    if pmv is None:
        return "unknown"
    try:
        value = float(pmv)
    except (TypeError, ValueError):
        return "unknown"
    if abs(value) <= 0.5:
        return "comfortable"
    if value > 1.0:
        return "hot"
    if value > 0.5:
        return "warm"
    if value < -1.0:
        return "cold"
    return "cool"


def _action(row: dict) -> str:
    active = []
    if row.get("blinds_shut"):
        active.append("shade")
    if row.get("window"):
        active.append("ventilate")
    if row.get("fan"):
        active.append(f"fan {row['fan']}")
    if row.get("ac"):
        active.append("AC")
    if row.get("light"):
        active.append("light")
    return " + ".join(active) if active else "holding, all devices off"


def _user_action(row: dict, ac_on: bool, contradicting: tuple = ()) -> str:
    """Plain-language version of _action for the user view.

    The ladder's own wording, including its policy fan levels, stays on the
    engineering view where the numbers can be read against the ladder.

    `contradicting` names the devices whose own readback disagrees with what
    automation asked for. The overview used to read "maintaining room
    conditions" while the Devices tab showed the fan reporting 6/6 against a
    desired 0, and the first screen is the wrong place to be reassuring about
    a room that is not doing what was asked.
    """
    active = []
    if ac_on:
        active.append("AC cooling")
    if row.get("blinds_shut"):
        active.append("blinds shaded")
    if row.get("window"):
        active.append("window venting")
    if row.get("fan"):
        active.append("fan running")
    if row.get("light"):
        active.append("light on")
    doing = " + ".join(active) if active else "maintaining room conditions"
    if not contradicting:
        return doing
    return f"{doing} — {' and '.join(contradicting)} not following automation"


def _device_disagreement(device: dict) -> str | None:
    """A fresh independent readback that contradicts automation, in one line.

    Only a reading young enough to still describe the room counts: a stale
    readback is a missing one, and raising a fault on it would cry wolf every
    time a vendor cloud went quiet.

    Two scales meet on the fan — the ladder plans levels 0-3 while the fan
    reports its own 0-6 speed — so only the running/not-running sense of it is
    comparable here, and the reported number is quoted rather than matched.
    A switch is compared exactly.
    """
    reported = device.get("reported") or {}
    if not reported.get("available"):
        return None
    age = reported.get("age_s")
    if age is None or age > DEVICE_STATE_FRESH_S:
        return None
    state = reported.get("state")
    if not isinstance(state, dict):
        return None
    label, desired = device.get("label") or device.get("key"), device.get("desired")
    speed = state.get("speed")
    if device.get("kind") == "fan" and isinstance(speed, int):
        wants = bool(desired)
        if bool(speed) == wants:
            return None
        return (f"{label} reports speed {speed} while automation wants "
                f"{desired if desired else 0}")
    power = state.get("power")
    if isinstance(power, bool) and isinstance(desired, bool) and power != desired:
        return (f"{label} reports {'on' if power else 'off'} while automation "
                f"wants it {'on' if desired else 'off'}")
    return None


def _user_reason(comfort_state: str, control_mode: str) -> str:
    """One human sentence in place of the ladder's engineering reason."""
    room = {
        "comfortable": "The room is comfortable",
        "warm": "The room is a little warm",
        "hot": "The room is warmer than the comfort band",
        "cool": "The room is a little cool",
        "cold": "The room is colder than the comfort band",
    }.get(comfort_state, "Comfort cannot be measured right now")
    doing = {
        "manual": "your manual request is in control until it expires",
        "automatic": "BreezeIQ is adjusting the room for you",
    }.get(control_mode, "BreezeIQ is watching the room without changing devices")
    return f"{room}, and {doing}."


# ── what the system actually did ────────────────────────────────────────
# `act_command` has journalled every command, its actor, its reason and how it
# ended since the journal existed, and neither page read it: Room showed one
# automation line with no outcome, the Workbench showed nothing at all. So
# "what did it just do, and did it work" was answerable only in SQLite.
#
# `outcome` is the journal's own vocabulary. Printing `quota_hold` at a person
# is not an explanation and printing nothing is what happened before, so each
# word maps to a state plus why it ended that way. An outcome this map does not
# know is shown as itself rather than swallowed, and an absent one reads as
# unavailable — never as a command that succeeded.
ACTION_OUTCOMES = {
    # One word per cause. Five of these used to print "not sent", which is the
    # same conflation that made the fan card say "unreachable" for four
    # different things: a word covering everything explains nothing.
    "acknowledged": ("done", "the device confirmed it"),
    "failed": ("failed", "the device did not carry it out"),
    "skipped": ("not sent", "nothing was sent to the device"),
    "rejected": ("refused", "the request was not valid for this device"),
    "blocked": ("held back", "a safety rule stopped it"),
    "safety_hold": ("held back", "a safety rule stopped it"),
    "manual_override": ("your hold", "your manual hold owns this device"),
    "quota_hold": ("out of commands", "today's fan commands are already used up"),
    "rate_limited": ("too soon", "commands were arriving too fast"),
    "retry_backoff": ("waiting to retry", "the last attempt failed; it backs off before trying again"),
    "dry_run": ("not live", "this board is set to decide without commanding devices"),
    "journal_unavailable": ("unrecorded", "the command journal was unavailable"),
}
UNRECORDED_OUTCOME = ("unavailable", "the journal did not record how it ended")
# The board's two L298 motor channels. Neither moves anything in the room any
# more — the blinds module was taken off the rig and D9's motor drives nothing —
# so neither is a device to offer an occupant, and neither is something to
# report as having happened in their room. Both stay on the Workbench, which is
# the as-built record of what is physically wired. They are also the only covers
# here, so for them "on" means driven open rather than switched on.
MOTOR_DEVICE_KEYS = ("blinds", "window")
# Said in place of a blinds control, because a closed window is the part of that
# job a person can still do and the room genuinely depends on.
WINDOW_INSTRUCTION = (
    "Please keep the window closed while BreezeIQ is cooling the room. "
    "The motorised blind has been removed from this room, so shading and "
    "draught are yours to manage.")


def _room_vetoable(preferences: dict) -> list:
    """The devices an occupant may forbid, without the retired motor channels.

    `VETOABLE` stays exactly as the control loop spells it — the loop still
    honours a stored `allow_blinds` and the Workbench still shows it. This is
    only the shorter list the room is offered.
    """
    return [key for key in preferences["options"]["vetoable"]
            if key not in MOTOR_DEVICE_KEYS]


def _action_phrase(action, requested, *, cover: bool) -> str:
    """The command as a person would say it, from what was recorded."""
    values = requested if isinstance(requested, dict) else {}
    if "speed" in values:
        speed = values["speed"]
        return "switched off" if not speed else f"set to speed {speed}"
    if "percent" in values:
        return f"driven to {values['percent']}%"
    if "command" in values:                  # bench verbs, engineering only
        return f"bench {values['command']}"
    if "power" in values:
        if cover:
            return "driven open" if values["power"] else "driven shut"
        return "switched on" if values["power"] else "switched off"
    return str(action or "action not recorded")


# The ladder's own lines, said the way an occupant would. A WHITELIST, not a
# substitution pass: an unrecognised reason falls back to the generic sentence
# rather than leaking "PMV" or "withdraw: fan -> 2" onto a public surface. The
# alternative — collapsing every automatic move to one sentence — made the
# room's history read as though it only ever did one thing for one reason.
_OCCUPANT_REASONS = (
    # Ordered most specific first, and matched on the ladder's own substrings
    # rather than loose words: "vent" alone would also match "event", and
    # "cap" would match "capacity". Taken from what the board has actually
    # written (the four most common account for ~7k rows), not from guessing
    # at the format strings.
    ("asleep", "somebody is asleep"),
    ("pre-cool", "getting ahead of you coming home"),
    ("eco band", "nobody home, so the room is allowed to drift"),
    ("away", "nobody home, so the room is allowed to drift"),
    ("adopted the room as found", "picking up from how the room already was"),
    ("comfortable for", "it has been comfortable for a while"),
    ("holds comfort", "holding the room where you like it, the cheapest way it found"),
    ("harvest sun", "letting the sun back in"),
    ("vent while", "letting in air while it is cooler outside"),
    ("shade before", "shading the window before it gets uncomfortable"),
    ("capped at", "holding the fan at its limit"),
    ("climb:", "reaching for a little more"),
    ("withdraw:", "easing off, the room is holding"),
)
GENERIC_AUTOMATIC_REASON = "BreezeIQ was keeping the room comfortable"


def _occupant_reason(actor: str, reason) -> str | None:
    """Why the room did it, without the ladder's vocabulary.

    A reason typed by a person is theirs, and is quoted exactly as written. An
    automatic move's recorded reason is the ladder's own line ("withdraw: fan
    -> 2"), so it is translated where we recognise it and generalised where we
    do not.
    """
    text = str(reason or "").strip()
    if actor != "automation":
        return text or None
    lowered = text.lower()
    for token, plain in _OCCUPANT_REASONS:
        if token in lowered:
            return plain
    return GENERIC_AUTOMATIC_REASON


def _action_history(commands: list, *, public: bool) -> list:
    """The actuation journal as a readable history, newest first.

    Both surfaces get the same events and only the vocabulary differs, so the
    room and the bench cannot tell two stories about one command. Nothing is
    filled in: a command whose outcome was never journalled says so, and a row
    with no timestamp keeps a null age rather than reading as "just now".
    """
    history = []
    for row in commands:
        device = row.get("device")
        key = "light" if device == "tubelight" else device
        if public and key in MOTOR_DEVICE_KEYS:
            continue
        outcome = str(row.get("outcome") or "")
        state, ended = ACTION_OUTCOMES.get(
            outcome, (outcome, "") if outcome else UNRECORDED_OUTCOME)
        actor = str(row.get("actor") or "")
        entry = {
            "at": row.get("at"), "age_s": row.get("age_s"), "device": key,
            "what": _action_phrase(row.get("action"), row.get("requested"),
                                   cover=key in MOTOR_DEVICE_KEYS),
            "why": (_occupant_reason(actor, row.get("reason")) if public
                    else (str(row.get("reason") or "").strip() or None)),
            "outcome": state, "outcome_detail": ended or None,
            # The bench keeps the journal's own word for who asked; the room is
            # told the product's name rather than the code's.
            "actor": (("BreezeIQ" if actor == "automation" else actor)
                      if public else actor) or None,
        }
        if not public:
            entry["acknowledged"] = row.get("acknowledged")
            entry["detail"] = str(row.get("detail") or "").strip() or None
            entry["command_id"] = row.get("command_id")
        history.append(entry)
    # One blocked thing, said once. A fan whose budget is gone is refused every
    # tick, so the room's history filled with the same row fifteen times over —
    # which is the same noise the suppressed retries made, wearing a different
    # outcome. Consecutive identical entries collapse for the occupant and carry
    # a count; the bench keeps every row, because "how often" is the engineering
    # question.
    if public:
        collapsed = []
        for entry in history:
            same = (collapsed and collapsed[-1]["device"] == entry["device"]
                    and collapsed[-1]["what"] == entry["what"]
                    and collapsed[-1]["outcome"] == entry["outcome"])
            if same:
                collapsed[-1]["repeated"] = collapsed[-1].get("repeated", 1) + 1
                continue
            collapsed.append(entry)
        history = collapsed
    return history


# ── the fan's daily vendor budget ───────────────────────────────────────
# The ceiling fan is the only device behind a metered cloud API, and the backend
# keeps a persistent ledger so a restart cannot spend the cap twice. Nothing
# published it, so a fan the board had simply run out of calls for looked
# identical on the card to a fan that had fallen off the network — a different
# problem with a different answer for whoever is standing in the room.
def _fan_quota(health: list) -> dict:
    """Whether the fan's daily vendor budget is spent, and what to do about it.

    Read from the device monitor's own health row rather than from the ledger
    file: this process is a read model over what the control process recorded,
    and the ledger is the control process's to write.
    """
    result = {"available": False, "state": "unavailable", "spent": False,
              "calls_today": None, "budget": None, "remaining": None,
              "message": "The fan's daily command budget has not been "
                         "reported yet."}
    detail = next((item.get("detail") for item in health
                   if item.get("component") == "devices"), None)
    backend = (((detail or {}).get("backends") or {}).get("atomberg")
               if isinstance(detail, dict) else None)
    if not isinstance(backend, dict):
        return result
    if not backend.get("configured"):
        return {**result, "state": "unconfigured", "message":
                "The ceiling fan's account is not set up on this board, so "
                "BreezeIQ cannot switch the fan at all. Use its remote."}
    calls, budget = backend.get("calls_today"), backend.get("budget")
    if not isinstance(calls, int) or not isinstance(budget, int) or budget <= 0:
        # The ledger fails CLOSED on a counter it cannot read, so an unknown
        # count is not "plenty left" — it is a fan the board will refuse to
        # command. Saying "unavailable" here would read as a missing card.
        return {**result, "state": "unreadable", "message":
                "The count of today's fan commands could not be read, so "
                "BreezeIQ will not command the fan. Switch it by hand."}
    remaining = max(0, budget - calls)
    return {
        "available": True, "state": "spent" if not remaining else "ok",
        "spent": not remaining, "calls_today": calls, "budget": budget,
        "remaining": remaining,
        "message": (
            f"BreezeIQ has used all {budget} of today's fan commands and "
            "cannot switch the fan again until they reset at midnight. "
            "Please switch the fan off by hand if you want it off."
            if not remaining else
            f"{remaining} of {budget} fan commands left today."),
    }


# What each table holds, in the room's language rather than the schema's.
# Doubles as the guard that keeps internal names off the page: several tables are
# called ac_twin_*, and listing raw names would put the word this console is not
# allowed to show straight into the System tab. Anything unmapped falls through to
# its own name, so a new table is visible rather than silently dropped — and the
# served-bytes scan will catch it if the name is one we do not want shown.
STORAGE_LABELS = {
    "tick": "Decisions",
    "system_health": "Component health",
    "act_reported_state": "Device readback",
    "horizon_decision": "Plans, including declines",
    "ac_twin_sample": "AC response history",
    "ac_twin_state": "AC response, current",
    "ac_twin_control": "AC control requests",
    "ac_twin_event": "AC control events",
    "energy_sample": "Energy readings",
    "energy_baseline": "Verified savings baselines",
    "energy_meter_cursor": "Meter cursors",
    "act_command": "Commands sent",
    "act_manual_override": "Manual holds",
    "act_guard": "Safety guards",
    "event": "Occupancy events",
    "fault": "Faults",
    "backup_run": "Backup runs",
    "retention_run": "Retention runs",
    "envelope_fit": "Room physics fits",
    "occupancy_prior": "Occupancy pattern",
    "user_preference": "Your preferences",
}


def _plan_defaults() -> dict:
    """A plan surface with nothing projected yet.

    The band and the horizon are policy, not evidence, so they are populated
    here and stay populated: they are true before the first projection and they
    are true on a board whose database predates the planner. Every measured or
    projected quantity starts as None and renders as unavailable, because a
    projected zero would read as "this hour is free".
    """
    return {
        "available": False,
        "state": "unavailable",
        "acted": None,
        "action": None,
        "reason": None,
        "priced": False,
        "watt_hours": None,
        "worst_ppd": None,
        "cheaper_by_wh": None,
        "at": None,
        "age_s": None,
        "horizon_min": HORIZON_MINUTES,
        "band": COMFORT_BAND_LABEL,
        "band_pmv": ISO_CATEGORY_B_PMV,
        "band_ppd": ISO_CATEGORY_B_PPD,
        "band_c": COMFORT_BAND_C,
        "basis": "unavailable",
        "basis_label": PLAN_BASIS_WORDS["unavailable"],
        "basis_detail": PLAN_BASIS_DETAIL["unavailable"],
        "fit": None,
        "occupancy_prior": {
            "state": "unavailable", "confidence": None,
            "hours_known": None, "hours_total": None, "window_days": None,
            "days_observed": None,
            "detail": "Hour-of-day occupancy history is unavailable.",
        },
        "decisions": [],
        "message": PLAN_METHOD_NOTE,
    }


def _plan_action(chosen) -> str | None:
    """The rung a recorded decision left in force, in the overview's own words.

    Deliberately `_user_action`: the plan and the "active action" card describe
    the same devices, and two vocabularies for one room would read as two rooms.
    """
    if not isinstance(chosen, dict) or not chosen:
        return None
    return _user_action(chosen, bool(chosen.get("ac")))


def _decimate(rows: list, now: float,
              full_rate_s: float = HISTORY_FULL_RATE_S) -> list:
    """Full resolution for the recent window, every other sample before it.

    A day of 30 s ticks is 2,880 points drawn on a chart around 900 px wide —
    three samples per pixel — and shipping all of them cost 0.48 MB on the user
    payload and 1.26 MB on the engineering one. Thinning only the older part
    halves that while leaving the hours somebody actually scrubs untouched. A
    row with no readable timestamp is always kept: dropping a sample because
    its clock is unreadable would hide a gap rather than a duplicate.
    """
    cutoff = now - full_rate_s
    kept = []
    for index, row in enumerate(rows):
        try:
            recent = float(row.get("at")) >= cutoff
        except (TypeError, ValueError, AttributeError):
            recent = True
        if recent or index % 2 == 0:
            kept.append(row)
    return kept


def _people_by_sample(samples: list, ticks: list) -> list:
    return _tick_field_by_sample(samples, ticks, "people")


def _tick_field_by_sample(samples: list, ticks: list, field: str) -> list:
    """The value of one tick field in force when each model sample was taken.

    Model samples and sensor ticks are both written once per control tick, a
    fraction of a second apart, so they are joined by "the most recent tick at
    or before this sample" rather than by an equal timestamp. That is the count
    the board was actually acting on when the sample was recorded, and it stays
    honest if one of the two writers ever skips a tick.
    """
    counts, index, current = [], 0, None
    for sample in samples:
        try:
            at = float(sample.get("at"))
        except (TypeError, ValueError):
            counts.append(None)
            continue
        while index < len(ticks):
            try:
                tick_at = float(ticks[index].get("at"))
            except (TypeError, ValueError):
                index += 1
                continue
            if tick_at > at:
                break
            current = ticks[index].get(field)
            index += 1
        counts.append(current)
    return counts


def _extra_registry_devices(health, known: set[str]) -> tuple:
    """Registry devices this page has no built-in card for.

    The control loop persists the registry's own device list with each health
    sample, so a device added to the registry after this page shipped still
    gets a tile — named and typed by the registry rather than by a second list
    kept here, which would have to be edited every time the room grows.
    """
    seen, extras = set(known), []
    for item in health or []:
        if item.get("component") != "devices":
            continue
        detail = item.get("detail")
        entries = detail.get("devices", []) if isinstance(detail, dict) else []
        for entry in entries:
            key = entry.get("device") if isinstance(entry, dict) else None
            if (not isinstance(key, str) or key in seen
                    or not GENERIC_DEVICE_KEY.match(key)):
                continue
            seen.add(key)
            label = entry.get("label") or key.replace("_", " ").capitalize()
            extras.append((str(label)[:40], key,
                           str(entry.get("kind") or "switch"), None))
        break
    return tuple(extras)


def _stored_ttl(value) -> int | None:
    """Stored manual-hold default as minutes, or None for no expiry."""
    if value == UNTIL_AUTO:
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MANUAL_TTL_MIN
    return (minutes if 1 <= minutes <= MAX_MANUAL_TTL_MIN
            else DEFAULT_MANUAL_TTL_MIN)


def public_preferences(stored) -> dict:
    """Stored preferences plus their options, with unknown values ignored.

    A preference read must never fail: a value written by a newer build, or a
    row that is simply missing, falls back to the same default the control loop
    would use, so the page and the room never disagree about what was asked for.
    """
    stored = stored if isinstance(stored, dict) else {}
    light = stored.get(LIGHT_PREFERENCE_KEY)
    # The control loop has always read these; nothing could write them, so a
    # standing "never use this device" was reachable only by editing the table
    # by hand. Publishing them is what lets a page show the veto it obeys.
    allow = {f"{VETO_PREFIX}{device}": not _is_denied(
                stored.get(f"{VETO_PREFIX}{device}"))
             for device in VETOABLE}
    return {
        LIGHT_PREFERENCE_KEY: (light if light in LIGHT_PREFERENCES
                               else DEFAULT_LIGHT_PREFERENCE),
        MANUAL_TTL_PREFERENCE_KEY: _stored_ttl(
            stored.get(MANUAL_TTL_PREFERENCE_KEY, DEFAULT_MANUAL_TTL_MIN)),
        **allow,
        "options": {
            LIGHT_PREFERENCE_KEY: list(LIGHT_PREFERENCES),
            MANUAL_TTL_PREFERENCE_KEY: list(MANUAL_TTL_CHOICES),
            "vetoable": list(VETOABLE),
        },
    }


# ── how the AC card got its number ──────────────────────────────────────
# Nothing is wired to the AC switch, so every temperature on that card is
# arithmetic rather than observation and has to be auditable on sight. The model
# publishes an explanation of each tick, but it lives in the control process's
# memory and is deliberately not persisted, so this read model rebuilds it from
# the two rows that ARE persisted — by calling `digital_twin.advance`, the same
# pure function the tick itself ran. A second copy of the equations here would
# drift from the model and the breakdown would stop describing the number
# printed above it, so the replayed room value is checked against the stored one
# and the step is published only when the two agree.
TWIN_REPLAY_TOLERANCE_C = 0.01
# Ticks where the model re-anchored on the thermometer instead of stepping.
# There is no arithmetic to show for these and inventing one would be the
# opposite of the point.
TWIN_ANCHOR_STATUSES = {
    "initialized": "This was the model's first tick, so it started from the "
                   "thermometer instead of stepping.",
    "reset_after_gap": "The board stopped reporting for long enough that the "
                       "model restarted from the thermometer.",
    "resynced_to_sensor": "The model had drifted implausibly below the "
                          "setpoint with the AC off, so it was pulled back "
                          "onto the thermometer.",
    "sensor_hold": "The thermometer was unavailable this tick, so the model "
                   "held its last value rather than advancing.",
}


def _twin_breakdown(state: dict, previous: dict | None,
                    interval_wh) -> dict:
    """The inputs, the one step, and the result — each labelled for what it is."""
    measured = {"indoor_c": state.get("sensed_indoor_c"),
                "indoor_rh": state.get("sensed_indoor_rh"),
                "outdoor_c": state.get("sensed_outdoor_c")}
    modelled = {"room_c": state.get("modeled_c"),
                "room_rh": state.get("modeled_rh"),
                "status": state.get("status"),
                "cooling_effect": state.get("cooling_effect"),
                "estimated_watts": state.get("estimated_watts"),
                "interval_wh": interval_wh}
    result = {"measured": measured, "inputs": None, "step": None,
              "modelled": modelled, "detail": ""}
    try:
        config = TwinConfig.from_env()
    except Exception as exc:
        result["detail"] = (
            f"The model's configuration is unreadable ({type(exc).__name__}), "
            "so its working cannot be shown.")
        return result
    result["inputs"] = inputs = {
        "power": bool(state.get("power")), "setpoint_c": state.get("setpoint_c"),
        "mode": state.get("mode"), "ac_model": state.get("ac_model"),
        # Tonnage and kW alongside the stored identity. The read model REPLAYS
        # the tick through the same pure advance(), so it has to publish the
        # same fields the live explanation does or the card silently loses
        # them — which is exactly what happened: ac_label read null on the
        # board while the twin itself was producing it.
        "ac_label": config.profile.label,
        "ac_tons": round(config.profile.tons, 2),
        "dt_s": None,
        "cooling_rate_c_h": round(config.effective_cooling_rate_c_h, 3),
        "coast_rate_c_h": round(config.coast_rate_c_h, 3),
        "ua_w_per_c": round(config.ua_w_per_c, 2),
        "reachable_depth_c": round(config.reachable_depth_c, 2),
    }
    anchored = TWIN_ANCHOR_STATUSES.get(str(state.get("status") or ""))
    if anchored:
        result["detail"] = anchored
        return result
    from_c, room_c = (previous or {}).get("modeled_c"), state.get("modeled_c")
    baseline_c, setpoint_c = measured["indoor_c"], state.get("setpoint_c")
    if from_c is None or room_c is None or baseline_c is None or setpoint_c is None:
        result["detail"] = ("No step to show: the previous room value or the "
                            "thermometer was unavailable for this tick.")
        return result
    gap_s = (None if state.get("at") is None or previous.get("at") is None
             else float(state["at"]) - float(previous["at"]))
    if gap_s is None or gap_s <= 0:
        result["detail"] = ("No step to show: the two samples carry no usable "
                            "time between them.")
        return result
    inputs["dt_s"] = round(min(gap_s, config.max_step_s), 2)
    step = twin_advance(
        modeled_c=float(from_c),
        cooling_effect=float(previous.get("cooling_effect") or 0.0),
        previous_watts=float(previous.get("estimated_watts") or 0.0),
        baseline_c=float(baseline_c),
        outdoor_c=(None if measured["outdoor_c"] is None
                   else float(measured["outdoor_c"])),
        power=inputs["power"], setpoint_c=float(setpoint_c), dt_s=gap_s,
        config=config, profile=config.profile,
        modeled_rh=(previous.get("modeled_rh")
                    if previous.get("modeled_rh") is not None
                    else measured["indoor_rh"]),
        baseline_rh=measured["indoor_rh"])
    if abs(step.modeled_c - float(room_c)) > TWIN_REPLAY_TOLERANCE_C:
        # Almost always a configuration changed since the tick ran. Showing the
        # replay anyway would put arithmetic on the card that does not add up to
        # the number beside it, which is worse than showing none.
        result["detail"] = (
            f"This step cannot be replayed: the stored inputs now produce "
            f"{step.modeled_c:.2f} °C rather than the {float(room_c):.2f} °C "
            "recorded, so the model's settings changed after the tick ran.")
        return result
    result["step"] = {
        "from_c": round(float(from_c), 3),
        "drift_c_h": round(step.drift_c_h, 3),
        "cooling_c_h": round(step.cooling_c_h, 3),
        "net_rate_c_h": round(step.net_rate_c_h, 3),
        "applied_c": round(float(room_c) - float(from_c), 3),
        "floor_c": round(step.floor_c, 2),
    }
    result["detail"] = (
        "The envelope pulls the model toward the thermometer and the appliance "
        "pulls it toward the setpoint; the difference, over this step, is the "
        "change applied.")
    return result


# ── the occupant's temperature dial ─────────────────────────────────────
# Two rows carry one number. `ac_twin_control` is what the occupant ASKED for
# and owns it; `ac_twin_state.setpoint_c` is only the echo of what the model
# last actually stepped with. Publishing the echo as the dial is what made a set
# temperature revert: between a tap and the next board tick the echo still holds
# the previous number, so the page snapped back to whatever the model last ran —
# which is why no code default explained the value people saw.
def _governing_setpoint(requested, model_setpoint_c, generated: float) -> dict:
    """The temperature in force, and which rule is going to end it.

    `held` ends only when somebody deliberately changes it, `expires` ends at a
    stated time, and `expired`/`unset` mean automatic control has the number
    back. Nothing is invented: with no request and no stored value it is None.
    """
    stored = requested if isinstance(requested, dict) else None
    manual = bool(stored) and stored.get("mode") == "manual"
    expires_at = stored.get("expires_at") if manual else None
    expired = (expires_at is not None and float(expires_at) <= generated)
    echo = None if model_setpoint_c is None else float(model_setpoint_c)
    if manual and not expired:
        value, remaining = float(stored["setpoint_c"]), None
        if expires_at is None:
            governed_by = "held"
            detail = f"Held at {value:.1f} °C until you change it."
        else:
            governed_by = "expires"
            remaining = round(max(0.0, float(expires_at) - generated))
            detail = (f"Held at {value:.1f} °C for another "
                      f"{max(1, round(remaining / 60))} min, then automatic "
                      "control resumes.")
        source = "occupant"
    else:
        value, remaining = echo, None
        governed_by = "expired" if expired else "unset"
        source = None if value is None else "automatic"
        if value is None:
            detail = ("The hold you set has expired and no temperature is in "
                      "force." if expired else
                      "No temperature has been set on this board yet.")
        else:
            aiming = f"automatic control is aiming at {value:.1f} °C"
            detail = (f"The hold you set has expired, so {aiming}." if expired
                      else f"Nobody has set a temperature, so {aiming}.")
    return {
        "setpoint_c": value, "source": source, "governed_by": governed_by,
        "expires_at": expires_at, "remaining_s": remaining,
        # The value a write must be based on, so a device holding an older read
        # cannot put its number back over a newer one.
        "revision": float(stored["updated_at"]) if stored else None,
        "applied": (value is not None and echo is not None
                    and abs(echo - value) < 0.05),
        "detail": detail,
    }


class DashboardStore:
    """Read-only SQLite projection used by every dashboard tab."""

    def __init__(self, path: str | None = None):
        self.path = Path(path or os.environ.get("BREEZEIQ_DB", "telemetry.sqlite3"))

    def _connect(self):
        if not self.path.exists():
            return None
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=1.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("PRAGMA busy_timeout=1000")
        return con

    @staticmethod
    def _tables(con) -> set[str]:
        return {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    @staticmethod
    def _columns(con, table: str) -> set[str]:
        return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _rows(con, sql: str, params=()) -> list[dict]:
        return [dict(row) for row in con.execute(sql, params).fetchall()]

    @staticmethod
    def _latest_by(rows: list[dict], key: str) -> dict[str, dict]:
        result = {}
        for row in rows:
            name = row.get(key)
            if name not in result:
                result[name] = row
        return result

    def _camera_link(self) -> dict:
        """Whether the camera is on the USB bus right now, read from sysfs.

        The database cannot answer this. A camera that vanished mid-session
        leaves its last good count behind, so "quiet room" and "device gone"
        look identical in telemetry — which is exactly how a bus dropout stayed
        invisible for a whole session.

        devnum is reported because it increments on every re-enumeration, so a
        number far above the rest of the bus suggests a device that keeps
        browning out and recovering. Internal video nodes have no USB parent and
        never set it, which is what separates "a camera is attached" from
        "video4linux exists".

        `downstream` is the fact that actually discriminates, and getting here
        took three wrong candidates — all of them plausible, all of them read off
        a board that was working at the time:

          usb_vbus       `disabled` with the camera counting normally. A powered
                         hub supplies its own VBUS, so the board never enables
                         its own and the value is the same whether the port is
                         busy or empty.
          port0-partner  ABSENT with the camera counting normally. The camera
                         reaches the SoC through the xHCI controller and a
                         powered hub, which registers no Type-C partner at all,
                         so "no partner" describes the healthy board too.
          root hub       present with `port0/data_role` reading `[device]`.
                         `usb1` and `usb2` are the xHCI controllers themselves;
                         they exist regardless of what the Type-C port is doing.

        Each of those would have printed a confident story about a cable on a
        board whose cable was fine. What actually changes when the camera goes
        away is what is ENUMERATED below the root hubs, so that is what is
        reported: `1-1` the hub, `1-1.2` the camera. Nothing below the hubs means
        nothing is reaching the bus; a populated bus with no capture node means
        the camera, not the cable that carries five other working devices.

          nothing downstream  -> the cable, the socket, or an unpowered hub
          devices, no camera  -> the camera itself or its own lead into the hub
        """
        downstream = sorted(p.name for p in USB_DEVICES.glob("*")
                            if _is_downstream(p.name))
        link = {"downstream": downstream}
        base = VIDEO4LINUX
        if not base.is_dir():
            return {"present": False, "nodes": [], **link,
                    "detail": "no video4linux subsystem on this host"}
        nodes, product, devnum = [], None, None
        for node in sorted(base.glob("video*")):
            nodes.append(node.name)
            if devnum is not None:
                continue
            device = node / "device"
            for _ in range(5):
                try:
                    device = device.resolve().parent
                except OSError:
                    break
                if (device / "devnum").is_file():
                    devnum = _read_line(device / "devnum")
                    product = _read_line(device / "product")
                    break
        present = devnum is not None
        return {
            "present": present, "nodes": nodes, **link,
            "product": product, "devnum": devnum,
            "detail": (f"{product or 'camera'} attached as USB device {devnum}"
                       if present else
                       "no camera enumerated on the USB bus"),
        }

    def _history(self, con, tick_columns: set[str], limit: int) -> list[dict]:
        wanted = [
            "at", "indoor_c", "outdoor_c", "indoor_rh", "pmv", "people",
            "people_confidence", "solar_index", "light_state", "valid", "fault",
            "light_raw", "lux", "radar_presence", "occupied", "air_raw",
        ]
        columns = [name for name in wanted if name in tick_columns]
        if not columns:
            return []
        rows = self._rows(
            con, f"SELECT {','.join(columns)} FROM tick ORDER BY at DESC LIMIT ?",
            (limit,),
        )
        rows.reverse()
        for row in rows:
            row["gap"] = not bool(row.get("valid", 1))
        return rows

    def read(self, history_limit: int = HISTORY_LIMIT, *, debug: bool = False) -> dict:
        generated = _now()
        con = None
        try:
            con = self._connect()
            if con is None:
                return self._empty("database file does not exist")
            tables = self._tables(con)
            if "tick" not in tables:
                return self._empty("telemetry table does not exist")
            tick_columns = self._columns(con, "tick")
            latest_row = con.execute("SELECT * FROM tick ORDER BY at DESC LIMIT 1").fetchone()
            if latest_row is None:
                return self._empty("control loop has not logged a sample")
            latest = dict(latest_row)
            rows = con.execute("SELECT COUNT(*) FROM tick").fetchone()[0]
            age_s = _age(latest.get("at"))
            valid = bool(latest.get("valid"))
            if age_s is not None and age_s > STALE_AFTER_S:
                data_state = "stale"
            elif not valid:
                data_state = "degraded"
            else:
                data_state = "live"

            commands = []
            if "act_command" in tables:
                commands = self._rows(
                    con, "SELECT * FROM act_command ORDER BY at DESC LIMIT 30")
                for item in commands:
                    item["requested"] = _json_value(item.pop("requested_json", None))
                    item["reported"] = _json_value(item.pop("reported_json", None))
                    item["age_s"] = _age(item.get("at"))

            reported = []
            if "act_reported_state" in tables:
                reported = self._rows(
                    con, "SELECT * FROM act_reported_state ORDER BY at DESC LIMIT 30")
                for item in reported:
                    item["state"] = _json_value(item.pop("state_json", None))
                    item["recorded_age_s"] = _age(item.get("at"))
                    item["age_s"] = _age(
                        item.get("evidence_at") or item.get("at"))

            overrides = []
            if "act_manual_override" in tables:
                overrides = self._rows(
                    con,
                    """SELECT override_id,device,action,value_json,operator,reason,
                              created_at,expires_at
                       FROM act_manual_override
                       WHERE cleared_at IS NULL AND expires_at>?
                       ORDER BY created_at DESC""",
                    (generated,),
                )
                for item in overrides:
                    item["value"] = _json_value(item.pop("value_json", None))
                    item["remaining_s"] = round(max(0, item["expires_at"] - generated))

            open_faults = []
            if "fault" in tables:
                open_faults = self._rows(
                    con,
                    """SELECT at,component,code,severity,detail,status
                       FROM fault WHERE status='open' ORDER BY at DESC LIMIT 20""",
                )
                for item in open_faults:
                    item["age_s"] = _age(item.get("at"))
            if latest.get("fault"):
                open_faults.insert(0, {
                    "at": latest.get("at"), "component": "sensors",
                    "code": "latest_sample", "severity": "warning",
                    "detail": latest["fault"], "status": "open", "age_s": age_s,
                })

            health_rows = []
            if "system_health" in tables:
                health_rows = self._rows(
                    con, "SELECT * FROM system_health ORDER BY at DESC LIMIT 50")
                for item in health_rows:
                    item["detail"] = _json_value(item.pop("detail_json", None))
                    item["age_s"] = _age(item.get("at"))
            health = list(self._latest_by(health_rows, "component").values())

            backup = None
            if "backup_run" in tables:
                result = con.execute(
                    "SELECT * FROM backup_run ORDER BY scheduled_at DESC LIMIT 1"
                ).fetchone()
                backup = dict(result) if result else None
                if backup:
                    backup["age_s"] = _age(
                        backup.get("finished_at") or backup.get("scheduled_at"))

            energy = self._energy(con, tables, generated)
            digital_twin = self._digital_twin(con, tables, generated, history_limit)
            if digital_twin.get("estimated_24h_wh") is not None:
                energy["estimated_wh"] = digital_twin["estimated_24h_wh"]
                if energy.get("state") == "unavailable":
                    energy["state"] = "estimated"
                energy["message"] = (
                    "AC energy is estimated from the configured cooling response; "
                    "measured energy remains separate.")
            command_by = self._latest_by(commands, "device")
            reported_by = self._latest_by(reported, "device")
            override_by = self._latest_by(overrides, "device")
            devices = []
            ac_present = os.environ.get(
                "BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1"
            for display, key, kind, desired in (
                ("AC" if ac_present else "AC response", "ac", "switch",
                 bool(latest.get("ac"))),
                ("Fan", "fan", "fan", latest.get("fan")),
                ("Light", "tubelight", "light", bool(latest.get("light"))),
            ) + _extra_registry_devices(health, {"ac", "fan", "tubelight"}):
                command = command_by.get(key) or command_by.get(
                    "light" if key == "tubelight" else key)
                state = reported_by.get(key) or reported_by.get(
                    "light" if key == "tubelight" else key)
                override = override_by.get(key) or override_by.get(
                    "light" if key == "tubelight" else key)
                devices.append({
                    "key": "light" if key == "tubelight" else key,
                    "registry_key": key,
                    "label": display,
                    "kind": kind,
                    "desired": desired,
                    "physical_present": ac_present if key == "ac" else True,
                    "command": command,
                    "reported": state,
                    "override": override,
                })

            # A device whose own readback contradicts automation is a fault,
            # not a footnote on another tab. Without this the overview read
            # "maintaining room conditions · 0 open faults" while the fan
            # reported 6/6 against a desired 0 two tabs away.
            contradicting = []
            for item in devices:
                detail = _device_disagreement(item)
                if not detail:
                    continue
                contradicting.append(str(item.get("label") or item["key"]).lower())
                open_faults.insert(0, {
                    "at": (item.get("reported") or {}).get("at"),
                    "component": "devices", "code": "reported_state_disagrees",
                    "severity": "warning", "status": "open",
                    "detail": f"{detail} — check the vendor app or a wall switch",
                    "age_s": (item.get("reported") or {}).get("age_s"),
                })

            reading = {
                "source": latest.get("source") or "unknown",
                "sample_at": latest.get("at"), "age_s": age_s,
                "quality": "valid" if valid else "invalid",
            }
            occupancy = {
                "mode": latest.get("mode"),
                "count": latest.get("people"),
                "confidence": latest.get("people_confidence"),
                "occupant": latest.get("occupant"),
                "pir_motion": None if latest.get("occupied") is None
                else bool(latest.get("occupied")),
                "pir_last_motion_at": latest.get("pir_last_motion_at"),
                "camera_health": latest.get("camera_health") or "unknown",
                "camera_source": latest.get("camera_source") or "not reported",
                "camera_last_valid_at": latest.get("camera_last_valid_at"),
                "camera_fault": latest.get("camera_fault"),
                "fallback": ("PIR hold" if latest.get("people") is None
                             else "camera count fused with PIR"),
                "privacy": "Raw frames stay local and are not stored by BreezeIQ.",
                "reading": reading,
            }
            if (occupancy["occupant"] == "AWAY"
                    and occupancy["count"] is None
                    and occupancy["camera_health"] != "ok"):
                occupancy["occupant"] = "PRESENCE UNCERTAIN"
                occupancy["fallback"] = (
                    "PIR is quiet and camera evidence is unavailable; "
                    "automatic device states are held")
            automatic_live = (
                os.environ.get("BREEZEIQ_LIVE", "0") == "1"
                and os.environ.get("BREEZEIQ_SAFETY_VALIDATED", "0") == "1"
                and os.environ.get(
                    "BREEZEIQ_AUTOMATION_CONTROL_ENABLED", "0") == "1")
            comfort = {
                "state": _comfort_state(latest.get("pmv")),
                "pmv": latest.get("pmv"),
                "indoor_c": latest.get("indoor_c"),
                "indoor_raw_c": latest.get("indoor_raw_c"),
                "outdoor_raw_c": latest.get("outdoor_raw_c"),
                "indoor_rh": latest.get("indoor_rh"),
                "outdoor_c": latest.get("outdoor_c"),
                "outdoor_rh": latest.get("outdoor_rh"),
                "light_raw": latest.get("light_raw"),
                "lux": latest.get("lux"),
                "radar_presence": latest.get("radar_presence"),
                # Radar edge/hold and motor drive state: bring-up figures the
                # cards and Workbench's motor bench read to show what the board
                # is doing rather than what it was last asked to do.
                "radar_edges": latest.get("radar_edges"),
                "radar_held_s": latest.get("radar_held_s"),
                "motor_dir": latest.get("motor_dir"),
                "motor_speed": latest.get("motor_speed"),
                "motor_left_s": latest.get("motor_left_s"),
                "failsafe_active": latest.get("failsafe_active"),
                "failsafe_episodes": latest.get("failsafe_episodes"),
                "fw_build": latest.get("fw_build"),
                "ldr_min": latest.get("ldr_min"),
                "ldr_max": latest.get("ldr_max"),
                "lux_bus": latest.get("lux_bus"),
                "i2c_bus0": latest.get("i2c_bus0"),
                "i2c_bus1": latest.get("i2c_bus1"),
                "i2c_bus2": latest.get("i2c_bus2"),
                "lux_addr": latest.get("lux_addr"),
                "i2c_devices": latest.get("i2c_devices"),
                "sda_pullup": latest.get("sda_pullup"),
                "scl_pullup": latest.get("scl_pullup"),
                "sda_level": latest.get("sda_level"),
                "scl_level": latest.get("scl_level"),
                "solar_index": latest.get("solar_index"),
                "light_state": latest.get("light_state"),
                "light_confidence": latest.get("light_confidence"),
                "light_provenance": _json_value(latest.get("light_provenance")),
                "air_raw": latest.get("air_raw"),
                "reading": reading,
            }
            payload = {
                "schema_version": 1,
                "generated_at": generated,
                "mode": "live",
                "debug": bool(debug),
                "data_state": data_state,
                "stale_after_s": STALE_AFTER_S,
                "sample": {"at": latest.get("at"), "age_s": age_s,
                           "valid": valid, "source": latest.get("source")},
                "overview": {
                    "comfort": comfort["state"], "pmv": comfort["pmv"],
                    "occupancy": occupancy["occupant"] or "unknown",
                    "people": occupancy["count"], "action": _action(latest),
                    "reason": latest.get("reason") or "No reason logged",
                    "control_mode": ("manual" if overrides else
                                     "automatic" if automatic_live else
                                     "observe only"),
                    "fault_count": len(open_faults), "devices": devices,
                },
                "comfort": comfort, "occupancy": occupancy,
                "devices": devices, "commands": commands,
                "actions": _action_history(commands, public=not debug),
                "fan_quota": _fan_quota(health),
                "overrides": overrides, "faults": open_faults,
                "energy": energy, "plan": self._plan(con, tables),
                # The page draws one button per speed, so it needs the
                # scale rather than a literal of its own — a literal is
                # how it came to offer a 6 the fan does not have.
                "fan_speed_max": FAN_SPEED_MAX,
                "preferences": self._preferences(con, tables),
                "digital_twin": digital_twin,
                "history": self._history(con, tick_columns, history_limit),
                "system": {
                    "database": {"ok": True, "path": str(self.path),
                                 "rows": rows,
                                 "size_bytes": self.path.stat().st_size},
                    "storage": self._storage(con, tables),
                    "health": health, "backup": backup,
                    "host": _host_health(self.path),
                },
            }
            if debug:
                payload["camera_link"] = self._camera_link()
            twin = payload["digital_twin"]
            if twin.get("mode") == "manual":
                payload["overview"]["control_mode"] = "manual"
            raw_debug = {
                "indoor_c": latest.get("indoor_c"),
                "indoor_raw_c": latest.get("indoor_raw_c"),
                "outdoor_raw_c": latest.get("outdoor_raw_c"),
                "indoor_rh": latest.get("indoor_rh"),
                "outdoor_c": latest.get("outdoor_c"),
                "outdoor_rh": latest.get("outdoor_rh"),
                "light_raw": latest.get("light_raw"),
                "solar_index": latest.get("solar_index"),
                "air_raw": latest.get("air_raw"),
                "source": latest.get("source"), "at": latest.get("at"),
            }
            # Indoor temperature is the AC-adjusted room temperature on both
            # views.
            # Outdoor temperature and humidity are unmodelled physical
            # readings, so they are reported as measured everywhere.
            payload["comfort"]["indoor_c"] = (
                twin.get("modeled_c") if twin.get("available") else None)
            payload["comfort"]["reading"] = {
                "source": (twin.get("provenance") if debug
                           else "AC conditioning estimate"),
                "sample_at": twin.get("at"), "age_s": twin.get("age_s"),
                "quality": (("modeled" if debug else "estimated")
                            if twin.get("available") else "unavailable"),
            }
            if debug:
                payload["debug_evidence"] = raw_debug
            else:
                payload["ac"] = twin
                payload.pop("digital_twin", None)
                # A response-only AC stays out of the public physical-device
                # list. Once an HA switch is explicitly mapped, its independent
                # readback and guarded controls belong beside fan and light.
                if not ac_present:
                    payload["devices"] = [
                        item for item in payload["devices"]
                        if item.get("key") != "ac"]
                    payload["overview"]["devices"] = [
                        item for item in payload["overview"]["devices"]
                        if item.get("key") != "ac"]
                # The two motor channels drive nothing in this room any more, so
                # the occupant is offered no card and no veto for them — see
                # MOTOR_DEVICE_KEYS. The Workbench keeps both, wiring included.
                payload["devices"] = [
                    item for item in payload["devices"]
                    if item.get("key") not in MOTOR_DEVICE_KEYS]
                payload["overview"]["devices"] = payload["devices"]
                payload["preferences"]["options"]["vetoable"] = _room_vetoable(
                    payload["preferences"])
                payload["window_instruction"] = WINDOW_INSTRUCTION
                payload["overview"]["action"] = _user_action(
                    latest, bool(twin.get("power")), tuple(contradicting))
                payload["overview"]["reason"] = _user_reason(
                    comfort["state"], payload["overview"]["control_mode"])
                # Automatic commands carry the ladder's reason. Operator
                # reasons were typed by the person reading this page, so they
                # stay exactly as written.
                for item in payload["commands"]:
                    if item.get("actor") == "automation":
                        item["reason"] = "automatic comfort control"
                # Occupancy belongs on the public history: "the room was empty
                # from here to here" is what makes a night of temperature
                # readable, and stripping it left the chart unexplainable.
                samples = twin.get("history", [])
                # Three series on one clock, so the page can show what the room
                # did, what it would have done untouched, and how that felt.
                # `without_cooling_c` is the sensor's own figure: with no
                # conditioning running the two coincide, and every degree
                # between them is what the appliance actually bought.
                ticks = payload["history"]
                pmvs = _tick_field_by_sample(samples, ticks, "pmv")
                payload["history"] = [{
                    "at": item.get("at"), "indoor_c": item.get("modeled_c"),
                    "without_cooling_c": item.get("sensed_indoor_c"),
                    "outdoor_c": item.get("sensed_outdoor_c"),
                    "people": count, "pmv": pmv,
                    "gap": item.get("modeled_c") is None,
                } for item, count, pmv in zip(
                    samples, _people_by_sample(samples, ticks), pmvs)]
                twin["history"] = [{
                    "at": item.get("at"), "room_c": item.get("modeled_c"),
                    "power": item.get("power"), "setpoint_c": item.get("setpoint_c"),
                    "status": item.get("status"),
                } for item in twin.get("history", [])]
                twin["events"] = []
                twin["message"] = (
                    "Estimated temperature follows AC power, setpoint, thermal "
                    "inertia, and room conditions.")
                # The public payload speaks the appliance's own language: one
                # room temperature, no model inputs and no model vocabulary.
                twin["room_c"] = twin.pop("modeled_c", None)
                twin.pop("sensed_indoor_c", None)
                # Same convention for the moisture the coil removed: product
                # language on the public view, raw sensor inputs only on
                # /debug. A running AC dries the room, and the occupant should
                # see the humidity they are actually in.
                twin["room_rh"] = twin.pop("modeled_rh", None)
                twin.pop("sensed_indoor_rh", None)
                twin.pop("sensed_outdoor_c", None)
                twin.pop("provenance", None)
                # The step-by-step working is an engineering surface by
                # construction: it is the model's inputs and its arithmetic.
                twin.pop("explanation", None)
                requested = twin.get("requested_control")
                if requested:
                    twin["requested_control"] = {
                        key: requested.get(key) for key in (
                            "updated_at", "mode", "power", "setpoint_c",
                            "expires_at", "active", "remaining_s")}
                # The dial is the occupant's own number. The engineering view
                # keeps the model's input, which lags a tap by up to one board
                # tick — reading THAT back is what made a set temperature
                # revert. `setpoint.applied` says whether the room has it yet.
                twin["setpoint_c"] = twin["setpoint"]["setpoint_c"]
                # Raw ADC counts and fusion inputs are engineering evidence;
                # humidity is a plain room reading the user asked to see.
                for key in ("indoor_raw_c", "outdoor_raw_c",
                            "light_raw", "lux", "radar_presence", "lux_addr",
                            "i2c_devices", "sda_pullup", "scl_pullup",
                            "sda_level", "scl_level",
                            "solar_index", "light_provenance",
                            "radar_edges", "radar_held_s",
                            "motor_dir", "motor_speed", "motor_left_s",
                            "failsafe_active", "failsafe_episodes", "fw_build",
                            "ldr_min", "ldr_max", "lux_bus",
                            "i2c_bus0", "i2c_bus1", "i2c_bus2"):
                    payload["comfort"].pop(key, None)
                for row in payload["history"]:
                    for key in ("light_raw", "lux", "radar_presence",
                                "air_raw"):
                        row.pop(key, None)
                # Per-sample energy rows are meter evidence for /debug. The
                # user view reports the totals those rows add up to.
                payload["energy"].pop("samples", None)
                for item in payload["system"]["health"]:
                    if item.get("component") == "ac_digital_twin":
                        item["component"] = "ac_conditioning"
                    if item.get("component") == "ac_conditioning":
                        detail = item.get("detail") or {}
                        if isinstance(detail, dict):
                            detail.pop("provenance", None)
            # Both series feed one chart on one clock, so both are thinned the
            # same way or the AC bands would stop lining up with the room line.
            payload["history"] = _decimate(payload["history"], generated)
            twin["history"] = _decimate(twin.get("history") or [], generated)
            return payload
        except Exception as exc:
            return self._empty(f"{type(exc).__name__}: {exc}")
        finally:
            if con is not None:
                con.close()

    def _storage(self, con, tables: set[str]) -> dict:
        """Where the board's bytes actually go, largest first.

        A total is not an answer. This database was 18.8 MB and 59 % of it was one
        component logging "ok" on every tick — invisible in a size figure, obvious
        the moment the tables are ranked. The System tab is where somebody looks
        when the disk is filling, so it should show the thing they can act on.

        `dbstat` is a compile-time option and not guaranteed present, so the byte
        figures are best-effort and flagged; the row counts always work.
        """
        out = {"tables": [], "azure_bytes": None, "azure_snapshots": None,
               "bytes_known": False}
        sizes: dict = {}
        try:
            sizes = {name: int(total) for name, total in con.execute(
                "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
            out["bytes_known"] = True
        except sqlite3.Error:
            pass                       # dbstat unavailable; rows still count
        rows = []
        for table in sorted(tables):
            try:
                count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                continue
            rows.append({"table": STORAGE_LABELS.get(table, table),
                         "rows": int(count), "bytes": sizes.get(table)})
        # Bytes first, rows as the tie-break. An empty table still owns a page
        # or two, so on a young database byte-ranking alone floats empty tables
        # above ones actually filling up — which is backwards for a panel whose
        # job is "what is eating the disk".
        key = ((lambda r: ((r["bytes"] or 0), r["rows"])) if out["bytes_known"]
               else (lambda r: r["rows"]))
        out["tables"] = sorted(rows, key=key, reverse=True)[:10]

        if "backup_run" in tables:
            try:
                n, total = con.execute(
                    "SELECT COUNT(*), SUM(size_bytes) FROM backup_run "
                    "WHERE status='uploaded'").fetchone()
                out["azure_snapshots"] = int(n or 0)
                out["azure_bytes"] = int(total or 0)
            except sqlite3.Error:
                pass
        return out

    def _plan(self, con, tables: set[str]) -> dict:
        """The hour-ahead projection: what it chose, what it costs, what it declined.

        Guarded exactly the way `ac_twin_sample` is. A board whose database was
        written before the planner shipped has none of these three tables, and
        the surface then reports itself unavailable rather than failing the whole
        read or showing a projection of zero.

        Declines are NOT filtered out. "No rung holds Category B for the hour"
        is the system explaining itself, and a card that only listed the actions
        could not answer the question a reviewer actually asks, which is why
        nothing happened at three in the afternoon.
        """
        plan = _plan_defaults()
        plan["fit"] = self._envelope_fit(con, tables)
        plan["occupancy_prior"] = self._occupancy_prior(con, tables)
        basis = (plan["fit"] or {}).get("basis")
        if "horizon_decision" in tables:
            rows = self._rows(
                con, "SELECT * FROM horizon_decision ORDER BY at DESC LIMIT ?",
                (PLAN_DECISION_LIMIT,))
            plan["decisions"] = [{
                "at": row.get("at"),
                "age_s": _age(row.get("at")),
                "acted": bool(row.get("acted")),
                "reason": row.get("reason") or "No reason recorded",
                "action": _plan_action(_json_value(row.get("chosen_json"))),
                "watt_hours": row.get("watt_hours"),
                "worst_ppd": row.get("worst_ppd"),
                "cheaper_by_wh": row.get("cheaper_by_wh"),
            } for row in rows]
            if rows:
                latest, shown = rows[0], plan["decisions"][0]
                basis = latest.get("provenance") or basis
                plan.update({
                    "available": True,
                    # `available` means the planner RAN. Whether it costed
                    # anything is a separate fact: a decision to defer records
                    # no watt-hours and no worst case, so every consumer was
                    # left inferring "declined" from a pair of nulls. Say it.
                    "priced": (shown["watt_hours"] is not None
                               and shown["worst_ppd"] is not None),
                    "state": "acting" if shown["acted"] else "deferring",
                    "acted": shown["acted"],
                    "action": shown["action"],
                    "reason": shown["reason"],
                    "watt_hours": shown["watt_hours"],
                    "worst_ppd": shown["worst_ppd"],
                    "cheaper_by_wh": shown["cheaper_by_wh"],
                    "at": shown["at"], "age_s": shown["age_s"],
                    # The horizon the decision was actually projected over, not
                    # the one this build would use: an old row explaining an old
                    # decision has to keep its own terms.
                    "horizon_min": (latest.get("horizon_min")
                                    or plan["horizon_min"]),
                })
        # A value written by a newer build is still a basis, and reporting it as
        # unavailable would be the one wrong answer: the board does know what its
        # coefficients came from.
        if basis:
            plan["basis"] = basis
            plan["basis_label"] = PLAN_BASIS_WORDS.get(basis, str(basis))
            plan["basis_detail"] = PLAN_BASIS_DETAIL.get(
                basis, f"Projection coefficients are recorded as {basis}.")
        return plan

    def _envelope_fit(self, con, tables: set[str]) -> dict | None:
        """The newest accepted envelope fit, or None when the room has no fit."""
        if "envelope_fit" not in tables:
            return None
        rows = self._rows(
            con, "SELECT * FROM envelope_fit ORDER BY at DESC LIMIT 1")
        if not rows:
            return None
        row = rows[0]
        return {
            "at": row.get("at"), "age_s": _age(row.get("at")),
            "basis": row.get("provenance") or "prior",
            "rmse": row.get("rmse"), "baseline_rmse": row.get("baseline_rmse"),
            "sample_count": row.get("sample_count"),
        }

    def _occupancy_prior(self, con, tables: set[str]) -> dict:
        """How much of the day the occupancy prior has actually earned a number for.

        An hour below its sample floor is stored WITH its `insufficient` verdict
        and no mean at all, so the only honest thing to publish is the count of
        hours that cleared the floor. The aggregate confidence quotes the
        WEAKEST bucket that has one: an aggregate that quoted its best bucket
        would claim a prior the planner does not have.
        """
        prior = _plan_defaults()["occupancy_prior"]
        if "occupancy_prior" not in tables:
            return prior
        rows = self._rows(
            con,
            "SELECT hour,day_kind,mean_presence,n_days,confidence,window_days "
            "FROM occupancy_prior "
            "WHERE at=(SELECT MAX(at) FROM occupancy_prior)")
        if not rows:
            return prior
        ranking = ("insufficient", "low", "medium", "high")
        known = [row for row in rows
                 if row.get("confidence") != "insufficient"
                 and row.get("mean_presence") is not None]
        days = [int(row["n_days"]) for row in rows
                if row.get("n_days") is not None]
        windows = [int(row["window_days"]) for row in rows
                   if row.get("window_days") is not None]
        prior.update({
            "hours_known": len(known), "hours_total": len(rows),
            "days_observed": max(days) if days else None,
            "window_days": max(windows) if windows else None,
        })
        if not known:
            prior.update({
                "state": "insufficient", "confidence": "insufficient",
                "detail": ("Insufficient history: no hour of the day has "
                           "earned an occupancy figure yet, so none is shown."),
            })
            return prior
        weakest = min(known, key=lambda row: ranking.index(row["confidence"])
                      if row.get("confidence") in ranking else 0)
        remaining = len(rows) - len(known)
        earned = (f"{len(known)} of {len(rows)} hour buckets have earned a "
                  f"figure; {remaining} are still insufficient" if remaining
                  else f"All {len(rows)} hour buckets have earned a figure")
        prior.update({
            "state": "available",
            "confidence": weakest.get("confidence"),
            "detail": (
                f"{earned} · weakest is {weakest.get('confidence')} confidence"
                + (f" · {prior['days_observed']} days observed"
                   if prior["days_observed"] else "") + "."),
        })
        return prior

    def _preferences(self, con, tables: set[str]) -> dict:
        if "user_preference" not in tables:
            return public_preferences({})
        return public_preferences({
            row["name"]: row["value"] for row in
            self._rows(con, "SELECT name,value FROM user_preference")})

    def _energy(self, con, tables: set[str], generated: float) -> dict:
        result = {
            "state": "unavailable", "window_hours": 24,
            "measured_wh": None, "derived_wh": None, "estimated_wh": None,
            "coverage": None, "baseline_wh": None, "current_wh": None,
            "saved_wh": None, "savings": None, "baseline_name": None,
            "message": "No verified energy samples are stored yet.", "samples": [],
            "estimate": {"available": False},
        }
        ac_meter = (os.environ.get("BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1"
                    and bool(os.environ.get("HA_AC_POWER_ENTITY")
                             or os.environ.get("HA_AC_ENERGY_ENTITY")))
        light_meter = bool(os.environ.get("HA_LIGHT_POWER_ENTITY")
                           or os.environ.get("HA_LIGHT_ENERGY_ENTITY"))
        if not (ac_meter or light_meter):
            result["message"] = (
                "No physical power meter is installed. Estimated AC output is "
                "excluded from measured energy and savings.")
            return self._estimated_energy(con, tables, generated, result)
        if "energy_sample" not in tables:
            return self._estimated_energy(con, tables, generated, result)
        samples = self._rows(
            con,
            """SELECT at,interval_start,interval_end,device,watts,watt_hours,
                      provenance,source,quality,missing_reason FROM energy_sample
               WHERE at>=? ORDER BY at DESC LIMIT 120""",
            (generated - 86400,),
        )
        result["samples"] = samples
        if not samples:
            return self._estimated_energy(con, tables, generated, result)
        totals = {"measured": 0.0, "derived": 0.0, "estimated": 0.0}
        valued = 0
        for row in samples:
            if row.get("watt_hours") is not None:
                totals[row["provenance"]] += float(row["watt_hours"])
                valued += 1
        required = tuple(
            item.strip() for item in os.environ.get(
                "BREEZEIQ_ENERGY_REQUIRED_DEVICES", "ac,tubelight").split(",")
            if item.strip())
        interval_summary = summarize_intervals(
            samples, generated - 86400, generated, required)
        result.update({
            "state": "available" if valued else "gap",
            "measured_wh": round(totals["measured"], 2),
            "derived_wh": round(totals["derived"], 2),
            "estimated_wh": round(totals["estimated"], 2),
            "coverage": (interval_summary.get("coverage")
                         if interval_summary.get("ok") else None),
            "message": ("Measured, derived, and estimated rows stay separate."
                        if valued else "Energy rows contain gaps, not zero usage."),
        })

        if "energy_baseline" not in tables:
            return self._estimated_energy(con, tables, generated, result)
        baseline_columns = self._columns(con, "energy_baseline")
        needed = {"watt_hours", "coverage", "device_set_json", "duration_s",
                  "quality"}
        if not needed <= baseline_columns:
            result["message"] = "Stored baseline needs a board database migration."
            return self._estimated_energy(con, tables, generated, result)
        baseline_rows = self._rows(
            con,
            """SELECT name,window_start,window_end,occupancy_basis,watt_hours,
                      coverage,device_set_json,provenance,duration_s,quality
               FROM energy_baseline WHERE watt_hours IS NOT NULL
               ORDER BY created_at DESC LIMIT 1""",
        )
        baseline = baseline_rows[0] if baseline_rows else None
        if not baseline:
            return self._estimated_energy(con, tables, generated, result)
        try:
            baseline_devices = json.loads(baseline["device_set_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            baseline_devices = []
        duration = float(baseline.get("duration_s") or 0)
        baseline_wh = baseline.get("watt_hours")
        result.update({"baseline_wh": baseline_wh,
                       "baseline_name": baseline.get("name")})
        if duration <= 0 or not baseline_devices or not baseline_wh:
            result["message"] = "Baseline metadata is incomplete; savings are unavailable."
            return self._estimated_energy(con, tables, generated, result)
        current_start = generated - duration
        if current_start < float(baseline["window_end"]):
            result["message"] = (
                "Collect a separate post-baseline window before claiming savings.")
            return self._estimated_energy(con, tables, generated, result)
        compare_rows = self._rows(
            con,
            """SELECT interval_start,interval_end,device,watt_hours,provenance,
                      quality,source,entity_id FROM energy_sample
               WHERE interval_start>=? AND interval_end<=? AND watt_hours IS NOT NULL
               ORDER BY device,interval_start""",
            (current_start, generated),
        )
        comparison = summarize_intervals(
            compare_rows, current_start, generated, baseline_devices)
        minimum = float(os.environ.get("BREEZEIQ_ENERGY_MIN_COVERAGE_PCT", "90"))
        if not comparison.get("ok"):
            result["message"] = comparison.get("error", "Energy comparison failed.")
            return self._estimated_energy(con, tables, generated, result)
        result["coverage"] = comparison["coverage"]
        if (float(baseline.get("coverage") or 0) < minimum
                or comparison["coverage"] < minimum):
            result["message"] = (
                f"Savings need {minimum:.0f}% coverage for every required device.")
            return self._estimated_energy(con, tables, generated, result)
        current_wh = comparison["watt_hours"]
        saved_wh = float(baseline_wh) - current_wh
        result.update({
            "current_wh": round(current_wh, 2),
            "saved_wh": round(saved_wh, 2),
            "savings": round(saved_wh / float(baseline_wh) * 100.0, 1),
            "message": (f"Compared with verified baseline {baseline['name']}; "
                        "estimated energy is excluded."),
        })
        return self._estimated_energy(con, tables, generated, result)

    def _estimated_energy(self, con, tables: set[str], generated: float,
                          result: dict) -> dict:
        if not {"tick", "ac_twin_sample"} <= tables:
            return result
        start = generated - 86400
        ticks = self._rows(
            con,
            "SELECT at,valid,occupant,pmv,fan,light FROM tick "
            "WHERE at>=? ORDER BY at", (start,))
        samples = self._rows(
            con,
            "SELECT at,modeled_c,power,interval_wh,status FROM ac_twin_sample "
            "WHERE at>=? ORDER BY at", (start,))
        result["estimate"] = estimate_ac_first_savings(ticks, samples)
        return result

    def _empty(self, reason: str) -> dict:
        preferences = public_preferences({})
        preferences["options"]["vetoable"] = _room_vetoable(preferences)
        return {
            "schema_version": 1, "generated_at": _now(), "mode": "live",
            "debug": False,
            "data_state": "failed", "stale_after_s": STALE_AFTER_S,
            "error": reason,
            "sample": None,
            "overview": {"comfort": "unknown", "pmv": None,
                         "occupancy": "unknown", "people": None,
                         "action": "No live decision", "reason": reason,
                         "control_mode": "unknown", "fault_count": 1,
                         "devices": []},
            "comfort": {}, "occupancy": {}, "devices": [], "commands": [],
            "actions": [], "fan_quota": _fan_quota([]),
            "window_instruction": WINDOW_INSTRUCTION,
            "overrides": [], "faults": [{"component": "database",
                                              "severity": "critical",
                                              "detail": reason}],
            "energy": {"state": "unavailable", "message": reason,
                       "samples": [], "savings": None},
            "plan": _plan_defaults(),
            "preferences": preferences,
            "ac": {"available": False, "status": "unavailable",
                   "message": reason, "history": [], "events": []},
            "history": [],
            "system": {"database": {"ok": False, "path": str(self.path),
                                       "error": reason},
                       "health": [], "backup": None,
                       "host": _host_health(self.path)},
        }

    def _digital_twin(self, con, tables: set[str], generated: float,
                      limit: int) -> dict:
        base = {
            "available": False, "status": "starting", "mode": "automatic",
            "power": False, "modeled_c": None, "setpoint_c": None,
            "cooling_effect": None, "estimated_watts": None,
            "estimated_24h_wh": None, "age_s": None,
            # Optional appliance identity and capacity advice. Absent until the
            # model records them, so both views must treat them as optional.
            "ac_model": None, "advisory": None,
            # Placeholder until the first model step writes a row of its own.
            # Taken from the model so the two cannot drift apart.
            "provenance": TWIN_PROVENANCE, "history": [], "events": [],
            "message": "The board is waiting for its first model step.",
            "setpoint": _governing_setpoint(None, None, generated),
        }
        if "ac_twin_state" not in tables:
            return base
        row = con.execute(
            "SELECT * FROM ac_twin_state WHERE id=1").fetchone()
        if row is None:
            return base
        state = dict(row)
        state["power"] = bool(state.get("power"))
        if (state.get("sensed_indoor_c") is not None
                and state.get("modeled_c") is not None):
            state["reduction_c"] = round(max(
                0.0, float(state["sensed_indoor_c"])
                - float(state["modeled_c"])), 2)
        else:
            state["reduction_c"] = None
        state["age_s"] = _age(state.get("at"))
        state["available"] = True
        state["message"] = (
            "AC-adjusted room temperature. Physical DHT22 evidence stays "
            "separate."
        )
        if "ac_twin_sample" in tables:
            history = self._rows(
                con,
                "SELECT at,modeled_c,cooling_effect,sensed_indoor_c,"
                "sensed_outdoor_c,power,setpoint_c,mode,status,estimated_watts,"
                "interval_wh,provenance FROM ac_twin_sample "
                "ORDER BY at DESC LIMIT ?", (limit,))
            history.reverse()
            for item in history:
                item["power"] = bool(item.get("power"))
            state["history"] = history
            energy = con.execute(
                "SELECT SUM(interval_wh) FROM ac_twin_sample "
                "WHERE at>=? AND power=1 AND interval_wh IS NOT NULL",
                (generated - 86400,)).fetchone()[0]
            state["estimated_24h_wh"] = (
                round(float(energy), 2) if energy is not None else None)
        else:
            state["history"] = []
            state["estimated_24h_wh"] = None
        if "ac_twin_event" in tables:
            events = self._rows(
                con, "SELECT at,action,value_json,operator,reason,provenance "
                "FROM ac_twin_event ORDER BY at DESC LIMIT 30")
            for item in events:
                item["value"] = _json_value(item.pop("value_json", None))
                item["age_s"] = _age(item.get("at"))
            state["events"] = events
        else:
            state["events"] = []
        requested = None
        if "ac_twin_control" in tables:
            control = con.execute(
                "SELECT updated_at,mode,power,setpoint_c,expires_at,operator,reason "
                "FROM ac_twin_control WHERE id=1").fetchone()
            requested = dict(control) if control else None
            if requested:
                requested["power"] = bool(requested.get("power"))
                requested["active"] = not (
                    requested.get("mode") == "manual"
                    and requested.get("expires_at") is not None
                    and float(requested["expires_at"]) <= generated)
                requested["remaining_s"] = (
                    round(max(0.0, float(requested["expires_at"]) - generated))
                    if requested.get("expires_at") is not None else None)
                state["pending_control"] = bool(
                    requested["active"] and float(requested["updated_at"])
                    > float(state.get("at") or 0))
            state["requested_control"] = requested
        state["setpoint"] = _governing_setpoint(
            requested, state.get("setpoint_c"), generated)
        previous, interval_wh = self._twin_previous_step(
            con, tables, state.get("at"))
        state["explanation"] = _twin_breakdown(state, previous, interval_wh)
        return {**base, **state}

    def _twin_previous_step(self, con, tables: set[str], at) -> tuple:
        """The sample before the current state, and the current one's interval.

        Its own query rather than the tail of `history`: the history projection
        selects a fixed column list that leaves out the humidity the replay
        needs, and it is truncated by the caller's history limit, which has
        nothing to do with whether a step can be shown. `interval_wh` lives only
        on the sample table, so the current row's own energy comes from here too.
        """
        if "ac_twin_sample" not in tables or at is None:
            return None, None
        previous = con.execute(
            "SELECT * FROM ac_twin_sample WHERE at<? ORDER BY at DESC LIMIT 1",
            (at,)).fetchone()
        current = con.execute(
            "SELECT interval_wh FROM ac_twin_sample WHERE at=? LIMIT 1",
            (at,)).fetchone()
        return (dict(previous) if previous else None,
                current["interval_wh"] if current else None)


def _tightest_mount(data_mount: str, data, root) -> dict:
    """Whichever filesystem is closest to full, named so it can be acted on.

    Ranked by percentage used rather than bytes free: the root filesystem on
    this board is small, so "most used" is what predicts the next failed write,
    and a free-bytes comparison would keep pointing at the roomy partition.
    """
    candidates = [(data_mount, data)]
    if root is not None and root.total and (
            root.total, root.free) != (data.total, data.free):
        candidates.append(("/", root))
    label, usage = max(
        candidates,
        key=lambda item: (item[1].total - item[1].free) / (item[1].total or 1))
    return {
        "constrained_mount": label,
        "constrained_free_bytes": usage.free,
        "constrained_total_bytes": usage.total,
        "constrained_used_pct": round(
            (usage.total - usage.free) / (usage.total or 1) * 100, 1),
    }


def _host_health(db_path: Path) -> dict:
    """Cheap board metrics with explicit unsupported states on non-Linux hosts.

    Both filesystems are reported, and the tighter of the two is named. The
    data partition is the one the database grows on, so it was the only one
    watched — while the root filesystem quietly reached 97% full and corrupted
    an image. A board with 17 GB free on one mount and 287 MB on the other is
    not a board with 17 GB free.
    """
    data_mount = db_path.parent if db_path.parent.exists() else Path(".")
    disk = shutil.disk_usage(data_mount)
    try:
        root = shutil.disk_usage("/")
    except OSError:
        root = None
    host = {
        "platform": sys.platform,
        "process_uptime_s": round(time.monotonic(), 1),
        "load_1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
        "data_mount": str(data_mount),
        "data_total_bytes": disk.total,
        "data_free_bytes": disk.free,
        "root_total_bytes": root.total if root else None,
        "root_free_bytes": root.free if root else None,
        # Kept under their original names for anything already reading them;
        # they have always meant the data partition.
        "storage_total_bytes": disk.total,
        "storage_free_bytes": disk.free,
        "linux_uptime_s": None, "memory_total_kb": None, "memory_available_kb": None,
    }
    host.update(_tightest_mount(str(data_mount), disk, root))
    try:
        host["linux_uptime_s"] = round(float(Path("/proc/uptime").read_text().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0])
        host["memory_total_kb"] = memory.get("MemTotal")
        host["memory_available_kb"] = memory.get("MemAvailable")
    except (OSError, ValueError, IndexError):
        pass
    return host


def _hardware_evidence(payload: dict) -> dict:
    """Project the one production Router/SQLite path into a wiring view."""
    sample = payload.get("sample") or {}
    comfort = payload.get("comfort") or {}
    raw = payload.get("debug_evidence") or {}
    occupancy = payload.get("occupancy") or {}
    state = payload.get("data_state", "failed")
    return {
        "live": state == "live",
        "data_state": state,
        "source": sample.get("source"),
        "age_s": sample.get("age_s"),
        "channels": {
            "indoor_dht22": {"pin": 7, "temperature_c": raw.get("indoor_c"),
                              "humidity_rh": raw.get("indoor_rh")},
            "outdoor_dht22": {"pin": 4, "temperature_c": raw.get("outdoor_c"),
                               "humidity_rh": raw.get("outdoor_rh")},
            "pir": {"pin": 8, "motion": occupancy.get("pir_motion"),
                    "last_motion_at": occupancy.get("pir_last_motion_at")},
            "ldr": {"pin": "A0", "raw": raw.get("light_raw"),
                    "solar_index": raw.get("solar_index")},
            "mq135": {"pin": "A1", "raw": raw.get("air_raw"),
                      "unit": "relative ADC only"},
            # address and bus population, not just the value: "no reading" has
            # two very different causes and they need different wires moved.
            "bh1750": {"bus": "SDA/SCL", "lux": comfort.get("lux"),
                       "address": comfort.get("lux_addr"),
                       "i2c_devices": comfort.get("i2c_devices"),
                       "sda_pullup": comfort.get("sda_pullup"),
                       "scl_pullup": comfort.get("scl_pullup"),
                       "sda_level": comfort.get("sda_level"),
                       "scl_level": comfort.get("scl_level")},
            "ld2410c": {"pin": 2, "presence": comfort.get("radar_presence")},
            "camera": {"source": occupancy.get("camera_source"),
                       "health": occupancy.get("camera_health"),
                       "count": occupancy.get("count"),
                       "confidence": occupancy.get("confidence")},
        },
    }


def _tap_reason(reason) -> tuple[str | None, str | None]:
    """A tap carries no typed explanation, so the tap itself is the reason.

    Every command still lands in the journal with a reason. Requiring a person
    to type one before their own light comes on was friction, not safety, and
    the sentence it produced was never read by anything.
    """
    if reason is None:
        return DEFAULT_TAP_REASON, None
    if not isinstance(reason, str) or len(reason.strip()) > 120:
        return None, "reason must be text of at most 120 characters"
    return reason.strip() or DEFAULT_TAP_REASON, None


def _tap_ttl(ttl) -> tuple[int | None, str | None]:
    """How long a manual state holds, or None for "until I return to automatic"."""
    if ttl is None:
        return None, None
    if (isinstance(ttl, bool) or not isinstance(ttl, int)
            or not 1 <= ttl <= MAX_MANUAL_TTL_MIN):
        return None, ("ttl_min must be an integer from 1 to "
                      f"{MAX_MANUAL_TTL_MIN}, or null")
    return ttl, None


class ManualCommandFacade:
    """Safety-checked adapter to the control process, never to appliances."""

    def __init__(self, db_path: str, socket_path: str | None = None):
        self.db_path = db_path
        self.socket_path = socket_path or os.environ.get(
            "BREEZEIQ_COMMAND_SOCKET", "/run/breezeiq/control.sock")
        self.enabled = os.environ.get("BREEZEIQ_MANUAL_CONTROL_ENABLED", "0") == "1"

    def capability(self) -> dict:
        socket_ready = Path(self.socket_path).is_socket()
        live_ready = (os.environ.get("BREEZEIQ_LIVE", "0") == "1"
                      and os.environ.get(
                          "BREEZEIQ_SAFETY_VALIDATED", "0") == "1")
        if not self.enabled:
            detail = "Manual command safety gate is disabled."
        elif not live_ready:
            detail = "Physical command safety gates are in observe-only mode."
        elif not socket_ready:
            detail = "Control-process command socket is unavailable."
        else:
            detail = "Board-local control process is ready."
        devices = ["fan", "light"]
        if os.environ.get("BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1":
            devices.insert(0, "ac")
        return {"enabled": self.enabled, "socket_ready": socket_ready,
                "live_ready": live_ready,
                "available": bool(self.enabled and live_ready and socket_ready),
                "detail": detail, "devices": devices,
                "ttl_min": 1, "ttl_max": MAX_MANUAL_TTL_MIN}

    @staticmethod
    def validate(body) -> tuple[dict | None, str | None]:
        if not isinstance(body, dict):
            return None, "JSON object required"
        # Confirmation stays a transport requirement, not a person's chore: the
        # page sends it with the tap that already expressed the intent.
        if body.get("confirm") is not True:
            return None, "explicit confirmation required"
        device = body.get("device")
        action = body.get("action")
        # Bench verbs travel the same audited path as everything else rather
        # than a side door, but they are only meaningful for the covers the
        # MCU drives, so they are allowed nowhere else.
        if action == "bench_motor":
            if not (isinstance(device, str) and GENERIC_DEVICE_KEY.match(device)):
                return None, "device must be a known device key"
            command = body.get("command")
            if command not in ("OPEN", "CLOSE", "STOP", "BRAKE"):
                return None, "command must be OPEN, CLOSE, STOP or BRAKE"
            speed, ms = body.get("speed"), body.get("ms")
            if speed is not None and (isinstance(speed, bool)
                                      or not isinstance(speed, int)
                                      or not 0 <= speed <= 100):
                return None, "speed must be an integer 0..100"
            if ms is not None and (isinstance(ms, bool)
                                   or not isinstance(ms, int)
                                   or not 1 <= ms <= 15000):
                return None, "ms must be an integer 1..15000"
            reason, error = _tap_reason(body.get("reason") or "bench motor test")
            if error:
                return None, error
            return {
                "request_id": uuid.uuid4().hex,
                "type": "manual_control", "device": device,
                "display_device": device, "action": "bench_motor",
                "command": command, "speed": speed, "ms": ms,
                "operator": "local-dashboard", "reason": reason,
                "confirm": True, "at": _now(),
            }, None
        if device in DEVICE_KEYS:
            registry_key = DEVICE_KEYS[device]
            allowed = ({"power", "auto"} if device in POWER_DEVICES
                       else {"power", "speed", "auto"})
        elif isinstance(device, str) and GENERIC_DEVICE_KEY.match(device):
            # A registry device with no card of its own here — a cover, say.
            # Speed belongs to appliances this page knows the scale of, so a
            # generic device gets on/off and auto, and the control process
            # refuses the key outright if the registry does not own it.
            registry_key, allowed = device, {"power", "auto"}
        else:
            return None, "device must be a known device key"
        if (device == "ac" and os.environ.get(
                "BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") != "1"):
            return None, "AC control unavailable: physical switch is absent"
        if action not in allowed:
            return None, f"action must be one of {', '.join(sorted(allowed))}"
        reason, error = _tap_reason(body.get("reason"))
        if error:
            return None, error
        ttl, error = _tap_ttl(body.get("ttl_min", DEFAULT_MANUAL_TTL_MIN))
        if error:
            return None, error
        value = body.get("value")
        if action == "power" and not isinstance(value, bool):
            return None, "power value must be true or false"
        if action == "speed" and (
            isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= FAN_SPEED_MAX
        ):
            return None, f"fan speed must be an integer from 0 to {FAN_SPEED_MAX}"
        return {
            "request_id": uuid.uuid4().hex,
            "type": "manual_control", "device": registry_key,
            "display_device": device, "action": action, "value": value,
            "operator": "local-dashboard", "reason": reason,
            "ttl_min": ttl, "confirm": True, "at": _now(),
        }, None

    def submit(self, body) -> tuple[dict, int]:
        request, error = self.validate(body)
        if error:
            return {"ok": False, "state": "rejected", "error": error}, 400
        capability = self.capability()
        if not capability["available"]:
            result = {
                "ok": False, "state": "blocked", "request_id": request["request_id"],
                "device": request["display_device"], "requested": request.get("value"),
                "acknowledged": False, "reported": None,
                "detail": capability["detail"],
            }
            self._audit_blocked(request, result["detail"])
            return result, 409
        try:
            payload = (json.dumps(request, separators=(",", ":")) + "\n").encode()
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(self.socket_path)
                client.sendall(payload)
                received = bytearray()
                while len(received) <= 65536:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    received.extend(chunk)
                    if b"\n" in chunk:
                        break
            response = json.loads(bytes(received).split(b"\n", 1)[0] or b"{}")
            if not isinstance(response, dict):
                raise ValueError("command sink response was not an object")
            response.setdefault("request_id", request["request_id"])
            response.setdefault("device", request["display_device"])
            response.setdefault("reported", None)
            acknowledged = response.get("acknowledged")
            if response.get("outcome") in {"safety_hold", "rate_limited", "manual_override"}:
                response["state"] = response["outcome"]
            elif response.get("reported") is not None:
                response["state"] = "reported"
            elif acknowledged is True:
                response["state"] = "acknowledged"
            elif response.get("outcome") in {"pending", "queued"}:
                response["state"] = "pending"
            else:
                response["state"] = "failed"
            response.setdefault("ok", response["state"] in {
                "reported", "acknowledged", "pending", "safety_hold", "rate_limited"
            })
            return response, 202 if response["state"] == "pending" else 200
        except Exception as exc:
            detail = f"Control-process command failed: {type(exc).__name__}"
            self._audit_blocked(request, detail)
            return {"ok": False, "state": "failed",
                    "request_id": request["request_id"], "device": request["display_device"],
                    "acknowledged": False, "reported": None, "detail": detail}, 502

    def _audit_blocked(self, request: dict, detail: str) -> None:
        try:
            from devices.journal import ActuationJournal
            journal = ActuationJournal(self.db_path)
            journal.record_command(
                command_id=request["request_id"], at=request["at"],
                device=request["device"], action=request["action"],
                requested={request["action"]: request.get("value")},
                actor=request["operator"], reason=request["reason"],
                override_id=None, acknowledged=False, reported=None,
                outcome="blocked", detail=detail,
            )
        except Exception:
            pass


class AcResponseFacade:
    """Controls only the persisted AC digital twin, never an appliance."""

    # Actions a linked physical AC owns. Its power state comes from its own
    # switch and Home Assistant readback, so writing it here would put a second
    # authority on the same appliance. Everything else is a stored preference.
    PHYSICAL_AC_ACTIONS = {"power", "auto"}

    def __init__(self, db_path: str):
        self.db_path = db_path

    def capability(self) -> dict:
        """What this endpoint can store, which is not the same as what it commands.

        The setpoint is a PREFERENCE, not an appliance command: the control
        loop reads it back through `requested_setpoint_c` and the ladder aims
        at it. Gating the whole endpoint on "no physical AC" therefore took the
        occupant's temperature dial away on exactly the board that has an AC to
        aim — the dial rendered nowhere and every POST came back 409. Power is
        the one action a physical switch genuinely owns; it stays blocked, and
        /api/manual remains the path to it.
        """
        physical_ac = os.environ.get(
            "BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1"
        return {
            "available": os.environ.get("BREEZEIQ_AC_TWIN_ENABLED", "1") == "1",
            "physical_actuation": False,
            "physical_ac": physical_ac,
            "actions": sorted({"power", "setpoint", "auto"}
                              - (self.PHYSICAL_AC_ACTIONS if physical_ac
                                 else set())),
            "detail": ("Physical AC is linked; its own switch owns power, and "
                       "the temperature you ask for is stored here."
                       if physical_ac else
                       "Controls the board-local estimated room response only."),
            "setpoint_min_c": 16, "setpoint_max_c": 30,
            "ttl_min": 1, "ttl_max": MAX_MANUAL_TTL_MIN,
        }

    @staticmethod
    def validate(body) -> tuple[dict | None, str | None]:
        if not isinstance(body, dict):
            return None, "JSON object required"
        if body.get("confirm") is not True:
            return None, "explicit confirmation required"
        action = body.get("action")
        if action not in {"power", "setpoint", "auto"}:
            return None, "action must be power, setpoint, or auto"
        reason, error = _tap_reason(body.get("reason"))
        if error:
            return None, error
        ttl, error = _tap_ttl(body.get("ttl_min", DEFAULT_MANUAL_TTL_MIN))
        if error:
            return None, error
        value = body.get("value")
        if action == "power" and not isinstance(value, bool):
            return None, "power value must be true or false"
        if action == "setpoint" and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not 16 <= float(value) <= 30):
            return None, "setpoint value must be a number from 16 to 30"
        if action == "auto" and value is not None:
            return None, "auto value must be null"
        request = {"action": action, "value": value, "reason": reason,
                   "ttl_min": ttl, "confirm": True}
        # A page derives the number it sends from the number it last read, so
        # two devices reading at different times would otherwise put an older
        # number back over a newer one. `revision` is the `updated_at` this
        # page read (null when it read no stored request); a mismatch means it
        # is writing from a state that has already been replaced. Omitting the
        # field is an unconditional write, which is what a script wants.
        if "revision" in body:
            revision = body["revision"]
            if revision is not None and (
                    isinstance(revision, bool)
                    or not isinstance(revision, (int, float))):
                return None, ("revision must be the number this page last "
                              "read, or null when it read none")
            request["revision"] = revision
        return request, None

    def submit(self, body) -> tuple[dict, int]:
        request, error = self.validate(body)
        if error:
            return {"ok": False, "state": "rejected", "error": error}, 400
        capability = self.capability()
        if not capability["available"]:
            return {"ok": False, "state": "blocked",
                    "error": capability["detail"]}, 409
        if request["action"] not in capability["actions"]:
            return {"ok": False, "state": "blocked", "error": (
                "Physical AC is linked; its own switch owns power. Use the "
                "device controls for it.")}, 409
        with Telemetry(self.db_path) as telemetry:
            state = telemetry.ac_twin_state() or {}
            stored = telemetry.ac_twin_control(active_only=False) or {}
            governing = _governing_setpoint(
                stored or None, state.get("setpoint_c"), _now())
            if ("revision" in request
                    and stored.get("updated_at") != request["revision"]):
                return {"ok": False, "state": "stale", "controller": "ac",
                        "error": ("This page was reading an older request. "
                                  "The stored one is shown instead."),
                        "setpoint": governing}, 409
            active = {} if governing["governed_by"] == "expired" else stored
            power = bool(active.get("power", state.get("power", False)))
            setpoint = float(active.get("setpoint_c", state.get(
                "setpoint_c", stored.get("setpoint_c", DEFAULT_SETPOINT_C))))
            action = request["action"]
            mode = "automatic" if action == "auto" else "manual"
            if action == "power":
                power = bool(request["value"])
            elif action == "setpoint":
                setpoint = float(request["value"])
            # No expiry is native here: a NULL expires_at is exactly how the
            # stored control row says "hold this until automatic is resumed".
            expires = None if (mode == "automatic"
                               or request["ttl_min"] is None) else (
                _now() + request["ttl_min"] * 60)
            ok = telemetry.set_ac_twin_control(
                mode=mode, power=power, setpoint_c=setpoint,
                expires_at=expires, operator="local-dashboard",
                reason=request["reason"], action=action)
            detail = telemetry.error
            # The revision the caller must quote on its NEXT write, so a person
            # tapping faster than the page polls is not told they are stale.
            written = (telemetry.ac_twin_control(active_only=False) or {}
                       ) if ok else {}
        if not ok:
            return {"ok": False, "state": "failed",
                    "error": detail or "AC response control was not stored"}, 503
        return {
            "ok": True, "state": "accepted", "controller": "ac",
            "mode": mode, "power": power, "setpoint_c": setpoint,
            "expires_at": expires, "physical_actuation": False,
            "setpoint": _governing_setpoint(
                written or None, state.get("setpoint_c"), _now()),
            "detail": "AC request stored. The next board tick updates the estimate.",
        }, 202


class PreferenceFacade:
    """Stores what the occupant asked for. Commands nothing.

    A preference is not an appliance command, so it needs no safety gate and
    stays available in observe-only mode: the control loop reads the stored row
    on its next tick and decides with it.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    @staticmethod
    def validate(body) -> tuple[dict | None, str | None]:
        if not isinstance(body, dict):
            return None, "JSON object required"
        name = body.get("name")
        value = body.get("value")
        if name == LIGHT_PREFERENCE_KEY:
            if value not in LIGHT_PREFERENCES:
                return None, (
                    f"{LIGHT_PREFERENCE_KEY} must be one of "
                    f"{', '.join(LIGHT_PREFERENCES)}")
            stored = value
        elif name == MANUAL_TTL_PREFERENCE_KEY:
            # null is "until I return to automatic"; it is stored as a word so
            # the meaning cannot be mistaken for a very patient timer.
            if value is None:
                stored = UNTIL_AUTO
            elif (not isinstance(value, bool) and isinstance(value, int)
                    and value in MANUAL_TTL_CHOICES):
                stored = str(value)
            else:
                return None, (
                    f"{MANUAL_TTL_PREFERENCE_KEY} must be null or one of "
                    f"{', '.join(str(item) for item in MANUAL_TTL_CHOICES)}")
        elif isinstance(name, str) and name.startswith(VETO_PREFIX) and (
                name[len(VETO_PREFIX):] in VETOABLE):
            # A standing permission, not a command: "never use the fan for me".
            # Stored as a word for the same reason the TTL is — the control loop
            # reads it as text and an unrecognised value must read as ALLOW, so
            # a typo can never be the reason a room cannot cool itself.
            if not isinstance(value, bool):
                return None, f"{name} must be true (allowed) or false (never)"
            stored = "yes" if value else "no"
        else:
            return None, "unknown preference"
        return {"name": name, "value": stored}, None

    def submit(self, body) -> tuple[dict, int]:
        request, error = self.validate(body)
        if error:
            return {"ok": False, "state": "rejected", "error": error}, 400
        with Telemetry(self.db_path) as telemetry:
            ok = telemetry.set_preference(request["name"], request["value"])
            detail = telemetry.error
            stored = telemetry.preferences() if ok else {}
        if not ok:
            return {"ok": False, "state": "failed",
                    "error": detail or "preference was not stored"}, 503
        return {"ok": True, "state": "saved", "name": request["name"],
                "preferences": public_preferences(stored),
                "detail": "Saved. The next board tick decides with it."}, 200


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    store_factory = DashboardStore
    command_factory = ManualCommandFacade
    ac_factory = AcResponseFacade
    preference_factory = PreferenceFacade
    # Bound below, once the page templates exist. Class attributes rather than
    # module globals so a second console can serve different HTML over this
    # exact API surface without forking the request handling.
    public_page = ""
    debug_page = ""
    log_page = ""

    # path -> (required intent header, the factory that owns the request)
    POST_ROUTES = {
        "/api/manual": ("manual-control", "command_factory"),
        "/api/ac": ("ac-conditioning-control", "ac_factory"),
        "/api/preferences": ("preference-update", "preference_factory"),
    }

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, separators=(",", ":"), default=str).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        # The camera preview is the one /api/ route MEANT to open in a browser
        # tab, so it is exempt from the html-accept bounce below. Without this,
        # clicking "Open live view" sends Accept: text/html and gets 303'd
        # straight back to the Room page instead of the frames.
        if (parsed.path.startswith("/api/")
                and not parsed.path.startswith("/api/camera/")
                and "text/html" in self.headers.get("Accept", "")):
            target = "/debug" if parsed.path.startswith("/api/debug/") else "/"
            self.send_response(303)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        store = self.store_factory()
        if parsed.path == "/":
            self._send(200, self.public_page.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/about":
            self._send(200, ABOUT_PAGE.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/log":
            self._send(200, self.log_page.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/debug":
            self._send(200, self.debug_page.encode(), "text/html; charset=utf-8")
        elif parsed.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif parsed.path == "/api/dashboard":
            self._json(store.read(debug=False))
        elif parsed.path == "/api/debug/dashboard":
            self._json(store.read(debug=True))
        elif parsed.path in {"/api/history", "/api/debug/history"}:
            query = parse_qs(parsed.query)
            try:
                limit = min(HISTORY_LIMIT,
                            max(10, int(query.get("limit", [HISTORY_LIMIT])[0])))
            except (TypeError, ValueError):
                limit = HISTORY_LIMIT
            debug = parsed.path == "/api/debug/history"
            payload = store.read(history_limit=limit, debug=debug)
            self._json({"generated_at": payload["generated_at"],
                        "data_state": payload["data_state"],
                        "history": payload["history"]})
        elif parsed.path == "/api/debug/hardware":
            self._json(_hardware_evidence(
                store.read(history_limit=10, debug=True)))
        elif parsed.path == "/api/hardware":
            self._json({"error": "not found"}, 404)
        elif parsed.path == "/api/manual":
            facade = self.command_factory(str(store.path))
            payload = store.read(history_limit=10)
            self._json({"capability": facade.capability(),
                        "overrides": payload.get("overrides", []),
                        "commands": payload.get("commands", [])[:10]})
        elif parsed.path == "/api/preferences":
            self._json({"preferences": store.read(
                history_limit=10).get("preferences", public_preferences({}))})
        elif parsed.path == "/api/ac":
            payload = store.read(history_limit=HISTORY_LIMIT)
            self._json({"capability": self.ac_factory(
                str(store.path)).capability(),
                "ac": payload.get("ac", {})})
        elif parsed.path == "/api/healthz":
            payload = store.read(history_limit=10)
            healthy = payload["data_state"] in {"live", "degraded", "stale"}
            self._json({"ok": healthy, "data_state": payload["data_state"],
                        "sample": payload.get("sample")}, 200 if healthy else 503)
        elif parsed.path == "/api/log/days":
            self._json({"days": self._log_days(str(store.path))})
        elif parsed.path == "/api/log":
            query = parse_qs(parsed.query)
            day = (query.get("day") or [""])[0]
            self._json(self._log_for_day(str(store.path), day))
        elif parsed.path == "/api/camera/preview.jpg":
            self._camera_still()
        elif parsed.path == "/api/camera/preview.mjpeg":
            self._camera_stream()
        else:
            self._json({"error": "not found"}, 404)

    # ── the full command log, by day ────────────────────────────────────
    # The room card shows the last few, collapsed. That is the right shape for a
    # glance and the wrong one for "what did it do on Tuesday": the journal holds
    # 23,889 rows over ten days on the live unit and none of it was reachable.
    # Two routes rather than one page-with-everything, so a day is a cheap query
    # instead of a scan of the table.
    LOG_DAY_LIMIT = 2000

    @staticmethod
    def _log_days(db_path: str) -> list:
        """Every day that has commands, newest first, with a count."""
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT date(at,'unixepoch','localtime') AS day, COUNT(*) AS n,"
                    " MIN(at) AS first_at, MAX(at) AS last_at"
                    " FROM act_command GROUP BY day ORDER BY day DESC").fetchall()
        except sqlite3.Error as exc:
            return [{"error": str(exc)}]
        return [{"day": r["day"], "commands": r["n"],
                 "first_at": r["first_at"], "last_at": r["last_at"]} for r in rows]

    def _log_for_day(self, db_path: str, day: str) -> dict:
        """One day's commands, oldest first so the day reads forwards.

        An unparseable day is refused rather than silently answered with
        everything — a log that quietly shows the wrong day is worse than one
        that says it cannot.
        """
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            return {"day": day, "error": "day must be YYYY-MM-DD",
                    "commands": [], "truncated": False}
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
                con.row_factory = sqlite3.Row
                rows = con.execute(
                    "SELECT * FROM act_command"
                    " WHERE date(at,'unixepoch','localtime') = ?"
                    " ORDER BY at ASC LIMIT ?",
                    (day, self.LOG_DAY_LIMIT + 1)).fetchall()
        except sqlite3.Error as exc:
            return {"day": day, "error": str(exc), "commands": [],
                    "truncated": False}
        truncated = len(rows) > self.LOG_DAY_LIMIT
        shown = []
        for row in rows[:self.LOG_DAY_LIMIT]:
            item = dict(row)
            # Same decode the live payload does. The journal stores these as
            # JSON text, and _action_history reads the decoded shape — handing
            # it raw rows would silently render "action not recorded" for every
            # command in the log.
            item["requested"] = _json_value(item.pop("requested_json", None))
            item["reported"] = _json_value(item.pop("reported_json", None))
            item["age_s"] = _age(item.get("at"))
            shown.append(item)
        # Same projection the two dashboards use, engineering vocabulary, so the
        # log cannot tell a third story about a command the other two describe.
        return {"day": day, "truncated": truncated,
                # Rows are already oldest-first from the query, and
                # _action_history preserves the order it is given, so the day
                # reads forwards without reversing anything.
                "commands": _action_history(shown, public=False)}

    @staticmethod
    def _live_preview():
        # Lazy so dashboard.py imports with no vision dependency, and so the
        # `dash` CLI (no counter) simply returns None here.
        try:
            from vision.counter import live_preview_jpeg
            return live_preview_jpeg()
        except Exception:
            return None

    def _camera_still(self) -> None:
        """The current camera frame as one JPEG. Made on demand, never stored —
        the privacy contract is about the durable record, not a live view."""
        jpg = self._live_preview()
        if not jpg:
            return self._json({"error": "no live camera preview"}, 503)
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpg)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(jpg)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _camera_stream(self) -> None:
        """MJPEG (multipart/x-mixed-replace) — the live view, rendered natively
        by any browser. One request-thread per viewer (ThreadingHTTPServer), so
        it holds nothing else up; it ends when the viewer closes the tab."""
        import time
        if self._live_preview() is None:
            return self._json({"error": "no live camera preview"}, 503)
        boundary = "breezeiqframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while True:
                jpg = self._live_preview()
                if jpg is not None and jpg is not last:
                    self.wfile.write(f"--{boundary}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(
                        f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                    last = jpg
                time.sleep(0.12)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        route = self.POST_ROUTES.get(path)
        if route is None:
            return self._json({"error": "not found"}, 404)
        required_intent, factory_name = route
        if self.headers.get("X-BreezeIQ-Intent") != required_intent:
            return self._json({"ok": False, "state": "rejected",
                               "error": f"{required_intent} intent header required"}, 403)
        origin = self.headers.get("Origin")
        try:
            origin_parts = urlsplit(origin or "")
            same_origin = (origin_parts.scheme in {"http", "https"}
                           and origin_parts.netloc == self.headers.get("Host"))
        except ValueError:
            same_origin = False
        if not same_origin:
            return self._json({"ok": False, "state": "rejected",
                               "error": "same-origin dashboard request required"}, 403)
        if self.headers.get_content_type() != "application/json":
            return self._json({"ok": False, "state": "rejected",
                               "error": "application/json required"}, 415)
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_BODY_BYTES:
            return self._json({"ok": False, "state": "rejected",
                               "error": "invalid request size"}, 413)
        try:
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            return self._json({"ok": False, "state": "rejected",
                               "error": "invalid JSON"}, 400)
        store = self.store_factory()
        response, code = getattr(self, factory_name)(str(store.path)).submit(body)
        self._json(response, code)

    def log_message(self, *_args) -> None:
        pass


# The two page templates that used to live here served :8000 and are gone.
# brain/console.py owns every rendered surface now; this module keeps the parts
# that were never about HTML — the store, the command facades, the payload
# split and the request handler — which is exactly the layer the console
# subclasses. `about.html` stays: Room links to it, and it is where the method
# note is stated once.


def serve(host: str = os.environ.get("BREEZEIQ_HOST", "127.0.0.1"),
          port: int = DEFAULT_PORT) -> None:
    print(f"BreezeIQ board console -> http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    serve()
