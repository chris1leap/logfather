"""Fleet data inventory: how much data exists, per system per day.

Two independent sources, each collected by its own worker so the Data
window can show whichever arrives first (Chris, 2026-09-05):

- Elastic: document counts per day per robot from a date_histogram with
  a terms sub-aggregation (no documents are fetched), plus the total
  document count and the oldest document in the index pattern.
- CCTV share: clip counts per day per system from the day listings (the
  past-day listing cache applies), with bytes ESTIMATED from one stat per
  day-folder - stat'ing every clip over the WAN would take many minutes.

Robot ids and system folders are related by elastic_schema
(PikPak012 <-> 35-2300-012); the window joins the two on that.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import (
    KIBANA_BASE_DEFAULT,
    _normalize_index_id,
    _search_url,
)
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.data.settings_store import Settings

INVENTORY_DAYS = 14

# Terms-aggregation field candidates, tried in order: keyword sub-fields
# first (a text field without fielddata rejects the aggregation).
_ROBOT_FIELD_CANDIDATES = (
    ("leap_robot_id.keyword", "system_id.keyword"),
    ("leap_robot_id", "system_id"),
)

_YEAR_RE = re.compile(r"^\d{4}$")
_MONTH_DAY_RE = re.compile(r"^\d{2}$")

ProgressFn = Callable[[str], None]
InterruptedFn = Callable[[], bool]


@dataclass
class ElasticInventory:
    days: list[date]
    counts: dict[str, dict[date, int]] = field(default_factory=dict)  # robot -> day -> docs
    total_docs: int | None = None
    oldest_ts: datetime | None = None
    # Storage: real index store size when the API key may read index
    # stats, else an estimate from sampled document JSON. bytes_per_doc
    # apportions either onto the per-day/per-robot document counts.
    total_bytes: int | None = None
    bytes_per_doc: float | None = None
    bytes_basis: str = ""


@dataclass
class CctvInventory:
    days: list[date]
    clips: dict[str, dict[date, int]] = field(default_factory=dict)  # system -> day -> clips
    est_bytes: dict[str, dict[date, int]] = field(default_factory=dict)
    oldest_day: dict[str, date | None] = field(default_factory=dict)
    day_folders: dict[str, int] = field(default_factory=dict)
    robot_ids: dict[str, str | None] = field(default_factory=dict)  # system -> robot


# ------------------------------------------------------------- pure logic


def inventory_days(today: date, days: int = INVENTORY_DAYS) -> list[date]:
    """The trailing window ending today, oldest first."""
    span = max(1, int(days))
    return [today - timedelta(days=span - 1 - i) for i in range(span)]


def parse_histogram(
    per_day_buckets: list, days: list[date], sub_aggs: tuple[str, ...] = ("per_robot", "per_system_id")
) -> dict[str, dict[date, int]]:
    """Fold an Elastic date_histogram (with robot terms sub-aggs) into
    robot -> day -> doc count, keeping only the requested days. A document
    carries its id in ONE of the two fields, so summing both sub-aggs does
    not double count."""
    wanted = set(days)
    counts: dict[str, dict[date, int]] = defaultdict(lambda: defaultdict(int))
    for bucket in per_day_buckets or []:
        key = str(bucket.get("key_as_string") or "")
        try:
            day = date.fromisoformat(key[:10])
        except ValueError:
            continue
        if day not in wanted:
            continue
        for agg_name in sub_aggs:
            agg = bucket.get(agg_name) or {}
            for sub in agg.get("buckets") or []:
                robot = str(sub.get("key") or "").strip()
                if not robot:
                    continue
                counts[robot][day] += int(sub.get("doc_count") or 0)
    return {robot: dict(per_day) for robot, per_day in counts.items()}


def sum_cat_indices(rows: list) -> tuple[int, int]:
    """(store bytes, docs) summed over _cat/indices JSON rows (bytes=b)."""
    store = docs = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            store += int(row.get("store.size") or 0)
            docs += int(row.get("docs.count") or 0)
        except (TypeError, ValueError):
            continue
    return store, docs


def mean_source_bytes(hits: list) -> float | None:
    """Average compact-JSON size of the hits' _source, or None."""
    sizes = [
        len(json.dumps(hit.get("_source") or {}, separators=(",", ":")))
        for hit in hits or []
        if isinstance(hit, dict)
    ]
    return (sum(sizes) / len(sizes)) if sizes else None


