"""Running totals of a logged event per system, from Elastic, for the
Overview's Additional data strips (Chris, 2026-09-11: motor over-current
trips plotted next to temperatures and currents). One date_histogram of
the matching lines, bucketed per robot, turned into a staircase track per
robot: the count so far in the loaded span, stepping up at each event.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from logfather.core.telemetry import Track, normalise_robot_id
from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import KIBANA_BASE_DEFAULT, _normalize_index_id, _search_url, condition_clause
from logfather.data.settings_store import Settings

# signal key -> (free-text search, as a timeline condition would use, track name)
EVENT_SIGNALS: dict[str, tuple[str, str]] = {
    "motor_overcurrent": ('"Current over limit"', "Motor overcurrent trips"),
}
BUCKET_SECONDS = 20


def parse_event_buckets(buckets: list, key: str, name: str, start_ms: int, end_ms: int, step_ms: int = BUCKET_SECONDS * 1000) -> dict[str, Track]:
    """robot -> staircase Track of the running total: 0 at the span start,
    a flat step into each event bucket, the total held to the span end."""
    per_robot: dict[str, dict[int, int]] = {}
    for bucket in buckets or []:
        t_ms = int(bucket.get("key") or 0)
        for agg in ("per_robot", "per_system_id"):
            for sub in ((bucket.get(agg) or {}).get("buckets") or []):
                robot = normalise_robot_id(str(sub.get("key") or ""))
                n = int(sub.get("doc_count") or 0)
                if robot and n:
                    slot = per_robot.setdefault(robot, {})
                    slot[t_ms] = slot.get(t_ms, 0) + n
    out: dict[str, Track] = {}
    for robot, counts in per_robot.items():
        times: list[int] = [start_ms]
        values: list[float] = [0.0]
        total = 0
        for t in sorted(counts):
            if t - step_ms > times[-1]:
                times.append(t - step_ms)
                values.append(float(total))
            total += counts[t]
            times.append(max(t, times[-1] + 1))
            values.append(float(total))
        if end_ms > times[-1]:
            times.append(end_ms)
            values.append(float(total))
        out[robot] = Track(name, times, values, key)
    return out


def fetch_elastic_event_counts(settings: Settings, key: str, start_utc: datetime, end_utc: datetime) -> dict[str, Track]:
    spec = EVENT_SIGNALS.get(key)
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if spec is None or not url_base or not api_key:
        return {}
    query, name = spec
    start_iso = start_utc.astimezone(timezone.utc).isoformat()
    end_iso = end_utc.astimezone(timezone.utc).isoformat()
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {
            "filter": [{"range": {"@timestamp_ros": {"gte": start_iso, "lte": end_iso}}}],
            "must": [condition_clause(query)],
        }},
        "aggs": {"per_bucket": {
            "date_histogram": {"field": "@timestamp_ros", "fixed_interval": f"{BUCKET_SECONDS}s", "min_doc_count": 1},
            "aggs": {
                "per_robot": {"terms": {"field": "leap_robot_id.keyword", "size": 500}},
                "per_system_id": {"terms": {"field": "system_id.keyword", "size": 500}},
            },
        }},
    }
    resp = requests.post(_search_url(url_base, _normalize_index_id(None)), json=body, headers=api_headers(api_key), timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"Elastic HTTP {resp.status_code}: {resp.text[:200]}")
    buckets = ((resp.json().get("aggregations") or {}).get("per_bucket") or {}).get("buckets") or []
    return parse_event_buckets(buckets, key, name, int(start_utc.timestamp() * 1000), int(end_utc.timestamp() * 1000))
