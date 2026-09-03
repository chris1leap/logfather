"""
Conveyor target tracking calibration.

The live method is the two-click tracking line: click the same belt landmark
on two frames a few seconds apart and the dialog derives the on-screen belt
velocity (tracking_line_* fields). Save / load as JSON, keyed by system_id.

Historical homography fields (points / cam_axis_* / homography) are kept as
raw pass-through data so existing calibration files round-trip unchanged;
the projection logic itself was removed as dead code (see
docs/CODE_REVIEW_2026-09.md).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConveyorCalibration:
    """
    Full calibration for one camera / robot system.

    belt_pixels_per_sec: normalised x-units per second (fraction of frame width).
    Positive = items move left→right in the frame.
    """
    system_id: str
    points: list[dict] = field(default_factory=list)  # legacy data, pass-through
    belt_pixels_per_sec: float = 0.0
    tracking_line_start_norm: list[float] | None = None
    tracking_line_end_norm: list[float] | None = None
    tracking_line_duration_sec: float = 0.0
    # Where the line's two points were captured: the clip (normalized stem)
    # and the positions as fractions of that clip's frame range. Lets the
    # dialog restore its scrub markers / go-to buttons when reopened on the
    # same clip; meaningless on any other clip.
    capture_clip_key: str | None = None
    capture_start_fraction: float | None = None
    capture_end_fraction: float | None = None
    cam_axis_0: int = 0     # legacy, pass-through
    cam_axis_1: int = 2     # legacy, pass-through
    homography: list[list[float]] | None = None  # legacy, pass-through

    # ------------------------------------------------------------------
    # Line-based manual tracking
    # ------------------------------------------------------------------
    def has_tracking_line(self) -> bool:
        return (
            isinstance(self.tracking_line_start_norm, list)
            and len(self.tracking_line_start_norm) >= 2
            and isinstance(self.tracking_line_end_norm, list)
            and len(self.tracking_line_end_norm) >= 2
            and float(self.tracking_line_duration_sec or 0.0) > 0.0
        )

    def tracking_velocity_norm_per_sec(self) -> tuple[float, float] | None:
        if not self.has_tracking_line():
            return None
        dt = float(self.tracking_line_duration_sec)
        start = self.tracking_line_start_norm or [0.0, 0.0]
        end = self.tracking_line_end_norm or [0.0, 0.0]
        return ((float(end[0]) - float(start[0])) / dt, (float(end[1]) - float(start[1])) / dt)

    def tracking_position_for_age(self, age_secs: float) -> tuple[float, float] | None:
        if not self.has_tracking_line():
            return None
        if age_secs < 0.0 or age_secs > float(self.tracking_line_duration_sec):
            return None
        start = self.tracking_line_start_norm or [0.0, 0.0]
        vel = self.tracking_velocity_norm_per_sec()
        if vel is None:
            return None
        vx, vy = vel
        return (float(start[0]) + vx * age_secs, float(start[1]) + vy * age_secs)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "system_id": self.system_id,
            "belt_pixels_per_sec": self.belt_pixels_per_sec,
            "tracking_line_start_norm": self.tracking_line_start_norm,
            "tracking_line_end_norm": self.tracking_line_end_norm,
            "tracking_line_duration_sec": self.tracking_line_duration_sec,
            "capture_clip_key": self.capture_clip_key,
            "capture_start_fraction": self.capture_start_fraction,
            "capture_end_fraction": self.capture_end_fraction,
            "cam_axis_0": self.cam_axis_0,
            "cam_axis_1": self.cam_axis_1,
            "homography": self.homography,
            "points": list(self.points or []),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConveyorCalibration":
        return cls(
            system_id=d.get("system_id", ""),
            points=[p for p in d.get("points", []) if isinstance(p, dict)],
            belt_pixels_per_sec=d.get("belt_pixels_per_sec", 0.0),
            tracking_line_start_norm=d.get("tracking_line_start_norm"),
            tracking_line_end_norm=d.get("tracking_line_end_norm"),
            tracking_line_duration_sec=d.get("tracking_line_duration_sec", 0.0),
            capture_clip_key=d.get("capture_clip_key"),
            capture_start_fraction=d.get("capture_start_fraction"),
            capture_end_fraction=d.get("capture_end_fraction"),
            cam_axis_0=d.get("cam_axis_0", 0),
            cam_axis_1=d.get("cam_axis_1", 2),
            homography=d.get("homography"),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _cal_dir() -> Path:
    p = Path.home() / ".logfather" / "calibrations"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cal_path_for(system_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in system_id)
    return _cal_dir() / f"{safe}.json"


def load_calibration(system_id: str) -> ConveyorCalibration:
    path = cal_path_for(system_id)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ConveyorCalibration.from_dict(data)
        except Exception:
            pass
    return ConveyorCalibration(system_id=system_id)


def save_calibration(cal: ConveyorCalibration) -> None:
    path = cal_path_for(cal.system_id)
    path.write_text(json.dumps(cal.to_dict(), indent=2), encoding="utf-8")
