"""Software history per system from Elastic (Chris, 2026-09-05).

Argus 2 systems put ``sw_version.<package>`` on every document and log a
"Node git details" document per node at each start (``node``, ``branch``,
``commit_sha``). Combining the two gives, per package, a run of dated
spans "version (commit)". Argus 1 systems log neither; they appear as
rows with no spans.

Pure logic (span building) is separate from the fetching so it can be
tested without Elastic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from logfather.data.elastic_client import api_headers
from logfather.data.elastic_loader import (
    KIBANA_BASE_DEFAULT,
    _normalize_index_id,
    _search_url,
)
from logfather.data.settings_store import Settings

PACKAGES = ("argus", "planner", "targeting", "actuators", "sensors", "infeed", "crate_change", "behaviour")

# The node whose git commit stands for each versioned package.
PACKAGE_NODE = {
    "argus": "/leap/manip1/health_node",
    "planner": "/leap/manip1/planner_node",
    "targeting": "/leap/manip1/targeting_node",
    "actuators": "/leap/manip1/act_controller",
    "sensors": "/leap/manip1/sensors_digital_output_node",
    "infeed": "/leap/conveyor/controller_node",
    "crate_change": "/leap/crate_change/controller_node",
    "behaviour": "/leap/manip1/behaviour_node",
}

_SKIP_IDS = {"", "35-2300-SIM", "35-2300-XXX"}


@dataclass(frozen=True)
class DatedValue:
    value: str
    start: datetime
    end: datetime
    count: int = 0
    branch: str = ""


@dataclass(frozen=True)
class VersionSpan:
    package: str
    version: str
    commit: str
    branch: str
    start: datetime
    end: datetime
    node_starts: int


@dataclass
class SystemSoftware:
    robot_id: str
    name: str
    generation: str  # "Argus 2" | "Argus 1" | "unknown"
    first_seen: datetime | None
    last_seen: datetime | None
    spans: list[VersionSpan] = field(default_factory=list)
    note: str = ""


# ------------------------------------------------------------- pure logic


def system_display_name(robot_id: str) -> str:
    m = re.match(r"^\d+-\d+-(\d{3})(.*)$", robot_id)
    if not m:
        return robot_id
    suffix = m.group(2).strip("-")
    return f"PikPak{m.group(1)}" + (f" ({suffix})" if suffix else "")


def clean_sha(raw: str) -> str:
    return str(raw or "").strip().strip('"').strip()


def build_spans(
    package: str,
    versions: list[DatedValue],
    commits: list[DatedValue],
) -> list[VersionSpan]:
    """Fold version spans and commit spans for one package into labelled
    spans. Commit spans are the finer series (a version can hold several
    commits), so each commit span becomes one output span labelled with
    the version whose dates contain its midpoint (or overlap it most).
    With no commit data, version spans stand on their own."""
    out: list[VersionSpan] = []
    if commits:
        for c in sorted(commits, key=lambda d: d.start):
            mid = c.start + (c.end - c.start) / 2
            version = ""
            best_overlap = timedelta(0)
            for v in versions:
                if v.start <= mid <= v.end:
                    version = v.value
                    break
                overlap = min(v.end, c.end) - max(v.start, c.start)
                if overlap > best_overlap:
                    best_overlap = overlap
                    version = v.value
            out.append(VersionSpan(package, version, c.value, c.branch, c.start, c.end, c.count))
        return out
    for v in sorted(versions, key=lambda d: d.start):
        out.append(VersionSpan(package, v.value, "", "", v.start, v.end, v.count))
    return out


def commit_owners(systems: list["SystemSoftware"]) -> dict[tuple[str, str], set[str]]:
    """(package, commit) -> the systems that ran it. A key owned by one
    system is code nobody else runs - the thing to highlight (Chris,
    2026-09-05: the differences were invisible in a table)."""
    owners: dict[tuple[str, str], set[str]] = {}
    for system in systems:
        for span in system.spans:
            if span.commit:
                owners.setdefault((span.package, span.commit), set()).add(system.name)
    return owners


def merge_adjacent(spans: list[VersionSpan]) -> list[VersionSpan]:
    """Join consecutive spans with the same version+commit (a node that
    restarted often produces one bucket, but be safe)."""
    merged: list[VersionSpan] = []
    for s in sorted(spans, key=lambda x: x.start):
        if merged and merged[-1].version == s.version and merged[-1].commit == s.commit and merged[-1].package == s.package:
            prev = merged[-1]
            merged[-1] = VersionSpan(prev.package, prev.version, prev.commit, prev.branch, prev.start, max(prev.end, s.end), prev.node_starts + s.node_starts)
        else:
            merged.append(s)
    return merged


# ---------------------------------------------------------------- fetching


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def fetch_software_history(
    settings: Settings,
    days: int = 182,
    progress: Callable[[str], None] | None = None,
) -> list[SystemSoftware]:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    url = _search_url(url_base, _normalize_index_id(None))
    headers = api_headers(api_key)
    now = datetime.now(timezone.utc)
    window = {"range": {"@timestamp": {"gte": (now - timedelta(days=max(1, days))).isoformat(), "lte": now.isoformat()}}}

    def post(body: dict, timeout: int = 300) -> dict:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def spans_agg(body_filters: list, key_field: str, extra_sub: dict | None = None) -> list[dict]:
        aggs = {"t": {"terms": {"field": key_field, "size": 40},
                      "aggs": {"lo": {"min": {"field": "@timestamp"}}, "hi": {"max": {"field": "@timestamp"}}}}}
        if extra_sub:
            aggs["t"]["aggs"].update(extra_sub)
        data = post({"size": 0, "query": {"bool": {"filter": [window] + body_filters}}, "aggs": aggs})
        return data["aggregations"]["t"]["buckets"]

    if progress:
        progress("Software: listing systems...")
    systems: dict[str, SystemSoftware] = {}
    seen_under: dict[str, set[str]] = {}
    for id_field, generation in (("system_id.keyword", "Argus 2"), ("leap_robot_id.keyword", "Argus 1")):
        for b in spans_agg([], id_field):
            rid = str(b["key"])
            if rid in _SKIP_IDS:
                continue
            lo, hi = _parse_ts(b["lo"]["value_as_string"]), _parse_ts(b["hi"]["value_as_string"])
            seen_under.setdefault(rid, set()).add(generation)
            existing = systems.get(rid)
            if existing is None:
                systems[rid] = SystemSoftware(rid, system_display_name(rid), generation, lo, hi)
            else:
                # Seen under both id fields (a migrated system): keep the
                # earliest/latest and call it Argus 2 if it has any.
                existing.first_seen = min(existing.first_seen or lo, lo)
                existing.last_seen = max(existing.last_seen or hi, hi)
                if generation == "Argus 2":
                    existing.generation = "Argus 2"

    argus2 = [rid for rid, s in systems.items() if s.generation == "Argus 2"]
    for idx, rid in enumerate(sorted(argus2), start=1):
        if progress:
            progress(f"Software: {system_display_name(rid)} ({idx}/{len(argus2)})...")
        sys_filter = {"term": {"system_id.keyword": rid}}
        versions: dict[str, list[DatedValue]] = {}
        for pkg in PACKAGES:
            buckets = spans_agg([sys_filter, {"exists": {"field": f"sw_version.{pkg}"}}], f"sw_version.{pkg}.keyword")
            versions[pkg] = [DatedValue(str(b["key"]), _parse_ts(b["lo"]["value_as_string"]), _parse_ts(b["hi"]["value_as_string"]), b["doc_count"]) for b in buckets]
        # Commits per node in one query.
        data = post({"size": 0, "query": {"bool": {"filter": [window, sys_filter, {"exists": {"field": "commit_sha"}}]}},
                     "aggs": {"n": {"terms": {"field": "node.keyword", "size": 40},
                                    "aggs": {"c": {"terms": {"field": "commit_sha.keyword", "size": 15},
                                                   "aggs": {"b": {"terms": {"field": "branch.keyword", "size": 2}},
                                                            "lo": {"min": {"field": "@timestamp"}}, "hi": {"max": {"field": "@timestamp"}}}}}}}})
        commits_by_node: dict[str, list[DatedValue]] = {}
        for nb in data["aggregations"]["n"]["buckets"]:
            lst = []
            for cb in nb["c"]["buckets"]:
                branch = clean_sha(cb["b"]["buckets"][0]["key"]) if cb["b"]["buckets"] else ""
                lst.append(DatedValue(clean_sha(cb["key"]), _parse_ts(cb["lo"]["value_as_string"]), _parse_ts(cb["hi"]["value_as_string"]), cb["doc_count"], branch))
            commits_by_node[str(nb["key"])] = lst
        spans: list[VersionSpan] = []
        for pkg in PACKAGES:
            node_commits = commits_by_node.get(PACKAGE_NODE[pkg], [])
            spans.extend(merge_adjacent(build_spans(pkg, versions.get(pkg, []), node_commits)))
        systems[rid].spans = spans
        if not spans:
            if "Argus 1" in seen_under.get(rid, set()):
                # A few Argus 2-shaped documents (e.g. a QC node) on an
                # otherwise Argus 1 machine: classify by the bulk.
                systems[rid].generation = "Argus 1"
            else:
                systems[rid].note = "no version documents in this window"
    for rid, s in systems.items():
        if s.generation == "Argus 1":
            s.note = "Argus 1: no version or commit fields are logged"
    ordered = sorted(systems.values(), key=lambda s: (s.generation != "Argus 2", s.name.lower()))
    return ordered
