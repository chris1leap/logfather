from __future__ import annotations

import re
import os
import json
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable, List

import requests
from requests.adapters import HTTPAdapter
from concurrent.futures import ThreadPoolExecutor, as_completed

from Time_Picker import (
    TimelineItem,
    load_day_files,
    parse_time_from_name,
    LAST_BLOCK_DURATION,
    inferred_live_clip_end,
    local_day_start_utc,
    local_day_end_utc,
)
from settings_store import Settings, Condition
from elastic_errors import ElasticFetchError

# Defaults (can be overridden in settings dialog). Index must be provided by user if default is incorrect.
KIBANA_BASE_DEFAULT = "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243"
DISCOVER_INDEX_ID_DEFAULT = None
ELASTIC_INDEX_PATTERN = "logstash-*,pikpak,pikpak-*"
ELASTIC_TIMESTAMP_FIELDS = ["@timestamp_ros", "@timestamp"]
SYSTEM_ID_OVERRIDE: str | None = None
ELASTIC_EVENT_MAX_WORKERS = 4
ELASTIC_EVENT_MAX_PAGES = 20
ELASTIC_EVENT_PAGE_SIZE = 1500
ELASTIC_EVENT_MIN_PAGE_SIZE = 300
ELASTIC_EVENT_TIMEOUT_SEC = 12
ELASTIC_TIMING_LOGS = True
FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS = 30

_thread_local = threading.local()


def set_system_id_override(system_id: str | None) -> None:
    global SYSTEM_ID_OVERRIDE
    SYSTEM_ID_OVERRIDE = system_id or None


def _normalize_index_id(_index_id: str | None) -> str | None:
    return ELASTIC_INDEX_PATTERN


def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache"
    return Path.home() / ".videolog_cache"


def _get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is not None:
        return session
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    _thread_local.session = session
    return session


