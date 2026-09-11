"""Durable actuation audit, safety state, and manual authority.

The control loop and dashboard are separate processes on the UNO Q.  SQLite is
their shared, crash-safe source of truth.  These tables intentionally live in
the same database as telemetry so a judge can trace sensor -> decision ->
command -> acknowledgement -> reported state without joining separate files.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS act_command (
    command_id       TEXT PRIMARY KEY,
    at               REAL NOT NULL,
    device           TEXT NOT NULL,
    action           TEXT NOT NULL,
    requested_json   TEXT NOT NULL,
    actor             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    override_id       TEXT,
    acknowledged      INTEGER,
    reported_json     TEXT,
    outcome           TEXT NOT NULL,
    detail            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS act_command_at ON act_command(at);

CREATE TABLE IF NOT EXISTS act_reported_state (
    at               REAL NOT NULL,
    device           TEXT NOT NULL,
    source           TEXT NOT NULL,
    available         INTEGER NOT NULL,
    state_json        TEXT,
    detail            TEXT NOT NULL,
    evidence_at       REAL
);
CREATE INDEX IF NOT EXISTS act_reported_state_at
    ON act_reported_state(device, at);

CREATE TABLE IF NOT EXISTS act_manual_override (
    override_id       TEXT PRIMARY KEY,
    device            TEXT NOT NULL,
    action            TEXT NOT NULL,
    value_json        TEXT NOT NULL,
    operator          TEXT NOT NULL,
    reason            TEXT NOT NULL,
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    cleared_at        REAL,
    clear_reason      TEXT
);
CREATE INDEX IF NOT EXISTS act_manual_override_active
    ON act_manual_override(device, expires_at, cleared_at);

CREATE TABLE IF NOT EXISTS act_guard (
    device                TEXT PRIMARY KEY,
    last_off_at            REAL NOT NULL DEFAULT 0,
    last_command_at        REAL NOT NULL DEFAULT 0,
    last_requested_json    TEXT,
    last_acknowledged      INTEGER
);
"""


