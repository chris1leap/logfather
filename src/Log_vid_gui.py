import sys
import os
import csv
import subprocess
import shutil
import hashlib
import argparse
import time
import math
import json
import re
import tempfile
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from datetime import timedelta, datetime, timezone
from typing import Callable
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

from settings_store import Settings, DEFAULT_SETTINGS_PATH, CustomFilterPreset, FilterPreset
from elastic_loader import fetch_logs_for_range
from elastic_errors import ElasticFetchError

import cv2
from PySide6.QtCore import Qt, QTimer, Signal, QEvent, QMetaObject, Slot, QRect, QPoint, QPointF, Q_ARG, QVariantAnimation, QEasingCurve, QAbstractListModel, QModelIndex
from PySide6.QtGui import QImage, QColor, QPainter, QPen, QBrush, QPalette, QFont, QTransform, QPolygonF, QPixmap, QFontDatabase
import numpy as np
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QFileDialog, QMessageBox,
    QSlider, QSizePolicy, QListView, QAbstractItemView,
    QCheckBox, QScrollArea, QProgressDialog, QTabWidget,
    QLineEdit, QComboBox, QInputDialog, QMenu, QColorDialog,
    QToolButton, QButtonGroup, QStyleOptionSlider, QStyle, QLCDNumber
)

from time_ocr import analyze_video_offset, OcrVideoPlayer, parse_filename_datetime

SKIP_INITIAL_FRAME_RENDER = False
from settings_dialog import SettingsPanel, SystemLayoutPanel, ReadmePanel
from app_version import format_version_label, format_version_suffix


# -------- CONFIG FOR CSV→LOG EVENTS --------

TIME_COLUMN = "@timestamp_ros"
TEXT_COLUMNS = ["source", "state_name", "message"]

SOURCE_COLUMN = "source"
STATE_COLUMN = "state_name"
MESSAGE_COLUMN = "message"

# Example: "16 Nov, 2025 @ 13:17:37.529"
TIMESTAMP_FORMAT = "%d %b, %Y @ %H:%M:%S.%f"

# How long each log entry is considered "active" (seconds)
CSV_EVENT_DURATION_SECONDS = 1.0
TARGET_QUEUE_MESSAGE = "adding new target to queue"
PPM_ROLLING_WINDOW_SECONDS = 60.0
CACHE_META_SUFFIX = ".meta.json"
CACHE_MAX_BYTES = 30 * 1024 * 1024 * 1024
CACHE_MAX_AGE_DAYS = 30
if ZoneInfo is not None:
    try:
        LOCAL_TIMEZONE = ZoneInfo("Europe/London")
    except Exception:
        LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
else:
    LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc


# -------- LOG EVENT STRUCTURES --------

class LogEvent:
    def __init__(self, index, start, end, text):
        self.index = index
        self.start = start  # timedelta (relative)
        self.end = end      # timedelta (relative)
        self.text = text


def format_timecode(td: timedelta) -> str:
    """Format timedelta as HH:MM:SS,mmm (SRT-style timecode)."""
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


# Forward jumps up to this many frames are decoded via grab() instead of a
# CAP_PROP_POS_FRAMES seek: a seek on H.264 jumps to the previous keyframe and
# decodes forward, which usually costs more than grabbing a handful of frames.
MAX_GRAB_SKIP_FRAMES = 15


def _position_capture_sequential(cap, in_sequence: bool, next_frame: int, target_frame: int) -> bool:
    """Try to reach target_frame without seeking.

    Returns True if cap's next read() will deliver target_frame (already there,
    or reached by grabbing a few frames forward). Returns False if the caller
    must seek. `in_sequence` says whether next_frame is trustworthy for cap.
    """
    if not in_sequence:
        return False
    delta = target_frame - next_frame
    if delta == 0:
        return True
    if 0 < delta <= MAX_GRAB_SKIP_FRAMES:
        for _ in range(delta):
            if not cap.grab():
                return False
        return True
    return False


def _to_local_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)


def _format_display_timestamp(dt: datetime) -> str:
    local_dt = _to_local_naive(dt) or dt
    return local_dt.strftime("%H:%M:%S.%f")[:-3]


def _resolve_asset_path(filename: str) -> str | None:
    try:
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(Path(sys._MEIPASS) / filename)
        candidates.append(Path(__file__).resolve().parent.parent / "assets" / filename)
        candidates.append(Path(__file__).resolve().parent / filename)
        candidates.append(Path(sys.executable).resolve().parent / filename)
        for path in candidates:
            if path.exists():
                return str(path)
    except Exception:
        return None
    return None


def _load_placeholder_image() -> QImage | None:
    image_path = _resolve_asset_path("Logfather Argus II.jpg")
    if not image_path:
        return None
    img = QImage(image_path)
    if img.isNull():
        return None
    return img


def get_log_text_at_time(events, t: float, offset_seconds: float) -> str:
    """
    Look up the log text that should be shown at video time t (seconds),
    taking into account a time offset between log and video.
    """
    t_td = timedelta(seconds=t) - timedelta(seconds=offset_seconds)
    for ev in events:
        if ev.start <= t_td <= ev.end:
            return ev.text
    return ""


# -------- CSV → EVENTS --------

def parse_csv_timestamp(ts_str: str) -> datetime:
    ts_str = ts_str.strip()
    return datetime.strptime(ts_str, TIMESTAMP_FORMAT)


def build_log_text_from_row(row: dict) -> str:
    parts = []
    for col in TEXT_COLUMNS:
        value = row.get(col, "")
        if value is None:
            continue
        value = str(value).strip()
        if value and value != "-":
            parts.append(value)
    return " | ".join(parts)


def load_csv_as_events_and_filters(path: Path):
    """
    Returns:
      - events: list[LogEvent] with relative times
      - display_rows: list[str] for the log panel
      - source_keys: list[str] for each event
      - state_keys: list[str] for each event
      - message_keys: list[str] for each event
    """
    rows = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row.get(TIME_COLUMN, "").strip()
            if not ts_str:
                continue
            try:
                dt = parse_csv_timestamp(ts_str)
            except Exception:
                continue

            text = build_log_text_from_row(row)
            if not text:
                continue

            source_key = str(row.get(SOURCE_COLUMN, "")).strip()
            state_key = str(row.get(STATE_COLUMN, "")).strip()
            message_key = str(row.get(MESSAGE_COLUMN, "")).strip()

            rows.append((dt, text, source_key, state_key, message_key))

    if not rows:
        raise ValueError("No valid rows with timestamps and text found in CSV")

    return build_events_from_rows(rows)


def build_events_from_rows(rows: list[tuple]):
    print(f"[viewer] build_events_from_rows start ({len(rows)} rows)", flush=True)
    if not rows:
        print("[viewer] build_events_from_rows early exit (no rows)", flush=True)
        return [], [], [], [], [], None
    rows.sort(key=lambda x: x[0])
    t0 = rows[0][0]

    events = []
    display_rows = []
    source_keys = []
    state_keys = []
    message_keys = []

    for i, row in enumerate(rows, start=1):
        if len(row) == 5:
            dt, text, source_key, state_key, message_key = row
        elif len(row) == 4:
            dt, text, source_key, message_key = row
            state_key = ""
        else:
            continue
        start = dt - t0
        end = start + timedelta(seconds=CSV_EVENT_DURATION_SECONDS)
        events.append(LogEvent(i, start, end, text))
        display_rows.append(f"{_format_display_timestamp(dt)}  |  {text}")
        source_keys.append(source_key)
        state_keys.append(state_key)
        message_keys.append(message_key)
    # Tile each event's end to its neighbour's start so there are no gaps.
    for i in range(len(events) - 1):
        events[i].end = events[i + 1].start
    print("[viewer] build_events_from_rows done", flush=True)
    return events, display_rows, source_keys, state_keys, message_keys, t0


def _distance_to_segment(px, py, x1, y1, x2, y2) -> float:
    vx = x2 - x1
    vy = y2 - y1
    wx = px - x1
    wy = py - y1
    denom = vx * vx + vy * vy
    if denom <= 0.0:
        return math.hypot(px - x1, py - y1)
    t = (wx * vx + wy * vy) / denom
    t = max(0.0, min(1.0, t))
    proj_x = x1 + t * vx
    proj_y = y1 + t * vy
    return math.hypot(px - proj_x, py - proj_y)


def _dist(a: QPointF, b: QPointF) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())


# -------- Frame analysis helpers (diff + optical flow) --------

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
    max_arrows: int | None = None,
    min_magnitude: float | None = None,
) -> np.ndarray:
    out = canvas_rgb.copy()
    h, w = flow.shape[:2]

    count = 0
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dx, dy = flow[y, x]
            if min_magnitude is not None and (dx * dx + dy * dy) <= (min_magnitude * min_magnitude):
                continue
            x2 = int(round(x + dx * scale))
            y2 = int(round(y + dy * scale))
            x2 = max(0, min(w - 1, x2))
            y2 = max(0, min(h - 1, y2))

            cv2.arrowedLine(out, (x, y), (x2, y2), (255, 255, 255), 1, tipLength=0.35)

            count += 1
            if max_arrows is not None and max_arrows > 0 and count >= max_arrows:
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
    arrow_min_mag: float | None = None,
) -> np.ndarray:
    base_gray = cv2.cvtColor(base_rgb, cv2.COLOR_RGB2GRAY)
    frame_gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

    base_small = _resize_for_compute(base_gray, compute_scale)
    frame_small = _resize_for_compute(frame_gray, compute_scale)

    flow = cv2.calcOpticalFlowFarneback(
        base_small,
        frame_small,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )

    mag, _ang = cv2.cartToPolar(flow[..., 0], flow[..., 1], angleInDegrees=False)
    m = mag.astype(np.float32) * float(gain)
    m = np.clip(m, 0.0, 255.0).astype(np.uint8)

    if min_motion > 0:
        _, m = cv2.threshold(m, min_motion, 255, cv2.THRESH_TOZERO)

    if heatmap:
        vis = cv2.applyColorMap(m, cv2.COLORMAP_TURBO)  # BGR
        vis = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
    else:
        vis = cv2.cvtColor(m, cv2.COLOR_GRAY2RGB)

    H, W = frame_rgb.shape[:2]
    if compute_scale < 0.999:
        vis = _upscale_to(vis, (H, W))
        flow_up = cv2.resize(flow, (W, H), interpolation=cv2.INTER_NEAREST).astype(np.float32)
        flow_up[..., 0] *= (1.0 / compute_scale)
        flow_up[..., 1] *= (1.0 / compute_scale)
    else:
        flow_up = flow

    if arrows:
        vis = draw_flow_arrows(
            vis,
            flow_up,
            step=arrow_step,
            scale=arrow_scale,
            max_arrows=None,
            min_magnitude=arrow_min_mag,
        )

    if overlay:
        return cv2.addWeighted(frame_rgb, 1.0 - alpha, vis, alpha, 0.0)

    return vis

# -------- Custom video label to handle scroll wheel scrubbing --------

class ScrubbableLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scrub_callback = None

    def set_scrub_callback(self, cb):
        """cb(delta_frames: int)"""
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


class VideoFrameLabel(ScrubbableLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        if text:
            self.setText(text)
        self._frame: QImage | None = None

    def set_frame(self, frame: QImage | None):
        self._frame = frame
        self.update()

    def paintEvent(self, event):
        if self._frame is None or self.width() <= 1 or self.height() <= 1:
            return super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        img = self._frame
        if img is None:
            painter.end()
            return
        target = self.rect()
        img_size = img.size()
        img_size.scale(target.size(), Qt.KeepAspectRatio)
        x = target.x() + (target.width() - img_size.width()) // 2
        y = target.y() + (target.height() - img_size.height()) // 2
        painter.drawImage(QRect(x, y, img_size.width(), img_size.height()), img)
        painter.end()


class SegmentDisplay(QLCDNumber):
    # Compatibility shim so existing QLabel-style updates (`setText`) still work.
    def setText(self, text: str):
        self.display(str(text))


class AnnotatedVideoWidget(QWidget):
    annotation_created = Signal(dict)
    annotation_context_requested = Signal(int, object)
    annotation_updated = Signal(int, dict)

    def __init__(self, placeholder_text: str = "", parent=None):
        super().__init__(parent)
        self._frame: QImage | None = None
        self._annotations: list[dict] = []
        self._tool = "line"
        self._color = QColor("#ffcc00")
        self._placeholder_text = placeholder_text
        self._placeholder_image: QImage | None = None
        self._pending_start: QPointF | None = None
        self._pending_end: QPointF | None = None
        self._editable = True
        self._current_frame_index = 0
        self._scrub_callback = None
        self._key_handler = None
        self._edit_idx: int | None = None
        self._drag_handle: str | None = None
        self._timed_start: dict | None = None
        self._tray_points: list[QPointF] = []
        self._tray_view_max = 220
        self._tray_view_window: QWidget | None = None
        self._tray_view_label: QLabel | None = None
        self._last_tray_view: QImage | None = None
        self._tray_update_cb = None
        self._fps = 0.0
        self._status_lines: list[str] = []
        self._target_overlays: list[dict] = []
        self.setMouseTracking(True)

    def set_frame(self, frame: QImage | None):
        self._frame = frame
        self.update()

    def set_placeholder_text(self, text: str):
        self._placeholder_text = text
        self.update()

    def set_placeholder_image(self, image: QImage | None):
        self._placeholder_image = image
        self.update()

    def set_annotations(self, annotations: list[dict]):
        self._annotations = list(annotations)
        if self._edit_idx is not None and self._edit_idx >= len(self._annotations):
            self._edit_idx = None
        self.update()

    def set_tool(self, tool: str):
        self._tool = tool
        if tool != "timed_line":
            self._timed_start = None
        if tool != "tray":
            self._tray_points = []
            self.update()
            if tool != "tray":
                self._clear_tray_view_popout()

    def set_color(self, color: QColor):
        self._color = QColor(color)

    def set_editable(self, editable: bool):
        self._editable = bool(editable)

    def set_current_frame_index(self, frame_index: int):
        self._current_frame_index = int(frame_index)
        self.update()

    def set_scrub_callback(self, cb):
        """cb(delta_frames: int)"""
        self._scrub_callback = cb

    def set_key_handler(self, cb):
        """cb(QKeyEvent)"""
        self._key_handler = cb

    def set_tray_update_callback(self, cb):
        """cb() called when tray points change."""
        self._tray_update_cb = cb

    def set_fps(self, fps: float):
        self._fps = float(fps or 0.0)

    def set_status_lines(self, lines: list[str] | None):
        self._status_lines = [str(line) for line in (lines or []) if str(line).strip()]
        self.update()

    def set_target_overlays(self, overlays: list[dict]) -> None:
        """
        overlays: list of dicts with keys:
          norm_x, norm_y  — position in [0..1] of frame dimensions
          label           — text drawn next to the dot (optional)
          color           — hex string (optional, default #3498db)
        """
        self._target_overlays = list(overlays)
        self.update()

    def eventFilter(self, obj, event):
        if obj is self._tray_view_window and event.type() == QEvent.Resize:
            if self._last_tray_view is not None:
                self._update_tray_view_popout(self._last_tray_view)
        return super().eventFilter(obj, event)

    def set_edit_index(self, idx: int | None):
        if idx is None:
            self._edit_idx = None
        else:
            self._edit_idx = int(idx)
        self.update()

    def get_edit_index(self) -> int | None:
        return self._edit_idx

    def _image_rect(self) -> QRect:
        if self._frame is None or self.width() <= 1 or self.height() <= 1:
            return QRect(0, 0, 0, 0)
        img_w = self._frame.width()
        img_h = self._frame.height()
        scale = min(self.width() / img_w, self.height() / img_h)
        draw_w = int(img_w * scale)
        draw_h = int(img_h * scale)
        x = (self.width() - draw_w) // 2
        y = (self.height() - draw_h) // 2
        return QRect(x, y, draw_w, draw_h)

    def _map_to_image(self, pos) -> QPointF | None:
        if self._frame is None:
            return None
        rect = self._image_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None
        if not rect.contains(int(pos.x()), int(pos.y())):
            return None
        scale = rect.width() / self._frame.width()
        x = (pos.x() - rect.x()) / scale
        y = (pos.y() - rect.y()) / scale
        return QPointF(x, y)

    def _map_from_image(self, pt: QPointF) -> QPointF:
        rect = self._image_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return QPointF(0, 0)
        scale = rect.width() / self._frame.width()
        x = rect.x() + pt.x() * scale
        y = rect.y() + pt.y() * scale
        return QPointF(x, y)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton and self._editable:
            idx = self._hit_test_annotation(event.position())
            if idx is not None:
                self.annotation_context_requested.emit(idx, event.globalPosition())
                return
        if self._frame is None or event.button() != Qt.LeftButton or not self._editable:
            return
        if self._tool == "tray":
            # Only allow one bird's eye region at a time. If one exists, switch to edit.
            existing_idx = None
            for i, ann in enumerate(self._annotations):
                if ann.get("type") == "tray":
                    existing_idx = i
                    break
            if existing_idx is not None:
                self.set_edit_index(existing_idx)
                handle = self._hit_test_handle(event.position())
                if handle is not None:
                    self._drag_handle = handle
                return
            img_pt = self._map_to_image(event.position())
            if img_pt is None:
                return
            self._tray_points.append(img_pt)
            if len(self._tray_points) >= 4:
                ann = {
                    "type": "tray",
                    "points": [[p.x(), p.y()] for p in self._tray_points[:4]],
                    "color": self._color.name(),
                    "pinned": False,
                }
                tray_view = self._build_tray_view(self._tray_points[:4])
                if tray_view is not None:
                    self._last_tray_view = tray_view
                self._tray_points = []
                self.annotation_created.emit(ann)
            self.update()
            return
        if self._edit_idx is not None:
            handle = self._hit_test_handle(event.position())
            if handle is not None:
                self._drag_handle = handle
                return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            return
        self._pending_start = img_pt
        self._pending_end = img_pt
        self.update()

    def mouseMoveEvent(self, event):
        if self._pending_start is None or not self._editable:
            if self._drag_handle and self._edit_idx is not None:
                ann = self._annotations[self._edit_idx]
                img_pt = self._map_to_image(event.position())
                if img_pt is None:
                    return
                if self._drag_handle.startswith("tray:") and ann.get("type") == "tray":
                    try:
                        idx = int(self._drag_handle.split(":")[1])
                    except Exception:
                        return
                    pts = ann.get("points") or []
                    if len(pts) == 4 and 0 <= idx < 4:
                        pts[idx] = [img_pt.x(), img_pt.y()]
                        ann["points"] = pts
                        tray_view = self._build_tray_view([QPointF(p[0], p[1]) for p in pts])
                        if tray_view is not None:
                            self._last_tray_view = tray_view
                        if self._tray_view_window is not None and self._tray_view_window.isVisible():
                            self._update_tray_view_popout(tray_view or self._last_tray_view)
                        if self._tray_update_cb is not None:
                            self._tray_update_cb()
                elif self._drag_handle == "start":
                    ann["start"] = [img_pt.x(), img_pt.y()]
                elif self._drag_handle == "end":
                    ann["end"] = [img_pt.x(), img_pt.y()]
                self.update()
            return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            return
        self._pending_end = img_pt
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._pending_start is None or not self._editable:
            if self._drag_handle and self._edit_idx is not None:
                ann = self._annotations[self._edit_idx]
                self._drag_handle = None
                self.annotation_updated.emit(self._edit_idx, ann)
                self.update()
            return
        img_pt = self._map_to_image(event.position())
        if img_pt is None:
            self._pending_start = None
            self._pending_end = None
            self.update()
            return
        start = self._pending_start
        end = img_pt
        self._pending_start = None
        self._pending_end = None
        if self._tool == "text":
            text, ok = QInputDialog.getText(self, "Add label", "Label text:")
            if ok and text.strip():
                ann = {
                    "type": "text",
                    "pos": [start.x(), start.y()],
                    "text": text.strip(),
                    "color": self._color.name(),
                    "pinned": False,
                }
                self.annotation_created.emit(ann)
        elif self._tool == "timed_line":
            if self._timed_start is None:
                self._timed_start = {
                    "pos": [start.x(), start.y()],
                    "frame_index": self._current_frame_index,
                }
            else:
                ann = {
                    "type": "timed_line",
                    "start": list(self._timed_start.get("pos", [start.x(), start.y()])),
                    "end": [end.x(), end.y()],
                    "start_frame": int(self._timed_start.get("frame_index", self._current_frame_index)),
                    "end_frame": int(self._current_frame_index),
                    "color": self._color.name(),
                    "pinned": False,
                }
                self._timed_start = None
                self.annotation_created.emit(ann)
        else:
            ann = {
                "type": self._tool,
                "start": [start.x(), start.y()],
                "end": [end.x(), end.y()],
                "color": self._color.name(),
                "pinned": False,
            }
            self.annotation_created.emit(ann)
        self.update()

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

    def keyPressEvent(self, event):
        if self._key_handler is not None:
            self._key_handler(event)
        else:
            super().keyPressEvent(event)

    def _handle_points_for_annotation(self, ann: dict) -> tuple[QPointF, QPointF] | None:
        if ann.get("type") not in ("line", "arrow", "measure", "timed_line"):
            return None
        start = ann.get("start") or [0, 0]
        end = ann.get("end") or [0, 0]
        start_pt = self._map_from_image(QPointF(start[0], start[1]))
        end_pt = self._map_from_image(QPointF(end[0], end[1]))
        return start_pt, end_pt

    def _hit_test_handle(self, pos) -> str | None:
        if self._edit_idx is None:
            return None
        ann = self._annotations[self._edit_idx]
        if ann.get("type") == "tray":
            pts = ann.get("points") or []
            if len(pts) != 4:
                return None
            widget_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
            for i, pt in enumerate(widget_pts):
                if math.hypot(pt.x() - pos.x(), pt.y() - pos.y()) <= 8:
                    return f"tray:{i}"
            return None
        handle_pts = self._handle_points_for_annotation(ann)
        if handle_pts is None:
            return None
        start_pt, end_pt = handle_pts
        dist_start = math.hypot(start_pt.x() - pos.x(), start_pt.y() - pos.y())
        dist_end = math.hypot(end_pt.x() - pos.x(), end_pt.y() - pos.y())
        if dist_start <= 6:
            return "start"
        if dist_end <= 6:
            return "end"
        return None

    def _hit_test_annotation(self, pos) -> int | None:
        if not self._annotations:
            return None
        best_idx = None
        best_dist = 12.0
        for idx, ann in enumerate(self._annotations):
            if ann.get("type") == "text":
                pt = ann.get("pos") or [0, 0]
                widget_pt = self._map_from_image(QPointF(pt[0], pt[1]))
                dist = math.hypot(widget_pt.x() - pos.x(), widget_pt.y() - pos.y())
            elif ann.get("type") == "tray":
                pts = ann.get("points") or []
                if len(pts) < 2:
                    continue
                img_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
                dist = 1e9
                for i in range(len(img_pts)):
                    a = img_pts[i]
                    b = img_pts[(i + 1) % len(img_pts)]
                    dist = min(dist, _distance_to_segment(pos.x(), pos.y(), a.x(), a.y(), b.x(), b.y()))
            else:
                start = ann.get("start") or [0, 0]
                end = ann.get("end") or [0, 0]
                s = self._map_from_image(QPointF(start[0], start[1]))
                e = self._map_from_image(QPointF(end[0], end[1]))
                dist = _distance_to_segment(pos.x(), pos.y(), s.x(), s.y(), e.x(), e.y())
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def _order_tray_points(self, pts: list[QPointF]) -> list[QPointF]:
        if len(pts) != 4:
            return pts
        # Robust ordering: sort by angle around centroid, then rotate so top-left is first.
        cx = sum(p.x() for p in pts) / 4.0
        cy = sum(p.y() for p in pts) / 4.0
        def _ang(p):
            return math.atan2(p.y() - cy, p.x() - cx)
        ordered = sorted(pts, key=_ang)
        # Rotate so top-left (min x+y) is first.
        tl_idx = min(range(4), key=lambda i: ordered[i].x() + ordered[i].y())
        ordered = ordered[tl_idx:] + ordered[:tl_idx]
        # Ensure clockwise order: tl -> tr should have smaller y than tl -> bl
        if ordered[1].y() > ordered[3].y():
            ordered = [ordered[0], ordered[3], ordered[2], ordered[1]]
        return ordered

    def _build_tray_view(self, pts: list[QPointF]) -> QImage | None:
        if self._frame is None or len(pts) != 4:
            return None
        ordered = self._order_tray_points(pts)
        tl, tr, br, bl = ordered
        edge_w = max(_dist(tl, tr), _dist(bl, br))
        edge_h = max(_dist(tl, bl), _dist(tr, br))
        w = int(edge_w)
        h = int(edge_h)
        w = max(40, min(800, w))
        h = max(40, min(800, h))
        try:
            frame = self._frame
            # Ensure we operate on RGB for predictable OpenCV warp.
            if frame.format() != QImage.Format_RGB888:
                frame = frame.convertToFormat(QImage.Format_RGB888)
            h_src = frame.height()
            w_src = frame.width()
            bytes_per_line = frame.bytesPerLine()
            ptr = frame.bits()
            size = h_src * bytes_per_line
            try:
                ptr.setsize(size)
            except Exception:
                try:
                    ptr = frame.constBits()
                    ptr.setsize(size)
                except Exception:
                    ptr = memoryview(ptr)
            arr = np.frombuffer(ptr, dtype=np.uint8, count=size).reshape((h_src, bytes_per_line // 3, 3))
            arr = arr[:, :w_src, :]
            src = np.array([[p.x(), p.y()] for p in ordered], dtype=np.float32)
            dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
            mat = cv2.getPerspectiveTransform(src, dst)
            warped = cv2.warpPerspective(arr, mat, (w, h))
            warped = cv2.rotate(warped, cv2.ROTATE_180)
            warped = np.ascontiguousarray(warped)
            qimg = QImage(warped.data, w, h, w * 3, QImage.Format_RGB888)
            return qimg.copy()
        except Exception as exc:
            print(f"[tray] warp failed: {exc}", flush=True)
            return None

    def _update_tray_view_popout(self, tray_view: QImage | None):
        if self._tray_view_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Bird's Eye")
            win.resize(320, 320)
            win.setMinimumSize(200, 200)
            win.installEventFilter(self)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = QLabel("Waiting for Bird's Eye...")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            self._tray_view_window = win
            self._tray_view_label = label
            win.destroyed.connect(lambda _=None: self._clear_tray_view_popout())
            win.show()
        else:
            self._tray_view_window.show()
        if self._tray_view_label is None:
            return
        if tray_view is None or tray_view.isNull():
            self._tray_view_label.setText("Bird's Eye unavailable")
            return
        self._last_tray_view = tray_view
        max_w = max(1, self._tray_view_label.width() - 4)
        max_h = max(1, self._tray_view_label.height() - 4)
        scaled = tray_view.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._tray_view_label.setPixmap(QPixmap.fromImage(scaled))

    def _clear_tray_view_popout(self):
        if self._tray_view_window is not None:
            try:
                self._tray_view_window.close()
            except Exception:
                pass
        self._tray_view_window = None
        self._tray_view_label = None


    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._frame is None:
            if self._placeholder_image is not None and not self._placeholder_image.isNull():
                img = self._placeholder_image
                target = self.rect()
                img_size = img.size()
                img_size.scale(target.size(), Qt.KeepAspectRatio)
                x = target.x() + (target.width() - img_size.width()) // 2
                y = target.y() + (target.height() - img_size.height()) // 2
                painter.drawImage(QRect(x, y, img_size.width(), img_size.height()), img)
                if self._placeholder_text and self._placeholder_text != "No video loaded":
                    painter.setPen(QColor("#e2e8f0"))
                    painter.drawText(self.rect().adjusted(0, 0, 0, -12), Qt.AlignBottom | Qt.AlignHCenter, self._placeholder_text)
            elif self._placeholder_text:
                painter.setPen(QColor("#888888"))
                painter.drawText(self.rect(), Qt.AlignCenter, self._placeholder_text)
            painter.end()
            return
        rect = self._image_rect()
        painter.drawImage(rect, self._frame)
        painter.setRenderHint(QPainter.Antialiasing, True)
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        def draw_line(start_pt: QPointF, end_pt: QPointF, color: QColor, arrow: bool, alpha: int):
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(start_pt, end_pt)
            if arrow:
                dx = end_pt.x() - start_pt.x()
                dy = end_pt.y() - start_pt.y()
                length = math.hypot(dx, dy)
                if length > 0:
                    ux = dx / length
                    uy = dy / length
                    size = 10.0
                    left = QPointF(
                        end_pt.x() - ux * size - uy * size * 0.5,
                        end_pt.y() - uy * size + ux * size * 0.5,
                    )
                    right = QPointF(
                        end_pt.x() - ux * size + uy * size * 0.5,
                        end_pt.y() - uy * size - ux * size * 0.5,
                    )
                    painter.drawLine(end_pt, left)
                    painter.drawLine(end_pt, right)

        def draw_timed_marker(
            center_pt: QPointF,
            radius_outer: int = 5,
            radius_inner: int = 2,
            inner_color: QColor = QColor("#ffffff"),
            outer_color: QColor = QColor("#000000"),
        ):
            painter.setPen(QPen(outer_color))
            painter.setBrush(outer_color)
            painter.drawEllipse(center_pt, radius_outer, radius_outer)
            painter.setPen(QPen(inner_color))
            painter.setBrush(inner_color)
            painter.drawEllipse(center_pt, radius_inner, radius_inner)

        def draw_pin(at_pt: QPointF, color: QColor, alpha: int):
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
            pen.setWidth(2)
            painter.setPen(pen)
            radius = 4
            painter.drawEllipse(QPointF(at_pt.x(), at_pt.y()), radius, radius)
            painter.drawLine(
                QPointF(at_pt.x(), at_pt.y() + radius),
                QPointF(at_pt.x(), at_pt.y() + radius + 10),
            )

        def draw_text_block(
            anchor: QPointF,
            lines: list[str],
            text_color: QColor = QColor("#ffffff"),
            line_colors: list[QColor] | None = None,
        ):
            pad = 2
            if not lines:
                return
            widths = [metrics.boundingRect(line).width() for line in lines]
            heights = [metrics.boundingRect(line).height() for line in lines]
            max_w = max(widths)
            line_h = max(heights)
            block_h = line_h * len(lines)
            bg_rect = QRect(
                int(anchor.x()),
                int(anchor.y()) - block_h,
                max_w + pad * 2,
                block_h + pad * 2,
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 200))
            painter.drawRect(bg_rect)
            for i, line in enumerate(lines):
                color = line_colors[i] if line_colors and i < len(line_colors) else text_color
                painter.setPen(QPen(color))
                painter.drawText(
                    QPointF(bg_rect.x() + pad, bg_rect.y() + pad + line_h * (i + 1) - 2),
                    line,
                )

        if self._status_lines:
            hud_font = QFont("Consolas")
            hud_font.setPointSize(11)
            painter.setFont(hud_font)
            hud_metrics = painter.fontMetrics()
            widths = [hud_metrics.horizontalAdvance(line) for line in self._status_lines]
            line_h = max(1, hud_metrics.height())
            pad_x = 8
            pad_y = 6
            spacing = 2
            hud_w = max(widths) + pad_x * 2 if widths else 0
            hud_h = (line_h * len(self._status_lines)) + (spacing * (len(self._status_lines) - 1)) + pad_y * 2
            hud_rect = QRect(rect.left() + 10, rect.top() + 10, hud_w, hud_h)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 150))
            painter.drawRoundedRect(hud_rect, 6, 6)
            painter.setPen(QPen(QColor("#00ff66")))
            y = hud_rect.y() + pad_y + hud_metrics.ascent()
            for line in self._status_lines:
                painter.drawText(QPointF(hud_rect.x() + pad_x, y), line)
                y += line_h + spacing
            painter.setFont(font)

        for ann in list(self._annotations):
            color = QColor(ann.get("color") or "#ffcc00")
            frame_idx = ann.get("frame_index")
            if frame_idx is not None and frame_idx != self._current_frame_index:
                alpha = 80
            else:
                alpha = 255
            if ann.get("type") == "text":
                pos = ann.get("pos") or [0, 0]
                img_pt = QPointF(pos[0], pos[1])
                widget_pt = self._map_from_image(img_pt)
                painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), alpha)))
                painter.drawText(widget_pt + QPointF(4, -4), ann.get("text", ""))
                if ann.get("pinned"):
                    draw_pin(widget_pt + QPointF(6, 10), color, alpha)
            elif ann.get("type") == "tray":
                pts = ann.get("points") or []
                if len(pts) >= 2:
                    pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha))
                    pen.setWidth(2)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    widget_pts = [self._map_from_image(QPointF(p[0], p[1])) for p in pts]
                    for i in range(len(widget_pts)):
                        a = widget_pts[i]
                        b = widget_pts[(i + 1) % len(widget_pts)]
                        painter.drawLine(a, b)
                    painter.setBrush(QColor(color.red(), color.green(), color.blue(), alpha))
                    for pt in widget_pts:
                        painter.drawEllipse(pt, 4, 4)
            else:
                start = ann.get("start") or [0, 0]
                end = ann.get("end") or [0, 0]
                start_pt = self._map_from_image(QPointF(start[0], start[1]))
                end_pt = self._map_from_image(QPointF(end[0], end[1]))
                arrow = ann.get("type") == "arrow"
                draw_line(start_pt, end_pt, color, arrow, alpha)
                if ann.get("type") == "timed_line":
                    start_frame = int(ann.get("start_frame", 0))
                    end_frame = int(ann.get("end_frame", 0))
                    denom = (end_frame - start_frame) or 1
                    raw_ratio = (self._current_frame_index - start_frame) / denom
                    # Allow the marker to extrapolate a bit past the line end/start.
                    draw_ratio = max(-1.0, min(2.0, raw_ratio))
                    tick_x = start_pt.x() + (end_pt.x() - start_pt.x()) * draw_ratio
                    tick_y = start_pt.y() + (end_pt.y() - start_pt.y()) * draw_ratio
                    extrapolated = raw_ratio < 0.0 or raw_ratio > 1.0
                    inner_color = QColor("#ff3b30") if extrapolated else QColor("#ffffff")
                    draw_timed_marker(QPointF(tick_x, tick_y), inner_color=inner_color)
                    if self._fps > 0:
                        duration_sec = abs(end_frame - start_frame) / self._fps
                        elapsed_frames = max(0, self._current_frame_index - start_frame)
                        elapsed_sec = elapsed_frames / self._fps
                        lines = [f"Total: {duration_sec:.3f}s", f"Elapsed: {elapsed_sec:.3f}s"]
                        if ann.get("distance_m") is not None:
                            try:
                                dist_m = float(ann.get("distance_m"))
                                ratio = max(0.0, elapsed_sec / duration_sec if duration_sec > 0 else 0.0)
                                dist_now = dist_m * ratio
                                lines.append(f"Distance: {dist_now:.3f} m")
                                if duration_sec > 0:
                                    lines.append(f"Speed: {dist_m / duration_sec:.3f} m/s")
                            except (TypeError, ValueError):
                                pass
                        tick_pt = QPointF(tick_x, tick_y)
                        text_color = QColor("#ff3b30") if extrapolated else QColor("#ffffff")
                        line_colors = None
                        if extrapolated:
                            line_colors = [QColor("#ffffff")] * len(lines)
                            if ann.get("distance_m") is not None and len(lines) >= 3:
                                line_colors[2] = QColor("#ff3b30")
                        draw_text_block(
                            tick_pt + QPointF(8, 14),
                            lines,
                            text_color=text_color,
                            line_colors=line_colors,
                        )
                if ann.get("type") == "measure":
                    dx = (end[0] - start[0])
                    dy = (end[1] - start[1])
                    dist = math.hypot(dx, dy)
                    mid = QPointF((start_pt.x() + end_pt.x()) / 2, (start_pt.y() + end_pt.y()) / 2)
                    draw_text_block(mid + QPointF(6, -6), [f"{dist:.1f}px"])
                if ann.get("pinned"):
                    draw_pin(end_pt + QPointF(6, 10), color, alpha)

        if self._edit_idx is not None and self._editable:
            if self._edit_idx >= len(self._annotations):
                self._edit_idx = None
            else:
                ann = self._annotations[self._edit_idx]
                if ann.get("type") == "tray":
                    pts = ann.get("points") or []
                    if len(pts) == 4:
                        pen = QPen(QColor("#ffffff"))
                        pen.setWidth(1)
                        painter.setPen(pen)
                        painter.setBrush(QColor("#1e90ff"))
                        size = 6
                        for p in pts:
                            pt = self._map_from_image(QPointF(p[0], p[1]))
                            painter.drawRect(QRect(int(pt.x() - size / 2), int(pt.y() - size / 2), size, size))
                else:
                    handle_pts = self._handle_points_for_annotation(ann)
                    if handle_pts is not None:
                        start_pt, end_pt = handle_pts
                        pen = QPen(QColor("#ffffff"))
                        pen.setWidth(1)
                        painter.setPen(pen)
                        painter.setBrush(QColor("#1e90ff"))
                        size = 6
                        painter.drawRect(QRect(int(start_pt.x() - size / 2), int(start_pt.y() - size / 2), size, size))
                        painter.drawRect(QRect(int(end_pt.x() - size / 2), int(end_pt.y() - size / 2), size, size))

        if self._pending_start is not None and self._pending_end is not None:
            start_pt = self._map_from_image(self._pending_start)
            end_pt = self._map_from_image(self._pending_end)
            draw_line(start_pt, end_pt, self._color, self._tool == "arrow", 255)
            if self._tool == "measure":
                dx = self._pending_end.x() - self._pending_start.x()
                dy = self._pending_end.y() - self._pending_start.y()
                dist = math.hypot(dx, dy)
                mid = QPointF((start_pt.x() + end_pt.x()) / 2, (start_pt.y() + end_pt.y()) / 2)
                draw_text_block(mid + QPointF(6, -6), [f"{dist:.1f}px"])
        if self._tool == "tray" and self._tray_points:
            pen = QPen(self._color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(QColor(self._color.red(), self._color.green(), self._color.blue(), 120))
            widget_pts = [self._map_from_image(p) for p in self._tray_points]
            for i in range(len(widget_pts) - 1):
                painter.drawLine(widget_pts[i], widget_pts[i + 1])
            for pt in widget_pts:
                painter.drawEllipse(pt, 4, 4)
        if self._timed_start is not None and self._tool == "timed_line":
            pos = self._timed_start.get("pos", [0, 0])
            start_pt = self._map_from_image(QPointF(pos[0], pos[1]))
            draw_timed_marker(start_pt)

        # Tray bird's-eye view overlay
        tray_ann = None
        for ann in reversed(self._annotations):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is not None:
            pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
            tray_view = self._build_tray_view(pts)
            if tray_view is not None:
                self._last_tray_view = tray_view

        # Conveyor target overlays
        if self._target_overlays and self._frame is not None:
            fw = self._frame.width()
            fh = self._frame.height()
            painter.setRenderHint(QPainter.Antialiasing)

            def _img_to_widget(nx, ny):
                return self._map_from_image(QPointF(nx * fw, ny * fh))

            for ov in self._target_overlays:
                opacity = float(ov.get("opacity", 1.0))
                painter.setOpacity(opacity)

                hex_color = ov.get("color", "#3498db")
                color = QColor(hex_color)
                label = str(ov.get("label", ""))
                info_lines = [str(line) for line in (ov.get("info_lines") or []) if str(line).strip()]
                alert = bool(ov.get("alert", False))
                text_bg_color = QColor(str(ov.get("text_bg_color", "#000000")))
                if not text_bg_color.isValid():
                    text_bg_color = QColor("#000000")

                # Rectangle outline
                corners = ov.get("rect_corners")
                if corners and len(corners) == 4:
                    poly = QPolygonF([_img_to_widget(nx, ny) for nx, ny in corners])
                    painter.setPen(QPen(color, 1.5))
                    fill = QColor(color.red(), color.green(), color.blue(),
                                  max(0, min(255, int(40 * opacity))))
                    painter.setBrush(QBrush(fill))
                    painter.drawPolygon(poly)

                # Centre dot
                nx = float(ov.get("norm_x", 0))
                ny = float(ov.get("norm_y", 0))
                wp = _img_to_widget(nx, ny)
                painter.setPen(QPen(color, 1))
                painter.setBrush(QBrush(color))
                painter.drawEllipse(wp, 3, 3)
                if alert:
                    painter.setPen(QPen(QColor("#f1c40f"), 2))
                    painter.setBrush(Qt.NoBrush)
                    painter.drawEllipse(wp, 6, 6)

                # Label (most recent only — caller sets label="" for ghosts)
                if info_lines or label:
                    lines = info_lines or [label]
                    f = painter.font()
                    f.setPointSize(8)
                    painter.setFont(f)
                    metrics = painter.fontMetrics()
                    text_x = int(wp.x()) + 10
                    text_y = int(wp.y()) + 4
                    text_w = max(metrics.horizontalAdvance(line) for line in lines)
                    line_h = metrics.height()
                    text_h = line_h * len(lines)
                    bg_rect = QRect(
                        text_x - 4,
                        text_y - metrics.ascent() - 2,
                        text_w + 8,
                        text_h + 4,
                    )
                    painter.setPen(Qt.NoPen)
                    text_bg_color.setAlpha(220)
                    painter.setBrush(QBrush(text_bg_color))
                    painter.drawRoundedRect(bg_rect, 4, 4)
                    painter.setPen(QPen(Qt.white))
                    for line_idx, line in enumerate(lines):
                        if line_idx == 0:
                            bold_font = painter.font()
                            bold_font.setBold(True)
                            painter.setFont(bold_font)
                            metrics = painter.fontMetrics()
                        elif line_idx == 1:
                            normal_font = painter.font()
                            normal_font.setBold(False)
                            painter.setFont(normal_font)
                            metrics = painter.fontMetrics()
                            line_h = metrics.height()
                        painter.drawText(text_x, text_y + line_idx * line_h, line)

            painter.setOpacity(1.0)

        painter.end()


class EventMarkerBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._markers: list[tuple[float, QColor]] = []
        self._left_pad = 0
        self._right_pad = 0
        self._triangle_red_markers = False
        self.setMinimumHeight(12)

    def set_markers(self, markers: list[tuple[float, str]]):
        converted: list[tuple[float, QColor]] = []
        for ratio, color in markers:
            r = max(0.0, min(1.0, float(ratio)))
            try:
                q_color = QColor(color)
                if not q_color.isValid():
                    q_color = QColor("#ffffff")
            except Exception:
                q_color = QColor("#ffffff")
            converted.append((r, q_color))
        self._markers = converted
        self.update()

    def set_track_padding(self, left: int, right: int):
        left = max(0, int(left))
        right = max(0, int(right))
        if left == self._left_pad and right == self._right_pad:
            return
        self._left_pad = left
        self._right_pad = right
        self.update()

    def clear(self):
        self._markers = []
        self.update()

    def set_triangle_red_markers(self, enabled: bool):
        enabled = bool(enabled)
        if self._triangle_red_markers == enabled:
            return
        self._triangle_red_markers = enabled
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QColor("#1e1e1e"))
        if not self._markers:
            return
        track_rect = rect.adjusted(self._left_pad, 0, -self._right_pad, 0)
        if track_rect.width() <= 0:
            return
        baseline = track_rect.height() - 1
        painter.setRenderHint(QPainter.Antialiasing, False)
        for ratio, color in self._markers:
            span = max(1, track_rect.width() - 1)
            x = int(track_rect.left() + ratio * span)
            is_red_marker = (
                self._triangle_red_markers
                and color.red() >= 180
                and color.red() > (color.green() + 40)
                and color.red() > (color.blue() + 40)
            )
            if is_red_marker:
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setPen(Qt.NoPen)
                painter.setBrush(color)
                triangle_half_width = 4
                triangle_height = min(track_rect.height(), 7)
                triangle = QPolygonF(
                    [
                        QPointF(x, track_rect.top()),
                        QPointF(x - triangle_half_width, track_rect.top() + triangle_height),
                        QPointF(x + triangle_half_width, track_rect.top() + triangle_height),
                    ]
                )
                painter.drawPolygon(triangle)
                painter.restore()
                continue
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(x, track_rect.top(), x, baseline)
        painter.end()


