"""What kinds of data Elastic holds (Chris, 2026-09-06: the ? box on the
Data window's Elastic summary). One aggregation over the last week: a
bucket per source node with its document count and a few example
documents, joined to a curated description of what that node logs."""
from __future__ import annotations

import json
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


# ------------------------------------------------------------ field detail
# (Chris, 2026-09-06: click a field for what it is and the values it has
# held over the last year.)

FIELD_DAYS = 365
FIELD_TOP_VALUES = 300

# Curated notes on the common fields, from the audits; anything not
# listed gets a note derived from its name and mapping type.
FIELD_INFO: dict[str, str] = {
    "state_name": "The state the node moved into; one document per transition. Error and stop states are counted by the Errors / Stops window.",
    "previous_state_name": "The state the system left when this transition happened.",
    "state_sequence": "Running count of system state transitions since start-up.",
    "active_package_states": "Snapshot of every package's state at the moment of a system state change.",
    "run_id": "Identifier of the production run (Argus 2), so documents can be tied to one run.",
    "sku": "The SKU (product line) being packed at the time.",
    "sw_version": "Software version per package (argus, planner, targeting...). Feeds the Software window.",
    "system_id": "The PikPak system that wrote the document (Argus 2 schema).",
    "leap_robot_id": "The PikPak system that wrote the document (Argus 1 schema).",
    "severity": "Numeric log severity (Argus 1 schema only).",
    "event": "Short event name, used with message to say what happened.",
    "image_url": "Link to the image captured for this event, when one exists.",
    "source_index": "The Elastic index the document was routed to.",
    "timestamp_ros_nano": "The ROS time of the event in nanoseconds.",
    "timer_id": "Identifier of a pick timer (monitor node).",
    "task_name": "The task a pick timer belongs to.",
    "expected_timer_fire_time": "When a pick timer was due to fire.",
    "traj_id": "Identifier of the arm trajectory being timed.",
    "traj_type": "Kind of trajectory (pick, place, home...).",
    "algo_rem_sec": "Remaining trajectory time as estimated by the algorithm, seconds.",
    "diff_rem_sec": "Difference between estimated and actual remaining time, seconds.",
    "median_diff_sec": "Median of the estimate error so far, seconds.",
    "median_minus_raw_sec": "Median estimate error minus the raw error, seconds.",
    "max_d": "Largest deviation recorded for the trajectory.",
    "motor_id": "Which motor / servo a service call or warning concerns.",
    "servo_id": "Which servo a warning concerns.",
    "error_code": "The drive's numeric error code in a warning update.",
    "update_info": "Free text carried by a warning update.",
    "service_id": "Identifier of the ROS service that was called.",
    "service_path": "The ROS service path that was called.",
    "qc_confidence": "Confidence of the quality-control classification.",
    "qc_product": "The product the QC model classified.",
    "qc_model_version": "Version of the QC model in use.",
    "qc_model_loaded": "Whether a QC model was loaded.",
    "qc_model_trained": "Whether the QC model has been trained.",
    "qc_event": "The QC event that produced the document.",
    "qc_count": "Running count of QC classifications.",
    "qc_last_classification": "The last QC classification result.",
    "qc_config_file": "The QC configuration file in use.",
    "qc_rgb_classification_enabled": "Whether RGB classification is switched on.",
    "bale_arm_state": "State of the bale arm as seen by QC.",
    "bale_arm_state_seq": "Sequence number of bale-arm state changes.",
    "tray_state": "State of the tray as seen by QC.",
    "tray_state_seq": "Sequence number of tray state changes.",
    "trigger": "What triggered the QC event.",
    "animation": "The stack-light animation that was set.",
    "transition": "How the stack-light animation changes over.",
    "eject_crate_msg": "Message returned when a crate was ejected.",
    "eject_crate_result": "Whether the crate eject succeeded.",
    "check_piston_state_msg": "Message returned by the piston state check.",
    "check_piston_state_result": "Result of the piston state check.",
    "pin_num": "The IO pin a sensor reading refers to.",
    "port": "The IO port a reading or log line refers to.",
    "average": "Average of the polled value over the reporting window.",
    "min": "Minimum of the polled value over the reporting window.",
    "max": "Maximum of the polled value over the reporting window.",
    "facility": "Syslog facility (Argus 1 schema).",
    "host": "The host that wrote the log line (Argus 1 schema).",
    "logtype": "Log type tag (Argus 1 schema).",
    "state_code": "Numeric code of the state (Argus 1 schema).",
    "state_group": "The group the state belongs to (Argus 1 schema).",
    "state_id": "Numeric state identifier (Argus 1 schema).",
    "state_severity": "Severity attached to the state (Argus 1 schema).",
    "data_collection": "Whether data collection was on at the time.",
    "node": "The node whose git details are being reported.",
    "branch": "Git branch the node was built from.",
    "commit_sha": "Git commit the node was built from.",
    "target_ids": "Products targeted by a pick (Argus 2 movement).",
    "product_ids": "Products picked in a movement (Argus 1, since mid 2026).",
    "products_packed": "Products packed so far in the run.",
    "trays_packed": "Trays packed so far in the run.",
    "products_seen": "Products seen so far in the run.",
    "products_rejected": "Products rejected so far in the run.",
    "run_duration": "How long the run has been going.",
}

_NUMERIC_TYPES = {"long", "integer", "short", "byte", "double", "float", "half_float", "scaled_float", "unsigned_long"}


