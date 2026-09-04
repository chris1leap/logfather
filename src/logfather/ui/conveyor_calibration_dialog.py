"""
Conveyor calibration dialog.

Line-based workflow only:
  1. Scrub to a frame and capture the start point on the conveyor.
  2. Scrub to a later frame and capture the same reference point again.
  3. Save the derived screen-space motion vector for manual marker tracking.
"""
from __future__ import annotations

import math
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
    QStyle,
    QStyleOptionSlider,
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


def _fmt_sig(value: float, sig: int = 2) -> str:
    """Friendly number: 2 significant figures, never scientific notation."""
    if value == 0 or not math.isfinite(value):
        return "0"
    magnitude = math.floor(math.log10(abs(value)))
    decimals = sig - 1 - magnitude
    rounded = round(value, decimals)
    return f"{rounded:.{max(0, decimals)}f}"


class _MarkerSlider(QSlider):
    """QSlider that paints the captured start/end positions as vertical
    ticks over the groove (fractions 0.0..1.0)."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._markers: list[float] = []

    def set_markers(self, fractions: list[float]) -> None:
        self._markers = [max(0.0, min(1.0, float(f))) for f in fractions]
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._markers:
            return
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
        span = max(1, groove.width() - handle.width())
        x0 = groove.x() + handle.width() // 2
        painter = QPainter(self)
        painter.setPen(QPen(QColor(theme.CAL_TRACK_LINE), 2))
        for fraction in self._markers:
            x = x0 + int(round(fraction * span))
            painter.drawLine(x, groove.top() - 2, x, groove.bottom() + 2)
        painter.end()


class _FrameCanvas(QLabel):
    clicked_norm = Signal(float, float)
    # ("start" | "end", nx, ny) — emitted continuously while a marker is
    # dragged, final position included.
    marker_dragged = Signal(str, float, float)

    _DRAG_HIT_RADIUS_PX = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setAlignment(Qt.AlignCenter)
        self.setMouseTracking(True)  # hover cursor over draggable markers
        self._qimage: QImage | None = None
        self._pending_dot: tuple[float, float] | None = None
        self._motion_line: tuple[tuple[float, float], tuple[float, float]] | None = None
        self._motion_marker: tuple[float, float] | None = None
        # Markers the dialog currently allows dragging (viewer parked on
        # that marker's frame): name -> normalized position for hit tests.
        self._draggable: dict[str, tuple[float, float]] = {}
        self._drag_name: str | None = None

    def set_draggable_markers(self, markers: dict[str, tuple[float, float]]) -> None:
        markers = dict(markers or {})
        if markers != self._draggable:
            self._draggable = markers
            self._refresh()  # the white drag-me outlines changed

    def _marker_at(self, px: float, py: float) -> str | None:
        x, y, dw, dh = self._image_rect()
        if dw <= 0 or dh <= 0:
            return None
        for name, (nx, ny) in self._draggable.items():
            mx, my = x + nx * dw, y + ny * dh
            if (px - mx) ** 2 + (py - my) ** 2 <= self._DRAG_HIT_RADIUS_PX ** 2:
                return name
        return None

    def _norm_at(self, px: float, py: float, clamp: bool = False) -> tuple[float, float] | None:
        x, y, dw, dh = self._image_rect()
        if dw <= 0 or dh <= 0:
            return None
        nx = (px - x) / dw
        ny = (py - y) / dh
        if clamp:
            return max(0.0, min(1.0, nx)), max(0.0, min(1.0, ny))
        if 0.0 <= nx <= 1.0 and 0.0 <= ny <= 1.0:
            return nx, ny
        return None

    def mouseMoveEvent(self, event):
        if self._drag_name is None:
            return
        norm = self._norm_at(event.position().x(), event.position().y(), clamp=True)
        if norm is not None:
            self.marker_dragged.emit(self._drag_name, norm[0], norm[1])

    def mouseReleaseEvent(self, event):
        self._drag_name = None

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
            painter.setPen(QPen(QColor(theme.CAL_TRACK_LINE), 2))
            painter.drawLine(int(px) - 10, int(py), int(px) + 10, int(py))
            painter.drawLine(int(px), int(py) - 10, int(px), int(py) + 10)
            fill = QColor(theme.CAL_TRACK_LINE)
            fill.setAlpha(120)
            painter.setBrush(QBrush(fill))
            painter.drawEllipse(QPointF(px, py), 5, 5)

        if self._motion_line is not None:
            (sx, sy), (ex, ey) = self._motion_line
            start_px = QPointF(x + sx * dw, y + sy * dh)
            end_px = QPointF(x + ex * dw, y + ey * dh)
            # A zero-length line is the start-only state (end not yet
            # captured): one dot, one label — both labels on the same
            # point made it read "End".
            start_only = abs(sx - ex) < 1e-9 and abs(sy - ey) < 1e-9
            painter.setPen(QPen(QColor(theme.CAL_TRACK_LINE), 2, Qt.DashLine))
            if not start_only:
                painter.drawLine(start_px, end_px)
            painter.setBrush(QBrush(QColor(theme.CAL_TRACK_LINE)))
            painter.drawEllipse(start_px, 5, 5)
            if not start_only:
                painter.drawEllipse(end_px, 5, 5)
            painter.setFont(QFont("monospace", 9, QFont.Bold))
            labels = [("Start", start_px)]
            if not start_only:
                labels.append(("End", end_px))
            for label, pt in labels:
                tx, ty = int(pt.x()) + 8, int(pt.y()) - 8
                # Shadow first so the label reads on any footage.
                painter.setPen(QPen(Qt.black))
                painter.drawText(tx + 1, ty + 1, label)
                painter.setPen(QPen(Qt.white))
                painter.drawText(tx, ty, label)
            if self._motion_marker is not None:
                mx, my = self._motion_marker
                marker_pt = QPointF(x + mx * dw, y + my * dh)
                painter.setPen(QPen(Qt.white, 2))
                painter.setBrush(QBrush(QColor("#ffffff")))
                painter.drawEllipse(marker_pt, 4, 4)

        # White outline = this point is draggable right now (the viewer is
        # parked on its capture frame).
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.setBrush(Qt.NoBrush)
        for _name, (nx, ny) in self._draggable.items():
            painter.drawEllipse(QPointF(x + nx * dw, y + ny * dh), 9, 9)

        painter.end()
        self.setPixmap(canvas)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self._qimage is None:
            return
        px = event.position().x()
        py = event.position().y()
        name = self._marker_at(px, py)
        if name is not None:
            self._drag_name = name
            return
        norm = self._norm_at(px, py)
        if norm is not None:
            self.clicked_norm.emit(norm[0], norm[1])


class ConveyorCalibrationDialog(QDialog):
    calibration_saved = Signal(object)
    # Transport requests: the dialog has no video of its own — the owner
    # (TargetOverlayController) applies these to the main viewer's playhead.
    transport_step = Signal(int)          # +/- frames
    transport_seek_fraction = Signal(float)  # 0.0..1.0 within the loaded clip
    transport_play = Signal()
    transport_pause = Signal()
    # Exact-frame jump to a captured position (0.0..1.0 of frame range).
    transport_jump_fraction = Signal(float)

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
            capture_clip_key=cal.capture_clip_key,
            capture_start_fraction=cal.capture_start_fraction,
            capture_end_fraction=cal.capture_end_fraction,
        )
        self._clip_key: str | None = None
        self._position_info = None  # set by set_clip_context
        self._current_time: datetime | None = None
        self._line_start_capture: tuple[datetime, tuple[float, float], float | None] | None = None
        self._line_capture_mode: str | None = None
        # Clip position (0..1 of frame range) fed by on_clip_position, and
        # the captured start/end positions for the scrub markers and the
        # go-to buttons. Session-only: saved calibrations don't carry them.
        self._last_position_fraction: float | None = None
        self._start_fraction: float | None = None
        self._end_fraction: float | None = None
        self._current_frame_index: int | None = None

        self._build_ui()
        self._refresh_status()
        self._refresh_overlays()

    def _build_ui(self):
        # Outer stack: the canvas/controls columns, then the full-width
        # scrub timeline, then the transport buttons under it (Chris,
        # 2026-09-04: timeline above the buttons, spanning the window).
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)
        root = QHBoxLayout()
        root.setSpacing(8)
        outer.addLayout(root, 1)

        left = QVBoxLayout()
        self._canvas = _FrameCanvas()
        self._canvas.clicked_norm.connect(self._on_canvas_click)
        self._canvas.marker_dragged.connect(self._on_marker_dragged)
        left.addWidget(self._canvas, 1)

        scrub_row = QHBoxLayout()
        scrub_row.setSpacing(8)
        self._clip_start_lbl = QLabel("")
        self._clip_start_lbl.setStyleSheet(theme.MONO_VALUE_LABEL)
        self._clip_start_lbl.setToolTip("Clip start time")
        self._clip_end_lbl = QLabel("")
        self._clip_end_lbl.setStyleSheet(theme.MONO_VALUE_LABEL)
        self._clip_end_lbl.setToolTip("Clip end time and total frames")
        self._scrub = _MarkerSlider(Qt.Horizontal)
        self._scrub.setRange(0, 1000)
        self._scrub.setToolTip("Scrub the main viewer within the loaded clip")
        self._scrub.valueChanged.connect(self._on_scrub_changed)
        scrub_row.addWidget(self._clip_start_lbl)
        scrub_row.addWidget(self._scrub, 1)
        scrub_row.addWidget(self._clip_end_lbl)
        outer.addLayout(scrub_row)

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
        # Standard media icons rather than words; -10, -1, play, pause,
        # +1, +10 order (Chris, 2026-09-04).
        self._btn_play = QPushButton()
        self._btn_play.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self._btn_play.setFixedWidth(36)
        self._btn_play.setToolTip("Play the main viewer")
        self._btn_play.clicked.connect(lambda _checked=False: self.transport_play.emit())
        transport_row.addWidget(self._btn_play)
        btn_pause = QPushButton()
        btn_pause.setIcon(self.style().standardIcon(QStyle.SP_MediaPause))
        btn_pause.setFixedWidth(36)
        btn_pause.setToolTip("Pause the main viewer")
        btn_pause.clicked.connect(lambda _checked=False: self.transport_pause.emit())
        transport_row.addWidget(btn_pause)
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
        self._frame_lbl = QLabel("")
        self._frame_lbl.setStyleSheet(theme.MONO_VALUE_LABEL)
        self._frame_lbl.setToolTip("Current frame / last frame of the loaded clip")
        transport_row.addWidget(self._frame_lbl)
        # Centre the whole transport cluster under the full-width timeline.
        transport_row.insertStretch(0, 1)
        transport_row.insertSpacing(7, 16)  # gap between buttons and readouts
        transport_row.addStretch(1)
        outer.addLayout(transport_row)

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

        goto_row = QHBoxLayout()
        self._btn_go_start = QPushButton("Edit start")
        self._btn_go_start.setToolTip("Jump to the start point's frame; its circle then becomes draggable for fine edits")
        self._btn_go_start.clicked.connect(lambda: self._jump_to_capture(self._start_fraction))
        self._btn_go_end = QPushButton("Edit end")
        self._btn_go_end.setToolTip("Jump to the end point's frame; its circle then becomes draggable for fine edits")
        self._btn_go_end.clicked.connect(lambda: self._jump_to_capture(self._end_fraction))
        for btn in (self._btn_go_start, self._btn_go_end):
            btn.setEnabled(False)
            goto_row.addWidget(btn)
        vel_layout.addLayout(goto_row)

        self._vel_status_lbl = QLabel("No markers set.")
        self._vel_status_lbl.setStyleSheet(theme.HINT_LABEL)
        self._vel_status_lbl.setWordWrap(True)
        vel_layout.addWidget(self._vel_status_lbl)

        right.addWidget(vel_box)

        # Results: the derived belt speed / distance numbers and the save
        # button, separate from the capture workflow (Chris, 2026-09-04).
        results_box = QGroupBox("Results")
        results_layout = QVBoxLayout(results_box)
        self._status_lbl = QLabel("")
        self._status_lbl.setWordWrap(True)
        self._status_lbl.setStyleSheet(theme.CAL_RESULTS_TEXT)
        results_layout.addWidget(self._status_lbl)
        btn_save = QPushButton("Save calibration")
        btn_save.clicked.connect(self._save)
        btn_save.setStyleSheet(theme.PRIMARY_ACTION_BUTTON)
        results_layout.addWidget(btn_save)
        right.addWidget(results_box)

        right.addStretch(1)

        root.addLayout(right, 2)

    def on_frame(self, img: QImage | None) -> None:
        first_frame = img is not None and (
            self._canvas._qimage is None or self._canvas._qimage.isNull()
        )
        self._canvas.set_frame(img)
        self._refresh_overlays()
        if first_frame:
            self._refresh_status()  # pixel positions become computable now

    def on_targets(self, targets: list[PickTarget]) -> None:
        _ = targets

    def on_time(self, dt: datetime) -> None:
        self._current_time = dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone(timezone.utc)
        self._time_lbl.setText(self._current_time.astimezone().strftime("%H:%M:%S.%f")[:-3])
        self._refresh_overlays()

    def on_playing_state(self, playing: bool) -> None:
        self._btn_play.setStyleSheet(theme.SYNC_DONE_BUTTON if playing else "")

    def on_clip_position(self, fraction: float, frame: int | None = None, frame_count: int | None = None) -> None:
        """Reflect the viewer's position on the scrub slider (0.0..1.0)."""
        self._last_position_fraction = max(0.0, min(1.0, float(fraction)))
        if frame is not None and int(frame) != self._current_frame_index:
            self._current_frame_index = int(frame)
            self._update_draggable_markers()
        if frame is not None and frame_count:
            # 1-based for display: the first frame is 1, the last is the
            # total frame count (Chris, 2026-09-04).
            self._frame_lbl.setText(f"Current frame: {int(frame) + 1} / {int(frame_count)}")
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
        # Result order per Chris (2026-09-04): start px, end px, distance,
        # frames elapsed, belt speed.
        if not self._cal.has_tracking_line():
            self._status_lbl.setText(
                f"Belt speed: {_fmt_sig(self._cal.belt_pixels_per_sec)} norm-x/s\n"
                "No tracking line captured yet."
            )
            return
        start = self._cal.tracking_line_start_norm or [0.0, 0.0]
        end = self._cal.tracking_line_end_norm or [0.0, 0.0]
        sx, sy = float(start[0]), float(start[1])
        ex, ey = float(end[0]), float(end[1])
        dx, dy = ex - sx, ey - sy
        lines = []
        img = self._canvas._qimage
        dims = None
        if img is not None and not img.isNull():
            dims = (img.width(), img.height())
        if dims:
            w, h = dims
            lines.append(f"{'Start:':<12}x: {sx * w:>5.0f} px     y: {sy * h:>5.0f} px")
            lines.append(f"{'End:':<12}x: {ex * w:>5.0f} px     y: {ey * h:>5.0f} px")
            lines.append(
                f"{'Distance:':<12}x: {abs(dx * w):>5.0f} px     y: {abs(dy * h):>5.0f} px"
                f"   (total {_fmt_sig(math.hypot(dx * w, dy * h))} px)"
            )
        else:
            lines.append(f"Start: x: {sx:.2f}, y: {sy:.2f} norm")
            lines.append(f"End: x: {ex:.2f}, y: {ey:.2f} norm")
            lines.append(f"Distance: {_fmt_sig(math.hypot(dx, dy))} norm")
        duration = float(self._cal.tracking_line_duration_sec)
        frames_elapsed = None
        if (
            self._position_info is not None
            and self._start_fraction is not None
            and self._end_fraction is not None
        ):
            try:
                start_frame, _ = self._position_info(self._start_fraction)
                end_frame, _ = self._position_info(self._end_fraction)
                frames_elapsed = abs(int(end_frame) - int(start_frame))
            except Exception:
                frames_elapsed = None
        if frames_elapsed is not None:
            lines.append(f"Frames elapsed: {frames_elapsed} ({_fmt_sig(duration)}s)")
        else:
            lines.append(f"Duration: {_fmt_sig(duration)}s")
        vx, vy = self._cal.tracking_velocity_norm_per_sec() or (0.0, 0.0)
        if dims:
            w, h = dims
            direction = "right to left" if vx < 0 else "left to right"
            lines.append(
                f"{'Belt speed:':<12}x: {vx * w:>5.0f} px/s   y: {vy * h:>5.0f} px/s"
                f"   (total {_fmt_sig(math.hypot(vx * w, vy * h))} px/s, {direction})"
            )
        else:
            lines.append(f"Belt speed: {_fmt_sig(self._cal.belt_pixels_per_sec)} norm-x/s")
            lines.append(f"Velocity: vx={_fmt_sig(vx)}, vy={_fmt_sig(vy)} norm/s")
        self._status_lbl.setText("\n".join(lines))

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
            self._line_start_capture = (
                self._current_time.astimezone(timezone.utc),
                (nx, ny),
                self._last_position_fraction,
            )
            self._start_fraction = self._last_position_fraction
            self._end_fraction = None
            self._update_capture_markers()
            self._line_capture_mode = None
            self._canvas.set_pending_dot(None)
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
        self._vel_status_lbl.setText("Click the conveyor reference point at the start frame.")

    def _capture_line_end(self):
        if self._line_start_capture is None:
            QMessageBox.information(self, "No start point", "Capture the start point first.")
            return
        if self._current_time is None:
            QMessageBox.information(self, "No time", "No playhead time available.")
            return
        self._line_capture_mode = "end"
        self._canvas.set_pending_dot(None)
        self._vel_status_lbl.setText("Click the same conveyor reference point at the end frame.")

    def set_clip_context(self, clip_key: str | None, position_info=None) -> None:
        """Tell the dialog which clip the viewer has open. Restores the
        saved capture markers when it matches the calibration's capture
        clip — the fractions are meaningless on any other clip.
        `position_info(fraction) -> (frame_number, playback_dt | None)`
        renders the captured positions as frame numbers and times."""
        self._clip_key = clip_key
        self._position_info = position_info
        self._refresh_clip_range_labels()
        if (
            clip_key
            and self._cal.capture_clip_key == clip_key
            and self._start_fraction is None
            and self._end_fraction is None
        ):
            self._start_fraction = self._cal.capture_start_fraction
            self._end_fraction = self._cal.capture_end_fraction
            self._update_capture_markers()

    def _refresh_clip_range_labels(self):
        start_text = end_text = ""
        if self._position_info is not None:
            try:
                _first, start_dt = self._position_info(0.0)
                last_frame, end_dt = self._position_info(1.0)
                if start_dt is not None:
                    start_text = start_dt.astimezone().strftime("%H:%M:%S")
                total = f"{int(last_frame) + 1} frames"
                if end_dt is not None:
                    end_text = f"{end_dt.astimezone().strftime('%H:%M:%S')} ({total})"
                else:
                    end_text = f"({total})"
            except Exception:
                pass
        self._clip_start_lbl.setText(start_text)
        self._clip_end_lbl.setText(end_text)

    def _frame_for_fraction(self, fraction: float | None) -> int | None:
        if fraction is None or self._position_info is None:
            return None
        try:
            return int(self._position_info(fraction)[0])
        except Exception:
            return None

    def _update_draggable_markers(self):
        """A marker is fine-editable by dragging only while the viewer is
        parked on the frame it was captured at."""
        markers: dict[str, tuple[float, float]] = {}
        cur = self._current_frame_index
        if cur is not None:
            if self._line_start_capture is not None:
                if self._frame_for_fraction(self._line_start_capture[2]) == cur:
                    markers["start"] = self._line_start_capture[1]
            elif self._cal.has_tracking_line():
                if self._frame_for_fraction(self._start_fraction) == cur:
                    start = self._cal.tracking_line_start_norm or [0.0, 0.0]
                    markers["start"] = (float(start[0]), float(start[1]))
                if self._frame_for_fraction(self._end_fraction) == cur:
                    end = self._cal.tracking_line_end_norm or [0.0, 0.0]
                    markers["end"] = (float(end[0]), float(end[1]))
        self._canvas.set_draggable_markers(markers)

    def _on_marker_dragged(self, name: str, nx: float, ny: float):
        if name == "start" and self._line_start_capture is not None:
            capture_dt, _pos, capture_fraction = self._line_start_capture
            self._line_start_capture = (capture_dt, (nx, ny), capture_fraction)
            self._refresh_overlays()
            self._update_draggable_markers()
            return
        if not self._cal.has_tracking_line():
            return
        if name == "start":
            self._cal.tracking_line_start_norm = [nx, ny]
        elif name == "end":
            self._cal.tracking_line_end_norm = [nx, ny]
        else:
            return
        duration = float(self._cal.tracking_line_duration_sec)
        if duration > 0:
            start = self._cal.tracking_line_start_norm or [0.0, 0.0]
            end = self._cal.tracking_line_end_norm or [0.0, 0.0]
            self._cal.belt_pixels_per_sec = (float(end[0]) - float(start[0])) / duration
        self._vel_status_lbl.setText(f"{name.capitalize()} point adjusted — Save to keep.")
        self._refresh_overlays()
        self._refresh_status()
        self._update_draggable_markers()

    def _jump_to_capture(self, fraction: float | None):
        if fraction is not None:
            self.transport_jump_fraction.emit(fraction)

    def _update_capture_markers(self):
        markers = [f for f in (self._start_fraction, self._end_fraction) if f is not None]
        self._scrub.set_markers(markers)
        unavailable_hint = (
            "Capture position unknown: the line was captured on a different "
            "clip (or with an older version). Recapture to enable."
        )
        for btn, name, fraction, tip in (
            (self._btn_go_start, "start", self._start_fraction,
             "Jump to the start point's frame; its circle then becomes draggable for fine edits"),
            (self._btn_go_end, "end", self._end_fraction,
             "Jump to the end point's frame; its circle then becomes draggable for fine edits"),
        ):
            known = fraction is not None
            btn.setEnabled(known)
            btn.setToolTip(tip if known else unavailable_hint)
            frame = None
            if known and self._position_info is not None:
                try:
                    frame, _dt = self._position_info(fraction)
                except Exception:
                    frame = None
            btn.setText(f"Edit {name} ({frame + 1})" if frame is not None else f"Edit {name}")
        self._update_draggable_markers()

    def _finish_tracking_line(self, nx: float, ny: float):
        if self._line_start_capture is None or self._current_time is None:
            return
        start_dt, start_pos, start_fraction = self._line_start_capture
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
        # point instead of the finished Start->End line with its end marker.
        self._line_start_capture = None
        # Keep the go-to fractions in time-forward order, mirroring the
        # point swap resolve_tracking_line applies on a reverse capture.
        end_fraction = self._last_position_fraction
        if (
            start_fraction is not None
            and end_fraction is not None
            and end_fraction < start_fraction
        ):
            start_fraction, end_fraction = end_fraction, start_fraction
        self._start_fraction = start_fraction
        self._end_fraction = end_fraction
        self._update_capture_markers()
        # Persisted with the calibration so reopening on this clip restores
        # the markers and go-to buttons.
        self._cal.capture_clip_key = self._clip_key
        self._cal.capture_start_fraction = start_fraction
        self._cal.capture_end_fraction = end_fraction
        self._cal.tracking_line_start_norm = [line_start[0], line_start[1]]
        self._cal.tracking_line_end_norm = [line_end[0], line_end[1]]
        self._cal.tracking_line_duration_sec = dt
        self._cal.belt_pixels_per_sec = (line_end[0] - line_start[0]) / dt
        vx, vy = self._cal.tracking_velocity_norm_per_sec() or (0.0, 0.0)
        self._vel_status_lbl.setText("Tracking line set.")
        self._refresh_overlays()
        self._refresh_status()

    def _clear_tracking_line(self):
        self._line_capture_mode = None
        self._line_start_capture = None
        self._start_fraction = None
        self._end_fraction = None
        self._update_capture_markers()
        self._cal.capture_clip_key = None
        self._cal.capture_start_fraction = None
        self._cal.capture_end_fraction = None
        self._cal.tracking_line_start_norm = None
        self._cal.tracking_line_end_norm = None
        self._cal.tracking_line_duration_sec = 0.0
        self._vel_status_lbl.setText("No markers set.")
        self._refresh_overlays()
        self._refresh_status()

    def _save(self):
        save_calibration(self._cal)
        self.calibration_saved.emit(self._cal)
        self._refresh_status()