def _events_cache_path_for_robot(
    settings: Settings,
    robot_id: str,
    day,
    pikpak_root: Path | None = None,
) -> Path | None:
    day_key = f"{day:%Y%m%d}"
    index_id = _normalize_index_id(None) or ""
    conds = [
        {"name": c.name or "", "query": c.query or "", "color": c.color or ""}
        for c in settings.conditions
        if c.query
    ]
    pikpak_key = ""
    if pikpak_root is not None:
        try:
            pikpak_key = str(pikpak_root.resolve()).lower()
        except Exception:
            pikpak_key = str(pikpak_root).lower()
    payload = {
        "robot_id": robot_id,
        "day": day_key,
        "pikpak_root": pikpak_key,
        "elastic_url": settings.elastic_url or "",
        "elastic_index": index_id,
        "elastic_ts_field": settings.elastic_timestamp_field or "",
        "conditions": conds,
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    cache_root = _default_cache_root() / "elastic_events"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    filename = f"events_{robot_id}_{day_key}_{digest}.json"
    return cache_root / filename


def _events_cache_path(settings: Settings, pikpak_root: Path, day) -> Path | None:
    robot_id = _extract_robot_id(pikpak_root)
    if not robot_id:
        return None
    return _events_cache_path_for_robot(settings, robot_id, day, pikpak_root=pikpak_root)


def _logs_cache_path(settings: Settings, pikpak_root: Path, day) -> Path | None:
    robot_id = _extract_robot_id(pikpak_root)
    if not robot_id:
        return None
    day_key = f"{day:%Y%m%d}"
    index_id = _normalize_index_id(None) or ""
    payload = {
        "schema_version": 8,
        "robot_id": robot_id,
        "day": day_key,
        "elastic_url": settings.elastic_url or "",
        "elastic_index": index_id,
        "elastic_ts_field": settings.elastic_timestamp_field or "",
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    cache_root = _default_cache_root() / "elastic_logs"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    filename = f"logs_{robot_id}_{day_key}_{digest}.json"
    return cache_root / filename


def _target_rate_cache_path(
    settings: Settings,
    robot_id: str,
    start_dt: datetime,
    end_dt: datetime,
    bucket_seconds: int,
    pikpak_root: Path | None = None,
) -> Path | None:
    index_id = _normalize_index_id(None) or ""
    pikpak_key = ""
    if pikpak_root is not None:
        try:
            pikpak_key = str(pikpak_root.resolve()).lower()
        except Exception:
            pikpak_key = str(pikpak_root).lower()
    payload = {
        "schema_version": 1,
        "robot_id": robot_id,
        "start": _ensure_utc(start_dt).isoformat(),
        "end": _ensure_utc(end_dt).isoformat(),
        "bucket_seconds": int(bucket_seconds),
        "pikpak_root": pikpak_key,
        "elastic_url": settings.elastic_url or "",
        "elastic_index": index_id,
        "elastic_ts_field": settings.elastic_timestamp_field or "",
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    cache_root = _default_cache_root() / "elastic_target_rate"
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    start_key = _ensure_utc(start_dt).strftime("%Y%m%dT%H%M%S")
    end_key = _ensure_utc(end_dt).strftime("%Y%m%dT%H%M%S")
    filename = f"target_rate_{robot_id}_{start_key}_{end_key}_{int(bucket_seconds)}_{digest}.json"
    return cache_root / filename


def _load_target_rate_cache(cache_path: Path | None) -> list[dict] | None:
    if cache_path is None or not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    buckets = data.get("buckets")
    if not isinstance(buckets, list):
        return None
    normalized: list[dict] = []
    for raw in buckets:
        if not isinstance(raw, dict):
            continue
        start = _parse_ts(str(raw.get("start", "")))
        end = _parse_ts(str(raw.get("end", "")))
        if start is None or end is None:
            continue
        try:
            count = int(raw.get("count", 0))
        except Exception:
            count = 0
        normalized.append({
            "start": _ensure_utc(start),
            "end": _ensure_utc(end),
            "count": max(0, count),
        })
    return normalized


def _save_target_rate_cache(cache_path: Path | None, buckets: list[dict]) -> None:
    if cache_path is None:
        return
    payload = {
        "schema_version": 1,
        "buckets": [
            {
                "start": _ensure_utc(bucket["start"]).isoformat(),
                "end": _ensure_utc(bucket["end"]).isoformat(),
                "count": int(bucket.get("count", 0)),
            }
            for bucket in buckets
            if isinstance(bucket, dict) and bucket.get("start") and bucket.get("end")
        ],
    }
    try:
        cache_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass


def _extract_robot_id(pikpak_root: Path) -> str | None:
    """
    Build robot id from PikPak folder name: expects trailing digits like PikPak012 -> 35-2300-012.
    """
    m = re.search(r"(\d{3})$", pikpak_root.name)
    if not m:
        return None
    return f"35-2300-{m.group(1)}"


def _get_robot_id(pikpak_root: Path | None) -> str | None:
    if SYSTEM_ID_OVERRIDE:
        return SYSTEM_ID_OVERRIDE
    if pikpak_root is None:
        return None
    return _extract_robot_id(pikpak_root)


def _iso_range_for_day(day: datetime.date) -> tuple[str, str]:
    start = local_day_start_utc(day)
    end = local_day_end_utc(day)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime | None:
    try:
        val = value.replace("Z", "+00:00")
        return datetime.fromisoformat(val)
    except Exception:
        return None


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_ui_selection(source_doc: dict) -> dict | None:
    source_val = str(source_doc.get("source") or "").strip()
    allowed_sources = {"/leap/manip1/ui_node", "/leap/manip1/behaviour_node"}
    has_sku_fields = any(
        key in source_doc
        for key in (
            "data_collection",
            "data_collection.sku_name",
            "sku",
            "sku.name",
        )
    )
    if source_val and source_val not in allowed_sources and not has_sku_fields:
        return None
    params = None
    json_req = source_doc.get("json_request")
    if isinstance(json_req, dict):
        params = json_req.get("params")
    if params is None:
        params = source_doc.get("json_request.params")
    payload = {}
    if isinstance(params, str) and params:
        try:
            payload = json.loads(params)
        except Exception:
            payload = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        sku = data.get("user_selection")
        tray = data.get("tray_selection")
        tool = data.get("tool_selection")
    else:
        sku = None
        tray = None
        tool = None

    # Argus 2.0: SKU may be stored directly on the document.
    if not sku:
        sku = source_doc.get("data_collection.sku_name") or source_doc.get("sku.name")
    if not sku:
        data_collection = source_doc.get("data_collection")
        if isinstance(data_collection, dict):
            # Argus 1.x schema
            sku = sku or data_collection.get("user_selection")
            tray = tray or data_collection.get("tray_selection")
            tool = tool or data_collection.get("tool_selection")
            # Argus 2.x schema
            sku = sku or data_collection.get("sku_name")
            tray = tray or data_collection.get("sku_tray")
            tool = tool or data_collection.get("sku_tool")
        sku_block = source_doc.get("sku")
        if isinstance(sku_block, dict):
            sku = sku or sku_block.get("name")
            tray = tray or sku_block.get("tray")
            tool = tool or sku_block.get("tool")
        # Flat field fallback if mapping flattens nested keys.
        sku = sku or source_doc.get("data_collection.user_selection")
        tray = tray or source_doc.get("data_collection.tray_selection")
        tool = tool or source_doc.get("data_collection.tool_selection")

    if not sku:
        return None
    return {
        "sku": str(sku),
        "tray": str(tray) if tray else "",
        "tool": str(tool) if tool else "",
    }


def _extract_ui_sku(source_doc: dict) -> str | None:
    selection = _extract_ui_selection(source_doc)
    if not selection:
        return None
    return selection.get("sku")


def _is_manual_state(state_name: str) -> bool:
    s = (state_name or "").strip().lower()
    if not s:
        return False
    return s == "controller_node_manual_mode" or ("manual" in s and "mode" in s)


def _is_automatic_state(state_name: str) -> bool:
    s = (state_name or "").strip().lower()
    if not s:
        return False
    return s == "controller_node_automatic_mode" or ("automatic" in s and "mode" in s)


def _is_shutdown_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return "shutting down system" in msg


def _extract_service_name(source_doc: dict) -> str:
    direct = source_doc.get("json_request.service_name")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    json_request = source_doc.get("json_request")
    if isinstance(json_request, dict):
        nested = json_request.get("service_name")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _is_stop_like_event(state_name: str, message: str, service_name: str = "") -> bool:
    lower_state = (state_name or "").strip().lower()
    if (
        "stop" in lower_state
        or "estop" in lower_state
        or "caution" in lower_state
        or lower_state in {
            "hardware_emergency_stop",
            "protective_stop",
            "emergency_stop",
            "system_stop",
            "stop_pnp",
            "caution_led_on",
        }
    ):
        return True
    if (service_name or "").strip().lower() == "system_shutdown":
        return True
    return _is_shutdown_message(message)


def _build_robot_filters(robot_id: str) -> dict:
    should_terms = []
    for field in ("leap_robot_id", "system_id", "system_id.raw"):
        should_terms.extend(
            [
                {"term": {f"{field}.keyword": robot_id}},
                {"term": {field: robot_id}},
                {"match_phrase": {field: robot_id}},
            ]
        )
    return {"bool": {"should": should_terms, "minimum_should_match": 1}}


def _build_multi_robot_filters(robot_ids: list[str]) -> dict:
    should_terms = []
    for robot_id in robot_ids:
        should_terms.append(_build_robot_filters(robot_id))
    return {"bool": {"should": should_terms, "minimum_should_match": 1}}


def _build_query(
    cond: Condition,
    robot_id: str,
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    size: int = 2000,
    search_after: list[str] | None = None,
) -> dict:
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.params",
        ],
        "sort": [{"@timestamp_ros": {"order": "asc", "format": "strict_date_optional_time"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {"bool": {"should": ts_should, "minimum_should_match": 1}},
                ],
                "must": [
                    {
                        "query_string": {
                            "query": cond.query,
                            "default_field": "*",
                            "default_operator": "AND",
                            "analyze_wildcard": True,
                            "lenient": True,
                        }
                    }
                ],
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def _extract_hit_robot_id(source_doc: dict) -> str | None:
    for key in ("leap_robot_id", "system_id", "system_id.raw"):
        value = source_doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_overview_query(
    robot_ids: list[str],
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    sort_field: str,
    size: int = 3000,
    search_after: list[str] | None = None,
) -> dict:
    transition_states = [
        "start_pnp",
        "stop_pnp",
        "operator_stop",
        "caution_led_on",
        "hardware_emergency_stop",
        "protective_stop",
        "controller_node_manual_mode",
        "controller_node_automatic_mode",
        "system_stop",
        "emergency_stop",
    ]
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "@timestamp",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.service_name",
            "json_request",
            "json_request.params",
        ],
        "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_multi_robot_filters(robot_ids),
                    {
                        "bool": {
                            "should": ts_should,
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"terms": {"state_name.keyword": transition_states}},
                                {"terms": {"state_name": transition_states}},
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"term": {"source.keyword": "/leap/manip1/ui_node"}},
                                                        {"term": {"source.keyword": "/leap/manip1/behaviour_node"}},
                                                        {"term": {"source": "/leap/manip1/ui_node"}},
                                                        {"term": {"source": "/leap/manip1/behaviour_node"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"exists": {"field": "data_collection.user_selection"}},
                                                        {"exists": {"field": "data_collection.sku_name"}},
                                                        {"exists": {"field": "sku.name"}},
                                                        {"exists": {"field": "json_request.params"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                        ]
                                    }
                                },
                                {"term": {"message.keyword": "Shutting down system"}},
                                {"term": {"message": "Shutting down system"}},
                                {"match_phrase": {"message": "Shutting down system"}},
                                {"term": {"json_request.service_name.keyword": "system_shutdown"}},
                                {"term": {"json_request.service_name": "system_shutdown"}},
                                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
                            ],
                            "minimum_should_match": 1,
                        },
                    },
                ]
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def fetch_overview_events(
    settings: Settings,
    system_roots: list[Path],
    start_dt: datetime,
    end_dt: datetime,
) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for chunk in fetch_overview_event_chunks(settings, system_roots, start_dt, end_dt):
        for robot_id, items in chunk.items():
            bucket = grouped.setdefault(robot_id, [])
            bucket.extend(items)
    for items in grouped.values():
        items.sort(key=lambda item: item.get("ts") or datetime.min.replace(tzinfo=timezone.utc))
    return grouped


def fetch_overview_event_chunks(
    settings: Settings,
    system_roots: list[Path],
    start_dt: datetime,
    end_dt: datetime,
    chunk_minutes: int = 10,
) -> Iterable[dict[str, list[dict]]]:
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key or not index_id:
        return []

    robot_to_root: dict[str, Path] = {}
    for root in system_roots:
        robot_id = _extract_robot_id(root)
        if robot_id:
            robot_to_root[robot_id] = root
    if not robot_to_root:
        return []

    start_dt = _ensure_utc(start_dt)
    end_dt = _ensure_utc(end_dt)
    if end_dt <= start_dt:
        return []
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    sort_field = ts_fields[0] if ts_fields else "@timestamp"
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true", "Authorization": f"ApiKey {api_key}"}
    search_endpoint = _search_url(url, index_id)
    session = _get_thread_session()
    chunk_delta = timedelta(minutes=max(1, int(chunk_minutes)))
    chunk_start = start_dt
    while chunk_start < end_dt:
        chunk_end = min(end_dt, chunk_start + chunk_delta)
        start_iso = chunk_start.isoformat().replace("+00:00", "Z")
        end_iso = chunk_end.isoformat().replace("+00:00", "Z")
        grouped: dict[str, list[dict]] = {robot_id: [] for robot_id in robot_to_root}
        search_after: list[str] | None = None
        page = 0
        max_pages = 60
        page_size = 3000

        while page < max_pages:
            body = _build_overview_query(
                list(robot_to_root.keys()),
                start_iso,
                end_iso,
                ts_fields,
                sort_field,
                size=page_size,
                search_after=search_after,
            )
            try:
                resp = session.post(search_endpoint, json=body, headers=headers, timeout=15)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                raise ElasticFetchError(f"[elastic] overview query failed: {exc}", []) from exc
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break
            for hit in hits:
                src = hit.get("_source", {})
                if not isinstance(src, dict):
                    continue
                robot_id = _extract_hit_robot_id(src)
                if not robot_id or robot_id not in grouped:
                    continue
                ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                grouped[robot_id].append(
                    {
                        "ts": _ensure_utc(ts),
                        "state_name": str(src.get("state_name") or "").strip(),
                        "message": str(src.get("message") or "").strip(),
                        "service_name": _extract_service_name(src),
                        "source": str(src.get("source") or "").strip(),
                        "selection": _extract_ui_selection(src),
                    }
                )
            if len(hits) < page_size:
                break
            last_sort = hits[-1].get("sort")
            if not last_sort:
                break
            search_after = last_sort
            page += 1

        for items in grouped.values():
            items.sort(key=lambda item: item.get("ts") or datetime.min.replace(tzinfo=timezone.utc))
        yield grouped
        chunk_start = chunk_end


