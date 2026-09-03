"""
Conveyor calibration dialog.

Line-based workflow only:
  1. Scrub to a frame and capture the start point on the conveyor.
  2. Scrub to a later frame and capture the same reference point again.
  3. Save the derived screen-space motion vector for manual marker tracking.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap, QBrush
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QGroupBox,
    QSizePolicy,
    QSlider,
    QMessageBox,
)

from logfather.data.conveyor_calibration import ConveyorCalibration, save_calibration
from logfather.ui import theme
from logfather.data.target_buffer_loader import PickTarget


def resolve_tracking_line(
    start_dt: datetime,
    start_pos: tuple[float, float],
    end_dt: datetime,
    end_pos: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float], float] | None:
    """Order two captures into a time-forward tracking line.

    Returns (line_start, line_end, duration_seconds), swapping the points
    when the "end" capture was taken at an EARLIER frame — previously that
    silently produced a belt running in reverse. Returns None when the
    captures are too close together in time (< 0.1s) to derive a velocity.
    """
    signed_dt = (end_dt - start_dt).total_seconds()
    if abs(signed_dt) < 0.1:
        return None
    if signed_dt < 0:
        start_pos, end_pos = end_pos, start_pos
        signed_dt = -signed_dt
    return (
        (float(start_pos[0]), float(start_pos[1])),
        (float(end_pos[0]), float(end_pos[1])),
        float(signed_dt),
    )


class _FrameCanvas(QLabel):
    clicked_norm = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self._qimage: QImage | None = None
        self._pending_dot: tuple[float, float] | None = None
        self._motion_line: tuple[tuple[float, float], tuple[float, float]] | None = None
        self._motion_marker: tuple[float, float] | None = None

    def set_frame(self, img: QImage | None) -> None:
        self._qimage = img
        self._refresh()

    def set_pending_dot(self, pos: tuple[float, float] | None) -> None:
        self._pending_dot = pos
        self._refresh()

    def set_motion_line(
        self,
        line: tuple[tuple[float, float], tuple[float, float]] | None,
        marker: tuple[float, float] | None = None,
    ) -> None:
        self._motion_line = line
        self._motion_marker = marker
        self._refresh()

    def _image_rect(self) -> tuple[int, int, int, int]:
        if self._qimage is None or self._qimage.isNull():
            return (0, 0, self.width(), self.height())
        iw, ih = self._qimage.width(), self._qimage.height()
        ww, wh = self.width(), self.height()
        scale = min(ww / iw, wh / ih)
        dw = int(iw * scale)
        dh = int(ih * scale)
        x = (ww - dw) // 2
        y = (wh - dh) // 2
        return x, y, dw, dh

    def _refresh(self) -> None:
        if self._qimage is None or self._qimage.isNull():
            self.setPixmap(QPixmap())
            self.update()
            return

        x, y, dw, dh = self._image_rect()
        canvas = QPixmap(self.width(), self.height())
        canvas.fill(QColor("#0d1117"))

        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        scaled = self._qimage.scaled(dw, dh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        painter.drawImage(x, y, scaled)

        if self._pending_dot is not None:
            nx, ny = self._pending_dot
            px = x + nx * dw
            py = y + ny * dh
            painter.setPen(QPen(QColor("#f1c40f"), 2))
            painter.drawLine(int(px) - 10, int(py), int(px) + 10, int(py))
            painter.drawLine(int(px), int(py) - 10, int(px), int(py) + 10)
            painter.setBrush(QBrush(QColor(241, 196, 15, 120)))
            painter.drawEllipse(QPointF(px, py), 5, 5)

        if self._motion_line is not None:
            (sx, sy), (ex, ey) = self._motion_line
            start_px = QPointF(x + sx * dw, y + sy * dh)
            end_px = QPointF(x + ex * dw, y + ey * dh)
            painter.setPen(QPen(QColor("#f39c12"), 2, Qt.DashLine))
            painter.drawLine(start_px, end_px)
            painter.setBrush(QBrush(QColor("#f39c12")))
            painter.drawEllipse(start_px, 5, 5)
            painter.drawEllipse(end_px, 5, 5)
            painter.setPen(QPen(Qt.white))
            painter.setFont(QFont("monospace", 8))
            painter.drawText(int(start_px.x()) + 8, int(start_px.y()) - 8, "A")
            painter.drawText(int(end_px.x()) + 8, int(end_px.y()) - 8, "B")
            if self._motion_marker is not None:
                mx, my = self._motion_marker
                marker_pt = QPointF(x + mx * dw, y + my * dh)
                painter.setPen(QPen(Qt.white, 2))
                painter.setBrush(QBrush(QColor("#ffffff")))
                painter.drawEllipse(marker_pt, 4, 4)

        painter.end()
        self.setPixmap(canvas)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self._qimage is None:
            return
        x, y, dw, dh = self._image_rect()
        if dw <= 0 or dh <= 0:
            return
        px = event.position().x()
        py = event.position().y()
        nx = (px - x) / dw
        ny = (py - y) / dh
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            self.clicked_norm.emit(nx, ny)


class ConveyorCalibrationDialog(QDialog):
    calibration_saved = Signal(object)
    # Transport requests: the dialog has no video of its own — the owner
    # (TargetOverlayController) applies these to the main viewer's playhead.
    transport_step = Signal(int)          # +/- frames
    transport_seek_fraction = Signal(float)  # 0.0..1.0 within the loaded clip

    def __init__(self, cal: ConveyorCalibration, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("Conveyor Calibration")
        self.setMinimumSize(860, 580)

        self._cal = ConveyorCalibration(
            system_id=cal.system_id,
            belt_pixels_per_sec=cal.belt_pixels_per_sec,
            tracking_line_start_norm=list(cal.tracking_line_start_norm) if cal.tracking_line_start_norm else None,
            tracking_line_end_norm=list(cal.tracking_line_end_norm) if cal.tracking_line_end_norm else None,
            tracking_line_duration_sec=cal.tracking_line_duration_sec,
        )
        self._current_time: datetime | None = None
        self._line_start_capture: tuple[datetime, tuple[float, float]] | None = None
        self._line_capture_mode: str | None = None

        self._build_ui()
        self._refresh_status()
        self._refresh_overlays()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        left = QVBoxLayout()
        self._canvas = _FrameCanvas()
        self._canvas.clicked_norm.connect(self._on_canvas_click)
        left.addWidget(self._canvas, 1)

        transport_row = QHBoxLayout()
        transport_row.setSpacing(4)
        for label, delta, tip in (
            ("−10", -10, "Step the viewer back 10 frames"),
            ("−1", -1, "Step the viewer back 1 frame"),
        ):
            btn = QPushButton(label)
            btn.setFixedWidth(44)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False, d=delta: self.transport_step.emit(d))
            transport_row.addWidget(btn)
        self._scrub = QSlider(Qt.Horizontal)
        self._scrub.setRange(0, 1000)
        self._scrub.setToolTip("Scrub the main viewer within the loaded clip")
        self._scrub.valueChanged.connect(self._on_scrub_changed)
        transport_row.addWidget(self._scrub, 1)
        for label, delta, tip in (
            ("+1", 1, "Step the viewer forward 1 frame"),
            ("+10", 10, "Step the viewer forward 10 frames"),
        ):
            btn = QPushButton(label)
            btn.setFixedWidth(44)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _checked=False, d=delta: self.transport_step.emit(d))
            transport_row.addWidget(btn)
        self._time_lbl = QLabel("--:--:--.---")
        self._time_lbl.setStyleSheet(theme.MONO_VALUE_LABEL)
        self._time_lbl.setToolTip("Current playhead time (local)")
        transport_row.addWidget(self._time_lbl)
        left.addLayout(transport_row)

        hint = QLabel(
            "The preview follows the main viewer — these controls move the viewer's playhead."
        )
        hint.setStyleSheet(theme.HINT_LABEL)
        hint.setWordWrap(True)
        left.addWidget(hint)

        self._mode_lbl = QLabel("Mode: idle")
        self._mode_lbl.setStyleSheet(theme.HINT_LABEL)
        left.addWidget(self._mode_lbl)
        root.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(6)

        vel_box = QGroupBox("Conveyor Tracking")
        vel_layout = QVBoxLayout(vel_box)

        vel_layout.addWidget(QLabel(
            "Use the same visible conveyor reference point across two frames.",
            wordWrap=True,
        ))
        vel_layout.addWidget(QLabel(
            "Capture start, scrub, then capture end. The saved line becomes the tracking vector.",
            wordWrap=True,
        ))

        mark_row = QHBoxLayout()
        self._btn_mark_a = QPushButton("Capture Start")
        self._btn_mark_a.clicked.connect(self._capture_line_start)
        mark_row.addWidget(self._btn_mark_a)
        self._btn_mark_b = QPushButton("Capture End")
        self._btn_mark_b.clicked.connect(self._capture_line_end)
        mark_row.addWidget(self._btn_mark_b)
        self._btn_clear_line = QPushButton("Clear Line")
        self._btn_clear_line.clicked.connect(self._clear_tracking_line)
        mark_row.addWidget(self._btn_clear_line)
        vel_layout.addLayout(mark_row)

        self._vel_status_lbl = QLabel("No markers set.")
        self._vel_status_lbl.setStyleSheet(theme.HINT_LABEL)
        self._vel_status_lbl.setWordWrap(True)
        vel_layout.addWidget(self._vel_status_lbl)

        right.addWidget(vel_box)

        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(theme.HINT_LABEL)
        right.addWidget(self._status_lbl)

        right.addStretch(1)

        btn_save = QPushButton("Save calibration")
        btn_save.clicked.connect(self._save)
        btn_save.setStyleSheet(theme.PRIMARY_ACTION_BUTTON)
        right.addWidget(btn_save)

        root.addLayout(right, 2)

    def on_frame(self, img: QImage | None) -> None:
        self._canvas.set_frame(img)
        self._refresh_overlays()

    def on_targets(self, targets: list[PickTarget]) -> None:
        _ = targets

    def on_time(self, dt: datetime) -> None:
        self._current_time = dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone(timezone.utc)
        self._time_lbl.setText(self._current_time.astimezone().strftime("%H:%M:%S.%f")[:-3])
        self._refresh_overlays()

    def on_clip_position(self, fraction: float) -> None:
        """Reflect the viewer's position on the scrub slider (0.0..1.0)."""
        if self._scrub.isSliderDown():
            return  # the user is dragging; don't fight them
        value = int(round(max(0.0, min(1.0, fraction)) * 1000))
        self._scrub.blockSignals(True)
        self._scrub.setValue(value)
        self._scrub.blockSignals(False)

    def _on_scrub_changed(self, value: int) -> None:
        # Only ever fires for user interaction: programmatic updates go
        # through on_clip_position with signals blocked.
        self.transport_seek_fraction.emit(value / 1000.0)

    def _refresh_status(self):
        parts = [f"Belt: {self._cal.belt_pixels_per_sec:.5f} norm-x/s"]
        if self._cal.has_tracking_line():
            vx, vy = self._cal.tracking_velocity_norm_per_sec() or (0.0, 0.0)
            parts.append(f"Track line: vx={vx:.5f}, vy={vy:.5f} norm/s")
            parts.append(f"Duration: {self._cal.tracking_line_duration_sec:.3f}s")
        else:
            parts.append("No tracking line captured yet.")
        self._status_lbl.setText("  ".join(parts))

    def _refresh_overlays(self):
        line = None
        marker = None
        if self._cal.has_tracking_line():
            start = self._cal.tracking_line_start_norm or [0.0, 0.0]
            end = self._cal.tracking_line_end_norm or [0.0, 0.0]
            line = ((float(start[0]), float(start[1])), (float(end[0]), float(end[1])))
        if self._line_start_capture is not None:
            line = (self._line_start_capture[1], self._line_start_capture[1])
            marker = self._line_start_capture[1]
        self._canvas.set_motion_line(line, marker)

    def _on_canvas_click(self, nx: float, ny: float):
        if self._line_capture_mode == "start":
            if self._current_time is None:
                QMessageBox.information(self, "No time", "No playhead time available.")
                return
            self._line_start_capture = (self._current_time.astimezone(timezone.utc), (nx, ny))
            self._line_capture_mode = None
            self._canvas.set_pending_dot(None)
            self._mode_lbl.setText(
                f"Start captured at ({nx:.4f}, {ny:.4f}) on {self._current_time.strftime('%H:%M:%S.%f')[:-3]}."
            )
            self._vel_status_lbl.setText("Start captured. Scrub to another frame, then capture the end point.")
            self._refresh_overlays()
            return
        if self._line_capture_mode == "end":
            self._finish_tracking_line(nx, ny)

    def _capture_line_start(self):
        if self._current_time is None:
            QMessageBox.information(self, "No time", "No playhead time available.")
            return
        self._line_capture_mode = "start"
        self._canvas.set_pending_dot(None)
        self._mode_lbl.setText("Click the conveyor reference point at the start frame.")
        self._vel_status_lbl.setText("Waiting for start-point click.")

    def _capture_line_end(self):
        if self._line_start_capture is None:
            QMessageBox.information(self, "No start point", "Capture the start point first.")
            return
        if self._current_time is None:
            QMessageBox.information(self, "No time", "No playhead time available.")
            return
        self._line_capture_mode = "end"
        self._canvas.set_pending_dot(None)
        self._mode_lbl.setText("Click the same conveyor reference point at the end frame.")
        self._vel_status_lbl.setText("Waiting for end-point click.")

    def _finish_tracking_line(self, nx: float, ny: float):
        if self._line_start_capture is None or self._current_time is None:
            return
        start_dt, start_pos = self._line_start_capture
        resolved = resolve_tracking_line(
            start_dt.astimezone(timezone.utc),
            start_pos,
            self._current_time.astimezone(timezone.utc),
            (nx, ny),
        )
        if resolved is None:
            QMessageBox.warning(self, "Too close", "Scrub further apart in time before capturing the end point.")
            return
        line_start, line_end, dt = resolved

        self._line_capture_mode = None
        # Clear the pending start capture: _refresh_overlays gives it
        # priority, so leaving it set drew a zero-length line at the start
        # point instead of the finished A->B line with its end marker.
        self._line_start_capture = None
        self._cal.tracking_line_start_norm = [line_start[0], line_start[1]]
        self._cal.tracking_line_end_norm = [line_end[0], line_end[1]]
        self._cal.tracking_line_duration_sec = dt
        self._cal.belt_pixels_per_sec = (line_end[0] - line_start[0]) / dt
        vx, vy = self._cal.tracking_velocity_norm_per_sec() or (0.0, 0.0)
        self._vel_status_lbl.setText(
            f"Tracking line set: dt={dt:.3f}s, vx={vx:.5f}, vy={vy:.5f} norm/s"
        )
        self._mode_lbl.setText("Mode: idle")
        self._refresh_overlays()
        self._refresh_status()

    def _clear_tracking_line(self):
        self._line_capture_mode = None
        self._line_start_capture = None
        self._cal.tracking_line_start_norm = None
        self._cal.tracking_line_end_norm = None
        self._cal.tracking_line_duration_sec = 0.0
        self._mode_lbl.setText("Mode: idle")
        self._vel_status_lbl.setText("No markers set.")
        self._refresh_overlays()
        self._refresh_status()

    def _save(self):
        save_calibration(self._cal)
        self.calibration_saved.emit(self._cal)
        self._refresh_status()
        self._vel_status_lbl.setText("Saved.")
