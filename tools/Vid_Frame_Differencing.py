import sys
import subprocess
import shutil
from pathlib import Path
from datetime import timedelta

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QSlider, QSizePolicy, QCheckBox, QComboBox,
)


def format_timecode(td: timedelta) -> str:
    total_ms = int(td.total_seconds() * 1000)
    if total_ms < 0:
        total_ms = 0
    hours = total_ms // 3_600_000
    rem = total_ms % 3_600_000
    minutes = rem // 60_000
    rem = rem % 60_000
    seconds = rem // 1000
    ms = rem % 1000
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


class ScrubbableLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scrub_callback = None

    def set_scrub_callback(self, cb):
        self._scrub_callback = cb

    def wheelEvent(self, event):
        if self._scrub_callback is not None:
            delta = event.angleDelta().y()
            if delta > 0:
                self._scrub_callback(-1)
            elif delta < 0:
                self._scrub_callback(1)
            event.accept()
        else:
            super().wheelEvent(event)


def _resize_for_compute(img: np.ndarray, scale: float) -> np.ndarray:
    if scale >= 0.999:
        return img
    h, w = img.shape[:2]
    nw = max(2, int(w * scale))
    nh = max(2, int(h * scale))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)


def _upscale_to(img: np.ndarray, target_shape_hw: tuple[int, int]) -> np.ndarray:
    th, tw = target_shape_hw
    h, w = img.shape[:2]
    if (h, w) == (th, tw):
        return img
    return cv2.resize(img, (tw, th), interpolation=cv2.INTER_NEAREST)


def compute_pixel_diff_view(
    frame_rgb: np.ndarray,
    base_rgb: np.ndarray,
    gain: float,
    threshold: int,
    heatmap: bool,
    overlay: bool,
    alpha: float,
) -> np.ndarray:
    diff = cv2.absdiff(frame_rgb, base_rgb)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_RGB2GRAY)

    d = diff_gray.astype(np.float32) * float(gain)
    d = np.clip(d, 0, 255).astype(np.uint8)

    if threshold > 0:
        _, d = cv2.threshold(d, threshold, 255, cv2.THRESH_TOZERO)

    if heatmap:
        colored = cv2.applyColorMap(d, cv2.COLORMAP_TURBO)  # BGR
        colored = cv2.cvtColor(colored, cv2.COLOR_BGR2RGB)
        if overlay:
            return cv2.addWeighted(frame_rgb, 1.0 - alpha, colored, alpha, 0.0)
        return colored

    gray_rgb = cv2.cvtColor(d, cv2.COLOR_GRAY2RGB)
    if overlay:
        return cv2.addWeighted(frame_rgb, 1.0 - alpha, gray_rgb, alpha, 0.0)
    return gray_rgb


def draw_flow_arrows(
    canvas_rgb: np.ndarray,
    flow: np.ndarray,
    step: int = 20,
    scale: float = 1.0,
    max_arrows: int = 2000,
) -> np.ndarray:
    """
    Draw sparse arrows on canvas_rgb using dense flow (dx,dy).
    step: sampling grid spacing in pixels (of flow space).
    scale: multiplier for arrow length (visual only).
    """
    out = canvas_rgb.copy()
    h, w = flow.shape[:2]

    count = 0
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dx, dy = flow[y, x]
            x2 = int(round(x + dx * scale))
            y2 = int(round(y + dy * scale))
            x2 = max(0, min(w - 1, x2))
            y2 = max(0, min(h - 1, y2))

            # cv2 draws in-place; it expects int tuples, color in RGB here (we don't force colors)
            cv2.arrowedLine(out, (x, y), (x2, y2), (255, 255, 255), 1, tipLength=0.35)

            count += 1
            if count >= max_arrows:
                return out
    return out