def estimate_bytes(clip_count: int, sample_size: int | None) -> int:
    """Bytes for a day-folder from its clip count and one sampled size."""
    if clip_count <= 0 or not sample_size or sample_size <= 0:
        return 0
    return int(clip_count) * int(sample_size)


def format_bytes(n: int | float | None) -> str:
    if not n or n <= 0:
        return "0 B"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_count(n: int | float | None) -> str:
    if not n:
        return "0"
    value = float(n)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}k"
    return f"{int(value):,}"


def _tz_offset_string(now_local: datetime) -> str:
    raw = now_local.strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}"


# ----------------------------------------------------------------- Elastic


def fetch_elastic_inventory(
    settings: Settings,
    days: int = INVENTORY_DAYS,
    progress: ProgressFn | None = None,
) -> ElasticInventory:
    now_local = datetime.now().astimezone()
    day_list = inventory_days(now_local.date(), days)
    inventory = ElasticInventory(days=day_list)
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    url = _search_url(url_base, _normalize_index_id(None))
    headers = api_headers(api_key)

    def _post(body: dict, timeout: int) -> dict:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    if progress:
        progress("Elastic: counting documents per day and system...")
    start_iso = datetime.combine(day_list[0], dt_time.min).astimezone().isoformat()
    last_error: Exception | None = None
    for robot_field, system_field in _ROBOT_FIELD_CANDIDATES:
        body = {
            "size": 0,
            "track_total_hits": False,
            "query": {"range": {"@timestamp": {"gte": start_iso, "lte": now_local.isoformat()}}},
            "aggs": {
                "per_day": {
                    "date_histogram": {
                        "field": "@timestamp",
                        "calendar_interval": "1d",
                        "time_zone": _tz_offset_string(now_local),
                        "min_doc_count": 1,
                    },
                    "aggs": {
                        "per_robot": {"terms": {"field": robot_field, "size": 500}},
                        "per_system_id": {"terms": {"field": system_field, "size": 500}},
                    },
                }
            },
        }
        try:
            data = _post(body, timeout=90)
        except RuntimeError as exc:
            last_error = exc
            if "HTTP 400" in str(exc):
                continue
            raise
        buckets = ((data.get("aggregations") or {}).get("per_day") or {}).get("buckets") or []
        inventory.counts = parse_histogram(buckets, day_list)
        last_error = None
        break
    if last_error is not None:
        raise last_error

    if progress:
        progress("Elastic: total document count and oldest record...")
    try:
        total = _post({"size": 0, "track_total_hits": True}, timeout=60)
        inventory.total_docs = int(((total.get("hits") or {}).get("total") or {}).get("value") or 0)
    except Exception:
        inventory.total_docs = None
    try:
        oldest = _post(
            {"size": 1, "sort": [{"@timestamp": "asc"}], "_source": ["@timestamp"]},
            timeout=90,
        )
        hits = ((oldest.get("hits") or {}).get("hits") or [])
        if hits:
            raw = str(hits[0].get("_source", {}).get("@timestamp") or "")
            inventory.oldest_ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        inventory.oldest_ts = None

    if progress:
        progress("Elastic: storage size...")
    # Real store size needs the monitor / view_index_metadata index
    # privilege; the search-only key gets 403, hence the sampled fallback.
    es_root = url.split("/_search")[0].rsplit("/", 1)[0]
    pattern = _normalize_index_id(None)
    try:
        resp = requests.get(
            f"{es_root}/_cat/indices/{pattern}?format=json&h=docs.count,store.size&bytes=b",
            headers=headers,
            timeout=60,
        )
        if resp.status_code == 200:
            store, docs = sum_cat_indices(resp.json())
            if store > 0 and docs > 0:
                inventory.total_bytes = store
                inventory.bytes_per_doc = store / docs
                inventory.bytes_basis = "index store size"
    except Exception:
        pass
    if inventory.bytes_per_doc is None:
        try:
            sample = _post(
                {"size": 300, "sort": [{"@timestamp": "desc"}], "track_total_hits": False},
                timeout=60,
            )
            avg = mean_source_bytes(((sample.get("hits") or {}).get("hits") or []))
            if avg:
                inventory.bytes_per_doc = avg
                inventory.bytes_basis = (
                    "estimated from sampled document JSON - the API key lacks the "
                    "index-stats privilege for real store sizes"
                )
                if inventory.total_docs:
                    inventory.total_bytes = int(inventory.total_docs * avg)
        except Exception:
            pass
    return inventory


