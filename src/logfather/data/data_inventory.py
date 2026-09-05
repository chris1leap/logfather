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
import os
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import (
    ELASTIC_INDEX_PATTERN,
    KIBANA_BASE_DEFAULT,
    _normalize_index_id,
    _search_url,
)
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.data.settings_store import Settings

INVENTORY_DAYS = 14

# Local cache of the Elastic inventory (Chris, 2026-09-05): finished days
# never change, so a refresh only queries the days not yet counted (today
# is always re-counted). Sampled document sizes are kept for a week.
INVENTORY_CACHE_SCHEMA = 1
INVENTORY_CACHE_KEEP_DAYS = 60
SAMPLE_MAX_AGE = timedelta(days=7)

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
    # Per-robot sampled average document size: systems differ a lot
    # (planner payloads vs heartbeats), so a single fleet factor would
    # just redraw the document chart at another scale.
    bytes_per_doc_by_robot: dict[str, float] = field(default_factory=dict)

    def bytes_factor(self, robot: str) -> float:
        value = self.bytes_per_doc_by_robot.get(robot)
        if value:
            return float(value)
        return float(self.bytes_per_doc or 0.0)


@dataclass
class CctvInventory:
    days: list[date]
    clips: dict[str, dict[date, int]] = field(default_factory=dict)  # system -> day -> clips
    est_bytes: dict[str, dict[date, int]] = field(default_factory=dict)
    oldest_day: dict[str, date | None] = field(default_factory=dict)
    day_folders: dict[str, int] = field(default_factory=dict)
    robot_ids: dict[str, str | None] = field(default_factory=dict)  # system -> robot


# ------------------------------------------------------------- pure logic


def _default_inventory_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache" / "data_inventory.json"
    return Path.home() / ".videolog_cache" / "data_inventory.json"


def load_inventory_cache(path: Path | None = None) -> dict | None:
    p = path if path is not None else _default_inventory_cache_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != INVENTORY_CACHE_SCHEMA:
        return None
    return data


def save_inventory_cache(cache: dict, path: Path | None = None) -> bool:
    p = path if path is not None else _default_inventory_cache_path()
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def cached_day_counts(cache: dict | None, days: list[date]) -> tuple[dict[str, dict[date, int]], set[date]]:
    """(robot -> day -> docs, the days of `days` the cache holds complete).
    A day is complete when it was counted on a later day."""
    if not cache:
        return {}, set()
    complete: set[date] = set()
    for raw in cache.get("complete_days") or []:
        try:
            complete.add(date.fromisoformat(str(raw)))
        except ValueError:
            continue
    wanted = complete & set(days)
    counts: dict[str, dict[date, int]] = {}
    for robot, per_day in (cache.get("counts") or {}).items():
        if not isinstance(per_day, dict):
            continue
        for raw, n in per_day.items():
            try:
                day = date.fromisoformat(str(raw))
            except ValueError:
                continue
            if day in wanted:
                counts.setdefault(str(robot), {})[day] = int(n or 0)
    return counts, wanted


def days_to_fetch(days: list[date], complete: set[date]) -> list[date]:
    """The trailing run of days from the first one not in the cache; the
    histogram is one range query, so a gap means everything after it."""
    for i, day in enumerate(days):
        if day not in complete:
            return list(days[i:])
    return [days[-1]] if days else []