def compute_optical_flow_view(
    frame_rgb: np.ndarray,
    base_rgb: np.ndarray,
    gain: float,
    min_motion: int,
    heatmap: bool,
    overlay: bool,
    alpha: float,
    arrows: bool,
    arrow_step: int,
    arrow_scale: float,
    compute_scale: float,
) -> np.ndarray:
    """
    Dense optical flow (Farneback) between base_rgb -> frame_rgb.
    Visualize magnitude as grayscale/heatmap, optionally overlay and arrows.
    """
    # Convert to gray
    base_gray = cv2.cvtColor(base_rgb, cv2.COLOR_RGB2GRAY)
    frame_gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

    # Downscale for compute speed
    base_small = _resize_for_compute(base_gray, compute_scale)
    frame_small = _resize_for_compute(frame_gray, compute_scale)

    # Farneback dense flow
    flow = cv2.calcOpticalFlowFarneback(
        base_small, frame_small, None,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0
    )

    mag, _ang = cv2.cartToPolar(flow[..., 0], flow[..., 1], angleInDegrees=False)

    # Visualize magnitude
    m = mag.astype(np.float32) * float(gain)

    # Map to 0..255 (clip)
    m = np.clip(m, 0.0, 255.0).astype(np.uint8)

    # Suppress low motion (min_motion is in 0..255 display space)
    if min_motion > 0:
        _, m = cv2.threshold(m, min_motion, 255, cv2.THRESH_TOZERO)

    if heatmap:
        vis = cv2.applyColorMap(m, cv2.COLORMAP_TURBO)  # BGR
        vis = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
    else:
        vis = cv2.cvtColor(m, cv2.COLOR_GRAY2RGB)

    # Upscale vis (and flow for arrows) back to full size if scaled
    H, W = frame_rgb.shape[:2]
    if compute_scale < 0.999:
        vis = _upscale_to(vis, (H, W))
        # also upscale flow vectors (need to scale displacement by inverse of compute_scale)
        flow_up = cv2.resize(flow, (W, H), interpolation=cv2.INTER_NEAREST).astype(np.float32)
        flow_up[..., 0] *= (1.0 / compute_scale)
        flow_up[..., 1] *= (1.0 / compute_scale)
    else:
        flow_up = flow

    if arrows:
        vis = draw_flow_arrows(vis, flow_up, step=arrow_step, scale=arrow_scale)

    if overlay:
        return cv2.addWeighted(frame_rgb, 1.0 - alpha, vis, alpha, 0.0)

    return vis


