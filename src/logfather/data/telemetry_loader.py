"""Fetch one system's telemetry for one day through Grafana (six PromQL
queries, one per metric), returning the grouped tracks the panel draws."""
from __future__ import annotations

from datetime import date

from logfather.core.telemetry import METRICS, SAMPLE_INTERVAL_MS, TelemetryDay, build_groups, robot_query
from logfather.core.timeline_model import local_day_end_utc, local_day_start_utc
from logfather.data import grafana_client
from logfather.data.settings_store import Settings


def fetch_telemetry_day(settings: Settings, robot_id: str, day: date, job=None) -> TelemetryDay:
    start_ms = int(local_day_start_utc(day).timestamp() * 1000)
    end_ms = int(local_day_end_utc(day).timestamp() * 1000)
    results = {}
    for spec in METRICS:
        if job is not None and job.interrupted():
            break
        results[spec.key] = grafana_client.query_series(
            settings,
            grafana_client.TELEMETRY_DATASOURCE,
            robot_query(spec, robot_id),
            start_ms,
            end_ms,
            interval_ms=SAMPLE_INTERVAL_MS,
            max_points=4000,
        )
    return TelemetryDay(robot_id=robot_id, day_start_ms=start_ms, day_end_ms=end_ms, groups=build_groups(results))


def fetch_fleet_temperatures(settings: Settings, keys: list[str], start_utc, end_utc, job=None) -> dict[str, dict[str, "Track"]]:
    """robot id -> metric key -> Track for the chosen temperature keys over
    [start, end], every system in one query per metric and label scheme
    (Chris, 2026-09-07: temperatures on the Overview)."""
    from logfather.core.telemetry import ROBOT_ID_LABELS, TEMPERATURE_CHOICES, Track, fleet_query, parse_temperature_key, tracks_by_robot

    specs = {m.key: m for m in METRICS}
    labels = dict(TEMPERATURE_CHOICES)
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000)
    span_minutes = max(1, (end_ms - start_ms) // 60_000)
    interval_ms = SAMPLE_INTERVAL_MS if span_minutes <= 36 * 60 else 60_000 if span_minutes <= 7 * 24 * 60 else 300_000
    out: dict[str, dict[str, Track]] = {}
    for key in keys:
        spec_key, motor_id = parse_temperature_key(key)
        spec = specs.get(spec_key)
        if spec is None:
            continue
        for label in ROBOT_ID_LABELS:
            if job is not None and job.interrupted():
                return out
            series = grafana_client.query_series(
                settings, grafana_client.TELEMETRY_DATASOURCE, fleet_query(spec, label, motor_id),
                start_ms, end_ms, interval_ms=interval_ms, max_points=min(6000, span_minutes * 2 + 10), timeout=120,
            )
            for robot, track in tracks_by_robot(series, label, labels.get(key, key), key).items():
                slot = out.setdefault(robot, {})
                existing = slot.get(key)
                if existing is None:
                    slot[key] = track
                else:
                    merged_t = sorted(set(existing.times_ms) | set(track.times_ms))
                    lookup = dict(zip(existing.times_ms, existing.values))
                    lookup.update({t: v for t, v in zip(track.times_ms, track.values) if v is not None})
                    slot[key] = Track(track.name, merged_t, [lookup.get(t) for t in merged_t], key)
    return out
