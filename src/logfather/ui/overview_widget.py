from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

import cv2

from PySide6.QtCore import QDate, QEvent, QThread, Qt, Signal, QTimer, QRectF, QVariantAnimation, QEasingCurve, QUrl
from PySide6.QtGui import QColor, QBrush, QPen, QFont, QFontMetrics, QImage, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QButtonGroup,
    QCalendarWidget,
    QDialog,
    QDialogButtonBox,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsRectItem,
    QStackedWidget,
)

from logfather.ui.Time_Picker import (
    TimelineItem,
    parse_time_from_name,
    ensure_utc,
    MIN_BLOCK_DURATION,
    LAST_BLOCK_DURATION,
    inferred_live_clip_end,
    _cache_key_for,
)
from logfather.data.elastic_loader import fetch_overview_event_chunks
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.ui.app_assets import resolve_asset_path as _resolve_asset_path
from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.overview_event_cache import (
    load_overview_events,
    save_overview_events,
)
from logfather.data.ui_state_store import (
    customer_collapsed_map,
    set_customer_collapsed,
)
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot
from logfather.data.settings_store import (
    Settings,
    display_customer_name,
    display_line_name,
    customer_starts_collapsed,
    system_group_sort_key,
)

OVERVIEW_REFRESH_MS = 60_000
OVERVIEW_REDRAW_MS = 1_000
OVERVIEW_FULL_RESYNC_MINUTES = 30
OVERVIEW_INCREMENTAL_OVERLAP = timedelta(minutes=2)
# Disk saves of the merged events are throttled to this interval; at most
# this much tail is refetched after an app restart.
OVERVIEW_CACHE_SAVE_MIN_SECONDS = 60.0
OVERVIEW_RANGE_ANIM_MS = 220
OVERVIEW_LOADING_VIDEO = "Logfather animated splash screen Argus II.mp4"


@dataclass(slots=True)
class OverviewSystemState:
    name: str
    root: Path
    robot_id: str | None
    events: list[dict] = field(default_factory=list)
    video_items: list[TimelineItem] = field(default_factory=list)
    last_event_time: datetime | None = None
    thumbnail_image: QImage | None = None
    last_stop_time: datetime | None = None
    last_thumbnail_queue_time: datetime | None = None
    # Bumped on every data merge; keys the per-system summary cache so the
    # event state machine reruns only when the data actually changed.
    events_version: int = 0


_THUMBNAIL_CACHE: OrderedDict[str, tuple[int, QImage | None]] = OrderedDict()
_THUMBNAIL_CACHE_MAX = 200
OVERVIEW_THUMBNAIL_REFRESH_MINUTES = 5
OVERVIEW_ROW_HEIGHT = 56
OVERVIEW_THUMB_SIZE = (78, 44)
OVERVIEW_THUMBNAIL_MAX_AGE = timedelta(minutes=30)


def _extract_robot_id(root: Path) -> str | None:
    return robot_id_from_folder(root.name)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _start_of_day_local(now_local: datetime) -> datetime:
    return now_local.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_day(day_value: date) -> datetime:
    """Local-aware midnight for an arbitrary calendar day (DST-correct:
    the naive combine is interpreted as local system time)."""
    return datetime.combine(day_value, dt_time.min).astimezone()


def _timeline_day_date(now_local: datetime) -> date:
    return now_local.date()


def _build_video_items(pikpak_root: Path, day_value: date) -> list[TimelineItem]:
    try:
        paths = list(load_day_files_cached(pikpak_root, day_value))
    except Exception:
        return []
    entries: list[tuple[Path, datetime]] = []
    for path_obj in paths:
        parsed_dt = parse_time_from_name(path_obj)
        if parsed_dt is not None:
            start_dt = parsed_dt
        else:
            try:
                stat = path_obj.stat()
            except FileNotFoundError:
                continue
            start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        entries.append((path_obj, ensure_utc(start_dt)))
    entries.sort(key=lambda tpl: tpl[1])
    items: list[TimelineItem] = []
    for idx, (path_obj, start_dt) in enumerate(entries):
        if idx + 1 < len(entries):
            next_start = entries[idx + 1][1]
            end_dt = next_start
            if (end_dt - start_dt) < MIN_BLOCK_DURATION:
                end_dt = start_dt + MIN_BLOCK_DURATION
        else:
            end_dt = inferred_live_clip_end(path_obj, start_dt)
        items.append(
            TimelineItem(
                start=start_dt,
                end=end_dt,
                label=path_obj.name,
                kind="video",
                color="#5e9bff",
                payload=path_obj,
                track_label="Video",
            )
        )
    return items


def _event_key(event: dict) -> tuple:
    ts = event.get("ts")
    selection = event.get("selection") if isinstance(event.get("selection"), dict) else {}
    return (
        ensure_utc(ts).isoformat() if isinstance(ts, datetime) else "",
        str(event.get("state_name") or ""),
        str(event.get("message") or ""),
        str(event.get("source") or ""),
        str(selection.get("sku") or ""),
        str(selection.get("tray") or ""),
        str(selection.get("tool") or ""),
    )


def _clip_window(start_dt: datetime, end_dt: datetime, window_start: datetime, window_end: datetime) -> tuple[datetime, datetime] | None:
    start_dt = ensure_utc(start_dt)
    end_dt = ensure_utc(end_dt)
    window_start = ensure_utc(window_start)
    window_end = ensure_utc(window_end)
    clipped_start = max(start_dt, window_start)
    clipped_end = min(end_dt, window_end)
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end


def _is_shutdown_message(message: str) -> bool:
    msg = (message or "").strip().lower()
    if not msg:
        return False
    return "shutting down system" in msg


def _is_stop_like_event(state_name: str, message: str, service_name: str = "") -> bool:
    lower_state = (state_name or "").strip().lower()
    if (
        "stop" in lower_state
        or "estop" in lower_state
        or "caution" in lower_state
        or lower_state in {
            "hardware_emergency_stop",
            "protective_stop",
            "emergency_stop",
            "system_stop",
            "stop_pnp",
            "caution_led_on",
        }
    ):
        return True
    if (service_name or "").strip().lower() == "system_shutdown":
        return True
    return _is_shutdown_message(message)


