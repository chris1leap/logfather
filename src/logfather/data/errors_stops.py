"""Errors and line stoppages per day per system, from Elastic (Chris,
2026-09-05: the Errors / Stops window).

Stops are the state transitions that halt the line (emergency,
protective, operator, caution). Errors are every ``state_name`` whose
name says error or failure, grouped into a handful of categories so a
day's total reads at a glance.

Counting is per event, not per document (Chris, 2026-09-11): one failure
is logged as a cascade of state changes a few hundred milliseconds apart
(a controller node error, then crate_change_package_error, then the
generic package_error; or already_stopped_error paired with
planner_error). The state-change documents are fetched in time order and
every document from the same system within EVENT_WINDOW of the first is
folded into that event, which takes the first document's state. Stops and
errors are clustered separately, so a stop and an error at the same
moment stay one of each. Categorisation and clustering are pure logic
(tested); fetching pages through the documents by time.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Callable, Iterable

import requests

from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import (
    KIBANA_BASE_DEFAULT,
    _normalize_index_id,
    _search_url,
)
from logfather.data.elastic_schema import extract_hit_robot_id
from logfather.data.settings_store import Settings

# Documents from one system within this much of an event's first document
# are the same event.
EVENT_WINDOW = timedelta(seconds=2)
FETCH_PAGE_SIZE = 10000
# One sentence for the window, kept next to the rule it describes.
COUNTING_NOTE = (
    "One failure is logged as several state changes a few hundred milliseconds apart, so "
    "documents from the same system within two seconds of the first are counted as one event, "
    "named by the first state. Stops and errors are counted separately."
)

STOP_KINDS: dict[str, str] = {
    "hardware_emergency_stop": "Emergency stop",
    "operator_stop_from_e_stop": "Emergency stop",
    "hardware_protective_stop": "Protective stop",
    "ui_protective_stop": "Protective stop",
    "operator_stop": "Operator stop",
    "caution": "Caution",
}
STOP_KIND_ORDER = ("Emergency stop", "Protective stop", "Operator stop", "Caution")

ERROR_CATEGORY_ORDER = ("Planner", "Targeting", "Motion", "Sensors", "Drives / power", "Crate change", "System", "Other")
_ERROR_RULES: tuple[tuple[str, str], ...] = (
    (r"planner", "Planner"),
    (r"targeting", "Targeting"),
    (r"already_stopped|already_moving|motion_control|actuators_package|pvt_error|execution_failed|go_home|going_home", "Motion"),
    (r"_reading_error|_warning_level|sensors_package|init_error_sensors|vacuum_solenoid|tray_change|gate_sensor|mosfet", "Sensors"),
    (r"high_current|high_temp|psu_dc_ok|nodes_dead", "Drives / power"),
    (r"controller_node_|crate_change|gate_operation|io_output_control|piston|eject", "Crate change"),
    (r"enabling_failed|disabling_failed|launch_failed|nodes_inactive|bad_start_time|start_pnp|prepare_pnp|clean_up|socket_offline|^package_error$|^error$", "System"),
)
_ERROR_NAME = re.compile(r"error|fail")
_SKIP_IDS = {"", "35-2300-SIM", "35-2300-XXX"}


def stop_kind(state_name: str) -> str | None:
    return STOP_KINDS.get(str(state_name or "").strip())


def is_error_state(state_name: str) -> bool:
    name = str(state_name or "").strip()
    return bool(name) and name not in STOP_KINDS and bool(_ERROR_NAME.search(name))


def categorize_error(state_name: str) -> str:
    name = str(state_name or "").strip().lower()
    for pattern, category in _ERROR_RULES:
        if re.search(pattern, name):
            return category
    return "Other"


@dataclass
class ErrorsStopsData:
    days: list[date]
    # day -> robot -> state -> count
    stops: dict[date, dict[str, dict[str, int]]] = field(default_factory=dict)
    errors: dict[date, dict[str, dict[str, int]]] = field(default_factory=dict)

    def stop_series(self) -> dict[str, dict[date, int]]:
        """kind -> day -> count"""
        out: dict[str, dict[date, int]] = {k: {} for k in STOP_KIND_ORDER}
        for day, robots in self.stops.items():
            for states in robots.values():
                for state, n in states.items():
                    kind = stop_kind(state)
                    if kind:
                        out[kind][day] = out[kind].get(day, 0) + n
        return out

    def error_series(self) -> dict[str, dict[date, int]]:
        out: dict[str, dict[date, int]] = {c: {} for c in ERROR_CATEGORY_ORDER}
        for day, robots in self.errors.items():
            for states in robots.values():
                for state, n in states.items():
                    cat = categorize_error(state)
                    out[cat][day] = out[cat].get(day, 0) + n
        return out

    def per_system(self) -> dict[str, dict[str, object]]:
        """robot -> {stops, errors, top_error (state, count)}"""
        out: dict[str, dict[str, object]] = {}
        for table, key in ((self.stops, "stops"), (self.errors, "errors")):
            for robots in table.values():
                for robot, states in robots.items():
                    entry = out.setdefault(robot, {"stops": 0, "errors": 0, "states": defaultdict(int)})
                    for state, n in states.items():
                        entry[key] += n
                        if key == "errors":
                            entry["states"][state] += n
        for entry in out.values():
            states = entry.pop("states")
            entry["top_error"] = max(states.items(), key=lambda kv: kv[1]) if states else ("", 0)
        return out

    def merge(self, other: "ErrorsStopsData") -> None:
        """Take another fetch's days into this one (its days replace ours)."""
        for day in other.days:
            self.stops.pop(day, None)
            self.errors.pop(day, None)
        self.stops.update(other.stops)
        self.errors.update(other.errors)
        self.days = sorted(set(self.days) | set(other.days))

    def system_series(self, table: str) -> dict[str, dict[date, int]]:
        """robot -> day -> total (stops or errors) for one bar per system."""
        source = self.stops if table == "stops" else self.errors
        out: dict[str, dict[date, int]] = {}
        for day, robots in source.items():
            for robot, states in robots.items():
                total = sum(states.values())
                if total:
                    out.setdefault(robot, {})[day] = total
        return out

    def system_day_states(self, table: str, robot: str, day: date) -> dict[str, int]:
        """state -> count for one system on one day."""
        source = self.stops if table == "stops" else self.errors
        return dict(source.get(day, {}).get(robot, {}))

    def day_breakdown(self, table: str, day: date, selector: Callable[[str], bool]) -> dict[str, int]:
        """robot -> count for one day, over the states the selector accepts."""
        source = self.stops if table == "stops" else self.errors
        out: dict[str, int] = {}
        for robot, states in source.get(day, {}).items():
            n = sum(c for state, c in states.items() if selector(state))
            if n:
                out[robot] = n
        return out


