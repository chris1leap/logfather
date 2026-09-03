from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import math
import re
import os
from pathlib import Path
from typing import Iterable, Optional
import sys

from logfather.paths import bundle_root

import cv2
import numpy as np
import shutil
import subprocess

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSlider,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    import pytesseract
    from pytesseract import pytesseract as pytesseract_module
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None
    pytesseract_module = None

OCR_SYNC_FAST_SECONDS = 1
OCR_SYNC_FALLBACK_SECONDS = 3
OCR_SYNC_COARSE_STEP_SECONDS = 0.2


@dataclass(frozen=True)
class Roi:
    x: int
    y: int
    w: int
    h: int

    @classmethod
    def top_center(
        cls,
        frame_w: int,
        frame_h: int,
        *,
        width_ratio: float = 0.5,
        height_ratio: float = 0.085,
        y_offset_ratio: float = 0.013,
    ) -> "Roi":
        width_ratio = max(0.01, min(1.0, float(width_ratio)))
        height_ratio = max(0.01, min(1.0, float(height_ratio)))
        y_offset_ratio = max(0.0, min(0.9, float(y_offset_ratio)))

        w = max(1, int(frame_w * width_ratio))
        h = max(1, int(frame_h * height_ratio))
        x = max(0, int((frame_w - w) / 2))
        y = max(0, int(frame_h * y_offset_ratio))
        return cls(x=x, y=y, w=w, h=h)

    def crop(self, frame_bgr: np.ndarray) -> np.ndarray:
        h, w = frame_bgr.shape[:2]
        x1 = max(0, min(w - 1, self.x))
        y1 = max(0, min(h - 1, self.y))
        x2 = max(x1 + 1, min(w, x1 + self.w))
        y2 = max(y1 + 1, min(h, y1 + self.h))
        return frame_bgr[y1:y2, x1:x2]

    @classmethod
    def top_center_time(
        cls,
        frame_w: int,
        frame_h: int,
        *,
        width_ratio: float = 0.22,
        height_ratio: float = 0.06,
        y_offset_ratio: float = 0.013,
        x_offset_ratio: float = 0.0,
    ) -> "Roi":
        roi = cls.top_center(
            frame_w,
            frame_h,
            width_ratio=width_ratio,
            height_ratio=height_ratio,
            y_offset_ratio=y_offset_ratio,
        )
        if abs(x_offset_ratio) < 1e-6:
            return roi
        shift = int(frame_w * x_offset_ratio)
        return cls(x=roi.x + shift, y=roi.y, w=roi.w, h=roi.h)


class ScrubbableLabel(QLabel):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._scrub_callback = None

    def set_scrub_callback(self, cb):
        self._scrub_callback = cb

    def wheelEvent(self, event):
        if self._scrub_callback is None:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta > 0:
            self._scrub_callback(-1)
        elif delta < 0:
            self._scrub_callback(1)
        event.accept()


@dataclass(frozen=True)
class OcrConfig:
    scale: float = 4.0
    blur_ksize: int = 1
    threshold: str = "otsu"  # "otsu", "adaptive", "none"
    invert: bool = True
    auto_invert: bool = False
    psm: int = 7
    whitelist: str = "0123456789:"
    lang: str = "eng"


@dataclass(frozen=True)
class RoiSettings:
    width_ratio: float
    height_ratio: float
    y_offset_ratio: float
    x_offset_ratio: float


def load_roi_settings(settings_path: Path, key: str | None) -> RoiSettings | None:
    if not key:
        return None
    if not settings_path.exists():
        return None
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get("roi_by_key", {}).get(key)
    if not isinstance(entry, dict):
        return None
    try:
        return RoiSettings(
            width_ratio=float(entry.get("width_ratio", 0.22)),
            height_ratio=float(entry.get("height_ratio", 0.06)),
            y_offset_ratio=float(entry.get("y_offset_ratio", 0.013)),
            x_offset_ratio=float(entry.get("x_offset_ratio", 0.0)),
        )
    except Exception:
        return None


def save_roi_settings(settings_path: Path, key: str | None, settings: RoiSettings) -> None:
    if not key:
        return
    try:
        data = {}
        if settings_path.exists():
            data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    roi_by_key = data.get("roi_by_key")
    if not isinstance(roi_by_key, dict):
        roi_by_key = {}
        data["roi_by_key"] = roi_by_key
    roi_by_key[key] = {
        "width_ratio": settings.width_ratio,
        "height_ratio": settings.height_ratio,
        "y_offset_ratio": settings.y_offset_ratio,
        "x_offset_ratio": settings.x_offset_ratio,
    }
    try:
        settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _preprocess_for_ocr(roi_bgr: np.ndarray, cfg: OcrConfig) -> np.ndarray:
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    if cfg.scale and abs(cfg.scale - 1.0) > 1e-6:
        gray = cv2.resize(gray, None, fx=cfg.scale, fy=cfg.scale, interpolation=cv2.INTER_CUBIC)

    if cfg.blur_ksize and cfg.blur_ksize >= 3:
        k = cfg.blur_ksize if cfg.blur_ksize % 2 == 1 else cfg.blur_ksize + 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)

    if cfg.threshold == "adaptive":
        bw = cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            31,
            10,
        )
    elif cfg.threshold == "otsu":
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        bw = gray

    if cfg.auto_invert:
        if np.mean(bw) < 127:
            bw = cv2.bitwise_not(bw)

    if cfg.invert:
        bw = cv2.bitwise_not(bw)

    kernel = np.ones((2, 2), np.uint8)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, kernel)

    return bw


def _ensure_tesseract() -> None:
    if pytesseract is None:
        raise RuntimeError(
            "pytesseract is not available. Install pytesseract and the Tesseract OCR "
            "engine locally to enable time OCR."
        )
    module = pytesseract_module or pytesseract

    def _configure_tessdata_for(cmd_path: Path) -> None:
        tessdata_dir = cmd_path.parent / "tessdata"
        if tessdata_dir.exists():
            os.environ["TESSDATA_PREFIX"] = str(tessdata_dir)
        else:
            # Avoid leaving a stale override from another Tesseract install.
            os.environ.pop("TESSDATA_PREFIX", None)

    bundled_root = bundle_root()
    bundled_exe = bundled_root / "tesseract" / "tesseract.exe"
    if bundled_exe.exists():
        module.tesseract_cmd = str(bundled_exe)
        # Always override stale machine/user Tesseract installs when the
        # bundled runtime is present, otherwise OCR can resolve traineddata
        # from an unrelated system path.
        _configure_tessdata_for(bundled_exe)
        return
    default_paths = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    configured = getattr(module, "tesseract_cmd", "") or ""
    configured_path = Path(configured) if configured else None
    if configured_path and configured_path.exists():
        _configure_tessdata_for(configured_path)
        return
    for candidate in default_paths:
        if candidate.exists():
            module.tesseract_cmd = str(candidate)
            _configure_tessdata_for(candidate)
            return
    found = shutil.which("tesseract")
    if found:
        module.tesseract_cmd = found
        _configure_tessdata_for(Path(found))
        return

    ok, message = _probe_tesseract()
    if not ok:
        raise RuntimeError(message)