def _search_url(base: str, index_id: str) -> str:
    base = base.rstrip("/")
    # If given a Kibana URL, convert to the ES endpoint instead of proxy (proxy often disabled).
    if "kb." in base:
        base = base.replace(".kb.", ".es.")
    # Standard ES _search endpoint
    return (
        f"{base}/{index_id}/_search"
        "?ignore_unavailable=true&allow_no_indices=true&request_cache=true"
    )


def _serialize_timeline_item(item: TimelineItem) -> dict:
    return {
        "start": _ensure_utc(item.start).isoformat(),
        "end": _ensure_utc(item.end).isoformat(),
        "label": item.label,
        "kind": item.kind,
        "color": str(item.color),
        "payload": item.payload,
        "track_label": item.track_label,
        "cached": bool(item.cached),
        "annotated": bool(item.annotated),
        "path_key": item.path_key,
    }


def _deserialize_timeline_item(data: dict) -> TimelineItem | None:
    try:
        start = _parse_ts(str(data.get("start", "")))
        end = _parse_ts(str(data.get("end", "")))
        if not start or not end:
            return None
        return TimelineItem(
            start=_ensure_utc(start),
            end=_ensure_utc(end),
            label=str(data.get("label", "")),
            kind=str(data.get("kind", "")),
            color=data.get("color", "#fa8c16"),
            payload=data.get("payload"),
            track_label=data.get("track_label"),
            cached=bool(data.get("cached", False)),
            annotated=bool(data.get("annotated", False)),
            path_key=data.get("path_key"),
        )
    except Exception:
        return None


def _events_cache_is_fresh(cache_path: Path, day) -> bool:
    # Past days are effectively immutable for timeline purposes.
    if day < datetime.now(timezone.utc).date():
        return True
    try:
        age_seconds = max(0.0, datetime.now(timezone.utc).timestamp() - cache_path.stat().st_mtime)
    except Exception:
        return False
    return age_seconds <= 120


def _load_events_cache(cache_path: Path | None, day) -> list[TimelineItem] | None:
    if cache_path is None or not cache_path.exists():
        return None
    if not _events_cache_is_fresh(cache_path, day):
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return None
    items: list[TimelineItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = _deserialize_timeline_item(raw)
        if item is not None:
            items.append(item)
    return items


def _save_events_cache(cache_path: Path | None, items: list[TimelineItem]) -> None:
    if cache_path is None:
        return
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "items": [_serialize_timeline_item(i) for i in items],
        }
        cache_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except Exception:
        pass


def _merge_sku_items(base_items: list[TimelineItem], sku_items: list[TimelineItem]) -> list[TimelineItem]:
    merged = [itm for itm in base_items if itm.kind != "sku"]
    if sku_items:
        merged.extend(sku_items)
    merged.sort(key=lambda it: it.start)
    return merged


def _perf_log(message: str) -> None:
    if ELASTIC_TIMING_LOGS:
        print(f"[elastic-perf] {message}", flush=True)


def _build_sku_query(
    robot_id: str,
    start_iso: str,
    end_iso: str,
    ts_fields: list[str],
    sort_field: str,
    size: int = 2000,
    search_after: list[str] | None = None,
) -> dict:
    transition_states = [
        "start_pnp",
        "stop_pnp",
        "operator_stop",
        "caution_led_on",
        "hardware_emergency_stop",
        "protective_stop",
        "controller_node_manual_mode",
        "controller_node_automatic_mode",
    ]
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    body = {
        "size": size,
        "track_total_hits": False,
        "_source": [
            "@timestamp_ros",
            "@timestamp",
            "message",
            "state_name",
            "source",
            "leap_robot_id",
            "system_id",
            "data_collection.sku_name",
            "data_collection",
            "sku.name",
            "sku.tray",
            "sku.tool",
            "sku",
            "json_request.service_name",
            "json_request",
            "json_request.params",
        ],
        "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {
                        "bool": {
                            "should": ts_should,
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"terms": {"state_name.keyword": transition_states}},
                                {"terms": {"state_name": transition_states}},
                                {
                                    "bool": {
                                        "filter": [
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"term": {"source.keyword": "/leap/manip1/ui_node"}},
                                                        {"term": {"source.keyword": "/leap/manip1/behaviour_node"}},
                                                        {"term": {"source": "/leap/manip1/ui_node"}},
                                                        {"term": {"source": "/leap/manip1/behaviour_node"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                            {
                                                "bool": {
                                                    "should": [
                                                        {"exists": {"field": "data_collection.user_selection"}},
                                                        {"exists": {"field": "data_collection.sku_name"}},
                                                        {"exists": {"field": "sku.name"}},
                                                        {"exists": {"field": "json_request.params"}},
                                                    ],
                                                    "minimum_should_match": 1,
                                                }
                                            },
                                        ]
                                    }
                                },
                                {"term": {"message.keyword": "Shutting down system"}},
                                {"term": {"message": "Shutting down system"}},
                                {"match_phrase": {"message": "Shutting down system"}},
                                {"term": {"json_request.service_name.keyword": "system_shutdown"}},
                                {"term": {"json_request.service_name": "system_shutdown"}},
                                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
                            ],
                            "minimum_should_match": 1,
                        },
                    },
                ]
            }
        },
    }
    if search_after:
        body["search_after"] = search_after
    return body


