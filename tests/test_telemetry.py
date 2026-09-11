from logfather.core.grafana import Series
from logfather.core.telemetry import (
    METRICS,
    Track,
    build_groups,
    downsample,
    is_absent,
    merge_series,
    robot_query,
    value_at,
    value_range,
)

SPEC = {m.key: m for m in METRICS}


def test_robot_query_selects_under_both_label_schemes():
    q = robot_query(SPEC["cpu_temp"], "35-2300-012")
    assert q == 'sensors_cpu_temperature{leap_robot_id="35-2300-012"} or sensors_cpu_temperature{system_id="35-2300-012"}'


def test_value_at_nearest_within_tolerance():
    times = [0, 30_000, 60_000, 90_000]
    values = [1.0, None, 3.0, 4.0]
    assert value_at(times, values, 31_000, 90_000) == 3.0  # 30_000 is None, so the next neighbour wins
    assert value_at(times, values, 59_000, 90_000) == 3.0
    assert value_at(times, values, 500_000, 90_000) is None
    assert value_at([], [], 5, 10) is None


def test_merge_series_stitches_runs_and_sorts():
    a = Series("x", [60_000, 90_000], [2.0, 3.0], {"run_id": "r2"})
    b = Series("x", [0, 30_000], [0.0, 1.0], {"run_id": "r1"})
    times, values = merge_series([a, b])
    assert times == [0, 30_000, 60_000, 90_000]
    assert values == [0.0, 1.0, 2.0, 3.0]


def test_is_absent_for_flat_zero_motor():
    assert is_absent([0.0, 0.0, None, 0.0])
    assert not is_absent([0.0, 0.3, 0.0])
    assert is_absent([None, None])


def test_build_groups_orders_and_drops_absent_motors():
    results = {
        "cpu_temp": [Series("cpu", [0, 30_000], [40.0, 41.0], {"leap_robot_id": "35-2300-007"})],
        "motor_current": [
            Series("c", [0, 30_000], [0.0, 0.0], {"motor_id": "0"}),
            Series("c", [0, 30_000], [1.0, 1.5], {"motor_id": "2"}),
            Series("c", [0], [0.8], {"motor_id": "1", "run_id": "a"}),
            Series("c", [30_000], [0.9], {"motor_id": "1", "run_id": "b"}),
        ],
        "motor_temp": [],
    }
    groups = build_groups(results)
    assert [g.name for g in groups] == ["Temperatures", "Motor currents"]
    currents = groups[1]
    assert currents.unit == "A"
    assert [t.name for t in currents.tracks] == ["Motor 1", "Motor 2"]
    assert currents.tracks[0].values == [0.8, 0.9]


def test_downsample_min_max_per_column():
    times = [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]
    values = [1.0, 5.0, 2.0, None, 3.0, 9.0, 1.0, 1.0, 1.0, 1.0]
    cols = downsample(times, values, 0, 100, 2)
    assert cols == [(0, 1.0, 5.0), (1, 1.0, 9.0)]
    assert downsample(times, values, 0, 100, 0) == []


def test_value_range_pads_and_handles_flat():
    lo, hi = value_range([Track("a", [0, 1], [10.0, 20.0])])
    assert lo < 10.0 and hi > 20.0
    assert value_range([Track("a", [0], [5.0])]) == (4.0, 6.0)
    assert value_range([]) == (0.0, 1.0)


def test_track_value_at_uses_sample_tolerance():
    t = Track("a", [0, 30_000], [1.0, 2.0])
    assert t.value_at(31_000) == 2.0
    assert t.value_at(10 * 60_000) is None