def cluster_events(
    docs: Iterable[tuple[datetime, str, str]],
    window: timedelta = EVENT_WINDOW,
    local_day: Callable[[datetime], date] | None = None,
) -> tuple[dict[date, dict[str, dict[str, int]]], dict[date, dict[str, dict[str, int]]]]:
    """Fold time-ordered (timestamp, robot, state) documents into events.

    A document starts a new event unless it is within ``window`` of the
    first document of the open event for the same robot and table (stops
    or errors). Each event counts once under its first document's state,
    on the local day of that document. Returns (stops, errors), each
    day -> robot -> state -> count.
    """
    to_day = local_day or (lambda ts: ts.astimezone().date())
    stops: dict[date, dict[str, dict[str, int]]] = {}
    errors: dict[date, dict[str, dict[str, int]]] = {}
    open_start: dict[tuple[str, str], datetime] = {}
    for ts, robot, state in docs:
        if stop_kind(state):
            table, out = "stops", stops
        elif is_error_state(state):
            table, out = "errors", errors
        else:
            continue
        key = (table, robot)
        start = open_start.get(key)
        if start is not None and timedelta(0) <= ts - start <= window:
            continue
        open_start[key] = ts
        states = out.setdefault(to_day(ts), {}).setdefault(robot, {})
        states[state] = states.get(state, 0) + 1
    return stops, errors


