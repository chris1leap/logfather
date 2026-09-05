"""data_inventory pure logic: day windows, histogram folding, formatting."""
from datetime import date

from logfather.data.data_inventory import (
    estimate_bytes,
    format_bytes,
    format_count,
    inventory_days,
    parse_histogram,
)


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
