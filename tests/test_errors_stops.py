"""errors_stops pure logic: classification and the series/summaries."""
from datetime import date, datetime, timedelta, timezone

from logfather.data.errors_stops import (
    ErrorsStopsData,
    categorize_error,
    cluster_events,
    day_list,
    is_error_state,
    stop_kind,
)


def test_stop_kinds_and_error_detection():
    assert stop_kind("hardware_emergency_stop") == "Emergency stop"
    assert stop_kind("operator_stop_from_e_stop") == "Emergency stop"
    assert stop_kind("ui_protective_stop") == "Protective stop"
    assert stop_kind("operator_stop") == "Operator stop"
    assert stop_kind("caution") == "Caution"
    assert stop_kind("planner_error") is None
    assert is_error_state("planner_error")
    assert is_error_state("enabling_failed")
    assert not is_error_state("operator_stop")
    assert not is_error_state("gate_sensor_on")


def test_categorize_error_rules():
    assert categorize_error("planner_error") == "Planner"
    assert categorize_error("planner_package_error") == "Planner"
    assert categorize_error("targeting_package_error") == "Targeting"
    assert categorize_error("already_stopped_error") == "Motion"
    assert categorize_error("go_home_failed") == "Motion"
    assert categorize_error("air_pressure_reading_error") == "Sensors"
    assert categorize_error("brake_resistor_temperature_value_warning_level") == "Sensors"
    assert categorize_error("high_current_error") == "Drives / power"
    assert categorize_error("controller_node_shift_crate_piston_error") == "Crate change"
    assert categorize_error("io_output_control_srv_failed") == "Crate change"
    assert categorize_error("enabling_failed") == "System"
    assert categorize_error("package_error") == "System"
    assert categorize_error("error") == "System"
    assert categorize_error("something_weird_error") == "Other"


def test_series_and_summaries():
    d1, d2 = date(2026, 9, 4), date(2026, 9, 5)
    data = ErrorsStopsData(days=day_list(d1, d2))
    data.stops = {
        d1: {"35-2300-007": {"operator_stop": 3, "hardware_emergency_stop": 1}},
        d2: {"35-2300-007": {"operator_stop": 2}, "35-2300-006": {"caution": 4}},
    }
    data.errors = {
        d1: {"35-2300-007": {"planner_error": 10, "already_stopped_error": 5}},
        d2: {"35-2300-006": {"air_pressure_reading_error": 7}},
    }
    stops = data.stop_series()
    assert stops["Operator stop"] == {d1: 3, d2: 2}
    assert stops["Emergency stop"] == {d1: 1}
    assert stops["Caution"] == {d2: 4}
    errors = data.error_series()
    assert errors["Planner"] == {d1: 10}
    assert errors["Motion"] == {d1: 5}
    assert errors["Sensors"] == {d2: 7}
    per = data.per_system()
    assert per["35-2300-007"]["stops"] == 6 and per["35-2300-007"]["errors"] == 15
    assert per["35-2300-007"]["top_error"] == ("planner_error", 10)
    assert per["35-2300-006"]["stops"] == 4 and per["35-2300-006"]["top_error"] == ("air_pressure_reading_error", 7)
    assert data.day_breakdown("stops", d2, lambda s: stop_kind(s) == "Caution") == {"35-2300-006": 4}
    assert data.day_breakdown("errors", d1, lambda s: categorize_error(s) == "Planner") == {"35-2300-007": 10}
    assert day_list(d1, d2) == [d1, d2]


def test_system_series_and_day_states():
    d1, d2 = date(2026, 9, 4), date(2026, 9, 5)
    data = ErrorsStopsData(days=day_list(d1, d2))
    data.errors = {
        d1: {"35-2300-007": {"planner_error": 10, "already_stopped_error": 5}},
        d2: {"35-2300-006": {"air_pressure_reading_error": 7}, "35-2300-007": {}},
    }
    assert data.system_series("errors") == {"35-2300-007": {d1: 15}, "35-2300-006": {d2: 7}}
    assert data.system_day_states("errors", "35-2300-007", d1) == {"planner_error": 10, "already_stopped_error": 5}
    assert data.system_day_states("errors", "35-2300-007", d2) == {}
    assert data.system_series("stops") == {}


