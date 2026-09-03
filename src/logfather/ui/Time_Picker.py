from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable, Optional, Dict, Tuple, List

from PySide6.QtCore import Qt, Signal, QEvent, QThread, QRectF, QPointF, QTimer

from logfather.ui.qt_worker import JobSlot
from PySide6.QtGui import QBrush, QColor, QPen, QPolygonF, QFont, QFontMetrics
from PySide6.QtWidgets import QApplication, QProgressDialog, QMessageBox, QMenu
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout,
    QGraphicsScene, QGraphicsView, QGraphicsRectItem, QGraphicsItem,
    QGraphicsPolygonItem, QGraphicsLineItem
)

from logfather.data.elastic_errors import ElasticFetchError
# Re-exported for the many UI modules that import these from Time_Picker.
from logfather.core.timeline_model import (  # noqa: F401
    LAST_BLOCK_DURATION,
    LOCAL_TIMEZONE,
    MIN_BLOCK_DURATION,
    TimelineItem,
    VIDEO_COLOR_CACHED,
    VIDEO_COLOR_SELECTED,
    VIDEO_COLOR_UNCACHED,
    _annotations_path_for,
    _build_annotation_index,
    _build_cache_index,
    _cache_key_for,
    _has_annotations,
    _is_path_cached,
    _path_key,
    ensure_local,
    ensure_playhead_local,
    ensure_utc,
    format_local_time,
    format_uk_date,
    inferred_live_clip_end,
    load_day_files,
    local_day_end_utc,
    local_day_start_utc,
    parse_time_from_name,
)

TIMELINE_TIMING_LOGS = True
SHOW_TIMELINE_INFO_TEXT = False
SHOW_TIMELINE_TOP_BUTTONS = False
DAY_RATE_PROXY_BUCKET_SECONDS = 300
DAY_RATE_PROXY_TERMS = ("eject", "crate")


def _timeline_perf_log(message: str) -> None:
    if TIMELINE_TIMING_LOGS:
        print(f"[timeline-perf] {message}", flush=True)


class VideoRectItem(QGraphicsRectItem):
    def __init__(self, rect, timeline_item: TimelineItem, picker: "TimePicker"):
        super().__init__(rect)
        self._timeline_item = timeline_item
        self._picker = picker
        self._display_text = ""
        self._display_color = QColor("#1a1a1a")
        self._display_font: QFont | None = None
        # Thumbnails disabled to avoid crashes; keep simple solid-color blocks.
        self.setAcceptHoverEvents(False)
        self.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)

    def set_display_text(self, text: str, color: QColor | None = None, font: QFont | None = None):
        self._display_text = text or ""
        if color is not None:
            self._display_color = QColor(color)
        if font is not None:
            self._display_font = font
        self.update()

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if not self._display_text:
            return
        painter.save()
        if self._display_font is not None:
            painter.setFont(self._display_font)
        painter.setPen(self._display_color)
        painter.drawText(self.rect(), Qt.AlignCenter, self._display_text)
        painter.restore()


