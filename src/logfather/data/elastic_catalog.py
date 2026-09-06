"""What kinds of data Elastic holds (Chris, 2026-09-06: the ? box on the
Data window's Elastic summary). One aggregation over the last week: a
bucket per source node with its document count and a few example
documents, joined to a curated description of what that node logs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import KIBANA_BASE_DEFAULT, _normalize_index_id, _search_url
from logfather.data.settings_store import Settings

CATALOG_DAYS = 7

# Node name (last path segment, or the full name when it has no path)
# -> (kind, what it logs). Written from the PikPak010 audit and the
# fleet error investigation (Sep 2026).
NODE_INFO: dict[str, tuple[str, str]] = {
    "monitor_node": ("Pick telemetry", "Timing of every pick: timers created and fired, trajectory-end inference, per-run and per-SKU tags. The biggest producer by far."),
    "motion_control_node": ("State transitions", "The arm's motion controller changing state (moving, stopped, already_stopped_error and friends)."),
    "planner_node": ("State transitions", "The pick planner's state changes, including planner_error, plus the planner's own payloads."),
    "act_controller": ("State transitions", "Actuator controller states; mirrors the planner (a planner error shows here as already_stopped)."),
    "targeting_node": ("State transitions", "Product targeting (vision) state changes."),
    "behaviour_node": ("System state", "The system-level state machine: running, operator / protective / emergency stops, caution. The source of the Errors / Stops window's stoppages."),
    "health_node": ("Health and software", "Node health, node start-ups, 'Node git details' (branch and commit per package) and software versions."),
    "sensors_digital_output_node": ("Sensor / IO", "Digital outputs switching (valves, solenoids, gates)."),
    "sensors_digital_input_node": ("Sensor / IO", "Digital inputs (gate sensors, buttons, tray-change detection)."),
    "sensors_analog_input_node": ("Sensor / IO", "Analog readings such as air pressure and temperatures, with warning levels."),
    "sensors_external_lights_node": ("Sensor / IO", "Stack-light animations set for each state."),
    "controller_node": ("Machine control", "Conveyor and crate-change controllers: sensor polling statistics, roller and piston commands, crate eject results."),
    "distance_sensor_node": ("Sensor / IO", "Crate-change distance sensor states."),
    "quality_control_node": ("Quality control", "QC classification of trays and products: confidence, model version, bale-arm and tray states."),
    "product_alignment_tool": ("Tooling", "Service calls received by the product alignment tool."),
    "ui_node": ("Operator UI", "Operator screen selections and UI node states."),
    "logs_node": ("Housekeeping", "The logging node's own states (Argus 1 schema)."),
    "metrics_node": ("Housekeeping", "Metrics node states."),
    "performance_node": ("Housekeeping", "Performance node states (rare)."),
}


def describe_source(source: str) -> tuple[str, str]:
    name = str(source or "").rstrip("/").split("/")[-1]
    return NODE_INFO.get(name, ("Other", ""))


@dataclass
class CatalogEntry:
    source: str
    docs: int
    share: float  # 0..1 of the window's documents
    kind: str
    description: str
    examples: list[str] = field(default_factory=list)
    fields: list[str] = field(default_factory=list)


@dataclass
class ElasticCatalog:
    days: int
    total_docs: int
    entries: list[CatalogEntry]


_NOISE_FIELDS = {"source", "source_index", "image_url", "message", "timestamp_ros_nano", "event", "@timestamp", "@timestamp_ros"}


def parse_catalog(buckets: list, days: int = CATALOG_DAYS) -> ElasticCatalog:
    total = sum(int(b.get("doc_count") or 0) for b in buckets or [])
    entries: list[CatalogEntry] = []
    for b in buckets or []:
        source = str(b.get("key") or "")
        docs = int(b.get("doc_count") or 0)
        kind, description = describe_source(source)
        examples: list[str] = []
        fields: set[str] = set()
        for hit in ((b.get("ex") or {}).get("hits") or {}).get("hits") or []:
            doc = hit.get("_source") or {}
            msg = str(doc.get("message") or "").strip().replace("\n", " ")
            if msg and msg not in examples:
                examples.append(msg[:80])
            fields.update(k for k in doc if not k.startswith("@") and k not in _NOISE_FIELDS)
        entries.append(CatalogEntry(source, docs, (docs / total) if total else 0.0, kind, description, examples[:3], sorted(fields)))
    entries.sort(key=lambda e: -e.docs)
    return ElasticCatalog(days=days, total_docs=total, entries=entries)


def fetch_catalog(settings: Settings, days: int = CATALOG_DAYS, progress: Callable[[str], None] | None = None) -> ElasticCatalog:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    url = _search_url(url_base, _normalize_index_id(None))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"range": {"@timestamp": {"gte": since}}},
        "aggs": {"src": {"terms": {"field": "source.keyword", "size": 80},
                         "aggs": {"ex": {"top_hits": {"size": 3, "_source": {"excludes": ["*payload*", "*.data", "planner*"]}}}}}},
    }
    if progress:
        progress(f"Elastic: listing what the last {days} days hold...")
    resp = requests.post(url, json=body, headers=api_headers(api_key), timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    buckets = (((resp.json().get("aggregations") or {}).get("src") or {}).get("buckets")) or []
    return parse_catalog(buckets, days)
