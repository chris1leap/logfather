"""Errors and line stoppages per day per system, from Elastic (Chris,
2026-09-05: the Errors / Stops window).

Stops are the state transitions that halt the line (emergency,
protective, operator, caution). Errors are every ``state_name`` whose
name says error or failure, grouped into a handful of categories so a
day's total reads at a glance. Categorisation is pure logic (tested);
fetching is two aggregation queries over the day range.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Callable

import requests

from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import (
    KIBANA_BASE_DEFAULT,
    _normalize_index_id,
    _search_url,
)
from logfather.data.settings_store import Settings

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


def day_list(start_day: date, end_day: date) -> list[date]:
    return [start_day + timedelta(days=i) for i in range((end_day - start_day).days + 1)]


def _tz_offset(now_local: datetime) -> str:
    raw = now_local.strftime("%z") or "+0000"
    return f"{raw[:3]}:{raw[3:]}"


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

    def run(label: str, state_filter: dict, include: str | None) -> dict[date, dict[str, dict[str, int]]]:
        if progress:
            progress(f"Errors / Stops: counting {label}...")
        state_terms = {"terms": {"field": "state_name.keyword", "size": 120}}
        if include:
            state_terms["terms"]["include"] = include
        body = {
            "size": 0,
            "query": {"bool": {"filter": filters + [state_filter]}},
            "aggs": {"d": {"date_histogram": {"field": "@timestamp_ros", "calendar_interval": "1d", "time_zone": _tz_offset(now_local), "min_doc_count": 1},
                           "aggs": {f"r{i}": {"terms": {"field": f, "size": 60}, "aggs": {"s": state_terms}}
                                    for i, f in enumerate(("leap_robot_id.keyword", "system_id.keyword"))}}},
        }
        resp = requests.post(url, json=body, headers=headers, timeout=300)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        out: dict[date, dict[str, dict[str, int]]] = {}
        for b in resp.json()["aggregations"]["d"]["buckets"]:
            try:
                day = date.fromisoformat(b["key_as_string"][:10])
            except ValueError:
                continue
            for i in range(2):
                for rb in b[f"r{i}"]["buckets"]:
                    robot = str(rb["key"])
                    if robot in _SKIP_IDS:
                        continue
                    states = out.setdefault(day, {}).setdefault(robot, {})
                    for sb in rb["s"]["buckets"]:
                        states[str(sb["key"])] = states.get(str(sb["key"]), 0) + sb["doc_count"]
        return out

    data.stops = run("line stoppages", {"terms": {"state_name.keyword": sorted(STOP_KINDS)}}, None)
    data.errors = run("errors", {"regexp": {"state_name.keyword": ".*(error|fail).*"}}, ".*(error|fail).*")
    # Stop states never count as errors even if a name matched both.
    for robots in data.errors.values():
        for states in robots.values():
            for name in list(states):
                if name in STOP_KINDS:
                    states.pop(name)
    return data
