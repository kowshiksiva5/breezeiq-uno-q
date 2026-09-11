"""Durable board-local telemetry for BreezeIQ.

SQLite is deliberately part of the room controller, not an optional analytics
sidecar.  The database uses WAL, explicit transactions, forward-only schema
migrations and bounded retention.  A database failure always degrades logging;
it must never stop the comfort loop.

Raw sensor values remain beside calibrated and fused values so future
calibration does not rewrite history.  Camera frames are never stored.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from threading import RLock
from typing import Any, Mapping, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS tick (
    at                    REAL PRIMARY KEY,
    indoor_c              REAL,
    indoor_rh             REAL,
    outdoor_c             REAL,
    outdoor_rh            REAL,
    light_raw             INTEGER,
    solar_index           REAL,
    air_raw               INTEGER,
    occupied              INTEGER,
    occupant              TEXT,
    pmv                   REAL,
    fan                   INTEGER,
    window                INTEGER,
    blinds_shut           INTEGER,
    ac                    INTEGER,
    light                 INTEGER,
    reason                TEXT,
    valid                 INTEGER,
    fault                 TEXT,
    source                TEXT,
    people                INTEGER,
    people_confidence     REAL,
    camera_health         TEXT,
    camera_source         TEXT,
    camera_last_valid_at  REAL,
    pir_last_motion_at    REAL,
    camera_light          REAL,
    light_state           TEXT,
    light_confidence      TEXT,
    light_provenance      TEXT,
    camera_fault          TEXT,
    mode                  TEXT
);
CREATE INDEX IF NOT EXISTS tick_at ON tick(at);

CREATE TABLE IF NOT EXISTS event (
    at      REAL,
    kind    TEXT,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS event_at ON event(at);

CREATE TABLE IF NOT EXISTS fault (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    component   TEXT NOT NULL,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    detail      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    cleared_at  REAL
);
CREATE INDEX IF NOT EXISTS fault_at ON fault(at);

CREATE TABLE IF NOT EXISTS system_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    component   TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS system_health_at ON system_health(at);

CREATE TABLE IF NOT EXISTS energy_sample (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    interval_start  REAL,
    interval_end    REAL,
    device          TEXT NOT NULL,
    watts           REAL,
    watt_hours      REAL,
    provenance      TEXT NOT NULL CHECK (
                        provenance IN ('measured','derived','estimated')),
    source          TEXT NOT NULL,
    entity_id       TEXT,
    quality         TEXT NOT NULL DEFAULT 'unknown',
    missing_reason  TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS energy_sample_at ON energy_sample(at);

CREATE TABLE IF NOT EXISTS energy_meter_cursor (
    entity_id       TEXT PRIMARY KEY,
    device          TEXT NOT NULL,
    measurement     TEXT NOT NULL CHECK (measurement IN ('power','energy')),
    sample_at       REAL NOT NULL,
    value           REAL NOT NULL,
    unit            TEXT NOT NULL,
    source          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS energy_baseline (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          REAL NOT NULL,
    name                TEXT NOT NULL,
    window_start        REAL NOT NULL,
    window_end          REAL NOT NULL,
    occupancy_basis     TEXT NOT NULL,
    source_query        TEXT NOT NULL,
    missing_data_rule   TEXT NOT NULL,
    uncertainty_json    TEXT NOT NULL DEFAULT '{}',
    watt_hours          REAL,
    coverage            REAL,
    sample_count        INTEGER,
    device_set_json     TEXT NOT NULL DEFAULT '[]',
    provenance          TEXT NOT NULL DEFAULT 'verified_meter',
    duration_s          REAL,
    quality             TEXT NOT NULL DEFAULT 'unknown',
    UNIQUE(name, window_start, window_end)
);

CREATE TABLE IF NOT EXISTS backup_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_at    REAL NOT NULL,
    started_at      REAL,
    finished_at     REAL,
    status          TEXT NOT NULL,
    local_path      TEXT,
    blob_name       TEXT,
    sha256          TEXT,
    size_bytes      INTEGER,
    attempt         INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS backup_run_status ON backup_run(status, scheduled_at);

CREATE TABLE IF NOT EXISTS retention_run (
    at              REAL PRIMARY KEY,
    cutoff_at       REAL NOT NULL,
    removed_rows    INTEGER NOT NULL,
    max_age_days    INTEGER NOT NULL,
    max_tick_rows   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ac_twin_control (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    updated_at      REAL NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('automatic','manual')),
    power           INTEGER NOT NULL CHECK (power IN (0,1)),
    setpoint_c      REAL NOT NULL,
    expires_at      REAL,
    operator        TEXT NOT NULL,
    reason          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ac_twin_state (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    at                  REAL NOT NULL,
    modeled_c           REAL,
    cooling_effect      REAL NOT NULL,
    sensed_indoor_c     REAL,
    sensed_outdoor_c    REAL,
    power               INTEGER NOT NULL CHECK (power IN (0,1)),
    setpoint_c          REAL NOT NULL,
    mode                TEXT NOT NULL CHECK (mode IN ('automatic','manual')),
    status              TEXT NOT NULL,
    estimated_watts     REAL NOT NULL,
    provenance          TEXT NOT NULL,
    advisory            TEXT,
    ac_model            TEXT,
    modeled_rh          REAL,
    sensed_indoor_rh    REAL
);

CREATE TABLE IF NOT EXISTS ac_twin_sample (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    at                  REAL NOT NULL,
    modeled_c           REAL,
    cooling_effect      REAL NOT NULL,
    sensed_indoor_c     REAL,
    sensed_outdoor_c    REAL,
    power               INTEGER NOT NULL CHECK (power IN (0,1)),
    setpoint_c          REAL NOT NULL,
    mode                TEXT NOT NULL,
    status              TEXT NOT NULL,
    estimated_watts     REAL NOT NULL,
    interval_wh         REAL,
    provenance          TEXT NOT NULL,
    advisory            TEXT,
    ac_model            TEXT,
    modeled_rh          REAL,
    sensed_indoor_rh    REAL
);
CREATE INDEX IF NOT EXISTS ac_twin_sample_at ON ac_twin_sample(at);

CREATE TABLE IF NOT EXISTS ac_twin_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    action          TEXT NOT NULL,
    value_json      TEXT NOT NULL,
    operator        TEXT NOT NULL,
    reason          TEXT NOT NULL,
    provenance      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ac_twin_event_at ON ac_twin_event(at);

CREATE TABLE IF NOT EXISTS user_preference (
    name            TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      REAL NOT NULL DEFAULT 0,
    operator        TEXT NOT NULL DEFAULT 'local-dashboard'
);

-- Every projection the planner made, including the ones where it declined.
-- The declines matter as much as the actions: "why did nothing happen at 3pm?"
-- is the question a reviewer actually asks, and without the reason recorded the
-- only honest answer is a shrug. `provenance` says whether the envelope
-- coefficients behind this decision were fitted from this room's own history or
-- are still the engineering priors, so no projection can be read as
-- better-founded than it was.
CREATE TABLE IF NOT EXISTS horizon_decision (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    acted           INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    horizon_min     REAL NOT NULL,
    band            TEXT NOT NULL,
    watt_hours      REAL,
    worst_ppd       REAL,
    cheaper_by_wh   REAL,
    chosen_json     TEXT NOT NULL DEFAULT '{}',
    provenance      TEXT NOT NULL DEFAULT 'prior'
);
CREATE INDEX IF NOT EXISTS horizon_decision_at ON horizon_decision(at);

-- Fitted envelope coefficients, one row per accepted fit. Append-only: an old
-- fit is evidence about what the room looked like then, and overwriting it would
-- lose the ability to explain a decision made under it. Lives in SQLite rather
-- than a JSON file so it inherits WAL, the integrity check, retention and the
-- verified Azure backup - `ar_weights.json` sits outside all four, which is
-- tolerable for a cache and not for something that gates real decisions.
CREATE TABLE IF NOT EXISTS envelope_fit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    at                  REAL NOT NULL,
    tau_h               REAL NOT NULL,
    solar_c_per_h       REAL NOT NULL,
    shade_block         REAL NOT NULL,
    vent_c_per_h_per_c  REAL NOT NULL,
    occupant_c_per_h    REAL NOT NULL,
    provenance          TEXT NOT NULL,
    identifiable_json   TEXT NOT NULL DEFAULT '{}',
    rmse                REAL,
    baseline_rmse       REAL,
    sample_count        INTEGER,
    window_start        REAL,
    window_end          REAL
);
CREATE INDEX IF NOT EXISTS envelope_fit_at ON envelope_fit(at);

-- The occupancy prior: mean and sigma of presence per hour-of-day, aggregated
-- across DAYS. `n_days` is stored beside every statistic on purpose - a mean
-- over four days and a mean over forty are different claims, and a reader who
-- cannot see which one this is has been misled by a number that looks precise.
CREATE TABLE IF NOT EXISTS occupancy_prior (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    hour            INTEGER NOT NULL,
    day_kind        TEXT NOT NULL DEFAULT 'all',
    mean_presence   REAL,
    sigma           REAL,
    n_days          INTEGER NOT NULL,
    confidence      TEXT NOT NULL,
    window_days     INTEGER
);
CREATE INDEX IF NOT EXISTS occupancy_prior_at ON occupancy_prior(at, hour);
"""


