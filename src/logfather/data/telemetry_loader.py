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
