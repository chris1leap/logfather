"""The Stop Report: one row per stop event on the loaded day, with a
clip thumbnail that jumps the viewer to that moment.

Extracted from Main_Window (it was ~500 lines of the hub). The build
pipeline needs only the loaded timeline items, the settings/day/root for
the operator-stop fallback query, and the viewer's ClipCache.
"""
from __future__ import annotations

from concurrent.futures import as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from Time_Picker import (
    TimelineItem,
    format_local_time,
    local_day_end_utc,
    local_day_start_utc,
)
from elastic_loader import fetch_logs_for_range
from settings_store import Settings

STOP_THUMB_SIZE = (352, 198)


@dataclass
class StopReportEntry:
    event_time: datetime
    category: str
    label: str
    video_item: TimelineItem | None
    video_path: Path | None
    seek_seconds: float
    thumbnail: QPixmap
    source: str = ""
    state_name: str = ""
    sku_info: str = ""


class StopReportDialog(QDialog):
    open_requested = Signal(object)  # StopReportEntry

    def __init__(self, entries: list[StopReportEntry], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Stop Report")
        self.resize(1050, 700)
        self._entries = list(entries)
        self._row_widgets: list[tuple[QWidget, str]] = []
        self._filter_buttons: dict[str, QPushButton] = {}
        self._media_content_size = QSize(STOP_THUMB_SIZE[0] - 20, STOP_THUMB_SIZE[1] - 20)

        root = QVBoxLayout(self)
        self._intro = QLabel()
        root.addWidget(self._intro)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        filter_label = QLabel("Filters:")
        filter_row.addWidget(filter_label)
        for label, key in (
            ("Caution", "caution"),
            ("E-stop", "estop"),
            ("Operator stop", "operator"),
            ("Manual stop", "manual"),
        ):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.toggled.connect(self._apply_filters)
            filter_row.addWidget(btn)
            self._filter_buttons[key] = btn
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(6)
        content_layout.setContentsMargins(6, 6, 6, 6)

        for entry in entries:
            row = QWidget()
            bg_color, border_color = self._entry_colors(entry)
            row.setStyleSheet(
                f"background-color: {bg_color.name()}; border: 1px solid {border_color.name()}; border-radius: 6px;"
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 4, 4, 4)
            row_layout.setSpacing(10)

            media_frame = self._build_media_frame(entry)
            row_layout.addWidget(media_frame)

            time_only = format_local_time(entry.event_time)
            text_col = QVBoxLayout()
            text_col.setSpacing(4)

            title = QLabel(f"{time_only} | {entry.category}")
            title_font = QFont(self.font())
            title_font.setPointSize(title_font.pointSize() + 3)
            title_font.setBold(True)
            title.setFont(title_font)
            title.setWordWrap(True)
            text_col.addWidget(title)

            detail = QLabel(
                f"{entry.state_name or entry.label}\n"
                f"SKU: {entry.sku_info or '-'}"
            )
            detail.setWordWrap(True)
            text_col.addWidget(detail)
            text_col.addStretch(1)
            row_layout.addLayout(text_col, 1)
            content_layout.addWidget(row)
            self._row_widgets.append((row, self._entry_filter_key(entry)))

        content_layout.addStretch(1)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        root.addWidget(close_btn, 0, Qt.AlignRight)
        self._apply_filters()

    def _request_open(self, entry: StopReportEntry):
        self.open_requested.emit(entry)
        self.accept()

    def _build_media_frame(self, entry: StopReportEntry) -> QWidget:
        holder = QWidget()
        holder.setFixedSize(STOP_THUMB_SIZE[0], STOP_THUMB_SIZE[1])
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        holder.setStyleSheet("background: #000000; border-radius: 8px;")
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(10, 10, 10, 10)
        holder_layout.setSpacing(0)

        thumb_btn = QPushButton()
        thumb_btn.setFixedSize(self._media_content_size)
        thumb_btn.setStyleSheet(
            "QPushButton { border: none; border-radius: 8px; background: transparent; }"
            "QPushButton:disabled { color: #d9d9d9; background-color: #3a3a3a; }"
        )
        if entry.video_path is None:
            thumb_btn.setText(f"{entry.category}\n{format_local_time(entry.event_time)}")
            thumb_btn.setEnabled(False)
        else:
            thumb_btn.setIcon(QIcon(entry.thumbnail))
            thumb_btn.setIconSize(self._media_content_size)
            thumb_btn.clicked.connect(lambda _checked=False, e=entry: self._request_open(e))
        holder_layout.addWidget(thumb_btn, 0, Qt.AlignCenter)
        return holder

    def _entry_colors(self, entry: StopReportEntry) -> tuple[QColor, QColor]:
        key = StopReportDialog._entry_filter_key(entry)
        base = self.palette().color(QPalette.Window)
        if key == "estop":
            accent = QColor("#c85c5c")
        elif key == "caution":
            accent = QColor("#c98732")
        elif key in ("operator", "manual"):
            accent = QColor("#4b7fc7")
        else:
            accent = self.palette().color(QPalette.Mid)
        bg_color = self._blend_colors(base, accent, 0.22)
        border_color = self._blend_colors(base, accent, 0.45)
        return bg_color, border_color

    @staticmethod
    def _blend_colors(base: QColor, accent: QColor, amount: float) -> QColor:
        amount = max(0.0, min(1.0, amount))
        inv = 1.0 - amount
        return QColor(
            int(base.red() * inv + accent.red() * amount),
            int(base.green() * inv + accent.green() * amount),
            int(base.blue() * inv + accent.blue() * amount),
        )

    @staticmethod
    def _entry_filter_key(entry: StopReportEntry) -> str:
        category = (entry.category or "").strip().lower()
        state_name = (entry.state_name or "").strip().lower()
        label = (entry.label or "").strip().lower()
        combined = f"{category} {state_name} {label}"
        if "caution" in combined:
            return "caution"
        if "manual" in combined:
            return "manual"
        if "operator" in combined:
            return "operator"
        if "emergency" in combined or "estop" in combined or "e-stop" in combined:
            return "estop"
        return "other"

    def _apply_filters(self):
        enabled = {key for key, btn in self._filter_buttons.items() if btn.isChecked()}
        visible_count = 0
        for row, key in self._row_widgets:
            is_visible = key not in self._filter_buttons or key in enabled
            row.setVisible(is_visible)
            if is_visible:
                visible_count += 1
        total = len(self._entries)
        if visible_count == total:
            self._intro.setText(f"{total} stop events for selected day")
        else:
            self._intro.setText(f"Showing {visible_count} of {total} stop events for selected day")



def build_stop_report_entries(
    items: list[TimelineItem],
    *,
    settings: Settings,
    day,
    root: Path | None,
    clip_cache,
    parent,
) -> list[StopReportEntry]:
    """Build the day's stop report from the loaded timeline items.

    `clip_cache` is the viewer's ClipCache (used to stage clips and read
    thumbnails from already-cached copies); `parent` hosts the progress
    dialog while clips download."""
    if not items:
        return []
    has_operator_stop_in_timeline = any(_is_operator_stop_item(itm) for itm in items)
    video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
    video_items.sort(key=lambda i: i.start)
    sku_items = [itm for itm in items if itm.kind == "sku" and itm.start is not None and itm.end is not None]
    sku_items.sort(key=lambda i: i.start)
    stop_items: list[tuple[TimelineItem, str, dict]] = []
    required_paths: set[Path] = set()
    for itm in items:
        if itm.kind in ("video", "additional"):
            continue
        category = _categorize_stop_event(itm)
        if category is None:
            continue
        src = {}
        if isinstance(itm.payload, dict):
            src_val = itm.payload.get("_source")
            if isinstance(src_val, dict):
                src = src_val
        stop_items.append((itm, category, src))
        video_item = _find_video_item_for_time(video_items, itm.start)
        if video_item and isinstance(video_item.payload, Path):
            required_paths.add(video_item.payload)
    if required_paths:
        _cache_paths_for_report(parent, clip_cache, sorted(required_paths, key=lambda p: str(p)))

    entries: list[StopReportEntry] = []
    thumb_cache: dict[tuple[str, int], QPixmap] = {}
    seen_keys: set[tuple[int, str]] = set()
    for itm, category, src in stop_items:
        state_name = str(src.get("state_name") or "").strip()
        source = str(src.get("source") or "").strip()
        video_item = _find_video_item_for_time(video_items, itm.start)
        video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
        seek_seconds = 0.0
        if video_item is not None:
            seek_seconds = max(0.0, (itm.start - video_item.start).total_seconds())
        thumb = _thumbnail_for_event(clip_cache, video_path, seek_seconds, itm.start, category, thumb_cache)
        key = (int(itm.start.timestamp()), state_name.lower() or category.lower())
        seen_keys.add(key)
        sku_info = _sku_for_time(sku_items, itm.start)
        entries.append(
            StopReportEntry(
                event_time=itm.start,
                category=category,
                label=itm.label,
                video_item=video_item,
                video_path=video_path,
                seek_seconds=seek_seconds,
                thumbnail=thumb,
                source=source,
                state_name=state_name,
                sku_info=sku_info,
            )
        )

    # Ensure behaviour-node operator_stop entries are included even when not
    # represented by configured timeline conditions.
    if not has_operator_stop_in_timeline:
        for ts, source, state_name, message in _fetch_operator_stop_events(settings, day, root):
            key = (int(ts.timestamp()), state_name.lower() or "operator_stop")
            if key in seen_keys:
                continue
            video_item = _find_video_item_for_time(video_items, ts)
            video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
            seek_seconds = 0.0
            if video_item is not None:
                seek_seconds = max(0.0, (ts - video_item.start).total_seconds())
            thumb = _thumbnail_for_event(clip_cache, video_path, seek_seconds, ts, "Operator Stop", thumb_cache)
            sku_info = _sku_for_time(sku_items, ts)
            entries.append(
                StopReportEntry(
                    event_time=ts,
                    category="Operator Stop",
                    label=message or state_name or "operator_stop",
                    video_item=video_item,
                    video_path=video_path,
                    seek_seconds=seek_seconds,
                    thumbnail=thumb,
                    source=source,
                    state_name=state_name,
                    sku_info=sku_info,
                )
            )
            seen_keys.add(key)

    entries.sort(key=lambda e: e.event_time)
    return entries

def _fetch_operator_stop_events(settings: Settings, day, root: Path | None) -> list[tuple[datetime, str, str, str]]:
    if day is None:
        return []
    start_dt = local_day_start_utc(day)
    end_dt = local_day_end_utc(day)
    try:
        rows = fetch_logs_for_range(
            settings,
            root,
            start_dt,
            end_dt,
            max_hits=30000,
        )
    except Exception:
        return []
    matches: list[tuple[datetime, str, str, str]] = []
    for ts, _text, source, state_name, message in rows:
        s_state = str(state_name or "").strip().lower()
        s_source = str(source or "").strip().lower()
        if s_state != "operator_stop":
            continue
        if "behaviour_node" not in s_source:
            continue
        matches.append((ts, str(source or ""), str(state_name or ""), str(message or "")))
    return matches

def _is_operator_stop_item(item: TimelineItem) -> bool:
    payload = item.payload if isinstance(item.payload, dict) else {}
    src = payload.get("_source") if isinstance(payload.get("_source"), dict) else {}
    state_name = str(src.get("state_name") or "").strip().lower()
    source = str(src.get("source") or "").strip().lower()
    return state_name == "operator_stop" and "behaviour_node" in source

def _cache_paths_for_report(parent, clip_cache, paths: list[Path]):
    if not paths:
        return
    progress = QProgressDialog("Preparing report clips...", "Cancel", 0, 1, parent)
    progress.setWindowTitle("Stop Report")
    progress.setWindowModality(Qt.WindowModal)
    progress.setMinimumDuration(0)
    progress.show()
    QApplication.processEvents()
    futures = []
    executor = clip_cache.executor
    for src_path in paths:
        if progress.wasCanceled():
            break
        try:
            cache_path = clip_cache.cache_path_for(src_path)
        except Exception:
            continue
        if cache_path.exists():
            continue
        futures.append(executor.submit(clip_cache.copy_to_cache, src_path, cache_path))
    progress.setMaximum(max(1, len(futures)))
    completed = 0
    for fut in as_completed(futures):
        completed += 1
        progress.setValue(completed)
        QApplication.processEvents()
        if progress.wasCanceled():
            break
        try:
            _ = fut.result()
        except Exception:
            continue
    progress.close()

def _find_video_item_for_time(video_items: list[TimelineItem], ts: datetime) -> TimelineItem | None:
    for itm in video_items:
        if itm.start <= ts < itm.end:
            return itm
    return None

def _sku_for_time(sku_items: list[TimelineItem], ts: datetime) -> str:
    # Prefer an active SKU interval, then fall back to the most recent
    # known SKU at/just before the stop boundary.
    last_known_sku = ""
    for itm in sku_items:
        if itm.start is None or itm.end is None:
            continue
        payload = itm.payload if isinstance(itm.payload, dict) else {}
        is_manual = bool(payload.get("_ui_manual"))
        label = _format_sku_label(itm)
        if not is_manual and label:
            last_known_sku = label
        # Inclusive end boundary so stop events that close a SKU run at the
        # same timestamp still resolve to that SKU.
        if itm.start <= ts <= itm.end:
            if is_manual:
                return "Manual"
            return label or last_known_sku
        if ts < itm.start:
            break
    if last_known_sku:
        return last_known_sku
    return ""

def _format_sku_label(item: TimelineItem) -> str:
    payload = item.payload if isinstance(item.payload, dict) else {}
    sku = str(payload.get("_ui_sku") or item.label or "").strip()
    tray = str(payload.get("_ui_tray") or "").strip()
    tool = str(payload.get("_ui_tool") or "").strip()
    parts = [p for p in (sku, tray, tool) if p]
    return " | ".join(parts) if parts else sku

def _categorize_stop_event(item: TimelineItem) -> str | None:
    # SKU manual segments are explicit stop periods.
    if item.kind == "sku" and isinstance(item.payload, dict) and item.payload.get("_ui_manual"):
        return "Manual Stop"

    state_name = ""
    message = item.label or ""
    if isinstance(item.payload, dict):
        src = item.payload.get("_source")
        if isinstance(src, dict):
            state_name = str(src.get("state_name") or "").strip().lower()
            message = str(src.get("message") or message).strip()
    track = (item.track_label or "").strip().lower()
    text = f"{track} {state_name} {message}".lower()
    if "go_home_check" in text:
        return None
    if "start_pnp" in text or "automatic_mode" in text or "start" == track:
        return None
    # Include any stop-like condition.
    if "manual_mode" in text or "manual" in text:
        return "Manual Stop"
    if "caution" in text:
        return "Caution Stop"
    if "emergency" in text or "estop" in text or "protective" in text:
        return "E-stop"
    if "manual" in text:
        return "Manual Stop"
    if "stop" in text:
        return "Normal Stop"
    return None

def _thumbnail_for_event(
        clip_cache,
        video_path: Path | None,
        seek_seconds: float,
        event_time: datetime,
        category: str,
        thumb_cache: dict[tuple[str, int], QPixmap],
    ) -> QPixmap:
        if video_path is not None:
            cache_key = (str(video_path), int(round(seek_seconds * 10)))
            if cache_key in thumb_cache:
                return thumb_cache[cache_key]
        source_path = _thumbnail_source_path(clip_cache, video_path)
        if source_path is not None and source_path.exists():
            cap = cv2.VideoCapture(str(source_path))
            try:
                if cap.isOpened():
                    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                    frame_idx = max(0, int(round(seek_seconds * fps)))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w = rgb.shape[:2]
                        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()
                        pix = QPixmap.fromImage(qimg).scaled(
                            STOP_THUMB_SIZE[0],
                            STOP_THUMB_SIZE[1],
                            Qt.KeepAspectRatioByExpanding,
                            Qt.SmoothTransformation,
                        )
                        if video_path is not None:
                            thumb_cache[(str(video_path), int(round(seek_seconds * 10)))] = pix
                        return pix
            finally:
                cap.release()
        # Placeholder when clip/frame unavailable.
        pm = QPixmap(STOP_THUMB_SIZE[0], STOP_THUMB_SIZE[1])
        pm.fill(QColor("#2d2d2d"))
        painter = QPainter(pm)
        painter.setPen(QColor("#dddddd"))
        painter.drawText(pm.rect(), Qt.AlignCenter, f"{category}\n{format_local_time(event_time)}")
        painter.end()
        if video_path is not None:
            thumb_cache[(str(video_path), int(round(seek_seconds * 10)))] = pm
        return pm

def _thumbnail_source_path(clip_cache, video_path: Path | None) -> Path | None:
    if video_path is None:
        return None
    # Avoid network reads in the UI thread: only use already-cached local files.
    try:
        cached = clip_cache.get_valid_cached_path(video_path)
        if cached and isinstance(cached, Path):
            return cached
        cached = clip_cache.cache_path_for(video_path)
        if cached and isinstance(cached, Path) and cached.exists():
            return cached
    except Exception:
        return None
    return None

