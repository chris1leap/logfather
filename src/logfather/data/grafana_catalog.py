"""The ? on the Data window's Grafana tile (Chris, 2026-09-07): what
Prometheus stores for the robots, one row per metric, with a plain meaning,
which generation reports it and how many series it holds today. Click a
metric for its series over the last day."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from logfather.core.grafana import Series
from logfather.data import grafana_client
from logfather.data.settings_store import Settings

ProgressFn = Callable[[str], None]

# Metric families by name prefix: (label, what that node measures).
FAMILIES: dict[str, tuple[str, str]] = {
    "actuators": ("Actuators", "The act_controller node: per-motor temperatures, currents, faults and whether each motor is operational."),
    "sensors": ("Sensors", "The analog-input node: computer, RCU, GPU and brake-resistor temperatures, air pressure."),
    "planner": ("Planner", "The motion planner: cycle and compute times, service calls, halts and time-budget misses."),
    "targeting": ("Targeting", "The vision pipeline on Argus 2: products seen, picked, rejected, retried, trays packed."),
    "crate_change": ("Crate change", "The conveyor and crate-change mechanism: roller velocities, piston and eject timings, distance sensors."),
    "infeed": ("Infeed", "The infeed conveyor: gate sensor, manual mode, target and quantised velocity, piston duration."),
    "diagnostics": ("Diagnostics", "Computer health: CCU and RCU CPU load and memory."),
    "behaviour": ("Behaviour", "Cross-cutting counters: CAN bus errors and frames, RCU/CCU clock offset, UI page loads and service calls."),
    "argus": ("Argus", "The Argus agent itself: log queue size and state updates."),
    "GRAFANA_ALERTS": ("Alerts", "Grafana's own alert-state series."),
}

# Hand-written meanings for the metrics people ask about; the rest get a
# reading of their name.
KNOWN: dict[str, str] = {
    "actuators_motor_temperature": "Temperature of each motor (label motor_id), °C.",
    "actuators_motor_current": "Current drawn by each motor, A; negative when braking.",
    "actuators_motor_max_current": "The peak current seen by each motor since the last reset, A.",
    "actuators_motor_halting_errors": "Count of halting errors raised by each motor.",
    "actuators_motor_operational": "1 when the motor is enabled and healthy, 0 otherwise.",
    "actuators_service_received": "Count of service calls the actuator controller has received.",
    "sensors_cpu_temperature": "Main computer CPU temperature, °C.",
    "sensors_gpu_temperature": "Main computer GPU temperature, °C.",
    "sensors_rcu_temperature": "Robot control unit temperature, °C.",
    "sensors_brake_resistor_temperature": "Brake resistor temperature, °C; rises with hard decelerations.",
    "sensors_air_pressure": "Supply air pressure at the system.",
    "planner_full_cycle_time": "Time for one full pick-and-place cycle, s.",
    "planner_planner_compute_time": "Time the planner spent computing the last plan, s.",
    "planner_actuators_call_time": "Time the actuator call took, s.",
    "planner_actuators_moving": "1 while the actuators are moving.",
    "planner_actuators_stopped": "1 while the actuators are stopped.",
    "planner_system_halted": "1 while the system is halted.",
    "planner_time_budget_unachievable": "Count of plans that could not fit the time budget.",
    "planner_service_called": "Count of planner service calls made.",
    "planner_service_received": "Count of planner service calls received.",
    "targeting_products_picked_and_placed": "Products successfully picked and placed (counter).",
    "targeting_products_picked_per_min": "Pick rate, products per minute.",
    "targeting_products_rejected": "Products rejected by the targeting checks (counter).",
    "targeting_products_failed_latch": "Products the gripper failed to latch (counter).",
    "targeting_products_not_attempted": "Products seen but not attempted (counter).",
    "targeting_trays_packed": "Trays completed (counter).",
    "targeting_pick_targets_seen": "Pick targets detected by the camera (counter).",
    "targeting_pick_pipeline_error_retry_count": "Retries after a pick-pipeline error (counter).",
    "diagnostics_ccu_cpu_average": "CCU average CPU load, %.",
    "diagnostics_ccu_cpu_max": "CCU peak CPU load, %.",
    "diagnostics_ccu_memory": "CCU memory in use.",
    "diagnostics_rcu_cpu_average": "RCU average CPU load, %.",
    "diagnostics_rcu_cpu_max": "RCU peak CPU load, %.",
    "diagnostics_rcu_memory": "RCU memory in use.",
    "behaviour_canbus_errors": "CAN bus errors seen (counter).",
    "behaviour_canbus_errors_near_power_event": "CAN bus errors within a short window of a power event (counter).",
    "behaviour_canbus_frames_seen": "CAN bus frames seen (counter).",
    "behaviour_RCU_CCU_time_offset": "Clock offset between the RCU and the CCU, s.",
    "argus_logs_queue_size": "Log lines waiting to be shipped to Elastic.",
    "argus_state_updated": "State updates published by the Argus agent (counter).",
    "crate_change_eject_crate_duration": "Time taken to eject a crate, s.",
    "infeed_gate_sensor": "Infeed gate sensor state.",
    "infeed_target_velocity": "Infeed conveyor target velocity.",
}
SUFFIXES = {"_avg": "per-scrape average of", "_max": "per-scrape maximum of", "_min": "per-scrape minimum of", "_single_core": "single-core figure of", "_average_max": "peak of the average"}


@dataclass
class MetricInfo:
    name: str
    family: str
    meaning: str
    argus1_series: int = 0
    argus2_series: int = 0


@dataclass
class GrafanaCatalog:
    metrics: list[MetricInfo] = field(default_factory=list)
    days: int = 1


@dataclass
class MetricSeriesSummary:
    labels: dict
    samples: int
    minimum: float
    average: float
    maximum: float
    last: float


@dataclass
class MetricDetail:
    name: str
    meaning: str
    hours: int
    rows: list[MetricSeriesSummary] = field(default_factory=list)


def family_of(name: str) -> tuple[str, str]:
    for prefix, (label, text) in FAMILIES.items():
        if name == prefix or name.startswith(prefix + "_"):
            return label, text
    return "Other", ""


def describe(name: str) -> str:
    if name in KNOWN:
        return KNOWN[name]
    for suffix, phrase in SUFFIXES.items():
        base = name[: -len(suffix)] if name.endswith(suffix) else None
        if base and base in KNOWN:
            return f"The {phrase} {KNOWN[base][0].lower() + KNOWN[base][1:]}"
    family, _ = family_of(name)
    words = name.split("_", 1)[1] if "_" in name else name
    return f"{family}: {words.replace('_', ' ')} (no description written yet; the name is the best guide)."


def merge_counts(argus1: list[Series], argus2: list[Series]) -> list[MetricInfo]:
    counts: dict[str, MetricInfo] = {}

    def add(series_list: list[Series], attr: str) -> None:
        for s in series_list:
            name = s.labels.get("__name__") or ""
            if not name:
                continue
            vals = [v for v in s.values if v is not None]
            if not vals:
                continue
            info = counts.setdefault(name, MetricInfo(name, family_of(name)[0], describe(name)))
            setattr(info, attr, getattr(info, attr) + int(vals[-1]))

    add(argus1, "argus1_series")
    add(argus2, "argus2_series")
    order = list(FAMILIES.values())
    rank = {label: i for i, (label, _t) in enumerate(order)}
    return sorted(counts.values(), key=lambda m: (rank.get(m.family, len(rank)), m.name))


def summarise(series_list: list[Series]) -> list[MetricSeriesSummary]:
    rows = []
    for s in series_list:
        vals = [v for v in s.values if v is not None]
        if not vals:
            continue
        labels = {k: v for k, v in s.labels.items() if k not in ("__name__", "instance", "job")}
        rows.append(MetricSeriesSummary(labels, len(vals), min(vals), sum(vals) / len(vals), max(vals), vals[-1]))
    rows.sort(key=lambda r: (r.labels.get("leap_robot_id") or r.labels.get("system_id") or "", r.labels.get("motor_id") or ""))
    return rows


def fetch_grafana_catalog(settings: Settings, progress: Optional[ProgressFn] = None) -> GrafanaCatalog:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = []
    for label in ("leap_robot_id", "system_id"):
        if progress:
            progress(f"Grafana: metrics carrying {label}...")
        out.append(grafana_client.query_series(
            settings, grafana_client.TELEMETRY_DATASOURCE,
            f'count by(__name__) (last_over_time({{{label}!=""}}[1d]))',
            now_ms - 60_000, now_ms, interval_ms=60_000, max_points=2,
        ))
    return GrafanaCatalog(metrics=merge_counts(out[0], out[1]), days=1)


def fetch_metric_detail(settings: Settings, name: str, hours: int = 24) -> MetricDetail:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    series = grafana_client.query_series(
        settings, grafana_client.TELEMETRY_DATASOURCE, f'{{__name__="{name}"}}',
        now_ms - hours * 3_600_000, now_ms, interval_ms=60_000, max_points=hours * 60 + 5, timeout=120,
    )
    return MetricDetail(name=name, meaning=describe(name), hours=hours, rows=summarise(series))
