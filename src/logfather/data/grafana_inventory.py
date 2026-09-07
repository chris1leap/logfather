"""How much telemetry Grafana holds, per system per day, for the Data window
(Chris, 2026-09-07). Prometheus counts series and samples, not bytes, so
the size is an estimate: samples times a typical compressed sample.

Sources, all through Grafana's query API:
  grafanacloud-prom      the robot metrics (per-system sample counts via a
                         30 s subquery, series counts via last_over_time)
  grafanacloud-usage     the stack's own meters: active series, samples per
                         second now and per day back through the retention
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from logfather.core.grafana import DatasourceRef, Series
from logfather.data import grafana_client
from logfather.data.data_inventory import inventory_days
from logfather.data.elastic_schema import ROBOT_ID_PREFIX
from logfather.data.settings_store import Settings

# Mimir (Grafana Cloud Metrics) stores a sample in roughly 1-2 bytes once
# compressed, plus index; 1.5 is the middle of that. Shown as "estimated".
BYTES_PER_SAMPLE = 1.5
# Grafana Cloud Metrics keeps 13 months by default.
RETENTION_DAYS = 395
USAGE_DATASOURCE = DatasourceRef(uid="grafanacloud-usage", type="prometheus")
DAY_MS = 86_400_000
ProgressFn = Callable[[str], None]


@dataclass
class GrafanaInventory:
    days: list[date]
    samples: dict[str, dict[date, int]] = field(default_factory=dict)   # robot -> day -> samples
    series_now: dict[str, int] = field(default_factory=dict)            # robot -> series in the last day
    generation: dict[str, str] = field(default_factory=dict)            # robot -> "Argus 1" / "Argus 2"
    active_series: Optional[int] = None
    samples_per_second: Optional[float] = None
    retained_samples: Optional[float] = None
    retained_since: Optional[date] = None
    metric_count: Optional[int] = None

    def bytes_for(self, samples: float) -> float:
        return samples * BYTES_PER_SAMPLE

    @property
    def retained_bytes(self) -> Optional[float]:
        return None if self.retained_samples is None else self.retained_samples * BYTES_PER_SAMPLE


def normalise_robot_id(label: str) -> str:
    """Argus 2 systems sometimes report just the three digits ("018")."""
    text = (label or "").strip()
    if text.isdigit() and len(text) == 3:
        return f"{ROBOT_ID_PREFIX}{text}"
    return text


def utc_midnight_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


def parse_daily(series_list: list[Series], label: str, days: list[date]) -> dict[str, dict[date, int]]:
    """A daily-step range query evaluated at UTC midnights: the point at T
    counts the day that ends at T."""
    wanted = set(days)
    out: dict[str, dict[date, int]] = {}
    for s in series_list:
        robot = normalise_robot_id(s.labels.get(label, ""))
        if not robot:
            continue
        per_day = out.setdefault(robot, {})
        for t_ms, v in zip(s.times_ms, s.values):
            if v is None:
                continue
            day = datetime.fromtimestamp((t_ms - 1) / 1000, tz=timezone.utc).date()
            if day in wanted:
                per_day[day] = per_day.get(day, 0) + int(round(v))
    return out


def retained_from_usage(series_list: list[Series]) -> tuple[Optional[float], Optional[date]]:
    """Total samples across the retention from the per-day samples/second
    history, and the first day the stack has figures for."""
    total = 0.0
    first: Optional[date] = None
    for s in series_list:
        for t_ms, v in zip(s.times_ms, s.values):
            if v is None:
                continue
            total += float(v) * 86_400
            day = datetime.fromtimestamp((t_ms - 1) / 1000, tz=timezone.utc).date()
            if first is None or day < first:
                first = day
    if first is None:
        return None, None
    return total, first


def fetch_grafana_inventory(settings: Settings, days_span: int, progress: Optional[ProgressFn] = None) -> GrafanaInventory:
    days = inventory_days(datetime.now().date(), days_span)
    inv = GrafanaInventory(days=days)
    from_ms = utc_midnight_ms(days[0])
    to_ms = utc_midnight_ms(days[-1] + timedelta(days=1))
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def say(text: str) -> None:
        if progress:
            progress(text)

    for label, gen in (("leap_robot_id", "Argus 1"), ("system_id", "Argus 2")):
        say(f"Grafana: samples per day ({gen})...")
        daily = grafana_client.query_series(
            settings, grafana_client.TELEMETRY_DATASOURCE,
            f'sum by({label}) (count_over_time({{{label}!=""}}[1d:30s]))',
            from_ms, to_ms, interval_ms=DAY_MS, max_points=len(days) + 2, timeout=120,
        )
        for robot, per_day in parse_daily(daily, label, days).items():
            target = inv.samples.setdefault(robot, {})
            for day, n in per_day.items():
                target[day] = target.get(day, 0) + n
            inv.generation.setdefault(robot, gen)
        series = grafana_client.query_series(
            settings, grafana_client.TELEMETRY_DATASOURCE,
            f'count by({label}) (last_over_time({{{label}!=""}}[1d]))',
            now_ms - 60_000, now_ms, interval_ms=60_000, max_points=2,
        )
        for s in series:
            robot = normalise_robot_id(s.labels.get(label, ""))
            vals = [v for v in s.values if v is not None]
            if robot and vals:
                inv.series_now[robot] = inv.series_now.get(robot, 0) + int(vals[-1])

    say("Grafana: stack usage...")
    try:
        for expr, attr in (("grafanacloud_instance_active_series", "active_series"), ("grafanacloud_instance_samples_per_second", "samples_per_second")):
            series = grafana_client.query_series(settings, USAGE_DATASOURCE, expr, now_ms - 3_600_000, now_ms, max_points=2)
            vals = [v for s in series for v in s.values if v is not None]
            if vals:
                setattr(inv, attr, int(vals[-1]) if attr == "active_series" else float(vals[-1]))
        history = grafana_client.query_series(
            settings, USAGE_DATASOURCE, "avg_over_time(grafanacloud_instance_samples_per_second[1d])",
            utc_midnight_ms(days[-1] - timedelta(days=RETENTION_DAYS)), to_ms, interval_ms=DAY_MS, max_points=RETENTION_DAYS + 5, timeout=120,
        )
        inv.retained_samples, inv.retained_since = retained_from_usage(history)
    except grafana_client.GrafanaError:
        pass  # the usage meters are a bonus; the robot figures stand alone

    say("Grafana: metric names...")
    try:
        names: set[str] = set()
        for label in ("leap_robot_id", "system_id"):
            data = grafana_client._request(
                settings, "GET",
                f"/api/datasources/uid/{grafana_client.TELEMETRY_DATASOURCE.uid}/resources/api/v1/label/__name__/values",
                params={"match[]": f'{{{label}!=""}}'},
            )
            names.update(data.get("data") or [])
        inv.metric_count = len(names)
    except grafana_client.GrafanaError:
        pass
    return inv
