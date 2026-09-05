"""Software history per system from Elastic (Chris, 2026-09-05).

Argus 2 systems put ``sw_version.<package>`` on every document and log a
"Node git details" document per node at each start (``node``, ``branch``,
``commit_sha``). Combining the two gives, per package, a run of dated
spans "version (commit)". Argus 1 systems log neither; they appear as
rows with no spans.

The raw facts (first/last seen per version value and per commit) are
cached locally under LOCALAPPDATA; a refresh queries only the days since
the cache was written and merges them, and a range the cache already
covers needs no query at all (Chris, 2026-09-05: the first load was
slow). Pure logic (span building, merging, clipping) is separate from the
fetching so it can be tested without Elastic.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


# ---------------------------------------------------------- raw + cache

CACHE_SCHEMA = 1
REFRESH_OVERLAP = timedelta(days=1)


def _default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache" / "software_history.json"
    return Path.home() / ".videolog_cache" / "software_history.json"


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def empty_raw(start: datetime, end: datetime) -> dict:
    return {"schema": CACHE_SCHEMA, "start": _iso(start), "end": _iso(end), "systems": {}}


def _merge_entry(base: list | None, new: list) -> list:
    """[start, end, count, branch] merged: earliest start, latest end,
    summed count, newer branch if it has one."""
    if base is None:
        return list(new)
    return [
        min(base[0], new[0]),
        max(base[1], new[1]),
        int(base[2]) + int(new[2]),
        new[3] if len(new) > 3 and new[3] else (base[3] if len(base) > 3 else ""),
    ]


def merge_raw(base: dict, new: dict) -> dict:
    """Fold a newer raw window into the cached one (values keyed by
    string; first/last seen widen, counts add). Neither input is mutated."""
    out = {"schema": CACHE_SCHEMA, "start": min(base["start"], new["start"]), "end": max(base["end"], new["end"]),
           "systems": json.loads(json.dumps(base.get("systems", {})))}
    for rid, ns in new.get("systems", {}).items():
        bs = out["systems"].setdefault(rid, {"seen_under": [], "first_seen": ns["first_seen"], "last_seen": ns["last_seen"], "versions": {}, "commits": {}})
        bs["seen_under"] = sorted(set(bs.get("seen_under", [])) | set(ns.get("seen_under", [])))
        bs["first_seen"] = min(bs["first_seen"], ns["first_seen"])
        bs["last_seen"] = max(bs["last_seen"], ns["last_seen"])
        for pkg, values in ns.get("versions", {}).items():
            target = bs["versions"].setdefault(pkg, {})
            for value, entry in values.items():
                target[value] = _merge_entry(target.get(value), entry)
        for node, commits in ns.get("commits", {}).items():
            target = bs["commits"].setdefault(node, {})
            for sha, entry in commits.items():
                target[sha] = _merge_entry(target.get(sha), entry)
    return out


def _clip_values(values: dict, window_start: datetime, window_end: datetime) -> list[DatedValue]:
    out = []
    for value, entry in values.items():
        start, end = _parse_ts(entry[0]), _parse_ts(entry[1])
        if end < window_start or start > window_end:
            continue
        out.append(DatedValue(str(value), max(start, window_start), min(end, window_end), int(entry[2]), entry[3] if len(entry) > 3 else ""))
    return out


def build_systems(raw: dict, window_start: datetime, window_end: datetime) -> list[SystemSoftware]:
    """Systems with their spans for the window, from raw facts."""
    systems: list[SystemSoftware] = []
    for rid, entry in raw.get("systems", {}).items():
        if rid in _SKIP_IDS:
            continue
        first, last = _parse_ts(entry["first_seen"]), _parse_ts(entry["last_seen"])
        if last < window_start:
            continue
        spans: list[VersionSpan] = []
        for pkg in PACKAGES:
            versions = _clip_values(entry.get("versions", {}).get(pkg, {}), window_start, window_end)
            commits = _clip_values(entry.get("commits", {}).get(PACKAGE_NODE[pkg], {}), window_start, window_end)
            spans.extend(merge_adjacent(build_spans(pkg, versions, commits)))
        seen_under = set(entry.get("seen_under", []))
        if spans:
            generation = "Argus 2"
        elif "Argus 1" in seen_under:
            generation = "Argus 1"
        else:
            generation = "Argus 2" if "Argus 2" in seen_under else "unknown"
        note = ""
        if generation == "Argus 1":
            note = "Argus 1: no version or commit fields are logged"
        elif not spans:
            note = "no version documents in this window"
        systems.append(SystemSoftware(rid, system_display_name(rid), generation, max(first, window_start), min(last, window_end), spans, note))
    systems.sort(key=lambda s: (s.generation != "Argus 2", s.name.lower()))
    return systems


def load_cache(path: Path | None = None) -> dict | None:
    p = path if path is not None else _default_cache_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA or "start" not in data or "end" not in data:
        return None
    return data


def save_cache(raw: dict, path: Path | None = None) -> bool:
    p = path if path is not None else _default_cache_path()
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(raw, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- fetching


def _fetch_raw(settings: Settings, t_from: datetime, t_to: datetime, progress: Callable[[str], None] | None, label: str) -> dict:
    url_base = settings.elastic_url or KIBANA_BASE_DEFAULT
    api_key = settings.elastic_api_key or ""
    if not url_base or not api_key:
        raise RuntimeError("Elastic URL or API key missing in settings")
    url = _search_url(url_base, _normalize_index_id(None))
    headers = api_headers(api_key)
    window = {"range": {"@timestamp": {"gte": _iso(t_from), "lte": _iso(t_to)}}}

    def post(body: dict, timeout: int = 300) -> dict:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def spans_agg(body_filters: list, key_field: str) -> list[dict]:
        aggs = {"t": {"terms": {"field": key_field, "size": 40},
                      "aggs": {"lo": {"min": {"field": "@timestamp"}}, "hi": {"max": {"field": "@timestamp"}}}}}
        data = post({"size": 0, "query": {"bool": {"filter": [window] + body_filters}}, "aggs": aggs})
        return data["aggregations"]["t"]["buckets"]

    raw = empty_raw(t_from, t_to)
    if progress:
        progress(f"Software: {label} - listing systems...")
    for id_field, generation in (("system_id.keyword", "Argus 2"), ("leap_robot_id.keyword", "Argus 1")):
        for b in spans_agg([], id_field):
            rid = str(b["key"])
            if rid in _SKIP_IDS:
                continue
            lo, hi = b["lo"]["value_as_string"], b["hi"]["value_as_string"]
            entry = raw["systems"].setdefault(rid, {"seen_under": [], "first_seen": lo, "last_seen": hi, "versions": {}, "commits": {}})
            entry["seen_under"] = sorted(set(entry["seen_under"]) | {generation})
            entry["first_seen"] = min(entry["first_seen"], lo)
            entry["last_seen"] = max(entry["last_seen"], hi)
    argus2 = sorted(rid for rid, e in raw["systems"].items() if "Argus 2" in e["seen_under"])
    for idx, rid in enumerate(argus2, start=1):
        if progress:
            progress(f"Software: {label} - {system_display_name(rid)} ({idx}/{len(argus2)})...")
        sys_filter = {"term": {"system_id.keyword": rid}}
        entry = raw["systems"][rid]
        for pkg in PACKAGES:
            buckets = spans_agg([sys_filter, {"exists": {"field": f"sw_version.{pkg}"}}], f"sw_version.{pkg}.keyword")
            if buckets:
                entry["versions"][pkg] = {str(b["key"]): [b["lo"]["value_as_string"], b["hi"]["value_as_string"], b["doc_count"], ""] for b in buckets}
        data = post({"size": 0, "query": {"bool": {"filter": [window, sys_filter, {"exists": {"field": "commit_sha"}}]}},
                     "aggs": {"n": {"terms": {"field": "node.keyword", "size": 40},
                                    "aggs": {"c": {"terms": {"field": "commit_sha.keyword", "size": 15},
                                                   "aggs": {"b": {"terms": {"field": "branch.keyword", "size": 2}},
                                                            "lo": {"min": {"field": "@timestamp"}}, "hi": {"max": {"field": "@timestamp"}}}}}}}})
        for nb in data["aggregations"]["n"]["buckets"]:
            node_entry = entry["commits"].setdefault(str(nb["key"]), {})
            for cb in nb["c"]["buckets"]:
                branch = clean_sha(cb["b"]["buckets"][0]["key"]) if cb["b"]["buckets"] else ""
                node_entry[clean_sha(cb["key"])] = [cb["lo"]["value_as_string"], cb["hi"]["value_as_string"], cb["doc_count"], branch]
    return raw


def fetch_software_history(
    settings: Settings,
    days: int = 182,
    progress: Callable[[str], None] | None = None,
    cache_path: Path | None = None,
    use_cache: bool = True,
) -> list[SystemSoftware]:
    """Systems + spans for the last `days`, from the local cache plus only
    the days elapsed since it was written; a full fetch only when the
    cache does not reach back far enough (or is absent)."""
    now = datetime.now(timezone.utc)
    want_start = now - timedelta(days=max(1, days))
    cached = load_cache(cache_path) if use_cache else None
    if cached is not None and _parse_ts(cached["start"]) <= want_start:
        cache_end = _parse_ts(cached["end"])
        tail_from = max(want_start, cache_end - REFRESH_OVERLAP)
        elapsed = now - cache_end
        if elapsed > timedelta(minutes=5):
            new = _fetch_raw(settings, tail_from, now, progress, f"updating the last {max(1, elapsed.days + 1)} day(s)")
            raw = merge_raw(cached, new)
            raw["end"] = _iso(now)
            save_cache(raw, cache_path)
        else:
            raw = cached
    else:
        raw = _fetch_raw(settings, want_start, now, progress, f"full fetch of {days} days")
        if cached is not None:
            raw = merge_raw(cached, raw)
        save_cache(raw, cache_path)
    return build_systems(raw, want_start, now)