def _dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class ActuationJournal:
    """Small thread-safe SQLite facade.  Errors fail closed at the caller."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or ":memory:"
        self.error: Optional[str] = None
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=3,
                                         check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.executescript(SCHEMA)
            columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(act_reported_state)")}
            if "evidence_at" not in columns:
                self._conn.execute(
                    "ALTER TABLE act_reported_state ADD COLUMN evidence_at REAL")
            self._conn.commit()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self._conn = None

    @property
    def ok(self) -> bool:
        return self._conn is not None

    def new_command_id(self) -> str:
        return uuid.uuid4().hex

    def record_command(self, *, command_id: str, at: float, device: str,
                       action: str, requested: Any, actor: str, reason: str,
                       override_id: Optional[str], acknowledged: Optional[bool],
                       reported: Any, outcome: str, detail: str) -> bool:
        if not self._conn:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT OR REPLACE INTO act_command VALUES
                       (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (command_id, at, device, action, _dump(requested), actor,
                     reason, override_id,
                     None if acknowledged is None else int(acknowledged),
                     None if reported is None else _dump(reported),
                     outcome, detail))
                self._conn.commit()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def record_reported(self, *, device: str, source: str, available: bool,
                        state: Any, detail: str, at: Optional[float] = None) -> bool:
        if not self._conn:
            return False
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT INTO act_reported_state
                       (at,device,source,available,state_json,detail,evidence_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (time.time(), device, source, int(available),
                     None if state is None else _dump(state), detail,
                     at or time.time()))
                self._conn.commit()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def guard(self, device: str) -> dict:
        if not self._conn:
            return {"last_off_at": 0.0, "last_command_at": 0.0,
                    "last_requested": None, "last_acknowledged": None}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM act_guard WHERE device=?", (device,)).fetchone()
        if not row:
            return {"last_off_at": 0.0, "last_command_at": 0.0,
                    "last_requested": None, "last_acknowledged": None}
        return {
            "last_off_at": float(row[1] or 0),
            "last_command_at": float(row[2] or 0),
            "last_requested": json.loads(row[3]) if row[3] else None,
            "last_acknowledged": None if row[4] is None else bool(row[4]),
        }

    def update_guard(self, device: str, *, last_off_at: Optional[float] = None,
                     last_command_at: Optional[float] = None,
                     last_requested: Any = None,
                     last_acknowledged: Optional[bool] = None) -> bool:
        if not self._conn:
            return False
        current = self.guard(device)
        off = current["last_off_at"] if last_off_at is None else last_off_at
        command = (current["last_command_at"] if last_command_at is None
                   else last_command_at)
        requested = (current["last_requested"] if last_requested is None
                     else last_requested)
        acknowledged = (current["last_acknowledged"]
                        if last_acknowledged is None else last_acknowledged)
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT OR REPLACE INTO act_guard
                       (device,last_off_at,last_command_at,last_requested_json,
                        last_acknowledged) VALUES (?,?,?,?,?)""",
                    (device, off, command,
                     None if requested is None else _dump(requested),
                     None if acknowledged is None else int(acknowledged)))
                self._conn.commit()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def create_override(self, device: str, action: str, value: Any,
                        operator: str, reason: str, ttl_seconds: int) -> str:
        if not self._conn:
            raise RuntimeError(self.error or "actuation journal unavailable")
        if not operator.strip() or not reason.strip():
            raise ValueError("operator and reason are required")
        now = time.time()
        override_id = uuid.uuid4().hex
        with self._lock:
            # One active authority per device.  Preserve the old row as history.
            self._conn.execute(
                """UPDATE act_manual_override
                   SET cleared_at=?, clear_reason='superseded'
                   WHERE device=? AND cleared_at IS NULL AND expires_at>?""",
                (now, device, now))
            self._conn.execute(
                "INSERT INTO act_manual_override VALUES (?,?,?,?,?,?,?,?,?,?)",
                (override_id, device, action, _dump(value), operator, reason,
                 now, now + max(1, int(ttl_seconds)), None, None))
            self._conn.commit()
        return override_id

    def active_override(self, device: str, now: Optional[float] = None) -> Optional[dict]:
        if not self._conn:
            return None
        now = now or time.time()
        with self._lock:
            row = self._conn.execute(
                """SELECT override_id,device,action,value_json,operator,reason,
                          created_at,expires_at
                   FROM act_manual_override
                   WHERE device=? AND cleared_at IS NULL AND expires_at>?
                   ORDER BY created_at DESC LIMIT 1""", (device, now)).fetchone()
        if not row:
            return None
        return {"override_id": row[0], "device": row[1], "action": row[2],
                "value": json.loads(row[3]), "operator": row[4],
                "reason": row[5], "created_at": row[6], "expires_at": row[7]}

    def active_overrides(self) -> list[dict]:
        if not self._conn:
            return []
        now = time.time()
        with self._lock:
            devices = [r[0] for r in self._conn.execute(
                """SELECT DISTINCT device FROM act_manual_override
                   WHERE cleared_at IS NULL AND expires_at>?""", (now,))]
        return [v for d in devices if (v := self.active_override(d, now))]

    def clear_override(self, device: str, operator: str,
                       reason: str = "return to automatic") -> int:
        if not self._conn:
            return 0
        if not operator.strip():
            raise ValueError("operator is required")
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """UPDATE act_manual_override SET cleared_at=?,clear_reason=?
                   WHERE device=? AND cleared_at IS NULL AND expires_at>?""",
                (now, f"{operator}: {reason}", device, now))
            self._conn.commit()
        return cur.rowcount

    def health(self) -> dict:
        return {"ok": self.ok, "path": self.path, "error": self.error,
                "active_overrides": len(self.active_overrides()) if self.ok else 0}