def cached_samples(cache: dict | None, now: datetime) -> dict[str, float]:
    """Per-robot sampled bytes/doc still fresh enough to reuse."""
    if not cache:
        return {}
    try:
        sampled_at = datetime.fromisoformat(str(cache.get("sampled_at") or ""))
    except ValueError:
        return {}
    if sampled_at.tzinfo is None:
        sampled_at = sampled_at.replace(tzinfo=timezone.utc)
    if now.astimezone(timezone.utc) - sampled_at.astimezone(timezone.utc) > SAMPLE_MAX_AGE:
        return {}
    out: dict[str, float] = {}
    for robot, value in (cache.get("sampled") or {}).items():
        try:
            if float(value) > 0:
                out[str(robot)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def merge_inventory_cache(
    cache: dict | None,
    inventory: "ElasticInventory",
    fetched_days: list[date],
    today: date,
    sampled: dict[str, float],
    sampled_at: datetime,
) -> dict:
    """The cache after this fetch: counts for every known day (pruned to
    the keep window), the days now complete, the oldest record and the
    sample set."""
    old = cache if isinstance(cache, dict) else {}
    counts: dict[str, dict[str, int]] = {}
    for robot, per_day in (old.get("counts") or {}).items():
        if isinstance(per_day, dict):
            counts[str(robot)] = {str(k): int(v or 0) for k, v in per_day.items()}
    fetched = set(fetched_days)
    # A fetched day replaces whatever the cache had for it (a robot with
    # no documents that day loses its stale partial count).
    for robot in counts:
        for day in fetched:
            counts[robot].pop(day.isoformat(), None)
    for robot, per_day in inventory.counts.items():
        target = counts.setdefault(robot, {})
        for day, n in per_day.items():
            target[day.isoformat()] = int(n)
    complete = set()
    for raw in old.get("complete_days") or []:
        try:
            complete.add(date.fromisoformat(str(raw)))
        except ValueError:
            continue
    complete |= {d for d in fetched if d < today}
    floor = today - timedelta(days=INVENTORY_CACHE_KEEP_DAYS)
    complete = {d for d in complete if d >= floor}
    for robot in list(counts):
        counts[robot] = {k: v for k, v in counts[robot].items() if k >= floor.isoformat()}
        if not counts[robot]:
            counts.pop(robot)
    return {
        "schema": INVENTORY_CACHE_SCHEMA,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "complete_days": sorted(d.isoformat() for d in complete),
        "oldest_ts": inventory.oldest_ts.isoformat() if inventory.oldest_ts else old.get("oldest_ts"),
        "sampled": {r: float(v) for r, v in sampled.items()},
        "sampled_at": sampled_at.astimezone(timezone.utc).isoformat(),
        "total_docs": inventory.total_docs if inventory.total_docs is not None else old.get("total_docs"),
        "total_bytes": inventory.total_bytes if inventory.total_bytes is not None else old.get("total_bytes"),
        "bytes_per_doc": inventory.bytes_per_doc if inventory.bytes_per_doc is not None else old.get("bytes_per_doc"),
        "bytes_basis": inventory.bytes_basis or str(old.get("bytes_basis") or ""),
    }


def cache_saved_at(cache: dict | None) -> datetime | None:
    """When the cache was last written, as local time."""
    try:
        raw = str((cache or {}).get("saved_at") or "")
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone()


def inventory_from_cache(cache: dict | None, days: list[date]) -> "ElasticInventory | None":
    """An inventory built only from the cache (Chris, 2026-09-05: the Data
    window opens on the last saved figures and says when they date from;
    Refresh fetches what has changed). None when the cache is empty."""
    if not cache or not cache.get("counts"):
        return None
    wanted = set(days)
    inventory = ElasticInventory(days=list(days))
    for robot, per_day in (cache.get("counts") or {}).items():
        if not isinstance(per_day, dict):
            continue
        for raw, n in per_day.items():
            try:
                day = date.fromisoformat(str(raw))
            except ValueError:
                continue
            if day in wanted:
                inventory.counts.setdefault(str(robot), {})[day] = int(n or 0)
    try:
        raw = str(cache.get("oldest_ts") or "")
        inventory.oldest_ts = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
    except ValueError:
        inventory.oldest_ts = None
    inventory.total_docs = int(cache["total_docs"]) if cache.get("total_docs") is not None else None
    inventory.total_bytes = int(cache["total_bytes"]) if cache.get("total_bytes") is not None else None
    inventory.bytes_per_doc = float(cache["bytes_per_doc"]) if cache.get("bytes_per_doc") is not None else None
    inventory.bytes_basis = str(cache.get("bytes_basis") or "")
    sampled = {}
    for robot, value in (cache.get("sampled") or {}).items():
        try:
            if float(value) > 0:
                sampled[str(robot)] = float(value)
        except (TypeError, ValueError):
            continue
    window_docs = {r: sum(v.values()) for r, v in inventory.counts.items()}
    real = inventory.bytes_per_doc if inventory.bytes_basis.startswith("index store") else None
    inventory.bytes_per_doc_by_robot = scale_robot_factors(sampled, window_docs, real)
    return inventory


def kibana_discover_url(kibana_base: str, robot_id: str, day: date, index_pattern: str = ELASTIC_INDEX_PATTERN) -> str:
    """Kibana Discover for one system's documents on one local day (Chris,
    2026-09-05: click an Elastic bar to see the actual data). Uses the
    browser's default data view; the query matches either id field."""
    base = (kibana_base or "").rstrip("/")
    if ".es." in base:
        base = base.replace(".es.", ".kb.")
    start = datetime.combine(day, dt_time.min).astimezone().astimezone(timezone.utc)
    end = start + timedelta(days=1)
    rid = str(robot_id).replace("'", "!'")
    query = f'leap_robot_id:"{rid}" or system_id:"{rid}"'
    g = f"(time:(from:'{start:%Y-%m-%dT%H:%M:%S.000Z}',to:'{end:%Y-%m-%dT%H:%M:%S.000Z}'))"
    a = f"(query:(language:kuery,query:'{query}'))"
    safe = "():,'!"
    return f"{base}/app/discover#/?_g={quote(g, safe=safe)}&_a={quote(a, safe=safe)}"


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


def scale_robot_factors(
    sampled: dict[str, float], window_docs: dict[str, int], real_bytes_per_doc: float | None
) -> dict[str, float]:
    """Per-robot sampled averages, rescaled so their document-weighted
    mean equals the real store bytes/doc when that is known. Robots
    without a sample get the (rescaled) mean."""
    if not sampled:
        return {}
    weights = {r: window_docs.get(r, 0) for r in sampled}
    total_w = sum(weights.values())
    if total_w > 0:
        mean = sum(sampled[r] * weights[r] for r in sampled) / total_w
    else:
        mean = sum(sampled.values()) / len(sampled)
    scale = (real_bytes_per_doc / mean) if (real_bytes_per_doc and mean > 0) else 1.0
    return {r: v * scale for r, v in sampled.items()}


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
    cache_path: Path | None = None,
    use_cache: bool = True,
) -> ElasticInventory:
    now_local = datetime.now().astimezone()
    day_list = inventory_days(now_local.date(), days)
    inventory = ElasticInventory(days=day_list)
    cache = load_inventory_cache(cache_path) if use_cache else None
    cached_counts, complete = cached_day_counts(cache, day_list)
    fetch_days = days_to_fetch(day_list, complete)
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
        if complete:
            progress(
                f"Elastic: {len(complete)} of {len(day_list)} days from the local cache; "
                f"counting {len(fetch_days)} day{'s' if len(fetch_days) != 1 else ''}..."
            )
        else:
            progress("Elastic: counting documents per day and system...")
    start_iso = datetime.combine(fetch_days[0], dt_time.min).astimezone().isoformat()
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
        inventory.counts = parse_histogram(buckets, fetch_days)
        last_error = None
        break
    if last_error is not None:
        raise last_error
    for robot, per_day in cached_counts.items():
        target = inventory.counts.setdefault(robot, {})
        for day, n in per_day.items():
            target[day] = target.get(day, 0) + n

    if progress:
        progress("Elastic: total document count and oldest record...")
    try:
        total = _post({"size": 0, "track_total_hits": True}, timeout=60)
        inventory.total_docs = int(((total.get("hits") or {}).get("total") or {}).get("value") or 0)
    except Exception:
        inventory.total_docs = None
    try:
        cached_oldest = str((cache or {}).get("oldest_ts") or "")
        if cached_oldest:
            inventory.oldest_ts = datetime.fromisoformat(cached_oldest.replace("Z", "+00:00"))
        else:
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
    # Per-robot document sizes from a random sample of each robot's
    # documents in the window (one query per robot).
    sampled: dict[str, float] = cached_samples(cache, now_local)
    sampled_at = now_local
    if sampled:
        try:
            sampled_at = datetime.fromisoformat(str(cache.get("sampled_at")))
        except (TypeError, ValueError):
            sampled_at = now_local
    robots = sorted(r for r in inventory.counts if r not in sampled)
    if not robots and progress:
        progress("Elastic: document sizes from the local cache")
    window_docs = {r: sum(per_day.values()) for r, per_day in inventory.counts.items()}
    for idx, robot in enumerate(robots, start=1):
        if progress:
            progress(f"Elastic: sampling document sizes ({idx}/{len(robots)})...")
        robot_filter = {
            "bool": {
                "should": [
                    {"term": {"leap_robot_id.keyword": robot}},
                    {"term": {"leap_robot_id": robot}},
                    {"term": {"system_id.keyword": robot}},
                    {"term": {"system_id": robot}},
                ],
                "minimum_should_match": 1,
            }
        }
        body = {
            "size": 200,
            "track_total_hits": False,
            "query": {
                "function_score": {
                    "query": {
                        "bool": {
                            "filter": [
                                robot_filter,
                                {"range": {"@timestamp": {"gte": start_iso, "lte": now_local.isoformat()}}},
                            ]
                        }
                    },
                    "random_score": {"seed": 7, "field": "_seq_no"},
                    "boost_mode": "replace",
                }
            },
        }
        try:
            data = _post(body, timeout=60)
            avg = mean_source_bytes(((data.get("hits") or {}).get("hits") or []))
            if avg:
                sampled[robot] = avg
        except Exception:
            continue
    real_per_doc = inventory.bytes_per_doc  # set only when index stats were readable
    inventory.bytes_per_doc_by_robot = scale_robot_factors(sampled, window_docs, real_per_doc)
    if inventory.bytes_per_doc is None and sampled:
        total_w = sum(window_docs.get(r, 0) for r in sampled) or len(sampled)
        inventory.bytes_per_doc = sum(
            sampled[r] * (window_docs.get(r, 0) if sum(window_docs.values()) else 1) for r in sampled
        ) / total_w
        inventory.bytes_basis = (
            "estimated from sampled document JSON per system - the API key lacks the "
            "index-stats privilege for real store sizes"
        )
        if inventory.total_docs:
            inventory.total_bytes = int(inventory.total_docs * inventory.bytes_per_doc)
    elif inventory.bytes_per_doc is not None and sampled:
        inventory.bytes_basis = "index store size, apportioned per system by sampled document sizes"
    if use_cache:
        save_inventory_cache(
            merge_inventory_cache(cache, inventory, fetch_days, now_local.date(), sampled, sampled_at),
            cache_path,
        )
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
