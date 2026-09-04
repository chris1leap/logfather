"""overview_event_cache: round-trip, day guards, pruning, corruption."""
import json
from datetime import date, datetime, timedelta, timezone

from logfather.data.overview_event_cache import (
    SCHEMA_VERSION,
    _cache_path,
    load_overview_events,
    save_overview_events,
)


def _event(ts: datetime, state: str = "RUNNING", message: str = "ok") -> dict:
    return {
        "ts": ts,
        "state_name": state,
        "message": message,
        "service_name": "svc",
        "source": "src",
        "selection": {"sku": "S1", "tray": "T1", "tool": ""},
    }


def _today() -> date:
    return datetime.now().date()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def test_round_trip_preserves_events_and_latest_ts(tmp_path):
    t0 = _now_utc() - timedelta(hours=2)
    t1 = _now_utc() - timedelta(hours=1)
    events = {"35-2300-012": [_event(t1), _event(t0)], "35-2300-013": [_event(t0)]}
    assert save_overview_events(_today(), events, cache_root=tmp_path)
    loaded = load_overview_events(_today(), cache_root=tmp_path)
    assert loaded is not None
    by_robot, latest = loaded
    assert set(by_robot) == {"35-2300-012", "35-2300-013"}
    # Events come back sorted by ts with tz-aware UTC datetimes.
    assert [e["ts"] for e in by_robot["35-2300-012"]] == [t0, t1]
    assert by_robot["35-2300-012"][0]["selection"] == {"sku": "S1", "tray": "T1", "tool": ""}
    assert latest == t1


def test_save_refuses_non_today_and_empty(tmp_path):
    yesterday = _today() - timedelta(days=1)
    assert not save_overview_events(yesterday, {"r": [_event(_now_utc())]}, cache_root=tmp_path)
    assert not save_overview_events(_today(), {}, cache_root=tmp_path)
    assert not save_overview_events(None, {"r": [_event(_now_utc())]}, cache_root=tmp_path)
    assert list(tmp_path.glob("*.json")) == []


def test_load_refuses_non_today(tmp_path):
    assert save_overview_events(_today(), {"r": [_event(_now_utc())]}, cache_root=tmp_path)
    assert load_overview_events(_today() - timedelta(days=1), cache_root=tmp_path) is None
    assert load_overview_events(None, cache_root=tmp_path) is None


def test_save_prunes_other_day_files(tmp_path):
    stale = _cache_path(tmp_path, _today() - timedelta(days=3))
    tmp_path.mkdir(exist_ok=True)
    stale.write_text("{}", encoding="utf-8")
    assert save_overview_events(_today(), {"r": [_event(_now_utc())]}, cache_root=tmp_path)
    remaining = sorted(p.name for p in tmp_path.glob("*.json"))
    assert remaining == [_cache_path(tmp_path, _today()).name]


def test_corrupt_or_mismatched_file_returns_none(tmp_path):
    path = _cache_path(tmp_path, _today())
    tmp_path.mkdir(exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert load_overview_events(_today(), cache_root=tmp_path) is None
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION + 1, "day": _today().isoformat(), "events_by_robot": {}}),
        encoding="utf-8",
    )
    assert load_overview_events(_today(), cache_root=tmp_path) is None
    path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "day": "1999-01-01", "events_by_robot": {"r": []}}),
        encoding="utf-8",
    )
    assert load_overview_events(_today(), cache_root=tmp_path) is None


def test_events_without_valid_ts_are_skipped(tmp_path):
    good = _event(_now_utc())
    events = {"r": [good, {"state_name": "X"}, {"ts": 12345}]}
    assert save_overview_events(_today(), events, cache_root=tmp_path)
    loaded = load_overview_events(_today(), cache_root=tmp_path)
    assert loaded is not None
    by_robot, latest = loaded
    assert len(by_robot["r"]) == 1
    assert latest == good["ts"]


def test_naive_ts_string_is_read_as_utc(tmp_path):
    naive = datetime(2001, 1, 1, 12, 0, 0)
    path = _cache_path(tmp_path, _today())
    tmp_path.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "day": _today().isoformat(),
                "events_by_robot": {"r": [{"ts": naive.isoformat(), "state_name": "S"}]},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_overview_events(_today(), cache_root=tmp_path)
    assert loaded is not None
    by_robot, _latest = loaded
    assert by_robot["r"][0]["ts"] == naive.replace(tzinfo=timezone.utc)
