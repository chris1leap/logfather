"""
Conveyor target tracking calibration.

Workflow:
  1. Pick ≥4 calibration points: click a pixel in the CCTV frame, then
     associate it with a target whose camera_space_position [x, y, z] is known.
  2. compute_homography() fits a 3×3 homography from two chosen camera axes
     (default axis_0=0 [X], axis_1=2 [Z]) → (norm_x, norm_y).
  3. project(cam_pos, age_secs) dead-reckons belt travel and returns
     (norm_x, norm_y) or None if outside [0..1].

Save / load as JSON, keyed by system_id.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

_AXIS_NAMES = ["X", "Y", "Z"]


@dataclass
class CalPoint:
    """One calibration correspondence: 3-D camera-space coords ↔ normalised pixel."""
    cam_pos: list[float]   # full [x, y, z] from camera_space_position
    norm_x: float          # pixel_x / frame_width  (0..1)
    norm_y: float          # pixel_y / frame_height (0..1)
    label: str = ""

    # Convenience shorthands
    @property
    def cam_x(self) -> float: return self.cam_pos[0]
    @property
    def cam_y(self) -> float: return self.cam_pos[1]
    @property
    def cam_z(self) -> float: return self.cam_pos[2] if len(self.cam_pos) > 2 else 0.0


@dataclass
class ConveyorCalibration:
    """
    Full calibration for one camera / robot system.

    cam_axis_0, cam_axis_1: which indices of camera_space_position [x,y,z] to use
    as the 2-D source for the homography.  Default (0, 2) = X and Z, which is
    typically correct when the belt runs roughly along Z and items spread in X.

    belt_pixels_per_sec: normalised x-units per second (fraction of frame width).
    Positive = items move left→right in the frame.
    """
    system_id: str
    points: list[CalPoint] = field(default_factory=list)
    belt_pixels_per_sec: float = 0.0
    tracking_line_start_norm: list[float] | None = None
    tracking_line_end_norm: list[float] | None = None
    tracking_line_duration_sec: float = 0.0
    cam_axis_0: int = 0     # which camera coord maps to homography "u"
    cam_axis_1: int = 2     # which camera coord maps to homography "v"
    homography: list[list[float]] | None = None   # 3×3, row-major

    # ------------------------------------------------------------------
    # Homography
    # ------------------------------------------------------------------

    def _src_pair(self, p: CalPoint) -> tuple[float, float]:
        """Extract the two chosen camera axes from a CalPoint."""
        a = p.cam_pos[self.cam_axis_0] if self.cam_axis_0 < len(p.cam_pos) else 0.0
        b = p.cam_pos[self.cam_axis_1] if self.cam_axis_1 < len(p.cam_pos) else 0.0
        return a, b

    def compute_homography(self) -> tuple[bool, str]:
        """
        Fit a mapping from (cam[axis_0], cam[axis_1]) → (norm_x, norm_y).

        Returns (success, message).
        Strategy:
          1. RANSAC homography  — best, needs non-collinear spread in both axes
          2. DLT (exact/LS)     — still needs non-collinear, no outlier rejection
          3. Affine via lstsq   — works with ≥3 points, even if collinear; stores as
                                  a 3×3 homography with bottom row [0,0,1]
        """
        if len(self.points) < 3:
            return False, f"Need ≥3 points, have {len(self.points)}."

        try:
            import cv2
        except ImportError:
            return False, "OpenCV (cv2) not available."

        src = np.array([self._src_pair(p) for p in self.points], dtype=np.float64)
        dst = np.array([[p.norm_x, p.norm_y] for p in self.points], dtype=np.float64)

        ax0_name = _AXIS_NAMES[self.cam_axis_0] if self.cam_axis_0 < 3 else str(self.cam_axis_0)
        ax1_name = _AXIS_NAMES[self.cam_axis_1] if self.cam_axis_1 < 3 else str(self.cam_axis_1)

        spread0 = float(np.ptp(src[:, 0]))
        spread1 = float(np.ptp(src[:, 1]))

        rank = int(np.linalg.matrix_rank(np.column_stack([src, np.ones(len(src))])))
        collinear = rank < 3

        if len(self.points) >= 4 and not collinear:
            # Scale-aware RANSAC threshold: 1 % of the source point spread
            spread = max(spread0, spread1, 1e-6)
            ransac_thresh = spread * 0.01
            H, mask = cv2.findHomography(src, dst, cv2.RANSAC, ransac_thresh)
            if H is not None:
                n_inliers = int(mask.sum()) if mask is not None else len(self.points)
                self.homography = H.tolist()
                return True, (
                    f"Homography (RANSAC) — {n_inliers}/{len(self.points)} inliers, "
                    f"axes {ax0_name}+{ax1_name}"
                )

            H, _ = cv2.findHomography(src, dst, 0)
            if H is not None:
                self.homography = H.tolist()
                return True, (
                    f"Homography (DLT) — {len(self.points)} points, "
                    f"axes {ax0_name}+{ax1_name}"
                )

        # Affine via least-squares — always produces a result.
        # Encodes as homography with bottom row [0, 0, 1].
        # Valid as long as src has spread in ≥1 axis; both axes needed for
        # full 2-D accuracy. Warns user if one axis has near-zero spread.
        A = np.column_stack([src, np.ones(len(src))])   # N×3
        coeff, _, _, _ = np.linalg.lstsq(A, dst, rcond=None)  # 3×2
        H = np.array([
            [coeff[0, 0], coeff[1, 0], coeff[2, 0]],
            [coeff[0, 1], coeff[1, 1], coeff[2, 1]],
            [0.0,         0.0,         1.0         ],
        ])
        self.homography = H.tolist()

        warnings = []
        if collinear:
            warnings.append(
                f"Points are collinear in {ax0_name}+{ax1_name} — "
                f"Y-axis mapping may be inaccurate. "
                f"Collect targets from different lateral positions."
            )
        if spread0 < 1e-4:
            warnings.append(f"cam-{ax0_name} has zero spread ({spread0:.4f}) — X mapping unconstrained.")
        if spread1 < 1e-4:
            warnings.append(f"cam-{ax1_name} has zero spread ({spread1:.4f}) — Y mapping unconstrained.")

        method = "Affine (lstsq)"
        if warnings:
            return True, f"{method} — WARNING: " + " ".join(warnings)
        return True, f"{method} — {len(self.points)} points, axes {ax0_name}+{ax1_name}"

    def project(self, cam_pos: list[float], age_secs: float) -> Optional[tuple[float, float]]:
        """
        Project a 3-D camera-space position to normalised frame coords.

        cam_pos:  [x, y, z] list
        age_secs: seconds since detection — used to dead-reckon belt travel.
        Returns (norm_x, norm_y) or None if outside [0..1] or homography absent.
        """
        if self.homography is None:
            return None

        a = cam_pos[self.cam_axis_0] if self.cam_axis_0 < len(cam_pos) else 0.0
        b = cam_pos[self.cam_axis_1] if self.cam_axis_1 < len(cam_pos) else 0.0

        H = np.array(self.homography, dtype=np.float64)
        pt = np.array([a, b, 1.0], dtype=np.float64)
        projected = H @ pt
        if abs(projected[2]) < 1e-9:
            return None
        nx = projected[0] / projected[2] + self.belt_pixels_per_sec * age_secs
        ny = projected[1] / projected[2]

        if not (0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0):
            return None
        return (nx, ny)

    def project_rect(
        self,
        front: list[float],
        back: list[float],
        width: float,
        age_secs: float = 0.0,
    ) -> list[tuple[float, float]] | None:
        """
        Project a rectangle defined by two 3-D centerline endpoints and a physical width.

        front, back: [x, y, z] camera-space points (e.g. front_corner_point /
                     back_corner_point from the metrics dict).
        width:       physical width of the rectangle in the same units as the
                     camera-space coordinates (metres).
        age_secs:    belt dead-reckoning offset (0 = at detection position).

        Returns 4 normalised (nx, ny) pixel corners in order
        [front-left, front-right, back-right, back-left], or None on failure.
        Corner values are NOT clamped to [0..1] so callers can draw partially
        off-screen rectangles correctly.
        """
        if self.homography is None:
            return None

        ax0, ax1 = self.cam_axis_0, self.cam_axis_1

        def _pick(pt: list[float]) -> np.ndarray:
            a = pt[ax0] if ax0 < len(pt) else 0.0
            b = pt[ax1] if ax1 < len(pt) else 0.0
            return np.array([a, b], dtype=np.float64)

        fa = _pick(front)
        ba = _pick(back)
        long_vec = ba - fa
        length = float(np.linalg.norm(long_vec))
        if length < 1e-6:
            return None
        long_dir = long_vec / length
        perp = np.array([-long_dir[1], long_dir[0]])
        hw = width / 2.0

        cam_corners = [
            fa + perp * hw,   # front-left
            fa - perp * hw,   # front-right
            ba - perp * hw,   # back-right
            ba + perp * hw,   # back-left
        ]

        H = np.array(self.homography, dtype=np.float64)
        result: list[tuple[float, float]] = []
        for c in cam_corners:
            pt = np.array([c[0], c[1], 1.0], dtype=np.float64)
            proj = H @ pt
            if abs(proj[2]) < 1e-9:
                return None
            nx = proj[0] / proj[2] + self.belt_pixels_per_sec * age_secs
            ny = proj[1] / proj[2]
            result.append((float(nx), float(ny)))
        return result

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
            "cam_axis_0": self.cam_axis_0,
            "cam_axis_1": self.cam_axis_1,
            "homography": self.homography,
            "points": [
                {"cam_pos": p.cam_pos, "norm_x": p.norm_x, "norm_y": p.norm_y, "label": p.label}
                for p in self.points
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ConveyorCalibration":
        raw_pts = d.get("points", [])
        points = []
        for p in raw_pts:
            # Backwards compat: old format stored cam_x/cam_y as floats
            if "cam_pos" in p:
                points.append(CalPoint(**p))
            elif "cam_x" in p:
                points.append(CalPoint(
                    cam_pos=[p["cam_x"], p.get("cam_y", 0.0), 0.0],
                    norm_x=p["norm_x"], norm_y=p["norm_y"], label=p.get("label", ""),
                ))
        return cls(
            system_id=d.get("system_id", ""),
            points=points,
            belt_pixels_per_sec=d.get("belt_pixels_per_sec", 0.0),
            tracking_line_start_norm=d.get("tracking_line_start_norm"),
            tracking_line_end_norm=d.get("tracking_line_end_norm"),
            tracking_line_duration_sec=d.get("tracking_line_duration_sec", 0.0),
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
