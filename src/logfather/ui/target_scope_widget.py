"""
Small floating panel that draws recent pick targets in flat camera-space X/Y.

No perspective or homography — just scale to fit, onion-skin opacity.
"""
from __future__ import annotations

import numpy as np

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QPolygonF, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy

from logfather.data.target_buffer_loader import PickTarget, get_cam_pos
from logfather.ui.target_buffer_widget import _display_target_id

_BG = QColor("#0d1117")
_GRID = QColor("#1a2530")
_TARGET_COLOR = QColor("#3498db")
_FADE_STEPS = 8
_PAD = 16          # pixel padding inside the panel
_MIN_RANGE = 0.05  # minimum x or y range in metres (prevents huge zoom on tiny spreads)


def _rect_corners_xy(t: PickTarget) -> list[tuple[float, float]] | None:
    """
    Return 4 (x, y) corners of the target rectangle in camera space.

    front_corner_point and back_corner_point are same-edge corners defining
    one long edge of the rectangle (ignoring Z).  The perpendicular width is
    taken from contour_area / long_edge_distance; if that is implausibly small
    (area units may be pixels, not m²) a fallback of 25 % of the edge length
    is used so the shape is always visible.
    """
    src = t.source_doc
    metrics = src.get("metrics") or {}

    front = metrics.get("front_corner_point")
    back  = metrics.get("back_corner_point")

    if not (front and back and len(front) >= 2 and len(back) >= 2):
        return None

    x1, y1 = float(front[0]), float(front[1])
    x2, y2 = float(back[0]),  float(back[1])

    # Long-axis vector from the two edge points
    ex, ey = x2 - x1, y2 - y1
    edge_len = float(np.hypot(ex, ey))
    if edge_len < 1e-6:
        return None

    long_dir = np.array([ex, ey]) / edge_len
    perp = np.array([-long_dir[1], long_dir[0]])   # 90° CCW → width direction

    # Width from metrics; fall back to 25 % of length if result is too small
    led  = metrics.get("long_edge_distance") or edge_len
    area = metrics.get("contour_area")
    if area and led > 1e-6:
        width = float(area) / float(led)
    else:
        width = edge_len * 0.25

    if width < edge_len * 0.05:          # implausibly thin → use fallback
        width = edge_len * 0.25

    p1 = np.array([x1, y1])
    p2 = np.array([x2, y2])
    return [
        tuple(p1),                        # front corner (given edge)
        tuple(p2),                        # back corner  (given edge)
        tuple(p2 + perp * width),         # back corner  (opposite edge)
        tuple(p1 + perp * width),         # front corner (opposite edge)
    ]


class TargetScopeWidget(QWidget):
    """
    Floating panel showing the last _FADE_STEPS pick targets in 2-D camera
    X/Y space, scaled to fit, with onion-skin opacity.

    Call :meth:`set_targets` whenever the buffer snapshot changes.
    """

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Window | Qt.Tool)
        self.setWindowTitle("Target Scope")
        self.setMinimumSize(200, 200)
        self.resize(280, 280)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._polys: list[tuple[list[tuple[float, float]] | None, str, float]] = []
        # Each entry: (corners_xy | None, label, opacity)

    # ------------------------------------------------------------------

    def set_targets(self, targets: list[PickTarget]) -> None:
        """targets: chronological (oldest first); most recent is last."""
        recent = list(reversed(targets[-_FADE_STEPS:]))   # [0] = most recent
        n = len(recent)
        self._polys = []
        for i, t in enumerate(recent):
            opacity = 0.85 ** i
            label = f"#{_display_target_id(t)}" if i == 0 else ""
            corners = _rect_corners_xy(t)
            if corners is None:
                # Fall back to a centre point rendered as a small square
                cp = get_cam_pos(t.source_doc)
                if cp:
                    r = 0.01
                    corners = [
                        (cp[0] - r, cp[1] - r),
                        (cp[0] + r, cp[1] - r),
                        (cp[0] + r, cp[1] + r),
                        (cp[0] - r, cp[1] + r),
                    ]
            self._polys.append((corners, label, opacity))
        self.update()

    # ------------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), _BG)

        if not self._polys:
            painter.setPen(QPen(QColor("#3d4f5c")))
            painter.setFont(QFont("monospace", 9))
            painter.drawText(self.rect(), Qt.AlignCenter, "No targets")
            return

        # Collect all corners to compute a common bounding box
        all_pts = [
            pt
            for corners, _, _ in self._polys
            if corners
            for pt in corners
        ]
        if not all_pts:
            return

        xs = [p[0] for p in all_pts]
        ys = [p[1] for p in all_pts]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        rx = max((max(xs) - min(xs)) / 2, _MIN_RANGE / 2)
        ry = max((max(ys) - min(ys)) / 2, _MIN_RANGE / 2)
        # Add 30 % margin so the shapes don't touch the edges
        rx *= 1.3
        ry *= 1.3

        draw_w = self.width()  - 2 * _PAD
        draw_h = self.height() - 2 * _PAD
        scale = min(draw_w / (2 * rx), draw_h / (2 * ry))

        def to_w(x: float, y: float) -> QPointF:
            """Camera X/Y → widget coords (Y is flipped so +y is up)."""
            wx = _PAD + draw_w / 2 + (x - cx) * scale
            wy = _PAD + draw_h / 2 - (y - cy) * scale
            return QPointF(wx, wy)

        # Light grid lines at the bounding-box centre
        painter.setPen(QPen(_GRID, 1))
        mid = to_w(cx, cy)
        painter.drawLine(int(mid.x()), _PAD, int(mid.x()), self.height() - _PAD)
        painter.drawLine(_PAD, int(mid.y()), self.width() - _PAD, int(mid.y()))

        # Draw from oldest → newest so newest renders on top
        for corners, label, opacity in reversed(self._polys):
            if corners is None:
                continue
            painter.setOpacity(opacity)
            wpts = [to_w(x, y) for x, y in corners]
            poly = QPolygonF(wpts)

            c = _TARGET_COLOR
            fill_a = max(0, min(255, int(50 * opacity)))
            painter.setPen(QPen(c, 1.5))
            painter.setBrush(QBrush(QColor(c.red(), c.green(), c.blue(), fill_a)))
            painter.drawPolygon(poly)

            # Centre dot
            centre = to_w(
                sum(p[0] for p in corners) / 4,
                sum(p[1] for p in corners) / 4,
            )
            painter.setBrush(QBrush(c))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(centre, 2.5, 2.5)

        painter.setOpacity(1.0)

        # Label for most recent (drawn last so it's on top)
        if self._polys:
            corners, label, _ = self._polys[0]
            if label and corners:
                centre = to_w(
                    sum(p[0] for p in corners) / 4,
                    sum(p[1] for p in corners) / 4,
                )
                f = QFont("monospace", 8)
                f.setBold(True)
                painter.setFont(f)
                metrics = painter.fontMetrics()
                text_x = int(centre.x()) + 8
                text_y = int(centre.y()) - 4
                text_w = metrics.horizontalAdvance(label)
                text_h = metrics.height()
                bg_rect = QRectF(
                    text_x - 4,
                    text_y - metrics.ascent() - 2,
                    text_w + 8,
                    text_h + 4,
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(QColor(0, 0, 0, 220)))
                painter.drawRoundedRect(bg_rect, 4, 4)
                painter.setPen(QPen(Qt.white))
                painter.drawText(
                    text_x,
                    text_y,
                    label,
                )