def test_summary_track_prefers_hottest_motor_then_cpu():
    from logfather.core.telemetry import TelemetryDay, TrackGroup, max_across, summary_track

    motors = TrackGroup("Motor temperatures", "°C", [
        Track("Motor 1", [0, 30_000, 60_000], [20.0, 35.0, None]),
        Track("Motor 2", [0, 30_000, 60_000], [25.0, 30.0, 28.0]),
    ])
    temps = TrackGroup("Temperatures", "°C", [Track("CPU", [0, 30_000], [40.0, 41.0], "cpu_temp")])
    data = TelemetryDay("35-2300-007", 0, 86_400_000, [temps, motors])
    track, unit = summary_track(data)
    assert unit == "°C" and track.name == "Hottest motor"
    assert track.times_ms == [0, 30_000, 60_000]
    assert track.values == [25.0, 35.0, 28.0]

    only_temps = TelemetryDay("35-2300-007", 0, 86_400_000, [temps])
    track, _ = summary_track(only_temps)
    assert track.name == "CPU temperature" and track.values == [40.0, 41.0]
    assert summary_track(None) is None
    assert max_across([], "x") is None


def test_fleet_query_and_tracks_by_robot():
    from logfather.core.telemetry import METRICS, fleet_query, normalise_robot_id, tracks_by_robot, window_stats

    spec = {m.key: m for m in METRICS}
    assert fleet_query(spec["cpu_temp"], "leap_robot_id") == 'max by(leap_robot_id) (sensors_cpu_temperature{leap_robot_id!=""})'
    assert fleet_query(spec["motor_temp"], "system_id") == 'max by(system_id) (actuators_motor_temperature{system_id!=""} != 0)'
    assert normalise_robot_id("013") == "35-2300-013" and normalise_robot_id("35-2300-013") == "35-2300-013"
    series = [
        Series("a", [0, 30_000], [40.0, 41.0], {"system_id": "013"}),
        Series("b", [60_000], [42.0], {"system_id": "35-2300-013"}),
        Series("c", [0], [50.0], {"system_id": "35-2300-006"}),
        Series("d", [0], [1.0], {}),
    ]
    tracks = tracks_by_robot(series, "system_id", "CPU", "cpu_temp")
    assert sorted(tracks) == ["35-2300-006", "35-2300-013"]
    assert tracks["35-2300-013"].values == [40.0, 41.0, 42.0] and tracks["35-2300-013"].name == "CPU"
    assert window_stats(tracks["35-2300-013"], 0, 45_000) == (40.0, 41.0, 41.0)
    assert window_stats(tracks["35-2300-013"], 100_000, 200_000) is None


def test_per_motor_temperature_keys():
    from logfather.core.telemetry import METRICS, TEMPERATURE_CHOICES, TEMPERATURE_COLOURS, fleet_query, parse_temperature_key

    assert parse_temperature_key("motor_temp_3") == ("motor_temp", "3")
    assert parse_temperature_key("cpu_temp") == ("cpu_temp", None)
    keys = [k for k, _ in TEMPERATURE_CHOICES]
    assert keys[:5] == ["cpu_temp", "rcu_temp", "gpu_temp", "brake_temp", "motor_temp"] and "motor_temp_6" in keys
    assert all(k in TEMPERATURE_COLOURS for k in keys)
    spec = {m.key: m for m in METRICS}["motor_temp"]
    assert fleet_query(spec, "leap_robot_id", "3") == 'max by(leap_robot_id) (actuators_motor_temperature{leap_robot_id!="", motor_id="3"} != 0)'


def test_current_keys_and_queries():
    from logfather.core.telemetry import CURRENT_CHOICES, CURRENT_COLOURS, METRICS, SIGNAL_LABELS, fleet_query, parse_signal_key

    keys = [k for k, _ in CURRENT_CHOICES]
    assert keys[0] == "motor_current" and "motor_current_5" in keys and "motor_current_4" not in keys
    assert all(k in CURRENT_COLOURS for k in keys)
    assert parse_signal_key("motor_current_5") == ("motor_current", "5")
    assert SIGNAL_LABELS["motor_current_2"] == "Motor 2" and SIGNAL_LABELS["cpu_temp"] == "CPU"
    spec = {m.key: m for m in METRICS}["motor_current"]
    assert fleet_query(spec, "system_id") == 'max by(system_id) (abs(actuators_motor_current{system_id!=""}))'
    assert fleet_query(spec, "leap_robot_id", "2") == 'max by(leap_robot_id) (actuators_motor_current{leap_robot_id!="", motor_id="2"})'