class VideoAnalysisViewer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Analysis Viewer (Diff + Optical Flow)")

        # Video state
        self.cap = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False

        # Current decoded frame
        self.last_frame_rgb: np.ndarray | None = None
        self.last_qimage: QImage | None = None

        # Reference frame for "Reference -> Current"
        self.ref_frame_rgb: np.ndarray | None = None
        self.ref_frame_index: int | None = None

        # Previous frame for "Previous -> Current"
        self.prev_frame_rgb: np.ndarray | None = None
        self.prev_frame_index: int | None = None

        # Playback timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)

        # ---------- UI ----------
        self.open_video_btn = QPushButton("Open Video")
        self.open_video_btn.clicked.connect(self.open_video)

        self.play_pause_btn = QPushButton("Play")
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)

        self.set_ref_btn = QPushButton("Set Reference Frame")
        self.set_ref_btn.clicked.connect(self.set_reference_frame)

        self.clear_ref_btn = QPushButton("Clear Reference")
        self.clear_ref_btn.clicked.connect(self.clear_reference_frame)

        top_controls = QHBoxLayout()
        top_controls.addWidget(self.open_video_btn)
        top_controls.addWidget(self.play_pause_btn)
        top_controls.addStretch(1)
        top_controls.addWidget(self.set_ref_btn)
        top_controls.addWidget(self.clear_ref_btn)

        # Views
        self.video_label = ScrubbableLabel("No video loaded")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(480, 270)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.set_scrub_callback(self.scrub_by_frames)

        self.analysis_label = ScrubbableLabel("Analysis view")
        self.analysis_label.setAlignment(Qt.AlignCenter)
        self.analysis_label.setMinimumSize(480, 270)
        self.analysis_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.analysis_label.set_scrub_callback(self.scrub_by_frames)

        views = QHBoxLayout()
        views.addWidget(self.video_label, stretch=1)
        views.addWidget(self.analysis_label, stretch=1)

        # Seek + info
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.on_slider_moved)
        self.seek_slider.sliderPressed.connect(self.pause)

        self.info_label = QLabel("Time: 00:00:00,000 | Frame: 0")

        # Analysis controls
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["None", "Pixel Diff", "Optical Flow"])
        self.mode_combo.currentIndexChanged.connect(self.refresh_views)

        self.pair_combo = QComboBox()
        self.pair_combo.addItems(["Reference → Current", "Previous → Current"])
        self.pair_combo.currentIndexChanged.connect(self.refresh_views)

        self.heatmap_cb = QCheckBox("Heatmap")
        self.heatmap_cb.setChecked(True)
        self.heatmap_cb.stateChanged.connect(self.refresh_views)

        self.overlay_cb = QCheckBox("Overlay on video")
        self.overlay_cb.setChecked(False)
        self.overlay_cb.stateChanged.connect(self.refresh_views)

        self.arrows_cb = QCheckBox("Flow arrows")
        self.arrows_cb.setChecked(False)
        self.arrows_cb.stateChanged.connect(self.refresh_views)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Mode:"))
        row1.addWidget(self.mode_combo)
        row1.addSpacing(12)
        row1.addWidget(QLabel("Pairing:"))
        row1.addWidget(self.pair_combo)
        row1.addStretch(1)
        row1.addWidget(self.heatmap_cb)
        row1.addWidget(self.overlay_cb)
        row1.addWidget(self.arrows_cb)

        # Gain slider
        self.gain_label = QLabel("Gain: 6x")
        self.gain_slider = QSlider(Qt.Horizontal)
        self.gain_slider.setRange(1, 30)
        self.gain_slider.setValue(6)
        self.gain_slider.valueChanged.connect(self.on_gain_changed)

        # Threshold / Min motion (0..255)
        self.thresh_label = QLabel("Threshold / Min motion: 15")
        self.thresh_slider = QSlider(Qt.Horizontal)
        self.thresh_slider.setRange(0, 255)
        self.thresh_slider.setValue(15)
        self.thresh_slider.valueChanged.connect(self.on_thresh_changed)

        # Alpha (0..1)
        self.alpha_label = QLabel("Overlay alpha: 0.60")
        self.alpha_slider = QSlider(Qt.Horizontal)
        self.alpha_slider.setRange(0, 100)
        self.alpha_slider.setValue(60)
        self.alpha_slider.valueChanged.connect(self.on_alpha_changed)

        # Compute scale (25%..100%)
        self.scale_label = QLabel("Compute scale: 100%")
        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(25, 100)
        self.scale_slider.setValue(100)
        self.scale_slider.valueChanged.connect(self.on_scale_changed)

        # Arrow density/scale
        self.arrow_step_label = QLabel("Arrow step: 20 px")
        self.arrow_step_slider = QSlider(Qt.Horizontal)
        self.arrow_step_slider.setRange(8, 60)
        self.arrow_step_slider.setValue(20)
        self.arrow_step_slider.valueChanged.connect(self.on_arrow_step_changed)

        self.arrow_scale_label = QLabel("Arrow length scale: 1.5x")
        self.arrow_scale_slider = QSlider(Qt.Horizontal)
        self.arrow_scale_slider.setRange(5, 50)  # 0.5..5.0
        self.arrow_scale_slider.setValue(15)
        self.arrow_scale_slider.valueChanged.connect(self.on_arrow_scale_changed)

        row2 = QHBoxLayout()
        row2.addWidget(self.gain_label)
        row2.addWidget(self.gain_slider)

        row3 = QHBoxLayout()
        row3.addWidget(self.thresh_label)
        row3.addWidget(self.thresh_slider)

        row4 = QHBoxLayout()
        row4.addWidget(self.alpha_label)
        row4.addWidget(self.alpha_slider)

        row5 = QHBoxLayout()
        row5.addWidget(self.scale_label)
        row5.addWidget(self.scale_slider)

        row6 = QHBoxLayout()
        row6.addWidget(self.arrow_step_label)
        row6.addWidget(self.arrow_step_slider)

        row7 = QHBoxLayout()
        row7.addWidget(self.arrow_scale_label)
        row7.addWidget(self.arrow_scale_slider)

        # Root layout
        root = QVBoxLayout()
        root.addLayout(top_controls)
        root.addLayout(views)
        root.addWidget(self.seek_slider)
        root.addWidget(self.info_label)
        root.addLayout(row1)
        root.addLayout(row2)
        root.addLayout(row3)
        root.addLayout(row4)
        root.addLayout(row5)
        root.addLayout(row6)
        root.addLayout(row7)

        self.setLayout(root)
        self.setMinimumSize(1250, 720)

    # ---- ffmpeg rewrap helper ----
    def try_rewrap_video_with_ffmpeg(self, file_path: str) -> str | None:
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            return None

        in_path = Path(file_path)
        out_path = in_path.with_name(in_path.stem + "_fixed" + in_path.suffix)
        if out_path.exists():
            return str(out_path)

        cmd = [ffmpeg_path, "-y", "-i", str(in_path), "-c", "copy", str(out_path)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                QMessageBox.information(
                    self, "Video rewrapped",
                    f"Video container was adjusted with ffmpeg:\n{out_path.name}"
                )
                return str(out_path)
            return None
        except Exception:
            return None

    # ---- Video handling ----
    def open_video(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        )
        if not file_path:
            return

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        fixed_path = self.try_rewrap_video_with_ffmpeg(file_path)
        load_path = fixed_path or file_path

        self.cap = cv2.VideoCapture(load_path)
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Failed to open video:\n{load_path}")
            self.cap = None
            return

        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.current_frame = 0
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))

        # Reset analysis state
        self.ref_frame_rgb = None
        self.ref_frame_index = None
        self.prev_frame_rgb = None
        self.prev_frame_index = None
        self.analysis_label.setText("Analysis view")

        self.show_frame(self.current_frame)

    # ---- Playback control ----
    def toggle_play_pause(self):
        if self.cap is None:
            return
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.cap is None:
            return
        if not self.playing:
            self.playing = True
            self.play_pause_btn.setText("Pause")
            interval_ms = int(1000 / self.fps) if self.fps > 0 else 40
            self.timer.start(interval_ms)

    def pause(self):
        if self.playing:
            self.playing = False
            self.play_pause_btn.setText("Play")
            self.timer.stop()

    def scrub_by_frames(self, delta_frames: int):
        if self.cap is None:
            return
        self.pause()
        new_frame = self.current_frame + delta_frames
        if self.frame_count > 0:
            new_frame = max(0, min(self.frame_count - 1, new_frame))
        else:
            new_frame = max(0, new_frame)
        self.current_frame = new_frame
        self.show_frame(self.current_frame)

    def next_frame(self):
        if self.cap is None:
            return
        self.current_frame += 1
        if self.current_frame >= self.frame_count:
            self.pause()
            return
        self.show_frame(self.current_frame)

    def on_slider_moved(self, value: int):
        if self.cap is None:
            return
        self.pause()
        self.current_frame = int(value)
        self.show_frame(self.current_frame)

    # ---- Reference ----
    def set_reference_frame(self):
        if self.last_frame_rgb is None:
            return
        self.ref_frame_rgb = self.last_frame_rgb.copy()
        self.ref_frame_index = int(self.current_frame)
        self.refresh_views()

    def clear_reference_frame(self):
        self.ref_frame_rgb = None
        self.ref_frame_index = None
        self.refresh_views()

    # ---- UI change handlers ----
    def on_gain_changed(self, v: int):
        self.gain_label.setText(f"Gain: {v}x")
        self.refresh_views()

    def on_thresh_changed(self, v: int):
        self.thresh_label.setText(f"Threshold / Min motion: {v}")
        self.refresh_views()

    def on_alpha_changed(self, v: int):
        a = v / 100.0
        self.alpha_label.setText(f"Overlay alpha: {a:.2f}")
        self.refresh_views()

    def on_scale_changed(self, v: int):
        self.scale_label.setText(f"Compute scale: {v}%")
        self.refresh_views()

    def on_arrow_step_changed(self, v: int):
        self.arrow_step_label.setText(f"Arrow step: {v} px")
        self.refresh_views()

    def on_arrow_scale_changed(self, v: int):
        s = v / 10.0
        self.arrow_scale_label.setText(f"Arrow length scale: {s:.1f}x")
        self.refresh_views()

    # ---- Rendering ----
    def show_frame(self, frame_index: int):
        if self.cap is None:
            return

        # Save previous displayed frame for "Previous -> Current"
        if self.last_frame_rgb is not None:
            self.prev_frame_rgb = self.last_frame_rgb.copy()
            self.prev_frame_index = self.current_frame

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, frame_bgr = self.cap.read()
        if not ret:
            return

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        self.last_frame_rgb = frame_rgb

        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        self.last_qimage = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)

        self.update_video_label()

        if self.frame_count > 0:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(frame_index)
            self.seek_slider.blockSignals(False)

        t = frame_index / self.fps if self.fps > 0 else 0.0
        self.info_label.setText(f"Time: {format_timecode(timedelta(seconds=t))} | Frame: {frame_index}")

        self.update_analysis_label()

    def update_video_label(self):
        if self.last_qimage is None:
            return
        pixmap = QPixmap.fromImage(self.last_qimage)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _pick_base_frame(self) -> tuple[np.ndarray | None, str]:
        pairing = self.pair_combo.currentText()

        if pairing.startswith("Reference"):
            if self.ref_frame_rgb is None:
                return None, "Set a reference frame first."
            return self.ref_frame_rgb, f"Reference frame: {self.ref_frame_index}"
        else:
            if self.prev_frame_rgb is None:
                return None, "No previous frame yet (scrub at least once)."
            return self.prev_frame_rgb, f"Previous frame: {self.prev_frame_index}"

    def update_analysis_label(self):
        if self.last_frame_rgb is None:
            self.analysis_label.setText("Analysis view (no frame)")
            self.analysis_label.setPixmap(QPixmap())
            return

        mode = self.mode_combo.currentText()
        if mode == "None":
            self.analysis_label.setText("Analysis view (mode: None)")
            self.analysis_label.setPixmap(QPixmap())
            return

        base_rgb, base_info = self._pick_base_frame()
        if base_rgb is None:
            self.analysis_label.setText(f"Analysis view ({base_info})")
            self.analysis_label.setPixmap(QPixmap())
            return

        gain = float(self.gain_slider.value())
        thresh = int(self.thresh_slider.value())
        heatmap = self.heatmap_cb.isChecked()
        overlay = self.overlay_cb.isChecked()
        alpha = self.alpha_slider.value() / 100.0

        compute_scale = self.scale_slider.value() / 100.0

        arrows = self.arrows_cb.isChecked()
        arrow_step = int(self.arrow_step_slider.value())
        arrow_scale = float(self.arrow_scale_slider.value()) / 10.0

        if mode == "Pixel Diff":
            out_rgb = compute_pixel_diff_view(
                frame_rgb=self.last_frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                threshold=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
            )
        else:
            out_rgb = compute_optical_flow_view(
                frame_rgb=self.last_frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                min_motion=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
                arrows=arrows,
                arrow_step=arrow_step,
                arrow_scale=arrow_scale,
                compute_scale=compute_scale,
            )

        h, w, ch = out_rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(out_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        self.analysis_label.setPixmap(
            pixmap.scaled(self.analysis_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

        # helpful tooltip
        self.analysis_label.setToolTip(
            f"{mode}\n{base_info}\nCurrent frame: {self.current_frame}"
        )

    def refresh_views(self):
        self.update_video_label()
        self.update_analysis_label()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh_views()


def main():
    app = QApplication(sys.argv)
    win = VideoAnalysisViewer()
    win.resize(1450, 780)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
