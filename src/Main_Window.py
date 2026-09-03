import sys
import time
import shutil
import subprocess
from dataclasses import dataclass
from concurrent.futures import as_completed
from bisect import bisect_left
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import cv2
from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QVariantAnimation, QEasingCurve, QSize, QThread
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QFontMetrics, QIcon, QGuiApplication, QImage, QPen, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QSplitter,
    QToolButton,
    QSizePolicy,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsTextItem,
    QPushButton,
    QDialog,
    QLabel,
    QScrollArea,
    QProgressDialog,
    QStackedWidget,
    QSplashScreen,
    QFileDialog,
)

from Date_Picker_frontend import DatePicker
from Time_Picker import (
    TimePicker,
    load_day_files,
    TimelineItem,
    parse_time_from_name,
    ensure_utc,
    local_day_start_utc,
    local_day_end_utc,
    format_local_time,
    MIN_BLOCK_DURATION,
    LAST_BLOCK_DURATION,
    inferred_live_clip_end,
    VIDEO_COLOR_CACHED,
    VIDEO_COLOR_UNCACHED,
    _is_path_cached,
    _build_cache_index,
    _path_key,
)
from elastic_loader import fetch_events, fetch_logs_for_range, set_system_id_override
from settings_store import Settings, display_customer_name, display_line_name
from Log_vid_gui import VideoLogViewer
from overview_widget import OverviewWidget
from fleetwide_elastic_search_widget import FleetwideElasticSearchWidget
from app_version import format_version_label
from target_buffer_loader import fetch_buffer_events
from target_buffer_widget import TargetBufferWidget, _summary_rows, _detail_rows, _display_target_id
from conveyor_calibration import ConveyorCalibration, load_calibration, save_calibration
from conveyor_calibration_dialog import ConveyorCalibrationDialog
from target_scope_widget import TargetScopeWidget

SPLASH_IMAGE_FILENAME = "Logfather Argus II.jpg"

DISABLE_CLIP_LOG_LOADING = True
DEBUG_CLIP_TIMING = True
ENABLE_CACHE_COLOR_UPDATE = True
ENABLE_EVENT_MARKERS = True
ENABLE_PREFETCH_ADJACENT = True
# Day-wide prefetch is off: HiDrive copies share the internet connection with
# Elastic Cloud, and saturating it made every timeline/log fetch crawl.
ENABLE_DAY_PREFETCH = False
ENABLE_LOG_BUTTON = True
STOP_THUMB_SIZE = (352, 198)
TIMELINE_MIN_HEIGHT = 165
TIMELINE_MAX_HEIGHT = 360
TIMELINE_EXPAND_DELAY_MS = 1500


class _BufferLoaderThread(QThread):
    done = Signal(list)

    def __init__(self, settings, pikpak_root, clip_start, clip_end):
        super().__init__()
        self._settings = settings
        self._pikpak_root = pikpak_root
        self._clip_start = clip_start
        self._clip_end = clip_end

    def run(self):
        try:
            events = fetch_buffer_events(
                self._settings, self._pikpak_root,
                self._clip_start, self._clip_end,
            )
        except Exception as exc:
            print(f"[buffer] load failed: {exc}")
            events = []
        self.done.emit(events)


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