def test_air_pressure_is_a_metric_and_a_signal():
    from logfather.core.telemetry import METRICS, PRESSURE_CHOICES, SIGNAL_LABELS, fleet_query

    spec = {m.key: m for m in METRICS}["air_pressure"]
    assert spec.metric == "sensors_air_pressure" and spec.group == "Air pressure" and not spec.per_motor
    assert PRESSURE_CHOICES == (("air_pressure", "Air pressure"),)
    assert SIGNAL_LABELS["air_pressure"] == "Air pressure"
    assert fleet_query(spec, "leap_robot_id") == 'max by(leap_robot_id) (sensors_air_pressure{leap_robot_id!=""})'


def test_additional_channels_and_queries():
    from logfather.core.telemetry import ADDITIONAL_CHANNELS, METRICS, SIGNAL_LABELS, fleet_query

    spec = {m.key: m for m in METRICS}
    names = [c["name"] for c in ADDITIONAL_CHANNELS]
    assert names == ["cpu", "memory", "clock", "halting", "overcurrent", "logqueue", "canbus"]
    from logfather.data.event_counts import EVENT_SIGNALS

    for c in ADDITIONAL_CHANNELS:
        for key, _label in c["choices"]:
            assert key in c["colours"] and key in SIGNAL_LABELS
            # Grafana metrics, or event counts from Elastic (2026-09-11).
            assert (key in spec and not spec[key].in_replay) or key in EVENT_SIGNALS
    assert fleet_query(spec["ccu_cpu"], "leap_robot_id") == 'max by(leap_robot_id) (diagnostics_ccu_cpu_average{leap_robot_id!=""})'
    assert fleet_query(spec["halting_errors"], "system_id") == 'sum by(system_id) (actuators_motor_halting_errors{system_id!=""})'
    assert fleet_query(spec["motor_temp"], "system_id", "3") == 'max by(system_id) (actuators_motor_temperature{system_id!="", motor_id="3"} != 0)'
    assert all(m.in_replay for m in METRICS if m.key in ("cpu_temp", "motor_temp", "motor_current", "air_pressure"))


def test_parse_pick_buckets_fills_gaps_and_smooths():
    from logfather.data.pick_rate import bucket_seconds, parse_pick_buckets

    assert bucket_seconds(60) == 20 and bucket_seconds(5 * 24 * 60) == 300 and bucket_seconds(60 * 24 * 60) == 1800
    s20 = 20_000
    buckets = [
        {"key": 0, "per_robot": {"buckets": [{"key": "35-2300-013", "doc_count": 4}]}, "per_system_id": {"buckets": []}},
        {"key": 2 * s20, "per_robot": {"buckets": [{"key": "35-2300-013", "doc_count": 2}]}, "per_system_id": {"buckets": [{"key": "013", "doc_count": 3}]}},
    ]
    tracks = parse_pick_buckets(buckets, 20, smooth_seconds=40)
    t = tracks["35-2300-013"]
    assert t.times_ms == [0, s20, 2 * s20]            # the empty bucket is filled in
    assert t.values == [12.0, 6.0, 7.5]                # trailing 40 s window as a per-minute rate; both id fields merged
    five = parse_pick_buckets([{"key": 0, "per_robot": {"buckets": [{"key": "35-2300-005", "doc_count": 50}]}, "per_system_id": {"buckets": []}}], 300, smooth_seconds=60)
    assert five["35-2300-005"].values == [10.0]        # 50 picks in a 5-minute bucket


def test_track_step_and_gap_break_scale_with_the_sample_spacing():
    from logfather.core.telemetry import Track

    fine = Track("x", [0, 30_000, 60_000, 90_000], [1.0, 2.0, 3.0, 4.0])
    assert fine.step_ms() == 30_000 and fine.gap_break_ms() == 5 * 60_000
    coarse = Track("x", [0, 1_800_000, 3_600_000, 5_400_000], [1.0, None, 3.0, 4.0])
    assert coarse.step_ms() == 1_800_000 and coarse.gap_break_ms() == 4_500_000
    # hover readout: a 30-minute track answers within its own spacing
    assert coarse.value_at(4_000_000) == 3.0
    assert coarse.value_at(20_000_000) is None
    assert Track("x", [5], [1.0]).step_ms() == 30_000