# -------------------------------------------------------------- CCTV share


def _folder_span(system_root: Path) -> tuple[date | None, int]:
    """Oldest day folder and total day-folder count from the shallow
    year/month/day tree (three listings deep, no clip listings)."""
    oldest: date | None = None
    total = 0
    try:
        years = sorted(p for p in system_root.iterdir() if p.is_dir() and _YEAR_RE.match(p.name))
    except OSError:
        return None, 0
    for year_dir in years:
        try:
            months = sorted(p for p in year_dir.iterdir() if p.is_dir() and _MONTH_DAY_RE.match(p.name))
        except OSError:
            continue
        for month_dir in months:
            try:
                day_dirs = sorted(p for p in month_dir.iterdir() if p.is_dir() and _MONTH_DAY_RE.match(p.name))
            except OSError:
                continue
            total += len(day_dirs)
            if oldest is None and day_dirs:
                try:
                    oldest = date(int(year_dir.name), int(month_dir.name), int(day_dirs[0].name))
                except ValueError:
                    oldest = None
    return oldest, total


def scan_cctv_inventory(
    parent_dir: Path,
    days: int = INVENTORY_DAYS,
    progress: ProgressFn | None = None,
    interrupted: InterruptedFn | None = None,
) -> CctvInventory:
    today = datetime.now().date()
    day_list = inventory_days(today, days)
    inventory = CctvInventory(days=day_list)
    try:
        system_roots = sorted(
            (p for p in parent_dir.iterdir() if p.is_dir()), key=lambda p: p.name.lower()
        )
    except OSError as exc:
        raise RuntimeError(f"cannot list {parent_dir}: {exc}") from exc
    total_systems = len(system_roots)
    for idx, root in enumerate(system_roots, start=1):
        if interrupted and interrupted():
            break
        name = root.name
        if progress:
            progress(f"CCTV: {name} ({idx}/{total_systems}) — listing {len(day_list)} day folders...")
        inventory.robot_ids[name] = robot_id_from_folder(name)
        oldest, folder_count = _folder_span(root)
        inventory.oldest_day[name] = oldest
        inventory.day_folders[name] = folder_count
        clips: dict[date, int] = {}
        est: dict[date, int] = {}
        for day_value in day_list:
            if interrupted and interrupted():
                break
            try:
                paths = list(load_day_files_cached(root, day_value))
            except Exception:
                paths = []
            count = len(paths)
            clips[day_value] = count
            sample_size: int | None = None
            if paths:
                # One stat per day-folder: the middle clip, so a truncated
                # first/last clip does not skew the estimate.
                try:
                    sample_size = int(paths[len(paths) // 2].stat().st_size)
                except OSError:
                    sample_size = None
            est[day_value] = estimate_bytes(count, sample_size)
        inventory.clips[name] = clips
        inventory.est_bytes[name] = est
    return inventory
