"""Fleet-wide overview event cache: today's raw events on disk.

The overview always shows the current day and, within a session, only
fetches events newer than the last one it saw. Across app restarts that
memory was lost, so the first Overview open re-fetched the whole day for
the entire fleet. This cache persists the merged raw events per robot;
a fresh session seeds from it and fetches only the tail.

Only the current LOCAL day is ever stored (one file, replaced on save);
past-day files are useless — the overview never shows them — and are
pruned on save. Any parse or schema problem simply falls back to a full
fetch, so a corrupt file can never wedge the overview.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

# Bump on any change to the stored event dict shape (see
# fetch_overview_event_chunks for the producer).
SCHEMA_VERSION = 1

_FILENAME_PREFIX = "overview_events_"


def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache" / "overview_events"
    return Path.home() / ".videolog_cache" / "overview_events"


def _cache_path(cache_root: Path, day: date) -> Path:
    return cache_root / f"{_FILENAME_PREFIX}{day:%Y%m%d}.json"


def _serialize_event(event: dict) -> dict | None:
    ts = event.get("ts")
    if not isinstance(ts, datetime):
        return None
    out = dict(event)
    out["ts"] = ts.astimezone(timezone.utc).isoformat()
    return out


def _deserialize_event(event: object) -> dict | None:
    if not isinstance(event, dict):
        return None
    ts_raw = event.get("ts")
    if not isinstance(ts_raw, str):
        return None
    try:
        ts = datetime.fromisoformat(ts_raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    out = dict(event)
    out["ts"] = ts.astimezone(timezone.utc)
    return out


def save_overview_events(
    day: object, events_by_robot: dict, cache_root: Path | None = None
) -> bool:
    """Persist today's merged events; prunes files for any other day.

    Refuses non-today days: yesterday's file would never be read again,
    and overwriting today's file with a stale day would poison the seed.
    """
    if not isinstance(day, date) or day != datetime.now().date():
        return False
    serialized: dict[str, list[dict]] = {}
    for robot_id, events in (events_by_robot or {}).items():
        if not robot_id:
            continue
        rows = [
            row
            for row in (_serialize_event(evt) for evt in (events or []))
            if row is not None
        ]
        if rows:
            serialized[str(robot_id)] = rows
    if not serialized:
        return False
    payload = {
        "schema_version": SCHEMA_VERSION,
        "day": day.isoformat(),
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "events_by_robot": serialized,
    }
    root = cache_root if cache_root is not None else _default_cache_root()
    target = _cache_path(root, day)
    # Unique tmp name: concurrent saves may overlap; os.replace keeps the
    # visible file atomic either way and the last writer wins.
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        root.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    for stale in root.glob(f"{_FILENAME_PREFIX}*.json"):
        if stale.name != target.name:
            try:
                stale.unlink()
            except OSError:
                pass
    return True


def load_overview_events(
    day: object, cache_root: Path | None = None
) -> tuple[dict[str, list[dict]], datetime | None] | None:
    """(events_by_robot, latest_ts) from today's cache file, else None."""
    if not isinstance(day, date) or day != datetime.now().date():
        return None
    root = cache_root if cache_root is not None else _default_cache_root()
    path = _cache_path(root, day)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("day") != day.isoformat():
        return None
    raw = data.get("events_by_robot")
    if not isinstance(raw, dict):
        return None
    events_by_robot: dict[str, list[dict]] = {}
    latest_ts: datetime | None = None
    for robot_id, events in raw.items():
        if not isinstance(events, list):
            continue
        rows: list[dict] = []
        for entry in events:
            evt = _deserialize_event(entry)
            if evt is None:
                continue
            rows.append(evt)
            if latest_ts is None or evt["ts"] > latest_ts:
                latest_ts = evt["ts"]
        if rows:
            rows.sort(key=lambda evt: evt["ts"])
            events_by_robot[str(robot_id)] = rows
    if not events_by_robot:
        return None
    return events_by_robot, latest_ts