# Old databases are migrated in place. Columns are append-only because SQLite
# versions on appliance boards vary and DROP/ALTER support is not assumed.
TICK_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("people", "INTEGER"),
    ("people_confidence", "REAL"),
    ("camera_health", "TEXT"),
    ("camera_source", "TEXT"),
    ("camera_last_valid_at", "REAL"),
    ("pir_last_motion_at", "REAL"),
    ("camera_light", "REAL"),
    ("light_state", "TEXT"),
    ("light_confidence", "TEXT"),
    ("light_provenance", "TEXT"),
    ("camera_fault", "TEXT"),
    # The occupancy mode the plan was chosen for. Without it a stored row cannot
    # answer "why was the fan off?" — EMPTY and OCCUPIED look identical after the
    # fact, and the analysis is the point of keeping the row at all.
    ("mode", "TEXT"),
    # Both are logged before either is trusted. A sensor earns a control path
    # by producing a record a human can look at first, which is the same order
    # the LDR was never held to.
    ("lux", "REAL"),
    ("radar_presence", "INTEGER"),
    # Bring-up figures, not room facts. A level alone cannot separate a stuck
    # radar pin from a busy room; edges and hold time can, and only if they
    # were written down at the time.
    ("radar_edges", "INTEGER"),
    ("radar_held_s", "REAL"),
    ("motor_dir", "INTEGER"),
    ("motor_speed", "INTEGER"),
    ("motor_left_s", "REAL"),
    ("failsafe_active", "INTEGER"),
    ("failsafe_episodes", "INTEGER"),
    ("fw_build", "INTEGER"),
    ("ldr_min", "INTEGER"),
    ("ldr_max", "INTEGER"),
    ("lux_bus", "INTEGER"),
    ("i2c_bus0", "INTEGER"),
    ("i2c_bus1", "INTEGER"),
    ("i2c_bus2", "INTEGER"),
    # Wiring facts, not room facts, but recorded per tick for the same reason
    # light_raw is: during bring-up the question "was it on the bus at 18:40?"
    # is only answerable if something wrote it down at 18:40.
    ("lux_addr", "INTEGER"),
    ("i2c_devices", "INTEGER"),
    ("sda_pullup", "INTEGER"),
    ("scl_pullup", "INTEGER"),
    ("sda_level", "INTEGER"),
    ("scl_level", "INTEGER"),
    ("indoor_raw_c", "REAL"),
    ("outdoor_raw_c", "REAL"),
)