class TimePicker(QWidget):
    time_selected = Signal(object)  # TimelineItem
    items_changed = Signal()

    def __init__(self, load_func: Optional[Callable[[Path, date], Iterable[Path]]] = None,
                 extra_loaders: Optional[list[Callable[[Path, date, Optional[datetime]], Iterable[TimelineItem]]]] = None,
                 static_tracks: Optional[List[Tuple[str, str, str]]] = None,
                 cache_root: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Time Picker")
        self._load_func = load_func
        self._extra_loaders = extra_loaders or []
        self._items: list[TimelineItem] = []
        # static_tracks entries: (kind, label, color_hex)
        self._static_tracks = static_tracks or [("video", "Video", "#cce5ff")]

        self.info = QLabel("Pick a date to list times.")
        self.info.setVisible(SHOW_TIMELINE_INFO_TEXT)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_clicked)
        self.fit_btn = QPushButton("Fit")
        self.fit_btn.clicked.connect(self._fit_to_items)


        top = QHBoxLayout()
        top.addStretch(1)
        top.addWidget(self.fit_btn)
        top.addWidget(self.refresh_btn)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setMinimumHeight(260)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.scene.selectionChanged.connect(self._emit_selection_from_timeline)
        self.view.horizontalScrollBar().valueChanged.connect(lambda _v: self._reposition_track_labels())

        layout = QVBoxLayout()
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(6)
        if SHOW_TIMELINE_TOP_BUTTONS:
            layout.addLayout(top)
        layout.addWidget(self.view, 1)
        self.setLayout(layout)

        self._current_root: Optional[Path] = None
        self._current_date: Optional[date] = None
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_time: Optional[datetime] = None
        self._ppm = 5
        self._day_start: Optional[datetime] = None
        self._baseline_y = 28
        self._scale_y = 4
        self._track_labels: Dict[str, object] = {}
        self._track_counts: Dict[str, object] = {}
        self._track_positions: Dict[str, float] = {}
        self._busy = False
        self._loading_rect = None
        self._loading_text = None
        self._loader_slot = JobSlot(self)
        self._progress: QProgressDialog | None = None
        self._last_cursor_x: Optional[float] = None
        self._cache_root = cache_root
        self._selected_video_item: Optional[TimelineItem] = None
        self._video_rects: dict[int, QGraphicsRectItem] = {}
        self._target_rate_day_buckets: list[dict] = []
        self._target_rate_clip_buckets: list[dict] = []
        self._target_rate_clip_start: Optional[datetime] = None
        self._target_rate_clip_end: Optional[datetime] = None
        self._suppress_selection_emit = False
        self._pending_time_selected = None
        self._time_selected_emit_scheduled = False
        # Rebuilding the scene on every resize event makes splitter drags
        # feel heavy; coalesce bursts into one redraw.
        self._resize_redraw_timer = QTimer(self)
        self._resize_redraw_timer.setSingleShot(True)
        self._resize_redraw_timer.setInterval(120)
        self._resize_redraw_timer.timeout.connect(self._fit_to_items)

    def set_loader(self, func: Callable[[Path, date], Iterable[Path]]):
        self._load_func = func

    @property
    def current_root(self) -> Optional[Path]:
        return self._current_root

    def show_times(self, pikpak_root: Optional[Path], day: Optional[date]):
        # Stop any in-flight loads so stale results can't repopulate after switching PikPak/day.
        self._stop_loader_thread()
        self._current_root = pikpak_root
        self._current_date = day
        self._items.clear()
        self.scene.clear()
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_time = None
        self._track_positions = {}
        self._selected_video_item = None
        self._target_rate_day_buckets = []
        self._target_rate_clip_buckets = []
        self._target_rate_clip_start = None
        self._target_rate_clip_end = None

        if not day:
            self._set_info_text("Pick a date to list times.")
            return

        load_root = pikpak_root
        load_func = self._load_func
        if load_root is None:
            if not self._extra_loaders:
                self._set_info_text("Pick a date to list times.")
                return
            load_root = Path(".")
            load_func = lambda _root, _day: []
            self._set_info_text(f"Loading logs for {format_uk_date(day)}...")
        elif not load_func:
            self._set_info_text("Pick a date to list times.")
            return

        self._set_busy(True, f"Loading items for {format_uk_date(day)}...")
        extra_loaders = list(self._extra_loaders)
        cache_root = self._cache_root
        self._loader_slot.start(
            lambda job: _load_timeline_items(job, load_root, day, load_func, extra_loaders, cache_root),
            on_result=self._on_load_result,
            on_error=self._on_load_failed,
            on_progress=self._on_load_progress,
        )

    def _redraw_timeline(self):
        print("[timeline] _redraw_timeline", flush=True)
        self.scene.clear()
        self._video_rects = {}
        if not self._items or not self._current_date:
            return

        day_start = local_day_start_utc(self._current_date)
        self._day_start = day_start
        ppm = self._ppm
        total_minutes = 24 * 60
        height = 80
        baseline_y = self._baseline_y
        scale_y = self._scale_y

        # Background track
        self.scene.addRect(0, baseline_y - 10, total_minutes * ppm, 20, QPen(Qt.NoPen), QBrush(QColor("#2e2e2e")))
        self._draw_day_rate_heat_strip(total_minutes * ppm)

        # Time scale across the top (dynamic tick density).
        # Define step in minutes based on zoom.
        if ppm >= 20:
            step_min = 5
        elif ppm >= 12:
            step_min = 10
        elif ppm >= 8:
            step_min = 15
        elif ppm >= 5:
            step_min = 30
        else:
            step_min = 60

        # Label frequency adjusts with zoom to keep labels visible.
        if ppm >= 20:
            label_every_min = step_min  # label every tick
        elif ppm >= 12:
            label_every_min = step_min * 2
        elif ppm >= 8:
            label_every_min = step_min * 2
        elif ppm >= 5:
            label_every_min = step_min * 2
        else:
            label_every_min = 60  # hourly labels at low zoom
        minor_height = 6
        major_height = 12
        for minute in range(0, total_minutes + 1, step_min):
            x = minute * ppm
            is_major = (minute % label_every_min) == 0
            height_tick = major_height if is_major else minor_height
            pen = QPen(QColor("#666666") if is_major else QColor("#4d4d4d"))
            tick_item = self.scene.addLine(x, scale_y, x, scale_y + height_tick, pen)
            tick_item.setZValue(1)
            if is_major:
                hour = minute // 60
                label = self.scene.addText(f"{hour:02d}:{minute % 60:02d}")
                label.setDefaultTextColor(QColor("#cccccc"))
                label.setPos(x + 2, scale_y - 14)
                label.setZValue(2)

        # Track stacking: keep video at baseline; stack other tracks below, tighter spacing.
        kinds = []
        label_map = {}
        color_map = {}
        for kind, label, color_hex in self._static_tracks:
            if kind not in kinds:
                kinds.append(kind)
                label_map[kind] = label
                if color_hex:
                    color_map[kind] = QColor(color_hex)
        seen = set(kinds)
        for item in self._items:
            if item.kind not in seen:
                seen.add(item.kind)
                kinds.append(item.kind)
            if item.kind not in label_map:
                label_map[item.kind] = item.track_label or item.kind.capitalize()
            if item.kind not in color_map:
                color_map[item.kind] = item.color

        track_map = {}
        spacing = 24
        non_video_start = baseline_y + 32
        if "video" in kinds:
            track_map["video"] = baseline_y
        next_row = non_video_start
        for kind in kinds:
            if kind == "video":
                continue
            track_map[kind] = next_row
            next_row += spacing

        for item in self._items:
            start_offset_min = max(0, (item.start - day_start).total_seconds() / 60.0)
            end_offset_min = (item.end - day_start).total_seconds() / 60.0
            width = max(2.0, (end_offset_min - start_offset_min) * ppm)
            x = max(0.0, start_offset_min * ppm)

            y_center = track_map.get(item.kind, baseline_y)

            if item.kind not in ("video", "additional", "sku") or width <= 4:
                # Draw tick mark for events or near-zero duration.
                pen = QPen(QColor(item.color))
                pen.setWidth(2)
                tick = self.scene.addLine(x, y_center - 8, x, y_center + 8, pen)
                tick.setData(0, item)
                tooltip = f"{format_local_time(item.start)}\n{item.label}"
                extra = self._tooltip_extra_lines(item)
                if extra:
                    tooltip = f"{tooltip}\n" + "\n".join(extra)
                tick.setToolTip(tooltip)
                tick.setZValue(2)
            else:
                rect = VideoRectItem(QRectF(x, y_center - 12, width, 24), item, self)
                rect.setPen(QPen(QColor("#0b1a33") if item.kind == "video" else QColor("#444444")))
                if item.kind == "video":
                    rect.setBrush(QBrush(self._color_for_video_item(item)))
                else:
                    rect.setBrush(QBrush(QColor(item.color)))
                rect.setData(0, item)
                if item.kind == "sku":
                    display_text, tooltip = self._sku_box_text(item, rect.rect().width())
                    font = QFont()
                    font.setPointSize(9)
                    rect.set_display_text(display_text, QColor("#1a1a1a"), font)
                    rect.setToolTip(tooltip or "")
                else:
                    tooltip = f"{format_local_time(item.start)} - {format_local_time(item.end)}\n{item.label}"
                    extra = self._tooltip_extra_lines(item)
                    if extra:
                        tooltip = f"{tooltip}\n" + "\n".join(extra)
                    rect.setToolTip(tooltip)
                rect.setFlag(QGraphicsRectItem.ItemIsSelectable, True)
                self.scene.addItem(rect)
                if item.kind == "video" and item.annotated:
                    self._add_pen_icon(rect)
                self._video_rects[id(item)] = rect

        self._draw_selected_clip_rate_heat()

        # Track labels on the left
        self._track_labels = {}
        self._track_counts = {}
        self._track_positions = track_map
        # Precompute counts per kind
        count_map: Dict[str, int] = {}
        for item in self._items:
            count_map[item.kind] = count_map.get(item.kind, 0) + 1

        for kind, y in track_map.items():
            text = "" if kind == "video" else label_map.get(kind, kind.capitalize())
            label_item = self.scene.addText(text)
            label_item.setDefaultTextColor(color_map.get(kind, QColor("#cccccc")))
            label_item.setZValue(3)
            self._track_labels[kind] = label_item

            count_val = count_map.get(kind, 0)
            count_item = self.scene.addText(f"{count_val}")
            count_item.setDefaultTextColor(color_map.get(kind, QColor("#cccccc")))
            count_item.setZValue(3)
            self._track_counts[kind] = count_item
        self._reposition_track_labels()

        # Scene height adjusts to number of tracks
        total_tracks = max(1, len(track_map))
        # Add top/side margin so cursor/time labels aren't clipped.
        self.scene.setSceneRect(-60, -40, total_minutes * ppm + 120, height + (total_tracks - 1) * spacing + 60)
        # Auto-scroll to the first item if it's off-screen.
        if self._items:
            first = self._items[0]
            first_x = max(0.0, (first.start - day_start).total_seconds() / 60.0 * ppm)
            self.view.centerOn(first_x, baseline_y)
        # Reset cursor overlay
        self._cursor_line = None
        self._cursor_label = None
        self._cursor_marker_outer = None
        self._cursor_marker_inner = None
        self._playhead_line = None
        self._playhead_time = None
        self._last_cursor_x = None
        self._update_playhead_indicator()

    def _refresh_clicked(self):
        self.show_times(self._current_root, self._current_date)

    @staticmethod
    def _tooltip_extra_lines(item: TimelineItem) -> list[str]:
        payload = item.payload
        if not isinstance(payload, dict):
            return []
        lines: list[str] = []
        sku = payload.get("_ui_sku")
        tray = payload.get("_ui_tray")
        tool = payload.get("_ui_tool")
        if sku:
            lines.append(f"SKU: {sku}")
        if tray:
            lines.append(f"Tray: {tray}")
        if tool:
            lines.append(f"Tool: {tool}")
        return lines

    @staticmethod
    def _fit_text(text: str, max_width: float, metrics: QFontMetrics) -> str:
        if metrics.horizontalAdvance(text) <= max_width:
            return text
        ellipsis = "..."
        if metrics.horizontalAdvance(ellipsis) > max_width:
            return ""
        trimmed = text
        while trimmed and metrics.horizontalAdvance(trimmed + ellipsis) > max_width:
            trimmed = trimmed[:-1]
        return trimmed + ellipsis if trimmed else ""

    def _sku_box_text(self, item: TimelineItem, width: float) -> tuple[str, str]:
        payload = item.payload if isinstance(item.payload, dict) else {}
        font = QFont()
        font.setPointSize(9)
        metrics = QFontMetrics(font)
        is_manual = bool(payload.get("_ui_manual"))
        if is_manual:
            fitted = self._fit_text("Manual", width - 8, metrics)
            tooltip = "" if fitted == "Manual" else "Manual Mode"
            return fitted, tooltip
        sku = payload.get("_ui_sku") or item.label
        tray = payload.get("_ui_tray") or ""
        tool = payload.get("_ui_tool") or ""
        parts = [p for p in [sku, tray, tool] if p]
        full = " | ".join(parts)
        if full:
            fitted_full = self._fit_text(full, width - 8, metrics)
            if fitted_full == full:
                return fitted_full, ""
        fitted_sku = self._fit_text(str(sku), width - 8, metrics)
        tooltip_lines: list[str] = []
        if str(fitted_sku) != str(sku) and sku:
            tooltip_lines.append(f"SKU: {sku}")
        if tray:
            tooltip_lines.append(f"Tray: {tray}")
        if tool:
            tooltip_lines.append(f"Tool: {tool}")
        tooltip = "\n".join(tooltip_lines)
        return fitted_sku, tooltip

    @staticmethod
    def _is_day_rate_proxy_item(item: TimelineItem) -> bool:
        if item is None or not str(item.kind).startswith("cond_"):
            return False
        parts = [str(item.track_label or ""), str(item.label or "")]
        payload = item.payload if isinstance(item.payload, dict) else {}
        src = payload.get("_source") if isinstance(payload, dict) else None
        if isinstance(src, dict):
            parts.extend([
                str(src.get("message") or ""),
                str(src.get("state_name") or ""),
                str(src.get("source") or ""),
            ])
        haystack = " ".join(parts).lower()
        return all(term in haystack for term in DAY_RATE_PROXY_TERMS)

    def _build_day_rate_proxy_buckets(self, items: list[TimelineItem]) -> list[dict]:
        if not self._current_date:
            return []
        day_start = local_day_start_utc(self._current_date)
        day_end = day_start + timedelta(days=1)
        bucket_seconds = DAY_RATE_PROXY_BUCKET_SECONDS
        bucket_count = max(1, int((day_end - day_start).total_seconds() // bucket_seconds))
        counts = [0] * bucket_count
        matched = False
        for item in items:
            if not self._is_day_rate_proxy_item(item):
                continue
            matched = True
            offset_seconds = (ensure_utc(item.start) - day_start).total_seconds()
            if offset_seconds < 0:
                continue
            idx = int(offset_seconds // bucket_seconds)
            if 0 <= idx < bucket_count:
                counts[idx] += 1
        if not matched:
            return []
        buckets: list[dict] = []
        for idx, count in enumerate(counts):
            start = day_start + timedelta(seconds=idx * bucket_seconds)
            buckets.append({
                "start": start,
                "end": start + timedelta(seconds=bucket_seconds),
                "count": int(count),
            })
        return buckets

    @staticmethod
    def _heat_color(count: int, max_count: int, *, empty_alpha: int = 24, full_alpha: int = 220) -> QColor:
        if max_count <= 0 or count <= 0:
            return QColor(28, 44, 54, empty_alpha)
        ratio = min(1.0, max(0.0, float(count) / float(max_count)))
        ratio = math.sqrt(ratio)
        cold = QColor("#123047")
        warm = QColor("#f59e0b")
        hot = QColor("#ef4444")
        if ratio < 0.6:
            local = ratio / 0.6
            r = int(cold.red() + (warm.red() - cold.red()) * local)
            g = int(cold.green() + (warm.green() - cold.green()) * local)
            b = int(cold.blue() + (warm.blue() - cold.blue()) * local)
        else:
            local = (ratio - 0.6) / 0.4
            r = int(warm.red() + (hot.red() - warm.red()) * local)
            g = int(warm.green() + (hot.green() - warm.green()) * local)
            b = int(warm.blue() + (hot.blue() - warm.blue()) * local)
        alpha = int(empty_alpha + (full_alpha - empty_alpha) * ratio)
        return QColor(r, g, b, alpha)

    def _draw_day_rate_heat_strip(self, scene_width: float) -> None:
        if not self._target_rate_day_buckets or not self._day_start or self._ppm <= 0:
            return
        strip_y = self._scale_y + 10
        strip_h = 8
        max_count = max((int(bucket.get("count", 0)) for bucket in self._target_rate_day_buckets), default=0)
        for bucket in self._target_rate_day_buckets:
            start = ensure_utc(bucket["start"])
            end = ensure_utc(bucket["end"])
            count = int(bucket.get("count", 0) or 0)
            start_min = max(0.0, (start - self._day_start).total_seconds() / 60.0)
            end_min = max(start_min, (end - self._day_start).total_seconds() / 60.0)
            x = max(0.0, min(scene_width, start_min * self._ppm))
            width = max(1.0, min(scene_width - x, (end_min - start_min) * self._ppm))
            color = self._heat_color(count, max_count)
            rect = self.scene.addRect(QRectF(x, strip_y, width, strip_h), QPen(Qt.NoPen), QBrush(color))
            rect.setZValue(1.5)
            rect.setAcceptedMouseButtons(Qt.NoButton)
            rect.setToolTip(f"Day proxy {format_local_time(start)}  count={count}")
        label = self.scene.addText("Rate")
        label.setDefaultTextColor(QColor("#9fb3c8"))
        font = QFont()
        font.setPointSize(8)
        font.setBold(True)
        label.setFont(font)
        label.setPos(-36, strip_y - 4)
        label.setZValue(3)
        label.setAcceptedMouseButtons(Qt.NoButton)

    def _draw_selected_clip_rate_heat(self) -> None:
        item = self._selected_video_item
        if item is None or not self._target_rate_clip_buckets:
            return
        rect = self._video_rects.get(id(item))
        if rect is None:
            return
        clip_start = self._target_rate_clip_start or item.start
        clip_end = self._target_rate_clip_end or item.end
        if clip_start is None or clip_end is None or clip_end <= clip_start:
            return
        clip_start_utc = ensure_utc(clip_start)
        clip_end_utc = ensure_utc(clip_end)
        clip_seconds = max(1.0, (clip_end_utc - clip_start_utc).total_seconds())
        rect_geom = rect.rect()
        bar_y = rect_geom.bottom() - 6
        bar_h = 5
        max_count = max((int(bucket.get("count", 0)) for bucket in self._target_rate_clip_buckets), default=0)
        for bucket in self._target_rate_clip_buckets:
            start = max(clip_start_utc, ensure_utc(bucket["start"]))
            end = min(clip_end_utc, ensure_utc(bucket["end"]))
            if end <= start:
                continue
            start_ratio = (start - clip_start_utc).total_seconds() / clip_seconds
            end_ratio = (end - clip_start_utc).total_seconds() / clip_seconds
            x = rect_geom.x() + rect_geom.width() * start_ratio
            width = max(1.0, rect_geom.width() * (end_ratio - start_ratio))
            count = int(bucket.get("count", 0) or 0)
            color = self._heat_color(count, max_count, empty_alpha=30, full_alpha=235)
            child = QGraphicsRectItem(QRectF(x, bar_y, width, bar_h), rect)
            child.setPen(QPen(Qt.NoPen))
            child.setBrush(QBrush(color))
            child.setZValue(4)
            child.setAcceptedMouseButtons(Qt.NoButton)
            child.setToolTip(f"Clip detail {format_local_time(start)}  count={count}")

    def set_clip_target_rate_heat(
        self,
        clip_start: Optional[datetime],
        clip_end: Optional[datetime],
        buckets: list[dict] | None,
    ) -> None:
        self._target_rate_clip_start = clip_start
        self._target_rate_clip_end = clip_end
        self._target_rate_clip_buckets = list(buckets or [])
        if self._items and self._current_date:
            h_value = self.view.horizontalScrollBar().value()
            self._suppress_selection_emit = True
            try:
                self._redraw_timeline()
                self.view.horizontalScrollBar().setValue(h_value)
            finally:
                self._suppress_selection_emit = False

    def clear_clip_target_rate_heat(self) -> None:
        self.set_clip_target_rate_heat(None, None, [])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._items and self._current_date:
            self._resize_redraw_timer.start()

    def _set_busy(self, busy: bool, message: str | None = None):
        if busy == self._busy:
            return
        self._busy = busy
        if message:
            self._set_info_text(message)
        if busy:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            if not self._items:
                self.view.setEnabled(False)
                self._show_loading_overlay()
            if self._progress is None:
                self._progress = QProgressDialog(message or "Loading...", None, 0, 0, self)
                self._progress.setWindowTitle("Loading timeline")
                self._progress.setWindowModality(Qt.NonModal)
                self._progress.setCancelButton(None)
                self._progress.setMinimumDuration(0)
                self._progress.setRange(0, 0)  # indefinite spinner/progress
                self._progress.show()
            QApplication.processEvents()
        else:
            QApplication.restoreOverrideCursor()
            self.view.setEnabled(True)
            if self._progress:
                self._progress.close()
                self._progress = None
            self._hide_loading_overlay()

    def _on_loaded(self, items: list[TimelineItem], day_loaded, root_loaded):
        # Ignore if stale
        if self._current_date != day_loaded or self._current_root != root_loaded:
            self._set_busy(False)
            return
        self._items = items
        self._target_rate_day_buckets = self._build_day_rate_proxy_buckets(self._items)
        self._fit_to_items()
        if items:
            self._set_info_text(f"{len(items)} items on {format_uk_date(day_loaded)}. Click timeline to select.")
        else:
            self._set_info_text(f"No files on {format_uk_date(day_loaded)}.")
        self.items_changed.emit()
        self._set_busy(False)

    def _on_partial_loaded(self, items: list[TimelineItem], day_loaded, append: bool, root_loaded):
        if self._current_date != day_loaded or self._current_root != root_loaded:
            return
        if not items:
            return
        if append:
            self._items.extend(items)
        else:
            self._items = list(items)
        self._target_rate_day_buckets = self._build_day_rate_proxy_buckets(self._items)
        self._fit_to_items()
        if self._items:
            self._set_info_text(
                f"{len(self._items)} items on {format_uk_date(day_loaded)}. Loading..."
            )
        self.items_changed.emit()
        self.view.setEnabled(True)
        self._hide_loading_overlay()

    def _on_load_failed(self, message: str):
        self._set_info_text(f"Load failed: {message}")
        self._set_busy(False)
        QMessageBox.warning(self, "Load failed", message)

    def _on_loader_warning(self, message: str):
        if not message:
            return
        QMessageBox.warning(self, "Elastic warning", message)

    def _set_info_text(self, text: str):
        if SHOW_TIMELINE_INFO_TEXT:
            self.info.setText(text)

    def shutdown_workers(self):
        """Stop background work. Called by MainWindow.closeEvent — Qt only
        delivers close events to the top-level window, so panel closeEvents
        never fire inside the app."""
        self._loader_slot.shutdown()

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def _stop_loader_thread(self):
        self._loader_slot.retire()

    def is_loading(self) -> bool:
        return self._loader_slot.is_running()

    def _on_load_result(self, payload):
        if payload is None:
            self._set_busy(False)
            return
        items, day_loaded, root_loaded = payload
        self._on_loaded(items, day_loaded, root_loaded)

    def _on_load_progress(self, payload):
        kind = payload[0]
        if kind == "partial":
            _, items, day_loaded, append, root_loaded = payload
            self._on_partial_loaded(items, day_loaded, append, root_loaded)
        elif kind == "warning":
            self._on_loader_warning(payload[1])

    def _fit_to_items(self):
        t0 = perf_counter()
        print("[timeline] _fit_to_items", flush=True)
        if not self._items or not self._current_date:
            return
        day_start = local_day_start_utc(self._current_date)
        first = min(self._items, key=lambda i: i.start)
        last = max(self._items, key=lambda i: i.end)
        start_min = max(0.0, (first.start - day_start).total_seconds() / 60.0)
        end_min = max(start_min + 0.1, (last.end - day_start).total_seconds() / 60.0)
        span_minutes = max(5.0, end_min - start_min)

        view_width = max(300, self.view.viewport().width() or 600)
        ppm = max(1, int(view_width / span_minutes))
        self._ppm = ppm
        self._redraw_timeline()

        # Center the view on the middle of the span
        mid_min = (start_min + end_min) / 2.0
        scene_width = self.scene.sceneRect().width()
        vp_width = self.view.viewport().width() or view_width
        target = mid_min * self._ppm - vp_width / 2.0
        target = max(0.0, min(scene_width - vp_width, target))
        hbar = self.view.horizontalScrollBar()
        hbar.setValue(int(target))
        _timeline_perf_log(f"_fit_to_items redraw+center: {(perf_counter() - t0) * 1000:.0f}ms")

    def _show_loading_overlay(self):
        try:
            rect = self.scene.sceneRect()
            self._loading_rect = self.scene.addRect(rect, QPen(Qt.NoPen), QBrush(QColor(40, 40, 40, 180)))
            self._loading_rect.setZValue(10)
            self._loading_text = self.scene.addText("Loading...")
            self._loading_text.setDefaultTextColor(QColor("#ffddaa"))
            self._loading_text.setZValue(11)
            self._loading_text.setPos(rect.center().x() - 40, rect.center().y() - 10)
        except Exception:
            pass

    def _hide_loading_overlay(self):
        try:
            if self._loading_rect:
                self.scene.removeItem(self._loading_rect)
                self._loading_rect = None
            if self._loading_text:
                self.scene.removeItem(self._loading_text)
                self._loading_text = None
        except Exception:
            pass

    def _emit_selection_from_timeline(self):
        if self._suppress_selection_emit:
            return
        for item in self.scene.selectedItems():
            data = item.data(0)
            if data:
                if isinstance(data, TimelineItem) and data.kind == "video":
                    if self._selected_video_item is not data:
                        self._selected_video_item = data
                        self._apply_video_highlights()
                elif self._selected_video_item is not None:
                    self._selected_video_item = None
                    self._apply_video_highlights()
                # selectionChanged is emitted from inside the scene's mouse
                # dispatch; time_selected handlers may clear/rebuild this very
                # scene (heat-strip redraws, modal dialogs, video loads), which
                # deletes the item Qt is still dispatching on — a native
                # use-after-free crash. Deliver the signal on the next event
                # loop turn instead, coalescing rapid selections.
                self._pending_time_selected = data
                if not self._time_selected_emit_scheduled:
                    self._time_selected_emit_scheduled = True
                    QTimer.singleShot(0, self._flush_pending_time_selected)
                break

    def _flush_pending_time_selected(self):
        self._time_selected_emit_scheduled = False
        data = self._pending_time_selected
        self._pending_time_selected = None
        if data is not None:
            self.time_selected.emit(data)

    def eventFilter(self, obj, event):
        if obj is self.view.viewport():
            if event.type() == QEvent.MouseMove:
                self._update_cursor_indicator(event)
        return super().eventFilter(obj, event)

    def _update_cursor_indicator(self, event):
        if not self._day_start or self._busy:
            return
        self._ensure_cursor_items_valid()
        viewport_pos = self._event_viewport_pos(event)
        if viewport_pos is None:
            return
        scene_pos = self.view.mapToScene(viewport_pos)
        minute = max(0.0, min(24 * 60, scene_pos.x() / self._ppm))
        cursor_time = self._day_start + timedelta(minutes=minute)
        x = minute * self._ppm

        # Draw/update cursor line
        if self._cursor_line is None:
            pen = QPen(QColor("#ff9900"))
            pen.setWidth(1)
            self._cursor_line = self.scene.addLine(x, self._scale_y, x, self._baseline_y + 14, pen)
            self._cursor_line.setZValue(3)
        else:
            self._cursor_line.setLine(x, self._scale_y, x, self._baseline_y + 14)

        # Draw/update cursor label above the ticks
        label_text = format_local_time(cursor_time)
        if self._cursor_label is None:
            self._cursor_label = self.scene.addText(label_text)
            self._cursor_label.setDefaultTextColor(QColor("#ffddaa"))
            self._cursor_label.setZValue(4)
        else:
            self._cursor_label.setPlainText(label_text)
        self._cursor_label.setPos(x + 4, self._scale_y - 26)

        marker_center_y = self._scale_y - 2
        outer_radius = 6
        inner_radius = 3
        outer_rect = QRectF(
            x - outer_radius,
            marker_center_y - outer_radius,
            outer_radius * 2,
            outer_radius * 2,
        )
        inner_rect = QRectF(
            x - inner_radius,
            marker_center_y - inner_radius,
            inner_radius * 2,
            inner_radius * 2,
        )
        if self._cursor_marker_outer is None:
            self._cursor_marker_outer = self.scene.addEllipse(
                outer_rect,
                QPen(QColor("#000000")),
                QBrush(QColor("#000000")),
            )
            self._cursor_marker_outer.setZValue(5)
        else:
            self._cursor_marker_outer.setRect(outer_rect)
        if self._cursor_marker_inner is None:
            self._cursor_marker_inner = self.scene.addEllipse(
                inner_rect,
                QPen(QColor("#ffffff")),
                QBrush(QColor("#ffffff")),
            )
            self._cursor_marker_inner.setZValue(6)
        else:
            self._cursor_marker_inner.setRect(inner_rect)
        self._last_cursor_x = x
        self._reposition_track_labels(cursor_x=x)

    @staticmethod
    def _event_viewport_pos(event) -> QPointF | None:
        if hasattr(event, "position"):
            try:
                pos = event.position()
                if pos is not None:
                    return pos.toPoint()
            except Exception:
                pass
        if hasattr(event, "pos"):
            try:
                return event.pos()
            except Exception:
                pass
        return None

    def set_playhead_datetime(self, dt: Optional[datetime]):
        self._playhead_time = dt
        if self._playhead_line is None and dt is None:
            return
        self._update_playhead_indicator()

    def _ensure_cursor_items_valid(self):
        for attr in (
            "_cursor_line",
            "_cursor_label",
            "_cursor_marker_outer",
            "_cursor_marker_inner",
        ):
            item = getattr(self, attr, None)
            if item is None:
                continue
            try:
                if item.scene() is None:
                    setattr(self, attr, None)
            except RuntimeError:
                setattr(self, attr, None)

    def _update_playhead_indicator(self):
        if self._playhead_line is not None:
            try:
                if self._playhead_line.scene() is None:
                    self._playhead_line = None
            except RuntimeError:
                self._playhead_line = None
        if not self._day_start or not self._current_date or self._playhead_time is None:
            if self._playhead_line is not None:
                try:
                    self.scene.removeItem(self._playhead_line)
                except Exception:
                    pass
                self._playhead_line = None
            return
        play_local = ensure_playhead_local(self._playhead_time)
        if play_local.date() != self._current_date:
            if self._playhead_line is not None:
                try:
                    self.scene.removeItem(self._playhead_line)
                except Exception:
                    pass
                self._playhead_line = None
            return
        play_dt = ensure_utc(play_local)
        minute = max(0.0, min(24 * 60, (play_dt - self._day_start).total_seconds() / 60.0))
        x = minute * self._ppm
        if self._playhead_line is None:
            pen = QPen(QColor("#2ecc71"))
            pen.setWidth(2)
            self._playhead_line = self.scene.addLine(x, self._scale_y, x, self._baseline_y + 14, pen)
            self._playhead_line.setZValue(3)
        else:
            self._playhead_line.setLine(x, self._scale_y, x, self._baseline_y + 14)

    def _reposition_track_labels(self, cursor_x: Optional[float] = None):
        if not self._track_positions:
            return
        if cursor_x is None:
            cursor_x = self._last_cursor_x
        if cursor_x is None:
            cursor_x = 0.0
        h_offset = self.view.horizontalScrollBar().value()
        left_x = h_offset + 4.0
        right_x = h_offset + max(120.0, self.view.viewport().width() - 20.0)
        for kind, y in self._track_positions.items():
            label_item = self._track_labels.get(kind)
            if label_item:
                try:
                    h = label_item.boundingRect().height()
                    # If item was deleted, boundingRect may raise.
                    if label_item.scene() is None:
                        continue
                    label_item.setPos(left_x, y - h / 2)
                except RuntimeError:
                    continue
            count_item = self._track_counts.get(kind)
            if count_item:
                try:
                    rect = count_item.boundingRect()
                    h2 = rect.height()
                    if count_item.scene() is None:
                        continue
                    count_item.setPos(right_x - rect.width(), y - h2 / 2)
                except RuntimeError:
                    continue

    def collect_event_markers(self, video_item: TimelineItem) -> list[tuple[float, str]]:
        markers: list[tuple[float, str]] = []
        if video_item is None or video_item.start is None or video_item.end is None:
            return markers
        for item in self._items:
            if item.kind == "video":
                continue
            if item.start is None or item.end is None:
                continue
            if item.end <= video_item.start or item.start >= video_item.end:
                continue
            ts = max(item.start, video_item.start)
            offset_sec = (ts - video_item.start).total_seconds()
            color_val = item.color
            if isinstance(color_val, QColor):
                color_hex = color_val.name()
            else:
                color_hex = str(color_val or "#ffffff")
            markers.append((offset_sec, color_hex))
        markers.sort(key=lambda t: t[0])
        return markers

    def video_paths(self) -> list[Path]:
        """All video clip paths on the loaded day, in timeline order."""
        items = [
            itm for itm in self._items
            if itm.kind == "video" and isinstance(itm.payload, Path)
        ]
        items.sort(key=lambda itm: itm.start)
        return [itm.payload for itm in items]

    def get_adjacent_video_items(self, current_item: TimelineItem) -> tuple[TimelineItem | None, TimelineItem | None]:
        if current_item is None or current_item.kind != "video":
            return None, None
        if not self._items:
            return None, None
        current_key = current_item.path_key
        if current_key is None and isinstance(current_item.payload, Path):
            current_key = _path_key(current_item.payload)
        video_items = [itm for itm in self._items if itm.kind == "video"]
        if not video_items:
            return None, None
        video_items.sort(key=lambda itm: itm.start)
        idx = None
        if current_key is not None:
            for i, itm in enumerate(video_items):
                if itm.path_key is None and isinstance(itm.payload, Path):
                    itm.path_key = _path_key(itm.payload)
                if itm.path_key == current_key:
                    idx = i
                    break
        if idx is None:
            try:
                idx = video_items.index(current_item)
            except ValueError:
                return None, None
        prev_item = video_items[idx - 1] if idx > 0 else None
        next_item = video_items[idx + 1] if idx + 1 < len(video_items) else None
        return prev_item, next_item

    def mark_video_cached(self, video_path: Path):
        if not self._cache_root or not self._items:
            return
        target_key = _path_key(video_path)
        for item in self._items:
            if item.kind != "video" or not isinstance(item.payload, Path):
                continue
            if item.path_key is None:
                item.path_key = _path_key(item.payload)
            if item.path_key == target_key and not item.cached:
                item.cached = True
                item.color = VIDEO_COLOR_CACHED
                rect = self._video_rects.get(id(item))
                if rect:
                    rect.setBrush(QBrush(self._color_for_video_item(item)))
                return

    def mark_video_annotated(self, video_path: Path, annotated: bool):
        if not self._items:
            return
        changed = False
        for item in self._items:
            if item.kind != "video" or not isinstance(item.payload, Path):
                continue
            if item.payload == video_path:
                if item.annotated != annotated:
                    item.annotated = annotated
                    changed = True
                break
        if changed:
            self._redraw_timeline()

    def _apply_video_highlights(self):
        if not self._video_rects:
            return
        for item in self._items:
            if item.kind != "video":
                continue
            rect = self._video_rects.get(id(item))
            if not rect:
                continue
            rect.setBrush(QBrush(self._color_for_video_item(item)))

    def _add_pen_icon(self, rect: QGraphicsRectItem):
        if rect.rect().width() < 12:
            return
        color = QColor("#ffcc00")
        pen = QPen(color)
        pen.setWidth(2)
        x = rect.rect().x() + rect.rect().width() - 12
        y = rect.rect().y() + 4
        line = QGraphicsLineItem(x, y + 6, x + 6, y, rect)
        line.setPen(pen)
        tip = QPolygonF([
            QPointF(x + 6, y),
            QPointF(x + 10, y + 2),
            QPointF(x + 8, y + 6),
        ])
        tri = QGraphicsPolygonItem(tip, rect)
        tri.setPen(pen)
        tri.setBrush(color)
        line.setZValue(3)
        tri.setZValue(3)

    def _color_for_video_item(self, item: TimelineItem) -> QColor:
        if self._selected_video_item is item:
            return QColor(VIDEO_COLOR_SELECTED)
        color = item.color
        if isinstance(color, QColor):
            return QColor(color)
        if not color:
            color = VIDEO_COLOR_UNCACHED
        return QColor(color)



def _load_timeline_items(job, root: Path, day: date, load_func, extra_loaders, cache_root: Optional[Path]):
    """Worker for the day timeline: video clips first (one partial), then
    the Elastic extra loaders as they complete (more partials), returning
    the merged sorted list. Progress payloads are tagged tuples:
    ("partial", items, day, append, root) and ("warning", message)."""
    t_total_start = perf_counter()
    t_video_start = perf_counter()
    paths = list(load_func(root, day))
    cache_index = _build_cache_index(cache_root) if cache_root else set()
    ann_index = _build_annotation_index(cache_root) if cache_root else set()
    video_entries: list[tuple[Path, datetime]] = []
    stat_fallback_count = 0
    stat_fallback_ms = 0.0
    for p in paths:
        if job.interrupted():
            return None
        parsed_dt = parse_time_from_name(p)
        if parsed_dt is not None:
            start_dt = parsed_dt
        else:
            t_stat = perf_counter()
            try:
                stat = p.stat()
            except FileNotFoundError:
                continue
            stat_fallback_ms += (perf_counter() - t_stat) * 1000.0
            stat_fallback_count += 1
            start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        video_entries.append((p, ensure_utc(start_dt)))

    video_entries.sort(key=lambda tpl: tpl[1])
    items: list[TimelineItem] = []
    for idx, (path_obj, start_dt) in enumerate(video_entries):
        if idx + 1 < len(video_entries):
            next_start = video_entries[idx + 1][1]
            end_dt = next_start
            if (end_dt - start_dt) < MIN_BLOCK_DURATION:
                end_dt = start_dt + MIN_BLOCK_DURATION
        else:
            end_dt = inferred_live_clip_end(path_obj, start_dt)
        cached = _is_path_cached(path_obj, cache_root, cache_index) if cache_root else False
        annotated = _has_annotations(path_obj, cache_root, ann_index) if cache_root else False
        items.append(
            TimelineItem(
                start=start_dt,
                end=end_dt,
                label=path_obj.name,
                kind="video",
                color=VIDEO_COLOR_CACHED if cached else VIDEO_COLOR_UNCACHED,
                payload=path_obj,
                track_label="Video",
                cached=bool(cached),
                annotated=bool(annotated),
                path_key=_path_key(path_obj),
            )
        )
    # The end of the last clip (inferred_live_clip_end) — handed to the
    # extra loaders so fetch_sku_items need not re-list this same day
    # folder on the share (~5s per scan on the WAN share).
    last_video_end = items[-1].end if items else None
    if items:
        job.emit_progress(("partial", items, day, False, root))
    _timeline_perf_log(
        f"video loader: files={len(paths)} items={len(items)} "
        f"time={(perf_counter() - t_video_start) * 1000:.0f}ms"
    )
    _timeline_perf_log(
        f"video loader stat fallback: count={stat_fallback_count} "
        f"time={stat_fallback_ms:.0f}ms"
    )

    # Run extra loaders (e.g., Elastic). Each loader already enforces its own HTTP/request
    # timeout so we don't apply another hard timeout here; that was causing results to be
    # dropped for larger queries that legitimately take longer than a few seconds.
    warnings: list[str] = []
    if extra_loaders:
        with ThreadPoolExecutor(max_workers=max(1, len(extra_loaders))) as executor:
            future_to_name = {}
            future_to_start = {}
            for loader in extra_loaders:
                future = executor.submit(loader, root, day, last_video_end)
                future_to_name[future] = getattr(loader, "__name__", repr(loader))
                future_to_start[future] = perf_counter()
            extra_started = perf_counter()
            for fut in as_completed(future_to_name):
                if job.interrupted():
                    executor.shutdown(cancel_futures=True)
                    return None
                loader_name = future_to_name.get(fut, "extra_loader")
                t_loader = future_to_start.get(fut, perf_counter())
                try:
                    res = fut.result()
                except ElasticFetchError as exc:
                    warnings.append(str(exc))
                    res = exc.items
                except Exception as exc:
                    warnings.append(str(exc))
                    continue
                _timeline_perf_log(
                    f"extra loader {loader_name}: items={len(res) if res else 0} "
                    f"time={(perf_counter() - t_loader) * 1000:.0f}ms"
                )
                if res:
                    for itm in res:
                        items.append(itm)
                    job.emit_progress(("partial", list(res), day, True, root))
            _timeline_perf_log(f"extra loaders total: {(perf_counter() - extra_started) * 1000:.0f}ms")

    t_finalize = perf_counter()
    for itm in items:
        itm.start = ensure_utc(itm.start)
        itm.end = ensure_utc(itm.end)
    items.sort(key=lambda s: s.start)
    _timeline_perf_log(f"finalize+sort: {(perf_counter() - t_finalize) * 1000:.0f}ms")
    if job.interrupted():
        return None
    if warnings:
        for msg in warnings:
            job.emit_progress(("warning", msg))
    _timeline_perf_log(f"load thread total: {(perf_counter() - t_total_start) * 1000:.0f}ms")
    return items, day, root