def describe_field(field: str, es_type: str = "") -> str:
    name = str(field or "")
    if name in FIELD_INFO:
        return FIELD_INFO[name]
    if name.startswith("sw_version."):
        return f"Software version of the {name.split('.', 1)[1]} package."
    if es_type in _NUMERIC_TYPES:
        return "A numeric value logged by this node (no curated note yet)."
    if es_type == "boolean":
        return "A true / false flag logged by this node (no curated note yet)."
    if es_type == "object":
        return "A nested object with its own sub-fields (no curated note yet)."
    return "A field logged by this node (no curated note yet)."


def pick_agg_field(field: str, caps: dict) -> tuple[str | None, str]:
    """(field to aggregate on, mapping type) from a _field_caps 'fields'
    map. Text fields use their .keyword twin; numbers, booleans, dates and
    keywords aggregate directly; objects and bare text cannot."""
    fields = caps.get("fields") or {}
    kw = f"{field}.keyword"
    if kw in fields and "keyword" in fields[kw]:
        return kw, "text"
    own = fields.get(field) or {}
    for es_type in own:
        if es_type in _NUMERIC_TYPES or es_type in ("boolean", "keyword", "date", "ip"):
            return field, es_type
    if any(k.startswith(field + ".") for k in fields):
        return None, "object"
    return None, next(iter(own), "unknown")


@dataclass
class FieldValues:
    field: str
    source: str
    days: int
    es_type: str
    description: str
    node_docs: int = 0
    docs_with_field: int = 0
    distinct: int | None = None
    values: list[tuple[str, int]] = field(default_factory=list)  # value, count (largest first)
    stats: dict | None = None  # min / max / avg for numbers
    subfields: list[str] = field(default_factory=list)
    sampled: bool = False  # values came from a sample of documents, not an aggregation


def parse_field_values(result: dict, field_name: str, source: str, days: int, es_type: str, description: str) -> FieldValues:
    out = FieldValues(field_name, source, days, es_type, description)
    out.node_docs = int((((result.get("hits") or {}).get("total") or {}).get("value")) or 0)
    aggs = result.get("aggregations") or {}
    with_field = aggs.get("with_field") or {}
    out.docs_with_field = int(with_field.get("doc_count") or 0)
    buckets = ((with_field.get("v") or {}).get("buckets")) or []
    out.values = [(str(b.get("key_as_string", b.get("key"))), int(b.get("doc_count") or 0)) for b in buckets]
    card = (with_field.get("c") or {}).get("value")
    out.distinct = int(card) if card is not None else None
    st = with_field.get("st")
    if isinstance(st, dict) and st.get("count"):
        out.stats = {k: st.get(k) for k in ("min", "max", "avg")}
    return out


def fetch_field_values(settings: Settings, source: str, field_name: str, days: int = FIELD_DAYS, progress: Callable[[str], None] | None = None) -> FieldValues:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    headers = api_headers(api_key)
    # field_caps only on the time-series indices: the bare "pikpak" name
    # in the pattern does not exist and makes the call 404.
    caps_url = _search_url(url_base, "logstash-*").split("/_search")[0] + f"/_field_caps?fields={field_name},{field_name}.keyword,{field_name}.*"
    if progress:
        progress(f"Elastic: mapping of {field_name}...")
    caps_resp = requests.get(caps_url, headers=headers, timeout=60)
    caps = caps_resp.json() if caps_resp.status_code == 200 else {}
    agg_field, es_type = pick_agg_field(field_name, caps)
    description = describe_field(field_name, es_type)
    url = _search_url(url_base, _normalize_index_id(None))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    filters = [{"term": {"source.keyword": source}}, {"range": {"@timestamp": {"gte": since}}}]
    if agg_field is None:
        # Object or bare text: show the sub-fields and a sample of values.
        out = FieldValues(field_name, source, days, es_type, description, sampled=True)
        out.subfields = sorted(k[len(field_name) + 1:] for k in (caps.get("fields") or {}) if k.startswith(field_name + ".") and not k.endswith(".keyword"))
        if progress:
            progress(f"Elastic: sampling {field_name} values...")
        body = {"size": 200, "track_total_hits": True, "_source": [field_name],
                "query": {"bool": {"filter": filters + [{"exists": {"field": field_name}}]}},
                "sort": [{"@timestamp": "desc"}]}
        resp = requests.post(url, json=body, headers=headers, timeout=180)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        out.docs_with_field = int((((data.get("hits") or {}).get("total") or {}).get("value")) or 0)
        counts: dict[str, int] = {}
        keys: set[str] = set()
        for hit in ((data.get("hits") or {}).get("hits") or []):
            value = (hit.get("_source") or {}).get(field_name)
            if isinstance(value, dict):
                keys.update(str(k) for k in value)
            text = json.dumps(value, sort_keys=True, default=str)[:160] if not isinstance(value, str) else value[:160]
            counts[text] = counts.get(text, 0) + 1
        if keys:
            # field_caps does not list an object's sub-fields for this key,
            # so read them off the sampled documents.
            out.es_type = "object"
            out.description = describe_field(field_name, "object")
            out.subfields = sorted(set(out.subfields) | keys)
        out.values = sorted(counts.items(), key=lambda kv: -kv[1])
        out.distinct = len(counts)
        return out
    if progress:
        progress(f"Elastic: values of {field_name} on {source.split('/')[-1]} over the last {days} days (can take a while)...")
    with_field: dict = {"filter": {"exists": {"field": field_name}}, "aggs": {
        "v": {"terms": {"field": agg_field, "size": FIELD_TOP_VALUES}},
        "c": {"cardinality": {"field": agg_field}},
    }}
    if es_type in _NUMERIC_TYPES:
        with_field["aggs"]["st"] = {"stats": {"field": field_name}}
    body = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": filters}}, "aggs": {"with_field": with_field}}
    resp = requests.post(url, json=body, headers=headers, timeout=600)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return parse_field_values(resp.json(), field_name, source, days, es_type, description)
