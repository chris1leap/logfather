"""event_counts pure logic: histogram buckets into running-total staircase tracks."""
from logfather.data.event_counts import EVENT_SIGNALS, parse_event_buckets


def _bucket(t_ms, robots):
    return {"key": t_ms, "per_robot": {"buckets": [{"key": r, "doc_count": n} for r, n in robots.items()]}, "per_system_id": {"buckets": []}}


def test_running_total_steps_up_at_each_event_and_holds_to_the_span_end():
    start, end, step = 1_000_000, 1_400_000, 20_000
    buckets = [_bucket(1_100_000, {"35-2300-007": 1}), _bucket(1_200_000, {"35-2300-007": 2})]
    tracks = parse_event_buckets(buckets, "motor_overcurrent", "Motor overcurrent trips", start, end, step)
    t = tracks["35-2300-007"]
    assert t.spec_key == "motor_overcurrent" and t.name == "Motor overcurrent trips"
    assert list(zip(t.times_ms, t.values)) == [
        (start, 0.0),
        (1_080_000, 0.0), (1_100_000, 1.0),
        (1_180_000, 1.0), (1_200_000, 3.0),
        (end, 3.0),
    ]


def test_systems_are_kept_apart_and_ids_normalised():
    buckets = [
        {"key": 5_000_000, "per_robot": {"buckets": [{"key": "35-2300-005", "doc_count": 1}]},
         "per_system_id": {"buckets": [{"key": "007", "doc_count": 4}]}},
    ]
    tracks = parse_event_buckets(buckets, "motor_overcurrent", "x", 4_000_000, 6_000_000)
    assert set(tracks) == {"35-2300-005", "35-2300-007"}
    assert tracks["35-2300-007"].values[-1] == 4.0 and tracks["35-2300-005"].values[-1] == 1.0


def test_adjacent_buckets_never_go_backwards_in_time():
    buckets = [_bucket(1_020_000, {"r": 1}), _bucket(1_040_000, {"r": 1})]
    t = parse_event_buckets(buckets, "motor_overcurrent", "x", 1_000_000, 1_100_000, 20_000)["r"]
    assert t.times_ms == sorted(t.times_ms) and len(set(t.times_ms)) == len(t.times_ms)
    assert t.values[-1] == 2.0


def test_signal_key_is_a_condition_style_search():
    assert EVENT_SIGNALS["motor_overcurrent"][0] == '"Current over limit"'