class FadeSplashScreen(QSplashScreen):
    def __init__(self, pixmap: QPixmap, flags=Qt.WindowType.Widget):
        super().__init__(pixmap, flags)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.valueChanged.connect(self._on_fade_value_changed)
        self._fade_anim.finished.connect(self._on_fade_finished)

    def fade_and_finish(self, widget: QWidget):
        try:
            widget.raise_()
            widget.activateWindow()
        except Exception:
            pass
        self.setWindowOpacity(1.0)
        self._fade_anim.start()

    def _on_fade_value_changed(self, value):
        try:
            self.setWindowOpacity(float(value))
        except Exception:
            pass

    def _on_fade_finished(self):
        self.close()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("The Logfather")
        self._post_show_started = False

        self.system_id_override: str | None = None
        self.settings = Settings.load()
        if self.settings.load_warning:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Settings recovered", self.settings.load_warning
                ),
            )
        self.viewer = VideoLogViewer()
        cache_root = getattr(self.viewer, "cache_root", None)
        self.overview_widget = OverviewWidget(
            self.settings,
            cache_root=cache_root,
            prefetch_clips=self._prefetch_overview_clips,
            parent=self,
        )
        self.overview_widget.open_requested.connect(self._open_system_from_overview)
        self._pending_overview_navigation: dict | None = None
        self._overview_nav_timer = QTimer(self)
        self._overview_nav_timer.setInterval(150)
        self._overview_nav_timer.timeout.connect(self._continue_overview_navigation)
        self.viewer.settings_saved.connect(self._reload_settings_from_viewer)
        # Allow timeline expansion in non-maximised windows by reducing
        # the viewer's minimum height constraint.
        self.viewer.setMinimumSize(980, 120)
        self.date_picker = DatePicker()
        self.date_picker.set_system_layout_settings(self.settings)
        # Build static tracks: video + additional + condition rows
        static_tracks = self._build_static_tracks()

        # Extra loaders: Elastic events + additional CCTV clips
        extra_loaders = [
            lambda root, day: fetch_events(self.settings, root, day),
            lambda root, day: self._load_additional_cctv_items(root, day, cache_root),
        ]

        self.time_picker = TimePicker(
            load_day_files,
            extra_loaders=extra_loaders,
            static_tracks=static_tracks,
            cache_root=cache_root,
        )
        self.content_stack = QStackedWidget()
        self.content_stack.addWidget(self.viewer)
        self.content_stack.addWidget(self.overview_widget)
        self.fleetwide_search_widget = FleetwideElasticSearchWidget(self.settings, parent=self)
        self.fleetwide_search_widget.settings_saved.connect(self._sync_settings_from_fleetwide_search)
        self.content_stack.addWidget(self.fleetwide_search_widget)
        self.stop_report_btn = QPushButton("Stop Report")
        self.stop_report_btn.clicked.connect(self.open_stop_report)
        self.overview_btn = QToolButton()
        self.overview_btn.setText("Overview")
        self.overview_btn.setCheckable(True)
        self.overview_btn.toggled.connect(self._on_overview_toggled)
        self.fleetwide_search_btn = QToolButton()
        self.fleetwide_search_btn.setText("Fleetwide Search")
        self.fleetwide_search_btn.setCheckable(True)
        self.fleetwide_search_btn.toggled.connect(self._on_fleetwide_search_toggled)
        self.current_system_label = QLabel("")
        self.current_system_label.setStyleSheet("color: #d7dde2; padding-left: 8px;")
        self.current_system_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if hasattr(self.viewer, "add_playback_right_widget"):
            self.viewer.add_playback_right_widget(self.stop_report_btn)
            self.viewer.add_playback_right_widget(self.time_picker.fit_btn)
            self.viewer.add_playback_right_widget(self.time_picker.refresh_btn)
        self.time_picker.setMinimumHeight(TIMELINE_MIN_HEIGHT)
        self._timeline_min_height = TIMELINE_MIN_HEIGHT
        self._timeline_max_height = TIMELINE_MAX_HEIGHT
        self._timeline_expanded = False
        self.date_picker.setMaximumWidth(320)

        # Pick-target buffer panel
        self.buffer_widget = TargetBufferWidget()
        self.buffer_widget.setMinimumWidth(220)
        self.buffer_widget.setMaximumWidth(400)
        self._buffer_panel_visible = False
        self._buffer_panel_target_width = 280
        self._buffer_events = []
        self._buffer_loader_thread = None
        self._buffer_loader_refs: list = []   # keeps stale threads alive until they finish
        self._buffer_clip_start: datetime | None = None
        self._buffer_clip_end: datetime | None = None

        # Conveyor calibration
        self._conveyor_cal: ConveyorCalibration = ConveyorCalibration(system_id="")
        self._cal_dialog: ConveyorCalibrationDialog | None = None
        self._last_targets: list = []
        self._last_playhead_dt: datetime | None = None
        self._tracking_enabled: bool = True
        self._close_gap_target_ids: set[str] = set()
        self._wide_gap_target_ids: set[str] = set()
        self._scope: TargetScopeWidget | None = None

        self.left_toggle = QToolButton()
        self.left_toggle.setText("Hide Date Picker")
        self.left_toggle.setCheckable(True)
        self.left_toggle.setChecked(True)
        self.left_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.left_toggle.setVisible(False)
        self._hover_reveal_enabled = True
        self._left_reveal_px = 12

        self.buffer_toggle = QToolButton()
        self.buffer_toggle.setText("Targets")
        self.buffer_toggle.setCheckable(True)
        self.buffer_toggle.setChecked(False)
        self.buffer_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.buffer_toggle.toggled.connect(self._set_buffer_panel_visible)

        horizontal_splitter = QSplitter(Qt.Horizontal)
        horizontal_splitter.addWidget(self.date_picker)
        horizontal_splitter.addWidget(self.content_stack)
        horizontal_splitter.addWidget(self.buffer_widget)
        horizontal_splitter.setStretchFactor(0, 2)
        horizontal_splitter.setStretchFactor(1, 8)
        horizontal_splitter.setStretchFactor(2, 0)
        self._horizontal_splitter = horizontal_splitter
        self._left_panel_target_width = 320
        self._left_panel_visible = True
        self._left_panel_anim = QVariantAnimation(self)
        self._left_panel_anim.setDuration(170)
        self._left_panel_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._left_panel_anim.valueChanged.connect(self._on_left_panel_anim_step)
        self._left_panel_anim.finished.connect(self._on_left_panel_anim_finished)
        self._buffer_panel_anim = QVariantAnimation(self)
        self._buffer_panel_anim.setDuration(170)
        self._buffer_panel_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._buffer_panel_anim.valueChanged.connect(self._on_buffer_panel_anim_step)
        self._buffer_panel_anim.finished.connect(self._on_buffer_panel_anim_finished)
        # Start with buffer panel hidden
        self.buffer_widget.setVisible(False)

        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.addWidget(horizontal_splitter)
        main_splitter.addWidget(self.time_picker)
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)
        self._main_splitter = main_splitter
        self._timeline_anim = QVariantAnimation(self)
        self._timeline_anim.setDuration(170)
        self._timeline_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timeline_anim.valueChanged.connect(self._on_timeline_anim_step)
        self._timeline_expand_timer = QTimer(self)
        self._timeline_expand_timer.setSingleShot(True)
        self._timeline_expand_timer.setInterval(TIMELINE_EXPAND_DELAY_MS)
        self._timeline_expand_timer.timeout.connect(lambda: self._set_timeline_expanded(True))

        top_controls = QHBoxLayout()
        top_controls.addWidget(self.left_toggle, 0, Qt.AlignLeft)
        top_controls.addWidget(self.overview_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.fleetwide_search_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.current_system_label, 0, Qt.AlignLeft)
        self.calibrate_btn = QToolButton()
        self.calibrate_btn.setText("Calibrate")
        self.calibrate_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.calibrate_btn.clicked.connect(self._open_calibration_dialog)

        self.track_toggle = QToolButton()
        self.track_toggle.setText("Track")
        self.track_toggle.setCheckable(True)
        self.track_toggle.setChecked(True)
        self.track_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.track_toggle.toggled.connect(self._on_track_toggled)

        self.about_btn = QToolButton()
        self.about_btn.setText("About")
        self.about_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.about_btn.clicked.connect(self._open_about_dialog)

        top_controls.addStretch(1)
        top_controls.addWidget(self.calibrate_btn, 0, Qt.AlignRight)
        top_controls.addWidget(self.track_toggle, 0, Qt.AlignRight)
        top_controls.addWidget(self.buffer_toggle, 0, Qt.AlignRight)
        top_controls.addWidget(self.about_btn, 0, Qt.AlignRight)

        layout = QVBoxLayout()
        layout.addLayout(top_controls)
        layout.addWidget(main_splitter, 1)
        self.setLayout(layout)
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.date_picker.installEventFilter(self)
        self.time_picker.installEventFilter(self)
        if hasattr(self.time_picker, "view") and hasattr(self.time_picker.view, "viewport"):
            self.time_picker.view.viewport().installEventFilter(self)

        self.date_picker.date_selected.connect(self.on_date_selected)
        if hasattr(self.date_picker, "system_id_selected"):
            self.date_picker.system_id_selected.connect(self._set_system_id_override)
        # Settings button removed from DatePicker UI
        self.time_picker.time_selected.connect(self.on_time_chosen)
        self.time_picker.items_changed.connect(self._sync_viewer_sku_overlay)
        if ENABLE_DAY_PREFETCH:
            self.time_picker.items_changed.connect(self._prefetch_day_clips)
        if hasattr(self.viewer, "current_time_changed"):
            self.viewer.current_time_changed.connect(self.time_picker.set_playhead_datetime)
        if hasattr(self.viewer, "clip_range_export_requested"):
            self.viewer.clip_range_export_requested.connect(self._export_current_viewer_clip_range)
        if hasattr(self.viewer, "annotation_status_changed"):
            self.viewer.annotation_status_changed.connect(self.time_picker.mark_video_annotated)
        if hasattr(self.viewer, "cache_clip_ready"):
            self.viewer.cache_clip_ready.connect(self.time_picker.mark_video_cached)
        if hasattr(self.viewer, "current_time_changed"):
            self.viewer.current_time_changed.connect(self._on_playhead_for_buffer)
        if hasattr(self.viewer, "close_gap_threshold_changed"):
            self.viewer.close_gap_threshold_changed.connect(self._on_close_gap_threshold_changed)
        if hasattr(self.viewer, "set_export_target_overlay_provider"):
            self.viewer.set_export_target_overlay_provider(self._export_tracked_target_overlays)
        self.left_toggle.toggled.connect(lambda checked: self._set_date_picker_visible(checked, horizontal_splitter))

        # Apply last parent if available
        if self.settings.last_parent:
            p = Path(self.settings.last_parent)
            if p.exists():
                self.date_picker.set_parent_dir(p)
                self.overview_widget.set_parent_dir(p)
                self.fleetwide_search_widget.set_parent_dir(p)
        QTimer.singleShot(0, self._apply_initial_timeline_size)
        QTimer.singleShot(0, self._sync_overview_mode)
        QTimer.singleShot(0, lambda: self._on_track_toggled(self.track_toggle.isChecked()))

    def _open_about_dialog(self):
        from about_page import AboutDialog

        dlg = AboutDialog(self)
        dlg.exec()

    def _current_calibration_system_id(self) -> str:
        if self.system_id_override:
            return str(self.system_id_override)
        top_dir = getattr(self.date_picker, "top_dir", None)
        if isinstance(top_dir, Path):
            return str(top_dir.name)
        active_name = getattr(self.date_picker, "active_pikpak_name", None)
        if isinstance(active_name, str) and active_name and active_name != "__SIM__":
            return active_name
        return ""

    def showEvent(self, event):
        super().showEvent(event)
        if self._post_show_started:
            return
        self._post_show_started = True
        if hasattr(self.viewer, "start_background_maintenance"):
            QTimer.singleShot(0, self.viewer.start_background_maintenance)

    def _apply_initial_timeline_size(self):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        total = max(1, int(sum(sizes)))
        bottom = max(self._timeline_min_height, min(self._timeline_max_height, sizes[1]))
        if total > 1:
            bottom = min(bottom, total - 1)
        self._main_splitter.setSizes([max(1, total - bottom), bottom])

    def _build_static_tracks(self) -> list[tuple[str, str, str]]:
        static_tracks = [
            ("video", "Video", "#cce5ff"),
            ("additional", "Additional CCTV", "#9fb3c8"),
            ("sku", "SKU", "#8fd19e"),
        ]
        for idx, cond in enumerate(self.settings.conditions):
            kind = f"cond_{idx}"
            label = cond.name or f"Cond {idx+1}"
            color = cond.color or ""
            static_tracks.append((kind, label, color))
        return static_tracks

    def on_date_selected(self, pikpak_root: Path | None, day: date | None):
        if hasattr(self.viewer, "prepare_for_new_clip"):
            self.viewer.prepare_for_new_clip(show_loading=False)
        self.time_picker.show_times(pikpak_root, day)
        if hasattr(self.time_picker, "clear_clip_target_rate_heat"):
            self.time_picker.clear_clip_target_rate_heat()
        self._update_current_system_label(pikpak_root, day)
        if self.date_picker.parent_dir:
            self.overview_widget.set_parent_dir(self.date_picker.parent_dir)
        self.buffer_widget.clear()
        self._reload_calibration()

    def _update_current_system_label(self, pikpak_root: Path | None, day: date | None = None):
        if not isinstance(pikpak_root, Path):
            if self.system_id_override:
                self.current_system_label.setText(self.system_id_override)
            else:
                self.current_system_label.setText("")
            return
        system_name = pikpak_root.name
        customer = display_customer_name(self.settings, system_name)
        line = display_line_name(self.settings, system_name)
        parts = [customer]
        if line:
            parts.append(line)
        parts.append(system_name)
        self.current_system_label.setText(" / ".join([part for part in parts if part]))

    # ------------------------------------------------------------------
    # Pick-buffer panel
    # ------------------------------------------------------------------

    def _load_buffer_events(self, pikpak_root: Path | None, clip_start, clip_end) -> None:
        self._buffer_events = []
        self._buffer_clip_start = clip_start
        self._buffer_clip_end = clip_end
        self.buffer_widget.clear()
        if pikpak_root is None or clip_start is None or clip_end is None:
            return

        if self._buffer_loader_thread is not None:
            # Disconnect so stale results are ignored, but keep the Python
            # reference alive until the OS thread exits — dropping it while
            # the thread is still running causes "QThread destroyed while
            # running" crashes.  Wrap every signal op in try/except: if the
            # previous thread already finished and its C++ object was queued
            # for deletion, any signal call raises RuntimeError.
            try:
                self._buffer_loader_thread.done.disconnect(self._on_buffer_events_loaded)
            except RuntimeError:
                pass
            try:
                stale = self._buffer_loader_thread
                self._buffer_loader_refs.append(stale)
                stale.finished.connect(lambda t=stale: self._buffer_loader_refs.remove(t)
                                       if t in self._buffer_loader_refs else None)
            except RuntimeError:
                pass
            self._buffer_loader_thread = None

        loader = _BufferLoaderThread(self.settings, pikpak_root, clip_start, clip_end)
        loader.done.connect(self._on_buffer_events_loaded)
        loader.finished.connect(loader.deleteLater)
        self._buffer_loader_thread = loader
        print(f"[buffer] starting load for {pikpak_root}  {clip_start} → {clip_end}")
        loader.start()

    def _on_buffer_events_loaded(self, events: list) -> None:
        self._buffer_events = events
        self._close_gap_target_ids, self._wide_gap_target_ids = self._compute_gap_target_ids(events)
        self.buffer_widget.set_buffer_events(events)
        self.buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
        self.buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
        if (
            hasattr(self.time_picker, "set_clip_target_rate_heat")
            and self._buffer_clip_start is not None
            and self._buffer_clip_end is not None
        ):
            buckets = self._clip_target_rate_buckets_from_buffer_events(
                events,
                self._buffer_clip_start,
                self._buffer_clip_end,
            )
            self.time_picker.set_clip_target_rate_heat(self._buffer_clip_start, self._buffer_clip_end, buckets)
        print(f"[buffer] {len(events)} buffer state transitions loaded")
        if self._last_playhead_dt:
            if self._buffer_panel_visible:
                self.buffer_widget.update_for_time(self._last_playhead_dt)
            self._push_conveyor_overlays(self._last_playhead_dt)

    def _on_playhead_for_buffer(self, dt: datetime) -> None:
        self._last_playhead_dt = dt
        if self._buffer_panel_visible and self._buffer_events:
            self.buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
            self.buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
            self.buffer_widget.update_for_time(dt)
        self._push_conveyor_overlays(dt)
        if self._cal_dialog is not None:
            self._cal_dialog.on_time(dt)

    def _on_close_gap_threshold_changed(self, _value: float) -> None:
        if not self._buffer_events:
            return
        self._close_gap_target_ids, self._wide_gap_target_ids = self._compute_gap_target_ids(self._buffer_events)
        self.buffer_widget.set_alerted_target_ids(self._close_gap_target_ids)
        self.buffer_widget.set_wide_gap_target_ids(self._wide_gap_target_ids)
        if self._buffer_panel_visible and self._last_playhead_dt is not None:
            self.buffer_widget.update_for_time(self._last_playhead_dt)
        if self._last_playhead_dt is not None:
            self._push_conveyor_overlays(self._last_playhead_dt)

    def _set_buffer_panel_visible(self, visible: bool) -> None:
        self._buffer_panel_visible = bool(visible)
        if self._buffer_panel_visible:
            self.buffer_widget.setVisible(True)
            self._animate_buffer_panel(self._buffer_panel_target_width)
        else:
            self._animate_buffer_panel(0)

    def _animate_buffer_panel(self, end_width: int) -> None:
        splitter = self._horizontal_splitter
        sizes = splitter.sizes()
        current = sizes[2] if len(sizes) > 2 else 0
        if int(current) == int(end_width):
            if end_width == 0:
                self.buffer_widget.setVisible(False)
            return
        if self._buffer_panel_anim.state() == QVariantAnimation.Running:
            self._buffer_panel_anim.stop()
        self._buffer_panel_anim.setStartValue(int(current))
        self._buffer_panel_anim.setEndValue(int(end_width))
        self._buffer_panel_anim.start()

    def _on_buffer_panel_anim_step(self, value: int) -> None:
        try:
            right = max(0, int(value))
            splitter = self._horizontal_splitter
            sizes = splitter.sizes()
            if len(sizes) < 3:
                return
            total = sum(sizes)
            left = sizes[0]
            centre = max(1, total - left - right)
            splitter.setSizes([left, centre, right])
        except Exception:
            pass

    def _on_buffer_panel_anim_finished(self) -> None:
        if not self._buffer_panel_visible:
            self.buffer_widget.setVisible(False)

    # ------------------------------------------------------------------
    # Conveyor calibration
    # ------------------------------------------------------------------

    def _reload_calibration(self) -> None:
        sid = self._current_calibration_system_id()
        self._conveyor_cal = load_calibration(sid)
        print(f"[cal] loaded calibration for '{sid}', "
              f"{'tracking line ready' if self._conveyor_cal.has_tracking_line() else 'no tracking line'}")

    def _open_calibration_dialog(self) -> None:
        if self._cal_dialog is not None:
            self._cal_dialog.raise_()
            self._cal_dialog.activateWindow()
            return
        self._reload_calibration()
        dialog = ConveyorCalibrationDialog(self._conveyor_cal, parent=self)
        dialog.calibration_saved.connect(self._on_calibration_saved)
        dialog.finished.connect(self._on_cal_dialog_closed)

        # Feed dialog the current frame if available
        if hasattr(self.viewer, "video_label"):
            frame = getattr(self.viewer.video_label, "_frame", None)
            if frame is not None:
                dialog.on_frame(frame)
            # Live frame updates
            self.viewer.current_time_changed.connect(self._feed_cal_dialog_frame)

        # Feed current targets
        if self._buffer_events and self._last_playhead_dt:
            from target_buffer_loader import buffer_state_at
            targets, _ = buffer_state_at(self._buffer_events, self._last_playhead_dt)
            dialog.on_targets(targets)
        if self._last_playhead_dt:
            dialog.on_time(self._last_playhead_dt)

        self._cal_dialog = dialog
        dialog.show()

    def _feed_cal_dialog_frame(self, dt: datetime) -> None:
        if self._cal_dialog is None:
            return
        if hasattr(self.viewer, "video_label"):
            frame = getattr(self.viewer.video_label, "_frame", None)
            if frame is not None:
                self._cal_dialog.on_frame(frame)
        if self._buffer_events:
            from target_buffer_loader import buffer_state_at
            if dt.tzinfo is None:
                dt = dt.astimezone(timezone.utc)
            targets, _ = buffer_state_at(self._buffer_events, dt)
            self._cal_dialog.on_targets(targets)

    def _on_calibration_saved(self, cal: ConveyorCalibration) -> None:
        self._conveyor_cal = cal
        if self._last_playhead_dt:
            self._push_conveyor_overlays(self._last_playhead_dt)

    def _on_cal_dialog_closed(self) -> None:
        try:
            self.viewer.current_time_changed.disconnect(self._feed_cal_dialog_frame)
        except Exception:
            pass
        self._cal_dialog = None

    def _on_track_toggled(self, enabled: bool) -> None:
        self._tracking_enabled = enabled
        if enabled:
            if self._scope is not None:
                self._scope.hide()
            if self._last_playhead_dt:
                self._push_conveyor_overlays(self._last_playhead_dt)
        else:
            if self._scope is not None:
                self._scope.hide()
            if hasattr(self.viewer, "video_label"):
                self.viewer.video_label.set_target_overlays([])

    def _push_conveyor_overlays(self, dt: datetime) -> None:
        """Update the target scope panel, target panel, and line-tracked overlays."""
        if not self._tracking_enabled:
            return
        buffer_targets, _last_event = self._buffer_targets_for_time(dt)
        tracked_targets = self._visible_tracked_targets(buffer_targets, dt)
        if hasattr(self.viewer, "video_label"):
            self.viewer.video_label.set_target_overlays(self._tracked_target_overlays(tracked_targets, dt))
        self._last_targets = tracked_targets

    def _buffer_targets_for_time(self, dt: datetime) -> tuple[list, object | None]:
        if not self._buffer_events:
            return [], None
        from target_buffer_loader import buffer_state_at
        if dt.tzinfo is None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        targets, last_event = buffer_state_at(self._buffer_events, dt)
        return targets, last_event

    def _visible_tracked_targets(self, targets: list, dt: datetime) -> list:
        if not self._conveyor_cal.has_tracking_line():
            return []
        if dt.tzinfo is None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        visible = []
        for target in targets:
            age = (dt - target.added_at.astimezone(timezone.utc)).total_seconds()
            if self._conveyor_cal.tracking_position_for_age(age) is not None:
                visible.append(target)
        return visible

    def _tracked_target_overlays(self, targets: list, dt: datetime) -> list[dict]:
        if not self._conveyor_cal.has_tracking_line():
            return []
        if dt.tzinfo is None:
            dt = dt.astimezone(timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        overlays: list[dict] = []
        total = max(1, len(targets))
        for idx, target in enumerate(targets):
            age = (dt - target.added_at.astimezone(timezone.utc)).total_seconds()
            pos = self._conveyor_cal.tracking_position_for_age(age)
            if pos is None:
                continue
            pid = _display_target_id(target)
            opacity = min(1.0, 0.45 + ((idx + 1) / total) * 0.55)
            is_valid = bool(target.source_doc.get("valid", True))
            is_close_gap = target.target_id in self._close_gap_target_ids
            is_wide_gap = target.target_id in self._wide_gap_target_ids
            detail_lines = [f"#{pid}"]
            for key, value in _summary_rows(target.source_doc) + _detail_rows(target.source_doc):
                if key in {"Front corner", "Back corner"}:
                    continue
                detail_lines.append(f"{key}: {value}")
            if is_close_gap:
                detail_lines.append("Tight gap")
            elif is_wide_gap:
                detail_lines.append("Wide gap")
            overlays.append({
                "norm_x": pos[0],
                "norm_y": pos[1],
                "label": f"#{pid}",
                "info_lines": detail_lines,
                "color": "#e74c3c" if not is_valid else "#f39c12",
                "text_bg_color": "#5c2020" if not is_valid else ("#7a4800" if is_close_gap else ("#163a5a" if is_wide_gap else "#1f4d2e")),
                "opacity": opacity,
                "alert": is_close_gap,
            })
        return overlays

    def _compute_gap_target_ids(self, events: list) -> tuple[set[str], set[str]]:
        close_flagged: set[str] = set()
        wide_flagged: set[str] = set()
        add_times: list[float] = []
        last_add_time: float | None = None
        threshold = float(getattr(self.viewer, "close_gap_threshold", 0.5))
        for ev in events:
            if getattr(ev, "event_type", "") != "target_added":
                continue
            if not getattr(ev, "buffer_snapshot", None):
                continue
            target = ev.buffer_snapshot[-1]
            current_dt = ev.timestamp
            if current_dt.tzinfo is None:
                current_dt = current_dt.astimezone(timezone.utc)
            else:
                current_dt = current_dt.astimezone(timezone.utc)
            current_ts = current_dt.timestamp()
            add_times.append(current_ts)
            if last_add_time is None or len(add_times) < 2:
                last_add_time = current_ts
                continue
            left = bisect_left(add_times, current_ts - 60.0)
            window_count = len(add_times) - left
            if window_count >= 2:
                span = current_ts - add_times[left]
                if span > 0:
                    avg_gap = span / float(window_count - 1)
                    actual_gap = current_ts - last_add_time
                    if avg_gap > 0.0:
                        if actual_gap < (avg_gap * threshold):
                            close_flagged.add(target.target_id)
                        elif threshold > 0.0 and actual_gap > (avg_gap / threshold):
                            wide_flagged.add(target.target_id)
            last_add_time = current_ts
        return close_flagged, wide_flagged

    def _export_tracked_target_overlays(self, t_seconds: float) -> list[dict]:
        playback_dt = None
        drift_seconds = float(getattr(self.viewer, "time_offset", 0.0) or 0.0)
        if getattr(self.viewer, "video_start_dt", None) is not None and getattr(self.viewer, "fps", 0) > 0:
            adjusted_seconds = t_seconds + (self.viewer.ocr_frame_offset / self.viewer.fps)
            playback_dt = self.viewer.video_start_dt + timedelta(seconds=adjusted_seconds - drift_seconds)
        elif getattr(self.viewer, "current_video_filename_dt", None) is not None:
            playback_dt = self.viewer.current_video_filename_dt + timedelta(seconds=t_seconds - drift_seconds)
        if playback_dt is None:
            return []
        buffer_targets, _last_event = self._buffer_targets_for_time(playback_dt)
        tracked_targets = self._visible_tracked_targets(buffer_targets, playback_dt)
        return self._tracked_target_overlays(tracked_targets, playback_dt)

    @staticmethod
    def _choose_clip_target_rate_bucket_seconds(clip_start: datetime, clip_end: datetime) -> int:
        span_seconds = max(1.0, (ensure_utc(clip_end) - ensure_utc(clip_start)).total_seconds())
        raw = span_seconds / 240.0
        candidates = [1, 2, 5, 10, 15, 30, 60]
        for candidate in candidates:
            if raw <= candidate:
                return candidate
        return 60

    def _clip_target_rate_buckets_from_buffer_events(
        self,
        events: list,
        clip_start: datetime,
        clip_end: datetime,
    ) -> list[dict]:
        clip_start_utc = ensure_utc(clip_start)
        clip_end_utc = ensure_utc(clip_end)
        if clip_end_utc <= clip_start_utc:
            return []
        bucket_seconds = self._choose_clip_target_rate_bucket_seconds(clip_start_utc, clip_end_utc)
        span_seconds = (clip_end_utc - clip_start_utc).total_seconds()
        bucket_count = max(1, int((span_seconds + bucket_seconds - 1) // bucket_seconds))
        counts = [0] * bucket_count
        for ev in events:
            if getattr(ev, "event_type", "") != "target_added":
                continue
            ts = getattr(ev, "timestamp", None)
            if not isinstance(ts, datetime):
                continue
            ts = ensure_utc(ts)
            if ts < clip_start_utc or ts >= clip_end_utc:
                continue
            idx = int((ts - clip_start_utc).total_seconds() // bucket_seconds)
            if 0 <= idx < bucket_count:
                counts[idx] += 1
        buckets: list[dict] = []
        for idx, count in enumerate(counts):
            start = clip_start_utc + timedelta(seconds=idx * bucket_seconds)
            end = min(clip_end_utc, start + timedelta(seconds=bucket_seconds))
            buckets.append({
                "start": start,
                "end": end,
                "count": int(count),
            })
        return buckets

    # ------------------------------------------------------------------

    def _set_system_id_override(self, system_id: str | None):
        self.system_id_override = system_id or None
        set_system_id_override(self.system_id_override)
        self._reload_calibration()

    def _set_date_picker_visible(self, visible: bool, splitter: QSplitter):
        _ = splitter
        self._left_panel_visible = bool(visible)
        if self._left_panel_visible:
            self.left_toggle.setText("Hide Date Picker")
            if not self.date_picker.isVisible():
                # Ensure the panel starts collapsed, otherwise splitter may
                # restore old width instantly and skip visible animation.
                self.date_picker.setVisible(True)
                sizes = self._horizontal_splitter.sizes()
                total = max(1, sum(sizes) or self.width())
                buf = sizes[2] if len(sizes) > 2 else 0
                self._horizontal_splitter.setSizes([0, max(1, total - buf), buf])
            self._animate_left_panel(self._left_panel_target_width)
        else:
            self.left_toggle.setText("Show Date Picker")
            self._animate_left_panel(0)

    def _animate_left_panel(self, end_width: int):
        splitter = self._horizontal_splitter
        sizes = splitter.sizes()
        current = sizes[0] if sizes else (self._left_panel_target_width if self.date_picker.isVisible() else 0)
        if int(current) == int(end_width):
            if end_width == 0:
                self.date_picker.setVisible(False)
            return
        if self._left_panel_anim.state() == QVariantAnimation.Running:
            self._left_panel_anim.stop()
        self._left_panel_anim.setStartValue(int(current))
        self._left_panel_anim.setEndValue(int(end_width))
        self._left_panel_anim.start()

    def _on_left_panel_anim_step(self, value):
        try:
            left = max(0, int(value))
            splitter = self._horizontal_splitter
            sizes = splitter.sizes()
            total = sum(sizes) or max(self.width(), left + 1000)
            buf = sizes[2] if len(sizes) > 2 else 0
            centre = max(1, total - left - buf)
            if len(sizes) > 2:
                splitter.setSizes([left, centre, buf])
            else:
                splitter.setSizes([left, centre])
        except Exception:
            pass

    def _on_left_panel_anim_finished(self):
        if not self._left_panel_visible:
            self.date_picker.setVisible(False)

    def eventFilter(self, obj, event):
        if not self._hover_reveal_enabled:
            return super().eventFilter(obj, event)
        if event.type() == QEvent.MouseMove:
            if not self._left_panel_visible:
                pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                if pos.x() <= self._left_reveal_px:
                    self._set_date_picker_visible(True, self._horizontal_splitter)
            if self._is_timeline_obj(obj):
                self._schedule_timeline_expand()
        elif event.type() == QEvent.Leave and obj is self.date_picker:
            QTimer.singleShot(50, self._auto_hide_if_outside)
        elif event.type() == QEvent.Leave and self._is_timeline_obj(obj):
            self._cancel_timeline_expand()
            QTimer.singleShot(60, self._auto_contract_timeline)
        return super().eventFilter(obj, event)

    def _is_timeline_obj(self, obj) -> bool:
        if obj is self.time_picker:
            return True
        if hasattr(self.time_picker, "view") and hasattr(self.time_picker.view, "viewport"):
            return obj is self.time_picker.view.viewport()
        return False

    def _set_timeline_expanded(self, expanded: bool):
        expanded = bool(expanded)
        if expanded == self._timeline_expanded:
            return
        self._timeline_expanded = expanded
        target = self._timeline_max_height if expanded else self._timeline_min_height
        self._animate_timeline_height(target)

    def _animate_timeline_height(self, target_height: int):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        bottom_current = max(0, int(sizes[1]))
        total = max(1, int(sum(sizes)))
        target = max(self._timeline_min_height, min(self._timeline_max_height, int(target_height)))
        if total > 1:
            target = min(target, total - 1)
        if bottom_current == target:
            return
        if self._timeline_anim.state() == QVariantAnimation.Running:
            self._timeline_anim.stop()
        self._timeline_anim.setStartValue(bottom_current)
        self._timeline_anim.setEndValue(target)
        self._timeline_anim.start()

    def _on_timeline_anim_step(self, value):
        sizes = self._main_splitter.sizes()
        if len(sizes) < 2:
            return
        total = max(1, int(sum(sizes)))
        bottom = max(0, int(value))
        if total > 1:
            bottom = min(bottom, total - 1)
        top = max(1, total - bottom)
        self._main_splitter.setSizes([top, bottom])

    def _schedule_timeline_expand(self):
        if self._timeline_expanded:
            return
        if not self._timeline_expand_timer.isActive():
            self._timeline_expand_timer.start()

    def _cancel_timeline_expand(self):
        if self._timeline_expand_timer.isActive():
            self._timeline_expand_timer.stop()

    def _auto_contract_timeline(self):
        pos = self.time_picker.mapFromGlobal(self.cursor().pos())
        if self.time_picker.rect().contains(pos):
            return
        self._cancel_timeline_expand()
        self._set_timeline_expanded(False)

    def _find_horizontal_splitter(self) -> QSplitter:
        # The first splitter inside the vertical splitter is the horizontal one.
        for child in self.findChildren(QSplitter):
            if child.orientation() == Qt.Horizontal:
                return child
        return QSplitter(Qt.Horizontal)

    def _auto_hide_if_outside(self):
        if not self.date_picker.isVisible():
            return
        if getattr(self.date_picker, "active_day", None) is None:
            return
        # Hide if mouse is not over date picker anymore.
        pos = self.date_picker.mapFromGlobal(self.cursor().pos())
        if not self.date_picker.rect().contains(pos):
            self._set_date_picker_visible(False, self._horizontal_splitter)

    def closeEvent(self, event):
        # Qt delivers close events only to the top-level window: the panels'
        # own closeEvents never fire inside the app, so every worker thread
        # must be stopped from here or it races Qt teardown and crashes.
        thread = self._buffer_loader_thread
        self._buffer_loader_thread = None
        if thread is not None:
            try:
                thread.requestInterruption()
                thread.wait(3000)
            except RuntimeError:
                pass
        for shutdown in (
            self.date_picker.stop_scan_thread,
            self.time_picker.shutdown_workers,
            self.overview_widget.shutdown_workers,
            self.fleetwide_search_widget.shutdown_workers,
            self.viewer.shutdown_workers,
        ):
            try:
                shutdown()
            except Exception as exc:
                print(f"[main] shutdown step failed: {exc}", flush=True)
        super().closeEvent(event)

    def on_time_chosen(self, item: TimelineItem):
        if item.kind == "video" and isinstance(item.payload, Path):
            self.open_in_viewer(item)
        elif item.kind == "additional" and isinstance(item.payload, Path):
            if hasattr(self.time_picker, "clear_clip_target_rate_heat"):
                self.time_picker.clear_clip_target_rate_heat()
            self.load_additional_in_viewer(item.payload)
        else:
            if hasattr(self.time_picker, "clear_clip_target_rate_heat"):
                self.time_picker.clear_clip_target_rate_heat()
            QMessageBox.information(self, "Selected item", item.label)

    def open_in_viewer(self, item: TimelineItem):
        video_path = item.payload
        if not isinstance(video_path, Path) or not video_path.exists():
            QMessageBox.warning(self, "File not found", str(video_path))
            return
        t0 = time.perf_counter()
        print(f"[main] Opening video: {video_path}", flush=True)
        self.viewer.prepare_for_new_clip()
        if DEBUG_CLIP_TIMING:
            print(f"[main] prepare_for_new_clip took {time.perf_counter() - t0:.2f}s", flush=True)
        if not self.viewer.load_video_from_path(str(video_path)):
            return
        print("[main] Video loaded OK", flush=True)
        if DEBUG_CLIP_TIMING:
            print(f"[main] load_video_from_path total {time.perf_counter() - t0:.2f}s", flush=True)
        self._sync_viewer_sku_overlay()
        # Keep cache color updates, but avoid log marker updates while logs are disabled.
        # Keep cache colors in the timeline; do it off the critical path.
        if ENABLE_CACHE_COLOR_UPDATE:
            # Only update cached color in-place; avoid full timeline redraw.
            QTimer.singleShot(0, lambda: self.time_picker.mark_video_cached(video_path))
            if DEBUG_CLIP_TIMING:
                QTimer.singleShot(
                    0,
                    lambda: print(f"[main] timeline cache update at +{time.perf_counter() - t0:.2f}s", flush=True),
                )
        elif DEBUG_CLIP_TIMING:
            QTimer.singleShot(
                0,
                lambda: print(f"[main] timeline cache update skipped at +{time.perf_counter() - t0:.2f}s", flush=True),
            )
        if DEBUG_CLIP_TIMING:
            QTimer.singleShot(0, lambda: print(f"[main] UI tick +{time.perf_counter() - t0:.2f}s", flush=True))
            QTimer.singleShot(200, lambda: print(f"[main] UI tick +{time.perf_counter() - t0:.2f}s", flush=True))
        if ENABLE_EVENT_MARKERS:
            def _apply_markers():
                markers = self.time_picker.collect_event_markers(item)
                self.viewer.set_timeline_markers(markers)
                if hasattr(self.viewer, "set_clip_marker_fallback"):
                    self.viewer.set_clip_marker_fallback(markers)
                if DEBUG_CLIP_TIMING:
                    print(f"[main] timeline markers set at +{time.perf_counter() - t0:.2f}s", flush=True)
            QTimer.singleShot(0, _apply_markers)
        if ENABLE_PREFETCH_ADJACENT:
            QTimer.singleShot(0, lambda: self._prefetch_adjacent_clips(item))
        current_root = getattr(self.time_picker, "current_root", None)
        if current_root and item.start and item.end:
            self._load_buffer_events(current_root, item.start, item.end)
        elif hasattr(self.time_picker, "clear_clip_target_rate_heat"):
            self.time_picker.clear_clip_target_rate_heat()

        if ENABLE_LOG_BUTTON:
            current_root = getattr(self.time_picker, "current_root", None)
            if current_root and item.start and item.end:
                start_iso = item.start.isoformat()
                end_iso = (item.end + timedelta(minutes=1)).isoformat()
                if DEBUG_CLIP_TIMING:
                    print(f"[main] Logs pending for {start_iso} -> {end_iso}", flush=True)
                if hasattr(self.viewer, "set_pending_logs"):
                    self.viewer.set_pending_logs(str(current_root), start_iso, end_iso)

    def load_additional_in_viewer(self, video_path: Path):
        if not isinstance(video_path, Path) or not video_path.exists():
            QMessageBox.warning(self, "File not found", str(video_path))
            return
        if hasattr(self.viewer, "load_additional_cctv_from_path"):
            self.viewer.load_additional_cctv_from_path(video_path)

    def _prefetch_day_clips(self):
        if not hasattr(self.viewer, "prefetch_clips_to_cache"):
            return
        if not hasattr(self.time_picker, "video_paths"):
            return
        # items_changed fires several times while a day loads; coalesce.
        timer = getattr(self, "_day_prefetch_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(1000)
            timer.timeout.connect(self._run_day_prefetch)
            self._day_prefetch_timer = timer
        timer.start()

    def _run_day_prefetch(self):
        paths = self.time_picker.video_paths()
        if paths:
            # Stop downloading a previously viewed day before queueing this one.
            if hasattr(self.viewer, "cancel_queued_prefetches"):
                self.viewer.cancel_queued_prefetches()
            print(f"[main] day prefetch: queueing {len(paths)} clips", flush=True)
            self.viewer.prefetch_clips_to_cache(paths)

    def _prefetch_adjacent_clips(self, item: TimelineItem):
        if not hasattr(self.time_picker, "get_adjacent_video_items"):
            return
        if not hasattr(self.viewer, "prefetch_clips_to_cache"):
            return
        prev_item, next_item = self.time_picker.get_adjacent_video_items(item)
        paths: list[Path] = []
        if prev_item and isinstance(prev_item.payload, Path):
            paths.append(prev_item.payload)
        if next_item and isinstance(next_item.payload, Path):
            paths.append(next_item.payload)
        if paths:
            self.viewer.prefetch_clips_to_cache(paths)

    def _prefetch_overview_clips(self, paths: list[Path]):
        if not paths:
            return
        if not hasattr(self.viewer, "prefetch_clips_to_cache"):
            return
        self.viewer.prefetch_clips_to_cache(paths)

    def _sync_viewer_sku_overlay(self):
        if not hasattr(self.viewer, "set_sku_timeline_items"):
            return
        sku_items = [
            itm
            for itm in getattr(self.time_picker, "_items", [])
            if itm.kind == "sku" and itm.start is not None and itm.end is not None
        ]
        sku_items.sort(key=lambda itm: itm.start)
        self.viewer.set_sku_timeline_items(sku_items)

    def _load_additional_cctv_items(self, pikpak_root: Path, day: date, cache_root: Path | None):
        additional_root = pikpak_root / "AdditionalCCTV"
        paths = list(load_day_files(additional_root, day))
        if not paths:
            return []
        cache_index = _build_cache_index(cache_root) if cache_root else set()
        entries: list[tuple[Path, datetime]] = []
        for p in paths:
            parsed_dt = parse_time_from_name(p)
            if parsed_dt is not None:
                start_dt = parsed_dt
            else:
                try:
                    stat = p.stat()
                except FileNotFoundError:
                    continue
                start_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            entries.append((p, ensure_utc(start_dt)))
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
            cached = _is_path_cached(path_obj, cache_root, cache_index) if cache_root else False
            items.append(
                TimelineItem(
                    start=start_dt,
                    end=end_dt,
                    label=path_obj.name,
                    kind="additional",
                    color=VIDEO_COLOR_CACHED if cached else VIDEO_COLOR_UNCACHED,
                    payload=path_obj,
                    cached=cached,
                    path_key=_path_key(path_obj),
                    track_label="Additional CCTV",
                )
            )
        return items

    def open_stop_report(self):
        entries = self._build_stop_report_entries()
        if not entries:
            QMessageBox.information(self, "Stop Report", "No stop events found for this day.")
            return
        dlg = StopReportDialog(entries, self)
        dlg.open_requested.connect(self._open_report_entry)
        dlg.exec()

    def _open_report_entry(self, entry: StopReportEntry):
        if entry.video_item is None or entry.video_path is None:
            return
        self.open_in_viewer(entry.video_item)
        if hasattr(self.viewer, "seek_to_seconds"):
            self.viewer.seek_to_seconds(entry.seek_seconds, pause=True)

    def _build_stop_report_entries(self) -> list[StopReportEntry]:
        items = list(getattr(self.time_picker, "_items", []) or [])
        if not items:
            return []
        has_operator_stop_in_timeline = any(self._is_operator_stop_item(itm) for itm in items)
        video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
        video_items.sort(key=lambda i: i.start)
        sku_items = [itm for itm in items if itm.kind == "sku" and itm.start is not None and itm.end is not None]
        sku_items.sort(key=lambda i: i.start)
        stop_items: list[tuple[TimelineItem, str, dict]] = []
        required_paths: set[Path] = set()
        for itm in items:
            if itm.kind in ("video", "additional"):
                continue
            category = self._categorize_stop_event(itm)
            if category is None:
                continue
            src = {}
            if isinstance(itm.payload, dict):
                src_val = itm.payload.get("_source")
                if isinstance(src_val, dict):
                    src = src_val
            stop_items.append((itm, category, src))
            video_item = self._find_video_item_for_time(video_items, itm.start)
            if video_item and isinstance(video_item.payload, Path):
                required_paths.add(video_item.payload)
        if required_paths:
            self._cache_paths_for_report(sorted(required_paths, key=lambda p: str(p)))

        entries: list[StopReportEntry] = []
        thumb_cache: dict[tuple[str, int], QPixmap] = {}
        seen_keys: set[tuple[int, str]] = set()
        for itm, category, src in stop_items:
            state_name = str(src.get("state_name") or "").strip()
            source = str(src.get("source") or "").strip()
            video_item = self._find_video_item_for_time(video_items, itm.start)
            video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
            seek_seconds = 0.0
            if video_item is not None:
                seek_seconds = max(0.0, (itm.start - video_item.start).total_seconds())
            thumb = self._thumbnail_for_event(video_path, seek_seconds, itm.start, category, thumb_cache)
            key = (int(itm.start.timestamp()), state_name.lower() or category.lower())
            seen_keys.add(key)
            sku_info = self._sku_for_time(sku_items, itm.start)
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
            for ts, source, state_name, message in self._fetch_operator_stop_events():
                key = (int(ts.timestamp()), state_name.lower() or "operator_stop")
                if key in seen_keys:
                    continue
                video_item = self._find_video_item_for_time(video_items, ts)
                video_path = video_item.payload if (video_item and isinstance(video_item.payload, Path)) else None
                seek_seconds = 0.0
                if video_item is not None:
                    seek_seconds = max(0.0, (ts - video_item.start).total_seconds())
                thumb = self._thumbnail_for_event(video_path, seek_seconds, ts, "Operator Stop", thumb_cache)
                sku_info = self._sku_for_time(sku_items, ts)
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

    def _fetch_operator_stop_events(self) -> list[tuple[datetime, str, str, str]]:
        day = getattr(self.time_picker, "_current_date", None)
        if day is None:
            return []
        root = getattr(self.time_picker, "current_root", None)
        start_dt = local_day_start_utc(day)
        end_dt = local_day_end_utc(day)
        try:
            rows = fetch_logs_for_range(
                self.settings,
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

    @staticmethod
    def _is_operator_stop_item(item: TimelineItem) -> bool:
        payload = item.payload if isinstance(item.payload, dict) else {}
        src = payload.get("_source") if isinstance(payload.get("_source"), dict) else {}
        state_name = str(src.get("state_name") or "").strip().lower()
        source = str(src.get("source") or "").strip().lower()
        return state_name == "operator_stop" and "behaviour_node" in source

    def _cache_paths_for_report(self, paths: list[Path]):
        if not paths:
            return
        if not hasattr(self.viewer, "_cache_executor") or not hasattr(self.viewer, "_cache_path_for"):
            return
        if not hasattr(self.viewer, "_copy_to_cache"):
            return
        progress = QProgressDialog("Preparing report clips...", "Cancel", 0, 1, self)
        progress.setWindowTitle("Stop Report")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()
        futures = []
        executor = self.viewer._cache_executor
        for src_path in paths:
            if progress.wasCanceled():
                break
            try:
                cache_path = self.viewer._cache_path_for(src_path)
            except Exception:
                continue
            if cache_path.exists():
                continue
            futures.append(executor.submit(self.viewer._copy_to_cache, src_path, cache_path))
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

    @staticmethod
    def _find_video_item_for_time(video_items: list[TimelineItem], ts: datetime) -> TimelineItem | None:
        for itm in video_items:
            if itm.start <= ts < itm.end:
                return itm
        return None

    @staticmethod
    def _sku_for_time(sku_items: list[TimelineItem], ts: datetime) -> str:
        # Prefer an active SKU interval, then fall back to the most recent
        # known SKU at/just before the stop boundary.
        last_known_sku = ""
        for itm in sku_items:
            if itm.start is None or itm.end is None:
                continue
            payload = itm.payload if isinstance(itm.payload, dict) else {}
            is_manual = bool(payload.get("_ui_manual"))
            label = MainWindow._format_sku_label(itm)
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

    @staticmethod
    def _format_sku_label(item: TimelineItem) -> str:
        payload = item.payload if isinstance(item.payload, dict) else {}
        sku = str(payload.get("_ui_sku") or item.label or "").strip()
        tray = str(payload.get("_ui_tray") or "").strip()
        tool = str(payload.get("_ui_tool") or "").strip()
        parts = [p for p in (sku, tray, tool) if p]
        return " | ".join(parts) if parts else sku

    @staticmethod
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
        self,
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
        source_path = self._thumbnail_source_path(video_path)
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

    def _thumbnail_source_path(self, video_path: Path | None) -> Path | None:
        if video_path is None:
            return None
        # Avoid network reads in the UI thread: only use already-cached local files.
        cache_root = getattr(self.viewer, "cache_root", None)
        if cache_root is None:
            return None
        try:
            if hasattr(self.viewer, "get_valid_cached_path"):
                cached = self.viewer.get_valid_cached_path(video_path)
                if cached and isinstance(cached, Path):
                    return cached
            if hasattr(self.viewer, "_cache_path_for"):
                cached = self.viewer._cache_path_for(video_path)
                if cached and isinstance(cached, Path) and cached.exists():
                    return cached
        except Exception:
            return None
        return None

    def _export_timeline_clip_range(self, start_dt: datetime, end_dt: datetime):
        if end_dt <= start_dt:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        items = list(getattr(self.time_picker, "_items", []) or [])
        video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
        video_items.sort(key=lambda i: i.start)
        epsilon = timedelta(milliseconds=1)
        start_item = self._find_video_item_for_time(video_items, start_dt)
        end_lookup = end_dt - epsilon if end_dt > start_dt else end_dt
        end_item = self._find_video_item_for_time(video_items, end_lookup)
        if start_item is None or end_item is None:
            QMessageBox.information(self, "Export Clip", "The selected range is not fully covered by a video clip.")
            return
        if Path(start_item.payload) != Path(end_item.payload):
            QMessageBox.information(
                self,
                "Export Clip",
                "The selected range spans more than one clip. Please keep the export range within a single clip.",
            )
            return
        source_path = self._export_source_path(Path(start_item.payload))
        if source_path is None or not source_path.exists():
            QMessageBox.warning(self, "Export Clip", "Unable to access the source clip for export.")
            return
        clip_start_seconds = max(0.0, (start_dt - start_item.start).total_seconds())
        clip_duration_seconds = max(0.0, (end_dt - start_dt).total_seconds())
        if clip_duration_seconds <= 0.0:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        default_name = (
            f"{source_path.stem}_"
            f"{format_local_time(start_dt, '%H%M%S')}_"
            f"{format_local_time(end_dt, '%H%M%S')}.mp4"
        )
        target_path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Clip",
            str(source_path.with_name(default_name)),
            "MP4 Files (*.mp4);;All Files (*)",
        )
        if not target_path_str:
            return
        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            QMessageBox.warning(self, "Export Clip", "ffmpeg was not found on PATH.")
            return
        target_path = Path(target_path_str)
        cmd = [
            ffmpeg_path,
            "-y",
            "-ss",
            f"{clip_start_seconds:.3f}",
            "-i",
            str(source_path),
            "-t",
            f"{clip_duration_seconds:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target_path),
        ]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            QApplication.restoreOverrideCursor()
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            QMessageBox.warning(
                self,
                "Export Clip",
                f"Clip export failed.\n\n{stderr[:800] if stderr else 'ffmpeg returned an error.'}",
            )
            return
        QMessageBox.information(self, "Export Clip", f"Clip exported to:\n{target_path}")

    def _export_source_path(self, original_path: Path) -> Path | None:
        viewer_original = getattr(self.viewer, "current_video_original_path", None)
        viewer_loaded = getattr(self.viewer, "current_video_path", None)
        if viewer_original is not None and Path(viewer_original) == original_path and viewer_loaded:
            viewer_loaded_path = Path(viewer_loaded)
            if viewer_loaded_path.exists():
                return viewer_loaded_path
        try:
            if hasattr(self.viewer, "get_valid_cached_path"):
                cached = self.viewer.get_valid_cached_path(original_path)
                if cached and cached.exists():
                    return cached
            if hasattr(self.viewer, "_cache_path_for") and hasattr(self.viewer, "_ensure_cached_copy"):
                cache_path = self.viewer._cache_path_for(original_path)
                if self.viewer._ensure_cached_copy(original_path, cache_path) and cache_path.exists():
                    return cache_path
        except Exception:
            pass
        return original_path if original_path.exists() else None

    def _export_current_viewer_clip_range(self, start_seconds: float, end_seconds: float):
        if end_seconds <= start_seconds:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        viewer_original = getattr(self.viewer, "current_video_original_path", None)
        if viewer_original is None:
            QMessageBox.information(self, "Export Clip", "No video is currently loaded.")
            return
        source_path = self._export_source_path(Path(viewer_original))
        if source_path is None or not source_path.exists():
            QMessageBox.warning(self, "Export Clip", "Unable to access the source clip for export.")
            return
        start_seconds = max(0.0, float(start_seconds))
        end_seconds = max(start_seconds, float(end_seconds))
        clip_duration_seconds = end_seconds - start_seconds
        if clip_duration_seconds <= 0.0:
            QMessageBox.information(self, "Export Clip", "Select a non-zero clip range first.")
            return
        default_name = (
            f"{source_path.stem}_"
            f"{int(start_seconds // 3600):02d}{int((start_seconds % 3600) // 60):02d}{int(start_seconds % 60):02d}_"
            f"{int(end_seconds // 3600):02d}{int((end_seconds % 3600) // 60):02d}{int(end_seconds % 60):02d}.mp4"
        )
        target_path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Clip",
            str(source_path.with_name(default_name)),
            "MP4 Files (*.mp4);;All Files (*)",
        )
        if not target_path_str:
            return
        ffmpeg_path = shutil.which("ffmpeg")
        target_path = Path(target_path_str)
        if not hasattr(self.viewer, "export_current_clip_with_overlays"):
            QMessageBox.warning(self, "Export Clip", "Overlay export is not available in this build.")
            return
        ok, message = self.viewer.export_current_clip_with_overlays(
            source_path,
            start_seconds,
            end_seconds,
            target_path,
        )
        if not ok:
            QMessageBox.warning(self, "Export Clip", message or "Clip export failed.")
            return
        if message:
            QMessageBox.information(self, "Export Clip", f"Clip exported to:\n{target_path}\n\n{message}")
            return
        QMessageBox.information(self, "Export Clip", f"Clip exported to:\n{target_path}")

    def _reload_settings_from_viewer(self):
        self.settings = Settings.load()
        self.date_picker.set_system_layout_settings(self.settings)
        self.overview_widget.set_system_layout_settings(self.settings)
        self.fleetwide_search_widget.set_settings(self.settings)
        if hasattr(self.time_picker, "_static_tracks"):
            self.time_picker._static_tracks = self._build_static_tracks()
        current_parent = self.date_picker.parent_dir
        target_parent = Path(self.settings.last_parent) if self.settings.last_parent else None
        if target_parent and target_parent.exists():
            same_parent = False
            if current_parent is not None:
                try:
                    same_parent = current_parent.resolve() == target_parent.resolve()
                except Exception:
                    same_parent = current_parent == target_parent
            if same_parent:
                self.overview_widget.set_parent_dir(target_parent)
            else:
                self.date_picker.set_parent_dir(target_parent)
                self.overview_widget.set_parent_dir(target_parent)
            self.fleetwide_search_widget.set_parent_dir(target_parent)
        self.overview_widget.refresh_layout()

    def _sync_settings_from_fleetwide_search(self):
        # Keep the viewer's embedded settings panels on the same settings
        # object so a later autosave cannot overwrite fleetwide searches.
        self.viewer.settings = self.settings
        if hasattr(self.viewer, "settings_panel"):
            self.viewer.settings_panel.settings = self.settings
        if hasattr(self.viewer, "system_layout_panel"):
            self.viewer.system_layout_panel.settings = self.settings

    def _recheck_ocr_offset(self):
        if hasattr(self.viewer, "recheck_ocr_offset"):
            self.viewer.recheck_ocr_offset()

    def _should_show_overview(self) -> bool:
        return self.overview_btn.isChecked()

    def _on_overview_toggled(self, checked: bool):
        if checked and self.fleetwide_search_btn.isChecked():
            self.fleetwide_search_btn.setChecked(False)
        self._sync_overview_mode()

    def _on_fleetwide_search_toggled(self, checked: bool):
        if checked and self.overview_btn.isChecked():
            self.overview_btn.setChecked(False)
        self._sync_overview_mode()

    def _sync_overview_mode(self):
        show_overview = self._should_show_overview()
        show_fleetwide_search = self.fleetwide_search_btn.isChecked()
        if show_overview:
            current_page = self.overview_widget
        elif show_fleetwide_search:
            current_page = self.fleetwide_search_widget
        else:
            current_page = self.viewer
        self.content_stack.setCurrentWidget(current_page)
        self.overview_widget.set_parent_dir(self.date_picker.parent_dir)
        self.overview_widget.activate(show_overview)
        self.fleetwide_search_widget.set_parent_dir(self.date_picker.parent_dir)
        self.fleetwide_search_widget.activate(show_fleetwide_search)
        if show_overview or show_fleetwide_search:
            self._hover_reveal_enabled = False
            self._cancel_timeline_expand()
            self._timeline_expanded = False
            self.viewer.setMinimumSize(320, 120)
            self.content_stack.setMinimumWidth(320)
            self.date_picker.setVisible(False)
            self.time_picker.setVisible(False)
            self._horizontal_splitter.setSizes([0, max(1, sum(self._horizontal_splitter.sizes()) or self.width())])
            self._main_splitter.setSizes([max(1, sum(self._main_splitter.sizes()) or self.height()), 0])
        else:
            self._hover_reveal_enabled = True
            self.viewer.setMinimumSize(980, 120)
            self.content_stack.setMinimumWidth(980)
            if self.left_toggle.isChecked():
                self.date_picker.setVisible(True)
                self._animate_left_panel(self._left_panel_target_width)
            self.time_picker.setVisible(True)
            self._apply_initial_timeline_size()

    def _open_system_from_overview(self, pikpak_root: Path | None, selected_day: date | None, target_dt: datetime | None = None):
        if not isinstance(pikpak_root, Path) or selected_day is None:
            return
        self.overview_btn.setChecked(False)
        if isinstance(target_dt, datetime):
            if target_dt.tzinfo is None:
                target_dt = target_dt.replace(tzinfo=timezone.utc)
            else:
                target_dt = target_dt.astimezone(timezone.utc)
            self._pending_overview_navigation = {
                "root": pikpak_root,
                "day": selected_day,
                "target_dt": target_dt,
                "stage": "load_timeline",
                "attempts": 0,
                "max_attempts": 400,
                "sync_forced": False,
            }
            self._overview_nav_timer.start()
        else:
            self._pending_overview_navigation = None
            self._overview_nav_timer.stop()
        if hasattr(self.date_picker, "select_pikpak_folder_and_day"):
            self.date_picker.select_pikpak_folder_and_day(pikpak_root, selected_day)
        else:
            self.date_picker.use_pikpak_folder(pikpak_root)
            self.on_date_selected(pikpak_root, selected_day)

    def _continue_overview_navigation(self):
        pending = self._pending_overview_navigation
        if not pending:
            self._overview_nav_timer.stop()
            return
        pending["attempts"] = int(pending.get("attempts", 0)) + 1
        max_attempts = max(1, int(pending.get("max_attempts", 400)))
        if pending["attempts"] > max_attempts:
            self._pending_overview_navigation = None
            self._overview_nav_timer.stop()
            return
        target_root = pending.get("root")
        target_day = pending.get("day")
        target_dt = pending.get("target_dt")
        if not isinstance(target_root, Path) or not isinstance(target_day, date) or not isinstance(target_dt, datetime):
            self._pending_overview_navigation = None
            self._overview_nav_timer.stop()
            return

        if pending.get("stage") == "load_timeline":
            if getattr(self.time_picker, "current_root", None) != target_root:
                return
            if getattr(self.time_picker, "_current_date", None) != target_day:
                return
            if getattr(self.time_picker, "_loader_thread", None) is not None:
                return
            items = list(getattr(self.time_picker, "_items", []) or [])
            video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
            if not video_items:
                return
            clip_item = None
            previous_item = None
            for itm in video_items:
                if itm.start <= target_dt < itm.end:
                    clip_item = itm
                    break
                if itm.start <= target_dt:
                    previous_item = itm
                elif target_dt < itm.start:
                    clip_item = previous_item or itm
                    break
            if clip_item is None:
                clip_item = previous_item or (video_items[0] if video_items else None)
            if clip_item is None:
                self._pending_overview_navigation = None
                self._overview_nav_timer.stop()
                return
            pending["clip_item"] = clip_item
            self.open_in_viewer(clip_item)
            viewer_path = getattr(self.viewer, "current_video_original_path", None)
            viewer_cap = getattr(self.viewer, "cap", None)
            if viewer_cap is None or viewer_path is None or Path(viewer_path) != Path(clip_item.payload):
                return
            pending["stage"] = "sync_and_seek"
            pending["attempts"] = 0
            pending["max_attempts"] = 200
            return

        clip_item = pending.get("clip_item")
        if not isinstance(clip_item, TimelineItem):
            self._pending_overview_navigation = None
            self._overview_nav_timer.stop()
            return
        viewer_path = getattr(self.viewer, "current_video_original_path", None)
        viewer_cap = getattr(self.viewer, "cap", None)
        if viewer_cap is None or viewer_path is None or Path(viewer_path) != Path(clip_item.payload):
            return
        clip_start_dt = clip_item.start
        if clip_start_dt.tzinfo is None:
            clip_start_dt = clip_start_dt.replace(tzinfo=timezone.utc)
        else:
            clip_start_dt = clip_start_dt.astimezone(timezone.utc)
        clip_end_dt = clip_item.end
        if clip_end_dt.tzinfo is None:
            clip_end_dt = clip_end_dt.replace(tzinfo=timezone.utc)
        else:
            clip_end_dt = clip_end_dt.astimezone(timezone.utc)
        seek_seconds = (target_dt - clip_start_dt).total_seconds()
        clip_duration_seconds = max(0.0, (clip_end_dt - clip_start_dt).total_seconds())
        if clip_duration_seconds > 0.0:
            seek_seconds = min(seek_seconds, clip_duration_seconds)
        seek_seconds = max(0.0, seek_seconds)
        if not pending.get("sync_forced") and hasattr(self.viewer, "_auto_sync_with_ocr"):
            pending["sync_forced"] = True
            try:
                self.viewer._auto_sync_with_ocr(force=True)
            except Exception:
                pass
        if hasattr(self.viewer, "seek_to_seconds"):
            self.viewer.seek_to_seconds(seek_seconds, pause=True)
        self._pending_overview_navigation = None
        self._overview_nav_timer.stop()


def main():
    app = QApplication(sys.argv)
    icon_path = _resolve_asset_path("logfather.ico")
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    splash = _build_splash_image()
    if splash is not None:
        splash.show()
        app.processEvents()
    win = MainWindow()
    win.resize(1400, 700)
    if splash is not None:
        win.show()
        if hasattr(splash, "fade_and_finish"):
            splash.fade_and_finish(win)
        else:
            splash.finish(win)
    else:
        win.show()
    sys.exit(app.exec())

def _build_splash_image() -> QSplashScreen | None:
    try:
        image_path = _resolve_asset_path(SPLASH_IMAGE_FILENAME)
        if not image_path:
            return None
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            return None
        scaled = pixmap.scaled(
            max(1, int(pixmap.width() * 0.33)),
            max(1, int(pixmap.height() * 0.33)),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        framed = QPixmap(scaled.width() + 4, scaled.height() + 4)
        framed.fill(QColor("#10151a"))
        painter = QPainter(framed)
        painter.drawPixmap(2, 2, scaled)
        pen = QPen(QColor("#4a5560"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(1, 1, framed.width() - 3, framed.height() - 3)
        painter.end()
        splash = FadeSplashScreen(framed, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        splash.setFont(QFont("", 10))
        splash.showMessage(
            f"Loading...  {format_version_label()}",
            Qt.AlignBottom | Qt.AlignHCenter,
            QColor(255, 255, 255, 210),
        )
        return splash
    except Exception:
        return None


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


if __name__ == "__main__":
    main()
