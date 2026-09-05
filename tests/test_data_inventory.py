"""data_inventory pure logic: day windows, histogram folding, formatting."""
from datetime import date

from logfather.data.data_inventory import (
    estimate_bytes,
    format_bytes,
    format_count,
    inventory_days,
    mean_source_bytes,
    parse_histogram,
    sum_cat_indices,
)


def test_sum_cat_indices_skips_malformed_rows():
    rows = [
        {"store.size": "1000", "docs.count": "10"},
        {"store.size": "abc", "docs.count": "5"},
        "not a row",
        {"store.size": 2000, "docs.count": 20},
    ]
    assert sum_cat_indices(rows) == (3000, 30)
    assert sum_cat_indices([]) == (0, 0)


def test_scale_robot_factors():
    from logfather.data.data_inventory import scale_robot_factors

    sampled = {"a": 400.0, "b": 800.0}
    docs = {"a": 300, "b": 100}
    # No real size known: sampled values pass through unchanged.
    assert scale_robot_factors(sampled, docs, None) == sampled
    # Weighted mean is 500; a real 1000 B/doc doubles every factor.
    scaled = scale_robot_factors(sampled, docs, 1000.0)
    assert scaled == {"a": 800.0, "b": 1600.0}
    assert scale_robot_factors({}, docs, 1000.0) == {}


def test_mean_source_bytes():
    hits = [{"_source": {"a": 1}}, {"_source": {"a": 1, "bb": "xx"}}, "junk"]
    # {"a":1} is 7 bytes; {"a":1,"bb":"xx"} is 17 bytes.
    assert mean_source_bytes(hits) == 12.0
    assert mean_source_bytes([]) is None


def test_inventory_days_is_trailing_window_oldest_first():
    days = inventory_days(date(2026, 9, 5), 3)
    assert days == [date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5)]
    assert inventory_days(date(2026, 9, 5), 0) == [date(2026, 9, 5)]


def test_parse_histogram_sums_both_id_fields_and_drops_other_days():
    days = [date(2026, 9, 4), date(2026, 9, 5)]
    buckets = [
        {
            "key_as_string": "2026-09-04T00:00:00.000+01:00",
            "per_robot": {"buckets": [{"key": "35-2300-012", "doc_count": 10}]},
            "per_system_id": {"buckets": [{"key": "35-2300-003", "doc_count": 4}]},
        },
        {
            "key_as_string": "2026-09-05T00:00:00.000+01:00",
            "per_robot": {"buckets": [{"key": "35-2300-012", "doc_count": 7}, {"key": "", "doc_count": 99}]},
            "per_system_id": {"buckets": []},
        },
        {
            "key_as_string": "2026-09-01T00:00:00.000+01:00",
            "per_robot": {"buckets": [{"key": "35-2300-012", "doc_count": 1000}]},
        },
        {"key_as_string": "not a date"},
    ]
    counts = parse_histogram(buckets, days)
    assert counts == {
        "35-2300-012": {date(2026, 9, 4): 10, date(2026, 9, 5): 7},
        "35-2300-003": {date(2026, 9, 4): 4},
    }


def test_estimate_bytes():
    assert estimate_bytes(10, 128 * 1024 * 1024) == 10 * 128 * 1024 * 1024
    assert estimate_bytes(0, 5) == 0
    assert estimate_bytes(5, None) == 0
    assert estimate_bytes(5, -1) == 0


def test_format_bytes_and_count():
    assert format_bytes(0) == "0 B"
    assert format_bytes(None) == "0 B"
    assert format_bytes(512) == "512 B"
    assert format_bytes(1536) == "2 KB"
    assert format_bytes(5 * 1024**3) == "5.0 GB"
    assert format_bytes(2.5 * 1024**4) == "2.5 TB"
    assert format_count(0) == "0"
    assert format_count(999) == "999"
    assert format_count(12_345) == "12k"
    assert format_count(2_895_676) == "2.90M"