def _parse_ts(value: object) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def day_list(start_day: date, end_day: date) -> list[date]:
    return [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]


def fetch_errors_stops(
    settings: Settings,
    start_day: date,
    end_day: date,
    robot_ids: set[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> ErrorsStopsData:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    url = _search_url(url_base, _normalize_index_id(None))
    headers = api_headers(api_key)
    now_local = datetime.now().astimezone()
    t_from = datetime.combine(start_day, dt_time.min).astimezone()
    t_to = min(now_local, datetime.combine(end_day, dt_time.min).astimezone() + timedelta(days=1))
    days = day_list(start_day, end_day)
    data = ErrorsStopsData(days=days)
    filters: list[dict] = [{"range": {"@timestamp_ros": {"gte": t_from.isoformat(), "lt": t_to.isoformat()}}}]
    if robot_ids:
        filters.append({"bool": {"should": [
            {"terms": {"leap_robot_id.keyword": sorted(robot_ids)}},
            {"terms": {"system_id.keyword": sorted(robot_ids)}},
        ], "minimum_should_match": 1}})

    filters.append({"bool": {"should": [
        {"terms": {"state_name.keyword": sorted(STOP_KINDS)}},
        {"regexp": {"state_name.keyword": ".*(error|fail).*"}},
    ], "minimum_should_match": 1}})

    # Page through the state-change documents in time order. Pages are cut
    # by timestamp (the next page starts at the last timestamp seen, and
    # ids already taken at that instant are skipped) so no point-in-time
    # or search_after tiebreaker field is needed.
    docs: list[tuple[datetime, str, str]] = []
    after_ts: str | None = None
    seen_at_after: set[str] = set()
    while True:
        if progress:
            progress(f"Errors / Stops: reading state changes ({len(docs):,} so far)...")
        page_filters = list(filters)
        if after_ts is not None:
            page_filters.append({"range": {"@timestamp_ros": {"gte": after_ts}}})
        body = {
            "size": FETCH_PAGE_SIZE,
            "track_total_hits": False,
            "sort": [{"@timestamp_ros": {"order": "asc", "format": "strict_date_optional_time_nanos"}}],
            "_source": ["@timestamp_ros", "state_name", "leap_robot_id", "system_id"],
            "query": {"bool": {"filter": page_filters}},
        }
        resp = requests.post(url, json=body, headers=headers, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        hits = resp.json().get("hits", {}).get("hits", [])
        new = 0
        for hit in hits:
            if hit.get("_id") in seen_at_after:
                continue
            src = hit.get("_source") or {}
            ts = _parse_ts(src.get("@timestamp_ros"))
            robot = extract_hit_robot_id(src) or ""
            state = str(src.get("state_name") or "").strip()
            if ts is None or robot in _SKIP_IDS or not state:
                continue
            docs.append((ts, robot, state))
            new += 1
        if len(hits) < FETCH_PAGE_SIZE:
            break
        last_raw = _sort_key(hits[-1])
        if last_raw is None or (last_raw == after_ts and new == 0):
            break
        after_ts = last_raw
        seen_at_after = {str(h.get("_id")) for h in hits if _sort_key(h) == last_raw}

    if progress:
        progress(f"Errors / Stops: grouping {len(docs):,} state changes into events...")
    docs.sort(key=lambda d: d[0])
    tz = now_local.tzinfo
    data.stops, data.errors = cluster_events(docs, EVENT_WINDOW, lambda ts: ts.astimezone(tz).date())
    return data


def _sort_key(hit: dict) -> str | None:
    """The timestamp a page was cut at: the sort value, else the source field."""
    sort_vals = hit.get("sort") or []
    if sort_vals and sort_vals[0] is not None:
        return str(sort_vals[0])
    raw = (hit.get("_source") or {}).get("@timestamp_ros")
    return str(raw) if raw is not None else None
