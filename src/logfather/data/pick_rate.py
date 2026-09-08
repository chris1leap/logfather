"""Picks per minute from Elastic, for the Overview's Picks strip on systems
Grafana does not cover (Argus 1 has no targeting metrics there; Chris,
2026-09-08). One date_histogram of the pick messages, bucketed per robot,
turned into a per-minute rate track per robot.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from logfather.core.telemetry import Track, normalise_robot_id
from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import KIBANA_BASE_DEFAULT, _normalize_index_id, _search_url
from logfather.data.settings_store import Settings

PICK_PHRASES = ("Picking products", "Successfully planned pick")
SMOOTH_MINUTES = 5


def bucket_minutes(span_minutes: int) -> int:
    """Finer buckets for a day, coarser for long spans (bucket count stays
    in the low thousands)."""
    if span_minutes <= 2 * 24 * 60:
        return 1
    if span_minutes <= 14 * 24 * 60:
        return 5
    return 30


def parse_pick_buckets(buckets: list, minutes_per_bucket: int, smooth: int = SMOOTH_MINUTES) -> dict[str, Track]:
    """robot -> Track of picks per minute, one point per bucket, smoothed
    with a trailing window of `smooth` minutes so a rate reads steadily."""
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
    step_ms = minutes_per_bucket * 60_000
    window = max(1, smooth // minutes_per_bucket)
    for robot, counts in per_robot.items():
        times = sorted(counts)
        # fill the gaps between the first and last bucket with zeros so idle
        # minutes read as 0 rather than as a hole
        filled: list[int] = []
        t = times[0]
        while t <= times[-1]:
            filled.append(t)
            t += step_ms
        values: list[float] = []
        history: list[int] = []
        for t in filled:
            history.append(counts.get(t, 0))
            recent = history[-window:]
            values.append(sum(recent) / (len(recent) * minutes_per_bucket))
        out[robot] = Track("Picks per minute", filled, values, "picks_per_min")
    return out


def fetch_elastic_pick_rate(settings: Settings, start_utc: datetime, end_utc: datetime) -> dict[str, Track]:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        return {}
    span_minutes = max(1, int((end_utc - start_utc).total_seconds() // 60))
    minutes = bucket_minutes(span_minutes)
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {
            "filter": [{"range": {"@timestamp": {"gte": start_utc.astimezone(timezone.utc).isoformat(), "lte": end_utc.astimezone(timezone.utc).isoformat()}}}],
            "should": [{"match_phrase": {"message": phrase}} for phrase in PICK_PHRASES],
            "minimum_should_match": 1,
        }},
        "aggs": {"per_min": {
            "date_histogram": {"field": "@timestamp", "fixed_interval": f"{minutes}m", "min_doc_count": 1},
            "aggs": {
                "per_robot": {"terms": {"field": "leap_robot_id.keyword", "size": 500}},
                "per_system_id": {"terms": {"field": "system_id.keyword", "size": 500}},
            },
        }},
    }
    resp = requests.post(_search_url(url_base, _normalize_index_id(None)), json=body, headers=api_headers(api_key), timeout=90)
    if resp.status_code != 200:
        raise RuntimeError(f"Elastic HTTP {resp.status_code}: {resp.text[:200]}")
    buckets = ((resp.json().get("aggregations") or {}).get("per_min") or {}).get("buckets") or []
    return parse_pick_buckets(buckets, minutes)
