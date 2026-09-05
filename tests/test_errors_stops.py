"""errors_stops pure logic: classification and the series/summaries."""
from datetime import date

from logfather.data.errors_stops import (
    ErrorsStopsData,
    categorize_error,
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