def fetch_events(settings: Settings, pikpak_root: Path | None, day) -> Iterable[TimelineItem]:
    t_fetch_start = perf_counter()
    if not day or (pikpak_root is None and SYSTEM_ID_OVERRIDE is None):
        print("[elastic] No PikPak or day selected; skipping event fetch.")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key:
        print("[elastic] Missing URL or API key; skipping event fetch.")
        return []
    if not index_id:
        print("[elastic] Missing index/pattern; set it in Settings.")
        return []


    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []
    cache_path = _events_cache_path_for_robot(settings, robot_id, day, pikpak_root=pikpak_root)
    t_cache_read_start = perf_counter()
    cached_items = _load_events_cache(cache_path, day)
    _perf_log(f"cache read took {(perf_counter() - t_cache_read_start) * 1000:.0f}ms")
    if cached_items is not None:
        print(f"[elastic] Using cached events ({len(cached_items)} items).")
        t_sku_refresh = perf_counter()
        try:
            sku_items = list(fetch_sku_items(settings, pikpak_root, day))
        except ElasticFetchError as exc:
            sku_items = list(exc.items) if exc.items else []
        except Exception:
            sku_items = []
        merged_items = _merge_sku_items(list(cached_items), sku_items)
        if sku_items:
            _save_events_cache(cache_path, merged_items)
        _perf_log(f"cache-hit SKU refresh took {(perf_counter() - t_sku_refresh) * 1000:.0f}ms")
        _perf_log(f"fetch_events total (cache hit): {(perf_counter() - t_fetch_start) * 1000:.0f}ms")
        return merged_items

    start_iso, end_iso = _iso_range_for_day(day)
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true", "Authorization": f"ApiKey {api_key}"}

    items: List[TimelineItem] = []
    any_condition = False
    search_endpoint = _search_url(url, index_id)
    active_conditions = [(idx, cond) for idx, cond in enumerate(settings.conditions) if cond.query]
    _perf_log(f"active conditions: {len(active_conditions)} / {len(settings.conditions)}")

    def fetch_for_condition(idx: int, cond: Condition) -> tuple[List[TimelineItem], str | None]:
        t_cond_start = perf_counter()
        session = _get_thread_session()
        hits_collected: List[TimelineItem] = []
        warning_msg: str | None = None
        search_after: list[str] | None = None
        page = 0
        requests_made = 0
        max_pages = ELASTIC_EVENT_MAX_PAGES
        page_size = ELASTIC_EVENT_PAGE_SIZE
        min_page_size = ELASTIC_EVENT_MIN_PAGE_SIZE
        timeout_sec = ELASTIC_EVENT_TIMEOUT_SEC
        timeout_retries = 0
        while page < max_pages:
            body = _build_query(
                cond,
                robot_id,
                start_iso,
                end_iso,
                ts_fields,
                size=page_size,
                search_after=search_after,
            )
            try:
                requests_made += 1
                resp = session.post(
                    search_endpoint,
                    json=body,
                    headers=headers,
                    timeout=timeout_sec,
                )
                resp.raise_for_status()
                data = resp.json()
                hits = data.get("hits", {}).get("hits", [])
                timeout_retries = 0
            except requests.exceptions.Timeout:
                if page_size > min_page_size:
                    page_size = max(min_page_size, page_size // 2)
                    timeout_sec = min(30, timeout_sec + 3)
                    print(
                        f"[elastic] condition {idx+1} timed out on page {page}; "
                        f"reducing page size to {page_size} (timeout {timeout_sec}s) and retrying..."
                    )
                    continue
                if timeout_retries < 2:
                    timeout_retries += 1
                    print(
                        f"[elastic] condition {idx+1} timed out on page {page} "
                        f"(page size {page_size}); retry {timeout_retries}/2..."
                    )
                    continue
                warning_msg = (
                    f"[elastic] condition {idx+1} giving up after repeated timeouts "
                    f"(page {page}, size {page_size})"
                )
                print(warning_msg)
                break
            except requests.RequestException as exc:
                err_text = ""
                if exc.response is not None:
                    try:
                        err_text = exc.response.text
                    except Exception:
                        err_text = ""
                warning_msg = (
                    f"[elastic] query failed for condition {idx+1} page {page}: {exc} {err_text}"
                )
                print(warning_msg)
                break

            if not hits:
                break
            for hit in hits:
                src = hit.get("_source", {})
                ts_val = src.get("@timestamp_ros") or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                selection = _extract_ui_selection(src)
                if selection:
                    hit["_ui_sku"] = selection.get("sku")
                    hit["_ui_tray"] = selection.get("tray")
                    hit["_ui_tool"] = selection.get("tool")
                label = src.get("message") or cond.name or cond.query
                hits_collected.append(
                    TimelineItem(
                        start=ts,
                        end=ts + timedelta(seconds=1),
                        label=label,
                        kind=f"cond_{idx}",
                        color=cond.color or "#fa8c16",
                        payload=hit,
                        track_label=cond.name or f"Cond {idx+1}",
                    )
                )
            if len(hits) < page_size:
                break
            last_sort = hits[-1].get("sort")
            if not last_sort:
                break
            search_after = last_sort
            page += 1
        print(f"[elastic] condition {idx+1} ({cond.name or cond.query}) collected {len(hits_collected)} hits")
        _perf_log(
            f"condition {idx+1} requests={requests_made} hits={len(hits_collected)} "
            f"time={(perf_counter() - t_cond_start) * 1000:.0f}ms"
        )
        return hits_collected, warning_msg

    warnings: list[str] = []
    tasks = []
    with ThreadPoolExecutor(max_workers=max(1, min(ELASTIC_EVENT_MAX_WORKERS, len(active_conditions)))) as executor:
        for idx, cond in active_conditions:
            any_condition = True
            tasks.append(executor.submit(fetch_for_condition, idx, cond))
        for future in as_completed(tasks):
            cond_items, warning = future.result()
            if cond_items:
                items.extend(cond_items)
            if warning:
                warnings.append(warning)

    if not any_condition:
        print("[elastic] No condition queries configured; nothing to fetch.")
    if not items:
        print("[elastic] No events returned for selected day/robot.")
    t_cache_write_start = perf_counter()
    try:
        sku_items = list(fetch_sku_items(settings, pikpak_root, day))
    except ElasticFetchError as exc:
        warnings.append(str(exc))
        sku_items = list(exc.items) if exc.items else []
    except Exception as exc:
        warnings.append(str(exc))
        sku_items = []
    items = _merge_sku_items(items, sku_items)
    _save_events_cache(cache_path, items)
    _perf_log(f"cache write took {(perf_counter() - t_cache_write_start) * 1000:.0f}ms")
    _perf_log(f"fetch_events total: {(perf_counter() - t_fetch_start) * 1000:.0f}ms (items={len(items)})")
    if warnings:
        raise ElasticFetchError("\n".join(warnings), items)
    return items


def _build_sku_items_from_event_items(
    event_items: list[TimelineItem],
    day,
    pikpak_root: Path | None,
) -> list[TimelineItem]:
    if not event_items or not day:
        return []
    last_video_end = _last_video_end(pikpak_root, day) if pikpak_root else None
    day_start = local_day_start_utc(day)
    day_end = local_day_end_utc(day) + timedelta(milliseconds=1)
    cap_end = _ensure_utc(last_video_end) if last_video_end else day_end

    events: list[tuple[datetime, str, dict | None, str]] = []
    for item in event_items:
        payload = item.payload if isinstance(item.payload, dict) else None
        if not payload:
            continue
        src = payload.get("_source")
        if not isinstance(src, dict):
            continue
        state_name = str(src.get("state_name") or "").strip()
        ts = _ensure_utc(item.start)
        selection = _extract_ui_selection(src)
        message = str(src.get("message") or "")
        service_name = _extract_service_name(src)
        if _is_manual_state(state_name):
            events.append((ts, "manual", None, state_name))
        if _is_automatic_state(state_name):
            events.append((ts, "auto", None, state_name))
        if _is_stop_like_event(state_name, message, service_name):
            events.append((ts, "stop", None, state_name))
        if state_name == "start_pnp":
            events.append((ts, "start", selection, state_name))
        elif selection:
            events.append((ts, "select", selection, state_name))

    if not events:
        return []

    order = {"stop": 0, "auto": 1, "manual": 2, "start": 3, "select": 4}
    events.sort(key=lambda item: (item[0], order.get(item[1], 9)))

    items: list[TimelineItem] = []
    current_kind: str | None = None
    current_data: dict | None = None
    current_start: datetime | None = None
    last_sku_data: dict | None = None

    def _sku_key(payload: dict | None) -> tuple[str, str, str]:
        if not isinstance(payload, dict):
            return ("", "", "")
        return (
            str(payload.get("sku") or ""),
            str(payload.get("tray") or ""),
            str(payload.get("tool") or ""),
        )

    def _close_current(end_ts: datetime):
        nonlocal current_kind, current_data, current_start
        if not current_kind or not current_start:
            return
        if end_ts <= current_start:
            current_kind = None
            current_data = None
            current_start = None
            return
        if current_kind == "sku":
            sku = current_data.get("sku") if current_data else ""
            tray = current_data.get("tray") if current_data else ""
            tool = current_data.get("tool") if current_data else ""
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label=sku or "SKU",
                    kind="sku",
                    color="#8fd19e",
                    payload={"_ui_sku": sku, "_ui_tray": tray, "_ui_tool": tool},
                    track_label="SKU",
                )
            )
        else:
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label="Manual",
                    kind="sku",
                    color="#f59e0b",
                    payload={"_ui_manual": True},
                    track_label="SKU",
                )
            )
        current_kind = None
        current_data = None
        current_start = None

    for ts, kind, data, _state in events:
        if ts >= cap_end:
            break
        if kind == "stop":
            # Stop-like states end SKU runs, but should not collapse an active manual span.
            if current_kind == "sku":
                _close_current(min(ts, cap_end))
            continue
        if kind == "auto":
            # Automatic mode ends manual periods.
            if current_kind == "manual":
                _close_current(min(ts, cap_end))
            continue
        if kind == "manual":
            if current_kind == "manual":
                continue
            _close_current(min(ts, cap_end))
            current_kind = "manual"
            current_data = None
            current_start = ts
            continue
        if kind == "select" and data:
            last_sku_data = data
            if current_kind == "sku" and _sku_key(current_data) == _sku_key(data):
                continue
            if current_kind == "sku" and current_start:
                _close_current(min(ts, cap_end))
                current_kind = "sku"
                current_data = data
                current_start = ts
            continue
        if kind == "start":
            if data:
                last_sku_data = data
            start_data = data or last_sku_data or {}
            _close_current(min(ts, cap_end))
            current_kind = "sku"
            current_data = start_data
            current_start = ts

    if current_kind and current_start:
        _close_current(cap_end)
    return items