def test_merge_takes_new_days_and_replaces_overlaps():
    d1, d2, d3 = date(2026, 9, 3), date(2026, 9, 4), date(2026, 9, 5)
    data = ErrorsStopsData(days=[d2, d3])
    data.errors = {d2: {"35-2300-007": {"planner_error": 1}}, d3: {"35-2300-007": {"planner_error": 2}}}
    older = ErrorsStopsData(days=[d1, d2])
    older.errors = {d1: {"35-2300-006": {"error": 4}}, d2: {"35-2300-007": {"planner_error": 9}}}
    older.stops = {d1: {"35-2300-006": {"caution": 1}}}
    data.merge(older)
    assert data.days == [d1, d2, d3]
    assert data.errors[d2] == {"35-2300-007": {"planner_error": 9}}
    assert data.errors[d1] == {"35-2300-006": {"error": 4}}
    assert data.stops == {d1: {"35-2300-006": {"caution": 1}}}


def _utc_day(ts):
    return ts.astimezone(timezone.utc).date()


def _t(seconds: float, day: int = 22) -> datetime:
    return datetime(2026, 8, day, 5, 56, 38, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_cluster_folds_a_cascade_into_one_event_named_by_its_first_state():
    # PikPak 010, 22 Aug: node error, package error, generic package_error within 200 ms.
    docs = [
        (_t(0.000), "35-2300-010", "controller_node_shift_crate_piston_error"),
        (_t(0.048), "35-2300-010", "crate_change_package_error"),
        (_t(0.173), "35-2300-010", "package_error"),
    ]
    stops, errors = cluster_events(docs, timedelta(seconds=2), _utc_day)
    assert stops == {}
    assert errors == {date(2026, 8, 22): {"35-2300-010": {"controller_node_shift_crate_piston_error": 1}}}


def test_cluster_window_runs_from_the_first_document_not_the_last():
    docs = [(_t(0), "r", "planner_error"), (_t(1.5), "r", "planner_error"), (_t(3.0), "r", "planner_error"), (_t(5.1), "r", "planner_error")]
    _stops, errors = cluster_events(docs, timedelta(seconds=2), _utc_day)
    # 0 and 1.5 s are one event; 3.0 s is more than 2 s after 0 so starts another; 5.1 s a third.
    assert errors[date(2026, 8, 22)]["r"] == {"planner_error": 3}


def test_cluster_keeps_pairs_more_than_two_seconds_apart_separate():
    docs = []
    for k in range(4):
        docs.append((_t(k * 3.0), "r", "already_stopped_error"))
        docs.append((_t(k * 3.0 + 0.001), "r", "planner_error"))
    _stops, errors = cluster_events(docs, timedelta(seconds=2), _utc_day)
    assert errors[date(2026, 8, 22)]["r"] == {"already_stopped_error": 4}


def test_cluster_separates_systems_stops_and_errors_and_ignores_other_states():
    docs = [
        (_t(0.0), "a", "hardware_emergency_stop"),
        (_t(0.1), "a", "already_stopped_error"),      # error at the same moment as the stop: both count
        (_t(0.2), "a", "operator_stop_from_e_stop"),  # a second stop within the window: folded
        (_t(0.3), "b", "already_stopped_error"),      # another system: its own event
        (_t(0.4), "a", "pnp_executing"),              # neither a stop nor an error
    ]
    stops, errors = cluster_events(docs, timedelta(seconds=2), _utc_day)
    assert stops == {date(2026, 8, 22): {"a": {"hardware_emergency_stop": 1}}}
    assert errors == {date(2026, 8, 22): {"a": {"already_stopped_error": 1}, "b": {"already_stopped_error": 1}}}


def test_cluster_uses_the_given_local_day():
    docs = [(datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc), "r", "planner_error")]
    plus_one = timezone(timedelta(hours=1))
    _stops, errors = cluster_events(docs, timedelta(seconds=2), lambda ts: ts.astimezone(plus_one).date())
    assert list(errors) == [date(2026, 8, 23)]
