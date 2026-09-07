"""Robot telemetry as the Logfather shows it: which Prometheus metrics, how
a system is selected, and how the series are grouped into tracks. Pure
logic, no Qt, no network.

Facts learned from the Actuators issues dashboard (2026-09-07):
  sensors_cpu_temperature, sensors_rcu_temperature, sensors_gpu_temperature,
  sensors_brake_resistor_temperature: one series per system.
  actuators_motor_temperature, actuators_motor_current: one series per
  motor (motor_id 0..6; a motor that is not fitted reads a flat 0).
  Argus 1 systems carry the id in leap_robot_id, Argus 2 in system_id, and
  Argus 2 series also carry run_id / sku labels, so one motor's day arrives
  as several series that have to be stitched back together.
  Samples every 30 s.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from logfather.core.grafana import Series


@dataclass(frozen=True)
class MetricSpec:
    key: str
    group: str
    label: str          # "{motor_id}" is filled in for per-motor metrics
    metric: str
    unit: str
    per_motor: bool = False


METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("cpu_temp", "Temperatures", "CPU", "sensors_cpu_temperature", "°C"),
    MetricSpec("rcu_temp", "Temperatures", "RCU", "sensors_rcu_temperature", "°C"),
    MetricSpec("gpu_temp", "Temperatures", "GPU", "sensors_gpu_temperature", "°C"),
    MetricSpec("brake_temp", "Temperatures", "Brake resistor", "sensors_brake_resistor_temperature", "°C"),
    MetricSpec("motor_temp", "Motor temperatures", "Motor {motor_id}", "actuators_motor_temperature", "°C", per_motor=True),
    MetricSpec("motor_current", "Motor currents", "Motor {motor_id}", "actuators_motor_current", "A", per_motor=True),
)
GROUP_ORDER = ("Temperatures", "Motor temperatures", "Motor currents")
SAMPLE_INTERVAL_MS = 30_000


@dataclass
class Track:
    name: str
    times_ms: list[int]
    values: list[Optional[float]]
    spec_key: str = ""

    def value_at(self, t_ms: int, tolerance_ms: int = 3 * SAMPLE_INTERVAL_MS) -> Optional[float]:
        return value_at(self.times_ms, self.values, t_ms, tolerance_ms)


@dataclass
class TrackGroup:
    name: str
    unit: str
    tracks: list[Track] = field(default_factory=list)


@dataclass
class TelemetryDay:
    robot_id: str
    day_start_ms: int
    day_end_ms: int
    groups: list[TrackGroup] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(g.tracks for g in self.groups)


def robot_query(spec: MetricSpec, robot_id: str) -> str:
    """Select one system under either label scheme."""
    m = spec.metric
    return f'{m}{{leap_robot_id="{robot_id}"}} or {m}{{system_id="{robot_id}"}}'


def value_at(times_ms: list[int], values: list[Optional[float]], t_ms: int, tolerance_ms: int) -> Optional[float]:
    """The sample nearest t, or None when the nearest is further than the
    tolerance (a gap, the robot off)."""
    if not times_ms:
        return None
    i = bisect_left(times_ms, t_ms)
    best: Optional[int] = None
    for j in (i - 1, i):
        if 0 <= j < len(times_ms) and values[j] is not None:
            if best is None or abs(times_ms[j] - t_ms) < abs(times_ms[best] - t_ms):
                best = j
    if best is None or abs(times_ms[best] - t_ms) > tolerance_ms:
        return None
    return values[best]


def merge_series(parts: Iterable[Series]) -> tuple[list[int], list[Optional[float]]]:
    """Stitch several series of the same signal (Argus 2 splits a day by
    run) into one, sorted by time, later duplicates winning."""
    points: dict[int, Optional[float]] = {}
    for s in parts:
        for t, v in zip(s.times_ms, s.values):
            if v is not None or t not in points:
                points[t] = v
    times = sorted(points)
    return times, [points[t] for t in times]


def is_absent(values: Iterable[Optional[float]]) -> bool:
    """A motor slot with nothing fitted reads a flat zero all day."""
    seen = False
    for v in values:
        if v is None:
            continue
        seen = True
        if abs(v) > 1e-9:
            return False
    return seen or True


def series_key(spec: MetricSpec, s: Series) -> str:
    if spec.per_motor:
        return s.labels.get("motor_id", "?")
    return ""


def build_groups(results: dict[str, list[Series]], specs: Iterable[MetricSpec] = METRICS) -> list[TrackGroup]:
    """results: spec.key -> the series Grafana returned for that metric."""
    groups: dict[str, TrackGroup] = {}
    for spec in specs:
        parts_by_key: dict[str, list[Series]] = {}
        for s in results.get(spec.key, []):
            parts_by_key.setdefault(series_key(spec, s), []).append(s)
        for key in sorted(parts_by_key, key=_motor_sort):
            times, values = merge_series(parts_by_key[key])
            if not times or (spec.per_motor and is_absent(values)):
                continue
            name = spec.label.format(motor_id=key) if spec.per_motor else spec.label
            group = groups.setdefault(spec.group, TrackGroup(spec.group, spec.unit))
            group.tracks.append(Track(name, times, values, spec.key))
    return [groups[g] for g in GROUP_ORDER if g in groups] + [g for n, g in groups.items() if n not in GROUP_ORDER]


def _motor_sort(key: str):
    return (0, int(key)) if key.isdigit() else (1, key)


def max_across(tracks: Iterable[Track], name: str, spec_key: str = "") -> Optional[Track]:
    """One track holding, at each sample time, the highest value across the
    given tracks (the hottest motor at that moment)."""
    best: dict[int, float] = {}
    for t in tracks:
        for time_ms, v in zip(t.times_ms, t.values):
            if v is None:
                continue
            if time_ms not in best or v > best[time_ms]:
                best[time_ms] = v
    if not best:
        return None
    times = sorted(best)
    return Track(name, times, [best[t] for t in times], spec_key)


def summary_track(data: Optional[TelemetryDay]) -> Optional[tuple[Track, str]]:
    """The one line worth a timeline row: the hottest motor through the
    day, or the CPU temperature when the system has no motor readings.
    Returns (track, unit)."""
    if data is None:
        return None
    by_name = {g.name: g for g in data.groups}
    motors = by_name.get("Motor temperatures")
    if motors and motors.tracks:
        track = max_across(motors.tracks, "Hottest motor", "motor_temp")
        if track is not None:
            return track, motors.unit
    temps = by_name.get("Temperatures")
    if temps:
        for t in temps.tracks:
            if t.spec_key == "cpu_temp":
                return Track("CPU temperature", t.times_ms, t.values, t.spec_key), temps.unit
    return None


def downsample(times_ms: list[int], values: list[Optional[float]], t0: int, t1: int, columns: int) -> list[tuple[int, float, float]]:
    """Per pixel column: (column, min, max) of the samples that fall in it.
    Painting this instead of ~2,900 points keeps a resize instant."""
    if columns <= 0 or t1 <= t0:
        return []
    span = t1 - t0
    lo: list[Optional[float]] = [None] * columns
    hi: list[Optional[float]] = [None] * columns
    for t, v in zip(times_ms, values):
        if v is None or t < t0 or t > t1:
            continue
        c = min(columns - 1, int((t - t0) * columns / span))
        if lo[c] is None or v < lo[c]:
            lo[c] = v
        if hi[c] is None or v > hi[c]:
            hi[c] = v
    return [(c, lo[c], hi[c]) for c in range(columns) if lo[c] is not None]


def value_range(tracks: Iterable[Track]) -> tuple[float, float]:
    lo, hi = None, None
    for t in tracks:
        for v in t.values:
            if v is None:
                continue
            lo = v if lo is None else min(lo, v)
            hi = v if hi is None else max(hi, v)
    if lo is None:
        return 0.0, 1.0
    if hi - lo < 1e-9:
        return lo - 1.0, hi + 1.0
    pad = (hi - lo) * 0.08
    return lo - pad, hi + pad


# ---- fleet-wide temperatures for the Overview (Chris, 2026-09-07) --------
# (key, label) in menu order; "motor_temp" is the hottest fitted motor.
TEMPERATURE_CHOICES: tuple[tuple[str, str], ...] = (
    ("cpu_temp", "CPU"),
    ("rcu_temp", "RCU"),
    ("gpu_temp", "GPU"),
    ("brake_temp", "Brake resistor"),
    ("motor_temp", "Hottest motor"),
)
TEMPERATURE_COLOURS = {"cpu_temp": "#5e9bff", "rcu_temp": "#ff8a65", "gpu_temp": "#2ecc71", "brake_temp": "#f1c40f", "motor_temp": "#d46bff"}
ROBOT_ID_LABELS = ("leap_robot_id", "system_id")
_ROBOT_PREFIX = "35-2300-"


def normalise_robot_id(label: str) -> str:
    """Argus 2 systems sometimes report just the three digits ("018")."""
    text = (label or "").strip()
    if text.isdigit() and len(text) == 3:
        return f"{_ROBOT_PREFIX}{text}"
    return text


def fleet_query(spec: MetricSpec, label: str) -> str:
    """One series per system for a metric, under one label scheme. Motors
    collapse to the hottest fitted one (an unfitted slot reads 0)."""
    if spec.per_motor:
        return f'max by({label}) ({spec.metric}{{{label}!=""}} != 0)'
    return f'max by({label}) ({spec.metric}{{{label}!=""}})'


def tracks_by_robot(series_list: Iterable[Series], label: str, name: str, spec_key: str = "") -> dict[str, Track]:
    """Series keyed by the robot id in `label`, merged per robot."""
    parts: dict[str, list[Series]] = {}
    for s in series_list:
        robot = normalise_robot_id(s.labels.get(label, ""))
        if robot:
            parts.setdefault(robot, []).append(s)
    out: dict[str, Track] = {}
    for robot, group in parts.items():
        times, values = merge_series(group)
        if times:
            out[robot] = Track(name, times, values, spec_key)
    return out


def window_stats(track: Track, t0_ms: int, t1_ms: int) -> Optional[tuple[float, float, float]]:
    """(min, max, latest) of the samples inside [t0, t1], or None."""
    lo = hi = last = None
    for t, v in zip(track.times_ms, track.values):
        if v is None or t < t0_ms or t > t1_ms:
            continue
        lo = v if lo is None else min(lo, v)
        hi = v if hi is None else max(hi, v)
        last = v
    if lo is None:
        return None
    return lo, hi, last
