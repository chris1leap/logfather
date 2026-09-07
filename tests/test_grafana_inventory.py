from datetime import date, datetime, timezone

from logfather.core.grafana import Series
from logfather.data.grafana_catalog import describe, family_of, merge_counts, summarise
from logfather.data.grafana_inventory import BYTES_PER_SAMPLE, GrafanaInventory, normalise_robot_id, parse_daily, retained_from_usage, utc_midnight_ms


def _ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def test_normalise_robot_id():
    assert normalise_robot_id("018") == "35-2300-018"
    assert normalise_robot_id("35-2300-003") == "35-2300-003"
    assert normalise_robot_id(" 35-2300-011-workshop ") == "35-2300-011-workshop"
    assert normalise_robot_id("") == ""


def test_parse_daily_maps_midnight_points_to_the_day_that_ended():
    d1, d2, d3 = date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)
    # points at the midnight that ENDS each day
    s = Series("x", [_ms(d2), _ms(d3), _ms(date(2026, 9, 8))], [100.0, None, 300.0], {"leap_robot_id": "35-2300-013"})
    out = parse_daily([s], "leap_robot_id", [d1, d2, d3])
    assert out == {"35-2300-013": {d1: 100, d3: 300}}
    # a bare-digit Argus 2 id is normalised and merged
    s2 = Series("x", [_ms(d2)], [5.0], {"system_id": "013"})
    assert parse_daily([s2], "system_id", [d1]) == {"35-2300-013": {d1: 5}}


def test_retained_from_usage_sums_samples_per_second_over_days():
    s = Series("sps", [_ms(date(2026, 9, 6)), _ms(date(2026, 9, 7))], [100.0, 200.0])
    total, first = retained_from_usage([s])
    assert total == (100.0 + 200.0) * 86_400
    assert first == date(2026, 9, 5)
    assert retained_from_usage([]) == (None, None)


def test_inventory_bytes_estimate():
    inv = GrafanaInventory(days=[], retained_samples=1_000_000)
    assert inv.retained_bytes == 1_000_000 * BYTES_PER_SAMPLE
    assert GrafanaInventory(days=[]).retained_bytes is None
    assert utc_midnight_ms(date(1970, 1, 2)) == 86_400_000


def test_catalog_family_and_description():
    assert family_of("actuators_motor_current") == ("Actuators", family_of("actuators_x")[1])
    assert family_of("GRAFANA_ALERTS")[0] == "Alerts"
    assert family_of("mystery_thing") == ("Other", "")
    assert describe("sensors_cpu_temperature").startswith("Main computer CPU temperature")
    assert describe("actuators_motor_current_max").startswith("The per-scrape maximum of current drawn")
    assert "no description written yet" in describe("planner_new_thing")


def test_merge_counts_orders_by_family_and_sums_generations():
    a1 = [Series("n", [1], [6.0], {"__name__": "sensors_cpu_temperature"}), Series("n", [1], [42.0], {"__name__": "actuators_motor_current"})]
    a2 = [Series("n", [1], [3.0], {"__name__": "sensors_cpu_temperature"}), Series("n", [1], [9.0], {"__name__": "targeting_trays_packed"})]
    rows = merge_counts(a1, a2)
    assert [m.name for m in rows] == ["actuators_motor_current", "sensors_cpu_temperature", "targeting_trays_packed"]
    cpu = rows[1]
    assert cpu.argus1_series == 6 and cpu.argus2_series == 3 and cpu.family == "Sensors"


def test_summarise_series_drops_noise_labels_and_sorts():
    s1 = Series("m", [1, 2, 3], [1.0, None, 3.0], {"__name__": "m", "instance": "i", "job": "j", "leap_robot_id": "35-2300-013", "motor_id": "2"})
    s2 = Series("m", [1], [5.0], {"leap_robot_id": "35-2300-008", "motor_id": "1"})
    rows = summarise([s1, s2])
    assert [r.labels for r in rows] == [{"leap_robot_id": "35-2300-008", "motor_id": "1"}, {"leap_robot_id": "35-2300-013", "motor_id": "2"}]
    r = rows[1]
    assert (r.samples, r.minimum, r.average, r.maximum, r.last) == (2, 1.0, 2.0, 3.0, 3.0)