ENERGY_BASELINE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("watt_hours", "REAL"),
    ("coverage", "REAL"),
    ("sample_count", "INTEGER"),
    ("device_set_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("provenance", "TEXT NOT NULL DEFAULT 'verified_meter'"),
    ("duration_s", "REAL"),
    ("quality", "TEXT NOT NULL DEFAULT 'unknown'"),
)

# The modeled appliance gained a named profile and an out-of-reach advisory,
# so databases written by the first twin release grow both columns in place.
AC_TWIN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("advisory", "TEXT"),
    ("ac_model", "TEXT"),
    # A coil below dew point condenses, so the response model tracks moisture
    # as well as temperature. Stored because humidity is a PMV input: without
    # these the modelled RH would reset to the raw sensor on every restart and
    # the room would forget it had been dried.
    ("modeled_rh", "REAL"),
    ("sensed_indoor_rh", "REAL"),
)

# Stored preferences answer "who asked for this, and when?" as well as "what".
# Both carry defaults so a board database written before the columns existed
# migrates in place instead of losing the preference it already holds.
USER_PREFERENCE_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("updated_at", "REAL NOT NULL DEFAULT 0"),
    ("operator", "TEXT NOT NULL DEFAULT 'local-dashboard'"),
)

# How long an UNCHANGED component may stay silent before it writes anyway. Five
# minutes keeps a liveness heartbeat — silence must never be mistaken for
# health — while cutting the noisiest writer from 144 rows an hour to 12.
HEALTH_HEARTBEAT_S = 300.0

PREFERENCE_MAX_NAME = 64
PREFERENCE_MAX_VALUE = 64

TICK_COLUMNS = (
    "at", "indoor_c", "indoor_rh", "outdoor_c", "outdoor_rh",
    "light_raw", "solar_index", "air_raw", "occupied", "occupant",
    "pmv", "fan", "window", "blinds_shut", "ac", "light", "reason",
    "valid", "fault", "source", "people", "people_confidence",
    "camera_health", "camera_source", "camera_last_valid_at",
    "pir_last_motion_at", "camera_light", "light_state",
    "light_confidence", "light_provenance", "camera_fault", "mode",
    "lux", "radar_presence", "radar_edges", "radar_held_s",
    "motor_dir", "motor_speed", "motor_left_s",
    "failsafe_active", "failsafe_episodes", "fw_build",
    "ldr_min", "ldr_max", "lux_bus", "i2c_bus0", "i2c_bus1", "i2c_bus2",
    "lux_addr", "i2c_devices",
    "sda_pullup", "scl_pullup", "sda_level", "scl_level",
    "indoor_raw_c", "outdoor_raw_c",
)


def _opt_int(value: Optional[bool]) -> Optional[int]:
    return None if value is None else int(value)