def _last_video_end(pikpak_root: Path, day) -> datetime | None:
    try:
        paths = list(load_day_files(pikpak_root, day))
    except Exception:
        return None
    entries: list[tuple[Path, datetime]] = []
    for p in paths:
        parsed_dt = parse_time_from_name(p)
        if parsed_dt is not None:
            start_dt = parsed_dt
        else:
            try:
                stat = p.stat()
            except FileNotFoundError:
                continue
            start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        entries.append((p, _ensure_utc(start_dt)))
    if not entries:
        return None
    entries.sort(key=lambda tpl: tpl[1])
    last_path, last_start = entries[-1]
    return _ensure_utc(inferred_live_clip_end(last_path, last_start))


def fetch_sku_items(settings: Settings, pikpak_root: Path | None, day) -> Iterable[TimelineItem]:
    print("[sku-debug] fetch_sku_items start", flush=True)
    if not day or (pikpak_root is None and SYSTEM_ID_OVERRIDE is None):
        print("[elastic] No PikPak or day selected; skipping SKU fetch.")
        return []
    last_video_end = _last_video_end(pikpak_root, day)
    day_start = local_day_start_utc(day)
    day_end = local_day_end_utc(day) + timedelta(milliseconds=1)
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key:
        print("[elastic] Missing URL or API key; skipping SKU fetch.")
        return []
    if not index_id:
        print("[elastic] Missing index/pattern; set it in Settings.")
        return []

    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []

    start_iso, end_iso = _iso_range_for_day(day)
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true", "Authorization": f"ApiKey {api_key}"}
    search_endpoint = _search_url(url, index_id)
    ts_fields = list(ELASTIC_TIMESTAMP_FIELDS)
    sort_field = ts_fields[0] if ts_fields else "@timestamp"
    session = _get_thread_session()
    ts_should = []
    for field in ts_fields:
        ts_should.append(
            {
                "range": {
                    field: {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )
    if not ts_should:
        ts_should.append(
            {
                "range": {
                    "@timestamp": {
                        "gte": start_iso,
                        "lte": end_iso,
                        "format": "strict_date_optional_time",
                    }
                }
            }
        )

    cap_end = _ensure_utc(last_video_end) if last_video_end else day_end
    events: list[tuple[datetime, str, dict | None, str]] = []
    total_hits = 0
    start_hits = 0
    search_after = None
    page = 0
    max_pages = 20
    page_size = 2000
    while page < max_pages:
        body = _build_sku_query(
            robot_id,
            start_iso,
            end_iso,
            ts_fields,
            sort_field,
            size=page_size,
            search_after=search_after,
        )
        try:
            resp = session.post(search_endpoint, json=body, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
        except Exception as exc:
            err_text = ""
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                try:
                    err_text = exc.response.text
                except Exception:
                    err_text = ""
            message = f"[elastic] SKU query failed: {exc} {err_text}"
            print(message)
            raise ElasticFetchError(message, []) from exc
        if not hits:
            break
        for hit in hits:
            total_hits += 1
            src = hit.get("_source", {})
            ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            ts = _ensure_utc(ts)
            state_name = str(src.get("state_name") or "").strip()
            message = str(src.get("message") or "")
            service_name = _extract_service_name(src)
            if state_name == "start_pnp":
                start_hits += 1
                sku_dbg = src.get("data_collection") or src.get("sku") or {}
                print(
                    f"[sku-debug] start_pnp {ts.isoformat()} system_id={src.get('system_id')} "
                    f"sku={sku_dbg}"
                )
            if _is_manual_state(state_name):
                events.append((ts, "manual", None, state_name))
            if _is_automatic_state(state_name):
                events.append((ts, "auto", None, state_name))
            if _is_stop_like_event(state_name, message, service_name):
                events.append((ts, "stop", None, state_name))
            selection = _extract_ui_selection(src)
            if state_name == "start_pnp":
                events.append((ts, "start", selection, state_name))
            elif selection:
                events.append((ts, "select", selection, state_name))
        if len(hits) < page_size:
            break
        last_sort = hits[-1].get("sort")
        if not last_sort:
            break
        search_after = last_sort
        page += 1
    if page >= max_pages - 1:
        print(
            f"[sku-debug] reached page cap (max_pages={max_pages}, page_size={page_size}); "
            "results may be truncated",
            flush=True,
        )

    if not events:
        if total_hits:
            print(f"[sku-debug] hits={total_hits} start_pnp={start_hits} (no SKU events created)", flush=True)

    manual_event_count = sum(1 for _ts, kind, _data, _state in events if kind == "manual")
    if manual_event_count == 0:
        # Defensive fallback: some mappings/indexes can miss manual states in the broader SKU query.
        manual_fallback_body = {
            "size": 5000,
            "track_total_hits": False,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "state_name",
                "source",
                "system_id",
            ],
            "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {"bool": {"should": ts_should, "minimum_should_match": 1}},
                        {
                            "bool": {
                                "should": [
                                    {"term": {"state_name.keyword": "controller_node_manual_mode"}},
                                    {"term": {"state_name": "controller_node_manual_mode"}},
                                    {"match_phrase": {"state_name": "controller_node_manual_mode"}},
                                    {"term": {"state_name.keyword": "controller_node_automatic_mode"}},
                                    {"term": {"state_name": "controller_node_automatic_mode"}},
                                    {"match_phrase": {"state_name": "controller_node_automatic_mode"}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
        }
        try:
            resp = session.post(search_endpoint, json=manual_fallback_body, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            for hit in hits:
                src = hit.get("_source", {})
                state_name = str(src.get("state_name") or "").strip()
                ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                ts = _ensure_utc(ts)
                if _is_manual_state(state_name):
                    events.append((ts, "manual", None, state_name))
                elif _is_automatic_state(state_name):
                    events.append((ts, "auto", None, state_name))
            if hits:
                print(f"[sku-debug] manual fallback hits={len(hits)}", flush=True)
        except Exception as exc:
            print(f"[sku-debug] manual fallback query failed: {exc}", flush=True)

    if start_hits <= 1:
        # Fallback: explicitly fetch start_pnp entries (Argus 2 nodes emit these on behaviour_node).
        fallback_body = {
            "size": 2000,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "state_name",
                "system_id",
                "data_collection.sku_name",
                "data_collection",
                "sku.name",
                "sku.tray",
                "sku.tool",
                "sku",
            ],
            "sort": [{sort_field: {"order": "asc", "format": "strict_date_optional_time", "missing": "_last"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {"term": {"state_name": "start_pnp"}},
                        {"bool": {"should": ts_should, "minimum_should_match": 1}},
                    ]
                }
            },
        }
        try:
            resp = session.post(search_endpoint, json=fallback_body, headers=headers, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
            if hits:
                print(f"[sku-debug] fallback start_pnp hits={len(hits)}", flush=True)
            for hit in hits:
                src = hit.get("_source", {})
                ts_val = src.get("@timestamp_ros") or src.get(sort_field) or src.get("@timestamp")
                ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
                if not ts:
                    continue
                ts = _ensure_utc(ts)
                selection = _extract_ui_selection(src)
                events.append((ts, "start", selection, "start_pnp"))
        except Exception as exc:
            print(f"[sku-debug] fallback start_pnp query failed: {exc}", flush=True)

    if not events:
        return []

    manual_event_count = sum(1 for _ts, kind, _data, _state in events if kind == "manual")
    if manual_event_count:
        print(f"[sku-debug] manual events={manual_event_count}", flush=True)

    order = {"stop": 0, "auto": 1, "manual": 2, "start": 3, "select": 4}
    events.sort(key=lambda item: (item[0], order.get(item[1], 9)))
    items: list[TimelineItem] = []
    current_kind: str | None = None
    current_data: dict | None = None
    current_start: datetime | None = None
    last_sku_data: dict | None = None

    def _sku_key(payload: dict | None) -> tuple[str, str, str]:
        if not isinstance(payload, dict):
            return ("", "", "")
        return (
            str(payload.get("sku") or ""),
            str(payload.get("tray") or ""),
            str(payload.get("tool") or ""),
        )

    def _close_current(end_ts: datetime):
        nonlocal current_kind, current_data, current_start
        if not current_kind or not current_start:
            return
        if end_ts <= current_start:
            current_kind = None
            current_data = None
            current_start = None
            return
        if current_kind == "sku":
            sku = current_data.get("sku") if current_data else ""
            tray = current_data.get("tray") if current_data else ""
            tool = current_data.get("tool") if current_data else ""
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label=sku or "SKU",
                    kind="sku",
                    color="#8fd19e",
                    payload={"_ui_sku": sku, "_ui_tray": tray, "_ui_tool": tool},
                    track_label="SKU",
                )
            )
        else:
            items.append(
                TimelineItem(
                    start=current_start,
                    end=end_ts,
                    label="Manual",
                    kind="sku",
                    color="#f59e0b",
                    payload={"_ui_manual": True},
                    track_label="SKU",
                )
            )
        current_kind = None
        current_data = None
        current_start = None

    for ts, kind, data, _state in events:
        if ts >= cap_end:
            break
        if kind == "stop":
            # Stop-like states end SKU runs, but should not collapse an active manual span.
            if current_kind == "sku":
                _close_current(min(ts, cap_end))
            continue
        if kind == "auto":
            # Automatic mode ends manual periods.
            if current_kind == "manual":
                _close_current(min(ts, cap_end))
            continue
        if kind == "manual":
            if current_kind == "manual":
                continue
            _close_current(min(ts, cap_end))
            current_kind = "manual"
            current_data = None
            current_start = ts
            continue
        if kind == "select" and data:
            last_sku_data = data
            if current_kind == "sku" and _sku_key(current_data) == _sku_key(data):
                continue
            if current_kind == "sku" and current_start:
                _close_current(min(ts, cap_end))
                current_kind = "sku"
                current_data = data
                current_start = ts
            continue
        if kind == "start":
            if data:
                last_sku_data = data
            start_data = data or last_sku_data
            _close_current(min(ts, cap_end))
            if start_data is None and last_sku_data:
                start_data = last_sku_data
            if start_data is None:
                start_data = {}
            current_kind = "sku"
            current_data = start_data
            current_start = ts
    if current_kind and current_start:
        _close_current(cap_end)
    manual_item_count = sum(1 for itm in items if isinstance(itm.payload, dict) and itm.payload.get("_ui_manual"))
    if manual_item_count:
        print(f"[sku-debug] manual items={manual_item_count}", flush=True)
    return items


def _fetch_logs_range_raw(
    settings: Settings,
    pikpak_root: Path | None,
    start_dt: datetime,
    end_dt: datetime,
    max_hits: int = 50000,
) -> list[tuple[datetime, str, str, str, str]]:
    if not pikpak_root and not SYSTEM_ID_OVERRIDE:
        print("[elastic] No PikPak root provided for log fetch.")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key or not index_id:
        print("[elastic] Missing URL/API key/index; cannot fetch logs.")
        return []
    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []

    start_iso = _ensure_utc(start_dt).isoformat().replace("+00:00", "Z")
    end_iso = _ensure_utc(end_dt).isoformat().replace("+00:00", "Z")
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true", "Authorization": f"ApiKey {api_key}"}
    search_endpoint = _search_url(url, index_id)
    ts_field = ELASTIC_TIMESTAMP_FIELDS[0]
    session = _get_thread_session()

    rows: list[tuple[datetime, str, str, str, str]] = []
    fetched = 0
    search_after = None
    page_size = 2000
    while fetched < max_hits:
        body = {
            "size": min(page_size, max_hits - fetched),
            "track_total_hits": False,
            "_source": [
                "@timestamp_ros",
                "@timestamp",
                "message",
                "state_name",
                "source",
                "leap_robot_id",
                "system_id",
                "json_request.params",
            ],
            "sort": [{ts_field: {"order": "asc", "format": "strict_date_optional_time"}}],
            "query": {
                "bool": {
                    "filter": [
                        _build_robot_filters(robot_id),
                        {
                            "range": {
                                ts_field: {
                                    "gte": start_iso,
                                    "lte": end_iso,
                                    "format": "strict_date_optional_time",
                                }
                            }
                        },
                    ]
                }
            },
        }
        if search_after:
            body["search_after"] = search_after
        try:
            resp = session.post(search_endpoint, json=body, headers=headers, timeout=6)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])
        except Exception as exc:
            err_text = ""
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                try:
                    err_text = exc.response.text
                except Exception:
                    err_text = ""
            message = f"[elastic] log range query failed: {exc} {err_text}"
            print(message)
            raise ElasticFetchError(message, rows) from exc
        if not hits:
            break
        for hit in hits:
            src = hit.get("_source", {})
            ts_val = src.get("@timestamp_ros") or src.get(ts_field) or src.get("@timestamp")
            ts = _parse_ts(ts_val) if isinstance(ts_val, str) else None
            if not ts:
                continue
            source_key = str(src.get("source", "") or "").strip()
            message_key = str(src.get("message", "") or "").strip()
            state_val = str(src.get("state_name", "") or "").strip()
            parts = [p for p in [source_key, state_val, message_key] if p]
            text = " | ".join(parts) if parts else message_key or state_val or source_key or "(event)"
            rows.append((ts, text, source_key, state_val, message_key))
        fetched += len(hits)
        if len(hits) < min(page_size, max_hits - (fetched - len(hits))):
            break
        last_sort = hits[-1].get("sort")
        if not last_sort:
            break
        search_after = last_sort
    return rows


def fetch_target_rate_histogram(
    settings: Settings,
    pikpak_root: Path | None,
    start_dt: datetime,
    end_dt: datetime,
    bucket_seconds: int,
) -> list[dict]:
    if not pikpak_root and not SYSTEM_ID_OVERRIDE:
        print("[elastic] No PikPak root provided for target-rate fetch.")
        return []
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    if not url or not api_key or not index_id:
        print("[elastic] Missing URL/API key/index; cannot fetch target-rate histogram.")
        return []
    robot_id = _get_robot_id(pikpak_root)
    if not robot_id:
        print(f"[elastic] Could not derive robot id from {pikpak_root}")
        return []

    start_dt = _ensure_utc(start_dt)
    end_dt = _ensure_utc(end_dt)
    if end_dt <= start_dt:
        return []
    bucket_seconds = max(1, int(bucket_seconds))
    cache_path = _target_rate_cache_path(
        settings,
        robot_id,
        start_dt,
        end_dt,
        bucket_seconds,
        pikpak_root=pikpak_root,
    )
    cached = _load_target_rate_cache(cache_path)
    if cached is not None:
        return cached

    start_iso = start_dt.isoformat().replace("+00:00", "Z")
    end_iso = end_dt.isoformat().replace("+00:00", "Z")
    headers = {"Content-Type": "application/json", "kbn-xsrf": "true", "Authorization": f"ApiKey {api_key}"}
    search_endpoint = _search_url(url, index_id)
    ts_field = ELASTIC_TIMESTAMP_FIELDS[0]
    session = _get_thread_session()
    interval = f"{bucket_seconds}s"
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {
            "bool": {
                "filter": [
                    _build_robot_filters(robot_id),
                    {
                        "range": {
                            ts_field: {
                                "gte": start_iso,
                                "lte": end_iso,
                                "format": "strict_date_optional_time",
                            }
                        }
                    },
                    {"wildcard": {"source": "*motion_control_node*"}},
                    {"match_phrase": {"message": "Adding new target to queue"}},
                ]
            }
        },
        "aggs": {
            "per_bucket": {
                "date_histogram": {
                    "field": ts_field,
                    "fixed_interval": interval,
                    "min_doc_count": 0,
                    "extended_bounds": {
                        "min": start_iso,
                        "max": end_iso,
                    },
                }
            }
        },
    }
    try:
        resp = session.post(search_endpoint, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        err_text = ""
        if isinstance(exc, requests.RequestException) and exc.response is not None:
            try:
                err_text = exc.response.text
            except Exception:
                err_text = ""
        message = f"[elastic] target-rate histogram query failed: {exc} {err_text}"
        print(message)
        raise ElasticFetchError(message, []) from exc

    raw_buckets = data.get("aggregations", {}).get("per_bucket", {}).get("buckets", [])
    buckets: list[dict] = []
    bucket_delta = timedelta(seconds=bucket_seconds)
    for raw in raw_buckets:
        key_as_string = raw.get("key_as_string")
        start = _parse_ts(str(key_as_string)) if key_as_string else None
        if start is None:
            continue
        buckets.append({
            "start": _ensure_utc(start),
            "end": _ensure_utc(start) + bucket_delta,
            "count": max(0, int(raw.get("doc_count", 0) or 0)),
        })
    _save_target_rate_cache(cache_path, buckets)
    return buckets


def fetch_logs_for_range(
    settings: Settings,
    pikpak_root: Path | None,
    start_dt: datetime,
    end_dt: datetime,
    max_hits: int = 50000,
) -> list[tuple[datetime, str, str, str, str]]:
    return _fetch_logs_range_raw(settings, pikpak_root, start_dt, end_dt, max_hits=max_hits)


def fetch_fleetwide_search_histogram(
    settings: Settings,
    pikpak_root: Path,
    query: str | list[str],
    start_dt: datetime,
    end_dt: datetime,
    bucket_seconds: int,
) -> dict:
    """Return all/operating counts and histograms for one system/search pair."""
    url = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    index_id = _normalize_index_id(None)
    robot_id = _extract_robot_id(pikpak_root)
    if not url or not api_key or not index_id:
        raise ElasticFetchError("[elastic] Missing URL/API key/index for fleetwide search.", [])
    if not robot_id:
        raise ElasticFetchError(f"[elastic] Could not derive robot id from {pikpak_root.name}", [])
    raw_queries = query if isinstance(query, list) else [query]
    queries = [str(value or "").strip() for value in raw_queries if str(value or "").strip()]
    if not queries:
        raise ElasticFetchError("[elastic] Fleetwide search query is blank.", [])
    query_clauses = []
    for query_index, query_text in enumerate(queries):
        if query_text.startswith("{"):
            try:
                parsed_query = json.loads(query_text)
            except json.JSONDecodeError as exc:
                raise ElasticFetchError(f"[elastic] Invalid fleetwide JSON query: {exc}", []) from exc
            if not isinstance(parsed_query, dict) or not parsed_query:
                raise ElasticFetchError("[elastic] Fleetwide JSON query must be a non-empty object.", [])
            clause = parsed_query
        else:
            clause = {
                "query_string": {
                    "query": query_text,
                    "default_field": "*",
                    "default_operator": "AND",
                    "analyze_wildcard": True,
                    "lenient": True,
                }
            }
        query_clauses.append(
            {
                "bool": {
                    "must": [clause],
                    "_name": f"fleet_search_{query_index}",
                }
            }
        )
    if len(query_clauses) == 1:
        query_clause = query_clauses[0]
    else:
        query_clause = {
            "bool": {
                "should": query_clauses,
                "minimum_should_match": 1,
            }
        }

    start_dt = _ensure_utc(start_dt)
    end_dt = _ensure_utc(end_dt)
    if end_dt <= start_dt:
        return {
            "robot_id": robot_id,
            "total": 0,
            "operational_total": 0,
            "non_operational_total": 0,
            "buckets": [],
        }

    start_iso = start_dt.isoformat().replace("+00:00", "Z")
    end_iso = end_dt.isoformat().replace("+00:00", "Z")
    ts_field = settings.elastic_timestamp_field or ELASTIC_TIMESTAMP_FIELDS[0]
    bucket_seconds = max(1, int(bucket_seconds))
    headers = {
        "Content-Type": "application/json",
        "kbn-xsrf": "true",
        "Authorization": f"ApiKey {api_key}",
    }
    session = _get_thread_session()
    search_endpoint = _search_url(url, index_id)

    def fetch_matching_hits(
        clause: dict,
        range_start: datetime,
        source_fields: list[str],
        *,
        endpoint: str = search_endpoint,
        robot_filter: dict | None = None,
        query_label: str = "search",
    ) -> list[dict]:
        range_start_iso = _ensure_utc(range_start).isoformat().replace("+00:00", "Z")
        search_after = None
        collected: list[dict] = []
        page_size = 1500
        min_page_size = 300
        max_pages = 100
        timeout_seconds = 15
        timeout_retries = 0
        for _page in range(max_pages):
            body = {
                "size": page_size,
                "track_total_hits": False,
                "_source": source_fields,
                "sort": [{ts_field: {"order": "asc", "format": "strict_date_optional_time"}}],
                "query": {
                    "bool": {
                        "filter": [
                            robot_filter or _build_robot_filters(robot_id),
                            {
                                "range": {
                                    ts_field: {
                                        "gte": range_start_iso,
                                        "lte": end_iso,
                                        "format": "strict_date_optional_time",
                                    }
                                }
                            },
                        ],
                        "must": [clause],
                    }
                },
            }
            if search_after:
                body["search_after"] = search_after
            try:
                response = session.post(endpoint, json=body, headers=headers, timeout=timeout_seconds)
                response.raise_for_status()
                hits = response.json().get("hits", {}).get("hits", [])
                timeout_retries = 0
            except requests.exceptions.Timeout as exc:
                if page_size > min_page_size:
                    page_size = max(min_page_size, page_size // 2)
                    timeout_seconds = min(30, timeout_seconds + 5)
                    continue
                if timeout_retries < 2:
                    timeout_retries += 1
                    timeout_seconds = min(30, timeout_seconds + 5)
                    continue
                raise ElasticFetchError(
                    f"[elastic] Fleetwide {query_label} timed out for {robot_id} after retries.",
                    [],
                ) from exc
            except Exception as exc:
                err_text = ""
                if isinstance(exc, requests.RequestException) and exc.response is not None:
                    try:
                        err_text = exc.response.text
                    except Exception:
                        pass
                message = f"[elastic] fleetwide {query_label} failed for {robot_id}: {exc} {err_text}".strip()
                raise ElasticFetchError(message, []) from exc
            collected.extend(hit for hit in hits if isinstance(hit, dict))
            if len(hits) < page_size:
                return collected
            search_after = hits[-1].get("sort")
            if not search_after:
                return collected
        raise ElasticFetchError(
            f"[elastic] Fleetwide {query_label} exceeded the pagination limit for {robot_id}.",
            [],
        )

    error_hits = fetch_matching_hits(
        query_clause,
        start_dt,
        [ts_field, "@timestamp_ros", "@timestamp", "leap_robot_id", "system_id"],
        query_label="occurrence query",
    )
    error_records: list[tuple[datetime, tuple[str, ...]]] = []
    for hit in error_hits:
        source = hit.get("_source", {})
        timestamp = _parse_ts(str(source.get(ts_field) or source.get("@timestamp_ros") or source.get("@timestamp") or ""))
        if timestamp is not None:
            matched_queries = hit.get("matched_queries")
            if isinstance(matched_queries, list):
                match_keys = tuple(sorted(str(value) for value in matched_queries if str(value)))
            else:
                match_keys = ()
            error_records.append((_ensure_utc(timestamp), match_keys or ("fleet_search_all",)))
    error_records.sort(key=lambda item: item[0])
    raw_error_count = len(error_records)
    cooldown = timedelta(seconds=FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS)
    deduplicated_error_times: list[datetime] = []
    last_counted_by_search: dict[str, datetime] = {}
    for timestamp, match_keys in error_records:
        if all(
            key in last_counted_by_search and timestamp - last_counted_by_search[key] < cooldown
            for key in match_keys
        ):
            continue
        deduplicated_error_times.append(timestamp)
        for key in match_keys:
            last_counted_by_search[key] = timestamp
    error_times = deduplicated_error_times

    # Match Elasticsearch fixed_interval alignment: buckets are anchored to
    # epoch boundaries, not to the moving "now minus N days" query start.
    # Without this, a daily bucket started at (for example) 14:20 and morning
    # events appeared under the preceding date.
    bucket_origin_epoch = int(start_dt.timestamp() // bucket_seconds) * bucket_seconds
    bucket_origin = datetime.fromtimestamp(bucket_origin_epoch, tz=timezone.utc)
    bucket_span_seconds = max(1.0, (end_dt - bucket_origin).total_seconds())
    bucket_count = max(1, int((bucket_span_seconds + bucket_seconds - 1) // bucket_seconds))
    buckets = [
        {
            "timestamp": bucket_origin + timedelta(seconds=index * bucket_seconds),
            "end": bucket_origin + timedelta(seconds=(index + 1) * bucket_seconds),
            "count": 0,
            "operational_count": 0,
            "non_operational_count": 0,
        }
        for index in range(bucket_count)
    ]
    if not error_times:
        return {
            "robot_id": robot_id,
            "total": 0,
            "operational_total": 0,
            "non_operational_total": 0,
            "operation_grace_seconds": 60,
            "raw_total": raw_error_count,
            "suppressed_count": raw_error_count,
            "cooldown_seconds": FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS,
            "buckets": buckets,
        }

    error_indices = {str(hit.get("_index") or "") for hit in error_hits}
    uses_pikpak = any(name == "pikpak" or name.startswith("pikpak-") for name in error_indices)
    uses_logstash = any(name.startswith("logstash-") for name in error_indices)
    if uses_pikpak and not uses_logstash:
        transition_index_pattern = "pikpak,pikpak-*"
    elif uses_logstash and not uses_pikpak:
        transition_index_pattern = "logstash-*"
    else:
        transition_index_pattern = index_id
    transition_endpoint = _search_url(url, transition_index_pattern)

    identity_fields = set()
    for hit in error_hits:
        source = hit.get("_source", {})
        if source.get("system_id"):
            identity_fields.add("system_id")
        if source.get("leap_robot_id"):
            identity_fields.add("leap_robot_id")
    if len(identity_fields) == 1:
        identity_field = next(iter(identity_fields))
        transition_robot_filter = {
            "bool": {
                "should": [
                    {"term": {f"{identity_field}.keyword": robot_id}},
                    {"term": {identity_field: robot_id}},
                    {"match_phrase": {identity_field: robot_id}},
                ],
                "minimum_should_match": 1,
            }
        }
    else:
        transition_robot_filter = _build_robot_filters(robot_id)
    transition_states = [
        "start_pnp",
        "stop_pnp",
        "operator_stop",
        "caution_led_on",
        "hardware_emergency_stop",
        "protective_stop",
        "system_stop",
        "emergency_stop",
        "controller_node_manual_mode",
    ]
    transition_clause = {
        "bool": {
            "should": [
                {"terms": {"state_name.keyword": transition_states}},
                {"terms": {"state_name": transition_states}},
                {"match_phrase": {"message": "Shutting down system"}},
                {"match_phrase": {"json_request.service_name": "system_shutdown"}},
            ],
            "minimum_should_match": 1,
        }
    }
    transition_hits = fetch_matching_hits(
        transition_clause,
        start_dt,
        [ts_field, "@timestamp_ros", "@timestamp", "state_name", "message", "json_request.service_name", "json_request"],
        endpoint=transition_endpoint,
        robot_filter=transition_robot_filter,
        query_label="operation-state query",
    )

    prior_body = {
        "size": 1,
        "track_total_hits": False,
        "_source": [ts_field, "@timestamp_ros", "@timestamp", "state_name", "message", "json_request.service_name", "json_request"],
        "sort": [{ts_field: {"order": "desc", "format": "strict_date_optional_time"}}],
        "query": {
            "bool": {
                "filter": [
                    transition_robot_filter,
                    {"range": {ts_field: {"lt": start_iso, "format": "strict_date_optional_time"}}},
                ],
                "must": [transition_clause],
            }
        },
    }
    prior_hits = None
    for prior_timeout in (15, 25):
        try:
            prior_response = session.post(
                transition_endpoint,
                json=prior_body,
                headers=headers,
                timeout=prior_timeout,
            )
            prior_response.raise_for_status()
            prior_hits = prior_response.json().get("hits", {}).get("hits", [])
            break
        except requests.exceptions.Timeout as exc:
            if prior_timeout == 25:
                raise ElasticFetchError(
                    f"[elastic] Fleetwide prior-state query timed out for {robot_id} after retries.",
                    [],
                ) from exc
        except Exception as exc:
            err_text = ""
            if isinstance(exc, requests.RequestException) and exc.response is not None:
                try:
                    err_text = exc.response.text
                except Exception:
                    pass
            message = f"[elastic] fleetwide prior-state query failed for {robot_id}: {exc} {err_text}".strip()
            raise ElasticFetchError(message, []) from exc
    prior_hits = prior_hits or []
    transition_hits = list(prior_hits) + transition_hits

    transitions: list[tuple[datetime, str, str, str]] = []
    for hit in transition_hits:
        source = hit.get("_source", {})
        timestamp = _parse_ts(str(source.get(ts_field) or source.get("@timestamp_ros") or source.get("@timestamp") or ""))
        if timestamp is None:
            continue
        transitions.append(
            (
                _ensure_utc(timestamp),
                str(source.get("state_name") or "").strip(),
                str(source.get("message") or ""),
                _extract_service_name(source),
            )
        )
    transitions.sort(key=lambda item: item[0])

    operation_grace = timedelta(seconds=60)
    active_after: datetime | None = None
    transition_index = 0
    operational_total = 0
    for timestamp in error_times:
        while transition_index < len(transitions) and transitions[transition_index][0] <= timestamp:
            transition_ts, state_name, message, service_name = transitions[transition_index]
            lower_state = state_name.lower()
            if state_name == "start_pnp":
                active_after = transition_ts + operation_grace
            elif (
                state_name == "controller_node_manual_mode"
                or ("manual" in lower_state and "mode" in lower_state)
                or _is_stop_like_event(state_name, message, service_name)
            ):
                active_after = None
            transition_index += 1
        operational = active_after is not None and timestamp >= active_after
        index = int((timestamp - bucket_origin).total_seconds() // bucket_seconds)
        if index < 0 or index >= len(buckets):
            continue
        buckets[index]["count"] += 1
        if operational:
            buckets[index]["operational_count"] += 1
            operational_total += 1
        else:
            buckets[index]["non_operational_count"] += 1
    return {
        "robot_id": robot_id,
        "total": len(error_times),
        "operational_total": operational_total,
        "non_operational_total": len(error_times) - operational_total,
        "operation_grace_seconds": int(operation_grace.total_seconds()),
        "raw_total": raw_error_count,
        "suppressed_count": raw_error_count - len(error_times),
        "cooldown_seconds": FLEETWIDE_OCCURRENCE_COOLDOWN_SECONDS,
        "buckets": buckets,
    }
