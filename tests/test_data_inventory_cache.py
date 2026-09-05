"""Data window local cache: which days to fetch, merging, samples, and
the Kibana Discover link."""
from datetime import date, datetime, timedelta, timezone

from logfather.data.data_inventory import (
    ElasticInventory,
    cache_saved_at,
    inventory_from_cache,
    cached_day_counts,
    cached_samples,
    days_to_fetch,
    inventory_days,
    kibana_discover_url,
    load_inventory_cache,
    merge_inventory_cache,
    save_inventory_cache,
)


def _cache(today: date) -> dict:
    d1, d2 = today - timedelta(days=2), today - timedelta(days=1)
    return {
        "schema": 1,
        "counts": {"35-2300-007": {d1.isoformat(): 100, d2.isoformat(): 200, today.isoformat(): 5}},
        "complete_days": [d1.isoformat(), d2.isoformat()],
        "oldest_ts": "2022-03-18T00:00:00+00:00",
        "sampled": {"35-2300-007": 900.0},
        "sampled_at": datetime.now(timezone.utc).isoformat(),
    }


def test_cached_days_and_fetch_run():
    today = date(2026, 9, 5)
    days = inventory_days(today, 4)  # 2,3,4,5 Sep
    counts, complete = cached_day_counts(_cache(today), days)
    assert complete == {date(2026, 9, 3), date(2026, 9, 4)}
    assert counts == {"35-2300-007": {date(2026, 9, 3): 100, date(2026, 9, 4): 200}}
    # 2 Sep is missing, so everything from it onward is fetched.
    assert days_to_fetch(days, complete) == days
    assert days_to_fetch(days, complete | {date(2026, 9, 2)}) == [today]
    assert days_to_fetch(days, set(days)) == [today]
    assert cached_day_counts(None, days) == ({}, set())


def test_merge_marks_finished_days_and_keeps_history(tmp_path):
    today = date(2026, 9, 5)
    inv = ElasticInventory(days=inventory_days(today, 3))
    inv.counts = {"35-2300-007": {date(2026, 9, 4): 250, today: 9}, "35-2300-010": {today: 3}}
    inv.oldest_ts = None
    merged = merge_inventory_cache(_cache(today), inv, [date(2026, 9, 4), today], today, {"35-2300-007": 900.0}, datetime.now(timezone.utc))
    assert merged["complete_days"] == ["2026-09-03", "2026-09-04"]
    per = merged["counts"]["35-2300-007"]
    assert per["2026-09-03"] == 100 and per["2026-09-04"] == 250 and per["2026-09-05"] == 9
    assert merged["counts"]["35-2300-010"] == {"2026-09-05": 3}
    assert merged["oldest_ts"] == "2022-03-18T00:00:00+00:00"
    path = tmp_path / "inv.json"
    assert save_inventory_cache(merged, path)
    assert load_inventory_cache(path)["counts"] == merged["counts"]
    assert load_inventory_cache(tmp_path / "missing.json") is None


def test_samples_expire_after_a_week():
    now = datetime.now(timezone.utc)
    fresh = {"sampled": {"a": 500, "b": 0}, "sampled_at": now.isoformat()}
    assert cached_samples(fresh, now) == {"a": 500.0}
    stale = {"sampled": {"a": 500}, "sampled_at": (now - timedelta(days=8)).isoformat()}
    assert cached_samples(stale, now) == {}
    assert cached_samples(None, now) == {}


def test_kibana_url_targets_system_and_day():
    url = kibana_discover_url("https://x.es.example.com:9243/", "35-2300-007", date(2026, 9, 4))
    assert url.startswith("https://x.kb.example.com:9243/app/discover#/?_g=(time:(from:'2026-09-0")
    assert "leap_robot_id:%2235-2300-007%22%20or%20system_id:%2235-2300-007%22" in url
    assert "language:kuery" in url


def test_inventory_from_cache_rebuilds_window():
    today = date(2026, 9, 5)
    cache = _cache(today)
    cache.update({"total_docs": 1000, "total_bytes": 900000, "bytes_per_doc": 900.0, "bytes_basis": "estimated"})
    inv = inventory_from_cache(cache, inventory_days(today, 3))
    assert inv is not None
    assert inv.counts == {"35-2300-007": {date(2026, 9, 3): 100, date(2026, 9, 4): 200, today: 5}}
    assert inv.total_docs == 1000 and inv.total_bytes == 900000
    assert inv.bytes_factor("35-2300-007") == 900.0
    assert inv.oldest_ts.year == 2022
    assert inventory_from_cache(None, inventory_days(today, 3)) is None
    assert cache_saved_at({"saved_at": "2026-09-04T10:00:00+00:00"}).date() == date(2026, 9, 4)
    assert cache_saved_at({}) is None