def _json(value: Any) -> str:
    """Stable JSON for evidence payloads; never fail logging on exotic types."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps({"unserializable": str(value)})


def _get(data: Optional[Mapping[str, Any]], key: str, default=None):
    return data.get(key, default) if isinstance(data, Mapping) else default


class Telemetry:
    """Thread-safe, restart-safe telemetry store.

    ``write`` remains source-compatible with the original five-argument API.
    Vision and fusion are optional, so missing camera support produces NULL
    evidence instead of invented values.
    """

    def __init__(self, path: str = "telemetry.sqlite3"):
        self.path = Path(path).expanduser()
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = RLock()
        self.rows = 0
        self.error: Optional[str] = None
        self._writes_since_retention = 0
        # component -> (last written at, last status), for the dedupe above.
        # In memory only: a restart writing one extra row per component is
        # the correct cost of not persisting this.
        self._health_seen: dict = {}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                str(self.path), check_same_thread=False, timeout=5.0)
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            # FULL syncs both WAL content and commit marker. This is deliberate
            # for a mains-powered appliance where evidence must survive loss.
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")
            self._conn.executescript(SCHEMA)
            self._migrate("tick", TICK_MIGRATIONS)
            self._migrate("energy_baseline", ENERGY_BASELINE_MIGRATIONS)
            self._migrate("ac_twin_state", AC_TWIN_MIGRATIONS)
            self._migrate("ac_twin_sample", AC_TWIN_MIGRATIONS)
            self._migrate("user_preference", USER_PREFERENCE_MIGRATIONS)
            self._conn.commit()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            if self._conn is not None:
                self._conn.close()
            self._conn = None

    def _migrate(self, table: str,
                 columns: tuple[tuple[str, str], ...]) -> None:
        assert self._conn is not None
        existing = {
            row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        for name, sql_type in columns:
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")

    def write(self, frame, pmv: float, plan, occupant: str,
              people: Optional[int] = None,
              vision: Optional[Mapping[str, Any]] = None,
              light_evidence: Optional[Mapping[str, Any]] = None,
              pir_last_motion_at: Optional[float] = None) -> bool:
        if self._conn is None:
            return False
        at = float(getattr(frame, "at", 0.0) or time.time())
        # A source timestamp can repeat when a dashboard/loop reads faster than
        # the MCU. Keep every decision while preserving time ordering.
        values = (
            at, frame.indoor_c, frame.indoor_rh, frame.outdoor_c,
            frame.outdoor_rh, frame.light_raw, frame.solar_index, frame.air_raw,
            1 if frame.occupied else 0, occupant, pmv, plan.fan,
            int(plan.window), int(plan.blinds_shut), int(plan.ac),
            int(plan.light), plan.reason, int(frame.valid), frame.fault,
            frame.source, people,
            _get(vision, "confidence"), _get(vision, "health"),
            _get(vision, "source"), _get(vision, "last_valid_at"),
            pir_last_motion_at, _get(vision, "luminance"),
            _get(light_evidence, "state"), _get(light_evidence, "confidence"),
            _json(_get(light_evidence, "sources", [])), _get(vision, "fault"),
            # A plan from before the mode existed still logs; a telemetry writer
            # that raises on a missing attribute would stop the comfort loop.
            getattr(plan, "mode", None),
            # getattr for the same reason as mode above: a frame from a reader
            # that predates these fields must still log rather than raise and
            # take the comfort loop down with it.
            getattr(frame, "lux", None),
            None if getattr(frame, "radar_presence", None) is None
            else int(frame.radar_presence),
            getattr(frame, "radar_edges", None),
            getattr(frame, "radar_held_s", None),
            getattr(frame, "motor_dir", None),
            getattr(frame, "motor_speed", None),
            getattr(frame, "motor_left_s", None),
            _opt_int(getattr(frame, "failsafe_active", None)),
            getattr(frame, "failsafe_episodes", None),
            getattr(frame, "fw_build", None),
            getattr(frame, "ldr_min", None), getattr(frame, "ldr_max", None),
            getattr(frame, "lux_bus", None),
            getattr(frame, "i2c_bus0", None), getattr(frame, "i2c_bus1", None),
            getattr(frame, "i2c_bus2", None),
            getattr(frame, "lux_addr", None), getattr(frame, "i2c_devices", None),
            _opt_int(getattr(frame, "sda_pullup", None)),
            _opt_int(getattr(frame, "scl_pullup", None)),
            getattr(frame, "sda_level", None), getattr(frame, "scl_level", None),
            getattr(frame, "indoor_raw_c", None),
            getattr(frame, "outdoor_raw_c", None),
        )
        placeholders = ",".join("?" for _ in TICK_COLUMNS)
        columns = ",".join(TICK_COLUMNS)
        try:
            with self._lock:
                # REPLACE retains compatibility with an MCU timestamp that is
                # deliberately re-read; named columns keep migrations safe.
                self._conn.execute(
                    f"INSERT OR REPLACE INTO tick ({columns}) VALUES ({placeholders})",
                    values)
                self._conn.commit()
                self.rows += 1
                self._writes_since_retention += 1
            self._maybe_retain()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def latest_pir_motion_at(self) -> Optional[float]:
        """Latest durable PIR motion timestamp for restart-safe occupancy."""
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT MAX(pir_last_motion_at) FROM tick").fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def event(self, kind: str, detail: str) -> None:
        self._execute_no_throw(
            "INSERT INTO event(at,kind,detail) VALUES (?,?,?)",
            (time.time(), kind, detail))

    def fault(self, component: str, code: str, detail: str,
              severity: str = "warning", status: str = "open") -> None:
        self._execute_no_throw(
            "INSERT INTO fault(at,component,code,severity,detail,status) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), component, code, severity, detail, status))

    def system_health(self, component: str, status: str,
                      detail: Optional[Mapping[str, Any]] = None) -> None:
        """Record a component's health, DEDUPED while nothing changes.

        Measured on the board: this table was 59 % of the whole database —
        11 MB of 18.8 MB — because three components wrote a row on every tick
        whether or not anything had happened. `ac_conditioning` alone logged 144
        identical "ok" rows an hour. Projected against the 30-day retention that
        is 147 MB, on a root filesystem with 295 MB free, and every hourly Azure
        snapshot copies the whole thing.

        What health rows are FOR is the moment something changed, plus enough of a
        heartbeat to prove the component is still running. Both survive here: a
        changed status writes immediately, and an unchanged one still writes every
        HEALTH_HEARTBEAT_S so silence remains distinguishable from health.

        The rest of this project already knew this — `_sensor_episode` writes one
        row per episode, which is why the camera has 8 rows where the AC model has
        5,980. This applies the same discipline to the writers that missed it.
        """
        status = str(status)
        now = time.time()
        last = self._health_seen.get(component)
        if last is not None:
            last_at, last_status = last
            if last_status == status and now - last_at < HEALTH_HEARTBEAT_S:
                return
        self._health_seen[component] = (now, status)
        self._execute_no_throw(
            "INSERT INTO system_health(at,component,status,detail_json) "
            "VALUES (?,?,?,?)",
            (now, component, status, _json(detail or {})))

    def preference(self, name: str) -> Optional[str]:
        """One stored occupant preference, or None when nobody has set it.

        Preferences are deliberately untyped strings here: the meaning of a
        value belongs to the policy that reads it, and a storage layer that
        knows the vocabulary would have to be migrated every time the policy
        gains an option.
        """
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value FROM user_preference WHERE name=?",
                    (str(name),)).fetchone()
            return str(row[0]) if row is not None else None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def preferences(self) -> dict:
        """Every stored preference, for a dashboard that renders all of them."""
        if self._conn is None:
            return {}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT name,value FROM user_preference").fetchall()
            return {str(name): str(value) for name, value in rows}
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return {}

    def set_preference(self, name: str, value: str, *,
                       operator: str = "local-dashboard") -> bool:
        """Store one preference and its audit row in a single transaction.

        A preference outlives the process that set it, so it is written the
        same way a manual AC request is: upsert plus an event the operator can
        read back, committed together or not at all.
        """
        name, value = str(name).strip(), str(value).strip()
        if not name or not value:
            raise ValueError("preference name and value are required")
        if len(name) > PREFERENCE_MAX_NAME or len(value) > PREFERENCE_MAX_VALUE:
            raise ValueError("preference name or value is too long")
        if self._conn is None:
            return False
        now = time.time()
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO user_preference(name,value,updated_at,operator) "
                    "VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                    "value=excluded.value,updated_at=excluded.updated_at,"
                    "operator=excluded.operator",
                    (name, value, now, str(operator)))
                self._conn.execute(
                    "INSERT INTO event(at,kind,detail) VALUES (?,?,?)",
                    (now, "preference",
                     _json({"name": name, "value": value,
                            "operator": str(operator)})))
                self._conn.commit()
            return True
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def horizon_decision(self, *, acted: bool, reason: str, horizon_min: float,
                         band: str, watt_hours: Optional[float] = None,
                         worst_ppd: Optional[float] = None,
                         cheaper_by_wh: Optional[float] = None,
                         chosen: Optional[Mapping[str, Any]] = None,
                         provenance: str = "prior",
                         at: Optional[float] = None) -> bool:
        """Record one projection, acted on or declined.

        Declines are logged deliberately. "Why did nothing happen at 3pm?" is the
        question a reviewer actually asks, and a table that only holds the
        actions cannot answer it.
        """
        return self._execute_no_throw(
            "INSERT INTO horizon_decision(at,acted,reason,horizon_min,band,"
            "watt_hours,worst_ppd,cheaper_by_wh,chosen_json,provenance) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (float(at if at is not None else time.time()), int(bool(acted)),
             str(reason), float(horizon_min), str(band), watt_hours, worst_ppd,
             cheaper_by_wh, _json(chosen or {}), str(provenance)))

    def save_envelope_fit(self, coefficients: Mapping[str, Any], *,
                          provenance: str,
                          identifiable: Optional[Mapping[str, Any]] = None,
                          rmse: Optional[float] = None,
                          baseline_rmse: Optional[float] = None,
                          sample_count: Optional[int] = None,
                          window: Optional[tuple] = None,
                          at: Optional[float] = None) -> bool:
        """Persist one accepted envelope fit. Append-only, never an update."""
        start, end = (window or (None, None))
        return self._execute_no_throw(
            "INSERT INTO envelope_fit(at,tau_h,solar_c_per_h,shade_block,"
            "vent_c_per_h_per_c,occupant_c_per_h,provenance,identifiable_json,"
            "rmse,baseline_rmse,sample_count,window_start,window_end) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (float(at if at is not None else time.time()),
             float(coefficients["tau_h"]),
             float(coefficients["solar_c_per_h"]),
             float(coefficients["shade_block"]),
             float(coefficients["vent_c_per_h_per_c"]),
             float(coefficients["occupant_c_per_h"]),
             str(provenance), _json(identifiable or {}), rmse, baseline_rmse,
             sample_count, start, end))

    def latest_envelope_fit(self) -> Optional[dict]:
        """The most recent accepted fit, or None when the room has never had one."""
        if self._conn is None:
            return None
        try:
            with self._lock:
                cur = self._conn.execute(
                    "SELECT * FROM envelope_fit ORDER BY at DESC LIMIT 1")
                row = cur.fetchone()
                columns = [item[0] for item in cur.description]
            return dict(zip(columns, row)) if row is not None else None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def save_occupancy_prior(self, buckets, *, window_days: int,
                             at: Optional[float] = None) -> bool:
        """Persist the whole prior as one generation of rows.

        `buckets` is an iterable of dicts carrying hour, day_kind, mean, sigma,
        n_days and confidence. A bucket below its sample floor is stored WITH its
        `insufficient` verdict rather than omitted — "we looked and there was not
        enough" and "we never looked" are different facts.
        """
        rows = [
            (float(at if at is not None else time.time()), int(b["hour"]),
             str(b.get("day_kind", "all")), b.get("mean"), b.get("sigma"),
             int(b.get("n_days", 0)), str(b.get("confidence", "insufficient")),
             int(window_days))
            for b in buckets
        ]
        if not rows:
            return False
        if self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.executemany(
                    "INSERT INTO occupancy_prior(at,hour,day_kind,mean_presence,"
                    "sigma,n_days,confidence,window_days) VALUES (?,?,?,?,?,?,?,?)",
                    rows)
                self._conn.commit()
            return True
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def latest_occupancy_prior(self) -> dict:
        """The newest generation of the prior, keyed by (hour, day_kind)."""
        if self._conn is None:
            return {}
        try:
            with self._lock:
                newest = self._conn.execute(
                    "SELECT MAX(at) FROM occupancy_prior").fetchone()
                if not newest or newest[0] is None:
                    return {}
                rows = self._conn.execute(
                    "SELECT hour,day_kind,mean_presence,sigma,n_days,confidence "
                    "FROM occupancy_prior WHERE at=?", (newest[0],)).fetchall()
            return {(int(h), str(k)): {"mean": m, "sigma": s, "n_days": int(n),
                                       "confidence": str(c)}
                    for h, k, m, s, n, c in rows}
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return {}

    def ac_twin_state(self) -> Optional[dict]:
        """Return the last explicit model state, never a sensor substitution."""
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM ac_twin_state WHERE id=1").fetchone()
                columns = [item[0] for item in self._conn.execute(
                    "SELECT * FROM ac_twin_state LIMIT 0").description]
            return dict(zip(columns, row)) if row is not None else None
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def ac_twin_control(self, at: Optional[float] = None, *,
                        active_only: bool = True) -> Optional[dict]:
        """Return a still-active manual model control, else automatic mode.

        `active_only=False` returns a lapsed row as well, because it is still
        the occupant's last expressed setpoint: a caller that has to name one
        should read what was asked for rather than invent a number.
        """
        if self._conn is None:
            return None
        now = float(at if at is not None else time.time())
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT updated_at,mode,power,setpoint_c,expires_at,operator,reason "
                    "FROM ac_twin_control WHERE id=1").fetchone()
            if row is None:
                return None
            keys = ("updated_at", "mode", "power", "setpoint_c", "expires_at",
                    "operator", "reason")
            result = dict(zip(keys, row))
            if (active_only and result["mode"] == "manual"
                    and result["expires_at"] is not None
                    and float(result["expires_at"]) <= now):
                return None
            return result
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def set_ac_twin_control(self, *, mode: str, power: bool,
                            setpoint_c: float, expires_at: Optional[float],
                            operator: str, reason: str, action: str) -> bool:
        """Audit and atomically update controls for the model only."""
        if mode not in {"automatic", "manual"}:
            raise ValueError("mode must be automatic or manual")
        if not 16 <= float(setpoint_c) <= 30:
            raise ValueError("setpoint_c must be from 16 to 30")
        now = time.time()
        if self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO ac_twin_control(id,updated_at,mode,power,setpoint_c,"
                    "expires_at,operator,reason) VALUES(1,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,"
                    "mode=excluded.mode,power=excluded.power,"
                    "setpoint_c=excluded.setpoint_c,expires_at=excluded.expires_at,"
                    "operator=excluded.operator,reason=excluded.reason",
                    (now, mode, int(bool(power)), float(setpoint_c), expires_at,
                     operator, reason))
                self._conn.execute(
                    "INSERT INTO ac_twin_event(at,action,value_json,operator,reason,"
                    "provenance) VALUES(?,?,?,?,?,?)",
                    (now, action, _json({"mode": mode, "power": bool(power),
                                        "setpoint_c": float(setpoint_c),
                                        "expires_at": expires_at}),
                     operator, reason, "digital_twin_control"))
                self._conn.commit()
            return True
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def ac_twin_save(self, state: Mapping[str, Any]) -> bool:
        """Persist current and historical model states with clear provenance."""
        interval_wh = state.get("interval_wh")
        trailing = (
            str(state.get("provenance") or "digital_twin_inverter_v2"),
            state.get("advisory"), state.get("ac_model"),
            state.get("modeled_rh"), state.get("sensed_indoor_rh"),
        )
        head = (
            float(state["at"]), state.get("modeled_c"),
            float(state.get("cooling_effect") or 0.0),
            state.get("sensed_indoor_c"), state.get("sensed_outdoor_c"),
            int(bool(state.get("power"))), float(state["setpoint_c"]),
            str(state["mode"]), str(state["status"]),
            float(state.get("estimated_watts") or 0.0),
        )
        if self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO ac_twin_state(id,at,modeled_c,cooling_effect,"
                    "sensed_indoor_c,sensed_outdoor_c,power,setpoint_c,mode,status,"
                    "estimated_watts,provenance,advisory,ac_model,modeled_rh,"
                    "sensed_indoor_rh) "
                    "VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET at=excluded.at,"
                    "modeled_c=excluded.modeled_c,cooling_effect=excluded.cooling_effect,"
                    "sensed_indoor_c=excluded.sensed_indoor_c,"
                    "sensed_outdoor_c=excluded.sensed_outdoor_c,power=excluded.power,"
                    "setpoint_c=excluded.setpoint_c,mode=excluded.mode,"
                    "status=excluded.status,estimated_watts=excluded.estimated_watts,"
                    "provenance=excluded.provenance,advisory=excluded.advisory,"
                    "ac_model=excluded.ac_model,modeled_rh=excluded.modeled_rh,"
                    "sensed_indoor_rh=excluded.sensed_indoor_rh",
                    head + trailing)
                self._conn.execute(
                    "INSERT INTO ac_twin_sample(at,modeled_c,cooling_effect,"
                    "sensed_indoor_c,sensed_outdoor_c,power,setpoint_c,mode,status,"
                    "estimated_watts,interval_wh,provenance,advisory,ac_model,"
                    "modeled_rh,sensed_indoor_rh) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    head + (interval_wh,) + trailing)
                self._conn.commit()
            return True
        except Exception as exc:
            try:
                self._conn.rollback()
            except Exception:
                pass
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def ac_twin_event(self, action: str, value: Mapping[str, Any], *,
                      operator: str, reason: str, provenance: str,
                      at: Optional[float] = None) -> bool:
        """Audit one model-side event, such as an out-of-reach advisory."""
        return self._execute_no_throw(
            "INSERT INTO ac_twin_event(at,action,value_json,operator,reason,"
            "provenance) VALUES (?,?,?,?,?,?)",
            (float(at if at is not None else time.time()), action,
             _json(value), operator, reason, provenance))

    def energy(self, device: str, provenance: str, source: str, *,
               at: Optional[float] = None,
               watts: Optional[float] = None,
               watt_hours: Optional[float] = None,
               interval_start: Optional[float] = None,
               interval_end: Optional[float] = None,
               entity_id: Optional[str] = None,
               quality: str = "unknown",
               missing_reason: Optional[str] = None,
               detail: Optional[Mapping[str, Any]] = None) -> bool:
        """Persist energy with explicit provenance.

        A row with no value is allowed only when ``missing_reason`` explains the
        gap. This prevents missing telemetry from becoming a silent zero.
        """
        if provenance not in {"measured", "derived", "estimated"}:
            raise ValueError("provenance must be measured, derived, or estimated")
        if watts is None and watt_hours is None and not missing_reason:
            raise ValueError("an energy value or missing_reason is required")
        return self._execute_no_throw(
            "INSERT INTO energy_sample(at,interval_start,interval_end,device,"
            "watts,watt_hours,provenance,source,entity_id,quality,missing_reason,"
            "detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (float(at if at is not None else time.time()),
             interval_start, interval_end, device, watts,
             watt_hours, provenance, source, entity_id, quality,
             missing_reason, _json(detail or {})))

    def energy_cursor(self, entity_id: str) -> Optional[dict]:
        """Return the last normalized meter reading used for integration.

        The cursor survives process restarts, preventing a reboot from turning
        a cumulative kWh total into a fresh appliance-usage claim.
        """
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT entity_id,device,measurement,sample_at,value,unit,source "
                    "FROM energy_meter_cursor WHERE entity_id=?", (entity_id,)
                ).fetchone()
            if row is None:
                return None
            keys = ("entity_id", "device", "measurement", "sample_at",
                    "value", "unit", "source")
            return dict(zip(keys, row))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return None

    def set_energy_cursor(self, entity_id: str, device: str,
                          measurement: str, sample_at: float, value: float,
                          unit: str, source: str) -> bool:
        if measurement not in {"power", "energy"}:
            raise ValueError("measurement must be power or energy")
        return self._execute_no_throw(
            "INSERT INTO energy_meter_cursor(entity_id,device,measurement,"
            "sample_at,value,unit,source) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET device=excluded.device,"
            "measurement=excluded.measurement,sample_at=excluded.sample_at,"
            "value=excluded.value,unit=excluded.unit,source=excluded.source",
            (entity_id, device, measurement, float(sample_at), float(value),
             unit, source))

    def energy_baseline(self, name: str, window_start: float,
                        window_end: float, occupancy_basis: str,
                        source_query: str, missing_data_rule: str, *,
                        watt_hours: float, coverage: float, sample_count: int,
                        devices: list[str],
                        uncertainty: Optional[Mapping[str, Any]] = None,
                        provenance: str = "verified_meter",
                        quality: str = "verified") -> bool:
        """Persist a reproducible, meter-backed comparison window."""
        if window_end <= window_start:
            raise ValueError("baseline window_end must be after window_start")
        if not name.strip() or not occupancy_basis.strip():
            raise ValueError("baseline name and occupancy basis are required")
        if watt_hours <= 0:
            raise ValueError("baseline watt_hours must be positive")
        if not 0 <= coverage <= 100:
            raise ValueError("baseline coverage must be between 0 and 100")
        if not devices:
            raise ValueError("baseline devices are required")
        return self._execute_no_throw(
            "INSERT INTO energy_baseline(created_at,name,window_start,window_end,"
            "occupancy_basis,source_query,missing_data_rule,uncertainty_json,"
            "watt_hours,coverage,sample_count,device_set_json,provenance,"
            "duration_s,quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), name.strip(), float(window_start), float(window_end),
             occupancy_basis.strip(), source_query, missing_data_rule,
             _json(uncertainty or {}), float(watt_hours), float(coverage),
             int(sample_count), _json(sorted(set(devices))), provenance,
             float(window_end - window_start), quality))

    def _execute_no_throw(self, sql: str, params: tuple) -> bool:
        if self._conn is None:
            return False
        try:
            with self._lock:
                self._conn.execute(sql, params)
                self._conn.commit()
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def _maybe_retain(self) -> None:
        every = max(1, int(os.environ.get("BREEZEIQ_RETENTION_EVERY", "120")))
        if self._writes_since_retention < every:
            return
        self._writes_since_retention = 0
        self.prune()

    def prune(self, max_age_days: Optional[int] = None,
              max_tick_rows: Optional[int] = None) -> dict:
        """Bound eMMC use by age and row count; never VACUUM the live DB."""
        if self._conn is None:
            return {"ok": False, "error": self.error}
        days = max_age_days if max_age_days is not None else int(
            os.environ.get("BREEZEIQ_RETENTION_DAYS", "30"))
        max_rows = max_tick_rows if max_tick_rows is not None else int(
            os.environ.get("BREEZEIQ_RETENTION_MAX_TICKS", "250000"))
        if days < 1 or max_rows < 100:
            return {"ok": False, "error": "unsafe retention limits"}
        now = time.time()
        cutoff = now - days * 86400
        removed = 0
        try:
            with self._lock:
                for table, column in (
                    ("tick", "at"), ("event", "at"), ("fault", "at"),
                    ("system_health", "at"), ("energy_sample", "at"),
                    ("ac_twin_sample", "at"), ("ac_twin_event", "at"),
                    # Per-tick rows, so they grow like `tick` and need the same
                    # bound. `envelope_fit` and `occupancy_prior` are deliberately
                    # NOT here: they are a handful of rows per fit and they are
                    # what explains an old decision.
                    ("horizon_decision", "at"),
                ):
                    cur = self._conn.execute(
                        f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                    removed += max(cur.rowcount, 0)
                count = self._conn.execute("SELECT COUNT(*) FROM tick").fetchone()[0]
                if count > max_rows:
                    excess = count - max_rows
                    cur = self._conn.execute(
                        "DELETE FROM tick WHERE at IN "
                        "(SELECT at FROM tick ORDER BY at ASC LIMIT ?)", (excess,))
                    removed += max(cur.rowcount, 0)
                self._conn.execute(
                    "INSERT INTO retention_run VALUES (?,?,?,?,?)",
                    (now, cutoff, removed, days, max_rows))
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return {"ok": True, "removed_rows": removed,
                    "max_age_days": days, "max_tick_rows": max_rows}
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self.error}

    def integrity_check(self, quick: bool = True) -> dict:
        if self._conn is None:
            return {"ok": False, "error": self.error}
        pragma = "quick_check" if quick else "integrity_check"
        try:
            with self._lock:
                rows = [r[0] for r in self._conn.execute(f"PRAGMA {pragma}")]
            return {"ok": rows == ["ok"], "result": rows}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def stats(self) -> dict:
        if self._conn is None:
            return {"ok": False, "error": self.error}
        try:
            with self._lock:
                n, first, last = self._conn.execute(
                    "SELECT COUNT(*), MIN(at), MAX(at) FROM tick").fetchone()
                journal = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
            hours = (last - first) / 3600.0 if (n and first and last) else 0.0
            files = [self.path, Path(str(self.path) + "-wal"),
                     Path(str(self.path) + "-shm")]
            size = sum(p.stat().st_size for p in files if p.exists())
            return {
                "ok": True, "rows": n or 0, "hours": round(hours, 2),
                "path": str(self.path), "size_kb": round(size / 1024, 1),
                "journal_mode": journal,
                "ready_for_training": bool(n and hours >= 48),
                "last_write_at": last,
                "retention_days": int(os.environ.get(
                    "BREEZEIQ_RETENTION_DAYS", "30")),
                "retention_max_ticks": int(os.environ.get(
                    "BREEZEIQ_RETENTION_MAX_TICKS", "250000")),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def close(self) -> None:
        if self._conn is None:
            return
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "Telemetry":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