class DriftSlider(QSlider):
    """Compact centre-zero slider with a red offset indicator."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setFixedWidth(96)
        self.setFixedHeight(18)
        self.setCursor(Qt.PointingHandCursor)

    def _track_rect(self) -> QRect:
        return self.rect().adjusted(8, 5, -8, -5)

    def _value_to_x(self, value: int) -> float:
        track = self._track_rect()
        rng = max(1, self.maximum() - self.minimum())
        ratio = (value - self.minimum()) / rng
        return track.left() + ratio * track.width()

    def _set_value_from_x(self, x: float) -> None:
        track = self._track_rect()
        if track.width() <= 0:
            return
        clamped_x = max(track.left(), min(track.right(), x))
        ratio = (clamped_x - track.left()) / max(1, track.width())
        value = self.minimum() + ratio * (self.maximum() - self.minimum())
        self.setValue(int(round(value)))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._set_value_from_x(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._set_value_from_x(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def paintEvent(self, event):
        _ = event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self._track_rect()
        cy = track.center().y()
        left = track.left()
        right = track.right()
        zero_value = 0 if self.minimum() <= 0 <= self.maximum() else self.minimum()
        mid_x = self._value_to_x(zero_value)
        value_x = self._value_to_x(self.value())

        painter.setPen(QPen(QColor("#4a5563"), 2))
        painter.drawLine(left, cy, right, cy)

        painter.setPen(QPen(QColor("#8b949e"), 1))
        painter.drawLine(int(mid_x), cy - 5, int(mid_x), cy + 5)

        painter.setPen(QPen(QColor("#d33b3b"), 2))
        painter.drawLine(QPointF(mid_x, cy), QPointF(value_x, cy))

        painter.setPen(QPen(QColor("#f0f6fc"), 1))
        painter.setBrush(QBrush(QColor("#f0f6fc")))
        painter.drawEllipse(QPointF(value_x, cy), 4, 4)
        painter.end()


class ClipRangeSlider(QSlider):
    clip_range_export_requested = Signal(int, int)

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._clip_start_value: int | None = None
        self._clip_end_value: int | None = None
        self._drag_handle: str | None = None
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def clear_clip_range(self):
        self._clip_start_value = None
        self._clip_end_value = None
        self._drag_handle = None
        self.unsetCursor()
        self.update()

    def has_clip_range(self) -> bool:
        return self._clip_start_value is not None and self._clip_end_value is not None

    def ordered_clip_range(self) -> tuple[int, int] | None:
        if self._clip_start_value is None or self._clip_end_value is None:
            return None
        start = int(self._clip_start_value)
        end = int(self._clip_end_value)
        if start <= end:
            return start, end
        return end, start

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            handle = self._handle_at_pos(event.position().toPoint())
            if handle is not None:
                self._drag_handle = handle
                self._update_drag_handle(event.position().toPoint())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_handle is not None:
            self._update_drag_handle(event.position().toPoint())
            event.accept()
            return
        if self._handle_at_pos(event.position().toPoint()) is not None:
            self.setCursor(Qt.SizeHorCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self._show_context_menu(event.position().toPoint())
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._drag_handle is not None:
            self._update_drag_handle(event.position().toPoint())
            self._drag_handle = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        groove = self._groove_rect()
        if not groove.isValid():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        ordered = self.ordered_clip_range()
        if ordered is not None:
            start_x = self._value_to_x(ordered[0], groove)
            end_x = self._value_to_x(ordered[1], groove)
            left_x = min(start_x, end_x)
            width = max(2, abs(end_x - start_x))
            fill_rect = QRect(int(left_x), groove.top(), int(width), groove.height())
            painter.fillRect(fill_rect, QColor(90, 145, 220, 70))
        for value, color in (
            (self._clip_start_value, QColor("#7dd3fc")),
            (self._clip_end_value, QColor("#fbbf24")),
        ):
            if value is None:
                continue
            x = self._value_to_x(value, groove)
            pen = QPen(color)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawLine(int(x), groove.top() - 3, int(x), groove.bottom() + 3)
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#101010")))
            painter.drawEllipse(QPointF(x, groove.center().y()), 5, 5)
        painter.end()

    def _show_context_menu(self, pos):
        click_value = self._pos_to_value(pos)
        menu = QMenu(self)
        set_start_action = menu.addAction("Set Clip Start")
        set_end_action = menu.addAction("Set Clip End")
        clear_action = None
        export_action = None
        if self.has_clip_range() and self._value_within_range(click_value):
            menu.addSeparator()
            export_action = menu.addAction("Export Clip")
            clear_action = menu.addAction("Clear Clip Range")
        elif self._clip_start_value is not None or self._clip_end_value is not None:
            menu.addSeparator()
            clear_action = menu.addAction("Clear Clip Range")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == set_start_action:
            self._clip_start_value = click_value
            if self._clip_end_value is not None and self._clip_end_value < click_value:
                self._clip_end_value = click_value
            self.update()
        elif chosen == set_end_action:
            self._clip_end_value = click_value
            if self._clip_start_value is not None and self._clip_start_value > click_value:
                self._clip_start_value = click_value
            self.update()
        elif chosen == clear_action:
            self.clear_clip_range()
        elif chosen == export_action:
            ordered = self.ordered_clip_range()
            if ordered is not None and ordered[1] > ordered[0]:
                self.clip_range_export_requested.emit(ordered[0], ordered[1])

    def _update_drag_handle(self, pos):
        value = self._pos_to_value(pos)
        if self._drag_handle == "start":
            if self._clip_end_value is not None and value > self._clip_end_value:
                value = self._clip_end_value
            self._clip_start_value = value
        elif self._drag_handle == "end":
            if self._clip_start_value is not None and value < self._clip_start_value:
                value = self._clip_start_value
            self._clip_end_value = value
        self.update()

    def _handle_at_pos(self, pos) -> str | None:
        groove = self._groove_rect()
        if not groove.isValid():
            return None
        margin = 8
        if self._clip_start_value is not None:
            x = self._value_to_x(self._clip_start_value, groove)
            if abs(pos.x() - x) <= margin:
                return "start"
        if self._clip_end_value is not None:
            x = self._value_to_x(self._clip_end_value, groove)
            if abs(pos.x() - x) <= margin:
                return "end"
        return None

    def _value_within_range(self, value: int) -> bool:
        ordered = self.ordered_clip_range()
        if ordered is None:
            return False
        return ordered[0] <= value <= ordered[1]

    def _groove_rect(self):
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)

    def _pos_to_value(self, pos) -> int:
        groove = self._groove_rect()
        if not groove.isValid():
            return self.minimum()
        span = max(1, groove.width() - 1)
        ratio = (pos.x() - groove.left()) / span
        ratio = max(0.0, min(1.0, ratio))
        return int(round(self.minimum() + ratio * (self.maximum() - self.minimum())))

    def _value_to_x(self, value: int, groove) -> float:
        rng = max(1, self.maximum() - self.minimum())
        ratio = (value - self.minimum()) / rng
        return groove.left() + ratio * max(1, groove.width() - 1)


_HIGHLIGHT_ACTIVE_BG  = QColor("#cc2222")  # red   — playhead is inside this event
_HIGHLIGHT_NEAREST_BG = QColor("#7a4800")  # amber — next upcoming event (forward bound)
_HIGHLIGHT_FG         = QColor("#ffffff")


class LogListModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[str] = []
        self._active: set[int] = set()
        self._nearest: int | None = None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self._rows):
            return None
        row = index.row()
        if role == Qt.DisplayRole:
            return self._rows[row]
        if role == Qt.BackgroundRole:
            if row in self._active:
                return _HIGHLIGHT_ACTIVE_BG
            if row == self._nearest:
                return _HIGHLIGHT_NEAREST_BG
        if role == Qt.ForegroundRole:
            if row in self._active or row == self._nearest:
                return _HIGHLIGHT_FG
        return None

    def reset_data(self, rows: list[str]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._active = set()
        self._nearest = None
        self.endResetModel()

    _HIGHLIGHT_ROLES = [Qt.BackgroundRole, Qt.ForegroundRole]

    def set_highlights(self, active: set[int], nearest: int | None) -> None:
        if active == self._active and nearest == self._nearest:
            return
        changed_rows = self._active | ({self._nearest} if self._nearest is not None else set())
        changed_rows |= active | ({nearest} if nearest is not None else set())
        self._active = active
        self._nearest = nearest
        for row in changed_rows:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, self._HIGHLIGHT_ROLES)


# -------- GUI APPLICATION --------

class VideoLogViewer(QWidget):
    logs_ready = Signal(list)
    logs_failed = Signal(str)
    current_time_changed = Signal(object)
    annotation_status_changed = Signal(object, bool)
    cache_prefetch_done = Signal()
    cache_clip_ready = Signal(object)
    clip_range_export_requested = Signal(float, float)
    settings_saved = Signal()
    close_gap_threshold_changed = Signal(float)

    def __init__(self):
        super().__init__()

        self.setWindowTitle("The Logfather")
        self.settings = Settings.load()
        self._export_target_overlay_provider = None

        # Video state
        self.cap = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False
        # Sequential-read tracking: which capture we last read from without
        # seeking, and the frame index its next read() will deliver. Seeking
        # (CAP_PROP_POS_FRAMES) forces a keyframe jump + decode-forward on
        # H.264, so it must only happen when playback actually jumps.
        self._seq_cap = None
        self._seq_next_frame = -1
        self._seq_secondary_cap = None
        self._seq_secondary_next_frame = -1

        self.last_qimage: QImage | None = None
        self.last_frame_rgb: np.ndarray | None = None
        self._last_frame_index: int | None = None
        self.current_video_path: str | None = None
        self.current_video_original_path: Path | None = None
        self.current_video_filename_dt: datetime | None = None

        # Frame analysis state (persists across clips for reference frame)
        self.analysis_ref_frame_rgb: np.ndarray | None = None
        self.analysis_ref_frame_index: int | None = None
        self.analysis_prev_frame_rgb: np.ndarray | None = None
        self.analysis_prev_frame_index: int | None = None

        # Secondary video state (AdditionalCCTV)
        self.secondary_cap = None
        self.secondary_fps = 25.0
        self.secondary_frame_count = 0
        self.secondary_current_frame = 0
        self.secondary_last_qimage: QImage | None = None
        self.secondary_video_path: str | None = None
        self.secondary_video_original_path: Path | None = None
        self.secondary_video_filename_dt: datetime | None = None
        self._pending_secondary_original_path: Path | None = None
        self._pending_secondary_poll = False
        self._pending_secondary_timer = QTimer(self)
        self._pending_secondary_timer.setInterval(500)
        self._pending_secondary_timer.timeout.connect(self._poll_pending_secondary_cache)
        self._pending_secondary_last_size: int | None = None
        self._pending_secondary_stable_count = 0
        self.secondary_video_start_dt: datetime | None = None
        self.secondary_ocr_offset_seconds: float | None = None
        self.secondary_ocr_frame_offset = 0
        self.secondary_manual_offset_frames = 0
        self._updating_video_label = False
        self._pending_video_label_update = False
        self._draw_secondary_video = False
        self._popout_window: QWidget | None = None
        self._popout_label: AnnotatedVideoWidget | None = None
        self._popout_color_btn: QToolButton | None = None
        self._popout_tool_group: QButtonGroup | None = None
        self._tray_view_window: QWidget | None = None
        self._tray_view_label: QLabel | None = None
        self._clip_annotations: list[dict] = []
        self._pinned_annotations: list[dict] = []
        self._annotation_history: list[dict] = []
        self._annotation_tool = "line"
        self._annotation_color = QColor("#ffcc00")

        # All events/logs from CSV (before filtering)
        self.all_events: list[LogEvent] = []
        self.all_log_display_rows: list[str] = []
        self.all_source_keys: list[str] = []
        self.all_state_keys: list[str] = []
        self.all_message_keys: list[str] = []

        # Active (filtered) events/logs
        self.events: list[LogEvent] = []
        self.log_display_rows: list[str] = []

        # Filter checkboxes: key -> QCheckBox
        self.source_checkboxes: dict[str, QCheckBox] = {}
        self.state_checkboxes: dict[str, QCheckBox] = {}
        self.message_checkboxes: dict[str, QCheckBox] = {}

        # Time offsets
        self.sync_offset = 0.0      # coarse sync (sync logs to video)
        self.time_offset = 0.0      # fine-tune offset from spinbox
        self.close_gap_threshold = 0.50
        self.close_gap_threshold_min = 0.25
        self.close_gap_threshold_max = 1.00
        self.close_gap_threshold_step = 0.05
        self.first_log_dt: datetime | None = None
        self.video_start_dt: datetime | None = None
        self.ocr_offset_seconds: float | None = None
        self.ocr_frame_offset = 0
        self._ocr_sync_prompt_choice: bool | None = None
        self.ocr_settings_path: Path | None = None
        self.offset_cache_path: Path | None = None
        self.pending_pikpak_path: str | None = None
        self.pending_start_iso: str | None = None
        self.pending_end_iso: str | None = None
        self.auto_load_clip_logs = True
        self._pending_log_request_key: tuple[str, str, str] | None = None
        self._active_log_request_key: tuple[str, str, str] | None = None
        self._loaded_log_request_key: tuple[str, str, str] | None = None
        self._pending_log_autoload_timer = QTimer(self)
        self._pending_log_autoload_timer.setSingleShot(True)
        self._pending_log_autoload_timer.setInterval(350)
        self._pending_log_autoload_timer.timeout.connect(self._auto_load_pending_logs)
        self._auto_ocr_attempted_key: str | None = None
        self._auto_secondary_ocr_attempted_key: str | None = None

        # First log time (string like "HH:MM:SS.mmm")
        self.first_log_time_str: str | None = None

        # Timer for playback
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self._log_executor = ThreadPoolExecutor(max_workers=1)
        self._cache_executor = ThreadPoolExecutor(max_workers=1)
        # Background prefetch runs on its own pool so a click-triggered
        # download (on _cache_executor) never queues behind a day's worth of
        # prefetch copies. Two workers: parallel SMB streams usually lift
        # aggregate throughput on the high-latency HiDrive share.
        self._prefetch_executor = ThreadPoolExecutor(max_workers=2)
        self._prefetch_futures: dict[str, Future] = {}
        self._cache_status_future: Future | None = None
        self._cache_status_pending = False
        self._prefetch_pending: set[str] = set()
        # Async clip download: (generation, source path on Z:, cache target).
        # Generation invalidates a pending download when another clip is
        # chosen before the copy finishes.
        self._pending_video_load: tuple[int, Path, Path] | None = None
        # Seek requested while the clip was still downloading; replayed once
        # the download opens (generation, seconds, pause).
        self._pending_seek: tuple[int, float, bool] | None = None
        self._video_load_generation = 0
        self._video_load_t0 = 0.0
        self._video_busy_dialog: QProgressDialog | None = None
        self._log_future: Future | None = None
        self._log_future_id = 0
        self.logs_ready.connect(self._on_elastic_logs_ready)
        self.logs_failed.connect(self._on_elastic_logs_failed)
        self.log_markers: list[tuple[float, str]] = []
        self.log_markers_enabled = False
        self.external_markers: list[tuple[float, str]] = []
        self.external_marker_source: str | None = None
        self._sku_timeline_items: list[object] = []
        self._ppm_event_seconds: list[float] = []
        self._ppm_interval_prefix_sum: list[float] = []
        self._ocr_tool_dialog = None

        # ----- LEFT FILTER PANEL: SOURCE + MESSAGE -----

        self.filters_loaded = False

        # Source filter
        self.source_label = QLabel(f"Filter by {SOURCE_COLUMN}")
        self.source_label.setWordWrap(True)

        self.source_container_widget = QWidget()
        self.source_layout_inner = QVBoxLayout(self.source_container_widget)
        self.source_layout_inner.addStretch(1)

        self.source_scroll = QScrollArea()
        self.source_scroll.setWidgetResizable(True)
        self.source_scroll.setWidget(self.source_container_widget)
        self.source_scroll.setMinimumWidth(160)

        self.source_all_btn = QPushButton("All")
        self.source_none_btn = QPushButton("None")
        self.source_all_btn.clicked.connect(self.select_all_sources)
        self.source_none_btn.clicked.connect(self.select_no_sources)

        source_header_layout = QHBoxLayout()
        source_header_layout.addWidget(self.source_label, 1)
        source_header_layout.addWidget(self.source_all_btn)
        source_header_layout.addWidget(self.source_none_btn)

        # State filter
        self.state_label = QLabel(f"Filter by {STATE_COLUMN}")
        self.state_label.setWordWrap(True)

        self.state_container_widget = QWidget()
        self.state_layout_inner = QVBoxLayout(self.state_container_widget)
        self.state_layout_inner.addStretch(1)

        self.state_scroll = QScrollArea()
        self.state_scroll.setWidgetResizable(True)
        self.state_scroll.setWidget(self.state_container_widget)
        self.state_scroll.setMinimumWidth(160)

        self.state_all_btn = QPushButton("All")
        self.state_none_btn = QPushButton("None")
        self.state_all_btn.clicked.connect(self.select_all_states)
        self.state_none_btn.clicked.connect(self.select_no_states)

        state_header_layout = QHBoxLayout()
        state_header_layout.addWidget(self.state_label, 1)
        state_header_layout.addWidget(self.state_all_btn)
        state_header_layout.addWidget(self.state_none_btn)

        # Message filter
        self.message_label = QLabel(f"Filter by {MESSAGE_COLUMN}")
        self.message_label.setWordWrap(True)

        self.message_container_widget = QWidget()
        self.message_layout_inner = QVBoxLayout(self.message_container_widget)
        self.message_layout_inner.addStretch(1)

        self.message_scroll = QScrollArea()
        self.message_scroll.setWidgetResizable(True)
        self.message_scroll.setWidget(self.message_container_widget)
        self.message_scroll.setMinimumWidth(160)

        self.message_all_btn = QPushButton("All")
        self.message_none_btn = QPushButton("None")
        self.message_all_btn.clicked.connect(self.select_all_messages)
        self.message_none_btn.clicked.connect(self.select_no_messages)

        message_header_layout = QHBoxLayout()
        message_header_layout.addWidget(self.message_label, 1)
        message_header_layout.addWidget(self.message_all_btn)
        message_header_layout.addWidget(self.message_none_btn)


        self.filter_panel_layout = QVBoxLayout()
        self.filter_panel_layout.addLayout(source_header_layout)
        self.filter_panel_layout.addWidget(self.source_scroll)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addLayout(state_header_layout)
        self.filter_panel_layout.addWidget(self.state_scroll)
        self.filter_panel_layout.addSpacing(12)
        self.filter_panel_layout.addLayout(message_header_layout)
        self.filter_panel_layout.addWidget(self.message_scroll)

        self.filter_panel = QWidget()
        self.filter_panel.setLayout(self.filter_panel_layout)
        self.filter_panel.setVisible(False)

        self.filter_container = QWidget()
        self.filter_container_layout = QVBoxLayout(self.filter_container)
        self.filter_container_layout.setContentsMargins(0, 0, 0, 0)
        self.filter_container_layout.setSpacing(8)
        self.filter_container_layout.addWidget(self.filter_panel)

        # ----- CUSTOM FILTER TAB -----

        self.custom_filter_blocks: list[tuple[QPushButton, QLineEdit, QLineEdit, QLabel]] = []
        self.custom_filter_mode = None
        self.custom_filter_hint = QLabel("Empty entries are ignored. Use commas to separate terms.")
        self.custom_filter_hint.setStyleSheet("color: #888888;")

        self.filter_preset_group: list[QPushButton] = []
        self.active_filter_preset_index: int | None = None
        self.active_filter_presets: set[int] = set()

        preset_container = QWidget()
        preset_container_layout = QVBoxLayout(preset_container)
        preset_container_layout.setContentsMargins(0, 0, 0, 0)
        preset_container_layout.setSpacing(4)

        preset_index = 0
        for _row in range(3):
            preset_row = QHBoxLayout()
            preset_row.setSpacing(6)
            for _col in range(5):
                idx = preset_index + 1
                btn = QPushButton(f"Preset {idx}")
                btn.setCheckable(True)
                btn.clicked.connect(lambda _checked, i=preset_index: self._on_filter_preset_clicked(i))
                btn.setContextMenuPolicy(Qt.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda _pos, i=preset_index: self._on_filter_preset_menu(i)
                )
                btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                preset_row.addWidget(btn, 1)
                self.filter_preset_group.append(btn)
                preset_index += 1
            preset_container_layout.addLayout(preset_row)

        custom_tab = QWidget()
        self._custom_tab = custom_tab
        custom_layout = QVBoxLayout(custom_tab)
        custom_layout.setContentsMargins(8, 8, 8, 8)
        custom_layout.setSpacing(6)
        custom_layout.addWidget(QLabel("Custom filters (comma separated)."))

        custom_layout.addWidget(self.custom_filter_hint)

        for idx in range(1, 6):
            block = QWidget()
            block_layout = QVBoxLayout(block)
            block_layout.setContentsMargins(0, 6, 0, 6)
            block_layout.setSpacing(4)

            btn = QPushButton(f"Preset {idx}")
            btn.setCheckable(True)
            btn.toggled.connect(self._on_custom_filter_changed)
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos, i=idx - 1: self._on_custom_filter_menu(i)
            )
            in_edit = QLineEdit()
            in_edit.setPlaceholderText("Filter in (comma separated)")
            in_edit.textChanged.connect(lambda _text, b=btn: self._on_custom_filter_text_changed(b))
            in_edit.textChanged.connect(self._validate_custom_filter_inputs)
            out_edit = QLineEdit()
            out_edit.setPlaceholderText("Filter out (comma separated)")
            out_edit.textChanged.connect(lambda _text, b=btn: self._on_custom_filter_text_changed(b))
            out_edit.textChanged.connect(self._validate_custom_filter_inputs)
            count_label = QLabel("Matches: -")
            count_label.setStyleSheet("color: #888888;")

            block_layout.addWidget(btn)
            block_layout.addWidget(in_edit)
            block_layout.addWidget(out_edit)
            block_layout.addWidget(count_label)
            custom_layout.addWidget(block)
            self.custom_filter_blocks.append((btn, in_edit, out_edit, count_label))

        custom_layout.addStretch(1)

        # ----- MIDDLE: VIDEO + CONTROLS -----

        self._placeholder_image = _load_placeholder_image()
        self.video_label = AnnotatedVideoWidget("No video loaded")
        self.video_label.setMinimumSize(300, 200)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.set_scrub_callback(self._handle_scroll_wheel)
        self.video_label.set_tray_update_callback(self._refresh_tray_view_if_open)
        self.video_label.set_editable(False)
        if self._placeholder_image is not None:
            self.video_label.set_placeholder_image(self._placeholder_image)
        self.video_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.video_label.customContextMenuRequested.connect(self._copy_main_frame_to_clipboard)
        self.video_label.installEventFilter(self)
        self.scroll_events_mode = False
        self.video_sync_btn = QPushButton("Sync Time")
        self.video_sync_btn.setFixedWidth(110)
        self.video_sync_btn.setEnabled(False)
        self.video_sync_btn.clicked.connect(self.open_ocr_roi_tool)
        self._main_sync_done = False

        self.secondary_video_label = VideoFrameLabel("Additional CCTV not loaded")
        self.secondary_video_label.setAlignment(Qt.AlignCenter)
        self.secondary_video_label.setMinimumSize(300, 200)
        self.secondary_video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.secondary_video_label.setVisible(False)
        self.secondary_video_label.set_scrub_callback(self._handle_secondary_scroll_wheel)
        self.secondary_video_label.setFocusPolicy(Qt.StrongFocus)
        self.secondary_video_label.installEventFilter(self)
        self.secondary_video_label.setContextMenuPolicy(Qt.CustomContextMenu)
        self.secondary_video_label.customContextMenuRequested.connect(self._copy_secondary_frame_to_clipboard)
        self.secondary_sync_btn = QPushButton("Sync Time")
        self.secondary_sync_btn.setFixedWidth(110)
        self.secondary_sync_btn.setEnabled(False)
        self.secondary_sync_btn.clicked.connect(self.open_secondary_ocr_tool)
        self._secondary_sync_done = False
        self.secondary_lock_toggle = QLabel("--Lock--")
        self.secondary_lock_toggle.setAlignment(Qt.AlignCenter)
        self.secondary_lock_toggle.setEnabled(False)
        self.secondary_lock_toggle.setStyleSheet("color: #888888;")
        self.secondary_lock_toggle.setCursor(Qt.PointingHandCursor)
        self.secondary_lock_toggle.mousePressEvent = self._toggle_secondary_lock
        self.secondary_locked = True

        self.seek_slider = ClipRangeSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.on_slider_moved)
        self.seek_slider.sliderPressed.connect(self.pause)
        self.seek_slider.clip_range_export_requested.connect(self._emit_seek_range_export_requested)

        self.info_label = SegmentDisplay()
        self.info_label.setDigitCount(12)  # 00:00:00.000
        self.info_label.setSegmentStyle(QLCDNumber.Flat)
        self.info_label.display("00:00:00.000")
        self.info_label.setFixedWidth(170)
        self.info_label.setStyleSheet("QLCDNumber { background-color: #000000; color: #00ff66; }")

        self.calc_label = SegmentDisplay()
        self.calc_label.setDigitCount(12)  # 00:00:00.000
        self.calc_label.setSegmentStyle(QLCDNumber.Flat)
        self.calc_label.display("00:00:00.000")
        self.calc_label.setFixedWidth(170)
        self.calc_label.setStyleSheet("QLCDNumber { background-color: #000000; color: #00ff66; }")

        self.frame_label = SegmentDisplay()
        self.frame_label.setDigitCount(8)
        self.frame_label.setSegmentStyle(QLCDNumber.Flat)
        self.frame_label.display("0")
        self.frame_label.setFixedWidth(120)
        self.frame_label.setStyleSheet("QLCDNumber { background-color: #000000; color: #00ff66; }")

        self.offset_min = -2.0
        self.offset_max = 2.0
        self.offset_step = 0.05
        self._offset_slider_scale = 1000

        self.play_pause_btn = QPushButton("Play")
        self.load_secondary_btn = None

        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        self.annotate_btn = QPushButton("Annotate")
        self.annotate_btn.clicked.connect(self._open_annotation_popout)
        self.tray_view_btn = QPushButton("Bird's Eye")
        self.tray_view_btn.clicked.connect(self._open_tray_view_window)
        self.analysis_main_alpha_label = QLabel("Overlay: 0.60")
        self.analysis_main_alpha_slider = QSlider(Qt.Horizontal)
        self.analysis_main_alpha_slider.setRange(0, 100)
        self.analysis_main_alpha_slider.setValue(60)
        self.analysis_main_alpha_slider.setFixedWidth(150)
        self.analysis_main_alpha_slider.valueChanged.connect(self._on_analysis_main_alpha_changed)

        self.cache_root = self._default_cache_root()
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.cache_root = Path.home() / ".videolog_cache"
            self.cache_root.mkdir(parents=True, exist_ok=True)
        settings_root = DEFAULT_SETTINGS_PATH.parent
        self.ocr_settings_path = settings_root / "ocr_settings.json"
        self.offset_cache_path = self.cache_root / "ocr_offsets.json"
        self.secondary_offset_cache_path = self.cache_root / "ocr_offsets_additional.json"
        self._load_pinned_annotations()
        self.cache_status_label = QLabel("")
        self.cache_status_label.setStyleSheet("color: #888888;")
        self.cache_status_label.setWordWrap(True)
        self.open_cache_btn = QPushButton("Open Cache Folder")
        self.open_cache_btn.clicked.connect(self.open_cache_folder)
        self.clear_cache_btn = QPushButton("Clear Cache")
        self.clear_cache_btn.clicked.connect(self.clear_cache)
        self.clear_elastic_cache_btn = QPushButton("Clear Event Cache")
        self.clear_elastic_cache_btn.clicked.connect(self.clear_elastic_event_cache)
        self.delete_cache_btn = QPushButton("Delete Current Cache Copy")
        self.delete_cache_btn.clicked.connect(self.delete_current_cache_copy)

        cache_controls_layout = QHBoxLayout()
        cache_controls_layout.addWidget(self.cache_status_label, 1)
        cache_controls_layout.addWidget(self.open_cache_btn)
        cache_controls_layout.addWidget(self.delete_cache_btn)
        cache_controls_layout.addWidget(self.clear_elastic_cache_btn)
        cache_controls_layout.addWidget(self.clear_cache_btn)

        self.playback_layout = QHBoxLayout()
        self.playback_layout.addWidget(self.play_pause_btn)
        self.playback_layout.addWidget(self.annotate_btn)
        self.playback_layout.addWidget(self.tray_view_btn)
        self.playback_layout.addWidget(self.analysis_main_alpha_label)
        self.playback_layout.addWidget(self.analysis_main_alpha_slider)
        # Additional CCTV loads via timeline selection.
        self.playback_layout.addStretch(1)

        # ----- Analysis controls (diff + optical flow) -----
        self.analysis_mode_combo = QComboBox()
        self.analysis_mode_combo.addItems(["Off", "Frame Diff", "Optical Flow"])
        self.analysis_mode_combo.currentIndexChanged.connect(self._on_analysis_mode_changed)

        self.analysis_display_combo = QComboBox()
        self.analysis_display_combo.addItems(["Main Overlay", "Main Side-by-side", "Popout"])
        self.analysis_display_combo.currentIndexChanged.connect(self._on_analysis_display_changed)

        self.analysis_pair_combo = QComboBox()
        self.analysis_pair_combo.addItems(["Reference -> Current", "Previous -> Current"])
        self.analysis_pair_combo.currentIndexChanged.connect(self._update_analysis_view)

        self.analysis_set_ref_btn = QPushButton("Set Reference")
        self.analysis_set_ref_btn.clicked.connect(self._set_analysis_reference)
        self.analysis_clear_ref_btn = QPushButton("Clear Reference")
        self.analysis_clear_ref_btn.clicked.connect(self._clear_analysis_reference)

        self.analysis_heatmap_cb = QCheckBox("Heatmap")
        self.analysis_heatmap_cb.setChecked(True)
        self.analysis_heatmap_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_overlay_cb = QCheckBox("Overlay")
        self.analysis_overlay_cb.setChecked(False)
        self.analysis_overlay_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_arrows_cb = QCheckBox("Flow arrows")
        self.analysis_arrows_cb.setChecked(False)
        self.analysis_arrows_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_arrows_cb.stateChanged.connect(self._update_analysis_controls_state)
        self.analysis_hide_zero_flow_cb = QCheckBox("Hide zero flow")
        self.analysis_hide_zero_flow_cb.setChecked(True)
        self.analysis_hide_zero_flow_cb.stateChanged.connect(self._update_analysis_view)
        self.analysis_hide_zero_flow_cb.stateChanged.connect(self._update_analysis_controls_state)
        self.analysis_zero_flow_label = QLabel("Min flow: 0.00")
        self.analysis_zero_flow_slider = QSlider(Qt.Horizontal)
        self.analysis_zero_flow_slider.setRange(0, 100)
        self.analysis_zero_flow_slider.setValue(1)
        self.analysis_zero_flow_slider.setFixedWidth(140)
        self.analysis_zero_flow_slider.valueChanged.connect(self._on_analysis_zero_flow_changed)
        self._update_analysis_zero_flow_label()

        self.analysis_gain_label = QLabel("Gain: 6x")
        self.analysis_gain_slider = QSlider(Qt.Horizontal)
        self.analysis_gain_slider.setRange(1, 30)
        self.analysis_gain_slider.setValue(6)
        self.analysis_gain_slider.valueChanged.connect(self._on_analysis_gain_changed)

        self.analysis_thresh_label = QLabel("Threshold / Min motion: 15")
        self.analysis_thresh_slider = QSlider(Qt.Horizontal)
        self.analysis_thresh_slider.setRange(0, 255)
        self.analysis_thresh_slider.setValue(15)
        self.analysis_thresh_slider.valueChanged.connect(self._on_analysis_thresh_changed)

        self.analysis_alpha_label = QLabel("Overlay alpha: 0.60")
        self.analysis_alpha_slider = QSlider(Qt.Horizontal)
        self.analysis_alpha_slider.setRange(0, 100)
        self.analysis_alpha_slider.setValue(60)
        self.analysis_alpha_slider.valueChanged.connect(self._on_analysis_alpha_changed)

        self.analysis_scale_label = QLabel("Compute scale: 100%")
        self.analysis_scale_slider = QSlider(Qt.Horizontal)
        self.analysis_scale_slider.setRange(25, 100)
        self.analysis_scale_slider.setValue(100)
        self.analysis_scale_slider.valueChanged.connect(self._on_analysis_scale_changed)

        self.analysis_arrow_step_label = QLabel("Arrow step: 20 px")
        self.analysis_arrow_step_slider = QSlider(Qt.Horizontal)
        self.analysis_arrow_step_slider.setRange(8, 60)
        self.analysis_arrow_step_slider.setValue(20)
        self.analysis_arrow_step_slider.valueChanged.connect(self._on_analysis_arrow_step_changed)

        self.analysis_arrow_scale_label = QLabel("Arrow length scale: 1.5x")
        self.analysis_arrow_scale_slider = QSlider(Qt.Horizontal)
        self.analysis_arrow_scale_slider.setRange(5, 50)
        self.analysis_arrow_scale_slider.setValue(15)
        self.analysis_arrow_scale_slider.valueChanged.connect(self._on_analysis_arrow_scale_changed)

        analysis_row1 = QHBoxLayout()
        analysis_row1.addWidget(QLabel("Analysis:"))
        analysis_row1.addWidget(self.analysis_mode_combo)
        analysis_row1.addSpacing(8)
        analysis_row1.addWidget(QLabel("Display:"))
        analysis_row1.addWidget(self.analysis_display_combo)
        analysis_row1.addStretch(1)

        analysis_row2 = QHBoxLayout()
        analysis_row2.addWidget(QLabel("Pairing:"))
        analysis_row2.addWidget(self.analysis_pair_combo)
        analysis_row2.addSpacing(8)
        analysis_row2.addWidget(self.analysis_set_ref_btn)
        analysis_row2.addWidget(self.analysis_clear_ref_btn)
        analysis_row2.addStretch(1)

        analysis_row3 = QHBoxLayout()
        analysis_row3.addWidget(self.analysis_heatmap_cb)
        analysis_row3.addWidget(self.analysis_overlay_cb)
        analysis_row3.addWidget(self.analysis_arrows_cb)
        analysis_row3.addWidget(self.analysis_hide_zero_flow_cb)
        analysis_row3.addWidget(self.analysis_zero_flow_label)
        analysis_row3.addWidget(self.analysis_zero_flow_slider)
        analysis_row3.addStretch(1)

        analysis_row4 = QHBoxLayout()
        analysis_row4.addWidget(self.analysis_gain_label)
        analysis_row4.addWidget(self.analysis_gain_slider)

        analysis_row5 = QHBoxLayout()
        analysis_row5.addWidget(self.analysis_thresh_label)
        analysis_row5.addWidget(self.analysis_thresh_slider)

        analysis_row6 = QHBoxLayout()
        analysis_row6.addWidget(self.analysis_alpha_label)
        analysis_row6.addWidget(self.analysis_alpha_slider)

        analysis_row7 = QHBoxLayout()
        analysis_row7.addWidget(self.analysis_scale_label)
        analysis_row7.addWidget(self.analysis_scale_slider)

        analysis_row8 = QHBoxLayout()
        analysis_row8.addWidget(self.analysis_arrow_step_label)
        analysis_row8.addWidget(self.analysis_arrow_step_slider)

        analysis_row9 = QHBoxLayout()
        analysis_row9.addWidget(self.analysis_arrow_scale_label)
        analysis_row9.addWidget(self.analysis_arrow_scale_slider)

        self.analysis_label = VideoFrameLabel("Analysis view")
        self.analysis_label.setAlignment(Qt.AlignCenter)
        self.analysis_label.setMinimumSize(480, 220)
        self.analysis_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.analysis_label.set_scrub_callback(self._handle_scroll_wheel)
        self.analysis_label.setVisible(False)
        self._analysis_window: QWidget | None = None
        self._analysis_window_label: VideoFrameLabel | None = None

        self.analysis_controls_panel = QWidget()
        analysis_controls_layout = QVBoxLayout(self.analysis_controls_panel)
        analysis_controls_layout.setContentsMargins(0, 0, 0, 0)
        analysis_controls_layout.setSpacing(6)
        analysis_controls_layout.addLayout(analysis_row1)
        analysis_controls_layout.addLayout(analysis_row2)
        analysis_controls_layout.addLayout(analysis_row3)
        analysis_controls_layout.addLayout(analysis_row4)
        analysis_controls_layout.addLayout(analysis_row5)
        analysis_controls_layout.addLayout(analysis_row6)
        analysis_controls_layout.addLayout(analysis_row7)
        analysis_controls_layout.addLayout(analysis_row8)
        analysis_controls_layout.addLayout(analysis_row9)
        analysis_controls_layout.addStretch(1)
        self.analysis_controls_panel.setMaximumWidth(330)

        for slider in (
            self.analysis_gain_slider,
            self.analysis_thresh_slider,
            self.analysis_alpha_slider,
            self.analysis_scale_slider,
            self.analysis_arrow_step_slider,
            self.analysis_arrow_scale_slider,
        ):
            slider.setFixedWidth(210)

        self.analysis_container = QWidget()
        analysis_layout = QVBoxLayout(self.analysis_container)
        analysis_layout.setContentsMargins(0, 0, 0, 0)
        analysis_layout.addWidget(self.analysis_label, 1)
        self.analysis_container.setVisible(False)
        self._update_analysis_controls_state()
        self._update_analysis_output()

        middle_layout = QVBoxLayout()
        self.event_marker_bar = EventMarkerBar()
        self.timeline_marker_bar = EventMarkerBar()
        self.timeline_marker_bar.set_triangle_red_markers(True)
        lock_row = QHBoxLayout()
        lock_row.addWidget(self.video_sync_btn)
        lock_row.addSpacing(8)
        lock_row.addWidget(self.info_label)
        lock_row.addSpacing(8)
        lock_row.addWidget(self.frame_label)
        lock_row.addSpacing(8)
        lock_row.addWidget(self.calc_label)
        lock_row.addStretch(1)
        lock_row.addWidget(self.secondary_lock_toggle)
        lock_row.addSpacing(8)
        lock_row.addWidget(self.secondary_sync_btn)
        middle_layout.addLayout(lock_row)
        video_row = QHBoxLayout()
        video_row.addWidget(self.video_label, 1)
        video_row.addWidget(self.secondary_video_label, 1)
        video_row.addWidget(self.analysis_label, 1)
        middle_layout.addLayout(video_row)
        middle_layout.addWidget(self.event_marker_bar)
        middle_layout.addWidget(self.seek_slider)
        middle_layout.addWidget(self.timeline_marker_bar)
        middle_layout.addLayout(self.playback_layout)
        QTimer.singleShot(0, self._update_marker_bar_padding)

        self.offset_caption = QLabel("Drift")
        self.offset_caption.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        self.offset_slider = DriftSlider(Qt.Horizontal)
        self.offset_slider.setRange(
            int(self.offset_min * self._offset_slider_scale),
            int(self.offset_max * self._offset_slider_scale),
        )
        self.offset_slider.setSingleStep(int(self.offset_step * self._offset_slider_scale))
        self.offset_slider.setPageStep(int(0.25 * self._offset_slider_scale))
        self.offset_slider.valueChanged.connect(self._on_offset_slider_changed)
        self.offset_display = QLabel("+0.00s")
        self.offset_display.setMinimumWidth(48)
        self.offset_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.offset_display.setStyleSheet("color: #d7dde2; font-family: Consolas, monospace; font-size: 10px;")
        self.playback_layout.addSpacing(8)
        self.playback_layout.addWidget(self.offset_caption)
        self.playback_layout.addWidget(self.offset_slider)
        self.playback_layout.addWidget(self.offset_display)
        self.close_gap_caption = QLabel("Gap")
        self.close_gap_caption.setStyleSheet("color: #9aa0a6; font-size: 10px;")
        self.close_gap_slider = DriftSlider(Qt.Horizontal)
        self.close_gap_slider.setRange(
            int(round(self.close_gap_threshold_min * 100.0)),
            int(round(self.close_gap_threshold_max * 100.0)),
        )
        self.close_gap_slider.setSingleStep(int(round(self.close_gap_threshold_step * 100.0)))
        self.close_gap_slider.setPageStep(10)
        self.close_gap_slider.valueChanged.connect(self._on_close_gap_slider_changed)
        self.close_gap_display = QLabel("0.50x")
        self.close_gap_display.setMinimumWidth(40)
        self.close_gap_display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.close_gap_display.setStyleSheet("color: #d7dde2; font-family: Consolas, monospace; font-size: 10px;")
        self.playback_layout.addSpacing(6)
        self.playback_layout.addWidget(self.close_gap_caption)
        self.playback_layout.addWidget(self.close_gap_slider)
        self.playback_layout.addWidget(self.close_gap_display)
        self._update_close_gap_threshold_display()

        # ----- RIGHT: LOG WINDOW -----

        self.log_label = QLabel("Log entries")
        self._log_model = LogListModel(self)
        self.log_list = QListView()
        self.log_list.setModel(self._log_model)
        self.log_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.log_list.setUniformItemSizes(True)
        self.log_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.log_list.setStyleSheet("""
            QListView::item:selected {
                background-color: #cc2222;
                color: white;
            }
            QListView::item:selected:!active {
                background-color: #882222;
                color: white;
            }
        """)
        self.log_list.clicked.connect(self._on_log_item_clicked)

        self.sync_start_btn = QPushButton("Sync logs to current video (first log)")
        self.sync_start_btn.clicked.connect(self.sync_logs_to_current_video_first_log)
        self.load_logs_btn = QPushButton("Load logs")
        self.load_logs_btn.clicked.connect(self.load_pending_logs)
        self.load_logs_btn.setEnabled(False)

        log_tab = QWidget()
        log_tab_layout = QVBoxLayout(log_tab)
        self.log_tab_layout = log_tab_layout
        log_tab_layout.addWidget(self.log_label)
        log_tab_layout.addWidget(self.log_list)
        log_tab_layout.addWidget(self.load_logs_btn)

        settings_tab = QWidget()
        settings_tab_layout = QVBoxLayout(settings_tab)
        version_label = QLabel(f"Build: {format_version_label()}")
        version_label.setStyleSheet("color: #9aa0a6;")
        settings_tab_layout.addWidget(version_label)
        self.settings_panel = SettingsPanel(self.settings, settings_tab)
        settings_tab_layout.addWidget(self.settings_panel)
        self.save_settings_btn = QPushButton("Save Settings")
        self.save_settings_btn.clicked.connect(self._flush_settings_autosave)
        settings_tab_layout.addWidget(self.save_settings_btn)

        io_settings_layout = QHBoxLayout()
        self.export_settings_btn = QPushButton("Export…")
        self.export_settings_btn.setToolTip(
            "Save filters, conditions and presets to a shareable JSON file. "
            "Your Elastic API key and PikPak parent path are NOT included."
        )
        self.export_settings_btn.clicked.connect(self._on_export_settings)
        self.import_settings_btn = QPushButton("Import…")
        self.import_settings_btn.setToolTip(
            "Load filters, conditions and presets from a shared JSON file."
        )
        self.import_settings_btn.clicked.connect(self._on_import_settings)
        io_settings_layout.addWidget(self.export_settings_btn)
        io_settings_layout.addWidget(self.import_settings_btn)
        settings_tab_layout.addLayout(io_settings_layout)

        systems_tab = QWidget()
        systems_tab_layout = QVBoxLayout(systems_tab)
        self.system_layout_panel = SystemLayoutPanel(self.settings, systems_tab)
        systems_tab_layout.addWidget(self.system_layout_panel)
        settings_tab_layout.addStretch(1)
        settings_tab_layout.addWidget(self.sync_start_btn)
        settings_tab_layout.addLayout(cache_controls_layout)
        settings_tab_layout.addStretch(1)

        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(log_tab, "Logs")
        self.right_tabs.addTab(self.filter_container, "Filters")
        self.right_tabs.addTab(custom_tab, "Custom")
        self.right_tabs.addTab(settings_tab, "Settings")
        self.right_tabs.addTab(systems_tab, "Systems")
        self.right_tabs.addTab(ReadmePanel(), "Readme")
        self._hover_reveal_enabled = True
        self._right_reveal_px = 12
        self._right_tabs_pinned = False
        self._right_tabs_expanded = True
        self._right_tabs_target_width = 510
        self.right_tabs.setMouseTracking(True)
        self.right_tabs.setMinimumWidth(0)
        self.right_tabs.setMaximumWidth(self._right_tabs_target_width)
        self._right_tabs_anim = QVariantAnimation(self)
        self._right_tabs_anim.setDuration(170)
        self._right_tabs_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._right_tabs_anim.valueChanged.connect(self._on_right_tabs_anim_step)

        self._pin_btn = QPushButton("📌")
        self._pin_btn.setCheckable(True)
        self._pin_btn.setFixedSize(26, 22)
        self._pin_btn.setToolTip("Pin panel open")
        self._pin_btn.setStyleSheet(
            "QPushButton { border: none; background: transparent; font-size: 13px; }"
            "QPushButton:checked { background: rgba(255,255,255,30); border-radius: 3px; }"
        )
        self._pin_btn.toggled.connect(self._on_pin_toggled)
        self.right_tabs.setCornerWidget(self._pin_btn, Qt.TopRightCorner)

        # ----- MAIN LAYOUT -----

        root_layout = QHBoxLayout()
        root_layout.addLayout(middle_layout, stretch=3)
        root_layout.addWidget(self.right_tabs, stretch=0)

        self.setLayout(root_layout)
        self.setMinimumSize(980, 560)
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.right_tabs.installEventFilter(self)
        self._update_load_filters_button()
        self._log_busy_dialog: QProgressDialog | None = None
        self._set_filter_tabs_enabled(False)

        self.filter_container_layout.insertWidget(0, preset_container)
        self._load_custom_filter_settings()
        self._load_filter_preset_settings()
        self._startup_maintenance_started = False
        self._settings_autosave_timer = QTimer(self)
        self._settings_autosave_timer.setSingleShot(True)
        self._settings_autosave_timer.setInterval(350)
        self._settings_autosave_timer.timeout.connect(self._save_settings_from_tab)
        self._filter_debounce_timer = QTimer(self)
        self._filter_debounce_timer.setSingleShot(True)
        self._filter_debounce_timer.setInterval(250)
        self._filter_debounce_timer.timeout.connect(self.apply_filters)
        self.settings_panel.changed.connect(self._schedule_settings_autosave)
        self.settings_panel.save_requested.connect(self._flush_settings_autosave)
        self.system_layout_panel.changed.connect(self._schedule_settings_autosave)

        if getattr(self.settings, "log_panel_pinned", False):
            self._pin_btn.setChecked(True)

    def _on_pin_toggled(self, pinned: bool) -> None:
        self._right_tabs_pinned = pinned
        self._hover_reveal_enabled = not pinned
        self._pin_btn.setToolTip("Unpin panel" if pinned else "Pin panel open")
        if pinned:
            self._set_right_tabs_visible(True)
        self.settings.log_panel_pinned = pinned
        self.settings.save()

    def start_background_maintenance(self):
        if self._startup_maintenance_started:
            return
        self._startup_maintenance_started = True
        QTimer.singleShot(0, self.prune_cache_if_needed)

    def _schedule_settings_autosave(self):
        if hasattr(self, "_settings_autosave_timer"):
            self._settings_autosave_timer.start()

    # ---- Sync button label ----

    def eventFilter(self, obj, event):
        if obj is self.video_label and event.type() == QEvent.MouseButtonDblClick:
            self._toggle_video_popout()
            return True
        if obj is self.secondary_video_label and event.type() == QEvent.Wheel:
            self._handle_secondary_scroll_wheel(event.angleDelta().y())
            return True
        if (
            hasattr(self, "video_label")
            and self.video_label is not None
            and obj is getattr(self.video_label, "_tray_view_window", None)
            and event.type() == QEvent.Resize
        ):
            self._refresh_tray_view_if_open()
        if self._hover_reveal_enabled:
            if event.type() == QEvent.MouseMove and obj is self:
                if not self._right_tabs_expanded:
                    pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                    if pos.x() >= self.width() - self._right_reveal_px:
                        self._set_right_tabs_visible(True)
            elif event.type() == QEvent.Leave and obj is self.right_tabs:
                QTimer.singleShot(50, self._auto_hide_right_tabs)
        return super().eventFilter(obj, event)

    def _set_right_tabs_visible(self, visible: bool):
        visible = bool(visible)
        if visible == self._right_tabs_expanded:
            return
        self._right_tabs_expanded = visible
        if self._right_tabs_anim.state() == QVariantAnimation.Running:
            self._right_tabs_anim.stop()
        current = self.right_tabs.width()
        if current <= 0:
            current = 0 if not visible else self._right_tabs_target_width
        end = self._right_tabs_target_width if visible else 0
        self._right_tabs_anim.setStartValue(int(current))
        self._right_tabs_anim.setEndValue(int(end))
        self._right_tabs_anim.start()

    def _on_right_tabs_anim_step(self, value):
        width = max(0, int(value))
        self.right_tabs.setMinimumWidth(width)
        self.right_tabs.setMaximumWidth(width)

    def _auto_hide_right_tabs(self):
        if not self._right_tabs_expanded or self._right_tabs_pinned:
            return
        try:
            current_idx = self.right_tabs.currentIndex()
            current_label = self.right_tabs.tabText(current_idx) if current_idx >= 0 else ""
            if str(current_label).strip().lower() == "systems":
                return
        except Exception:
            pass
        pos = self.right_tabs.mapFromGlobal(self.cursor().pos())
        if not self.right_tabs.rect().contains(pos):
            self._set_right_tabs_visible(False)

    def _update_tray_view_popout(self, tray_view: QImage):
        if tray_view is None or tray_view.isNull():
            return
        if self._tray_view_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Bird's Eye")
            win.resize(320, 320)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            self._tray_view_window = win
            self._tray_view_label = label
            win.destroyed.connect(lambda _=None: self._clear_tray_view_popout())
            win.show()
        if self._tray_view_label is None:
            return
        max_w = 420
        max_h = 420
        scaled = tray_view.scaled(max_w, max_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._tray_view_label.setPixmap(QPixmap.fromImage(scaled))

    # ---- Analysis (diff + optical flow) ----

    def _on_analysis_mode_changed(self, _index: int | None = None):
        enabled = self.analysis_mode_combo.currentText() != "Off"
        self.analysis_display_combo.setEnabled(enabled)
        self.analysis_main_alpha_label.setVisible(enabled)
        self.analysis_main_alpha_slider.setVisible(enabled)
        self._update_analysis_controls_state()
        self._update_analysis_output()
        self._update_analysis_view()

    def _on_analysis_display_changed(self, _index: int | None = None):
        self._update_analysis_output()
        self._update_analysis_view()

    def _update_analysis_output(self):
        enabled = self.analysis_mode_combo.currentText() != "Off"
        if not enabled:
            self.analysis_container.setVisible(False)
            self._hide_analysis_popout()
            self.analysis_label.setVisible(False)
            self._refresh_secondary_visibility()
            self.analysis_main_alpha_label.setVisible(False)
            self.analysis_main_alpha_slider.setVisible(False)
            return
        display = self.analysis_display_combo.currentText()
        show_main_overlay = display == "Main Overlay"
        self.analysis_main_alpha_label.setVisible(show_main_overlay)
        self.analysis_main_alpha_slider.setVisible(show_main_overlay)
        if display == "Popout":
            self.analysis_container.setVisible(False)
            self._show_analysis_popout()
            self.analysis_label.setVisible(False)
            self._refresh_secondary_visibility()
            return
        if display == "Main Side-by-side":
            self._hide_analysis_popout()
            self.analysis_container.setVisible(False)
            self.analysis_label.setVisible(True)
            self._refresh_secondary_visibility()
            return
        # Main Overlay
        self._hide_analysis_popout()
        self.analysis_container.setVisible(False)
        self.analysis_label.setVisible(False)
        self._refresh_secondary_visibility()

    def _update_analysis_controls_state(self, _state: int | None = None):
        mode = self.analysis_mode_combo.currentText()
        is_flow = mode == "Optical Flow"
        is_main_overlay = self.analysis_display_combo.currentText() == "Main Overlay"
        self.analysis_arrows_cb.setEnabled(is_flow)
        self.analysis_hide_zero_flow_cb.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        zero_flow_enabled = (
            is_flow
            and self.analysis_arrows_cb.isChecked()
            and self.analysis_hide_zero_flow_cb.isChecked()
        )
        self.analysis_zero_flow_label.setEnabled(zero_flow_enabled)
        self.analysis_zero_flow_slider.setEnabled(zero_flow_enabled)
        self.analysis_arrow_step_slider.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        self.analysis_arrow_scale_slider.setEnabled(is_flow and self.analysis_arrows_cb.isChecked())
        self.analysis_scale_slider.setEnabled(is_flow)
        self.analysis_arrow_step_label.setEnabled(is_flow)
        self.analysis_arrow_scale_label.setEnabled(is_flow)
        self.analysis_scale_label.setEnabled(is_flow)
        self.analysis_overlay_cb.setEnabled(not is_main_overlay)
        self.analysis_alpha_slider.setEnabled(not is_main_overlay)
        self.analysis_alpha_label.setEnabled(not is_main_overlay)

    def _show_analysis_popout(self):
        if self._analysis_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Analysis View")
            win.resize(800, 450)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            label = VideoFrameLabel("Analysis view")
            label.setAlignment(Qt.AlignCenter)
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            layout.addWidget(label, 1)
            win.setLayout(layout)
            win.destroyed.connect(lambda _=None: self._clear_analysis_popout())
            self._analysis_window = win
            self._analysis_window_label = label
        self._analysis_window.show()

    def _hide_analysis_popout(self):
        if self._analysis_window is not None:
            self._analysis_window.hide()

    def _clear_analysis_popout(self):
        self._analysis_window = None
        self._analysis_window_label = None

    def _refresh_secondary_visibility(self):
        display = self.analysis_display_combo.currentText()
        show_side_by_side = display == "Main Side-by-side" and self.analysis_mode_combo.currentText() != "Off"
        if show_side_by_side:
            self.secondary_video_label.setVisible(False)
            return
        if self._draw_secondary_video and self.secondary_video_label is not None:
            self.secondary_video_label.setVisible(True)
        else:
            self.secondary_video_label.setVisible(False)

    def _on_analysis_main_alpha_changed(self, v: int):
        a = v / 100.0
        self.analysis_main_alpha_label.setText(f"Overlay: {a:.2f}")
        self._request_video_label_update()

    def _set_analysis_reference(self):
        if self.last_frame_rgb is None:
            return
        self.analysis_ref_frame_rgb = self.last_frame_rgb.copy()
        self.analysis_ref_frame_index = int(self.current_frame)
        self._update_analysis_view()

    def _clear_analysis_reference(self):
        self.analysis_ref_frame_rgb = None
        self.analysis_ref_frame_index = None
        self._update_analysis_view()

    def _on_analysis_gain_changed(self, v: int):
        self.analysis_gain_label.setText(f"Gain: {v}x")
        self._update_analysis_view()

    def _on_analysis_thresh_changed(self, v: int):
        self.analysis_thresh_label.setText(f"Threshold / Min motion: {v}")
        self._update_analysis_view()

    def _on_analysis_alpha_changed(self, v: int):
        a = v / 100.0
        self.analysis_alpha_label.setText(f"Overlay alpha: {a:.2f}")
        self._update_analysis_view()

    def _on_analysis_scale_changed(self, v: int):
        self.analysis_scale_label.setText(f"Compute scale: {v}%")
        self._update_analysis_view()

    def _on_analysis_arrow_step_changed(self, v: int):
        self.analysis_arrow_step_label.setText(f"Arrow step: {v} px")
        self._update_analysis_view()

    def _on_analysis_arrow_scale_changed(self, v: int):
        s = v / 10.0
        self.analysis_arrow_scale_label.setText(f"Arrow length scale: {s:.1f}x")
        self._update_analysis_view()

    def _analysis_zero_flow_value(self) -> float:
        return self.analysis_zero_flow_slider.value() / 20.0

    def _update_analysis_zero_flow_label(self):
        v = self._analysis_zero_flow_value()
        self.analysis_zero_flow_label.setText(f"Min flow: {v:.2f}")

    def _on_analysis_zero_flow_changed(self, _v: int):
        self._update_analysis_zero_flow_label()
        self._update_analysis_view()

    def _analysis_base_frame(self) -> tuple[np.ndarray | None, str]:
        pairing = self.analysis_pair_combo.currentText()
        if pairing.startswith("Reference"):
            if self.analysis_ref_frame_rgb is None:
                return None, "Set a reference frame first."
            label = "Reference frame"
            if self.analysis_ref_frame_index is not None:
                label += f": {self.analysis_ref_frame_index}"
            return self.analysis_ref_frame_rgb, label
        if self.analysis_prev_frame_rgb is None:
            return None, "No previous frame yet (scrub at least once)."
        label = "Previous frame"
        if self.analysis_prev_frame_index is not None:
            label += f": {self.analysis_prev_frame_index}"
        return self.analysis_prev_frame_rgb, label

    def _compute_analysis_output(self) -> tuple[np.ndarray | None, str]:
        if self.analysis_mode_combo.currentText() == "Off":
            return None, ""
        if self.last_frame_rgb is None:
            return None, "Analysis view (no frame)"
        base_rgb, base_info = self._analysis_base_frame()
        if base_rgb is None:
            return None, f"Analysis view ({base_info})"

        frame_rgb = self.last_frame_rgb
        if base_rgb.shape != frame_rgb.shape:
            h, w = frame_rgb.shape[:2]
            base_rgb = cv2.resize(base_rgb, (w, h), interpolation=cv2.INTER_AREA)
            base_info = f"{base_info} (resized)"

        gain = float(self.analysis_gain_slider.value())
        thresh = int(self.analysis_thresh_slider.value())
        heatmap = self.analysis_heatmap_cb.isChecked()
        overlay = self.analysis_overlay_cb.isChecked()
        if self.analysis_display_combo.currentText() == "Main Overlay":
            overlay = False
        alpha = self.analysis_alpha_slider.value() / 100.0
        compute_scale = self.analysis_scale_slider.value() / 100.0
        arrows = self.analysis_arrows_cb.isChecked()
        arrow_step = int(self.analysis_arrow_step_slider.value())
        arrow_scale = float(self.analysis_arrow_scale_slider.value()) / 10.0

        mode = self.analysis_mode_combo.currentText()
        if mode == "Frame Diff":
            out_rgb = compute_pixel_diff_view(
                frame_rgb=frame_rgb,
                base_rgb=base_rgb,
                gain=gain,
                threshold=thresh,
                heatmap=heatmap,
                overlay=overlay,
                alpha=alpha,
            )
        else:
            out_rgb = compute_optical_flow_view(
                frame_rgb=frame_rgb,
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
                arrow_min_mag=(
                    self._analysis_zero_flow_value()
                    if self.analysis_hide_zero_flow_cb.isChecked()
                    else None
                ),
            )
        tooltip = f"{mode}\n{base_info}\nCurrent frame: {self.current_frame}"
        return out_rgb, tooltip

    def _update_analysis_view(self, _state: int | None = None):
        if self.analysis_mode_combo.currentText() == "Off":
            self.analysis_label.setText("Analysis view")
            self.analysis_label.setToolTip("")
            self.analysis_label.set_frame(None)
            if self._analysis_window_label is not None:
                self._analysis_window_label.setText("Analysis view")
                self._analysis_window_label.set_frame(None)
            return
        out_rgb, tooltip = self._compute_analysis_output()
        if out_rgb is None:
            msg = tooltip or "Analysis view"
            self.analysis_label.setText(msg)
            self.analysis_label.setToolTip("")
            self.analysis_label.set_frame(None)
            if self._analysis_window_label is not None:
                self._analysis_window_label.setText(msg)
                self._analysis_window_label.setToolTip("")
                self._analysis_window_label.set_frame(None)
            return

        h, w, ch = out_rgb.shape
        bytes_per_line = out_rgb.strides[0]
        qimg = QImage(out_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
        if self.analysis_display_combo.currentText() == "Popout":
            self.analysis_label.set_frame(None)
            self.analysis_label.setToolTip("")
            if self._analysis_window_label is not None:
                self._analysis_window_label.set_frame(qimg)
                self._analysis_window_label.setToolTip(tooltip)
        else:
            self.analysis_label.set_frame(qimg)
            self.analysis_label.setToolTip(tooltip)
            if self._analysis_window_label is not None:
                self._analysis_window_label.set_frame(None)

    def update_sync_button_label(self):
        """Update the sync button text to include the first log time (if known)."""
        if self.first_log_time_str:
            self.sync_start_btn.setText(
                f"Sync logs to current video (first log: {self.first_log_time_str})"
            )
        else:
            self.sync_start_btn.setText("Sync logs to current video (first log)")

    def _save_settings_from_tab(self):
        if not hasattr(self, "settings_panel"):
            return
        if hasattr(self, "_settings_autosave_timer") and self._settings_autosave_timer.isActive():
            self._settings_autosave_timer.stop()
        self.settings_panel.apply_to(self.settings)
        if hasattr(self, "system_layout_panel"):
            self.system_layout_panel.apply_to(self.settings)
        self.settings.save()
        self.settings_saved.emit()

    def _flush_settings_autosave(self):
        if hasattr(self, "_settings_autosave_timer") and self._settings_autosave_timer.isActive():
            self._settings_autosave_timer.stop()
        self._save_settings_from_tab()

    def _on_export_settings(self):
        # Flush any pending edits so the exported file reflects what's on screen.
        self._flush_settings_autosave()
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export settings",
            "logfather-settings.json",
            "JSON files (*.json)",
        )
        if not path_str:
            return
        try:
            self.settings.export_shareable(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export settings:\n{exc}")
            return
        QMessageBox.information(self, "Export complete", f"Settings exported to:\n{path_str}")

    def _on_import_settings(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Import settings",
            "",
            "JSON files (*.json)",
        )
        if not path_str:
            return
        confirm = QMessageBox.question(
            self,
            "Import settings",
            "Importing will replace your current filters, conditions, presets, "
            "customers and system layouts.\n\n"
            "Your Elastic API key and PikPak parent folder will be kept.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.settings.import_shareable(Path(path_str))
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", f"Could not import settings:\n{exc}")
            return
        # Persist immediately and refresh the UI panels so the user sees the
        # imported values without needing to restart.
        self.settings.save()
        if hasattr(self, "settings_panel"):
            self.settings_panel.reload_from_settings()
        if hasattr(self, "system_layout_panel"):
            try:
                self.system_layout_panel.reload_from_settings()
            except AttributeError:
                pass
        self._load_custom_filter_settings()
        self._load_filter_preset_settings()
        QMessageBox.information(
            self,
            "Import complete",
            "Settings imported. Some changes (such as Elastic URL) may only "
            "take effect after reloading data.",
        )

    def _update_sync_button_style(self):
        if hasattr(self, "video_sync_btn"):
            if self._main_sync_done:
                self.video_sync_btn.setStyleSheet("background-color: #2e7d32; color: white;")
            else:
                self.video_sync_btn.setStyleSheet("")
        if hasattr(self, "secondary_sync_btn"):
            if self._secondary_sync_done:
                self.secondary_sync_btn.setStyleSheet("background-color: #2e7d32; color: white;")
            else:
                self.secondary_sync_btn.setStyleSheet("")

    def _set_filter_tabs_enabled(self, enabled: bool):
        if not hasattr(self, "right_tabs"):
            return
        tab_bar = self.right_tabs.tabBar()
        default_color = self.palette().color(QPalette.WindowText)
        disabled_color = QColor("#888888")
        filter_idx = self.right_tabs.indexOf(self.filter_container)
        if filter_idx >= 0:
            self.right_tabs.setTabEnabled(filter_idx, enabled)
            tab_bar.setTabTextColor(filter_idx, default_color if enabled else disabled_color)
        custom_idx = self.right_tabs.indexOf(self._custom_tab)
        if custom_idx >= 0:
            self.right_tabs.setTabEnabled(custom_idx, enabled)
            tab_bar.setTabTextColor(custom_idx, default_color if enabled else disabled_color)

    def _load_custom_filter_settings(self):
        presets = getattr(self.settings, "custom_filters", [])
        if not presets:
            return
        for preset, block in zip(presets, self.custom_filter_blocks):
            btn, in_edit, out_edit, _count_label = block
            if preset.name:
                btn.setText(preset.name)
            in_edit.setText(preset.filter_in or "")
            out_edit.setText(preset.filter_out or "")
            btn.setChecked(bool(preset.enabled))
        self._update_custom_filter_counts()
        self._update_tab_highlights()

    def _save_custom_filter_settings(self):
        presets: list[CustomFilterPreset] = []
        for btn, in_edit, out_edit, _count_label in self.custom_filter_blocks:
            presets.append(
                CustomFilterPreset(
                    name=btn.text(),
                    filter_in=in_edit.text(),
                    filter_out=out_edit.text(),
                    enabled=btn.isChecked(),
                )
            )
        self.settings.custom_filters = presets
        self.settings.save()

    def _load_filter_preset_settings(self):
        presets = getattr(self.settings, "filter_presets", [])
        if not presets:
            return
        for preset, btn in zip(presets, self.filter_preset_group):
            if preset.name:
                btn.setText(preset.name)

    def _save_filter_preset_settings(self):
        presets: list[FilterPreset] = []
        for idx, btn in enumerate(self.filter_preset_group):
            if idx < len(self.settings.filter_presets):
                existing = self.settings.filter_presets[idx]
                presets.append(
                    FilterPreset(
                        name=btn.text(),
                        sources=list(existing.sources),
                        states=list(existing.states),
                        messages=list(existing.messages),
                    )
                )
            else:
                presets.append(FilterPreset(name=btn.text()))
        self.settings.filter_presets = presets
        self.settings.save()

    def set_pending_logs(self, pikpak_path: str, start_iso: str, end_iso: str):
        self.pending_pikpak_path = pikpak_path
        self.pending_start_iso = start_iso
        self.pending_end_iso = end_iso
        self._pending_log_request_key = (str(pikpak_path), str(start_iso), str(end_iso))
        if hasattr(self, "load_logs_btn"):
            self.load_logs_btn.setEnabled(True)
        if self.auto_load_clip_logs:
            self._pending_log_autoload_timer.start()

    def _auto_load_pending_logs(self):
        if not self.pending_pikpak_path or not self.pending_start_iso or not self.pending_end_iso:
            return
        request_key = (str(self.pending_pikpak_path), str(self.pending_start_iso), str(self.pending_end_iso))
        if self._loaded_log_request_key == request_key and self.all_events:
            return
        if self._active_log_request_key == request_key and self._log_future is not None:
            return
        self.load_logs_from_elastic(
            self.pending_pikpak_path,
            self.pending_start_iso,
            self.pending_end_iso,
            show_busy=False,
        )

    def load_pending_logs(self):
        if not self.pending_pikpak_path or not self.pending_start_iso or not self.pending_end_iso:
            QMessageBox.information(self, "No logs", "No pending log range found.")
            return
        self.load_logs_from_elastic(
            self.pending_pikpak_path,
            self.pending_start_iso,
            self.pending_end_iso,
            show_busy=True,
        )

    # ---- ffmpeg rewrap helper ----

    def try_rewrap_video_with_ffmpeg(self, file_path: str) -> str | None:
        """
        Use ffmpeg to losslessly rewrap the video:
        ffmpeg -i input -c copy output
        Returns the new path on success, or None on failure.
        """
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            return None

        in_path = Path(file_path)
        try:
            cache_path = self._cache_path_for(in_path)
        except Exception:
            cache_path = None

        if cache_path is None:
            out_path = in_path.with_name(in_path.stem + "_fixed" + in_path.suffix)
            stage_path = in_path
        else:
            out_path = cache_path
            stage_path = cache_path.with_name(cache_path.stem + "_source" + cache_path.suffix)

        # If we've already created it before, reuse it
        if out_path.exists():
            return str(out_path)

        # Ensure local staged copy before rewrap
        if stage_path != in_path:
            try:
                if stage_path.exists():
                    stage_path.unlink()
                shutil.copy2(in_path, stage_path)
            except Exception as exc:
                QMessageBox.warning(self, "Copy failed", f"Unable to stage video locally:\n{exc}")
                return None

        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(stage_path),
            "-c", "copy",
            str(out_path),
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode == 0:
                print(f"[viewer] video rewrapped with ffmpeg: {out_path.name}", flush=True)
                if stage_path != in_path:
                    stage_path.unlink(missing_ok=True)
                self.update_cache_status()
                return str(out_path)
            else:
                # Uncomment to debug ffmpeg errors:
                # QMessageBox.warning(self, "ffmpeg error", proc.stderr[:500])
                if stage_path != in_path:
                    stage_path.unlink(missing_ok=True)
                return None
        except Exception:
            if stage_path != in_path:
                stage_path.unlink(missing_ok=True)
            return None

    # ---- Video handling ----

    def open_video(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        )
        if not file_path:
            return

        self.load_video_from_path(file_path)

    def open_additional_cctv(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Additional CCTV", "", "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        )
        if not file_path:
            return
        self.load_additional_cctv_from_path(Path(file_path))

    def load_video_from_path(self, file_path: str) -> bool:
        t0 = time.perf_counter()
        print(f"[viewer] load_video_from_path start: {file_path}", flush=True)
        # Supersede any download still pending from a previous clip choice.
        self._video_load_generation += 1
        self._pending_video_load = None
        self._pending_seek = None
        self._set_video_busy(False)
        if self.cap is not None:
            self.cap.release()
            self.cap = None

        path_obj = Path(file_path)
        if not path_obj.exists():
            QMessageBox.warning(self, "File not found", file_path)
            return False

        self.current_video_path = file_path

        # Prefer a local cached copy to avoid network read timeouts/crashes.
        cache_path = None
        try:
            cache_path = self._cache_path_for(path_obj)
        except Exception:
            cache_path = None
        if cache_path is not None and not self._is_cached_copy_current(path_obj, cache_path):
            # Download on the cache executor and finish loading when it lands
            # (_on_prefetch_done) — copying from the CCTV share takes ~15-20s
            # per clip and must not freeze the UI. (Streaming straight from
            # the share while downloading was tried and reverted: too slow.)
            self._video_load_t0 = t0
            self._begin_async_video_download(path_obj, cache_path)
            return True
        if cache_path is not None and cache_path.exists():
            self._touch_cache_entry(cache_path)

        open_path = str(cache_path) if cache_path and cache_path.exists() else file_path
        return self._open_downloaded_video(open_path, path_obj, t0)

    def _begin_async_video_download(self, path_obj: Path, cache_path: Path) -> None:
        key = str(cache_path)
        self._pending_video_load = (self._video_load_generation, path_obj, cache_path)
        self._set_video_busy(True, f"Downloading {path_obj.name} from the CCTV share...")
        if key in self._prefetch_pending:
            # A background prefetch already owns this clip. If it is still
            # queued, cancel it and download on the click executor instead so
            # the user doesn't wait behind the rest of the prefetch queue; if
            # it is actively copying, just reuse it.
            prefetch_future = self._prefetch_futures.get(key)
            if prefetch_future is None or not prefetch_future.cancel():
                return
            self._prefetch_futures.pop(key, None)
        else:
            self._prefetch_pending.add(key)
        future = self._cache_executor.submit(self._copy_to_cache, path_obj, cache_path)
        future.add_done_callback(
            lambda fut, p=path_obj, k=key: self._schedule_prefetch_done(fut, p, k)
        )

    def _set_video_busy(self, busy: bool, message: str | None = None):
        if busy:
            if self._video_busy_dialog is None:
                dlg = QProgressDialog(message or "Working...", None, 0, 0, self)
                dlg.setWindowTitle("Loading clip")
                dlg.setCancelButton(None)
                dlg.setWindowModality(Qt.NonModal)
                dlg.setMinimumDuration(0)
                dlg.setRange(0, 0)
                self._video_busy_dialog = dlg
            self._video_busy_dialog.setLabelText(message or "Working...")
            self._video_busy_dialog.show()
        elif self._video_busy_dialog is not None:
            self._video_busy_dialog.close()
            self._video_busy_dialog = None

    def _finish_pending_video_load(self, source_path: str, ok: bool) -> None:
        pending = self._pending_video_load
        if pending is None:
            return
        generation, p_source, p_cache = pending
        if str(p_source) != source_path:
            return
        self._pending_video_load = None
        self._set_video_busy(False)
        if generation != self._video_load_generation:
            return  # a different clip was chosen while this one downloaded
        print(
            f"[viewer] async cache copy finished (ok={ok}) after "
            f"{time.perf_counter() - self._video_load_t0:.2f}s",
            flush=True,
        )
        open_path = str(p_cache) if ok and p_cache.exists() else str(p_source)
        self._open_downloaded_video(open_path, p_source, self._video_load_t0)

    def _open_downloaded_video(self, open_path: str, path_obj: Path, t0: float) -> bool:
        self.current_video_path = open_path
        t_open = time.perf_counter()
        self.cap = cv2.VideoCapture(open_path)
        if not self.cap.isOpened():
            if self.cap is not None:
                self.cap.release()
            self.cap = None
            # Try to rewrap with ffmpeg only if direct open fails.
            fixed_path = self.try_rewrap_video_with_ffmpeg(open_path)
            if fixed_path:
                self.current_video_path = fixed_path
                self.cap = cv2.VideoCapture(fixed_path)
        if self.cap is None or not self.cap.isOpened():
            QMessageBox.critical(self, "Error", f"Failed to open video:\n{self.current_video_path}")
            if self.cap is not None:
                self.cap.release()
            self.cap = None
            return False
        print(f"[viewer] video opened: {self.current_video_path}", flush=True)
        print(f"[viewer] VideoCapture open took {time.perf_counter() - t_open:.2f}s", flush=True)

        t_meta = time.perf_counter()
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        print(f"[viewer] metadata read took {time.perf_counter() - t_meta:.2f}s", flush=True)
        self.current_frame = 0
        self.time_offset = 0.0
        self.set_offset_value(0.0)
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))
        # Defer first render to the event loop to avoid Qt widget crashes during load.
        if not SKIP_INITIAL_FRAME_RENDER:
            QTimer.singleShot(0, lambda: self.show_frame(self.current_frame))

        # Refresh sync button text (in case a CSV is already loaded)
        self.update_sync_button_label()
        self.update_cache_status()
        self.log_markers_enabled = False
        self._set_log_markers([])
        self.set_timeline_markers([])
        self.video_start_dt = None
        self.ocr_offset_seconds = None
        self.ocr_frame_offset = 0
        self._main_sync_done = False
        self._update_sync_button_style()
        self.video_sync_btn.setEnabled(False)
        self.current_video_filename_dt = parse_filename_datetime(path_obj)
        self.video_sync_btn.setEnabled(True)
        self._update_sync_button_style()
        # Must be set before _load_clip_annotations(): the annotations key is
        # derived from the original share path, and the fallback (the cache
        # copy path, or a stale previous clip) hashes to a different key.
        self.current_video_original_path = path_obj
        self._load_clip_annotations()
        key = self._offset_cache_key(Path(self.current_video_path))
        cached = self._get_cached_offset(key, self.offset_cache_path)
        if cached:
            try:
                self.ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(self.current_video_path)
                if filename_dt:
                    self.video_start_dt = filename_dt + timedelta(seconds=self.ocr_offset_seconds)
                    self._apply_auto_sync_if_possible()
                    self._main_sync_done = True
                    self._update_sync_button_style()
        if self.ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing:
                self._auto_sync_with_ocr()
            elif self.first_log_dt is not None and self._confirm_ocr_sync():
                self._auto_sync_with_ocr()
        pending_seek = self._pending_seek
        self._pending_seek = None
        if pending_seek is not None and pending_seek[0] == self._video_load_generation:
            _, seek_seconds, seek_pause = pending_seek
            QTimer.singleShot(
                0, lambda: self.seek_to_seconds(seek_seconds, pause=seek_pause)
            )
        print(f"[viewer] load_video_from_path total {time.perf_counter() - t0:.2f}s", flush=True)
        return True

    # Prefetch caching disabled (was slowing clip switching)

    def _confirm_ocr_sync(self) -> bool:
        if self.current_video_path:
            key = self._offset_cache_key(Path(self.current_video_path))
            if self._get_cached_offset(key, self.offset_cache_path):
                return True
        settings = Settings.load()
        if not settings.auto_ocr_sync:
            return False
        if self._ocr_sync_prompt_choice is not None:
            return self._ocr_sync_prompt_choice
        remember_cb = QCheckBox("Remember my choice for this session")
        msg = QMessageBox(self)
        msg.setWindowTitle("Auto OCR sync")
        msg.setText("Run OCR time sync for this video?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setCheckBox(remember_cb)
        resp = msg.exec()
        choice = resp == QMessageBox.Yes
        if remember_cb.isChecked():
            self._ocr_sync_prompt_choice = choice
        return choice

    def prepare_for_new_clip(self, show_loading: bool = True):
        self.pause()
        self._cancel_log_future()
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.current_video_path = None
        self.last_qimage = None
        self.last_frame_rgb = None
        self._last_frame_index = None
        self.analysis_prev_frame_rgb = None
        self.analysis_prev_frame_index = None
        if hasattr(self, "analysis_label"):
            self.analysis_label.setText("Analysis view")
            self.analysis_label.setToolTip("")
            self.analysis_label.set_frame(None)
        placeholder = "Loading video..." if show_loading else "No video loaded"
        self.video_label.set_frame(None)
        self.video_label.set_placeholder_text(placeholder)
        if self._popout_label is not None:
            self._popout_label.set_frame(None)
            self._popout_label.set_placeholder_text(placeholder)
        self.seek_slider.setRange(0, 0)
        if hasattr(self.seek_slider, "clear_clip_range"):
            self.seek_slider.clear_clip_range()
        self.current_frame = 0
        self.frame_count = 0
        self.info_label.display("00:00:00.000")
        if hasattr(self, "calc_label"):
            self.calc_label.display("00:00:00.000")
        if hasattr(self, "frame_label"):
            self.frame_label.display("0")
        self.log_markers_enabled = False
        self.log_markers = []
        self.external_markers = []
        self.external_marker_source = None
        self.event_marker_bar.clear()
        if hasattr(self, "timeline_marker_bar"):
            self.timeline_marker_bar.clear()
        self._clip_annotations = []
        self._annotation_history = []
        self._refresh_annotation_view()
        self.events = []
        self._event_start_times: list[float] = []
        self.log_display_rows = []
        self.all_events = []
        self.all_log_display_rows = []
        self.all_source_keys = []
        self.all_state_keys = []
        self.all_message_keys = []
        self._sku_timeline_items = []
        self._rebuild_ppm_model()
        if hasattr(self, "video_label"):
            self.video_label.set_status_lines([])
        if self._popout_label is not None:
            self._popout_label.set_status_lines([])
        self.video_start_dt = None
        self.ocr_offset_seconds = None
        self.ocr_frame_offset = 0
        self._auto_ocr_attempted_key = None
        self.current_video_original_path = None
        self.current_video_filename_dt = None
        self._reset_secondary_video()
        self.pending_pikpak_path = None
        self.pending_start_iso = None
        self.pending_end_iso = None
        self._pending_log_request_key = None
        self._active_log_request_key = None
        self._loaded_log_request_key = None
        self._pending_log_autoload_timer.stop()
        if hasattr(self, "load_logs_btn"):
            self.load_logs_btn.setEnabled(False)
        self.populate_log_list()
        self._reset_filter_state(show_busy=False)
        self._set_log_busy(False)
        if hasattr(self, "filter_panel"):
            self.filter_panel.setVisible(False)
        self._set_filter_tabs_enabled(False)

    def open_csv(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open CSV Log", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not file_path:
            return
        path = Path(file_path)
        try:
            data = load_csv_as_events_and_filters(path)
            self._apply_loaded_events(*data)

            QMessageBox.information(
                self,
                "Logs loaded",
                f"Loaded {len(self.all_events)} log entries from CSV."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load CSV log: {e}")
            self._clear_events()

    def _apply_loaded_events(self, events, display_rows, source_keys, state_keys, message_keys, first_dt):
        print("[viewer] _apply_loaded_events start", flush=True)
        self._set_log_busy(True, "Processing Elastic events...")
        self.all_events = events or []
        self.all_log_display_rows = display_rows or []
        self.all_source_keys = source_keys or []
        self.all_state_keys = state_keys or []
        self.all_message_keys = message_keys or []
        self._rebuild_ppm_model()
        print(f"[viewer] array copies done (events={len(self.all_events)})", flush=True)

        self.sync_offset = 0.0
        self.time_offset = 0.0
        self.set_offset_value(0.0)

        print(f"[viewer] total events: {len(self.all_events)}, display rows: {len(self.all_log_display_rows)}", flush=True)
        self._reset_filter_state()
        self.events = list(self.all_events)
        self.log_display_rows = list(self.all_log_display_rows)
        self._rebuild_event_start_times()
        self.populate_log_list()

        self.first_log_dt = _to_local_naive(first_dt)
        if self.all_log_display_rows:
            first_row = self.all_log_display_rows[0]
            self.first_log_time_str = first_row.split("  |", 1)[0].strip()
        else:
            self.first_log_time_str = None
        self.update_sync_button_label()
        self._set_log_busy(False)
        self._update_timeline_markers()
        if not self.filters_loaded:
            self.load_filters_panel()
        self.apply_filters(manage_busy=False)
        self._apply_auto_sync_if_possible()
        if self.current_video_path and self.ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing or self._confirm_ocr_sync():
                self._auto_sync_with_ocr()
        self._update_tab_highlights()
        self._set_filter_tabs_enabled(True)

    def set_timeline_markers(self, markers: list[tuple[float, str]] | None, source: str | None = None):
        markers = markers or []
        self.external_markers = markers
        self.external_marker_source = source or "clip_relative"
        self._refresh_timeline_marker_bar()

    def set_clip_marker_fallback(self, markers: list[tuple[float, str]] | None):
        """Populate the lower marker bar until clip logs are loaded and synced."""
        if self.events or self.cap is None:
            return
        self._set_log_markers(markers or [])

    def _set_log_markers(self, markers: list[tuple[float, str]] | None):
        markers = markers or []
        self.log_markers = markers
        if markers:
            self.log_markers_enabled = True
        self._refresh_marker_bar()

    def _refresh_marker_bar(self):
        duration = 0.0
        if self.fps and self.fps > 0:
            duration = (self.frame_count or 0) / self.fps
        if (
            duration <= 0.0
            or not self.log_markers
            or not self.log_markers_enabled
        ):
            self.event_marker_bar.set_markers([])
            return
        ratios: list[tuple[float, str]] = []
        for offset, color in self.log_markers:
            try:
                offset_val = float(offset)
            except (TypeError, ValueError):
                continue
            if offset_val < 0.0 or offset_val > duration:
                continue
            ratio = offset_val / duration
            ratios.append((ratio, color))
        self.event_marker_bar.set_markers(ratios)

    def _refresh_timeline_marker_bar(self):
        duration = 0.0
        if self.fps and self.fps > 0:
            duration = (self.frame_count or 0) / self.fps
        if duration <= 0.0 or not self.external_markers:
            if hasattr(self, "timeline_marker_bar"):
                self.timeline_marker_bar.set_markers([])
            return
        ratios: list[tuple[float, str]] = []
        offset_adjust = 0.0
        if self.external_marker_source in {"absolute", "clip_relative"} and self.ocr_offset_seconds is not None:
            offset_adjust = -float(self.ocr_offset_seconds)
        for offset, color in self.external_markers:
            try:
                offset_val = float(offset) + offset_adjust
            except (TypeError, ValueError):
                continue
            if offset_val < 0.0 or offset_val > duration:
                continue
            ratio = offset_val / duration
            ratios.append((ratio, color))
        if hasattr(self, "timeline_marker_bar"):
            self.timeline_marker_bar.set_markers(ratios)

    def _rebuild_event_start_times(self) -> None:
        self._event_start_times = [ev.start.total_seconds() for ev in self.events]

    def _clear_events(self):
        self.all_events = []
        self.all_log_display_rows = []
        self.all_source_keys = []
        self.all_state_keys = []
        self.all_message_keys = []
        self._rebuild_ppm_model()
        self.events = []
        self._event_start_times = []
        self.log_display_rows = []
        self.populate_log_list()
        self._reset_filter_state(show_busy=False)
        self.first_log_time_str = None
        self.first_log_dt = None
        self.update_sync_button_label()
        self._set_log_busy(False)
        self.log_markers_enabled = False
        self.log_markers = []
        self._refresh_marker_bar()
        self._set_filter_tabs_enabled(False)

    # ---- Filter UI helpers ----
    # (unchanged from your version)

    def clear_filter_checkboxes(self, show_busy: bool = True):
        if show_busy:
            self._set_log_busy(True, "Resetting source filters...")
        self._reset_source_panel()
        if show_busy:
            QApplication.processEvents()
            self._set_log_busy(True, "Resetting state filters...")
        self._reset_state_panel()
        if show_busy:
            QApplication.processEvents()
            self._set_log_busy(True, "Resetting message filters...")
        self._reset_message_panel()
        if show_busy:
            QApplication.processEvents()
        self.source_checkboxes.clear()
        self.state_checkboxes.clear()
        self.message_checkboxes.clear()
        self._update_load_filters_button()
        if show_busy:
            self._set_log_busy(False)

    def _reset_filter_state(self, show_busy: bool = False):
        self.filters_loaded = False
        self.clear_filter_checkboxes(show_busy=show_busy)
        self._update_load_filters_button()
        if hasattr(self, "filter_panel"):
            self.filter_panel.setVisible(False)

    def _reset_source_panel(self):
        print("[viewer] resetting source panel", flush=True)
        if self.source_container_widget is not None:
            self.source_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.source_container_widget = new_widget
        self.source_layout_inner = new_layout
        self.source_scroll.setWidget(new_widget)

    def _reset_message_panel(self):
        print("[viewer] resetting message panel", flush=True)
        if self.message_container_widget is not None:
            self.message_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.message_container_widget = new_widget
        self.message_layout_inner = new_layout
        self.message_scroll.setWidget(new_widget)

    def _reset_state_panel(self):
        print("[viewer] resetting state panel", flush=True)
        if self.state_container_widget is not None:
            self.state_container_widget.deleteLater()
        new_widget = QWidget()
        new_layout = QVBoxLayout(new_widget)
        new_layout.addStretch(1)
        self.state_container_widget = new_widget
        self.state_layout_inner = new_layout
        self.state_scroll.setWidget(new_widget)

    def _update_load_filters_button(self):
        return

    def build_filter_checkboxes(self):
        if not self.filters_loaded:
            return
        self.clear_filter_checkboxes()

        # ----- Sources: build once from all rows -----
        unique_sources = sorted({k for k in self.all_source_keys if k})
        if unique_sources:
            last = self.source_layout_inner.takeAt(self.source_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_sources:
                cb = QCheckBox(key)
                cb.setChecked(True)
                cb.stateChanged.connect(self.on_source_checkbox_changed)
                self.source_layout_inner.addWidget(cb)
                self.source_checkboxes[key] = cb

            self.source_layout_inner.addStretch(1)

        # ----- States: build once from all rows -----
        unique_states = sorted({k if k else "(null)" for k in self.all_state_keys})
        if unique_states:
            last = self.state_layout_inner.takeAt(self.state_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_states:
                cb = QCheckBox(key)
                cb.setChecked(True)
                cb.stateChanged.connect(self.on_state_checkbox_changed)
                self.state_layout_inner.addWidget(cb)
                self.state_checkboxes[key] = cb

            self.state_layout_inner.addStretch(1)

        # ----- Messages: build once from all rows -----
        unique_messages = sorted({k for k in self.all_message_keys if k})
        if unique_messages:
            last = self.message_layout_inner.takeAt(self.message_layout_inner.count() - 1)
            if last is not None and last.widget() is not None:
                last.widget().setParent(None)

            for key in unique_messages:
                cb = QCheckBox(key)
                cb.setChecked(True)
                cb.stateChanged.connect(self.on_message_checkbox_changed)
                self.message_layout_inner.addWidget(cb)
                self.message_checkboxes[key] = cb

        self.message_layout_inner.addStretch(1)

        self.update_message_visibility_from_filters()

    def load_filters_panel(self):
        if not self.all_events:
            QMessageBox.information(self, "No logs", "Load a video/logs before loading filters.")
            return
        if self.filters_loaded:
            QMessageBox.information(self, "Filters already loaded", "Filters are already available.")
            return
        self.filters_loaded = True
        self._set_log_busy(True, "Resetting filters...")
        self.clear_filter_checkboxes(show_busy=False)
        self._set_log_busy(True, "Building filter lists...")
        self.build_filter_checkboxes()
        self._set_log_busy(True, "Applying filters and refreshing log list...")
        self.apply_filters(status_message="Applying filters...", manage_busy=False)
        self._set_log_busy(False)
        if hasattr(self, "filter_panel"):
            self.filter_panel.setVisible(True)
        print(
            f"[viewer] filter checkboxes built (sources={len(self.source_checkboxes)}, "
            f"states={len(self.state_checkboxes)}, messages={len(self.message_checkboxes)})",
            flush=True,
        )
        self._update_load_filters_button()

    def update_message_visibility_from_filters(self):
        if (
            not self.filters_loaded
            or not self.all_events
            or not self.message_checkboxes
            or not self.state_checkboxes
        ):
            return

        source_filter_active = bool(self.source_checkboxes)
        state_filter_active = bool(self.state_checkboxes)
        include_empty_state = True
        allowed_sources = {
            key for key, cb in self.source_checkboxes.items() if cb.isChecked()
        } if source_filter_active else set()
        allowed_states = {
            key for key, cb in self.state_checkboxes.items() if cb.isChecked()
        } if state_filter_active else set()

        states_used = set()
        messages_used = set()
        for src, state, msg in zip(self.all_source_keys, self.all_state_keys, self.all_message_keys):
            if source_filter_active and src not in allowed_sources:
                continue
            state_val = state if state else "(null)"
            states_used.add(state_val)
            if state_filter_active and state_val not in allowed_states:
                continue
            if msg:
                messages_used.add(msg)

        for state_val, cb in self.state_checkboxes.items():
            if state_val == "(null)":
                cb.setVisible(True)
            else:
                cb.setVisible(state_val in states_used)
        for msg_val, cb in self.message_checkboxes.items():
            cb.setVisible(msg_val in messages_used)

    def on_source_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def on_state_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def on_message_checkbox_changed(self, _state):
        if not self.filters_loaded:
            return
        self._clear_active_filter_preset()
        self.apply_filters()

    def select_all_sources(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_no_sources(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_all_states(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.state_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_no_states(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.state_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def select_all_messages(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.apply_filters()

    def select_no_messages(self):
        if not self.filters_loaded:
            return
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.apply_filters()

    def apply_filters(self, status_message: str | None = None, manage_busy: bool = True):
        print("[viewer] apply_filters start", flush=True)
        if not self.all_events:
            print("[viewer] apply_filters no events", flush=True)
            if manage_busy:
                self._set_log_busy(False)
            return
        if manage_busy:
            self._set_log_busy(True, status_message or "Applying filters...")

        if self.active_filter_presets:
            base_rows = self._collect_preset_filtered_rows()
        else:
            base_rows = self._collect_base_filtered_rows()
        self.events = []
        self.log_display_rows = []

        custom_filters = self._get_active_custom_filters()
        custom_mode = "OR"

        for ev, row_text in base_rows:
            if custom_filters:
                text = row_text
                if "  |  " in row_text:
                    text = row_text.split("  |  ", 1)[1]
                if not self._custom_filter_match(text, custom_filters, custom_mode):
                    continue
            self.events.append(ev)
            self.log_display_rows.append(row_text)

        self._rebuild_event_start_times()
        self.populate_log_list()

        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self._update_custom_filter_counts()
        self._update_timeline_markers()
        self._update_tab_highlights()
        if manage_busy:
            self._set_log_busy(False)
        print("[viewer] apply_filters done", flush=True)

    def _set_all_filters_checked(self):
        for cb in self.source_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        for cb in self.state_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        for cb in self.message_checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()

    def _collect_base_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        if not self.filters_loaded:
            return list(zip(self.all_events, self.all_log_display_rows))
        allowed_sources = {
            key for key, cb in self.source_checkboxes.items() if cb.isChecked()
        }
        allowed_states = {
            key for key, cb in self.state_checkboxes.items() if cb.isChecked()
        }
        allowed_messages = {
            key for key, cb in self.message_checkboxes.items()
            if cb.isChecked() and cb.isVisible()
        }

        source_filter_active = bool(self.source_checkboxes)
        state_filter_active = bool(self.state_checkboxes)
        message_filter_active = any(cb.isVisible() for cb in self.message_checkboxes.values())

        rows: list[tuple[LogEvent, str]] = []
        for ev, row_text, src, state, msg in zip(
            self.all_events,
            self.all_log_display_rows,
            self.all_source_keys,
            self.all_state_keys,
            self.all_message_keys,
        ):
            state_val = state if state else "(null)"
            if source_filter_active and src not in allowed_sources:
                continue
            if state_filter_active and state_val not in allowed_states:
                continue
            if message_filter_active and msg not in allowed_messages:
                continue
            rows.append((ev, row_text))
        return rows

    def _collect_preset_filtered_rows(self) -> list[tuple[LogEvent, str]]:
        presets = getattr(self.settings, "filter_presets", [])
        if not presets or not self.active_filter_presets:
            return list(zip(self.all_events, self.all_log_display_rows))
        active = [presets[i] for i in sorted(self.active_filter_presets) if i < len(presets)]
        rows: list[tuple[LogEvent, str]] = []
        for ev, row_text, src, state, msg in zip(
            self.all_events,
            self.all_log_display_rows,
            self.all_source_keys,
            self.all_state_keys,
            self.all_message_keys,
        ):
            state_val = state if state else "(null)"
            matched = False
            for preset in active:
                if preset.sources and src not in preset.sources:
                    continue
                if preset.states and state_val not in preset.states:
                    continue
                if preset.messages and msg not in preset.messages:
                    continue
                if not preset.sources and not preset.states and not preset.messages:
                    continue
                matched = True
                break
            if matched:
                rows.append((ev, row_text))
        return rows

    def _parse_custom_terms(self, text: str) -> list[str]:
        return [term.strip().lower() for term in text.split(",") if term.strip()]

    def _get_active_custom_filters(self) -> list[tuple[list[str], list[str]]]:
        filters: list[tuple[list[str], list[str]]] = []
        for btn, in_edit, out_edit, _count_label in getattr(self, "custom_filter_blocks", []):
            if not btn.isChecked():
                continue
            in_terms = self._parse_custom_terms(in_edit.text())
            out_terms = self._parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                continue
            filters.append((in_terms, out_terms))
        return filters

    def _custom_filter_match(
        self,
        text: str,
        filters: list[tuple[list[str], list[str]]],
        mode: str,
    ) -> bool:
        if not filters:
            return True
        text_l = text.lower()
        for in_terms, out_terms in filters:
            include_ok = True
            if in_terms:
                if mode == "AND":
                    include_ok = all(term in text_l for term in in_terms)
                else:
                    include_ok = any(term in text_l for term in in_terms)
            if not include_ok:
                continue
            if out_terms and any(term in text_l for term in out_terms):
                continue
            return True
        return False

    def _on_custom_filter_changed(self, *_args):
        self._update_tab_highlights()
        if not self.all_events:
            return
        self.apply_filters()

    def _on_custom_filter_text_changed(self, button: QPushButton):
        if not self.all_events:
            return
        if not button.isChecked():
            return
        self._filter_debounce_timer.start()

    def _clear_active_filter_preset(self):
        if self.active_filter_preset_index is None and not self.active_filter_presets:
            return
        for btn in self.filter_preset_group:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        self.active_filter_preset_index = None
        self.active_filter_presets.clear()

    def _validate_custom_filter_inputs(self):
        for _btn, in_edit, out_edit, _count_label in getattr(self, "custom_filter_blocks", []):
            for edit in (in_edit, out_edit):
                text = edit.text()
                has_empty = text.strip().startswith(",") or text.strip().endswith(",") or ",," in text
                if has_empty:
                    edit.setStyleSheet("border: 1px solid #cc8800;")
                    edit.setToolTip("Empty entries will be ignored.")
                else:
                    edit.setStyleSheet("")
                    edit.setToolTip("")

    def _rename_custom_preset(self, button: QPushButton):
        menu = QMenu(self)
        action = menu.addAction("Rename")
        chosen = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        if chosen != action:
            return
        text, ok = QInputDialog.getText(self, "Rename preset", "Preset name:", text=button.text())
        if ok and text.strip():
            button.setText(text.strip())

    def _on_custom_filter_menu(self, index: int):
        if index < 0 or index >= len(self.custom_filter_blocks):
            return
        btn, _in_edit, _out_edit, _count_label = self.custom_filter_blocks[index]
        menu = QMenu(self)
        save_action = menu.addAction("Save current selection")
        rename_action = menu.addAction("Rename")
        chosen = menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        if chosen == rename_action:
            text, ok = QInputDialog.getText(self, "Rename preset", "Preset name:", text=btn.text())
            if ok and text.strip():
                btn.setText(text.strip())
                self._save_custom_filter_settings()
        elif chosen == save_action:
            self._save_custom_filter_settings()

    def _update_custom_filter_counts(self):
        if not self.all_events:
            for _btn, _in_edit, _out_edit, count_label in getattr(self, "custom_filter_blocks", []):
                count_label.setText("Matches: -")
            return
        base_rows = self._collect_base_filtered_rows()
        mode = "OR"
        for idx, (btn, in_edit, out_edit, count_label) in enumerate(self.custom_filter_blocks, start=1):
            in_terms = self._parse_custom_terms(in_edit.text())
            out_terms = self._parse_custom_terms(out_edit.text())
            if not in_terms and not out_terms:
                count_label.setText("Matches: 0")
                continue
            custom_filters = [(in_terms, out_terms)]
            match_count = 0
            for _ev, row_text in base_rows:
                text = row_text.split("  |  ", 1)[1] if "  |  " in row_text else row_text
                if self._custom_filter_match(text, custom_filters, mode):
                    match_count += 1
            count_label.setText(f"Matches: {match_count}")

    def _on_filter_preset_clicked(self, index: int):
        if index < 0 or index >= len(self.filter_preset_group):
            return
        btn = self.filter_preset_group[index]
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            if btn.isChecked():
                self.active_filter_presets.add(index)
            else:
                self.active_filter_presets.discard(index)
            self.active_filter_preset_index = None
            for idx, b in enumerate(self.filter_preset_group):
                b.blockSignals(True)
                b.setChecked(idx in self.active_filter_presets)
                b.blockSignals(False)
            if not self.active_filter_presets:
                self._set_all_filters_checked()
            self.apply_filters()
            return
        if not btn.isChecked():
            self.active_filter_preset_index = None
            self.active_filter_presets.clear()
            self._set_all_filters_checked()
            self.apply_filters()
            return
        for idx, b in enumerate(self.filter_preset_group):
            b.blockSignals(True)
            b.setChecked(idx == index)
            b.blockSignals(False)
        self.active_filter_presets = {index}
        self.active_filter_preset_index = index
        self._apply_filter_preset(index)

    def _apply_filter_preset(self, index: int):
        if not self.all_events:
            return
        if not self.filters_loaded:
            self.load_filters_panel()
        if not self.filters_loaded:
            return
        presets = getattr(self.settings, "filter_presets", [])
        if index < 0 or index >= len(presets):
            return
        preset = presets[index]
        for key, cb in self.source_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.sources)
            cb.blockSignals(False)
        for key, cb in self.state_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.states)
            cb.blockSignals(False)
        for key, cb in self.message_checkboxes.items():
            cb.blockSignals(True)
            cb.setChecked(key in preset.messages)
            cb.blockSignals(False)
        self.update_message_visibility_from_filters()
        self.apply_filters()

    def _on_filter_preset_menu(self, index: int):
        if index < 0 or index >= len(self.filter_preset_group):
            return
        menu = QMenu(self)
        save_action = menu.addAction("Save current selection")
        rename_action = menu.addAction("Rename")
        chosen = menu.exec(self.filter_preset_group[index].mapToGlobal(
            self.filter_preset_group[index].rect().bottomLeft()
        ))
        if chosen == rename_action:
            text, ok = QInputDialog.getText(
                self, "Rename preset", "Preset name:", text=self.filter_preset_group[index].text()
            )
            if ok and text.strip():
                self.filter_preset_group[index].setText(text.strip())
                self._save_filter_preset_settings()
        elif chosen == save_action:
            self._save_current_filter_selection(index)

    def _save_current_filter_selection(self, index: int):
        if not self.filters_loaded:
            return
        if index < 0 or index >= len(self.filter_preset_group):
            return
        sources = [k for k, cb in self.source_checkboxes.items() if cb.isChecked()]
        states = [k for k, cb in self.state_checkboxes.items() if cb.isChecked()]
        messages = [k for k, cb in self.message_checkboxes.items() if cb.isChecked() and cb.isVisible()]
        presets = getattr(self.settings, "filter_presets", [])
        while len(presets) < 15:
            presets.append(FilterPreset(name=f"Preset {len(presets) + 1}"))
        presets[index] = FilterPreset(
            name=self.filter_preset_group[index].text(),
            sources=sources,
            states=states,
            messages=messages,
        )
        self.settings.filter_presets = presets
        self.settings.save()

    def _update_tab_highlights(self):
        if not hasattr(self, "right_tabs"):
            return
        highlight = QColor("#ff4d4f")
        default_color = QApplication.palette().windowText().color()
        disabled_color = QColor("#888888")
        tab_bar = self.right_tabs.tabBar()
        # Filters tab highlight
        filter_idx = self.right_tabs.indexOf(self.filter_container)
        if filter_idx >= 0:
            if not self.right_tabs.isTabEnabled(filter_idx):
                self.right_tabs.setTabText(filter_idx, "Filters")
                tab_bar.setTabTextColor(filter_idx, disabled_color)
            else:
                active = False
                if self.active_filter_presets:
                    active = True
                if self.filters_loaded:
                    for cb in self.source_checkboxes.values():
                        if not cb.isChecked():
                            active = True
                            break
                    if not active:
                        for cb in self.state_checkboxes.values():
                            if not cb.isChecked():
                                active = True
                                break
                    if not active:
                        for cb in self.message_checkboxes.values():
                            if not cb.isChecked():
                                active = True
                                break
                self.right_tabs.setTabText(filter_idx, "Filters")
                tab_bar.setTabTextColor(filter_idx, highlight if active else default_color)
        # Custom tab highlight
        custom_idx = self.right_tabs.indexOf(self._custom_tab)
        if custom_idx >= 0:
            if not self.right_tabs.isTabEnabled(custom_idx):
                self.right_tabs.setTabText(custom_idx, "Custom")
                tab_bar.setTabTextColor(custom_idx, disabled_color)
            else:
                custom_active = any(btn.isChecked() for btn, _in, _out, _count in self.custom_filter_blocks)
                self.right_tabs.setTabText(custom_idx, "Custom")
                tab_bar.setTabTextColor(custom_idx, highlight if custom_active else default_color)

    def _maybe_save_active_filter_preset(self):
        if self.active_filter_preset_index is None:
            return
        if not self.filters_loaded:
            return
        self._save_current_filter_selection(self.active_filter_preset_index)

    # ---- Log list ----

    def populate_log_list(self):
        print(f"[viewer] populate_log_list start (rows={len(self.log_display_rows)})", flush=True)
        self._log_model.reset_data(self.log_display_rows)
        print("[viewer] populate_log_list done", flush=True)

    def _event_seconds_to_video_seconds(self, event_seconds: float) -> float:
        t = float(event_seconds) + self.effective_offset()
        if self.fps > 0 and self.ocr_frame_offset:
            t -= float(self.ocr_frame_offset) / float(self.fps)
        return max(0.0, t)

    def _on_log_item_clicked(self, index: QModelIndex):
        if self.cap is None or not self.events:
            return
        row = index.row()
        if row < 0 or row >= len(self.events):
            return

        ev = self.events[row]
        t = self._event_seconds_to_video_seconds(ev.start.total_seconds())

        frame = int(round(t * self.fps)) if self.fps > 0 else 0
        if self.frame_count > 0:
            frame = max(0, min(self.frame_count - 1, frame))

        self.pause()
        self.current_frame = frame
        self.show_frame(self.current_frame)

    def add_playback_right_widget(self, widget: QWidget):
        if widget is None:
            return
        if not hasattr(self, "playback_layout") or self.playback_layout is None:
            return
        self.playback_layout.addWidget(widget)

    @staticmethod
    def _apply_segment_font(label: QLabel, point_size: int = 12):
        # Prefer common seven-segment style fonts when available.
        preferred = (
            "DSEG7 Classic",
            "Digital-7 Mono",
            "Digital-7",
            "DS-Digital",
            "Seven Segment",
        )
        available = {name.lower(): name for name in QFontDatabase().families()}
        family = None
        for name in preferred:
            key = name.lower()
            if key in available:
                family = available[key]
                break
        if family is None:
            family = "Consolas"
        font = QFont(family)
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(point_size)
        label.setFont(font)

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

    def _handle_scroll_wheel(self, delta_steps: int):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ControlModifier:
            step = 1 if delta_steps > 0 else -1
            for _ in range(abs(delta_steps)):
                self._jump_to_adjacent_event(step)
        elif modifiers & Qt.ShiftModifier:
            seconds = 1 if delta_steps > 0 else -1
            frames = int(round(seconds * self.fps)) if self.fps > 0 else seconds
            self.scrub_by_frames(frames)
        else:
            self.scrub_by_frames(delta_steps)


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

    def _handle_secondary_scroll_wheel(self, delta_steps: int):
        if self.secondary_cap is None or self.secondary_fps <= 0:
            return
        if self.secondary_locked:
            modifiers = QApplication.keyboardModifiers()
            if abs(delta_steps) > 1:
                steps = max(1, int(round(abs(delta_steps) / 120)))
            else:
                steps = 1
            step = -1 if delta_steps > 0 else 1
            if modifiers & Qt.ControlModifier:
                for _ in range(steps):
                    self._jump_to_adjacent_event(step)
            elif modifiers & Qt.ShiftModifier:
                seconds = step * steps
                frames = int(round(seconds * self.fps)) if self.fps > 0 else seconds
                self.scrub_by_frames(frames)
            else:
                self.scrub_by_frames(step * steps)
            return
        if abs(delta_steps) > 1:
            steps = max(1, int(round(abs(delta_steps) / 120)))
        else:
            steps = 1
        step = -1 if delta_steps > 0 else 1
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            frames_per_step = int(round(self.secondary_fps))
            if frames_per_step <= 0:
                frames_per_step = 1
            self.secondary_manual_offset_frames += step * frames_per_step * steps
        else:
            for _ in range(steps):
                self.secondary_manual_offset_frames += step
        t = self.current_frame / self.fps if self.fps > 0 else 0.0
        self._update_secondary_frame_for_time(t)
        self._request_video_label_update()

    def _update_secondary_lock_style(self):
        if self.secondary_cap is None:
            self.secondary_lock_toggle.setStyleSheet("color: #888888;")
            return
        if self.secondary_locked:
            self.secondary_lock_toggle.setStyleSheet("color: #2ecc71;")
        else:
            self.secondary_lock_toggle.setStyleSheet("color: #ff4d4f;")

    def _toggle_secondary_lock(self, _event):
        if self.secondary_cap is None:
            return
        if not self.secondary_lock_toggle.isEnabled():
            return
        self.secondary_locked = not self.secondary_locked
        self._update_secondary_lock_style()

    def _grab_annotated_frame_pixmap(self) -> QPixmap | None:
        if self.video_label is None or not self.video_label.isVisible():
            return None
        rect = None
        if hasattr(self.video_label, "_image_rect"):
            try:
                rect = self.video_label._image_rect()
            except Exception:
                rect = None
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return self.video_label.grab()
        return self.video_label.grab(rect)

    def _copy_main_frame_to_clipboard(self, _pos):
        if self.video_label is not None and self.video_label.isVisible():
            try:
                pixmap = self._grab_annotated_frame_pixmap()
                if pixmap is not None and not pixmap.isNull():
                    QApplication.clipboard().setPixmap(pixmap)
                    QMessageBox.information(self, "Copied", "Annotated frame copied to clipboard.")
                    return
            except Exception:
                pass
        if self.last_qimage is None:
            return
        QApplication.clipboard().setImage(self.last_qimage)
        QMessageBox.information(self, "Copied", "Main frame copied to clipboard.")

    def _copy_secondary_frame_to_clipboard(self, _pos):
        if self.secondary_last_qimage is None:
            return
        QApplication.clipboard().setImage(self.secondary_last_qimage)
        QMessageBox.information(self, "Copied", "Additional CCTV frame copied to clipboard.")

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

    def seek_to_seconds(self, seconds: float, pause: bool = True):
        if self.cap is None:
            # The clip may still be downloading; replay the seek once it opens.
            if self._pending_video_load is not None:
                self._pending_seek = (
                    self._pending_video_load[0], float(seconds), bool(pause)
                )
            return
        if self.fps <= 0:
            return
        if pause:
            self.pause()
        target = max(0, float(seconds))
        frame = int(round(target * self.fps))
        if self.frame_count > 0:
            frame = max(0, min(self.frame_count - 1, frame))
        self.current_frame = frame
        self.show_frame(self.current_frame)

    def _emit_seek_range_export_requested(self, start_frame: int, end_frame: int):
        if self.fps <= 0:
            return
        start_seconds = max(0.0, float(start_frame) / float(self.fps))
        end_seconds = max(0.0, float(end_frame) / float(self.fps))
        self.clip_range_export_requested.emit(start_seconds, end_seconds)

    def export_current_clip_with_overlays(
        self,
        source_path: Path,
        start_seconds: float,
        end_seconds: float,
        target_path: Path,
    ) -> tuple[bool, str]:
        if end_seconds <= start_seconds:
            return False, "Select a non-zero clip range first."
        if self.fps <= 0:
            return False, "No loaded clip is available for export."
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path is None:
            return False, "ffmpeg was not found on PATH."
        cap = cv2.VideoCapture(str(source_path))
        if not cap.isOpened():
            return False, f"Failed to open source clip:\n{source_path}"
        fps = cap.get(cv2.CAP_PROP_FPS) or self.fps or 25.0
        start_frame = max(0, int(round(start_seconds * fps)))
        end_frame = max(start_frame + 1, int(round(end_seconds * fps)))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if width <= 0 or height <= 0:
            cap.release()
            return False, "Unable to determine clip dimensions for export."
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        temp_dir = Path(tempfile.mkdtemp(prefix="logfather_export_"))
        temp_video = temp_dir / "video_no_audio.mp4"
        writer = cv2.VideoWriter(str(temp_video), fourcc, fps, (width, height))
        if not writer.isOpened():
            cap.release()
            return False, "Unable to create temporary export video."

        export_widget = AnnotatedVideoWidget()
        export_widget.resize(width, height)
        export_widget.set_editable(False)
        export_widget.set_fps(fps)
        export_widget.set_annotations(self._current_annotations())

        progress = QProgressDialog("Exporting clip with overlays...", "Cancel", 0, max(1, end_frame - start_frame), self)
        progress.setWindowTitle("Export Clip")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
        try:
            for frame_idx in range(start_frame, end_frame):
                if progress.wasCanceled():
                    writer.release()
                    cap.release()
                    try:
                        temp_video.unlink(missing_ok=True)
                        temp_dir.rmdir()
                    except Exception:
                        pass
                    return False, "Export canceled."
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                if not frame_rgb.flags["C_CONTIGUOUS"]:
                    frame_rgb = frame_rgb.copy()
                qimg = QImage(
                    frame_rgb.data,
                    width,
                    height,
                    frame_rgb.strides[0],
                    QImage.Format_RGB888,
                ).copy()
                export_widget.set_frame(qimg)
                export_widget.set_current_frame_index(frame_idx)
                t_seconds = frame_idx / fps if fps > 0 else 0.0
                overlay_lines, _ = self._overlay_context_for_time(t_seconds)
                export_widget.set_status_lines(overlay_lines)
                export_overlays = []
                if callable(self._export_target_overlay_provider):
                    try:
                        export_overlays = list(self._export_target_overlay_provider(t_seconds) or [])
                    except Exception:
                        export_overlays = []
                export_widget.set_target_overlays(export_overlays)
                rendered = QImage(width, height, QImage.Format_ARGB32)
                rendered.fill(Qt.black)
                painter = QPainter(rendered)
                export_widget.render(painter, QPoint(0, 0))
                painter.end()
                rendered = rendered.convertToFormat(QImage.Format_RGB888)
                bits = rendered.bits()
                frame_bytes = bits.tobytes()
                out_rgb = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))
                out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
                writer.write(out_bgr)
                progress.setValue(frame_idx - start_frame + 1)
                QApplication.processEvents()
        finally:
            writer.release()
            cap.release()
            progress.close()

        temp_with_audio = temp_dir / "video_with_audio.mp4"
        mux_cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{start_seconds:.3f}",
            "-i",
            str(source_path),
            "-i",
            str(temp_video),
            "-map",
            "1:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_with_audio),
        ]
        proc = subprocess.run(mux_cmd, capture_output=True, text=True)
        final_source = temp_with_audio if proc.returncode == 0 and temp_with_audio.exists() else temp_video
        try:
            if target_path.exists():
                target_path.unlink()
            shutil.move(str(final_source), str(target_path))
        except Exception as exc:
            return False, f"Export completed but saving failed:\n{exc}"
        try:
            if temp_video.exists():
                temp_video.unlink()
            if temp_with_audio.exists():
                temp_with_audio.unlink()
            temp_dir.rmdir()
        except Exception:
            pass
        if proc.returncode != 0:
            return True, "Clip exported with baked overlays, but audio could not be muxed back in."
        return True, ""

    def set_export_target_overlay_provider(self, provider) -> None:
        self._export_target_overlay_provider = provider

    # ---- Rendering ----
    # NOTE: your remaining methods (show_frame, update_video_label, resizeEvent,
    # update_time_and_overlay, update_log_highlight, offset_changed,
    # sync_logs_to_current_video_first_log) should remain exactly as they are
    # below this point in your file.

    def show_frame(self, frame_index):
        t_total = time.perf_counter()
        if self.cap is None:
            return

        if not self.video_label.isVisible():
            return

        if self.last_frame_rgb is not None:
            self.analysis_prev_frame_rgb = self.last_frame_rgb.copy()
            self.analysis_prev_frame_index = self._last_frame_index

        if not _position_capture_sequential(
            self.cap, self._seq_cap is self.cap, self._seq_next_frame, frame_index
        ):
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        t_read = time.perf_counter()
        ret, frame = self.cap.read()
        read_dt = time.perf_counter() - t_read
        if read_dt > 0.5:
            print(f"[viewer] frame read took {read_dt:.2f}s", flush=True)
        if not ret or frame is None:
            self._seq_cap = None
            return
        self._seq_cap = self.cap
        self._seq_next_frame = frame_index + 1
        if getattr(frame, "ndim", 0) != 3 or frame.shape[0] <= 0 or frame.shape[1] <= 0:
            return

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if not frame_rgb.flags["C_CONTIGUOUS"]:
            frame_rgb = frame_rgb.copy()
        h, w, ch = frame_rgb.shape
        bytes_per_line = frame_rgb.strides[0]
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888).copy()
        self.last_qimage = qimg
        self.last_frame_rgb = frame_rgb
        self._last_frame_index = int(frame_index)
        t = frame_index / self.fps if self.fps > 0 else 0.0
        self._update_secondary_frame_for_time(t)
        self._request_video_label_update()

        if self.frame_count > 0:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(frame_index)
            self.seek_slider.blockSignals(False)

        self.update_time_and_overlay(t, frame_index)
        self.update_log_highlight(t)
        dt_total = time.perf_counter() - t_total
        if dt_total > 0.5:
            print(f"[viewer] show_frame total took {dt_total:.2f}s", flush=True)

    def update_video_label(self):
        if self._updating_video_label:
            return
        self._pending_video_label_update = False
        self._updating_video_label = True
        t0 = time.perf_counter()
        try:
            if self.last_qimage is not None and self.video_label is not None:
                if not self.video_label.isVisible():
                    return
                if self.video_label.width() <= 1 or self.video_label.height() <= 1:
                    return
                self.video_label.set_fps(self.fps)
                self.video_label.set_current_frame_index(self.current_frame)
                frame_to_show = self.last_qimage
                if (
                    self.analysis_mode_combo.currentText() != "Off"
                    and self.analysis_display_combo.currentText() == "Main Overlay"
                ):
                    out_rgb, _tooltip = self._compute_analysis_output()
                    if out_rgb is not None and self.last_frame_rgb is not None:
                        alpha = self.analysis_main_alpha_slider.value() / 100.0
                        try:
                            blended = cv2.addWeighted(self.last_frame_rgb, 1.0 - alpha, out_rgb, alpha, 0.0)
                            h, w, ch = blended.shape
                            bytes_per_line = blended.strides[0]
                            frame_to_show = QImage(
                                blended.data, w, h, bytes_per_line, QImage.Format_RGB888
                            ).copy()
                        except Exception:
                            frame_to_show = self.last_qimage
                self.video_label.set_frame(frame_to_show)
                self._refresh_tray_view_if_open()
                if self._popout_label is not None:
                    self._popout_label.set_fps(self.fps)
                    self._popout_label.set_current_frame_index(self.current_frame)
                    self._popout_label.set_frame(self.last_qimage)
                    self._refresh_tray_view_if_open()
            if (
                self._draw_secondary_video
                and self.secondary_last_qimage is not None
                and self.secondary_video_label is not None
                and self.secondary_video_label.isVisible()
                and self.secondary_video_label.width() > 1
                and self.secondary_video_label.height() > 1
            ):
                self.secondary_video_label.set_frame(self.secondary_last_qimage)
            if hasattr(self, "analysis_label"):
                self._update_analysis_view()
        finally:
            self._updating_video_label = False
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] update_video_label took {dt:.2f}s", flush=True)

    def _request_video_label_update(self):
        if self._pending_video_label_update:
            return
        self._pending_video_label_update = True
        QTimer.singleShot(0, self.update_video_label)

    def _toggle_video_popout(self):
        if self._popout_window is not None and self._popout_window.isVisible():
            self._popout_window.close()
            return
        if self._popout_window is None:
            win = QWidget(self, Qt.Window)
            win.setWindowTitle("Video Popout")
            win.resize(900, 600)
            layout = QVBoxLayout(win)
            layout.setContentsMargins(6, 6, 6, 6)
            toolbar = QHBoxLayout()
            tool_group = QButtonGroup(win)
            tool_group.setExclusive(True)
            for tool_key, label_text in (
                ("line", "Line"),
                ("arrow", "Arrow"),
                ("text", "Text"),
                ("measure", "Measure"),
                ("timed_line", "Timed Line"),
                ("tray", "Bird's Eye"),
            ):
                btn = QToolButton()
                btn.setText(label_text)
                btn.setCheckable(True)
                btn.setChecked(self._annotation_tool == tool_key)
                btn.clicked.connect(lambda _checked, t=tool_key: self._set_annotation_tool(t))
                tool_group.addButton(btn)
                toolbar.addWidget(btn)
            color_btn = QToolButton()
            color_btn.setText("Color")
            color_btn.clicked.connect(self._pick_annotation_color)
            self._set_color_button_style(color_btn, self._annotation_color)
            toolbar.addWidget(color_btn)
            undo_btn = QToolButton()
            undo_btn.setText("Undo")
            undo_btn.clicked.connect(self._undo_annotation)
            toolbar.addWidget(undo_btn)
            clear_btn = QToolButton()
            clear_btn.setText("Clear Clip")
            clear_btn.clicked.connect(self._clear_clip_annotations)
            toolbar.addWidget(clear_btn)
            toolbar.addStretch(1)
            layout.addLayout(toolbar)

            content_row = QHBoxLayout()
            label = AnnotatedVideoWidget("No video loaded")
            label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            label.set_editable(True)
            if getattr(self, "_placeholder_image", None) is not None:
                label.set_placeholder_image(self._placeholder_image)
            label.annotation_created.connect(self._add_annotation)
            label.annotation_context_requested.connect(self._show_annotation_context_menu)
            label.annotation_updated.connect(self._on_annotation_updated)
            label.set_scrub_callback(self._handle_scroll_wheel)
            label.set_key_handler(self._handle_popout_key_event)
            label.set_tray_update_callback(self._refresh_tray_view_if_open)
            label.set_tool(self._annotation_tool)
            label.set_color(self._annotation_color)
            label.set_annotations(self._current_annotations())
            label.setFocusPolicy(Qt.StrongFocus)
            content_row.addWidget(label, 1)

            if hasattr(self, "analysis_controls_panel"):
                self.analysis_controls_panel.setParent(win)
                self.analysis_controls_panel.setVisible(True)
                content_row.addWidget(self.analysis_controls_panel)
            layout.addLayout(content_row, 1)
            win.setLayout(layout)
            win.destroyed.connect(lambda _=None: self._clear_video_popout())
            self._popout_window = win
            self._popout_label = label
            self._popout_color_btn = color_btn
            self._popout_tool_group = tool_group
            win.destroyed.connect(lambda _=None: self._clear_tray_view_popout())
        if self.last_qimage is not None and self._popout_label is not None:
            self._popout_label.set_frame(self.last_qimage)
        self._refresh_annotation_view()
        self._popout_window.show()
        if self._popout_label is not None:
            QTimer.singleShot(0, self._popout_label.setFocus)

    def _open_annotation_popout(self):
        if self._popout_window is None or not self._popout_window.isVisible():
            self._toggle_video_popout()
        else:
            self._popout_window.raise_()
            self._popout_window.activateWindow()

    def _open_tray_view_window(self):
        # Find latest tray annotation
        tray_ann = None
        for ann in reversed(self._current_annotations()):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is None or self.video_label is None:
            QMessageBox.information(self, "Bird's Eye", "No bird's eye region defined.")
            return
        pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
        tray_view = self.video_label._build_tray_view(pts)
        if tray_view is None or tray_view.isNull():
            QMessageBox.warning(self, "Bird's Eye", "Bird's Eye unavailable for current frame.")
            return
        self.video_label._update_tray_view_popout(tray_view)

    def _refresh_tray_view_if_open(self):
        if self.video_label is None:
            return
        if self.video_label._tray_view_window is None or not self.video_label._tray_view_window.isVisible():
            return
        tray_ann = None
        for ann in reversed(self._current_annotations()):
            if ann.get("type") == "tray" and len(ann.get("points") or []) == 4:
                tray_ann = ann
                break
        if tray_ann is None:
            return
        pts = [QPointF(p[0], p[1]) for p in tray_ann.get("points", [])]
        tray_view = self.video_label._build_tray_view(pts)
        if tray_view is not None and not tray_view.isNull():
            self.video_label._update_tray_view_popout(tray_view)

    def _clear_video_popout(self):
        self._popout_window = None
        self._popout_label = None
        self._popout_color_btn = None
        self._popout_tool_group = None
        self._clear_tray_view_popout()

    def _clear_tray_view_popout(self):
        if self._tray_view_window is not None:
            try:
                self._tray_view_window.close()
            except Exception:
                pass
        self._tray_view_window = None
        self._tray_view_label = None

    def _current_annotations(self) -> list[dict]:
        return list(self._pinned_annotations) + list(self._clip_annotations)

    def _annotations_dir(self) -> Path:
        path = self.cache_root / "annotations"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return path

    def _clip_annotations_path(self) -> Path | None:
        base_path = None
        if self.current_video_original_path is not None:
            base_path = self.current_video_original_path
        elif self.current_video_path:
            base_path = Path(self.current_video_path)
        if base_path is None:
            return None
        try:
            cache_path = self._cache_path_for(Path(base_path))
        except Exception:
            cache_path = Path(base_path)
        filename = f"{cache_path.stem}.json"
        return self._annotations_dir() / filename

    def _pinned_annotations_path(self) -> Path:
        return self._annotations_dir() / "pinned.json"

    def _load_pinned_annotations(self):
        self._pinned_annotations = []
        path = self._pinned_annotations_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("annotations", [])
            if isinstance(items, list):
                self._pinned_annotations = [i for i in items if isinstance(i, dict)]
        except Exception:
            self._pinned_annotations = []

    def _save_pinned_annotations(self):
        path = self._pinned_annotations_path()
        payload = {"annotations": self._pinned_annotations}
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_clip_annotations(self):
        self._clip_annotations = []
        path = self._clip_annotations_path()
        if path is None or not path.exists():
            self._annotation_history = []
            self._refresh_annotation_view()
            self._emit_clip_annotation_status()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = data.get("annotations", [])
            if isinstance(items, list):
                self._clip_annotations = [i for i in items if isinstance(i, dict)]
        except Exception:
            self._clip_annotations = []
        self._annotation_history = list(self._current_annotations())
        self._refresh_annotation_view()
        self._emit_clip_annotation_status()

    def _save_clip_annotations(self):
        path = self._clip_annotations_path()
        if path is None:
            return
        payload = {"annotations": self._clip_annotations}
        try:
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._emit_clip_annotation_status()

    def _emit_clip_annotation_status(self):
        base_path = self.current_video_original_path or (Path(self.current_video_path) if self.current_video_path else None)
        if base_path is None:
            return
        has_annotations = bool(self._clip_annotations)
        self.annotation_status_changed.emit(base_path, has_annotations)

    def _save_annotations(self):
        self._save_clip_annotations()
        self._save_pinned_annotations()

    def _refresh_annotation_view(self):
        annotations = self._current_annotations()
        if self.video_label is not None:
            self.video_label.set_annotations(annotations)
            self.video_label.set_current_frame_index(self.current_frame)
            self.video_label.set_fps(self.fps)
        if self._popout_label is None:
            return
        self._popout_label.set_annotations(annotations)
        self._popout_label.set_tool(self._annotation_tool)
        self._popout_label.set_color(self._annotation_color)
        self._popout_label.set_current_frame_index(self.current_frame)
        self._popout_label.set_fps(self.fps)

    def _add_annotation(self, ann: dict):
        if ann.get("pinned"):
            self._pinned_annotations.append(ann)
        else:
            self._clip_annotations.append(ann)
        self._annotation_history.append(ann)
        self._save_annotations()
        self._refresh_annotation_view()

    def _set_annotation_tool(self, tool: str):
        self._annotation_tool = tool
        if self._popout_label is not None:
            self._popout_label.set_tool(tool)
        if self.video_label is not None:
            self.video_label.set_tool(tool)

    def _set_annotation_color(self, color: QColor):
        self._annotation_color = QColor(color)
        if self._popout_label is not None:
            self._popout_label.set_color(self._annotation_color)
        if self._popout_color_btn is not None:
            self._set_color_button_style(self._popout_color_btn, self._annotation_color)

    def _set_color_button_style(self, button: QToolButton, color: QColor):
        button.setStyleSheet(f"background-color: {color.name()};")

    def _pick_annotation_color(self):
        color = QColorDialog.getColor(self._annotation_color, self, "Select annotation color")
        if color.isValid():
            self._set_annotation_color(color)

    def _show_annotation_context_menu(self, idx: int, global_pos):
        annotations = self._current_annotations()
        if idx < 0 or idx >= len(annotations):
            return
        ann = annotations[idx]
        menu = QMenu(self)
        edit_action = menu.addAction("Edit annotation")
        pin_action = menu.addAction("Toggle pin across clips")
        frame_action = menu.addAction("Toggle pin to current frame")
        distance_action = None
        if ann.get("type") == "timed_line":
            distance_action = menu.addAction("Set distance (m)")
        delete_action = menu.addAction("Delete annotation")
        chosen = menu.exec(global_pos.toPoint())
        if chosen == edit_action:
            if ann.get("type") in ("line", "arrow", "measure", "tray"):
                if self._popout_label is not None:
                    current = self._popout_label.get_edit_index()
                    self._popout_label.set_edit_index(None if current == idx else idx)
        elif chosen == pin_action:
            if ann.get("pinned"):
                ann["pinned"] = False
                if ann in self._pinned_annotations:
                    self._pinned_annotations.remove(ann)
                if ann not in self._clip_annotations:
                    self._clip_annotations.append(ann)
            else:
                ann["pinned"] = True
                if ann in self._clip_annotations:
                    self._clip_annotations.remove(ann)
                if ann not in self._pinned_annotations:
                    self._pinned_annotations.append(ann)
            self._save_annotations()
            self._refresh_annotation_view()
        elif chosen == frame_action:
            frame_idx = self.current_frame
            if ann.get("frame_index") == frame_idx:
                ann.pop("frame_index", None)
            else:
                ann["frame_index"] = frame_idx
            self._save_annotations()
            self._refresh_annotation_view()
        elif distance_action is not None and chosen == distance_action:
            current = ann.get("distance_m")
            text, ok = QInputDialog.getText(
                self,
                "Set distance (m)",
                "Distance in meters:",
                text="" if current is None else str(current),
            )
            if ok and text.strip():
                cleaned = re.sub(r"[^0-9.+-eE]", "", text)
                try:
                    ann["distance_m"] = float(cleaned)
                except ValueError:
                    ann["distance_m"] = None
            elif ok and not text.strip():
                ann.pop("distance_m", None)
            self._save_annotations()
            self._refresh_annotation_view()
        elif chosen == delete_action:
            if ann in self._pinned_annotations:
                self._pinned_annotations.remove(ann)
            if ann in self._clip_annotations:
                self._clip_annotations.remove(ann)
            while ann in self._annotation_history:
                self._annotation_history.remove(ann)
            if self._popout_label is not None:
                self._popout_label.set_edit_index(None)
            self._save_annotations()
            self._refresh_annotation_view()

    def _on_annotation_updated(self, _idx: int, _ann: dict):
        self._save_annotations()
        self._refresh_annotation_view()

    def _handle_popout_key_event(self, event):
        self.keyPressEvent(event)

    def _undo_annotation(self):
        if not self._annotation_history:
            return
        ann = self._annotation_history.pop()
        if ann.get("pinned"):
            if ann in self._pinned_annotations:
                self._pinned_annotations.remove(ann)
        else:
            if ann in self._clip_annotations:
                self._clip_annotations.remove(ann)
        self._save_annotations()
        self._refresh_annotation_view()

    def _clear_clip_annotations(self):
        if not self._clip_annotations:
            return
        self._clip_annotations = []
        self._annotation_history = [a for a in self._annotation_history if a.get("pinned")]
        self._save_clip_annotations()
        self._refresh_annotation_view()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_video_label()
        self._update_marker_bar_padding()

    def _update_marker_bar_padding(self):
        if not hasattr(self, "seek_slider") or not hasattr(self, "event_marker_bar"):
            return
        slider = self.seek_slider
        if slider.width() <= 0:
            return
        opt = QStyleOptionSlider()
        slider.initStyleOption(opt)
        style = slider.style()
        groove = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, slider)
        handle = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, slider)
        if not groove.isValid() or not handle.isValid():
            self.event_marker_bar.set_track_padding(0, 0)
            return
        half = int(round(handle.width() / 2))
        left_pad = max(0, groove.left() + half)
        right_pad = max(0, slider.width() - 1 - (groove.right() - half))
        self.event_marker_bar.set_track_padding(left_pad, right_pad)
        if hasattr(self, "timeline_marker_bar"):
            self.timeline_marker_bar.set_track_padding(left_pad, right_pad)

    def _rebuild_ppm_model(self):
        secs: list[float] = []
        for ev, msg in zip(self.all_events, self.all_message_keys):
            msg_text = str(msg or "").strip().lower()
            if TARGET_QUEUE_MESSAGE not in msg_text:
                continue
            try:
                sec = float(ev.start.total_seconds())
            except Exception:
                continue
            secs.append(sec)
        secs.sort()
        self._ppm_event_seconds = secs
        prefix = [0.0]
        for i in range(1, len(secs)):
            gap = max(0.0, secs[i] - secs[i - 1])
            prefix.append(prefix[-1] + gap)
        self._ppm_interval_prefix_sum = prefix

    def _ppm_overlay_lines(self, t_seconds: float) -> list[str]:
        if not self._ppm_event_seconds:
            return []
        t_log = float(t_seconds) - float(self.effective_offset())
        n = bisect_right(self._ppm_event_seconds, t_log)
        if n <= 0:
            return []

        lines: list[str] = []
        instant = None
        if n >= 2:
            dt = self._ppm_event_seconds[n - 1] - self._ppm_event_seconds[n - 2]
            if dt > 0:
                instant = 60.0 / dt
        if instant is not None:
            lines.append(f"Now: {instant:5.1f} ppm")

        win_start = t_log - PPM_ROLLING_WINDOW_SECONDS
        left = bisect_left(self._ppm_event_seconds, win_start)
        if (n - left) >= 2:
            span = self._ppm_event_seconds[n - 1] - self._ppm_event_seconds[left]
            if span > 0:
                roll = 60.0 * ((n - left) - 1) / span
                lines.append(f"Avg60s: {roll:5.1f} ppm")

        if n >= 2 and len(self._ppm_interval_prefix_sum) >= n:
            total_span = self._ppm_interval_prefix_sum[n - 1]
            if total_span > 0:
                overall = 60.0 * (n - 1) / total_span
                lines.append(f"AvgAll: {overall:5.1f} ppm")
        return lines

    def set_sku_timeline_items(self, items: list[object] | None):
        self._sku_timeline_items = list(items or [])

    @staticmethod
    def _format_sku_overlay_label(item) -> str:
        payload = item.payload if isinstance(getattr(item, "payload", None), dict) else {}
        if payload.get("_ui_manual"):
            return ""
        sku = str(payload.get("_ui_sku") or getattr(item, "label", "") or "").strip()
        tray = str(payload.get("_ui_tray") or "").strip()
        tool = str(payload.get("_ui_tool") or "").strip()
        parts = [part for part in (sku, tray, tool) if part]
        return " | ".join(parts) if parts else sku

    @staticmethod
    def _sku_overlay_lines_from_item(item) -> list[str]:
        payload = item.payload if isinstance(getattr(item, "payload", None), dict) else {}
        if payload.get("_ui_manual"):
            return []
        sku = str(payload.get("_ui_sku") or getattr(item, "label", "") or "").strip()
        tray = str(payload.get("_ui_tray") or "").strip()
        tool = str(payload.get("_ui_tool") or "").strip()
        lines: list[str] = []
        if sku:
            lines.append(f"SKU: {sku[:39]}..." if len(sku) > 44 else f"SKU: {sku}")
        if tray:
            lines.append(f"Tray: {tray[:38]}..." if len(tray) > 43 else f"Tray: {tray}")
        if tool:
            lines.append(f"Tool: {tool[:38]}..." if len(tool) > 43 else f"Tool: {tool}")
        return lines

    def _current_sku_overlay_lines(self, playback_dt: datetime | None) -> list[str]:
        if playback_dt is None or not self._sku_timeline_items:
            return []
        if playback_dt.tzinfo is None:
            playback_dt = playback_dt.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        else:
            playback_dt = playback_dt.astimezone(timezone.utc)
        last_known_item = None
        for item in self._sku_timeline_items:
            start = getattr(item, "start", None)
            end = getattr(item, "end", None)
            if start is None or end is None:
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            else:
                start = start.astimezone(timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            else:
                end = end.astimezone(timezone.utc)
            label = self._format_sku_overlay_label(item)
            if label:
                last_known_item = item
            if start <= playback_dt <= end:
                return self._sku_overlay_lines_from_item(item)
            if playback_dt < start:
                break
        if last_known_item is not None:
            return self._sku_overlay_lines_from_item(last_known_item)
        return []

    def effective_offset(self) -> float:
        return self.sync_offset + self.time_offset

    def _overlay_context_for_time(self, t_seconds: float) -> tuple[list[str], datetime | None]:
        playback_dt = None
        if self.video_start_dt is not None and self.fps > 0:
            adjusted_seconds = t_seconds + (self.ocr_frame_offset / self.fps)
            playback_dt = self.video_start_dt + timedelta(seconds=adjusted_seconds)
        elif self.current_video_filename_dt is not None:
            playback_dt = self.current_video_filename_dt + timedelta(seconds=t_seconds)
        ppm_lines = self._ppm_overlay_lines(t_seconds)
        sku_lines = self._current_sku_overlay_lines(playback_dt)
        if sku_lines:
            ppm_lines = list(ppm_lines) + sku_lines
        return ppm_lines, playback_dt

    def update_time_and_overlay(self, t_seconds: float, frame_index: int):
        td = timedelta(seconds=t_seconds)
        time_str = format_timecode(td).replace(",", ".")
        if hasattr(self, "info_label"):
            self.info_label.display(time_str)
        drift_seconds = float(self.time_offset)
        playback_dt = None
        if self.video_start_dt is not None and self.fps > 0:
            adjusted_seconds = t_seconds + (self.ocr_frame_offset / self.fps)
            calc_dt = self.video_start_dt + timedelta(seconds=adjusted_seconds)
            calc_str = calc_dt.strftime("%H:%M:%S.%f")[:-3]
            if hasattr(self, "calc_label"):
                self.calc_label.display(calc_str)
            playback_dt = calc_dt - timedelta(seconds=drift_seconds)
        else:
            if hasattr(self, "calc_label"):
                self.calc_label.display("00:00:00.000")
            if self.current_video_filename_dt is not None:
                playback_dt = self.current_video_filename_dt + timedelta(seconds=t_seconds - drift_seconds)
        ppm_lines, playback_dt_from_helper = self._overlay_context_for_time(t_seconds)
        if playback_dt is None:
            playback_dt = playback_dt_from_helper
        if hasattr(self, "video_label"):
            self.video_label.set_status_lines(ppm_lines)
        if self._popout_label is not None:
            self._popout_label.set_status_lines(ppm_lines)
        if hasattr(self, "frame_label"):
            self.frame_label.display(str(frame_index))
        self.current_time_changed.emit(playback_dt)

    def update_log_highlight(self, t_seconds: float):
        if not self.events or self._log_model.rowCount() == 0:
            return

        # Mirror the correction applied in _event_seconds_to_video_seconds so
        # the reverse mapping stays consistent when OCR frame sync is active.
        ocr_correction = (self.ocr_frame_offset / self.fps) if self.fps > 0 and self.ocr_frame_offset else 0.0
        t_td = timedelta(seconds=t_seconds + ocr_correction) - timedelta(seconds=self.effective_offset())
        t_secs = t_td.total_seconds()

        # Frame interval: [t_secs, t_secs + one_frame).  All log events whose
        # start falls inside this window are "between this frame and the next."
        one_frame = (1.0 / self.fps) if self.fps > 0 else 0.0
        frame_end = t_secs + one_frame

        left  = bisect_left(self._event_start_times, t_secs)
        right = bisect_left(self._event_start_times, frame_end)

        # Red: every event that starts within the current frame interval.
        active: set[int] = set(range(left, right))

        # Amber: the next event just beyond the frame interval (upper bound).
        nearest: int | None = right if right < len(self.events) else None

        if active:
            scroll_to = min(active)
        elif nearest is not None:
            scroll_to = nearest
        else:
            scroll_to = max(0, left - 1)

        self._log_model.set_highlights(active, nearest)
        if scroll_to != getattr(self, "_last_highlight_row", None):
            self._last_highlight_row = scroll_to
            self.log_list.scrollTo(
                self._log_model.index(scroll_to),
                QListView.EnsureVisible,
            )

    def set_offset_value(self, value: float):
        clamped = max(self.offset_min, min(self.offset_max, float(value)))
        if abs(clamped - self.time_offset) < 1e-6:
            return
        self.time_offset = clamped
        self._update_offset_display()
        self._apply_offset()

    def adjust_offset(self, delta: float):
        self.set_offset_value(self.time_offset + delta)

    def _update_offset_display(self):
        if hasattr(self, "offset_display"):
            self.offset_display.setText(f"{self.time_offset:+.2f}s")
        if hasattr(self, "offset_slider"):
            slider_value = int(round(self.time_offset * self._offset_slider_scale))
            self.offset_slider.blockSignals(True)
            self.offset_slider.setValue(slider_value)
            self.offset_slider.blockSignals(False)

    def _on_offset_slider_changed(self, value: int):
        self.set_offset_value(float(value) / float(self._offset_slider_scale))

    def set_close_gap_threshold_value(self, value: float):
        clamped = max(self.close_gap_threshold_min, min(self.close_gap_threshold_max, float(value)))
        if abs(clamped - self.close_gap_threshold) < 1e-6:
            return
        self.close_gap_threshold = clamped
        self._update_close_gap_threshold_display()
        self.close_gap_threshold_changed.emit(self.close_gap_threshold)

    def _update_close_gap_threshold_display(self):
        if hasattr(self, "close_gap_display"):
            self.close_gap_display.setText(f"{self.close_gap_threshold:.2f}x")
        if hasattr(self, "close_gap_slider"):
            slider_value = int(round(self.close_gap_threshold * 100.0))
            self.close_gap_slider.blockSignals(True)
            self.close_gap_slider.setValue(slider_value)
            self.close_gap_slider.blockSignals(False)

    def _on_close_gap_slider_changed(self, value: int):
        self.set_close_gap_threshold_value(float(value) / 100.0)

    def _apply_offset(self):
        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self._update_timeline_markers()

    def sync_logs_to_current_video_first_log(self):
        if not self.events:
            QMessageBox.warning(
                self,
                "No logs",
                "Load a CSV log and make sure at least one source/message filter is enabled."
            )
            return
        if self.cap is None:
            QMessageBox.warning(self, "No video", "Open a video file first.")
            return

        first_event = self.events[0]
        first_start_secs = first_event.start.total_seconds()
        t_current = self.current_frame / self.fps if self.fps > 0 else 0.0

        self.sync_offset = t_current - first_start_secs
        self.time_offset = 0.0
        self.set_offset_value(0.0)

        self.update_time_and_overlay(t_current, self.current_frame)
        self.update_log_highlight(t_current)
        self.log_markers_enabled = True
        self._update_timeline_markers()

        QMessageBox.information(
            self,
            "Logs synced",
            "Logs are now aligned so that the FIRST visible log entry matches the CURRENT video frame."
        )

    # ---- Cache helpers ----

    def _default_cache_root(self) -> Path:
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "VideoLogViewer" / "cache"
        return Path.home() / ".videolog_cache"

    def _cache_path_for(self, original_path: Path) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(str(original_path).encode("utf-8")).hexdigest()[:16]
        filename = f"{original_path.stem}_{key}{original_path.suffix}"
        return self.cache_root / filename

    def _cache_meta_path_for(self, cache_path: Path) -> Path:
        return cache_path.with_name(cache_path.name + CACHE_META_SUFFIX)

    def _clip_annotation_path_for_cache(self, cache_path: Path) -> Path:
        return self._annotations_dir() / f"{cache_path.stem}.json"

    def _read_cache_meta(self, cache_path: Path) -> dict | None:
        try:
            meta_path = self._cache_meta_path_for(cache_path)
            if not meta_path.exists():
                return None
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _write_cache_meta(self, source_path: Path, cache_path: Path) -> None:
        try:
            source_stat = source_path.stat()
            payload = {
                "source_path": str(source_path),
                "source_size": int(source_stat.st_size),
                "source_mtime_ns": int(source_stat.st_mtime_ns),
                "cached_at": datetime.now().isoformat(),
            }
            self._cache_meta_path_for(cache_path).write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass

    def _invalidate_cached_copy(self, cache_path: Path) -> None:
        for target in (cache_path, self._cache_meta_path_for(cache_path), self._clip_annotation_path_for_cache(cache_path)):
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass

    def _touch_cache_entry(self, cache_path: Path) -> None:
        now_ts = time.time()
        for target in (cache_path, self._cache_meta_path_for(cache_path), self._clip_annotation_path_for_cache(cache_path)):
            try:
                if target.exists():
                    os.utime(target, (now_ts, now_ts))
            except Exception:
                pass

    def _cache_group_size(self, paths: list[Path]) -> int:
        total = 0
        for path in paths:
            try:
                if path.exists():
                    total += int(path.stat().st_size)
            except Exception:
                continue
        return total

    def _cache_group_last_used(self, paths: list[Path]) -> float:
        latest = 0.0
        for path in paths:
            try:
                if path.exists():
                    stat = path.stat()
                    latest = max(latest, float(stat.st_mtime))
            except Exception:
                continue
        return latest

    def _iter_cache_groups(self) -> list[dict]:
        if not self.cache_root.exists():
            return []
        groups: list[dict] = []
        try:
            entries = list(self.cache_root.iterdir())
        except Exception:
            return []
        for entry in entries:
            if not entry.is_file():
                continue
            name = entry.name
            if name.endswith(".part") or name.endswith(CACHE_META_SUFFIX):
                continue
            if entry in {self.offset_cache_path, self.secondary_offset_cache_path}:
                continue
            paths = [entry, self._cache_meta_path_for(entry), self._clip_annotation_path_for_cache(entry)]
            groups.append(
                {
                    "cache_path": entry,
                    "paths": paths,
                    "size": self._cache_group_size(paths),
                    "last_used": self._cache_group_last_used(paths),
                }
            )
        return groups

    def prune_cache_if_needed(self) -> None:
        try:
            self._prune_cache()
        except Exception:
            pass
        self.update_cache_status()

    def _prune_cache(self) -> None:
        groups = self._iter_cache_groups()
        if not groups:
            return
        cutoff_ts = time.time() - (CACHE_MAX_AGE_DAYS * 24 * 60 * 60)
        protected_paths: set[str] = set()
        for active in (self.current_video_path, self.secondary_video_path):
            if not active:
                continue
            try:
                protected_paths.add(str(Path(active).resolve()))
            except Exception:
                protected_paths.add(str(active))

        def _delete_group(group: dict) -> None:
            cache_path = group.get("cache_path")
            if isinstance(cache_path, Path):
                try:
                    resolved = str(cache_path.resolve())
                except Exception:
                    resolved = str(cache_path)
                if resolved in protected_paths:
                    return
            for path in group.get("paths", []):
                try:
                    if isinstance(path, Path) and path.exists():
                        path.unlink()
                except Exception:
                    continue

        for group in groups:
            if group.get("last_used", 0.0) < cutoff_ts:
                _delete_group(group)

        groups = [g for g in self._iter_cache_groups() if g.get("size", 0) > 0]
        total_bytes = sum(int(g.get("size", 0)) for g in groups)
        if total_bytes <= CACHE_MAX_BYTES:
            return
        groups.sort(key=lambda g: (float(g.get("last_used", 0.0)), str(g.get("cache_path"))))
        for group in groups:
            if total_bytes <= CACHE_MAX_BYTES:
                break
            cache_path = group.get("cache_path")
            if isinstance(cache_path, Path):
                try:
                    resolved = str(cache_path.resolve())
                except Exception:
                    resolved = str(cache_path)
                if resolved in protected_paths:
                    continue
            size = int(group.get("size", 0))
            _delete_group(group)
            total_bytes -= size

    def _is_cached_copy_current(self, source_path: Path, cache_path: Path) -> bool:
        try:
            if not cache_path.exists():
                return False
        except Exception:
            return False
        try:
            source_stat = source_path.stat()
        except Exception:
            return True
        meta = self._read_cache_meta(cache_path)
        if meta:
            try:
                return (
                    int(meta.get("source_size")) == int(source_stat.st_size)
                    and int(meta.get("source_mtime_ns")) == int(source_stat.st_mtime_ns)
                )
            except Exception:
                pass
        try:
            cache_stat = cache_path.stat()
        except Exception:
            return False
        return int(cache_stat.st_size) == int(source_stat.st_size)

    def _ensure_cached_copy(self, source_path: Path, cache_path: Path) -> bool:
        if self._is_cached_copy_current(source_path, cache_path):
            self._touch_cache_entry(cache_path)
            return True
        self._invalidate_cached_copy(cache_path)
        return self._copy_to_cache(source_path, cache_path)

    def get_valid_cached_path(self, original_path: Path) -> Path | None:
        try:
            cache_path = self._cache_path_for(original_path)
        except Exception:
            return None
        if self._is_cached_copy_current(original_path, cache_path):
            self._touch_cache_entry(cache_path)
            return cache_path
        return None

    def prefetch_clips_to_cache(self, paths: list[Path]):
        if not paths:
            return
        for path in paths:
            try:
                path_obj = Path(path)
            except Exception:
                continue
            try:
                cache_path = self._cache_path_for(path_obj)
            except Exception:
                continue
            key = str(cache_path)
            if key in self._prefetch_pending:
                continue
            # No filesystem checks here: this runs on the UI thread, and even
            # a stat() on the share can block for seconds when the link is
            # saturated by running copies. The worker decides cached-vs-copy.
            self._prefetch_pending.add(key)
            future = self._prefetch_executor.submit(
                self._check_or_copy_to_cache, path_obj, cache_path
            )
            self._prefetch_futures[key] = future
            future.add_done_callback(
                lambda fut, p=path_obj, k=key: self._schedule_prefetch_done(fut, p, k)
            )

    def _check_or_copy_to_cache(self, source_path: Path, cache_path: Path) -> bool:
        if self._is_cached_copy_current(source_path, cache_path):
            self._touch_cache_entry(cache_path)
            return True
        return self._copy_to_cache(source_path, cache_path)

    def cancel_queued_prefetches(self) -> None:
        """Drop prefetch jobs that haven't started copying yet (e.g. when the
        user switches to a different day/system). Active copies finish."""
        for key, future in list(self._prefetch_futures.items()):
            if future.cancel():
                self._prefetch_futures.pop(key, None)
                self._prefetch_pending.discard(key)

    def _schedule_prefetch_done(self, future: Future, source_path: Path, key: str):
        if future.cancelled():
            return  # superseded by a click-triggered download of the same clip
        try:
            ok = bool(future.result())
        except Exception:
            ok = False
        QMetaObject.invokeMethod(
            self,
            "_on_prefetch_done",
            Qt.QueuedConnection,
            Q_ARG(str, str(source_path)),
            Q_ARG(str, key),
            Q_ARG(bool, ok),
        )

    def _copy_to_cache(self, source_path: Path, cache_path: Path) -> bool:
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if tmp_path.exists():
                tmp_path.unlink()
            shutil.copy2(source_path, tmp_path)
            tmp_path.replace(cache_path)
            self._write_cache_meta(source_path, cache_path)
            self._touch_cache_entry(cache_path)
            self._prune_cache()
            return True
        except Exception:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False

    @Slot(str, str, bool)
    def _on_prefetch_done(self, source_path: str, key: str, ok: bool):
        self._prefetch_pending.discard(key)
        self._prefetch_futures.pop(key, None)
        if ok:
            self.update_cache_status()
            try:
                self.cache_clip_ready.emit(Path(source_path))
            except Exception:
                self.cache_clip_ready.emit(Path(source_path))
        self._finish_pending_video_load(source_path, ok)

    def _calculate_cache_stats(self) -> tuple[int, int]:
        if not self.cache_root.exists():
            return 0, 0
        total = 0
        count = 0
        for entry in self.cache_root.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                    count += 1
                except OSError:
                    continue
        return count, total

    @Slot()
    def update_cache_status(self):
        if self._cache_status_future is not None and not self._cache_status_future.done():
            self._cache_status_pending = True
            return
        self._cache_status_pending = False
        self._cache_status_future = self._cache_executor.submit(self._calculate_cache_stats)
        self._cache_status_future.add_done_callback(self._on_cache_status_ready)

    def _on_cache_status_ready(self, future: Future):
        try:
            count, total = future.result()
        except Exception as exc:
            QMetaObject.invokeMethod(
                self,
                "_finish_cache_status_error",
                Qt.QueuedConnection,
                Q_ARG(str, str(exc)),
            )
            return
        mb = total / (1024 * 1024) if total else 0.0
        QMetaObject.invokeMethod(
            self,
            "_finish_cache_status_update",
            Qt.QueuedConnection,
            Q_ARG(int, int(count)),
            Q_ARG(float, float(mb)),
        )

    @Slot(int, float)
    def _finish_cache_status_update(self, count: int, mb: float):
        self._cache_status_future = None
        self.cache_status_label.setText(f"Cache: {count} file(s), {mb:.1f} MB")
        if self._cache_status_pending:
            self._cache_status_pending = False
            self.update_cache_status()

    @Slot(str)
    def _finish_cache_status_error(self, message: str):
        self._cache_status_future = None
        self.cache_status_label.setText(f"Cache status unavailable ({message})")
        if self._cache_status_pending:
            self._cache_status_pending = False
            self.update_cache_status()

    def clear_cache(self):
        if not self.cache_root.exists():
            self.update_cache_status()
            return
        resp = QMessageBox.question(
            self,
            "Clear cache",
            "This will delete all locally cached videos. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        try:
            for entry in self.cache_root.iterdir():
                if entry.is_file():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to clear cache: {exc}")
        finally:
            self.update_cache_status()

    def clear_elastic_event_cache(self):
        elastic_cache_root = self.cache_root / "elastic_events"
        if not elastic_cache_root.exists():
            QMessageBox.information(self, "Event cache", "No cached Elastic events found.")
            return
        resp = QMessageBox.question(
            self,
            "Clear event cache",
            "This will delete cached Elastic event results only. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        try:
            for entry in elastic_cache_root.iterdir():
                if entry.is_file():
                    entry.unlink(missing_ok=True)
                else:
                    shutil.rmtree(entry, ignore_errors=True)
            QMessageBox.information(self, "Event cache", "Cached Elastic events removed.")
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to clear Elastic event cache: {exc}")
        finally:
            self.update_cache_status()

    def delete_current_cache_copy(self):
        if not self.current_video_path:
            QMessageBox.information(self, "No video", "Load a cached video first.")
            return
        path = Path(self.current_video_path)
        if not self._is_path_in_cache(path):
            QMessageBox.information(
                self, "Not cached", "The current video is not stored in the cache."
            )
            return
        resp = QMessageBox.question(
            self,
            "Delete cached copy",
            "The currently loaded cached copy will be deleted and the video closed. Continue?",
        )
        if resp != QMessageBox.Yes:
            return
        if self.cap is not None:
            self.pause()
            self.cap.release()
            self.cap = None
            self.video_label.set_placeholder_text("No video loaded")
        try:
            path.unlink(missing_ok=True)
            QMessageBox.information(self, "Deleted", "Cached copy removed.")
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to delete cached copy: {exc}")
        self.current_video_path = None
        self.update_cache_status()

    def _is_path_in_cache(self, path: Path) -> bool:
        try:
            return Path(path).resolve().is_relative_to(self.cache_root.resolve())
        except AttributeError:
            # For Python < 3.9
            try:
                path_resolved = Path(path).resolve()
                cache_resolved = self.cache_root.resolve()
                return str(path_resolved).startswith(str(cache_resolved))
            except Exception:
                return False
            except Exception:
                return False

    def open_cache_folder(self):
        try:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(self.cache_root))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(self.cache_root)])
            else:
                subprocess.Popen(["xdg-open", str(self.cache_root)])
        except Exception as exc:
            QMessageBox.warning(self, "Error", f"Failed to open cache folder: {exc}")

    def _extract_pikpak_id(self, path: Path) -> str | None:
        for part in reversed(path.parts):
            match = re.search(r"(PikPak\d+)", part, flags=re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def _find_pikpak_root(self, path: Path) -> Path | None:
        for idx, part in enumerate(path.parts):
            if re.match(r"^PikPak\d+$", part, flags=re.IGNORECASE):
                return Path(*path.parts[: idx + 1])
        return None

    def _find_additional_cctv_clip(
        self,
        main_path: Path,
        main_start: datetime | None,
        main_duration: float | None,
    ) -> Path | None:
        pikpak_root = self._find_pikpak_root(main_path)
        if pikpak_root is None:
            return None
        day_dir = main_path.parent
        if len(day_dir.parts) < 3:
            return None
        month_dir = day_dir.parent
        year_dir = month_dir.parent
        additional_day = pikpak_root / "AdditionalCCTV" / year_dir.name / month_dir.name / day_dir.name
        if not additional_day.exists():
            return None
        allowed = {".mp4", ".mov", ".mkv", ".avi"}
        candidates = []
        for entry in additional_day.iterdir():
            if not entry.is_file() or entry.suffix.lower() not in allowed:
                continue
            start_dt = parse_filename_datetime(entry)
            if start_dt is None:
                try:
                    start_dt = datetime.fromtimestamp(entry.stat().st_mtime)
                except Exception:
                    continue
            candidates.append((start_dt, entry))
        if not candidates:
            return None
        if main_start is None or main_duration is None:
            after = [c for c in candidates if main_start and c[0] >= main_start]
            if after:
                return min(after, key=lambda t: t[0])[1]
            return min(candidates, key=lambda t: t[0])[1]
        main_end = main_start + timedelta(seconds=main_duration)
        best_entry = None
        best_overlap = -1.0
        for start_dt, entry in candidates:
            duration = self._get_video_duration_seconds(entry)
            if duration is None:
                continue
            end_dt = start_dt + timedelta(seconds=duration)
            overlap = (min(main_end, end_dt) - max(main_start, start_dt)).total_seconds()
            if overlap > best_overlap:
                best_overlap = overlap
                best_entry = entry
        if best_entry is not None:
            return best_entry
        after = [c for c in candidates if c[0] >= main_start]
        if after:
            return min(after, key=lambda t: t[0])[1]
        return min(candidates, key=lambda t: t[0])[1]

    def load_additional_cctv_from_path(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, "File not found", str(path))
            return
        self._reset_secondary_video()
        self.secondary_video_original_path = path
        self.secondary_video_filename_dt = parse_filename_datetime(path)
        if self.secondary_video_filename_dt is None:
            try:
                self.secondary_video_filename_dt = datetime.fromtimestamp(path.stat().st_mtime)
            except Exception:
                self.secondary_video_filename_dt = None
        cached_path = None
        try:
            cached_path = self.get_valid_cached_path(path)
        except Exception:
            cached_path = None
        if cached_path is None:
            self._pending_secondary_original_path = path
            self._pending_secondary_last_size = None
            self._pending_secondary_stable_count = 0
            self.secondary_video_label.setText("Caching Additional CCTV...")
            self.secondary_video_label.setVisible(True)
            self._ensure_cached_copy_async(path)
            self._start_pending_secondary_timer()
            return
        self._open_secondary_from_path(cached_path, allow_rewrap=True)

    def _reset_secondary_video(self):
        if self.secondary_cap is not None:
            self.secondary_cap.release()
            self.secondary_cap = None
        self.secondary_fps = 25.0
        self.secondary_frame_count = 0
        self.secondary_current_frame = 0
        self.secondary_last_qimage = None
        self.secondary_video_path = None
        self.secondary_video_original_path = None
        self._pending_secondary_original_path = None
        self._pending_secondary_poll = False
        if self._pending_secondary_timer.isActive():
            self._pending_secondary_timer.stop()
        self._pending_secondary_last_size = None
        self._pending_secondary_stable_count = 0
        self.secondary_video_filename_dt = None
        self.secondary_video_start_dt = None
        self.secondary_ocr_offset_seconds = None
        self.secondary_ocr_frame_offset = 0
        self.secondary_manual_offset_frames = 0
        self._auto_secondary_ocr_attempted_key = None
        self._secondary_sync_done = False
        self._update_sync_button_style()
        self.secondary_video_label.setText("Additional CCTV not loaded")
        self.secondary_video_label.setVisible(False)
        self._draw_secondary_video = False
        self.secondary_locked = True
        self.secondary_lock_toggle.setEnabled(False)
        self._update_secondary_lock_style()
        self.secondary_sync_btn.setEnabled(False)

    def _ensure_cached_copy_async(self, path: Path):
        try:
            cache_path = self._cache_path_for(path)
        except Exception:
            return
        if self._is_cached_copy_current(path, cache_path):
            return
        def _copy():
            if not self._ensure_cached_copy(path, cache_path):
                return
            QMetaObject.invokeMethod(self, "_on_secondary_cache_copy_complete", Qt.QueuedConnection)
            QMetaObject.invokeMethod(self, "update_cache_status", Qt.QueuedConnection)
        self._cache_executor.submit(_copy)

    @Slot()
    def _on_secondary_cache_copy_complete(self):
        if self._pending_secondary_original_path is None:
            return
        self._open_secondary_cached(self._pending_secondary_original_path)

    def _open_secondary_cached(self, original_path: Path):
        if self._pending_secondary_original_path != original_path:
            return
        try:
            cache_path = self._cache_path_for(original_path)
        except Exception:
            return
        if not self._is_cached_copy_current(original_path, cache_path):
            return
        if not self._open_secondary_from_path(cache_path, allow_rewrap=False):
            self._start_pending_secondary_timer()

    def _open_secondary_from_path(self, path: Path, allow_rewrap: bool) -> bool:
        self.secondary_video_path = str(path)
        self.secondary_cap = cv2.VideoCapture(str(path))
        if not self.secondary_cap.isOpened():
            if allow_rewrap:
                fixed_path = self.try_rewrap_video_with_ffmpeg(str(path))
                if fixed_path:
                    self.secondary_cap.release()
                    self.secondary_cap = cv2.VideoCapture(fixed_path)
                    self.secondary_video_path = fixed_path
            if not self.secondary_cap.isOpened():
                self.secondary_cap = None
                return False
        self.secondary_fps = self.secondary_cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.secondary_frame_count = int(self.secondary_cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.secondary_current_frame = 0
        self.secondary_video_label.setVisible(True)
        self._draw_secondary_video = True
        self._update_secondary_frame_for_time(0.0)
        self._pending_secondary_original_path = None
        self._pending_secondary_poll = False
        if self._pending_secondary_timer.isActive():
            self._pending_secondary_timer.stop()
        self.secondary_locked = True
        key_path = self.secondary_video_original_path or Path(self.secondary_video_path)
        key = self._offset_cache_key(key_path, tag="additional")
        cached = self._get_cached_offset(key, self.secondary_offset_cache_path)
        if isinstance(cached, dict) and cached.get("source") == "additional":
            try:
                self.secondary_ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.secondary_ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.secondary_ocr_offset_seconds = None
                self.secondary_ocr_frame_offset = 0
            if self.secondary_ocr_offset_seconds is not None:
                filename_dt = self.secondary_video_filename_dt
                if filename_dt is None:
                    filename_dt = parse_filename_datetime(key_path)
                if filename_dt is None and self.secondary_video_original_path is not None:
                    try:
                        filename_dt = datetime.fromtimestamp(self.secondary_video_original_path.stat().st_mtime)
                    except Exception:
                        filename_dt = None
                if filename_dt:
                    self.secondary_video_start_dt = filename_dt + timedelta(seconds=self.secondary_ocr_offset_seconds)
                    self._secondary_sync_done = True
                    self._update_sync_button_style()
                    self._refresh_secondary_after_sync()
        if self.secondary_ocr_offset_seconds is None:
            settings = Settings.load()
            if settings.auto_ocr_open_on_missing or settings.auto_ocr_sync:
                self._auto_sync_secondary_with_ocr()
        self.secondary_lock_toggle.setEnabled(True)
        self._update_secondary_lock_style()
        self.secondary_sync_btn.setEnabled(True)
        self._update_sync_button_style()
        self.update_video_label()
        return True

    def _start_pending_secondary_timer(self):
        if self._pending_secondary_timer.isActive():
            return
        self._pending_secondary_poll = True
        self._pending_secondary_timer.start()

    def _poll_pending_secondary_cache(self):
        if self._pending_secondary_original_path is None:
            self._pending_secondary_poll = False
            if self._pending_secondary_timer.isActive():
                self._pending_secondary_timer.stop()
            return
        try:
            cache_path = self._cache_path_for(self._pending_secondary_original_path)
        except Exception:
            self._pending_secondary_poll = False
            if self._pending_secondary_timer.isActive():
                self._pending_secondary_timer.stop()
            return
        if cache_path.exists():
            try:
                size = cache_path.stat().st_size
            except Exception:
                size = None
            if size is not None and size == self._pending_secondary_last_size:
                self._pending_secondary_stable_count += 1
            else:
                self._pending_secondary_stable_count = 0
                self._pending_secondary_last_size = size
            if self._pending_secondary_stable_count >= 1:
                if self._open_secondary_from_path(cache_path, allow_rewrap=False):
                    return

    def _get_video_duration_seconds(self, path: Path) -> float | None:
        target_path = path
        if not self._is_path_in_cache(path):
            try:
                cached = self._cache_path_for(path)
            except Exception:
                cached = None
            if cached is None or not cached.exists():
                return None
            target_path = cached
        cap = cv2.VideoCapture(str(target_path))
        if not cap.isOpened():
            return None
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        cap.release()
        if fps <= 0 or frame_count <= 0:
            return None
        return float(frame_count) / float(fps)

    def _update_secondary_frame_for_time(self, t_seconds: float):
        t0 = time.perf_counter()
        if self.secondary_cap is None:
            if self._pending_secondary_original_path is not None:
                try:
                    cache_path = self._cache_path_for(self._pending_secondary_original_path)
                except Exception:
                    return
                if cache_path.exists():
                    try:
                        size = cache_path.stat().st_size
                    except Exception:
                        size = None
                    if size is not None and size == self._pending_secondary_last_size:
                        self._pending_secondary_stable_count += 1
                    else:
                        self._pending_secondary_stable_count = 0
                        self._pending_secondary_last_size = size
                    if self._pending_secondary_stable_count >= 1:
                        self._open_secondary_from_path(cache_path, allow_rewrap=False)
            if self.secondary_cap is None:
                dt = time.perf_counter() - t0
                if dt > 0.5:
                    print(f"[viewer] secondary update took {dt:.2f}s (no secondary)", flush=True)
                return
        if self.secondary_fps <= 0:
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] secondary update took {dt:.2f}s (no fps)", flush=True)
            return
        if self.video_start_dt is not None and self.secondary_video_start_dt is not None:
            adjusted_seconds = t_seconds
            if self.fps and self.fps > 0:
                adjusted_seconds += self.ocr_frame_offset / self.fps
            abs_time = self.video_start_dt + timedelta(seconds=adjusted_seconds)
            t2 = (abs_time - self.secondary_video_start_dt).total_seconds()
            if self.secondary_fps > 0:
                t2 -= self.secondary_ocr_frame_offset / self.secondary_fps
        else:
            t2 = t_seconds
        frame_index = int(round(t2 * self.secondary_fps)) + int(self.secondary_manual_offset_frames)
        if self.secondary_frame_count > 0:
            frame_index = max(0, min(self.secondary_frame_count - 1, frame_index))
        else:
            frame_index = max(0, frame_index)
        if frame_index == self.secondary_current_frame and self.secondary_last_qimage is not None:
            return
        self.secondary_current_frame = frame_index
        if not _position_capture_sequential(
            self.secondary_cap,
            self._seq_secondary_cap is self.secondary_cap,
            self._seq_secondary_next_frame,
            frame_index,
        ):
            self.secondary_cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        t_read = time.perf_counter()
        ret, frame = self.secondary_cap.read()
        read_dt = time.perf_counter() - t_read
        if read_dt > 0.5:
            print(f"[viewer] secondary frame read took {read_dt:.2f}s", flush=True)
        if ret:
            self._seq_secondary_cap = self.secondary_cap
            self._seq_secondary_next_frame = frame_index + 1
        else:
            self._seq_secondary_cap = None
        if not ret:
            dt = time.perf_counter() - t0
            if dt > 0.5:
                print(f"[viewer] secondary update took {dt:.2f}s (read fail)", flush=True)
            return
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        if not frame_rgb.flags["C_CONTIGUOUS"]:
            frame_rgb = frame_rgb.copy()
        h, w, ch = frame_rgb.shape
        bytes_per_line = frame_rgb.strides[0]
        self.secondary_last_qimage = QImage(
            frame_rgb.data,
            w,
            h,
            bytes_per_line,
            QImage.Format_RGB888,
        ).copy()
        dt = time.perf_counter() - t0
        if dt > 0.5:
            print(f"[viewer] secondary update took {dt:.2f}s", flush=True)

    def _load_offset_cache(self, cache_path: Path | None = None) -> dict:
        target = cache_path or self.offset_cache_path
        if not target:
            return {}
        if not target.exists():
            return {}
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_offset_cache(self, data: dict, cache_path: Path | None = None) -> None:
        target = cache_path or self.offset_cache_path
        if not target:
            return
        try:
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _get_cached_offset(self, key: str, cache_path: Path | None = None) -> dict | None:
        data = self._load_offset_cache(cache_path)
        offsets = data.get("offsets", {})
        if not isinstance(offsets, dict):
            return None
        item = offsets.get(key)
        if not isinstance(item, dict):
            return None
        return item

    def _offset_cache_key(self, path: Path, *, tag: str | None = None) -> str:
        pikpak_id = self._extract_pikpak_id(path) or "unknown"
        ts = parse_filename_datetime(path)
        ts_key = ts.strftime("%Y%m%d%H%M%S") if ts else path.stem
        suffix = f":{tag}" if tag else ""
        return f"{pikpak_id}:{ts_key}{suffix}"

    def _set_cached_offset(
        self,
        key: str,
        offset_seconds: float,
        frame_offset: int,
        *,
        source: str | None = None,
        cache_path: Path | None = None,
    ) -> None:
        data = self._load_offset_cache(cache_path)
        offsets = data.get("offsets")
        if not isinstance(offsets, dict):
            offsets = {}
            data["offsets"] = offsets
        entry = {
            "offset_seconds": float(offset_seconds),
            "frame_offset": int(frame_offset),
        }
        if source:
            entry["source"] = source
        offsets[key] = entry
        self._save_offset_cache(data, cache_path)

    def _clear_cached_offset(self, key: str, cache_path: Path | None = None) -> None:
        data = self._load_offset_cache(cache_path)
        offsets = data.get("offsets")
        if not isinstance(offsets, dict) or key not in offsets:
            return
        offsets.pop(key, None)
        self._save_offset_cache(data, cache_path)

    def _get_cached_video_for_ocr(self, path: Path) -> Path:
        if self._is_path_in_cache(path):
            return path
        if self._pending_video_load is not None and path == self._pending_video_load[1]:
            # This clip is downloading right now; don't start a second,
            # blocking copy of the same file — let OCR read the share.
            return path
        try:
            cache_path = self._cache_path_for(path)
            if not cache_path.exists():
                shutil.copy2(path, cache_path)
            return cache_path
        except Exception:
            return path

    def open_ocr_roi_tool(self, auto_start: bool = True, auto_close_on_success: bool = False):
        if not self.current_video_path:
            QMessageBox.information(self, "No video", "Load a video first.")
            return
        pikpak_id = self._extract_pikpak_id(Path(self.current_video_path))
        if not pikpak_id:
            QMessageBox.information(self, "No PikPak ID", "Unable to detect PikPak ID.")
            return
        key = self._offset_cache_key(Path(self.current_video_path))
        dlg = None

        def _on_offset_approved(video_start_dt, offset_seconds, frame_offset):
            try:
                self.ocr_offset_seconds = float(offset_seconds)
                self.ocr_frame_offset = int(frame_offset)
                self.video_start_dt = video_start_dt
                self._set_cached_offset(
                    key,
                    offset_seconds,
                    frame_offset,
                    cache_path=self.offset_cache_path,
                )
                self._apply_auto_sync_if_possible()
                self._main_sync_done = True
                self._update_sync_button_style()
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "OCR sync apply failed",
                    f"OCR found an offset, but applying it failed:\n{exc}",
                )
            finally:
                if auto_close_on_success and dlg is not None and self._ocr_tool_dialog is dlg:
                    QTimer.singleShot(0, dlg.close)

        if self._ocr_tool_dialog is not None:
            try:
                self._ocr_tool_dialog.close()
            except Exception:
                pass
        dlg = OcrVideoPlayer(
            settings_path=self.ocr_settings_path,
            settings_key=pikpak_id,
            auto_analyze=auto_start,
            on_offset_approved=_on_offset_approved,
        )
        dlg.video_label.setText("Preparing video...")
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda _=None: setattr(self, "_ocr_tool_dialog", None))
        dlg.resize(900, 600)
        dlg.show()
        self._ocr_tool_dialog = dlg
        def _open_after_show():
            if self._ocr_tool_dialog is not dlg:
                return
            ready_path = self._get_cached_video_for_ocr(Path(self.current_video_path))
            if self._ocr_tool_dialog is not dlg:
                return
            dlg.open_video(str(ready_path))
        QTimer.singleShot(0, _open_after_show)

    def open_secondary_ocr_tool(self, auto_start: bool = True, auto_close_on_success: bool = False):
        if not self.secondary_video_path:
            QMessageBox.information(self, "No video", "Load an additional CCTV clip first.")
            return
        key_path = self.secondary_video_original_path or Path(self.secondary_video_path)
        pikpak_id = self._extract_pikpak_id(key_path)
        if not pikpak_id:
            QMessageBox.information(self, "No PikPak ID", "Unable to detect PikPak ID.")
            return
        key = self._offset_cache_key(key_path, tag="additional")

        dlg = None

        def _on_offset_approved(video_start_dt, offset_seconds, frame_offset):
            try:
                self.secondary_ocr_offset_seconds = float(offset_seconds)
                self.secondary_ocr_frame_offset = int(frame_offset)
                self.secondary_video_start_dt = video_start_dt
                self._set_cached_offset(
                    key,
                    offset_seconds,
                    frame_offset,
                    source="additional",
                    cache_path=self.secondary_offset_cache_path,
                )
                self._refresh_secondary_after_sync()
                self._secondary_sync_done = True
                self._update_sync_button_style()
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Additional CCTV OCR apply failed",
                    f"OCR found an offset, but applying it failed:\n{exc}",
                )
            finally:
                if auto_close_on_success and dlg is not None and self._ocr_tool_dialog is dlg:
                    QTimer.singleShot(0, dlg.close)

        if self._ocr_tool_dialog is not None:
            try:
                self._ocr_tool_dialog.close()
            except Exception:
                pass
        dlg = OcrVideoPlayer(
            settings_path=self.ocr_settings_path,
            settings_key=pikpak_id,
            auto_analyze=auto_start,
            on_offset_approved=_on_offset_approved,
        )
        dlg.video_label.setText("Preparing video...")
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.destroyed.connect(lambda _=None: setattr(self, "_ocr_tool_dialog", None))
        dlg.resize(900, 600)
        dlg.show()
        self._ocr_tool_dialog = dlg
        def _open_after_show():
            if self._ocr_tool_dialog is not dlg:
                return
            ready_path = self._get_cached_video_for_ocr(Path(self.secondary_video_path))
            if self._ocr_tool_dialog is not dlg:
                return
            dlg.open_video(str(ready_path))
        QTimer.singleShot(0, _open_after_show)

    def recheck_ocr_offset(self):
        if not self.current_video_path:
            QMessageBox.information(self, "No video", "Load a video first.")
            return
        key = self._offset_cache_key(Path(self.current_video_path))
        self._clear_cached_offset(key, cache_path=self.offset_cache_path)
        self._auto_sync_with_ocr(force=True)

    def _auto_sync_with_ocr(self, force: bool = False):
        if not self.current_video_path:
            return
        path = Path(self.current_video_path)
        pikpak_id = self._extract_pikpak_id(path)
        key = self._offset_cache_key(path)
        settings = Settings.load()
        cached = None if force else self._get_cached_offset(key, self.offset_cache_path)
        if cached:
            try:
                self.ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.ocr_offset_seconds = None
                self.ocr_frame_offset = 0
            if self.ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(self.current_video_path)
                if filename_dt:
                    self.video_start_dt = filename_dt + timedelta(seconds=self.ocr_offset_seconds)
                    self._apply_auto_sync_if_possible()
                return
        if settings.auto_ocr_open_on_missing:
            if not pikpak_id:
                return
            if self._ocr_tool_dialog is not None:
                return
            if self._auto_ocr_attempted_key == key:
                return
            self._auto_ocr_attempted_key = key
            self.open_ocr_roi_tool(auto_start=True, auto_close_on_success=True)
            return
        if not settings.auto_ocr_sync and not force:
            return
        if not pikpak_id:
            return
        video_path = self._get_cached_video_for_ocr(Path(self.current_video_path))
        result = analyze_video_offset(
            str(video_path),
            settings_path=self.ocr_settings_path,
            settings_key=pikpak_id,
            parent=self,
        )
        if result is None:
            QMessageBox.information(
                self,
                "OCR failed",
                "OCR sync failed. Please adjust the ROI and try again.",
            )
            self.open_ocr_roi_tool(auto_start=False)
            return
        self.ocr_offset_seconds = result.offset_seconds
        self.ocr_frame_offset = result.frame_offset
        self.video_start_dt = result.video_start_dt
        self._set_cached_offset(
            key,
            result.offset_seconds,
            result.frame_offset,
            cache_path=self.offset_cache_path,
        )
        self._apply_auto_sync_if_possible()

    def _auto_sync_secondary_with_ocr(self, force: bool = False):
        if not self.secondary_video_path:
            return
        cache_path = Path(self.secondary_video_path)
        key_path = self.secondary_video_original_path or cache_path
        pikpak_id = self._extract_pikpak_id(key_path)
        key = self._offset_cache_key(key_path, tag="additional")
        settings = Settings.load()
        cached = None if force else self._get_cached_offset(key, self.secondary_offset_cache_path)
        if isinstance(cached, dict) and cached.get("source") != "additional":
            cached = None
        if cached:
            try:
                self.secondary_ocr_offset_seconds = float(cached.get("offset_seconds"))
                self.secondary_ocr_frame_offset = int(cached.get("frame_offset", 0))
            except Exception:
                self.secondary_ocr_offset_seconds = None
                self.secondary_ocr_frame_offset = 0
            if self.secondary_ocr_offset_seconds is not None:
                filename_dt = parse_filename_datetime(key_path)
                if filename_dt is None:
                    filename_dt = self.secondary_video_filename_dt
                if filename_dt is None and self.secondary_video_original_path is not None:
                    filename_dt = parse_filename_datetime(self.secondary_video_original_path)
                if filename_dt is None and self.secondary_video_original_path is not None:
                    try:
                        filename_dt = datetime.fromtimestamp(self.secondary_video_original_path.stat().st_mtime)
                    except Exception:
                        filename_dt = None
                if filename_dt:
                    self.secondary_video_start_dt = filename_dt + timedelta(seconds=self.secondary_ocr_offset_seconds)
                    self._refresh_secondary_after_sync()
                return
        if settings.auto_ocr_open_on_missing:
            if not pikpak_id:
                return
            if self._ocr_tool_dialog is not None:
                return
            if self._auto_secondary_ocr_attempted_key == key:
                return
            self._auto_secondary_ocr_attempted_key = key
            self.open_secondary_ocr_tool(auto_start=True, auto_close_on_success=True)
            return
        if not settings.auto_ocr_sync and not force:
            return
        if not pikpak_id:
            return
        video_path = self._get_cached_video_for_ocr(cache_path)
        result = analyze_video_offset(
            str(video_path),
            settings_path=self.ocr_settings_path,
            settings_key=pikpak_id,
            parent=self,
        )
        if result is None:
            QMessageBox.information(
                self,
                "Additional CCTV OCR failed",
                "OCR sync failed for the additional CCTV clip.",
            )
            return
        self.secondary_ocr_offset_seconds = result.offset_seconds
        self.secondary_ocr_frame_offset = result.frame_offset
        self.secondary_video_start_dt = result.video_start_dt
        self._set_cached_offset(
            key,
            result.offset_seconds,
            result.frame_offset,
            source="additional",
            cache_path=self.secondary_offset_cache_path,
        )
        self._refresh_secondary_after_sync()

    def _refresh_secondary_after_sync(self):
        if self.secondary_cap is None or self.secondary_fps <= 0:
            return
        t = self.current_frame / self.fps if self.fps > 0 else 0.0
        self._update_secondary_frame_for_time(t)
        self.update_video_label()

    def _apply_auto_sync_if_possible(self):
        if self.video_start_dt is None or self.first_log_dt is None:
            if self.external_markers and self.ocr_offset_seconds is not None:
                self._refresh_timeline_marker_bar()
            return
        local_video_start = _to_local_naive(self.video_start_dt)
        if local_video_start is None:
            return
        self.video_start_dt = local_video_start
        sync_offset = (self.first_log_dt - local_video_start).total_seconds()
        self.sync_offset = sync_offset
        self.time_offset = 0.0
        self.set_offset_value(0.0)
        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_time_and_overlay(t, self.current_frame)
            self.update_log_highlight(t)
        self.log_markers_enabled = True
        self._update_timeline_markers()
        self._refresh_timeline_marker_bar()

    def _update_timeline_markers(self):
        if not self.events or self.cap is None:
            self._set_log_markers([])
            return
        offset = self.effective_offset()
        markers: list[tuple[float, str]] = []
        for ev in self.events:
            try:
                markers.append((ev.start.total_seconds() + offset, "#ffcc00"))
            except Exception:
                continue
        self._set_log_markers(markers)

    # ---- Elastic log loading ----

    def load_logs_from_elastic(self, pikpak_path: str, start_iso: str, end_iso: str, show_busy: bool = True):
        request_key = (str(pikpak_path), str(start_iso), str(end_iso))
        if self._loaded_log_request_key == request_key and self.all_events:
            return
        if self._active_log_request_key == request_key and self._log_future is not None:
            return
        try:
            start_dt = self._parse_iso(start_iso)
            end_dt = self._parse_iso(end_iso)
        except Exception:
            QMessageBox.warning(self, "Invalid time range", "Could not parse provided timestamps.")
            return
        self._cancel_log_future()
        self._active_log_request_key = request_key
        print("[viewer] load_logs_from_elastic starting", flush=True)
        self._log_future_id += 1
        fetch_id = self._log_future_id
        settings = Settings.load()
        if self._log_executor is None:
            self._log_executor = ThreadPoolExecutor(max_workers=1)
        self._log_future = self._log_executor.submit(
            fetch_logs_for_range,
            settings,
            Path(pikpak_path),
            start_dt,
            end_dt,
        )
        if show_busy:
            self._set_log_busy(True, "Fetching Elastic logs...")
        print(f"[viewer] scheduled log fetch id {fetch_id}", flush=True)
        self._poll_log_future(fetch_id)

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        val = value.strip()
        if val.endswith("Z"):
            val = val[:-1] + "+00:00"
        return datetime.fromisoformat(val)

    def _set_log_busy(self, busy: bool, message: str | None = None):
        if busy:
            if self._log_busy_dialog is None:
                dlg = QProgressDialog(message or "Working...", None, 0, 0, self)
                dlg.setWindowTitle("Log Viewer")
                dlg.setCancelButton(None)
                dlg.setWindowModality(Qt.NonModal)
                dlg.setMinimumDuration(0)
                dlg.setRange(0, 0)
                self._log_busy_dialog = dlg
            self._log_busy_dialog.setLabelText(message or "Working...")
            self._log_busy_dialog.show()
            QApplication.processEvents()
        else:
            if self._log_busy_dialog is not None:
                self._log_busy_dialog.close()
                self._log_busy_dialog = None

    def _on_elastic_logs_ready(self, rows: list):
        print(f"[viewer] _on_elastic_logs_ready (rows={len(rows)})", flush=True)
        self._set_log_busy(False)
        if not rows:
            QMessageBox.information(self, "No events", "No Elastic events found for this clip timeframe.")
            self._clear_events()
            return
        self._apply_loaded_events(*build_events_from_rows(rows))
        # Avoid modal dialog here; it can re-enter UI updates during heavy redraw.
        print("[viewer] events loaded", flush=True)

    def _on_elastic_logs_failed(self, message: str):
        print(f"[viewer] _on_elastic_logs_failed: {message}", flush=True)
        self._set_log_busy(False)
        if message:
            QMessageBox.warning(self, "Elastic fetch failed", message)

    def _poll_log_future(self, fetch_id: int):
        future = self._log_future
        if future is None or fetch_id != self._log_future_id:
            return
        if future.done():
            self._log_future = None
            try:
                rows = future.result()
            except ElasticFetchError as exc:
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} partial failure: {exc}", flush=True)
                if exc.items:
                    print(
                        f"[viewer] delivering {len(exc.items)} partial rows despite failure",
                        flush=True,
                    )
                    self._loaded_log_request_key = self._active_log_request_key
                    self.logs_ready.emit(exc.items)
                self.logs_failed.emit(str(exc))
                return
            except Exception as exc:
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} failed: {exc}", flush=True)
                self.logs_failed.emit(str(exc))
                return
            else:
                self._loaded_log_request_key = self._active_log_request_key
                self._active_log_request_key = None
                print(f"[viewer] log future {fetch_id} completed with {len(rows)} rows", flush=True)
                print("[viewer] invoking _on_elastic_logs_ready", flush=True)
                self.logs_ready.emit(rows)
                print("[viewer] returned from _on_elastic_logs_ready", flush=True)
        else:
            QTimer.singleShot(100, lambda fid=fetch_id: self._poll_log_future(fid))

    def _cancel_log_future(self):
        future = self._log_future
        self._log_future = None
        self._active_log_request_key = None
        if future is None:
            return
        print("[viewer] cancelling prior log future", flush=True)
        future.cancel()

    def closeEvent(self, event):
        try:
            self._flush_settings_autosave()
        except Exception:
            pass
        self._cancel_log_future()
        if self._log_executor:
            try:
                self._log_executor.shutdown(wait=False)
            except Exception:
                pass
            self._log_executor = None
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Left:
            self.scrub_by_frames(-1)
            event.accept()
            return
        if event.key() == Qt.Key_Right:
            self.scrub_by_frames(1)
            event.accept()
            return
        if event.key() == Qt.Key_Up:
            self._jump_to_adjacent_event(-1)
            event.accept()
            return
        if event.key() == Qt.Key_Down:
            self._jump_to_adjacent_event(1)
            event.accept()
            return
        super().keyPressEvent(event)

    def _jump_to_adjacent_event(self, direction: int):
        if not self.events or self.cap is None or self._log_model.rowCount() == 0:
            return
        current_row = self.log_list.currentIndex().row()
        if current_row == -1:
            # No selection yet; pick the closest event to current time.
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            if self.fps > 0 and self.ocr_frame_offset:
                t += float(self.ocr_frame_offset) / float(self.fps)
            current_td = timedelta(seconds=t) - timedelta(seconds=self.effective_offset())
            try:
                closest = min(
                    range(len(self.events)),
                    key=lambda idx: abs((self.events[idx].start - current_td).total_seconds()),
                )
            except ValueError:
                return
            current_row = closest
        target = max(0, min(len(self.events) - 1, current_row + direction))
        target_index = self._log_model.index(target)
        self.log_list.setCurrentIndex(target_index)
        self._on_log_item_clicked(target_index)


def main():
    parser = argparse.ArgumentParser(description="Video + Log Viewer")
    parser.add_argument("--video", help="Video file to open on startup")
    parser.add_argument("--pikpak", help="Path to PikPak folder for Elastic lookups")
    parser.add_argument("--start", help="Clip start time (ISO) for Elastic query")
    parser.add_argument("--end", help="Clip end time (ISO) for Elastic query")
    args, qt_args = parser.parse_known_args()

    app = QApplication([sys.argv[0]] + qt_args)
    win = VideoLogViewer()
    win.resize(1400, 700)
    win.show()
    if args.video:
        win.load_video_from_path(args.video)
    if args.pikpak and args.start and args.end:
        win.load_logs_from_elastic(args.pikpak, args.start, args.end)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