class _OverviewThumbItem(QGraphicsRectItem):
    def __init__(self, rect: QRectF, state: OverviewSystemState, image: QImage | None, widget: "OverviewWidget"):
        super().__init__(rect)
        self._state = state
        self._image = image
        self._widget = widget
        self._pixmap = None
        self.setAcceptHoverEvents(True)
        self.setPen(QPen(QColor("#31414d")))
        self.setBrush(QBrush(QColor("#0f1419")))
        if image is not None and not image.isNull():
            self._pixmap = QPixmap.fromImage(image).scaled(
                int(rect.width()),
                int(rect.height()),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self.setToolTip(f"{state.name}\nNewest CCTV clip")
        else:
            self.setToolTip(f"{state.name}\nNo cached preview yet")

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self._pixmap is not None:
            painter.save()
            painter.setClipRect(self.rect())
            painter.drawPixmap(self.rect().topLeft(), self._pixmap)
            painter.restore()
            return
        painter.save()
        painter.setPen(QColor("#7f95a6"))
        painter.drawText(self.rect(), Qt.AlignCenter, "No\nclip")
        painter.restore()

    def hoverEnterEvent(self, event):
        self._widget.show_thumbnail_preview(self._state, self.sceneBoundingRect())
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._widget.hide_thumbnail_preview()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._widget.open_requested.emit(self._state.root, self._widget.current_day(), None)
            event.accept()
            return
        super().mousePressEvent(event)


class _DayRangeDialog(QDialog):
    """From/To day pickers as plain calendars (Chris, 2026-09-05: one
    button opens this; simplest possible range selection)."""

    def __init__(self, initial: tuple[date, date], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Show data for days")
        today = QDate.currentDate()
        layout = QVBoxLayout(self)
        cal_row = QHBoxLayout()
        cal_row.setSpacing(14)
        self._from_cal = QCalendarWidget()
        self._to_cal = QCalendarWidget()
        for cal, label_text, day_value in (
            (self._from_cal, "From", initial[0]),
            (self._to_cal, "To", initial[1]),
        ):
            cal.setGridVisible(True)
            cal.setMinimumDate(today.addDays(-60))
            cal.setMaximumDate(today)
            cal.setSelectedDate(QDate(day_value.year, day_value.month, day_value.day))
            column = QVBoxLayout()
            title = QLabel(label_text)
            title.setStyleSheet("font-weight: bold;")
            column.addWidget(title)
            column.addWidget(cal)
            cal_row.addLayout(column)
        # A From after the current To drags To along with it.
        self._from_cal.clicked.connect(self._on_from_picked)
        self._from_cal.selectionChanged.connect(self._on_from_changed)
        layout.addLayout(cal_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_from_picked(self, qdate: QDate):
        if self._to_cal.selectedDate() < qdate:
            self._to_cal.setSelectedDate(qdate)

    def _on_from_changed(self):
        self._on_from_picked(self._from_cal.selectedDate())

    def selected_range(self) -> tuple[date, date]:
        d1 = self._from_cal.selectedDate().toPython()
        d2 = self._to_cal.selectedDate().toPython()
        return (min(d1, d2), max(d1, d2))


class _OverviewCustomerHeaderItem(QGraphicsRectItem):
    def __init__(self, rect: QRectF, customer_name: str, widget: "OverviewWidget"):
        super().__init__(rect)
        self._customer_name = customer_name
        self._widget = widget
        self._arrow_item = None
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_arrow_item(self, item) -> None:
        """The ▲/▼ collapse arrow highlighted while this header is
        hovered (Chris, 2026-09-05). Same scene lifecycle as the header,
        so the reference can never outlive the item."""
        self._arrow_item = item

    def hoverEnterEvent(self, event):
        if self._arrow_item is not None:
            self._arrow_item.setDefaultTextColor(QColor(theme.ACCENT))
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self._arrow_item is not None:
            self._arrow_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._widget.toggle_customer_collapsed(self._customer_name)
            event.accept()
            return
        super().mousePressEvent(event)


class _OverviewTimelineClickItem(QGraphicsRectItem):
    def __init__(
        self,
        rect: QRectF,
        state: OverviewSystemState,
        day_value: date,
        window_start: datetime,
        window_end: datetime,
        widget: "OverviewWidget",
    ):
        super().__init__(rect)
        self._state = state
        self._day_value = day_value
        self._window_start = ensure_utc(window_start)
        self._window_end = ensure_utc(window_end)
        self._widget = widget
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setAcceptHoverEvents(False)
        self.setPen(QPen(Qt.NoPen))
        self.setBrush(QBrush(Qt.transparent))
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
        rect = self.rect()
        if rect.width() <= 0:
            self._widget.open_requested.emit(self._state.root, self._day_value, None)
            event.accept()
            return
        click_x = min(max(event.pos().x(), rect.left()), rect.right())
        ratio = (click_x - rect.left()) / rect.width()
        total_seconds = max(1.0, (self._window_end - self._window_start).total_seconds())
        clicked_ts = self._window_start + timedelta(seconds=ratio * total_seconds)
        # The clicked moment names the day: in a multi-day range the
        # window spans several days and _day_value is only the last one.
        clicked_day = clicked_ts.astimezone().date()
        self._widget.open_requested.emit(self._state.root, clicked_day, ensure_utc(clicked_ts))
        event.accept()


def _latest_video_thumbnail(
    video_items: list[TimelineItem],
    cache_root: Path | None,
    now_dt: datetime,
) -> QImage | None:
    latest_path = None
    latest_start = None
    for item in video_items:
        if item.kind != "video" or not isinstance(item.payload, Path):
            continue
        if latest_start is None or item.start > latest_start:
            latest_start = item.start
            latest_path = item.payload
    if latest_path is None or latest_start is None or cache_root is None:
        return None
    if ensure_utc(now_dt) - ensure_utc(latest_start) > OVERVIEW_THUMBNAIL_MAX_AGE:
        return None
    try:
        source_path = _cache_key_for(latest_path, cache_root)
    except Exception:
        return None
    if not source_path.exists():
        return None
    try:
        stat = source_path.stat()
        cache_key = str(source_path)
        cache_token = int(stat.st_mtime_ns)
    except Exception:
        return None
    cached = _THUMBNAIL_CACHE.get(cache_key)
    if cached and cached[0] == cache_token:
        return cached[1]
    image: QImage | None = None
    cap = cv2.VideoCapture(str(source_path))
    try:
        if cap.isOpened():
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            target_frame = max(0, min(frame_count - 1, 3)) if frame_count > 0 else 0
            cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
            ok, frame = cap.read()
            if ok and frame is not None:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = rgb.shape[:2]
                image = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888).copy()
    except Exception:
        image = None
    finally:
        cap.release()
    _THUMBNAIL_CACHE[cache_key] = (cache_token, image)
    while len(_THUMBNAIL_CACHE) > _THUMBNAIL_CACHE_MAX:
        _THUMBNAIL_CACHE.popitem(last=False)
    return image


def _fmt_eta(seconds: float) -> str:
    secs = max(1, int(round(seconds)))
    if secs < 90:
        return f"~{secs}s left"
    minutes, rem = divmod(secs, 60)
    return f"~{minutes}m {rem:02d}s left"


def _run_overview_load(job, settings: Settings, parent_dir: Path, cache_root: Path | None, full_refresh: bool, since_dt: datetime | None, use_disk_cache: bool = False, day_range: tuple[date, date] | None = None):
    now_local = _local_now()
    if day_range is not None:
        # Historic day/range mode (Chris, 2026-09-05): fixed span, no
        # incremental fetch, replaces whatever was loaded before.
        range_start_day, range_end_day = day_range
        day_value = range_end_day
        day_start_local = _start_of_day(range_start_day)
        fetch_end_local = min(now_local, _start_of_day(range_end_day) + timedelta(days=1))
        day_list = [
            range_start_day + timedelta(days=offset)
            for offset in range((range_end_day - range_start_day).days + 1)
        ]
    else:
        day_value = _timeline_day_date(now_local)
        day_start_local = _start_of_day_local(now_local)
        fetch_end_local = now_local
        day_list = [day_value]
    # Progress narration: numbered stages, what is being worked on, and a
    # running time estimate from the completed items — the bare counter
    # read as a hang while each system's day folder was listed on the
    # WAN share (Chris, 2026-09-05).
    job.emit_progress("Step 1/3 — listing systems on the CCTV share...")
    system_roots = []
    if parent_dir.exists():
        system_roots = [p for p in parent_dir.iterdir() if p.is_dir()]
    system_roots.sort(key=lambda p: p.name.lower())
    total_systems = len(system_roots)
    systems = []
    t_scan_start = time.perf_counter()
    for idx, root in enumerate(system_roots, start=1):
        if job.interrupted():
            return None
        eta = ""
        if idx > 1:
            avg = (time.perf_counter() - t_scan_start) / (idx - 1)
            eta = f" — {_fmt_eta(avg * (total_systems - idx + 1))}"
        day_note = f" × {len(day_list)} days" if len(day_list) > 1 else ""
        job.emit_progress(
            f"Step 1/3 — scanning {root.name} ({idx}/{total_systems}{day_note}){eta}"
        )
        robot_id = _extract_robot_id(root)
        video_items = []
        for day_entry in day_list:
            video_items.extend(_build_video_items(root, day_entry))
        systems.append(
            {
                "name": root.name,
                "root": root,
                "robot_id": robot_id,
                "events": [],
                "video_items": video_items,
                "thumbnail_image": _latest_video_thumbnail(video_items, cache_root, now_local),
            }
        )
    final_systems = []
    for row in systems:
        if job.interrupted():
            return None
        updated_row = dict(row)
        updated_row["events"] = []
        final_systems.append(updated_row)
    row_by_robot: dict[str, dict] = {}
    for row in final_systems:
        robot_id = row.get("robot_id")
        if isinstance(robot_id, str) and robot_id:
            row_by_robot[robot_id] = row

    # Fresh session: seed from the on-disk cache of today's events and
    # only fetch the tail since the newest cached event (Chris,
    # 2026-09-04). No cache -> the fetch below covers the whole day and
    # the payload is flagged full_refresh so the resync clock is stamped.
    if use_disk_cache and not full_refresh and since_dt is None:
        cached = load_overview_events(day_value)
        if cached is not None:
            cached_events, cache_latest_ts = cached
            for robot_id, events in cached_events.items():
                row = row_by_robot.get(robot_id)
                if row is not None:
                    row["events"] = list(events)
            if cache_latest_ts is not None:
                since_dt = cache_latest_ts - OVERVIEW_INCREMENTAL_OVERLAP
    covers_full_day = full_refresh or since_dt is None
    fetch_start = day_start_local if covers_full_day else max(day_start_local, since_dt)
    chunk_minutes = 10
    if day_range is not None:
        # Multi-day spans keep the chunk count reasonable (~48 max) so a
        # week-long fetch is not 1000 requests.
        span_minutes = max(1, int((fetch_end_local - fetch_start).total_seconds() // 60))
        chunk_minutes = max(10, ((span_minutes // 48) // 10 + 1) * 10)
    total_chunks = max(1, int(((fetch_end_local - fetch_start).total_seconds() + (chunk_minutes * 60) - 1) // (chunk_minutes * 60)))
    job.emit_progress(
        f"Step 2/3 — loading events from Elastic (0/{total_chunks} chunks)"
    )
    t_chunks_start = time.perf_counter()
    for chunk_idx, chunk in enumerate(fetch_overview_event_chunks(
        settings,
        system_roots,
        fetch_start,
        fetch_end_local,
        chunk_minutes=chunk_minutes,
    ), start=1):
        if job.interrupted():
            return None
        for robot_id, events in chunk.items():
            row = row_by_robot.get(robot_id)
            if row is None:
                continue
            row["events"].extend(list(events or []))
        eta = ""
        if chunk_idx < total_chunks:
            avg = (time.perf_counter() - t_chunks_start) / chunk_idx
            eta = f" — {_fmt_eta(avg * (total_chunks - chunk_idx))}"
        job.emit_progress(
            f"Step 2/3 — loading events from Elastic ({chunk_idx}/{total_chunks} chunks){eta}"
        )
    job.emit_progress("Step 3/3 — rendering overview...")
    return {
        "systems": final_systems,
        "now_local": now_local,
        "day_value": day_value,
        # A full-day fetch counts as a full refresh even when it started
        # as a cache-seeded load that found no cache file.
        "full_refresh": covers_full_day,
        # Historic loads replace whatever mode was loaded before.
        "replace": day_range is not None,
        # Events older than this are trimmed at merge (range start, or
        # today's midnight in live mode).
        "cutoff_utc": day_start_local.astimezone(timezone.utc),
        "final": True,
    }


class OverviewWidget(QWidget):
    open_requested = Signal(object, object, object)
    # Same shape as the viewer's: (key, label, done, total) feeding the
    # main window's bottom activity bar (Chris, 2026-09-05).
    activity_progress = Signal(str, str, object, object)
    activity_cleared = Signal(str)

    def __init__(
        self,
        settings: Settings,
        cache_root: Path | None = None,
        prefetch_clips: Callable[[list[Path]], None] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.cache_root = cache_root
        self._prefetch_clips = prefetch_clips
        self.parent_dir: Path | None = None
        self._states: dict[str, OverviewSystemState] = {}
        self._display_mode = "1h"
        self._last_day: date | None = None
        self._last_refreshed_local: datetime | None = None
        self._latest_event_ts: datetime | None = None
        self._last_full_refresh_local: datetime | None = None
        self._active = False
        self._background_enabled = False
        # Day-range filter (Chris, 2026-09-05): None = live today; a
        # (start_day, end_day) pair shows that span's data, immutable —
        # loaded once, no incremental refresh, no now/updated markers.
        self._filter_day_range: tuple[date, date] | None = None
        self._historic_loaded_range: tuple[date, date] | None = None
        self._overview_slot = JobSlot(self)
        self._collapsed_customers: set[str] = set()
        self._range_anim_now_local: datetime | None = None
        self._range_anim_span_seconds: float | None = None

        self.status_label = QLabel("Overview inactive")
        self.status_label.setStyleSheet(theme.OVERVIEW_STATUS)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda: self.refresh(force_full=True))

        self.range_group = QButtonGroup(self)
        self.one_hour_btn = QPushButton("1h")
        self.five_hour_btn = QPushButton("5h")
        self.all_day_btn = QPushButton("All Day")
        for btn, value in (
            (self.one_hour_btn, "1h"),
            (self.five_hour_btn, "5h"),
            (self.all_day_btn, "all"),
        ):
            btn.setCheckable(True)
            self.range_group.addButton(btn)
            btn.clicked.connect(lambda _checked=False, mode=value: self._set_display_mode(mode))
        self.one_hour_btn.setChecked(True)

        # Day filter: live today, or one button opening a calendar
        # dialog for a day / span of days (Chris, 2026-09-05).
        self.live_btn = QPushButton("Live")
        self.live_btn.setCheckable(True)
        self.live_btn.setChecked(True)
        self.live_btn.setToolTip("Follow today's data live")
        self.live_btn.clicked.connect(self._on_live_clicked)
        self.pick_days_btn = QPushButton("Choose days…")
        self.pick_days_btn.setToolTip(
            "Show all data for a chosen day or span of days"
        )
        self.pick_days_btn.clicked.connect(self._on_pick_days_clicked)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.addWidget(QLabel("Zoom"))
        controls.addWidget(self.one_hour_btn)
        controls.addWidget(self.five_hour_btn)
        controls.addWidget(self.all_day_btn)
        controls.addSpacing(18)
        controls.addWidget(self.live_btn)
        controls.addWidget(self.pick_days_btn)
        controls.addStretch(1)
        controls.addWidget(self.status_label)
        controls.addWidget(self.refresh_btn)

        self.scene = QGraphicsScene(self)
        self.view = QGraphicsView(self.scene, self)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.view.setFrameShape(QGraphicsView.NoFrame)
        self.view.setStyleSheet(theme.PANEL_SURFACE)
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self._preview_label = QLabel(self.view.viewport())
        self._preview_label.setStyleSheet(theme.HOVER_PREVIEW)
        self._preview_label.hide()
        self._hover_scene_x: float | None = None
        self._hover_window_start: datetime | None = None
        self._hover_window_end: datetime | None = None
        self._hover_timeline_x: float = 0.0
        self._hover_timeline_width: float = 0.0
        self._hover_grid_top: float = 0.0
        self._hover_grid_bottom: float = 0.0
        # Persistent hover crosshair items; recreated lazily after scene
        # rebuilds so mouse-moves never trigger a full redraw.
        self._hover_line_item = None
        self._hover_label_item = None
        # Sticky header: (item, base_y) pairs shifted to the viewport top
        # on every scroll so the column titles and time labels stay
        # visible while the system list scrolls (Chris, 2026-09-04).
        self._sticky_header_items: list = []
        # Blue last-update marker: full-height line + sticky time label
        # (Chris, 2026-09-04). Persistent like the now-label.
        self._last_update_line_item = None
        self._last_update_label_item = None
        self.view.verticalScrollBar().valueChanged.connect(self._reposition_sticky_header)
        self._content_stack = QStackedWidget(self)
        self._loading_page = QWidget(self)
        self._loading_page.setStyleSheet(theme.PANEL_SURFACE)
        loading_layout = QVBoxLayout(self._loading_page)
        loading_layout.setContentsMargins(0, 0, 0, 0)
        loading_layout.setSpacing(0)
        self._loading_video = QVideoWidget(self._loading_page)
        self._loading_video.setStyleSheet(theme.PANEL_BG)
        loading_layout.addWidget(self._loading_video, 1)
        self._loading_label = QLabel("Loading overview...", self._loading_page)
        self._loading_label.setAlignment(Qt.AlignCenter)
        self._loading_label.setStyleSheet(theme.LOADING_BADGE)
        self._loading_audio = QAudioOutput(self)
        self._loading_audio.setVolume(0.0)
        self._loading_player = QMediaPlayer(self)
        self._loading_player.setAudioOutput(self._loading_audio)
        self._loading_player.setVideoOutput(self._loading_video)
        self._loading_player.mediaStatusChanged.connect(self._on_loading_media_status_changed)
        loading_video_path = _resolve_asset_path(OVERVIEW_LOADING_VIDEO)
        if loading_video_path:
            self._loading_player.setSource(QUrl.fromLocalFile(loading_video_path))

        self._content_stack.addWidget(self.view)
        self._content_stack.addWidget(self._loading_page)
        self._content_stack.setCurrentWidget(self.view)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addLayout(controls)
        layout.addWidget(self._content_stack, 1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(OVERVIEW_REFRESH_MS)
        self._refresh_timer.timeout.connect(self._on_refresh_timer)
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(OVERVIEW_REDRAW_MS)
        self._redraw_timer.timeout.connect(self._on_redraw_timer)
        # All rebuild requests funnel through this zero-length single-shot:
        # coalesces bursts (range animation + timer) and, crucially, keeps
        # scene.clear() out of scene event dispatch — clearing the scene
        # under a QGraphicsItem's mouse handler is a use-after-free (the
        # same shape that crashed the timeline).
        self._redraw_soon = QTimer(self)
        self._redraw_soon.setSingleShot(True)
        self._redraw_soon.setInterval(0)
        self._redraw_soon.timeout.connect(self._redraw)
        self._summary_cache: dict[str, tuple[int, datetime, dict]] = {}
        self._last_cache_save_mono = 0.0
        self._now_label_item = None
        self._last_drawn_window: tuple[datetime, datetime] | None = None
        self._range_anim = QVariantAnimation(self)
        self._range_anim.setDuration(OVERVIEW_RANGE_ANIM_MS)
        self._range_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._range_anim.valueChanged.connect(self._on_range_anim_value_changed)
        self._range_anim.finished.connect(self._on_range_anim_finished)

    def current_day(self) -> date:
        return self._last_day or _local_now().date()

    def set_parent_dir(self, parent_dir: Path | None):
        if self.parent_dir == parent_dir:
            return
        self.parent_dir = parent_dir
        self._sync_collapsed_customers(reset=True)
        if self._active or self._background_enabled:
            self.refresh(force_full=True)

    def set_system_layout_settings(self, settings: Settings):
        self.settings = settings
        self._sync_collapsed_customers(reset=True)
        self.refresh_layout()

    def _sync_collapsed_customers(self, customers: Iterable[str] | None = None, reset: bool = False):
        if customers is None:
            known_customers = {display_customer_name(self.settings, state.name) for state in self._states.values()}
        else:
            known_customers = {str(name or "").strip() for name in customers if str(name or "").strip()}
        # Same rule as the date picker: the user's persisted collapse
        # choices win over the configured start-collapsed default.
        stored = customer_collapsed_map()
        self._collapsed_customers = {
            name
            for name in known_customers
            if stored.get(name, customer_starts_collapsed(self.settings, name))
        }

    def activate(self, active: bool):
        active = bool(active)
        if active == self._active:
            return
        self._active = active
        if active:
            self._refresh_timer.start()
            self._redraw_timer.start()
            # Plain refresh: the first of a session seeds from the disk
            # cache, later activations fetch incrementally from memory.
            # force_full here made every mode switch refetch the whole
            # day for the fleet; the periodic resync covers drift.
            self.refresh()
        else:
            self._redraw_timer.stop()
            self.hide_thumbnail_preview()
            self._stop_loading_video()
            self._maybe_persist_events_cache(_local_now(), force=True)

    def _reset_loaded_data(self):
        self._states.clear()
        self._summary_cache.clear()
        self._latest_event_ts = None
        self._last_refreshed_local = None
        self._last_full_refresh_local = None
        self._historic_loaded_range = None

    def _on_pick_days_clicked(self):
        today = _local_now().date()
        initial = self._filter_day_range or (today, today)
        dialog = _DayRangeDialog(initial, self)
        if dialog.exec() != QDialog.Accepted:
            return
        start_day, end_day = dialog.selected_range()
        end_day = min(end_day, today)
        start_day = min(start_day, end_day)
        if self._filter_day_range == (start_day, end_day):
            return
        self._filter_day_range = (start_day, end_day)
        if start_day == end_day:
            self.pick_days_btn.setText(start_day.strftime("%d/%m/%Y"))
        else:
            self.pick_days_btn.setText(
                f"{start_day:%d/%m} – {end_day:%d/%m/%Y}"
            )
        self.live_btn.setChecked(False)
        self._reset_loaded_data()
        self.refresh(force_full=True)

    def _on_live_clicked(self):
        self.live_btn.setChecked(True)
        if self._filter_day_range is None:
            return
        self._filter_day_range = None
        self.pick_days_btn.setText("Choose days…")
        self._reset_loaded_data()
        self.refresh(force_full=True)

    def refresh(self, force_full: bool = False):
        if self.parent_dir is None:
            if self._active:
                self._schedule_redraw()
            return
        if not self._active and not self._background_enabled:
            return
        day_range = self._filter_day_range
        if day_range is not None:
            # Historic range: immutable data, loaded once per selection;
            # the manual Refresh button (force_full) re-fetches it.
            if self._overview_slot.is_running():
                return
            if not force_full and self._historic_loaded_range == day_range:
                return
            label = (
                day_range[0].strftime("%d/%m/%Y")
                if day_range[0] == day_range[1]
                else f"{day_range[0]:%d/%m/%Y} – {day_range[1]:%d/%m/%Y}"
            )
            self.status_label.setText(f"Loading {label}...")
            if not self._states:
                self._set_loading_visible(True, f"Loading {label}...")
            settings = self.settings
            parent_dir = self.parent_dir
            cache_root = self.cache_root
            self._historic_loaded_range = day_range
            self._overview_slot.start(
                lambda job: _run_overview_load(
                    job, settings, parent_dir, cache_root, True, None, False, day_range
                ),
                on_result=self._on_loaded,
                on_error=self._on_failed,
                on_progress=self._on_load_progress,
            )
            return
        now_local = _local_now()
        if self._last_day and self._last_day != now_local.date():
            force_full = True
            self._states.clear()
            self._summary_cache.clear()
            self._latest_event_ts = None
        # First load of a session seeds from the on-disk event cache
        # instead of forcing a full-day fetch; the worker falls back to
        # the full day when no cache file exists.
        first_load = self._last_refreshed_local is None
        use_disk_cache = first_load and not force_full
        full_refresh = force_full
        if not full_refresh and not first_load and self._last_full_refresh_local is not None:
            full_refresh = (now_local - self._last_full_refresh_local) >= timedelta(minutes=OVERVIEW_FULL_RESYNC_MINUTES)
        since_dt = None
        if not full_refresh and self._latest_event_ts is not None:
            since_dt = self._latest_event_ts - OVERVIEW_INCREMENTAL_OVERLAP
        if self._overview_slot.is_running():
            return
        if not self._states:
            self.status_label.setText("Loading overview...")
            self._set_loading_visible(True, "Loading overview...")
        else:
            self.status_label.setText("Refreshing overview...")
        settings = self.settings
        parent_dir = self.parent_dir
        cache_root = self.cache_root
        self._overview_slot.start(
            lambda job: _run_overview_load(job, settings, parent_dir, cache_root, full_refresh, since_dt, use_disk_cache),
            on_result=self._on_loaded,
            on_error=self._on_failed,
            on_progress=self._on_load_progress,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_loading_label"):
            self._loading_label.adjustSize()
            label_size = self._loading_label.sizeHint()
            page_rect = self._loading_page.rect()
            x = max(12, (page_rect.width() - label_size.width()) // 2)
            y = max(12, page_rect.height() - label_size.height() - 18)
            self._loading_label.setGeometry(x, y, label_size.width(), label_size.height())
        self._schedule_redraw()

    def shutdown_workers(self):
        """Stop timers and background work. Called by MainWindow.closeEvent —
        panel closeEvents never fire inside the app."""
        self._refresh_timer.stop()
        self._redraw_timer.stop()
        self._redraw_soon.stop()
        self._stop_load_thread()
        self._stop_loading_video()

    def closeEvent(self, event):
        self.shutdown_workers()
        super().closeEvent(event)

    def eventFilter(self, obj, event):
        if obj is self.view.viewport():
            if event.type() == QEvent.MouseMove:
                scene_pos = self.view.mapToScene(event.pos())
                x = float(scene_pos.x())
                if self._hover_timeline_width > 0 and self._hover_timeline_x <= x <= (self._hover_timeline_x + self._hover_timeline_width):
                    self._hover_scene_x = x
                else:
                    self._hover_scene_x = None
                self._update_hover_indicator()
            elif event.type() == QEvent.Leave:
                if self._hover_scene_x is not None:
                    self._hover_scene_x = None
                    self._update_hover_indicator()
        return super().eventFilter(obj, event)

    def _update_hover_indicator(self):
        """Move (or lazily create) the hover crosshair without rebuilding the
        scene. This used to trigger a full scene.clear()+rebuild — including
        re-running every system's event summary — on every mouse move."""
        for attr in ("_hover_line_item", "_hover_label_item"):
            item = getattr(self, attr)
            if item is not None:
                try:
                    if item.scene() is None:
                        setattr(self, attr, None)
                except RuntimeError:
                    setattr(self, attr, None)
        x = self._hover_scene_x
        valid = (
            x is not None
            and self._hover_window_start is not None
            and self._hover_window_end is not None
            and self._hover_timeline_width > 0
        )
        if not valid:
            for item in (self._hover_line_item, self._hover_label_item):
                if item is not None:
                    item.setVisible(False)
            return
        timeline_x = self._hover_timeline_x
        timeline_width = self._hover_timeline_width
        hover_x = min(max(x, timeline_x), timeline_x + timeline_width)
        if self._hover_line_item is None:
            pen = QPen(QColor("#ffe08a"))
            pen.setWidth(1)
            self._hover_line_item = self.scene.addLine(0, 0, 0, 1, pen)
            self._hover_line_item.setZValue(2.7)
        self._hover_line_item.setLine(hover_x, self._hover_grid_top, hover_x, self._hover_grid_bottom)
        self._hover_line_item.setVisible(True)
        total_seconds = max(1.0, (self._hover_window_end - self._hover_window_start).total_seconds())
        ratio = (hover_x - timeline_x) / timeline_width
        hover_dt = self._hover_window_start + timedelta(seconds=ratio * total_seconds)
        if self._hover_label_item is None:
            self._hover_label_item = self.scene.addText("")
            self._hover_label_item.setDefaultTextColor(QColor("#ffe08a"))
            self._hover_label_item.setZValue(3.2)
        self._hover_label_item.setPlainText(hover_dt.astimezone().strftime("%H:%M:%S"))
        self._hover_label_item.setPos(
            min(max(timeline_x, hover_x - 26), timeline_x + timeline_width - 70), 16
        )
        self._hover_label_item.setVisible(True)

    def _set_display_mode(self, mode: str):
        if mode == self._display_mode:
            return
        current_window = self._visible_window()
        self._display_mode = mode
        target_window = self._visible_window()
        if current_window is not None and target_window is not None:
            start_current, end_current = current_window
            start_target, end_target = target_window
            current_span = max(60.0, (end_current - start_current).total_seconds())
            target_span = max(60.0, (end_target - start_target).total_seconds())
            self._range_anim_now_local = _local_now()
            self._range_anim_span_seconds = current_span
            if self._range_anim.state() == QVariantAnimation.Running:
                self._range_anim.stop()
            self._range_anim.setStartValue(float(current_span))
            self._range_anim.setEndValue(float(target_span))
            self._range_anim.start()
        self._schedule_redraw()

    def _on_refresh_timer(self):
        self.refresh(force_full=False)

    def _schedule_redraw(self):
        self._redraw_soon.start()

    def _on_redraw_timer(self):
        if not self._active:
            return
        if self._window_shift_needs_redraw():
            self._schedule_redraw()
        else:
            # The window hasn't moved a visible amount: just tick the clock.
            self._update_now_label()

    def _window_shift_needs_redraw(self) -> bool:
        """True when the now-anchored window has slid far enough since the
        last rebuild that band positions are visibly stale (>= 0.5 px)."""
        if self._last_drawn_window is None or self._hover_timeline_width <= 0:
            return True
        window = self._visible_window()
        if window is None:
            return True
        new_start, new_end = window
        old_start, old_end = self._last_drawn_window
        if new_end.astimezone().date() != old_end.astimezone().date():
            return True
        total_seconds = max(60.0, (new_end - new_start).total_seconds())
        px_per_second = self._hover_timeline_width / total_seconds
        shift_px = abs((new_end - old_end).total_seconds()) * px_per_second
        # Window-start shifts differently when clamped to the day start.
        shift_px = max(shift_px, abs((new_start - old_start).total_seconds()) * px_per_second)
        return shift_px >= 0.5

    def _sticky_offset(self) -> float:
        """Scene-y of the viewport top; 0 until the view scrolls."""
        return max(0.0, self.view.mapToScene(0, 0).y())

    def _reposition_sticky_header(self, _value=None):
        offset = self._sticky_offset()
        for item, base_y in self._sticky_header_items:
            item.setY(base_y + offset)
        self._update_now_label()
        self._update_last_update_marker()

    def _live_scene_item(self, item):
        """The item if it still belongs to a scene, else None (the scene
        rebuild clears everything; Qt frees cleared items)."""
        if item is None:
            return None
        try:
            return item if item.scene() is not None else None
        except RuntimeError:
            return None

    def _update_last_update_marker(self):
        """Blue full-height line at the time the data was last refreshed,
        with an 'updated HH:MM:SS' label on the sticky header's second
        row (Chris, 2026-09-04). Persistent items, updated in place."""
        line = self._live_scene_item(self._last_update_line_item)
        label = self._live_scene_item(self._last_update_label_item)
        if (
            self._hover_timeline_width <= 0
            or self._hover_window_start is None
            or self._hover_window_end is None
            or self._last_refreshed_local is None
            or self._filter_day_range is not None
        ):
            for item in (line, label):
                if item is not None:
                    item.setVisible(False)
            return
        updated_utc = ensure_utc(self._last_refreshed_local)
        total_seconds = max(1.0, (self._hover_window_end - self._hover_window_start).total_seconds())
        ratio = (updated_utc - self._hover_window_start).total_seconds() / total_seconds
        timeline_x = self._hover_timeline_x
        timeline_width = self._hover_timeline_width
        x = timeline_x + max(0.0, min(1.0, ratio)) * timeline_width
        if line is None:
            pen = QPen(QColor(theme.ACCENT))
            pen.setWidth(1)
            line = self.scene.addLine(0, 0, 0, 0, pen)
            line.setZValue(2.6)
        self._last_update_line_item = line
        line.setLine(x, self._hover_grid_top, x, self._hover_grid_bottom)
        line.setVisible(True)
        if label is None:
            label = self.scene.addText("")
            label.setDefaultTextColor(QColor(theme.ACCENT))
            label.setZValue(10)
        self._last_update_label_item = label
        label.setPlainText(f"updated {self._last_refreshed_local:%H:%M:%S}")
        label.setPos(
            min(max(timeline_x, x - 40), timeline_x + timeline_width - 110),
            20 + self._sticky_offset(),
        )
        label.setVisible(True)

    def _update_now_label(self):
        """Update the persistent HH:MM:SS marker in place between rebuilds.
        Hidden in historic range mode - "now" is off the timeline there."""
        if self._filter_day_range is not None:
            item = self._live_scene_item(self._now_label_item)
            if item is not None:
                item.setVisible(False)
            return
        if (
            self._hover_timeline_width <= 0
            or self._hover_window_start is None
            or self._hover_window_end is None
        ):
            return
        item = self._now_label_item
        if item is not None:
            try:
                if item.scene() is None:
                    item = None
            except RuntimeError:
                item = None
        now_local = _local_now()
        total_seconds = max(1.0, (self._hover_window_end - self._hover_window_start).total_seconds())
        ratio = (ensure_utc(now_local) - self._hover_window_start).total_seconds() / total_seconds
        timeline_x = self._hover_timeline_x
        timeline_width = self._hover_timeline_width
        x = timeline_x + max(0.0, min(1.0, ratio)) * timeline_width
        if item is None:
            item = self.scene.addText("")
            item.setDefaultTextColor(QColor("#ffe08a"))
            item.setZValue(10)
        self._now_label_item = item
        # Opaque background: the clock slides across the tick labels on
        # the same header row; without it the two texts merge into a
        # jumble (Chris, 2026-09-05 screenshot).
        item.setHtml(
            f'<span style="background-color:{theme.BG}; color:#ffe08a;">'
            f"{now_local.strftime('%H:%M:%S')}</span>"
        )
        item.setPos(
            min(max(timeline_x, x - 20), timeline_x + timeline_width - 64),
            4 + self._sticky_offset(),
        )

    def _on_range_anim_value_changed(self, value):
        try:
            self._range_anim_span_seconds = float(value)
        except Exception:
            self._range_anim_span_seconds = None
        self._schedule_redraw()

    def _on_range_anim_finished(self):
        self._range_anim_now_local = None
        self._range_anim_span_seconds = None
        self._schedule_redraw()

    def _on_loading_media_status_changed(self, status):
        if status == QMediaPlayer.EndOfMedia and self._content_stack.currentWidget() is self._loading_page:
            self._loading_player.setPosition(0)
            self._loading_player.play()

    def _stop_loading_video(self):
        try:
            self._loading_player.pause()
            self._loading_player.setPosition(0)
        except Exception:
            pass

    def _set_loading_visible(self, visible: bool, message: str | None = None):
        if message:
            self._loading_label.setText(message)
            self._loading_label.adjustSize()
        if visible:
            self._content_stack.setCurrentWidget(self._loading_page)
            if self._loading_player.source().isEmpty():
                return
            if self._loading_player.playbackState() != QMediaPlayer.PlayingState:
                self._loading_player.setPosition(0)
                self._loading_player.play()
        else:
            if self._content_stack.currentWidget() is not self.view:
                self._content_stack.setCurrentWidget(self.view)
            self._stop_loading_video()

    def _merge_payload(self, payload: dict, is_final: bool):
        systems = payload.get("systems", [])
        now_local = payload.get("now_local")
        day_value = payload.get("day_value")
        full_refresh = bool(payload.get("full_refresh"))
        replace = bool(payload.get("replace"))
        if not isinstance(now_local, datetime):
            now_local = _local_now()
        if full_refresh and replace:
            self._states.clear()
            self._summary_cache.clear()
            self._latest_event_ts = None
        if full_refresh and is_final:
            self._last_full_refresh_local = now_local
        self._last_day = day_value if isinstance(day_value, date) else now_local.date()
        if is_final:
            self._last_refreshed_local = now_local
        latest_ts = self._latest_event_ts

        for row in systems:
            root = row.get("root")
            if not isinstance(root, Path):
                continue
            key = str(root)
            state = self._states.get(key)
            if state is None:
                state = OverviewSystemState(
                    name=str(row.get("name") or root.name),
                    root=root,
                    robot_id=row.get("robot_id"),
                )
                self._states[key] = state
            state.name = str(row.get("name") or root.name)
            state.robot_id = row.get("robot_id")
            state.video_items = list(row.get("video_items") or [])
            thumb = row.get("thumbnail_image")
            state.thumbnail_image = thumb if isinstance(thumb, QImage) else None
            incoming_events = list(row.get("events") or [])
            if replace:
                state.events = []
            if full_refresh and not incoming_events and replace:
                state.events = []
            elif full_refresh and is_final:
                state.events = incoming_events
            else:
                seen = {_event_key(evt) for evt in state.events}
                for evt in incoming_events:
                    evt_key = _event_key(evt)
                    if evt_key in seen:
                        continue
                    state.events.append(evt)
                    seen.add(evt_key)
            state.events.sort(key=lambda item: ensure_utc(item.get("ts")) if isinstance(item.get("ts"), datetime) else datetime.min.replace(tzinfo=timezone.utc))
            cutoff = payload.get("cutoff_utc")
            if not isinstance(cutoff, datetime):
                cutoff = _start_of_day_local(now_local).astimezone(timezone.utc)
            state.events = [
                evt for evt in state.events
                if isinstance(evt.get("ts"), datetime) and ensure_utc(evt["ts"]) >= cutoff
            ]
            if state.events:
                state.last_event_time = ensure_utc(state.events[-1]["ts"])
                if latest_ts is None or state.last_event_time > latest_ts:
                    latest_ts = state.last_event_time
            else:
                state.last_event_time = None
            state.events_version += 1
            new_last_stop = self._latest_stop_time(state)
            stop_changed = new_last_stop is not None and new_last_stop != state.last_stop_time
            state.last_stop_time = new_last_stop
            self._maybe_queue_thumbnail_prefetch(state, now_local, force=stop_changed or full_refresh)
        if is_final:
            self._latest_event_ts = latest_ts
            self._set_loading_visible(False)
            # Cache-seeded sessions never stamp a full refresh; anchor the
            # periodic resync clock at first final merge so it still fires.
            if self._last_full_refresh_local is None:
                self._last_full_refresh_local = now_local
            self._maybe_persist_events_cache(now_local)
        self._schedule_redraw()

    def _maybe_persist_events_cache(self, now_local: datetime, force: bool = False):
        """Write today's merged events to disk, throttled; the JSON dump
        and file write run on a daemon thread off the UI. Live mode only:
        a historic range must never masquerade as today's cache."""
        if self._filter_day_range is not None:
            return
        if self._last_day != now_local.date():
            return
        now_mono = time.monotonic()
        if not force and (now_mono - self._last_cache_save_mono) < OVERVIEW_CACHE_SAVE_MIN_SECONDS:
            return
        events_by_robot: dict[str, list[dict]] = {}
        for state in self._states.values():
            if state.robot_id and state.events:
                # Shallow snapshot: event dicts are never mutated after the
                # merge, only the per-state list is.
                events_by_robot[state.robot_id] = [dict(evt) for evt in state.events]
        if not events_by_robot:
            return
        self._last_cache_save_mono = now_mono
        threading.Thread(
            target=save_overview_events,
            args=(self._last_day, events_by_robot),
            daemon=True,
        ).start()

    def _on_loaded(self, payload: dict):
        self.activity_cleared.emit("overview-load")
        if payload is None:
            return
        self._background_enabled = True
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()
        self._merge_payload(payload, is_final=True)

    def _on_load_progress(self, message: str):
        text = str(message or "").strip()
        if not text:
            return
        self.status_label.setText(text)
        self.activity_progress.emit("overview-load", f"Overview: {text}", None, None)
        if not self._states:
            self._set_loading_visible(True, text)

    def _on_failed(self, message: str):
        self.activity_cleared.emit("overview-load")
        self.status_label.setText(f"Overview refresh failed: {message}")
        if self._states:
            self._set_loading_visible(False)
        else:
            self._set_loading_visible(True, f"Overview refresh failed: {message}")

    def _stop_load_thread(self):
        self._overview_slot.retire()

    def _earliest_data_utc(self) -> datetime | None:
        """Earliest event or video-clip start across all systems (UTC).
        Both per-state lists are kept sorted, so only the heads matter."""
        earliest: datetime | None = None
        for state in self._states.values():
            candidates = []
            if state.events:
                ts = state.events[0].get("ts")
                if isinstance(ts, datetime):
                    candidates.append(ensure_utc(ts))
            for item in state.video_items:
                start = getattr(item, "start", None)
                if isinstance(start, datetime):
                    candidates.append(ensure_utc(start))
                break
            for ts in candidates:
                if earliest is None or ts < earliest:
                    earliest = ts
        return earliest

    def _visible_window(self) -> tuple[datetime, datetime] | None:
        now_local = self._range_anim_now_local or _local_now()
        # Live mode anchors the window end at "now" within today; a
        # historic range anchors it at the range end (or now, when the
        # range includes today).
        if self._filter_day_range is not None:
            start_day, end_day = self._filter_day_range
            window_floor = _start_of_day(start_day)
            window_end = min(now_local, _start_of_day(end_day) + timedelta(days=1))
            if window_end <= window_floor:
                window_end = window_floor + timedelta(minutes=1)
        else:
            window_floor = _start_of_day_local(now_local)
            window_end = now_local
        if self._range_anim_span_seconds is not None:
            span_delta = timedelta(seconds=max(60.0, self._range_anim_span_seconds))
            start_local = max(window_floor, window_end - span_delta)
        elif self._display_mode == "1h":
            start_local = max(window_floor, window_end - timedelta(hours=1))
        elif self._display_mode == "all":
            # "All" zooms to the actual data, not to midnight: first
            # event/clip minus 30 min of lead-in, clamped to the span
            # (Chris, 2026-09-05). No data yet -> the whole span.
            start_local = window_floor
            earliest = self._earliest_data_utc()
            if earliest is not None:
                padded = earliest.astimezone() - timedelta(minutes=30)
                start_local = max(
                    window_floor,
                    min(padded, window_end - timedelta(minutes=1)),
                )
        else:
            start_local = max(window_floor, window_end - timedelta(hours=5))
        return start_local.astimezone(timezone.utc), window_end.astimezone(timezone.utc)

    def _summary_for(self, state: OverviewSystemState, data_cutoff_utc: datetime) -> dict:
        """Cached _summarize_system: the redraw timer fires every second but
        the underlying events change only when a refresh merges data."""
        key = str(state.root)
        cached = self._summary_cache.get(key)
        if cached is not None and cached[0] == state.events_version and cached[1] == data_cutoff_utc:
            return cached[2]
        summary = self._summarize_system(state, data_cutoff_utc)
        self._summary_cache[key] = (state.events_version, data_cutoff_utc, summary)
        return summary

    def _summarize_system(self, state: OverviewSystemState, data_cutoff_utc: datetime) -> dict:
        order = {"stop": 0, "auto": 1, "manual": 2, "start": 3, "select": 4}
        events: list[tuple[datetime, str, dict | None, str, str]] = []
        for evt in state.events:
            ts = evt.get("ts")
            if not isinstance(ts, datetime):
                continue
            ts = ensure_utc(ts)
            state_name = str(evt.get("state_name") or "").strip()
            message = str(evt.get("message") or "")
            service_name = str(evt.get("service_name") or "")
            selection = evt.get("selection") if isinstance(evt.get("selection"), dict) else None
            lower_state = state_name.lower()
            if state_name == "start_pnp":
                events.append((ts, "start", selection, state_name, message))
            elif selection:
                events.append((ts, "select", selection, state_name, message))
            if state_name == "controller_node_manual_mode" or ("manual" in lower_state and "mode" in lower_state):
                events.append((ts, "manual", None, state_name, message))
            if state_name == "controller_node_automatic_mode" or ("automatic" in lower_state and "mode" in lower_state):
                events.append((ts, "auto", None, state_name, message))
            if _is_stop_like_event(state_name, message, service_name):
                events.append((ts, "stop", None, state_name, message))
        events.sort(key=lambda item: (item[0], order.get(item[1], 9)))

        sku_segments: list[dict] = []
        manual_segments: list[dict] = []
        stop_markers: list[dict] = []
        current_kind: str | None = None
        current_start: datetime | None = None
        current_data: dict | None = None
        last_sku_data: dict | None = None
        current_status = "Unknown"
        last_state_name = ""

        def close_current(end_ts: datetime):
            nonlocal current_kind, current_start, current_data
            if not current_kind or not current_start or end_ts <= current_start:
                current_kind = None
                current_start = None
                current_data = None
                return
            if current_kind == "manual":
                manual_segments.append({"start": current_start, "end": end_ts})
            else:
                payload = current_data or {}
                sku_segments.append(
                    {
                        "start": current_start,
                        "end": end_ts,
                        "sku": str(payload.get("sku") or ""),
                        "tray": str(payload.get("tray") or ""),
                        "tool": str(payload.get("tool") or ""),
                    }
                )
            current_kind = None
            current_start = None
            current_data = None

        def sku_key(payload: dict | None) -> tuple[str, str, str]:
            if not isinstance(payload, dict):
                return ("", "", "")
            return (
                str(payload.get("sku") or ""),
                str(payload.get("tray") or ""),
                str(payload.get("tool") or ""),
            )

        for ts, kind, data, state_name, message in events:
            last_state_name = state_name or message or last_state_name
            if kind == "stop":
                stop_markers.append({"ts": ts, "state_name": state_name, "message": message})
                if current_kind == "sku":
                    close_current(ts)
                if current_kind != "manual":
                    current_status = "Stopped"
                continue
            if kind == "auto":
                if current_kind == "manual":
                    close_current(ts)
                    current_status = "Auto"
                continue
            if kind == "manual":
                if current_kind == "manual":
                    current_status = "Manual"
                    continue
                close_current(ts)
                current_kind = "manual"
                current_start = ts
                current_data = None
                current_status = "Manual"
                continue
            if kind == "select" and data:
                last_sku_data = data
                if current_kind == "sku" and sku_key(current_data) != sku_key(data):
                    close_current(ts)
                    current_kind = "sku"
                    current_start = ts
                    current_data = data
                    current_status = "Running"
                continue
            if kind == "start":
                if data:
                    last_sku_data = data
                close_current(ts)
                current_kind = "sku"
                current_start = ts
                current_data = data or last_sku_data or {}
                current_status = "Running"

        active_kind = current_kind
        active_data = dict(current_data or {}) if isinstance(current_data, dict) else current_data
        close_current(data_cutoff_utc)
        active_sku = ""
        faded_segment = None
        if active_kind == "manual":
            active_sku = ""
        elif active_kind == "sku" and active_data:
            active_sku = self._format_sku_parts(active_data)
        elif sku_segments:
            active_sku = self._format_sku_parts(sku_segments[-1])
        if active_kind == "manual":
            faded_segment = {
                "kind": "manual",
                "sku": "",
            }
        elif active_kind == "sku":
            faded_segment = {
                "kind": "sku",
                "sku": "",
            }
            if isinstance(active_data, dict):
                faded_segment["sku"] = self._format_sku_parts(active_data)
        if active_kind == "manual":
            current_status = "Manual"
        elif active_kind == "sku":
            current_status = "Running"
        elif current_status == "Unknown" and state.events:
            current_status = "Idle"

        return {
            "status": current_status,
            "current_sku": active_sku,
            "last_state_name": last_state_name,
            "sku_segments": sku_segments,
            "manual_segments": manual_segments,
            "stop_markers": stop_markers,
            "faded_segment": faded_segment,
        }

    @staticmethod
    def _format_sku_parts(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return ""
        parts = [
            str(payload.get("sku") or "").strip(),
            str(payload.get("tray") or "").strip(),
            str(payload.get("tool") or "").strip(),
        ]
        return " | ".join([part for part in parts if part])

    def _row_tooltip(self, state: OverviewSystemState, summary: dict) -> str:
        lines = [state.name]
        lines.append(f"Status: {summary.get('status') or 'Unknown'}")
        sku_value = summary.get("current_sku") or ""
        if summary.get("status") == "Running" and sku_value:
            lines.append(f"SKU: {sku_value}")
        if state.last_event_time is not None:
            lines.append(f"Last event: {ensure_utc(state.last_event_time).astimezone().strftime('%H:%M:%S')}")
        last_state_name = str(summary.get("last_state_name") or "").strip()
        if last_state_name:
            lines.append(last_state_name)
        return "\n".join(lines)

    @staticmethod
    def _fit_text(text: str, max_width: float, metrics: QFontMetrics) -> str:
        if max_width <= 0:
            return ""
        if metrics.horizontalAdvance(text) <= max_width:
            return text
        ellipsis = "..."
        if metrics.horizontalAdvance(ellipsis) > max_width:
            return ""
        trimmed = text
        while trimmed and metrics.horizontalAdvance(trimmed + ellipsis) > max_width:
            trimmed = trimmed[:-1]
        return trimmed + ellipsis if trimmed else ""

    @staticmethod
    def _latest_stop_time(state: OverviewSystemState) -> datetime | None:
        latest: datetime | None = None
        for evt in state.events:
            ts = evt.get("ts")
            if not isinstance(ts, datetime):
                continue
            state_name = str(evt.get("state_name") or "").strip().lower()
            message = str(evt.get("message") or "")
            service_name = str(evt.get("service_name") or "")
            if _is_stop_like_event(state_name, message, service_name):
                ts = ensure_utc(ts)
                if latest is None or ts > latest:
                    latest = ts
        return latest

    def _maybe_queue_thumbnail_prefetch(self, state: OverviewSystemState, now_local: datetime, force: bool = False):
        if self._prefetch_clips is None or self.cache_root is None:
            return
        latest_video_path = None
        latest_video_start = None
        for item in state.video_items:
            if item.kind != "video" or not isinstance(item.payload, Path):
                continue
            if latest_video_start is None or item.start > latest_video_start:
                latest_video_start = item.start
                latest_video_path = item.payload
        if latest_video_path is None:
            return
        queue_due = force
        if not queue_due:
            if state.last_thumbnail_queue_time is None:
                queue_due = True
            else:
                queue_due = (now_local - state.last_thumbnail_queue_time) >= timedelta(minutes=OVERVIEW_THUMBNAIL_REFRESH_MINUTES)
        if not queue_due:
            return
        try:
            cache_path = _cache_key_for(latest_video_path, self.cache_root)
        except Exception:
            return
        if cache_path.exists() and not force:
            state.last_thumbnail_queue_time = now_local
            return
        try:
            self._prefetch_clips([latest_video_path])
            state.last_thumbnail_queue_time = now_local
        except Exception:
            pass

    def show_thumbnail_preview(self, state: OverviewSystemState, thumb_rect: QRectF):
        image = state.thumbnail_image
        if image is None or image.isNull():
            self._preview_label.hide()
            return
        pixmap = QPixmap.fromImage(image).scaled(
            260,
            160,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self._preview_label.setPixmap(pixmap)
        self._preview_label.adjustSize()
        viewport_pos = self.view.mapFromScene(thumb_rect.topRight())
        x = min(self.view.viewport().width() - self._preview_label.width() - 8, viewport_pos.x() + 12)
        y = max(8, min(self.view.viewport().height() - self._preview_label.height() - 8, viewport_pos.y() - 8))
        self._preview_label.move(max(8, x), y)
        self._preview_label.show()
        self._preview_label.raise_()

    def hide_thumbnail_preview(self):
        self._preview_label.hide()

    def refresh_layout(self):
        self._schedule_redraw()

    def toggle_customer_collapsed(self, customer_name: str):
        key = str(customer_name or "").strip()
        if not key:
            return
        if key in self._collapsed_customers:
            self._collapsed_customers.discard(key)
        else:
            self._collapsed_customers.add(key)
        set_customer_collapsed(key, key in self._collapsed_customers)
        self._schedule_redraw()

    def _redraw(self):
        self._redraw_soon.stop()
        self.scene.clear()
        # Everything the clear just deleted; early returns below must leave
        # a consistent "nothing drawn" state.
        self._hover_line_item = None
        self._hover_label_item = None
        self._now_label_item = None
        self._hover_timeline_width = 0
        self._last_drawn_window = None
        self.hide_thumbnail_preview()
        if self.parent_dir is None:
            self.status_label.setText("Set a parent folder to enable overview")
            self._set_loading_visible(False)
            return
        states = sorted(self._states.values(), key=lambda item: system_group_sort_key(self.settings, item.name))
        if not states:
            if self._overview_slot.is_running():
                self.status_label.setText("Loading overview...")
                self._set_loading_visible(True, "Loading overview...")
            else:
                self.status_label.setText("No overview data loaded")
                self._set_loading_visible(False)
            return
        self._set_loading_visible(False)

        visible_window = self._visible_window()
        if visible_window is None:
            return
        window_start, window_end = visible_window
        now_local = _local_now()
        updated_text = self._last_refreshed_local.strftime("%H:%M:%S") if self._last_refreshed_local else "--:--:--"
        self.status_label.setText(f"{len(states)} systems | data updated {updated_text}")

        viewport_width = max(360, self.view.viewport().width() or 360)
        viewport_height = max(320, self.view.viewport().height() or 500)
        left_pad = 128
        right_pad = 190
        # Two sticky header rows: column titles / time ticks / now-clock
        # on the first, the blue last-update label on the second.
        top_pad = 46
        bottom_pad = 18
        row_height = OVERVIEW_ROW_HEIGHT
        header_height = 36
        display_rows: list[tuple[str, object]] = []
        last_customer = None
        for state in states:
            customer_name = display_customer_name(self.settings, state.name)
            if customer_name != last_customer:
                display_rows.append(("header", customer_name))
                last_customer = customer_name
            if customer_name not in self._collapsed_customers:
                display_rows.append(("system", state))
        scene_width = viewport_width
        timeline_x = left_pad
        timeline_width = max(120, scene_width - left_pad - right_pad - 12)
        self._hover_window_start = window_start
        self._hover_window_end = window_end
        self._hover_timeline_x = timeline_x
        self._hover_timeline_width = timeline_width
        scene_height = top_pad
        for row_type, _payload in display_rows:
            scene_height += header_height if row_type == "header" else row_height
        scene_height += bottom_pad
        self.scene.setSceneRect(0, 0, scene_width, scene_height)

        title_font = QFont()
        title_font.setBold(True)
        # Sticky header band: opaque, above rows/grid, below its labels.
        self._sticky_header_items = []
        band = self.scene.addRect(
            QRectF(0, 0, scene_width, top_pad - 6), QPen(Qt.NoPen), QBrush(QColor(theme.BG))
        )
        band.setZValue(8)
        self._sticky_header_items.append((band, 0.0))
        header = self.scene.addText("System")
        header.setFont(title_font)
        header.setDefaultTextColor(QColor("#d7dde2"))
        header.setPos(8, 4)
        header.setZValue(10)
        self._sticky_header_items.append((header, 4.0))
        status_header = self.scene.addText("State / SKU")
        status_header.setFont(title_font)
        status_header.setDefaultTextColor(QColor("#d7dde2"))
        status_header.setPos(scene_width - right_pad + 8, 4)
        status_header.setZValue(10)
        self._sticky_header_items.append((status_header, 4.0))

        total_seconds = max(60.0, (window_end - window_start).total_seconds())
        total_minutes = max(1, int(total_seconds // 60))
        if total_minutes <= 60:
            minor_step_min = 5
            major_step_min = 15
        elif total_minutes <= 5 * 60:
            minor_step_min = 15
            major_step_min = 60
        elif total_minutes <= 24 * 60:
            minor_step_min = 30
            major_step_min = 120
        elif total_minutes <= 3 * 24 * 60:
            minor_step_min = 120
            major_step_min = 360
        else:
            minor_step_min = 360
            major_step_min = 1440
        multi_day = total_minutes > 24 * 60

        tick = window_start.replace(second=0, microsecond=0)
        remainder = tick.minute % minor_step_min
        if remainder:
            tick += timedelta(minutes=(minor_step_min - remainder))
        grid_top = top_pad - 10
        grid_bottom = scene_height - bottom_pad
        self._hover_grid_top = grid_top
        self._hover_grid_bottom = grid_bottom
        while tick <= window_end:
            ratio = (tick - window_start).total_seconds() / total_seconds
            x = timeline_x + max(0.0, min(1.0, ratio)) * timeline_width
            minutes_from_midnight = tick.hour * 60 + tick.minute
            is_major = (minutes_from_midnight % major_step_min) == 0
            pen = QPen(QColor("#33424d") if is_major else QColor("#25313a"))
            pen.setWidth(1 if is_major else 1)
            line = self.scene.addLine(x, grid_top, x, grid_bottom, pen)
            line.setZValue(1.5)
            if is_major:
                tick_fmt = "%d/%m %H:%M" if multi_day else "%H:%M"
                label = self.scene.addText(tick.astimezone().strftime(tick_fmt))
                label.setDefaultTextColor(QColor("#8ea2b2"))
                label.setPos(x + 2, 4)
                label.setZValue(10)
                self._sticky_header_items.append((label, 4.0))
            tick += timedelta(minutes=minor_step_min)

        # Applies the current scroll offset to the fresh sticky items and
        # refreshes the now-label in one go.
        self._reposition_sticky_header()
        # The scene was cleared above; hover items are lazily recreated.
        self._update_hover_indicator()
        self._last_drawn_window = (window_start, window_end)

        current_y = top_pad
        system_row_index = 0
        for row_type, payload in display_rows:
            if row_type == "header":
                # Customer bar: accent-blue so it stands out from the
                # rows; name first, then the collapse arrow; no logos
                # (Chris, 2026-09-05).
                header_rect = QRectF(4, current_y, scene_width - 8, header_height - 2)
                header_item = _OverviewCustomerHeaderItem(header_rect, str(payload), self)
                header_item.setPen(QPen(Qt.NoPen))
                header_item.setBrush(QBrush(QColor(theme.ACCENT_DIM)))
                header_item.setZValue(0.2)
                self.scene.addItem(header_item)
                collapsed = str(payload) in self._collapsed_customers
                header_text = self.scene.addText(str(payload))
                header_text.setDefaultTextColor(QColor(theme.TEXT_BRIGHT))
                header_text.setPos(10, current_y + 7)
                header_text.setZValue(2.2)
                arrow_item = self.scene.addText("▼" if collapsed else "▲")
                arrow_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
                arrow_item.setPos(
                    10 + header_text.boundingRect().width() + 4, current_y + 7
                )
                arrow_item.setZValue(2.2)
                header_item.set_arrow_item(arrow_item)
                current_y += header_height
                continue

            state = payload
            y = current_y
            # Machines sit slightly indented under their customer bar
            # (Chris, 2026-09-05).
            row_rect = QRectF(18, y, scene_width - 22, row_height - 2)
            background = QColor("#182028" if system_row_index % 2 == 0 else "#141b22")
            row_item = QGraphicsRectItem(row_rect)
            row_item.setPen(QPen(Qt.NoPen))
            row_item.setBrush(QBrush(background))
            row_item.setZValue(0)
            self.scene.addItem(row_item)

            line_name = display_line_name(self.settings, state.name)
            row_label = f"{line_name} | {state.name}" if line_name else state.name
            name_item = self.scene.addText(row_label)
            name_item.setDefaultTextColor(QColor("#dde6ee"))
            name_item.setPos(22, y + 4)

            lane_rect = QRectF(timeline_x, y + 5, timeline_width, row_height - 10)
            lane = QGraphicsRectItem(lane_rect)
            lane.setPen(QPen(QColor("#31414d")))
            lane.setBrush(QBrush(QColor("#0f1419")))
            lane.setToolTip("Loading...")
            lane.setZValue(1)
            self.scene.addItem(lane)

            # Historic ranges summarise up to the range end, not "now" -
            # otherwise the final state reads as lasting days.
            if self._filter_day_range is not None:
                data_cutoff_utc = window_end
            else:
                data_cutoff_utc = ensure_utc(self._last_refreshed_local or now_local)
            summary = self._summary_for(state, data_cutoff_utc)
            lane.setToolTip(self._row_tooltip(state, summary))

            for video_item in state.video_items:
                clipped = _clip_window(video_item.start, video_item.end, window_start, window_end)
                if not clipped:
                    continue
                start_dt, end_dt = clipped
                x1 = timeline_x + ((start_dt - window_start).total_seconds() / total_seconds) * timeline_width
                x2 = timeline_x + ((end_dt - window_start).total_seconds() / total_seconds) * timeline_width
                band = QRectF(x1, y + row_height - 11, max(1.0, x2 - x1), 4)
                video_band = QGraphicsRectItem(band)
                video_band.setPen(QPen(Qt.NoPen))
                video_band.setBrush(QBrush(QColor("#3a6ea5")))
                video_band.setZValue(2)
                self.scene.addItem(video_band)

            for segment in summary.get("sku_segments", []):
                clipped = _clip_window(segment["start"], segment["end"], window_start, window_end)
                if not clipped:
                    continue
                start_dt, end_dt = clipped
                x1 = timeline_x + ((start_dt - window_start).total_seconds() / total_seconds) * timeline_width
                x2 = timeline_x + ((end_dt - window_start).total_seconds() / total_seconds) * timeline_width
                rect = QRectF(x1, y + 8, max(1.5, x2 - x1), max(6, row_height - 16))
                sku_band = QGraphicsRectItem(rect)
                sku_band.setPen(QPen(Qt.NoPen))
                sku_band.setBrush(QBrush(QColor("#7cc77b")))
                sku_band.setToolTip(self._format_sku_parts(segment) or "Running")
                sku_band.setZValue(3)
                self.scene.addItem(sku_band)
                label_text = self._format_sku_parts(segment)
                if label_text and rect.width() >= 44:
                    font = QFont()
                    font.setPointSize(8)
                    metrics = QFontMetrics(font)
                    fitted = self._fit_text(label_text, rect.width() - 6, metrics)
                    if fitted:
                            text_item = self.scene.addText(fitted, font)
                            text_item.setDefaultTextColor(QColor("#102611"))
                            text_item.setPos(rect.x() + 3, rect.y() + max(0, (rect.height() - 18) / 2))
                            text_item.setZValue(4)

            for segment in summary.get("manual_segments", []):
                clipped = _clip_window(segment["start"], segment["end"], window_start, window_end)
                if not clipped:
                    continue
                start_dt, end_dt = clipped
                x1 = timeline_x + ((start_dt - window_start).total_seconds() / total_seconds) * timeline_width
                x2 = timeline_x + ((end_dt - window_start).total_seconds() / total_seconds) * timeline_width
                rect = QRectF(x1, y + 8, max(1.5, x2 - x1), max(6, row_height - 16))
                manual_band = QGraphicsRectItem(rect)
                manual_band.setPen(QPen(Qt.NoPen))
                manual_band.setBrush(QBrush(QColor("#f0ad4e")))
                manual_band.setToolTip("Manual mode")
                manual_band.setZValue(3)
                self.scene.addItem(manual_band)

            faded_segment = summary.get("faded_segment")
            if isinstance(faded_segment, dict) and self._last_refreshed_local is not None:
                fade_start = ensure_utc(self._last_refreshed_local)
                fade_end = ensure_utc(now_local)
                clipped = _clip_window(fade_start, fade_end, window_start, window_end)
                if clipped:
                    start_dt, end_dt = clipped
                    x1 = timeline_x + ((start_dt - window_start).total_seconds() / total_seconds) * timeline_width
                    x2 = timeline_x + ((end_dt - window_start).total_seconds() / total_seconds) * timeline_width
                    rect = QRectF(x1, y + 8, max(1.5, x2 - x1), max(6, row_height - 16))
                    color = QColor("#7cc77b") if faded_segment.get("kind") == "sku" else QColor("#f0ad4e")
                    color.setAlpha(90)
                    faded_band = QGraphicsRectItem(rect)
                    faded_band.setPen(QPen(Qt.NoPen))
                    faded_band.setBrush(QBrush(color))
                    faded_band.setToolTip("Last known state since previous refresh")
                    faded_band.setZValue(2.5)
                    self.scene.addItem(faded_band)
                    label_text = str(faded_segment.get("sku") or "")
                    if faded_segment.get("kind") == "sku" and label_text and rect.width() >= 44:
                        font = QFont()
                        font.setPointSize(8)
                        metrics = QFontMetrics(font)
                        fitted = self._fit_text(label_text, rect.width() - 6, metrics)
                        if fitted:
                            text_item = self.scene.addText(fitted, font)
                            ghost_color = QColor("#102611")
                            ghost_color.setAlpha(140)
                            text_item.setDefaultTextColor(ghost_color)
                            text_item.setPos(rect.x() + 3, rect.y() + max(0, (rect.height() - 18) / 2))
                            text_item.setZValue(4)

            for marker in summary.get("stop_markers", []):
                ts = marker.get("ts")
                if not isinstance(ts, datetime):
                    continue
                if ts < window_start or ts > window_end:
                    continue
                x = timeline_x + ((ts - window_start).total_seconds() / total_seconds) * timeline_width
                pen = QPen(QColor("#ff5f56"))
                pen.setWidth(2)
                line = self.scene.addLine(x, y + 6, x, y + row_height - 6, pen)
                line.setZValue(4)

            status_text = str(summary.get("status") or "Unknown")
            status_color = {
                "Running": QColor("#7cc77b"),
                "Manual": QColor("#f0ad4e"),
                "Stopped": QColor("#ff7a70"),
                "Idle": QColor("#9aa9b5"),
            }.get(status_text, QColor("#9aa9b5"))
            status_item = self.scene.addText(status_text)
            status_item.setDefaultTextColor(status_color)
            status_item.setPos(scene_width - right_pad + 8, y + 2)
            status_item.setZValue(4)

            sku_text = str(summary.get("current_sku") or "") if status_text == "Running" else ""
            if len(sku_text) > 28:
                sku_text = f"{sku_text[:25]}..."
            sku_item = self.scene.addText(sku_text)
            sku_item.setDefaultTextColor(QColor("#d7dde2"))
            sku_item.setPos(scene_width - right_pad + 8, y + 16)
            sku_item.setZValue(4)
            click_item = _OverviewTimelineClickItem(lane_rect, state, self.current_day(), window_start, window_end, self)
            click_item.setToolTip(self._row_tooltip(state, summary))
            click_item.setZValue(4.8)
            self.scene.addItem(click_item)
            current_y += row_height
            system_row_index += 1