def _probe_tesseract() -> tuple[bool, str]:
    if pytesseract is None:
        return False, "pytesseract is not available"
    module = pytesseract_module or pytesseract
    cmd = getattr(module, "tesseract_cmd", "") or "tesseract"
    try:
        proc = subprocess.run([cmd, "--version"], capture_output=True, text=True)
    except FileNotFoundError:
        return False, f"Tesseract not found: {cmd}"
    except Exception as exc:
        return False, f"Failed to run {cmd}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if detail:
            return False, detail
        return False, f"Tesseract exited with code {proc.returncode}"
    first_line = (proc.stdout or "").splitlines()[:1]
    return True, first_line[0] if first_line else "OK"


def ocr_time_from_frame(
    frame_bgr: np.ndarray,
    *,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> str:
    _ensure_tesseract()
    if ocr_cfg is None:
        ocr_cfg = OcrConfig()

    h, w = frame_bgr.shape[:2]
    if roi is None:
        roi = Roi.top_center_time(w, h)

    roi_bgr = roi.crop(frame_bgr)
    prepared = _preprocess_for_ocr(roi_bgr, ocr_cfg)

    config = f"--oem 3 --psm {ocr_cfg.psm} -c tessedit_char_whitelist={ocr_cfg.whitelist}"
    text = pytesseract.image_to_string(prepared, config=config, lang=ocr_cfg.lang)
    return text.strip()


def _read_frame(
    video_path: Path | str,
    *,
    frame_index: Optional[int] = None,
    time_seconds: Optional[float] = None,
) -> tuple[np.ndarray, int, float]:
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Unable to open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if frame_index is None:
        if time_seconds is None:
            frame_index = 0
        else:
            frame_index = int(round(time_seconds * fps)) if fps > 0 else int(time_seconds * 25)

    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        raise RuntimeError(f"Unable to read frame {frame_index} from video: {video_path}")
    return frame, int(frame_index), float(fps)


def ocr_time_from_video(
    video_path: Path | str,
    *,
    frame_index: Optional[int] = None,
    time_seconds: Optional[float] = None,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> str:
    frame, _, _ = _read_frame(video_path, frame_index=frame_index, time_seconds=time_seconds)
    return ocr_time_from_frame(frame, roi=roi, ocr_cfg=ocr_cfg)


def ocr_time_from_video_samples(
    video_path: Path | str,
    *,
    frame_indices: Optional[Iterable[int]] = None,
    time_seconds_list: Optional[Iterable[float]] = None,
    roi: Optional[Roi] = None,
    ocr_cfg: Optional[OcrConfig] = None,
) -> list[str]:
    if frame_indices is None and time_seconds_list is None:
        frame_indices = [0]

    results: list[str] = []
    if frame_indices is not None:
        for idx in frame_indices:
            results.append(ocr_time_from_video(video_path, frame_index=idx, roi=roi, ocr_cfg=ocr_cfg))
    if time_seconds_list is not None:
        for t in time_seconds_list:
            results.append(ocr_time_from_video(video_path, time_seconds=t, roi=roi, ocr_cfg=ocr_cfg))
    return results


_TIME_RE = re.compile(r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})$")


def _normalize_ocr_text(text: str) -> str:
    cleaned = " ".join(text.split())
    return cleaned.strip()


def _combine_date_and_time(base_dt: datetime, time_text: str) -> datetime:
    hour, minute, second = (int(part) for part in time_text.split(":"))
    candidate = base_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if candidate < base_dt and (base_dt - candidate) > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _is_valid_time_text(text: str) -> bool:
    match = _TIME_RE.match(text)
    if not match:
        return False
    try:
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        second = int(match.group("second"))
    except ValueError:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59


class OcrVideoPlayer(QWidget):
    def __init__(
        self,
        *,
        settings_path: Path | None = None,
        settings_key: str | None = None,
        auto_analyze: bool = True,
        on_offset_approved=None,
    ):
        super().__init__()
        self.setWindowTitle("CCTV Time OCR")
        self._roi_settings_path = settings_path
        self._roi_settings_key = settings_key
        self._auto_analyze = bool(auto_analyze)
        self._on_offset_approved = on_offset_approved

        self.cap: cv2.VideoCapture | None = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False
        self.current_video_path: str | None = None
        self.filename_dt: datetime | None = None
        self.estimated_start_dt: datetime | None = None
        self.time_frame_offset = 1

        self.video_label = ScrubbableLabel("Open a video to start")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(640, 360)
        self.video_label.set_scrub_callback(self._scrub_by_frames)

        self.ocr_label = QLabel("OCR: (not running)")
        self.ocr_label.setAlignment(Qt.AlignCenter)
        self.ocr_enabled_checkbox = QCheckBox("Enable OCR")
        self.ocr_enabled_checkbox.setChecked(True)
        self.ocr_history = QListWidget()
        self.ocr_history.setMinimumWidth(260)
        self.ocr_history.setUniformItemSizes(True)
        self.last_ocr_text: str | None = None
        self.roi_preview = QLabel("ROI preview")
        self.roi_preview.setAlignment(Qt.AlignCenter)
        self.roi_preview.setMinimumSize(260, 80)

        self.time_label = QLabel("Time: 00:00:00.000")
        self.time_label.setAlignment(Qt.AlignCenter)
        self.status_label = QLabel("Frame: 0")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.tesseract_label = QLabel("Tesseract: (checking)")
        self.tesseract_label.setAlignment(Qt.AlignCenter)
        self.offset_label = QLabel("Offset: (not analyzed)")
        self.offset_label.setAlignment(Qt.AlignCenter)

        self.sync_btn = QPushButton("Sync Time")
        self.sync_btn.setEnabled(False)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)

        default_roi = RoiSettings(0.22, 0.06, 0.013, 0.0)
        saved_roi = None
        if self._roi_settings_path and self._roi_settings_key:
            saved_roi = load_roi_settings(self._roi_settings_path, self._roi_settings_key)
        roi = saved_roi or default_roi
        self.width_slider = self._make_ratio_slider(roi.width_ratio)
        self.height_slider = self._make_ratio_slider(roi.height_ratio)
        self.y_offset_slider = self._make_ratio_slider(roi.y_offset_ratio)
        self.x_offset_slider = self._make_ratio_slider(roi.x_offset_ratio, signed=True)
        self._roi_label = QLabel("")
        self._update_roi_label()

        left_layout = QVBoxLayout()
        left_layout.addWidget(self.video_label, 1)
        left_layout.addWidget(self.seek_slider)
        left_layout.addWidget(self.ocr_label)
        left_layout.addWidget(self.ocr_enabled_checkbox)
        left_layout.addWidget(self.tesseract_label)
        left_layout.addWidget(self.offset_label)
        left_layout.addWidget(self.time_label)
        left_layout.addWidget(self.status_label)
        left_layout.addWidget(QLabel("ROI width"))
        left_layout.addWidget(self.width_slider)
        left_layout.addWidget(QLabel("ROI height"))
        left_layout.addWidget(self.height_slider)
        left_layout.addWidget(QLabel("ROI Y offset"))
        left_layout.addWidget(self.y_offset_slider)
        left_layout.addWidget(QLabel("ROI X offset"))
        left_layout.addWidget(self.x_offset_slider)
        left_layout.addWidget(self._roi_label)
        left_layout.addWidget(self.sync_btn)

        root_layout = QHBoxLayout()
        root_layout.addLayout(left_layout, 1)
        right_layout = QVBoxLayout()
        right_layout.addWidget(self.roi_preview)
        right_layout.addWidget(self.ocr_history, 1)
        root_layout.addLayout(right_layout)
        self.setLayout(root_layout)

        self.sync_btn.clicked.connect(self._analyze_first_10s)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next_frame)
        self.ocr_available = pytesseract is not None
        if not self.ocr_available:
            self.ocr_label.setText("OCR: pytesseract not available")
        QTimer.singleShot(0, self._update_tesseract_status)
        self._closing = False

        self.width_slider.valueChanged.connect(self._on_roi_changed)
        self.height_slider.valueChanged.connect(self._on_roi_changed)
        self.y_offset_slider.valueChanged.connect(self._on_roi_changed)
        self.x_offset_slider.valueChanged.connect(self._on_roi_changed)
        self._load_roi_settings()
        self.ocr_enabled_checkbox.stateChanged.connect(self._on_ocr_toggle)

    def _open_video_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Video",
            "",
            "Video Files (*.mp4 *.avi *.mkv *.mov);;All Files (*)",
        )
        if path:
            self.open_video(path)

    def closeEvent(self, event):
        self._closing = True
        try:
            self.timer.stop()
        except Exception:
            pass
        self.playing = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        super().closeEvent(event)

    def open_video(self, path: str):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            self.video_label.setText("Unable to open video")
            self.sync_btn.setEnabled(False)
            self.current_video_path = None
            return
        self.current_video_path = path

        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.current_frame = 0
        self.current_time_seconds = 0.0
        self.filename_dt = None
        self.estimated_start_dt = None
        self.filename_dt = self._parse_filename_datetime()
        self.offset_label.setText("Offset: (not analyzed)")
        self.time_label.setText("Time: 00:00:00.000")
        self.ocr_history.clear()
        self.last_ocr_text = None
        self.ocr_enabled_checkbox.setChecked(True)
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))
        self.seek_slider.setValue(0)
        self.sync_btn.setEnabled(True)
        self.playing = False
        self.timer.stop()
        self._read_and_show(self.current_frame)
        if self._auto_analyze:
            self._analyze_first_10s()

    def _toggle_play_pause(self):
        if self.cap is None:
            return
        if self.playing:
            self.playing = False
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Play")
            self.timer.stop()
        else:
            self.playing = True
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Pause")
            interval_ms = int(1000 / self.fps) if self.fps > 0 else 40
            self.timer.start(interval_ms)

    def _next_frame(self):
        if self.cap is None:
            return
        self.current_frame += 1
        if self.frame_count and self.current_frame >= self.frame_count:
            self._toggle_play_pause()
            return
        self._read_and_show(self.current_frame)

    def _on_slider_moved(self, value: int):
        if self.cap is None:
            return
        self.playing = False
        if hasattr(self, "play_btn"):
            self.play_btn.setText("Play")
        self.timer.stop()
        self.current_frame = int(value)
        self._read_and_show(self.current_frame)

    def _read_and_show(self, frame_index: int):
        if self._closing:
            return
        if self.cap is None:
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
        ret, frame = self.cap.read()
        if not ret or frame is None:
            return
        if self.fps > 0:
            self.current_time_seconds = frame_index / self.fps
        else:
            pos_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
            self.current_time_seconds = pos_msec / 1000.0 if pos_msec and pos_msec > 0 else 0.0
        self._show_frame(frame)
        self._update_ocr(frame)
        self._update_status()
        self.seek_slider.blockSignals(True)
        self.seek_slider.setValue(frame_index)
        self.seek_slider.blockSignals(False)

    def _show_frame(self, frame_bgr: np.ndarray):
        if self._closing:
            return
        display_frame = frame_bgr.copy()
        roi = self._current_roi(display_frame.shape[1], display_frame.shape[0])
        cv2.rectangle(
            display_frame,
            (roi.x, roi.y),
            (roi.x + roi.w, roi.y + roi.h),
            (0, 255, 0),
            2,
        )
        overlay_text = self._current_actual_time_str()
        if overlay_text:
            text_size = cv2.getTextSize(
                overlay_text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1
            )[0]
            text_x = max(0, roi.x + (roi.w - text_size[0]) // 2)
            text_y = min(display_frame.shape[0] - 10, roi.y + roi.h + 20)
            cv2.putText(
                display_frame,
                overlay_text,
                (text_x, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (0, 0, 0),
                3,
                cv2.LINE_AA,
            )
            cv2.putText(
                display_frame,
                overlay_text,
                (text_x, text_y),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )
        frame_rgb = cv2.cvtColor(display_frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimg)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        roi_bgr = roi.crop(frame_bgr)
        roi_rgb = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB)
        rh, rw, rch = roi_rgb.shape
        roi_qimg = QImage(roi_rgb.data, rw, rh, rch * rw, QImage.Format_RGB888).copy()
        roi_pixmap = QPixmap.fromImage(roi_qimg)
        self.roi_preview.setPixmap(
            roi_pixmap.scaled(self.roi_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _update_ocr(self, frame_bgr: np.ndarray):
        if not self.ocr_available or not self.ocr_enabled_checkbox.isChecked():
            return
        try:
            roi = self._current_roi(frame_bgr.shape[1], frame_bgr.shape[0])
            raw_text = ocr_time_from_frame(frame_bgr, roi=roi)
        except Exception as exc:
            self.ocr_label.setText(f"OCR error: {exc}")
            self._update_tesseract_status()
            return
        text = _normalize_ocr_text(raw_text)
        valid = _is_valid_time_text(text)
        suffix = " (valid)" if valid else ""
        self.ocr_label.setText(f"OCR: {text or '(blank)'}{suffix}")
        if text != self.last_ocr_text:
            self._add_history_entry(
                f"{self.current_frame + 1}: {text or '(blank)'}",
                "valid" if valid else None,
            )
            self.last_ocr_text = text
            if self.ocr_history.count() > 500:
                self.ocr_history.takeItem(0)

    def _update_status(self):
        self.status_label.setText(f"Frame: {self.current_frame + 1}/{self.frame_count}")
        total_ms, hours, minutes, seconds, ms, actual_dt = self._display_time_parts()
        if actual_dt:
            self.time_label.setText(actual_dt.strftime("Time: %d/%m/%Y %H:%M:%S.") + f"{ms:03}")
        else:
            self.time_label.setText(f"Time: {hours:02}:{minutes:02}:{seconds:02}.{ms:03}")

    def _current_actual_time_str(self) -> str:
        total_ms, hours, minutes, seconds, ms, actual_dt = self._display_time_parts()
        if actual_dt:
            return actual_dt.strftime("%H:%M:%S.") + f"{ms:03}"
        return f"{hours:02}:{minutes:02}:{seconds:02}.{ms:03}"

    def _update_tesseract_status(self):
        if pytesseract is None:
            self.tesseract_label.setText("Tesseract: not available")
            return
        try:
            _ensure_tesseract()
        except Exception as exc:
            self.tesseract_label.setText(f"Tesseract: {exc}")
            return
        module = pytesseract_module or pytesseract
        configured = getattr(module, "tesseract_cmd", "") or ""
        path = Path(configured) if configured else None
        if path and path.exists():
            ok, msg = _probe_tesseract()
            if ok:
                self.tesseract_label.setText(f"Tesseract: {path}")
            else:
                self.tesseract_label.setText(f"Tesseract: {path} ({msg})")
        elif configured:
            resolved = shutil.which(configured)
            if resolved:
                self.tesseract_label.setText(f"Tesseract: {resolved}")
            else:
                self.tesseract_label.setText(f"Tesseract: {configured} (missing)")
        else:
            self.tesseract_label.setText("Tesseract: not configured")

    def _add_history_entry(self, text: str, status: str | None = None) -> None:
        item = QListWidgetItem(text)
        if status == "valid":
            item.setBackground(QColor("#2d6a2d"))
            item.setForeground(QColor("#ffffff"))
        elif status == "invalid":
            item.setBackground(QColor("#7a1f1f"))
            item.setForeground(QColor("#ffffff"))
        elif status == "outlier":
            item.setBackground(QColor("#8a6d1f"))
            item.setForeground(QColor("#ffffff"))
        self.ocr_history.addItem(item)
        self.ocr_history.scrollToBottom()

    def _make_ratio_slider(self, value: float, *, signed: bool = False) -> QSlider:
        slider = QSlider(Qt.Horizontal)
        slider.setRange(-50, 50) if signed else slider.setRange(0, 50)
        slider.setValue(int(round(value * 100)))
        slider.setSingleStep(1)
        slider.setPageStep(5)
        return slider

    def _slider_ratio(self, slider: QSlider) -> float:
        return float(slider.value()) / 100.0

    def _current_roi(self, frame_w: int, frame_h: int) -> Roi:
        return Roi.top_center_time(
            frame_w,
            frame_h,
            width_ratio=self._slider_ratio(self.width_slider),
            height_ratio=self._slider_ratio(self.height_slider),
            y_offset_ratio=self._slider_ratio(self.y_offset_slider),
            x_offset_ratio=self._slider_ratio(self.x_offset_slider),
        )

    def _update_roi_label(self):
        self._roi_label.setText(
            "ROI ratios: "
            f"w={self._slider_ratio(self.width_slider):.3f} "
            f"h={self._slider_ratio(self.height_slider):.3f} "
            f"y={self._slider_ratio(self.y_offset_slider):.3f} "
            f"x={self._slider_ratio(self.x_offset_slider):+.3f}"
        )

    def _on_roi_changed(self, _value: int):
        self._update_roi_label()
        self._save_roi_settings()
        if self.cap is not None:
            self._read_and_show(self.current_frame)

    def _on_ocr_toggle(self, _state: int):
        if self.cap is None:
            return
        if not self.ocr_enabled_checkbox.isChecked():
            self.ocr_label.setText("OCR: (disabled)")
            return
        self._read_and_show(self.current_frame)

    def _scrub_by_frames(self, delta_frames: int):
        if self.cap is None:
            return
        if self.playing:
            self.playing = False
            if hasattr(self, "play_btn"):
                self.play_btn.setText("Play")
            self.timer.stop()
        new_frame = self.current_frame + delta_frames
        if self.frame_count > 0:
            new_frame = max(0, min(self.frame_count - 1, new_frame))
        else:
            new_frame = max(0, new_frame)
        self.current_frame = new_frame
        self._read_and_show(self.current_frame)

    def _settings_path(self) -> Path:
        if self._roi_settings_path:
            return self._roi_settings_path
        return Path.cwd() / "time_ocr_settings.json"

    def _load_roi_settings(self) -> None:
        if self._roi_settings_path and self._roi_settings_key:
            settings = load_roi_settings(self._roi_settings_path, self._roi_settings_key)
            if settings:
                self._set_slider_ratio(self.width_slider, settings.width_ratio)
                self._set_slider_ratio(self.height_slider, settings.height_ratio)
                self._set_slider_ratio(self.y_offset_slider, settings.y_offset_ratio)
                self._set_slider_ratio(self.x_offset_slider, settings.x_offset_ratio)
                self._update_roi_label()
                return
        path = self._settings_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        self._set_slider_ratio(self.width_slider, data.get("width_ratio"))
        self._set_slider_ratio(self.height_slider, data.get("height_ratio"))
        self._set_slider_ratio(self.y_offset_slider, data.get("y_offset_ratio"))
        self._set_slider_ratio(self.x_offset_slider, data.get("x_offset_ratio"))
        self._update_roi_label()

    def _save_roi_settings(self) -> None:
        settings = RoiSettings(
            width_ratio=self._slider_ratio(self.width_slider),
            height_ratio=self._slider_ratio(self.height_slider),
            y_offset_ratio=self._slider_ratio(self.y_offset_slider),
            x_offset_ratio=self._slider_ratio(self.x_offset_slider),
        )
        if self._roi_settings_path and self._roi_settings_key:
            save_roi_settings(self._roi_settings_path, self._roi_settings_key, settings)
            return
        data = {
            "width_ratio": settings.width_ratio,
            "height_ratio": settings.height_ratio,
            "y_offset_ratio": settings.y_offset_ratio,
            "x_offset_ratio": settings.x_offset_ratio,
        }
        path = self._settings_path()
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _set_slider_ratio(self, slider: QSlider, value: object) -> None:
        if not isinstance(value, (int, float)):
            return
        slider.setValue(int(round(float(value) * 100)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.cap is not None:
            self._read_and_show(self.current_frame)

    def _analyze_first_10s(self):
        if self.cap is None:
            return
        if not self.ocr_enabled_checkbox.isChecked():
            self.offset_label.setText("Offset: OCR disabled")
            return
        if self.playing:
            self.playing = False
            self.play_btn.setText("Play")
            self.timer.stop()
        self.offset_label.setText("Offset: (analyzing...)")
        filename_dt = self._parse_filename_datetime()
        if filename_dt is None:
            self.offset_label.setText("Offset: filename time not found")
            return
        def _pick_best_start(samples: list[tuple[int, float, datetime, str]]) -> datetime | None:
            transition_result = self._estimate_start_from_transitions(samples)
            if transition_result is None:
                inferred = []
                for _frame_idx, video_t, ocr_dt, ocr_text in samples:
                    inferred_start = ocr_dt - timedelta(seconds=video_t)
                    inferred.append((inferred_start, ocr_text))
                inferred.sort(key=lambda item: item[0])
                if not inferred:
                    return None  # no usable OCR samples (e.g. clock unreadable)
                median_start = inferred[len(inferred) // 2][0]
                inliers = [
                    item for item in inferred
                    if abs((item[0] - median_start).total_seconds()) <= 2.0
                ]
                if not inliers:
                    return None
                outliers = [item for item in inferred if item not in inliers]
                if outliers:
                    for outlier_start, outlier_text in outliers:
                        delta = (outlier_start - median_start).total_seconds()
                        self._add_history_entry(
                            f"disregarded sample {outlier_text} (offset {delta:+.2f}s)",
                            "outlier",
                        )
                return inliers[len(inliers) // 2][0]
            best_start, median_start, outliers = transition_result
            if outliers:
                for outlier_start, outlier_text in outliers:
                    delta = (outlier_start - median_start).total_seconds()
                    self._add_history_entry(
                        f"disregarded transition {outlier_text} (offset {delta:+.2f}s)",
                        "outlier",
                    )
            return best_start

        fast_seconds = OCR_SYNC_FAST_SECONDS
        fallback_seconds = OCR_SYNC_FALLBACK_SECONDS
        roi = None
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ret, frame = self.cap.read()
        if ret and frame is not None:
            roi = self._current_roi(frame.shape[1], frame.shape[0])

        samples: list[tuple[int, float, datetime, str]] = []
        if roi is not None:
            samples = _find_second_boundary_samples_for_cap(
                self.cap,
                self.fps,
                self.frame_count,
                seconds=fast_seconds,
                base_dt=filename_dt,
                roi=roi,
                parent=self,
                progress_label=f"Scanning first {fast_seconds}s (coarse)...",
            )
        best_start = _pick_best_start(samples) if samples else None
        if best_start is None:
            samples = self._collect_ocr_samples(
                seconds=fast_seconds,
                progress_label=f"Analyzing first {fast_seconds}s...",
            )
            best_start = _pick_best_start(samples)
        if best_start is None and fallback_seconds > fast_seconds:
            if roi is not None:
                samples = _find_second_boundary_samples_for_cap(
                    self.cap,
                    self.fps,
                    self.frame_count,
                    seconds=fallback_seconds,
                    base_dt=filename_dt,
                    roi=roi,
                    parent=self,
                    progress_label=f"Scanning first {fallback_seconds}s (coarse)...",
                )
                best_start = _pick_best_start(samples) if samples else None
        if best_start is None and fallback_seconds > fast_seconds:
            samples = self._collect_ocr_samples(
                seconds=fallback_seconds,
                progress_label=f"Analyzing first {fallback_seconds}s...",
            )
            if not samples:
                self.offset_label.setText("Offset: no valid OCR samples")
                return
            best_start = _pick_best_start(samples)
        if best_start is None:
            self.offset_label.setText("Offset: no consistent OCR samples")
            return
        offset = best_start - filename_dt
        self.estimated_start_dt = best_start
        self.time_frame_offset, report = self._verify_frame_offset(best_start)
        self.offset_label.setText(
            f"Offset: {offset.total_seconds():+.2f}s vs filename"
        )
        self.ocr_enabled_checkbox.setChecked(False)
        if callable(self._on_offset_approved):
            def _notify():
                self._on_offset_approved(
                    best_start,
                    offset.total_seconds(),
                    int(self.time_frame_offset),
                )
            QTimer.singleShot(0, _notify)
        self.offset_label.setText(
            f"Offset applied: {offset.total_seconds():+.2f}s vs filename"
        )

    def _collect_ocr_samples(
        self,
        *,
        seconds: int,
        progress_label: str,
    ) -> list[tuple[int, float, datetime, str]]:
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0:
            return []
        roi = None
        max_frame_idx = int(math.ceil(seconds * self.fps))
        if max_frame_idx < 0:
            return []
        max_frame_idx = min(self.frame_count - 1, max_frame_idx)
        max_frames = max_frame_idx + 1
        if max_frames <= 0:
            return []
        step = 1
        filename_dt = self._parse_filename_datetime()
        if filename_dt is None:
            return []
        samples: list[tuple[int, float, datetime, str]] = []
        progress = QProgressDialog(progress_label, None, 0, max_frames, self)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        for frame_idx in range(0, max_frames, step):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            ret, frame = self.cap.read()
            if not ret or frame is None:
                continue
            self.current_frame = frame_idx
            self._show_frame(frame)
            self._update_status()
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(frame_idx)
            self.seek_slider.blockSignals(False)
            progress.setValue(frame_idx + 1)
            QApplication.processEvents()
            if roi is None:
                roi = self._current_roi(frame.shape[1], frame.shape[0])
            try:
                raw_text = ocr_time_from_frame(frame, roi=roi)
            except Exception:
                continue
            text = _normalize_ocr_text(raw_text)
            if not _is_valid_time_text(text):
                self._add_history_entry(
                    f"{frame_idx + 1}: {text or '(blank)'} (invalid)",
                    "invalid",
                )
                continue
            ocr_dt = self._combine_date_and_time(filename_dt, text)
            if self.fps > 0:
                video_t = frame_idx / self.fps
            else:
                pos_msec = self.cap.get(cv2.CAP_PROP_POS_MSEC)
                video_t = pos_msec / 1000.0 if pos_msec and pos_msec > 0 else 0.0
            samples.append((frame_idx, video_t, ocr_dt, text))
            self._add_history_entry(f"{frame_idx + 1}: {text} (sample)", "valid")
        progress.setValue(max_frames)
        progress.close()
        return samples

    def _parse_filename_datetime(self) -> datetime | None:
        if self.filename_dt is not None:
            return self.filename_dt
        if self.current_video_path:
            name = Path(self.current_video_path).name
        else:
            return None
        match = re.search(r"(\d{14})", name)
        if not match:
            return None
        self.filename_dt = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        return self.filename_dt

    def _combine_date_and_time(self, base_dt: datetime, time_text: str) -> datetime:
        hour, minute, second = (int(part) for part in time_text.split(":"))
        candidate = base_dt.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate < base_dt and (base_dt - candidate) > timedelta(hours=12):
            candidate += timedelta(days=1)
        return candidate

    def _offset_seconds(self) -> float:
        if self.fps > 0:
            return self.time_frame_offset / self.fps
        return 0.0

    def _display_time_seconds(self) -> float:
        return self.current_time_seconds + self._offset_seconds()

    def _display_time_parts(self) -> tuple[int, int, int, int, int, datetime | None]:
        display_seconds = self._display_time_seconds()
        if self.estimated_start_dt:
            actual_dt = self.estimated_start_dt + timedelta(seconds=display_seconds)
            hours = actual_dt.hour
            minutes = actual_dt.minute
            seconds = actual_dt.second
            ms = int(actual_dt.microsecond / 1000)
            total_ms = int(display_seconds * 1000)
        else:
            total_ms = int(display_seconds * 1000)
            if total_ms < 0:
                total_ms = 0
            hours = total_ms // 3_600_000
            rem = total_ms % 3_600_000
            minutes = rem // 60_000
            rem = rem % 60_000
            seconds = rem // 1000
            ms = rem % 1000
            actual_dt = None
        return total_ms, hours, minutes, seconds, ms, actual_dt


    def _time_text_to_seconds(self, time_text: str) -> int | None:
        try:
            hour, minute, second = (int(part) for part in time_text.split(":"))
        except ValueError:
            return None
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return hour * 3600 + minute * 60 + second

    def _estimate_start_from_transitions(
        self,
        samples: list[tuple[int, float, datetime, str]],
    ) -> tuple[datetime, datetime, list[tuple[datetime, str]]] | None:
        if len(samples) < 2:
            return None
        transitions = []
        for idx in range(1, len(samples)):
            prev_frame, prev_t, prev_dt, prev_text = samples[idx - 1]
            curr_frame, curr_t, curr_dt, curr_text = samples[idx]
            if prev_text == curr_text:
                continue
            prev_secs = self._time_text_to_seconds(prev_text)
            curr_secs = self._time_text_to_seconds(curr_text)
            if prev_secs is None or curr_secs is None:
                continue
            if curr_secs != (prev_secs + 1) % 86400:
                continue
            boundary_video_t = curr_t
            boundary_ocr_dt = curr_dt
            inferred_start = boundary_ocr_dt - timedelta(seconds=boundary_video_t)
            transitions.append((inferred_start, curr_text))
        if not transitions:
            return None
        transitions.sort(key=lambda item: item[0])
        median_start = transitions[len(transitions) // 2][0]
        inliers = [
            item for item in transitions
            if abs((item[0] - median_start).total_seconds()) <= 1.0
        ]
        if not inliers:
            return None
        outliers = [item for item in transitions if item not in inliers]
        best_start = inliers[len(inliers) // 2][0]
        return best_start, median_start, outliers

    def _verify_frame_offset(
        self,
        estimated_start: datetime,
    ) -> tuple[int, list[tuple[str, str]]]:
        if self.cap is None or self.fps <= 0 or self.frame_count <= 0:
            return 0, [("Unable to verify frame offset (no video/fps).", "info")]
        mid_frame = self.frame_count // 2
        mid_dt = estimated_start + timedelta(seconds=mid_frame / self.fps)
        target_second = mid_dt.replace(microsecond=0)
        target_seconds = (target_second - estimated_start).total_seconds()
        candidates = [-2, -1, 0, 1, 2]
        best_offset = 0
        best_score = -1
        lines: list[tuple[str, str]] = [
            (
                f"Verifying around mid-frame {mid_frame} "
                f"({target_second.strftime('%H:%M:%S')})",
                "info",
            ),
        ]

        for offset in candidates:
            boundary_frame = int(math.ceil(target_seconds * self.fps - offset))
            frames = [
                max(0, boundary_frame - 1),
                max(0, min(self.frame_count - 1, boundary_frame)),
                max(0, min(self.frame_count - 1, boundary_frame + 1)),
            ]
            score = 0
            lines.append((f"Offset {offset:+d} frames:", "info"))
            for frame_idx in frames:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
                ret, frame = self.cap.read()
                if not ret or frame is None:
                    lines.append((f"  frame {frame_idx + 1}: read failed", "miss"))
                    continue
                roi = self._current_roi(frame.shape[1], frame.shape[0])
                try:
                    raw_text = ocr_time_from_frame(frame, roi=roi)
                except Exception as exc:
                    lines.append((f"  frame {frame_idx + 1}: OCR error {exc}", "miss"))
                    continue
                ocr_text = _normalize_ocr_text(raw_text)
                calc_dt = estimated_start + timedelta(
                    seconds=(frame_idx + offset) / self.fps
                )
                calc_text = calc_dt.strftime("%H:%M:%S")
                matched = ocr_text == calc_text
                if matched:
                    score += 1
                lines.append(
                    (
                        f"  frame {frame_idx + 1}: OCR={ocr_text or '(blank)'} "
                        f"calc={calc_text} {'OK' if matched else 'MISS'}",
                        "ok" if matched else "miss",
                    )
                )
            lines.append(("", "info"))
            if score > best_score:
                best_score = score
                best_offset = offset

        lines.append((f"Chosen offset: {best_offset:+d} frame(s)", "info"))
        return best_offset, lines

    def _show_verification_dialog(
        self,
        report: list[tuple[str, str]],
    ) -> bool:
        return show_verification_dialog(self, report)


@dataclass(frozen=True)
class OcrOffsetResult:
    video_start_dt: datetime
    offset_seconds: float
    frame_offset: int
    report: list[tuple[str, str]]


def parse_filename_datetime(video_path: str | Path) -> datetime | None:
    name = Path(video_path).name
    match = re.search(r"(\d{14})", name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d%H%M%S")


def show_verification_dialog(parent: QWidget, report: list[tuple[str, str]]) -> bool:
    dlg = QDialog(parent)
    dlg.setWindowTitle("Offset verification")
    dlg.resize(720, 520)

    browser = QTextBrowser()
    browser.setOpenExternalLinks(False)
    browser.setReadOnly(True)
    html_lines = []
    for text, status in report:
        if not text:
            html_lines.append("<div style='height:6px'></div>")
            continue
        if status == "ok":
            color = "#1f6f1f"
        elif status == "miss":
            color = "#7a1f1f"
        else:
            color = "#202020"
        safe = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html_lines.append(
            f"<div style='background:{color}; padding:4px 6px; "
            f"margin:2px 0; color:#ffffff; font-family:monospace;'>{safe}</div>"
        )
    browser.setHtml("".join(html_lines))

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.button(QDialogButtonBox.Ok).setText("Approve offset")
    buttons.button(QDialogButtonBox.Cancel).setText("Reject")
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)

    layout = QVBoxLayout()
    layout.addWidget(browser, 1)
    layout.addWidget(buttons)
    dlg.setLayout(layout)
    return dlg.exec() == QDialog.Accepted


def analyze_video_offset(
    video_path: str | Path,
    *,
    settings_path: Path | None = None,
    settings_key: str | None = None,
    parent: QWidget | None = None,
) -> OcrOffsetResult | None:
    _ensure_tesseract()
    base_dt = parse_filename_datetime(video_path)
    if base_dt is None:
        return None
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return None
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0 or frame_count <= 0:
        cap.release()
        return None

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    ret, frame = cap.read()
    if not ret or frame is None:
        cap.release()
        return None

    roi_settings = None
    if settings_path and settings_key:
        roi_settings = load_roi_settings(settings_path, settings_key)
    if roi_settings is None:
        roi_settings = RoiSettings(0.22, 0.06, 0.013, 0.0)
    roi = Roi.top_center_time(
        frame.shape[1],
        frame.shape[0],
        width_ratio=roi_settings.width_ratio,
        height_ratio=roi_settings.height_ratio,
        y_offset_ratio=roi_settings.y_offset_ratio,
        x_offset_ratio=roi_settings.x_offset_ratio,
    )
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    fast_seconds = OCR_SYNC_FAST_SECONDS
    fallback_seconds = OCR_SYNC_FALLBACK_SECONDS
    samples = _find_second_boundary_samples_for_cap(
        cap,
        fps,
        frame_count,
        seconds=fast_seconds,
        base_dt=base_dt,
        roi=roi,
        parent=parent,
        progress_label=f"Scanning first {fast_seconds}s (coarse)...",
    )
    best_start = _estimate_start_from_samples(samples, base_dt)
    if best_start is None:
        samples = _collect_ocr_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fast_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Analyzing first {fast_seconds}s...",
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
    if best_start is None and fallback_seconds > fast_seconds:
        samples = _find_second_boundary_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fallback_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Scanning first {fallback_seconds}s (coarse)...",
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
    if best_start is None and fallback_seconds > fast_seconds:
        samples = _collect_ocr_samples_for_cap(
            cap,
            fps,
            frame_count,
            seconds=fallback_seconds,
            base_dt=base_dt,
            roi=roi,
            parent=parent,
            progress_label=f"Analyzing first {fallback_seconds}s...",
        )
        best_start = _estimate_start_from_samples(samples, base_dt)
    if best_start is None:
        cap.release()
        return None

    offset_seconds = (best_start - base_dt).total_seconds()
    frame_offset, report = _verify_frame_offset_for_cap(
        cap,
        fps,
        frame_count,
        best_start,
        roi,
    )
    cap.release()
    result = OcrOffsetResult(
        video_start_dt=best_start,
        offset_seconds=offset_seconds,
        frame_offset=frame_offset,
        report=report,
    )
    return result


def _collect_ocr_samples_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    *,
    seconds: int,
    base_dt: datetime,
    roi: Roi,
    parent: QWidget | None,
    progress_label: str,
) -> list[tuple[int, float, datetime, str]]:
    if frame_count <= 0 or fps <= 0:
        return []
    max_frame_idx = int(math.ceil(seconds * fps))
    if max_frame_idx < 0:
        return []
    max_frame_idx = min(frame_count - 1, max_frame_idx)
    max_frames = max_frame_idx + 1
    if max_frames <= 0:
        return []
    samples: list[tuple[int, float, datetime, str]] = []
    progress = None
    if parent is not None:
        progress = QProgressDialog(progress_label, None, 0, max_frames, parent)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
    for frame_idx in range(0, max_frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
        ret, frame = cap.read()
        if not ret or frame is None:
            continue
        try:
            raw_text = ocr_time_from_frame(frame, roi=roi)
        except Exception:
            continue
        text = _normalize_ocr_text(raw_text)
        if not _is_valid_time_text(text):
            continue
        ocr_dt = _combine_date_and_time(base_dt, text)
        video_t = frame_idx / fps
        samples.append((frame_idx, video_t, ocr_dt, text))
        if progress:
            progress.setValue(frame_idx + 1)
            QApplication.processEvents()
    if progress:
        progress.setValue(max_frames)
        progress.close()
    return samples


def _ocr_sample_for_frame(
    cap: cv2.VideoCapture,
    frame_idx: int,
    fps: float,
    base_dt: datetime,
    roi: Roi,
) -> tuple[int, float, datetime, str] | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
    ret, frame = cap.read()
    if not ret or frame is None:
        return None
    try:
        raw_text = ocr_time_from_frame(frame, roi=roi)
    except Exception:
        return None
    text = _normalize_ocr_text(raw_text)
    if not _is_valid_time_text(text):
        return None
    ocr_dt = _combine_date_and_time(base_dt, text)
    video_t = frame_idx / fps
    return frame_idx, video_t, ocr_dt, text


def _find_second_boundary_samples_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    *,
    seconds: int,
    base_dt: datetime,
    roi: Roi,
    parent: QWidget | None,
    progress_label: str,
) -> list[tuple[int, float, datetime, str]]:
    if frame_count <= 0 or fps <= 0:
        return []
    max_frame_idx = int(math.ceil(seconds * fps))
    if max_frame_idx < 0:
        return []
    max_frame_idx = min(frame_count - 1, max_frame_idx)
    if max_frame_idx <= 0:
        return []

    step = max(1, int(round(fps * OCR_SYNC_COARSE_STEP_SECONDS)))
    frame_indices = list(range(0, max_frame_idx + 1, step))
    if frame_indices[-1] != max_frame_idx:
        frame_indices.append(max_frame_idx)

    progress = None
    if parent is not None:
        progress = QProgressDialog(progress_label, None, 0, len(frame_indices), parent)
        progress.setWindowTitle("OCR Analysis")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

    prev: tuple[int, float, datetime, str] | None = None
    for idx, frame_idx in enumerate(frame_indices):
        sample = _ocr_sample_for_frame(cap, frame_idx, fps, base_dt, roi)
        if progress:
            progress.setValue(idx + 1)
            QApplication.processEvents()
        if sample is None:
            continue
        if prev is not None:
            prev_text = prev[3]
            curr_text = sample[3]
            if prev_text != curr_text:
                prev_secs = _time_text_to_seconds(prev_text)
                curr_secs = _time_text_to_seconds(curr_text)
                if prev_secs is not None and curr_secs is not None:
                    if curr_secs == (prev_secs + 1) % 86400:
                        start = min(prev[0], sample[0])
                        end = max(prev[0], sample[0])
                        boundary_sample = None
                        for frame_scan in range(start, end + 1):
                            scanned = _ocr_sample_for_frame(
                                cap,
                                frame_scan,
                                fps,
                                base_dt,
                                roi,
                            )
                            if scanned and scanned[3] == curr_text:
                                boundary_sample = scanned
                                break
                        if boundary_sample is None:
                            boundary_sample = sample
                        if progress:
                            progress.setValue(len(frame_indices))
                            progress.close()
                        return [prev, boundary_sample]
        prev = sample

    if progress:
        progress.setValue(len(frame_indices))
        progress.close()
    return []


def _estimate_start_from_samples(
    samples: list[tuple[int, float, datetime, str]],
    base_dt: datetime,
) -> datetime | None:
    transition = _estimate_start_from_transitions(samples)
    if transition is not None:
        return transition[0]
    inferred = []
    for frame_idx, video_t, ocr_dt, ocr_text in samples:
        inferred.append((ocr_dt - timedelta(seconds=video_t), ocr_text))
    inferred.sort(key=lambda item: item[0])
    median_start = inferred[len(inferred) // 2][0]
    inliers = [
        item for item in inferred
        if abs((item[0] - median_start).total_seconds()) <= 2.0
    ]
    if not inliers:
        return None
    return inliers[len(inliers) // 2][0]


def _time_text_to_seconds(time_text: str) -> int | None:
    try:
        hour, minute, second = (int(part) for part in time_text.split(":"))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    return hour * 3600 + minute * 60 + second


def _estimate_start_from_transitions(
    samples: list[tuple[int, float, datetime, str]],
) -> tuple[datetime, datetime, list[tuple[datetime, str]]] | None:
    if len(samples) < 2:
        return None
    transitions = []
    for idx in range(1, len(samples)):
        _prev_frame, prev_t, prev_dt, prev_text = samples[idx - 1]
        _curr_frame, curr_t, curr_dt, curr_text = samples[idx]
        if prev_text == curr_text:
            continue
        prev_secs = _time_text_to_seconds(prev_text)
        curr_secs = _time_text_to_seconds(curr_text)
        if prev_secs is None or curr_secs is None:
            continue
        if curr_secs != (prev_secs + 1) % 86400:
            continue
        boundary_video_t = curr_t
        boundary_ocr_dt = curr_dt
        inferred_start = boundary_ocr_dt - timedelta(seconds=boundary_video_t)
        transitions.append((inferred_start, curr_text))
    if not transitions:
        return None
    transitions.sort(key=lambda item: item[0])
    median_start = transitions[len(transitions) // 2][0]
    inliers = [
        item for item in transitions
        if abs((item[0] - median_start).total_seconds()) <= 1.0
    ]
    if not inliers:
        return None
    outliers = [item for item in transitions if item not in inliers]
    best_start = inliers[len(inliers) // 2][0]
    return best_start, median_start, outliers


def _verify_frame_offset_for_cap(
    cap: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    estimated_start: datetime,
    roi: Roi,
) -> tuple[int, list[tuple[str, str]]]:
    if fps <= 0 or frame_count <= 0:
        return 0, [("Unable to verify frame offset (no video/fps).", "info")]
    mid_frame = frame_count // 2
    mid_dt = estimated_start + timedelta(seconds=mid_frame / fps)
    target_second = mid_dt.replace(microsecond=0)
    target_seconds = (target_second - estimated_start).total_seconds()
    candidates = [-2, -1, 0, 1, 2]
    best_offset = 0
    best_score = -1
    lines: list[tuple[str, str]] = [
        (
            f"Verifying around mid-frame {mid_frame} "
            f"({target_second.strftime('%H:%M:%S')})",
            "info",
        ),
    ]
    for offset in candidates:
        boundary_frame = int(math.ceil(target_seconds * fps - offset))
        frames = [
            max(0, boundary_frame - 1),
            max(0, min(frame_count - 1, boundary_frame)),
            max(0, min(frame_count - 1, boundary_frame + 1)),
        ]
        score = 0
        lines.append((f"Offset {offset:+d} frames:", "info"))
        for frame_idx in frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
            ret, frame = cap.read()
            if not ret or frame is None:
                lines.append((f"  frame {frame_idx + 1}: read failed", "miss"))
                continue
            try:
                raw_text = ocr_time_from_frame(frame, roi=roi)
            except Exception as exc:
                lines.append((f"  frame {frame_idx + 1}: OCR error {exc}", "miss"))
                continue
            ocr_text = _normalize_ocr_text(raw_text)
            calc_dt = estimated_start + timedelta(seconds=(frame_idx + offset) / fps)
            calc_text = calc_dt.strftime("%H:%M:%S")
            matched = ocr_text == calc_text
            if matched:
                score += 1
            lines.append(
                (
                    f"  frame {frame_idx + 1}: OCR={ocr_text or '(blank)'} "
                    f"calc={calc_text} {'OK' if matched else 'MISS'}",
                    "ok" if matched else "miss",
                )
            )
        lines.append(("", "info"))
        if score > best_score:
            best_score = score
            best_offset = offset
    lines.append((f"Chosen offset: {best_offset:+d} frame(s)", "info"))
    return best_offset, lines


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="CCTV Time OCR")
    parser.add_argument("--video", help="Path to video file")
    parser.add_argument("--gui", action="store_true", help="Open the GUI player")
    args = parser.parse_args()

    if args.gui or not args.video:
        app = QApplication([])
        win = OcrVideoPlayer()
        win.resize(900, 600)
        win.show()
        if args.video:
            win.open_video(args.video)
        raise SystemExit(app.exec())

    frame, _, _ = _read_frame(args.video, frame_index=0, time_seconds=None)
    text = ocr_time_from_frame(frame)
    print(text)
