"""Meter-backed room-energy evidence and savings baselines.

The UNO Q owns this pipeline. Home Assistant supplies numeric device entities;
the board validates units, persists normalized readings, integrates power only
over bounded gaps, and compares against a separately recorded meter baseline.
Estimated values remain visible evidence but never enter the savings claim.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from telemetry import Telemetry


POWER_UNITS = {"W": 1.0, "kW": 1000.0}
ENERGY_UNITS = {"Wh": 1.0, "kWh": 1000.0}
VERIFIED_PROVENANCE = {"measured", "derived"}
DEFAULT_REQUIRED_DEVICES = ("ac", "tubelight")
ESTIMATE_FAN_W = {0: 0.0, 1: 8.0, 2: 18.0, 3: 32.0}
ESTIMATE_AC_W = 1500.0
REFERENCE_ON_C = 25.0
REFERENCE_OFF_C = 24.0
ESTIMATE_MAX_GAP_S = 120.0
ESTIMATE_MIN_WINDOW_S = 300.0


@dataclass(frozen=True)
class MeterMapping:
    device: str
    power_entity: str = ""
    energy_entity: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.power_entity or self.energy_entity)


@dataclass(frozen=True)
class EnergyConfig:
    mappings: tuple[MeterMapping, ...]
    required_devices: tuple[str, ...]
    sample_seconds: float = 30.0
    max_power_gap_s: float = 180.0
    max_meter_gap_s: float = 7200.0
    max_state_age_s: float = 7200.0
    min_baseline_hours: float = 24.0
    min_coverage_pct: float = 90.0

    @classmethod
    def from_env(cls) -> "EnergyConfig":
        ac_present = os.environ.get(
            "BREEZEIQ_AC_PHYSICALLY_PRESENT", "1") == "1"
        required = tuple(
            item.strip() for item in os.environ.get(
                "BREEZEIQ_ENERGY_REQUIRED_DEVICES",
                ",".join(DEFAULT_REQUIRED_DEVICES)).split(",")
            if item.strip()
        )
        return cls(
            mappings=(
                MeterMapping(
                    "ac",
                    os.environ.get("HA_AC_POWER_ENTITY", "") if ac_present else "",
                    os.environ.get("HA_AC_ENERGY_ENTITY", "") if ac_present else ""),
                MeterMapping("tubelight",
                             os.environ.get("HA_LIGHT_POWER_ENTITY", ""),
                             os.environ.get("HA_LIGHT_ENERGY_ENTITY", "")),
            ),
            required_devices=required,
            sample_seconds=max(10.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_SAMPLE_SECONDS", "30"))),
            max_power_gap_s=max(30.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_MAX_POWER_GAP_S", "180"))),
            max_meter_gap_s=max(60.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_MAX_METER_GAP_S", "7200"))),
            max_state_age_s=max(30.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_MAX_STATE_AGE_S", "7200"))),
            min_baseline_hours=max(1.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_BASELINE_MIN_HOURS", "24"))),
            min_coverage_pct=min(100.0, max(1.0, float(os.environ.get(
                "BREEZEIQ_ENERGY_MIN_COVERAGE_PCT", "90")))),
        )


def _timestamp(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _validated_numeric(reported, measurement: str, now: float,
                       max_state_age_s: float) -> tuple[Optional[dict], str]:
    if reported is None or not getattr(reported, "available", False):
        return None, getattr(reported, "detail", "entity unavailable")
    state = getattr(reported, "state", None)
    if not isinstance(state, Mapping) or "value" not in state:
        return None, "entity state is non-numeric"
    try:
        raw = float(state["value"])
    except (TypeError, ValueError):
        return None, "entity value is non-numeric"
    if not math.isfinite(raw) or raw < 0:
        return None, "entity value must be finite and non-negative"

    units = POWER_UNITS if measurement == "power" else ENERGY_UNITS
    unit = state.get("unit_of_measurement")
    if unit not in units:
        return None, f"unsupported {measurement} unit: {unit or 'missing'}"
    device_class = state.get("device_class")
    if device_class and device_class != measurement:
        return None, f"device_class is {device_class}, expected {measurement}"
    if measurement == "energy" and state.get("state_class") not in {
            "total", "total_increasing"}:
        return None, "energy entity must declare total or total_increasing"

    sample_at = _timestamp(state.get("last_updated"))
    if sample_at is None:
        return None, "Home Assistant last_updated is unavailable"
    age = now - sample_at
    if age < -60:
        return None, "Home Assistant sample timestamp is in the future"
    if age > max_state_age_s:
        return None, f"Home Assistant sample is stale by {round(age)} seconds"
    normalized_unit = "W" if measurement == "power" else "Wh"
    return {
        "value": raw * units[unit], "sample_at": sample_at,
        "unit": normalized_unit, "reported_unit": unit,
        "quality": "ha-class-verified" if device_class else "unit-verified",
        "state_class": state.get("state_class"),
    }, ""


class EnergyCollector:
    """Read HA meters without ever commanding an appliance."""

    def __init__(self, telemetry: Telemetry, backend,
                 config: Optional[EnergyConfig] = None):
        self.telemetry = telemetry
        self.backend = backend
        self.config = config or EnergyConfig.from_env()

    @property
    def configured(self) -> bool:
        return self.backend is not None and any(
            mapping.configured for mapping in self.config.mappings)

    def _gap(self, mapping: MeterMapping, entity_id: str, measurement: str,
             reason: str, at: float, start: Optional[float] = None) -> bool:
        return self.telemetry.energy(
            mapping.device, "measured", f"home_assistant:{entity_id}", at=at,
            interval_start=start, interval_end=at if start is not None else None,
            entity_id=entity_id, quality="gap", missing_reason=reason,
            detail={"measurement": measurement})

    def _power(self, mapping: MeterMapping, now: float,
               integrate: bool) -> dict:
        entity = mapping.power_entity
        if not entity:
            return {"measurement": "power", "state": "unconfigured"}
        reported = self.backend.read_numeric_entity(entity)
        sample, error = _validated_numeric(
            reported, "power", now, self.config.max_state_age_s)
        if sample is None:
            self._gap(mapping, entity, "power", error, now)
            return {"measurement": "power", "state": "gap", "detail": error}

        cursor = self.telemetry.energy_cursor(entity)
        sample_at, watts = sample["sample_at"], sample["value"]
        if cursor and sample_at <= cursor["sample_at"]:
            return {"measurement": "power", "state": "unchanged",
                    "watts": watts, "sample_at": sample_at}

        self.telemetry.energy(
            mapping.device, "measured", f"home_assistant:{entity}",
            at=sample_at, watts=watts, entity_id=entity,
            quality=sample["quality"],
            detail={"reported_unit": sample["reported_unit"]})
        derived_wh = None
        state = "measured"
        if integrate and cursor:
            elapsed = sample_at - cursor["sample_at"]
            if elapsed <= 0:
                state = "unchanged"
            elif elapsed > self.config.max_power_gap_s:
                self._gap(mapping, entity, "power",
                          f"power integration gap {round(elapsed)}s exceeds limit",
                          sample_at, cursor["sample_at"])
                state = "gap"
            else:
                derived_wh = (cursor["value"] + watts) * 0.5 * elapsed / 3600.0
                self.telemetry.energy(
                    mapping.device, "derived", "integrated_home_assistant_power",
                    at=sample_at, watt_hours=derived_wh,
                    interval_start=cursor["sample_at"], interval_end=sample_at,
                    entity_id=entity, quality="trapezoid_from_measured_watts",
                    detail={"start_w": cursor["value"], "end_w": watts,
                            "elapsed_s": elapsed})
                state = "integrated"
        self.telemetry.set_energy_cursor(
            entity, mapping.device, "power", sample_at, watts, "W",
            "home_assistant")
        return {"measurement": "power", "state": state,
                "watts": round(watts, 3),
                "watt_hours": round(derived_wh, 6) if derived_wh is not None else None,
                "sample_at": sample_at}

    def _energy(self, mapping: MeterMapping, now: float) -> dict:
        entity = mapping.energy_entity
        if not entity:
            return {"measurement": "energy", "state": "unconfigured"}
        reported = self.backend.read_numeric_entity(entity)
        sample, error = _validated_numeric(
            reported, "energy", now, self.config.max_state_age_s)
        if sample is None:
            self._gap(mapping, entity, "energy", error, now)
            return {"measurement": "energy", "state": "gap", "detail": error}

        cursor = self.telemetry.energy_cursor(entity)
        sample_at, total_wh = sample["sample_at"], sample["value"]
        if cursor and sample_at <= cursor["sample_at"]:
            return {"measurement": "energy", "state": "unchanged",
                    "total_wh": round(total_wh, 3), "sample_at": sample_at}

        state, delta_wh = "initialized", None
        if cursor:
            elapsed = sample_at - cursor["sample_at"]
            delta = total_wh - cursor["value"]
            if elapsed > self.config.max_meter_gap_s:
                self._gap(mapping, entity, "energy",
                          f"meter interval {round(elapsed)}s exceeds coverage limit",
                          sample_at, cursor["sample_at"])
                state = "gap"
            elif delta < -1e-6:
                self._gap(mapping, entity, "energy",
                          "cumulative meter decreased or reset",
                          sample_at, cursor["sample_at"])
                state = "reset"
            else:
                delta_wh = max(0.0, delta)
                self.telemetry.energy(
                    mapping.device, "measured", "home_assistant_cumulative_energy",
                    at=sample_at, watt_hours=delta_wh,
                    interval_start=cursor["sample_at"], interval_end=sample_at,
                    entity_id=entity, quality=sample["quality"],
                    detail={"start_total_wh": cursor["value"],
                            "end_total_wh": total_wh,
                            "reported_unit": sample["reported_unit"],
                            "state_class": sample["state_class"]})
                state = "measured"
        else:
            self._gap(mapping, entity, "energy", "meter cursor initialized",
                      sample_at)
        self.telemetry.set_energy_cursor(
            entity, mapping.device, "energy", sample_at, total_wh, "Wh",
            "home_assistant")
        return {"measurement": "energy", "state": state,
                "total_wh": round(total_wh, 3),
                "watt_hours": round(delta_wh, 6) if delta_wh is not None else None,
                "sample_at": sample_at}

    def collect(self, now: Optional[float] = None) -> dict:
        at = float(now if now is not None else time.time())
        if not self.configured:
            result = {"ok": False, "state": "unconfigured", "at": at,
                      "detail": "HA power or energy entity mapping is required",
                      "devices": []}
            self.telemetry.system_health("energy", "unconfigured", result)
            return result
        devices = []
        for mapping in self.config.mappings:
            if not mapping.configured:
                devices.append({"device": mapping.device,
                                "state": "unconfigured", "measurements": []})
                continue
            measurements = [self._power(
                mapping, at, integrate=not bool(mapping.energy_entity))]
            if mapping.energy_entity:
                measurements.append(self._energy(mapping, at))
            states = {item["state"] for item in measurements}
            devices.append({"device": mapping.device,
                            "state": "gap" if {"gap", "reset"} & states
                            else "ok", "measurements": measurements})
        ok = all(item["state"] == "ok" for item in devices
                 if item["device"] in self.config.required_devices)
        result = {"ok": ok, "state": "ok" if ok else "degraded",
                  "at": at, "devices": devices}
        self.telemetry.system_health("energy", result["state"], result)
        return result


class EnergyMonitor:
    """Run meter I/O beside the comfort loop so HA latency cannot delay safety."""

    def __init__(self, collector: EnergyCollector):
        self.collector = collector
        self.latest: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "EnergyMonitor":
        if not self.collector.configured:
            self.latest = self.collector.collect()
            return self
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="energy-monitor",
                                        daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.latest = self.collector.collect()
            except Exception as exc:
                self.latest = {"ok": False, "state": "failed",
                               "detail": f"{type(exc).__name__}: {exc}"}
            self._stop.wait(self.collector.config.sample_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def estimate_ac_first_savings(ticks: Iterable[Mapping[str, Any]],
                              ac_samples: Iterable[Mapping[str, Any]], *,
                              max_gap_s: float = ESTIMATE_MAX_GAP_S,
                              min_window_s: float = ESTIMATE_MIN_WINDOW_S) -> dict:
    """Compare logged BreezeIQ operation with an AC-first thermostat reference.

    This is an engineering estimate, never meter evidence.  Both sides use the
    same board-observed interval, occupancy, and modeled room temperature.  The
    reference compressor turns on above 25 C, off below 24 C, and never runs in
    an empty room.  BreezeIQ usage includes its modeled compressor interval and
    the configured Atomberg fan wattage.  Lighting is excluded from both sides,
    so it cannot inflate the cooling claim.

    Intervals with invalid/stale ticks, gaps over ``max_gap_s``, or comfort
    outside |PMV| <= 0.85 are excluded.  This makes the result conservative and
    reproducible from SQLite rather than extrapolating a short run to 24 hours.
    """
    tick_rows = sorted((dict(row) for row in ticks),
                       key=lambda row: float(row.get("at") or 0))
    samples = sorted((dict(row) for row in ac_samples),
                     key=lambda row: float(row.get("at") or 0))
    result = {
        "available": False, "used_wh": None, "reference_wh": None,
        "saved_wh": None, "savings_pct": None, "observed_hours": 0.0,
        "coverage_pct": None, "comfortable_pct": None,
        "method": "estimated_ac_first_thermostat_v1",
        "assumptions": {
            "reference": "occupied AC-first thermostat, on above 25 C and off below 24 C",
            "ac_w": ESTIMATE_AC_W, "fan_w": ESTIMATE_FAN_W,
            "lighting": "excluded from both sides",
        },
        "message": "At least five minutes of valid model history is required.",
    }
    if len(tick_rows) < 1 or len(samples) < 2:
        return result

    tick_index = 0
    reference_on = False
    used_wh = reference_wh = covered_s = comfortable_s = eligible_s = 0.0
    span_start = float(samples[0].get("at") or 0)
    span_end = float(samples[-1].get("at") or 0)
    for previous, current in zip(samples, samples[1:]):
        left = float(previous.get("at") or 0)
        right = float(current.get("at") or 0)
        elapsed = right - left
        if elapsed <= 0 or elapsed > max_gap_s:
            reference_on = False
            continue
        # The plan recorded at ``left`` governs the interval that follows it.
        # Do not use a tick written at ``right`` retroactively.
        while (tick_index + 1 < len(tick_rows)
               and float(tick_rows[tick_index + 1].get("at") or 0) <= left):
            tick_index += 1
        tick = tick_rows[tick_index]
        tick_age = left - float(tick.get("at") or 0)
        if tick_age < -1 or tick_age > max_gap_s or not bool(tick.get("valid", 1)):
            reference_on = False
            continue
        modeled = current.get("modeled_c")
        try:
            modeled_c = float(modeled)
            pmv = float(tick.get("pmv"))
        except (TypeError, ValueError):
            reference_on = False
            continue

        occupant = str(tick.get("occupant") or "").upper()
        occupied = occupant not in {"", "AWAY"}
        if not occupied:
            reference_on = False
        elif modeled_c > REFERENCE_ON_C:
            reference_on = True
        elif modeled_c < REFERENCE_OFF_C:
            reference_on = False

        eligible_s += elapsed
        comfortable = abs(pmv) <= 0.85
        if not comfortable:
            continue
        comfortable_s += elapsed
        covered_s += elapsed
        fan_w = ESTIMATE_FAN_W.get(int(tick.get("fan") or 0), 0.0)
        actual_ac_wh = current.get("interval_wh")
        try:
            actual_ac_wh = float(actual_ac_wh or 0.0)
        except (TypeError, ValueError):
            actual_ac_wh = 0.0
        # Historical response rows may contain decaying thermal effect after
        # power-off. Residual cooling consumes no compressor electricity.
        if not bool(current.get("power")):
            actual_ac_wh = 0.0
        used_wh += max(0.0, actual_ac_wh) + fan_w * elapsed / 3600.0
        if reference_on:
            reference_wh += ESTIMATE_AC_W * elapsed / 3600.0

    span_s = max(0.0, span_end - span_start)
    result["observed_hours"] = round(covered_s / 3600.0, 2)
    result["coverage_pct"] = round(
        covered_s / span_s * 100.0, 1) if span_s else None
    result["comfortable_pct"] = round(
        comfortable_s / eligible_s * 100.0, 1) if eligible_s else None
    if covered_s < min_window_s:
        return result
    saved_wh = reference_wh - used_wh
    result.update({
        "available": True,
        "used_wh": round(used_wh, 2),
        "reference_wh": round(reference_wh, 2),
        "saved_wh": round(saved_wh, 2),
        "savings_pct": (round(saved_wh / reference_wh * 100.0, 1)
                        if reference_wh > 0 else None),
        "message": (
            "Estimated over valid, comfortable board-observed intervals versus "
            "an occupied AC-first 24-25 C thermostat reference. Not meter-verified."
        ),
    })
    return result


def _merge_seconds(intervals: Iterable[tuple[float, float]],
                   start: float, end: float) -> float:
    merged: list[list[float]] = []
    for left, right in sorted(intervals):
        left, right = max(start, left), min(end, right)
        if right <= left:
            continue
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    return sum(right - left for left, right in merged)


def summarize_intervals(rows: Iterable[Mapping[str, Any]], start: float,
                        end: float, devices: Iterable[str]) -> dict:
    """Summarize verified Wh rows and refuse overlapping claims."""
    required = tuple(dict.fromkeys(devices))
    duration = end - start
    if duration <= 0 or not required:
        return {"ok": False, "error": "positive window and devices are required"}
    totals, coverage, counts = {}, {}, {}
    for device in required:
        selected = []
        for row in rows:
            if row.get("device") != device or row.get("watt_hours") is None:
                continue
            if row.get("provenance") not in VERIFIED_PROVENANCE:
                continue
            left, right = row.get("interval_start"), row.get("interval_end")
            if left is None or right is None:
                continue
            left, right = float(left), float(right)
            if left < start or right > end or right <= left:
                continue
            selected.append((left, right, float(row["watt_hours"])))
        selected.sort()
        previous_end = None
        for left, right, _ in selected:
            if previous_end is not None and left < previous_end - 1e-6:
                return {"ok": False,
                        "error": f"overlapping energy intervals for {device}"}
            previous_end = right
        totals[device] = sum(item[2] for item in selected)
        coverage[device] = round(
            _merge_seconds(((x[0], x[1]) for x in selected), start, end)
            / duration * 100.0, 2)
        counts[device] = len(selected)
    return {
        "ok": True, "watt_hours": round(sum(totals.values()), 6),
        "by_device_wh": totals, "coverage_by_device": coverage,
        "coverage": min(coverage.values()) if coverage else 0.0,
        "sample_count": sum(counts.values()), "sample_count_by_device": counts,
        "duration_s": duration, "devices": list(required),
    }


def read_energy_rows(db_path: str, start: float, end: float) -> list[dict]:
    path = Path(db_path)
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='energy_sample'"
        ).fetchone()
        if not exists:
            return []
        return [dict(row) for row in con.execute(
            "SELECT interval_start,interval_end,device,watt_hours,provenance,"
            "quality,source,entity_id FROM energy_sample "
            "WHERE interval_start>=? AND interval_end<=? AND watt_hours IS NOT NULL "
            "ORDER BY device,interval_start", (start, end))]


def create_baseline(telemetry: Telemetry, name: str, start: float, end: float,
                    occupancy_basis: str, devices: Iterable[str], *,
                    min_hours: float = 24.0,
                    min_coverage_pct: float = 90.0) -> dict:
    required = tuple(dict.fromkeys(item.strip() for item in devices if item.strip()))
    duration_h = (end - start) / 3600.0
    if duration_h < min_hours:
        return {"ok": False,
                "error": f"baseline requires at least {min_hours:g} hours"}
    rows = read_energy_rows(str(telemetry.path), start, end)
    summary = summarize_intervals(rows, start, end, required)
    if not summary.get("ok"):
        return summary
    if summary["coverage"] < min_coverage_pct:
        return {"ok": False,
                "error": (f"baseline coverage {summary['coverage']:.1f}% is below "
                          f"{min_coverage_pct:.1f}%"), **summary}
    if summary["watt_hours"] <= 0:
        return {"ok": False, "error": "baseline has no positive verified usage",
                **summary}
    source_query = (
        "energy_sample intervals fully inside window; provenance in "
        "('measured','derived'); estimated rows excluded")
    missing_rule = (
        f"each required device must have at least {min_coverage_pct:.1f}% "
        "non-overlapping interval coverage")
    saved = telemetry.energy_baseline(
        name, start, end, occupancy_basis, source_query, missing_rule,
        watt_hours=summary["watt_hours"], coverage=summary["coverage"],
        sample_count=summary["sample_count"], devices=list(required),
        uncertainty={"coverage_by_device": summary["coverage_by_device"],
                     "by_device_wh": summary["by_device_wh"],
                     "method": "measured cumulative meter or integrated measured watts"})
    return {"ok": bool(saved), "name": name, **summary}


def _parse_time(value: str) -> float:
    parsed = _timestamp(value)
    if parsed is None:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                "use Unix seconds or ISO-8601 with timezone") from exc
    return parsed


def _backend():
    from devices.assistant import HomeAssistantBackend
    return HomeAssistantBackend(
        base_url=os.environ.get("HA_URL", "http://127.0.0.1:8123"),
        token=os.environ.get("HA_TOKEN", ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="perform one read-only HA meter collection")
    sub.add_parser("discover", help="list HA power and energy entities")
    baseline = sub.add_parser("baseline", help="record a verified comparison window")
    baseline.add_argument("--name", required=True)
    baseline.add_argument("--start", type=_parse_time, required=True)
    baseline.add_argument("--end", type=_parse_time, required=True)
    baseline.add_argument("--occupancy-basis", required=True)
    baseline.add_argument("--devices", default=",")
    args = parser.parse_args(argv)
    config = EnergyConfig.from_env()
    db = os.environ.get("BREEZEIQ_DB", "telemetry.sqlite3")
    if args.command == "discover":
        print(json.dumps(_backend().measurement_entities(), indent=2,
                         sort_keys=True))
        return 0
    with Telemetry(db) as telemetry:
        if args.command == "collect":
            result = EnergyCollector(telemetry, _backend(), config).collect()
        else:
            devices = tuple(item.strip() for item in args.devices.split(",")
                            if item.strip()) or config.required_devices
            result = create_baseline(
                telemetry, args.name, args.start, args.end,
                args.occupancy_basis, devices,
                min_hours=config.min_baseline_hours,
                min_coverage_pct=config.min_coverage_pct)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
