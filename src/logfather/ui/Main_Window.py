import shutil
import time
from dataclasses import asdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QEvent, QVariantAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
    QSplitter,
    QToolButton,
    QSizePolicy,
    QPushButton,
    QLabel,
    QStackedWidget,
    QFileDialog,
    QProgressBar,
    QProgressDialog,
)

from logfather.ui import theme
from logfather.ui.Date_Picker_frontend import DatePicker
from logfather.ui.Time_Picker import (
    TimePicker,
    TimelineItem,
    parse_time_from_name,
    ensure_utc,
    ensure_playhead_local,
    MIN_BLOCK_DURATION,
    inferred_live_clip_end,
    VIDEO_COLOR_CACHED,
    VIDEO_COLOR_UNCACHED,
    _is_path_cached,
    _build_cache_index,
    _path_key,
)
from logfather.core.app_version import load_version_info
from logfather.data.day_listing_cache import load_day_files_cached
from logfather.data.elastic_loader import fetch_events, set_system_id_override
from logfather.ui.qt_worker import JobSlot
from logfather.ui.stop_report import (
    StopReportDialog,
    StopReportEntry,
    build_stop_report_entries,
    collect_stop_report_data,
)
from logfather.ui.target_overlay_controller import TargetOverlayController
from logfather.data.settings_store import Settings, display_customer_name, display_line_name
from logfather.ui.Log_vid_gui import VideoLogViewer
from logfather.ui.overview_widget import OverviewWidget
from logfather.ui.fleetwide_elastic_search_widget import FleetwideElasticSearchWidget
from logfather.ui.target_buffer_widget import TargetBufferWidget


DEBUG_CLIP_TIMING = True
ENABLE_CACHE_COLOR_UPDATE = True
ENABLE_EVENT_MARKERS = True
ENABLE_PREFETCH_ADJACENT = True
# Day-wide prefetch is off: HiDrive copies share the internet connection with
# Elastic Cloud, and saturating it made every timeline/log fetch crawl.
ENABLE_DAY_PREFETCH = False
ENABLE_LOG_BUTTON = True
TIMELINE_MIN_HEIGHT = 165
TIMELINE_MAX_HEIGHT = 360
TIMELINE_EXPAND_DELAY_MS = 1500


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        # Version in the title so it's always obvious which build is being
        # tested (Chris, 2026-09-03). Dev runs derive it live from git
        # (0.<commit count>); frozen builds read the stamped version.json.
        version = str(load_version_info().get("version") or "dev")
        self.setWindowTitle(f"The Logfather - version {version}")
        self._post_show_started = False
        self._shutdown_in_progress = False

        self.system_id_override: str | None = None
        self.settings = Settings.load()
        self._last_layout_snapshot = self._layout_settings_snapshot(self.settings)
        if self.settings.load_warning:
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    self, "Settings recovered", self.settings.load_warning
                ),
            )
        # Construction is split into ordered sections (Stage 3); later
        # sections consume attributes from earlier ones.
        self._build_panels()
        self._build_splitters()
        self._build_top_bar_and_layout()
        self._wire_signals()

    def _build_panels(self):
        """The five content panels, their workers, and the overlay
        controller: viewer, overview, date/time pickers, fleetwide,
        target-buffer."""
        self.viewer = VideoLogViewer()
        cache_root = self.viewer.cache_root
        self.overview_widget = OverviewWidget(
            self.settings,
            cache_root=cache_root,
            prefetch_clips=self._prefetch_overview_clips,
            parent=self,
        )
        self.overview_widget.open_requested.connect(self._open_system_from_overview)
        self._pending_overview_navigation: dict | None = None
        # Failsafe: drop a navigation that never completes (e.g. the target
        # day turns out to have no clips) so it can't fire much later.
        self._overview_nav_failsafe = QTimer(self)
        self._overview_nav_failsafe.setSingleShot(True)
        self._overview_nav_failsafe.setInterval(120_000)
        self._overview_nav_failsafe.timeout.connect(self._cancel_overview_navigation)
        self.viewer.settings_saved.connect(self._reload_settings_from_viewer)
        # Allow timeline expansion in non-maximised windows by reducing
        # the viewer's minimum height constraint.
        self.viewer.setMinimumSize(980, 120)
        self.date_picker = DatePicker()
        self.date_picker.set_system_layout_settings(self.settings)
        # Build static tracks: video + additional + condition rows
        static_tracks = self._build_static_tracks()

        # Extra loaders: Elastic events + additional CCTV clips. The third
        # argument is the day's last-video-end from the timeline scan, so
        # the SKU fetch doesn't re-list the share.
        extra_loaders = [
            lambda root, day, last_video_end: fetch_events(
                self.settings, root, day, last_video_end=last_video_end
            ),
            lambda root, day, last_video_end: self._load_additional_cctv_items(root, day, cache_root),
        ]

        self.time_picker = TimePicker(
            load_day_files_cached,
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
        self._stop_report_slot = JobSlot(self)
        self._stop_report_progress = None
        self.overview_btn = QToolButton()
        self.overview_btn.setText("Overview")
        self.overview_btn.setCheckable(True)
        self.overview_btn.toggled.connect(self._on_overview_toggled)
        self.fleetwide_search_btn = QToolButton()
        self.fleetwide_search_btn.setText("Fleetwide Search")
        self.fleetwide_search_btn.setCheckable(True)
        self.fleetwide_search_btn.toggled.connect(self._on_fleetwide_search_toggled)
        self.current_system_label = QLabel("")
        self.current_system_label.setStyleSheet(theme.TOP_BAR_LABEL)
        self.current_system_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        self._day_prefetch_timer: QTimer | None = None
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setInterval(60_000)
        self._session_save_timer.timeout.connect(self._save_last_session)
        self._session_save_timer.start()
        # Buffer events, gap classification, calibration and per-frame
        # overlays live in the controller.
        self._overlay_controller = TargetOverlayController(
            viewer=self.viewer,
            buffer_widget=self.buffer_widget,
            time_picker=self.time_picker,
            settings_provider=lambda: self.settings,
            calibration_system_id_provider=self._current_calibration_system_id,
            parent_widget=self,
        )

    def _build_splitters(self):
        """Panel toggles, the horizontal/vertical splitters, and their
        show/hide animations."""
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

    def _build_top_bar_and_layout(self):
        """Top control strip (view toggles left, tool buttons right) and the
        root layout; installs the hover-reveal event filters."""
        top_controls = QHBoxLayout()
        top_controls.addWidget(self.left_toggle, 0, Qt.AlignLeft)
        top_controls.addWidget(self.overview_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.fleetwide_search_btn, 0, Qt.AlignLeft)
        top_controls.addWidget(self.current_system_label, 0, Qt.AlignLeft)
        self.calibrate_btn = QToolButton()
        self.calibrate_btn.setText("Calibrate")
        self.calibrate_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.calibrate_btn.clicked.connect(self._overlay_controller.open_calibration_dialog)

        self.track_toggle = QToolButton()
        self.track_toggle.setText("Track")
        self.track_toggle.setCheckable(True)
        self.track_toggle.setChecked(True)
        self.track_toggle.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.track_toggle.toggled.connect(self._overlay_controller.set_tracking_enabled)

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
        layout.addWidget(self._main_splitter, 1)
        self.setLayout(layout)
        self.setMouseTracking(True)
        self.installEventFilter(self)
        self.date_picker.installEventFilter(self)
        self.time_picker.installEventFilter(self)
        self.time_picker.view.viewport().installEventFilter(self)

    def _wire_signals(self):
        """Cross-panel signal wiring and the deferred startup steps."""
        self.date_picker.date_selected.connect(self.on_date_selected)
        self.date_picker.system_id_selected.connect(self._set_system_id_override)
        # Settings button removed from DatePicker UI
        self.time_picker.time_selected.connect(self.on_time_chosen)
        self.time_picker.items_changed.connect(self._sync_viewer_sku_overlay)
        self.time_picker.items_changed.connect(self._on_items_changed_for_navigation)
        self.viewer.clip_opened.connect(self._on_clip_opened_for_navigation)
        if ENABLE_DAY_PREFETCH:
            self.time_picker.items_changed.connect(self._prefetch_day_clips)
        self.viewer.current_time_changed.connect(self.time_picker.set_playhead_datetime)
        self.viewer.clip_range_export_requested.connect(self._export_current_viewer_clip_range)
        self.viewer.annotation_status_changed.connect(self.time_picker.mark_video_annotated)
        self.viewer.cache_clip_ready.connect(self.time_picker.mark_video_cached)
        self.viewer.current_time_changed.connect(self._overlay_controller.on_playhead)
        self.viewer.close_gap_threshold_changed.connect(
            self._overlay_controller.on_close_gap_threshold_changed
        )
        self.viewer.set_export_target_overlay_provider(
            self._overlay_controller.export_overlays_for
        )
        self.left_toggle.toggled.connect(
            lambda checked: self._set_date_picker_visible(checked, self._horizontal_splitter)
        )

        # Apply last parent if available
        if self.settings.last_parent:
            p = Path(self.settings.last_parent)
            if p.exists():
                self.date_picker.set_parent_dir(p)
                self.overview_widget.set_parent_dir(p)
                self.fleetwide_search_widget.set_parent_dir(p)
        QTimer.singleShot(0, self._apply_initial_timeline_size)
        QTimer.singleShot(600, self._maybe_resume_last_session)
        QTimer.singleShot(0, self._sync_overview_mode)
        QTimer.singleShot(
            0,
            lambda: self._overlay_controller.set_tracking_enabled(self.track_toggle.isChecked()),
        )

    def _open_about_dialog(self):
        from logfather.ui.about_page import AboutDialog

        dlg = AboutDialog(self)
        dlg.exec()

    def _current_calibration_system_id(self) -> str:
        if self.system_id_override:
            return str(self.system_id_override)
        top_dir = self.date_picker.top_dir
        if isinstance(top_dir, Path):
            return str(top_dir.name)
        active_name = self.date_picker.active_pikpak_name
        if isinstance(active_name, str) and active_name and active_name != "__SIM__":
            return active_name
        return ""

    def showEvent(self, event):
        super().showEvent(event)
        if self._post_show_started:
            return
        self._post_show_started = True
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
        self.viewer.prepare_for_new_clip(show_loading=False)
        self.time_picker.show_times(pikpak_root, day)
        self.time_picker.clear_clip_target_rate_heat()
        self._update_current_system_label(pikpak_root, day)
        if self.date_picker.parent_dir:
            self.overview_widget.set_parent_dir(self.date_picker.parent_dir)
        self._overlay_controller.clear()
        self._overlay_controller.reload_calibration()

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

    def _set_buffer_panel_visible(self, visible: bool) -> None:
        self._buffer_panel_visible = bool(visible)
        self._overlay_controller.panel_visible = self._buffer_panel_visible
        if self._buffer_panel_visible and self._overlay_controller._last_playhead_dt:
            self.buffer_widget.update_for_time(self._overlay_controller._last_playhead_dt)
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

    def _set_system_id_override(self, system_id: str | None):
        self.system_id_override = system_id or None
        set_system_id_override(self.system_id_override)
        self._overlay_controller.reload_calibration()

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
        return obj is self.time_picker.view.viewport()

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


    def _auto_hide_if_outside(self):
        if not self.date_picker.isVisible():
            return
        if self.date_picker.active_day is None:
            return
        # Hide if mouse is not over date picker anymore.
        pos = self.date_picker.mapFromGlobal(self.cursor().pos())
        if not self.date_picker.rect().contains(pos):
            self._set_date_picker_visible(False, self._horizontal_splitter)

    def _save_last_session(self, playhead_override: datetime | None = None) -> None:
        """Remember system/day/playhead so startup can offer to resume.

        Saved on every clip open and once a minute (not just at close), so a
        killed process still resumes close to where the user was."""
        root = self.time_picker.current_root
        day = self.time_picker._current_date
        if root is None or day is None:
            return
        playhead = (
            playhead_override
            if playhead_override is not None
            else self._overlay_controller._last_playhead_dt
        )
        playhead_iso = None
        if isinstance(playhead, datetime):
            playhead_iso = ensure_playhead_local(playhead).astimezone(timezone.utc).isoformat()
        self.settings.last_session = {
            "root": str(root),
            "day": day.isoformat(),
            "playhead": playhead_iso,
        }
        self.settings.save()
        print(f"[main] session saved: {root.name} {day.isoformat()} @ {playhead_iso}", flush=True)

    def _maybe_resume_last_session(self) -> None:
        # Always asks; there is deliberately no "remember my choice"
        # (Chris, 2026-09-03 — a remembered "never" was too easy to
        # set once and impossible to discover later).
        session = self.settings.last_session
        if not isinstance(session, dict):
            return
        try:
            root = Path(str(session.get("root")))
            day = date.fromisoformat(str(session.get("day")))
        except Exception:
            return
        target_dt = None
        playhead_raw = session.get("playhead")
        if isinstance(playhead_raw, str):
            try:
                target_dt = datetime.fromisoformat(playhead_raw)
            except ValueError:
                target_dt = None
        if not root.exists():
            return
        when = (
            target_dt.astimezone().strftime("%H:%M:%S")
            if target_dt is not None
            else "start of day"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Resume session")
        box.setText(
            "Resume where you left off?\n\n"
            f"{root.name} on {day.strftime('%d/%m/%Y')} at {when}"
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        # Reuse the signal-driven jump: select system+day, open the clip
        # containing the playhead moment, sync and seek once it is open.
        self._open_system_from_overview(root, day, target_dt)

    def closeEvent(self, event):
        # Qt delivers close events only to the top-level window: the panels'
        # own closeEvents never fire inside the app, so every worker thread
        # must be stopped from here or it races Qt teardown and crashes.
        # A small always-on-top popup narrates the steps (Chris, 2026-09-03:
        # closing could take seconds with no sign anything was happening),
        # and each step's duration is printed so slow ones are attributable.
        if getattr(self, "_shutdown_in_progress", False):
            event.accept()
            return
        self._shutdown_in_progress = True
        t_shutdown = time.perf_counter()
        popup = QWidget(
            None,
            Qt.SplashScreen | Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint,
        )
        popup.setStyleSheet(theme.SHUTDOWN_POPUP)
        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(24, 18, 24, 18)
        popup_title = QLabel("Shutting down...")
        popup_title.setStyleSheet(theme.POPUP_TITLE)
        popup_step = QLabel("")
        popup_step.setStyleSheet(theme.POPUP_STEP)

        steps = (
            ("Stopping target-overlay worker", self._overlay_controller.shutdown),
            ("Stopping stop-report worker", self._stop_report_slot.shutdown),
            ("Stopping date scan", self.date_picker.stop_scan_thread),
            ("Stopping timeline loader", self.time_picker.shutdown_workers),
            ("Stopping overview loader", self.overview_widget.shutdown_workers),
            ("Stopping fleetwide search", self.fleetwide_search_widget.shutdown_workers),
            ("Saving settings, stopping viewer workers", self.viewer.shutdown_workers),
        )
        popup_bar = QProgressBar()
        popup_bar.setRange(0, len(steps) + 1)
        popup_bar.setTextVisible(False)
        popup_layout.addWidget(popup_title)
        popup_layout.addWidget(popup_step)
        popup_layout.addWidget(popup_bar)
        popup.setMinimumWidth(360)

        def _step(label: str, done: int):
            popup_step.setText(label)
            popup_bar.setValue(done)
            popup.show()
            QApplication.processEvents()

        for done, (label, shutdown) in enumerate(steps):
            _step(label, done)
            t0 = time.perf_counter()
            try:
                shutdown()
            except Exception as exc:
                print(f"[main] shutdown step failed: {exc}", flush=True)
            dt_ms = (time.perf_counter() - t0) * 1000
            if dt_ms > 100:
                print(f"[shutdown] '{label}' took {dt_ms:.0f}ms", flush=True)
        _step("Saving session", len(steps))
        # Geometry capture and session save must come AFTER
        # viewer.shutdown_workers: its settings flush emits settings_saved,
        # which makes _reload_settings_from_viewer REPLACE self.settings —
        # anything written onto the old object before that point is lost.
        # normalGeometry() when maximized, so un-maximizing after a restart
        # returns to a sensible size instead of the full-screen rect.
        try:
            geo = self.normalGeometry() if self.isMaximized() else self.geometry()
            self.settings.window_geometry = {
                "x": geo.x(),
                "y": geo.y(),
                "w": geo.width(),
                "h": geo.height(),
                "maximized": bool(self.isMaximized()),
            }
        except Exception:
            pass
        self._save_last_session()
        # _save_last_session early-returns without saving when nothing was
        # open; the window geometry must persist regardless.
        try:
            self.settings.save()
        except Exception:
            pass
        popup_bar.setValue(len(steps) + 1)
        print(
            f"[shutdown] total {(time.perf_counter() - t_shutdown) * 1000:.0f}ms",
            flush=True,
        )
        popup.close()
        super().closeEvent(event)

    def on_time_chosen(self, item: TimelineItem):
        if item.kind == "video" and isinstance(item.payload, Path):
            self.open_in_viewer(item)
        elif item.kind == "additional" and isinstance(item.payload, Path):
            self.time_picker.clear_clip_target_rate_heat()
            self.load_additional_in_viewer(item.payload)
        else:
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
                self.viewer.set_clip_marker_fallback(markers)
                if DEBUG_CLIP_TIMING:
                    print(f"[main] timeline markers set at +{time.perf_counter() - t0:.2f}s", flush=True)
            QTimer.singleShot(0, _apply_markers)
        if ENABLE_PREFETCH_ADJACENT:
            QTimer.singleShot(0, lambda: self._prefetch_adjacent_clips(item))
        if item.start is not None:
            self._save_last_session(playhead_override=item.start)
        current_root = self.time_picker.current_root
        if current_root and item.start and item.end:
            self._overlay_controller.load_buffer_events(current_root, item.start, item.end)
        else:
            self.time_picker.clear_clip_target_rate_heat()

        if ENABLE_LOG_BUTTON:
            current_root = self.time_picker.current_root
            if current_root and item.start and item.end:
                start_iso = item.start.isoformat()
                end_iso = (item.end + timedelta(minutes=1)).isoformat()
                if DEBUG_CLIP_TIMING:
                    print(f"[main] Logs pending for {start_iso} -> {end_iso}", flush=True)
                self.viewer.set_pending_logs(str(current_root), start_iso, end_iso)

    def load_additional_in_viewer(self, video_path: Path):
        if not isinstance(video_path, Path) or not video_path.exists():
            QMessageBox.warning(self, "File not found", str(video_path))
            return
        self.viewer.load_additional_cctv_from_path(video_path)

    def _prefetch_day_clips(self):
        # items_changed fires several times while a day loads; coalesce.
        timer = self._day_prefetch_timer
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
            self.viewer.cancel_queued_prefetches()
            print(f"[main] day prefetch: queueing {len(paths)} clips", flush=True)
            self.viewer.prefetch_clips_to_cache(paths)

    def _prefetch_adjacent_clips(self, item: TimelineItem):
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
        self.viewer.prefetch_clips_to_cache(paths)

    def _sync_viewer_sku_overlay(self):
        sku_items = [
            itm
            for itm in self.time_picker._items
            if itm.kind == "sku" and itm.start is not None and itm.end is not None
        ]
        sku_items.sort(key=lambda itm: itm.start)
        self.viewer.set_sku_timeline_items(sku_items)

    def _load_additional_cctv_items(self, pikpak_root: Path, day: date, cache_root: Path | None):
        additional_root = pikpak_root / "AdditionalCCTV"
        paths = list(load_day_files_cached(additional_root, day))
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
        # The collect phase (Elastic fallback fetch, SMB clip copies, cv2
        # thumbnail decodes) runs on a worker; only the QPixmap conversion
        # and the dialog happen here. Starting a new build retires a
        # running one (JobSlot semantics).
        items = list(self.time_picker._items or [])
        settings = self.settings
        day = self.time_picker._current_date
        root = self.time_picker.current_root
        clip_cache = self.viewer.clip_cache

        self.stop_report_btn.setEnabled(False)
        progress = QProgressDialog("Building stop report...", "Cancel", 0, 0, self)
        progress.setWindowTitle("Stop Report")
        progress.setMinimumDuration(0)
        progress.setValue(0)
        self._stop_report_progress = progress

        def _cleanup():
            self.stop_report_btn.setEnabled(True)
            if self._stop_report_progress is progress:
                self._stop_report_progress = None
            try:
                progress.canceled.disconnect(_on_canceled)
            except (RuntimeError, TypeError):
                pass
            progress.close()

        def _on_canceled():
            self._stop_report_slot.retire()
            _cleanup()

        progress.canceled.connect(_on_canceled)

        def _on_progress(payload):
            try:
                phase, done, total = payload
            except Exception:
                return
            label = "Copying report clips..." if phase == "copies" else "Reading stop thumbnails..."
            progress.setLabelText(label)
            progress.setMaximum(max(1, int(total)))
            progress.setValue(int(done))

        def _on_result(data):
            _cleanup()
            if not data:
                QMessageBox.information(self, "Stop Report", "No stop events found for this day.")
                return
            entries = build_stop_report_entries(data)
            dlg = StopReportDialog(entries, self)
            dlg.open_requested.connect(self._open_report_entry)
            dlg.exec()

        def _on_error(message):
            _cleanup()
            QMessageBox.warning(self, "Stop Report", f"Stop report build failed:\n{message}")

        self._stop_report_slot.start(
            lambda job: collect_stop_report_data(
                items,
                settings=settings,
                day=day,
                root=root,
                clip_cache=clip_cache,
                job=job,
            ),
            on_result=_on_result,
            on_error=_on_error,
            on_progress=_on_progress,
        )

    def _open_report_entry(self, entry: StopReportEntry):
        if entry.video_item is None or entry.video_path is None:
            return
        self.open_in_viewer(entry.video_item)
        self.viewer.seek_to_seconds(entry.seek_seconds, pause=True)

    def _export_source_path(self, original_path: Path) -> Path | None:
        viewer_original = self.viewer.current_video_original_path
        viewer_loaded = self.viewer.current_video_path
        if viewer_original is not None and Path(viewer_original) == original_path and viewer_loaded:
            viewer_loaded_path = Path(viewer_loaded)
            if viewer_loaded_path.exists():
                return viewer_loaded_path
        try:
            cached = self.viewer.get_valid_cached_path(original_path)
            if cached and cached.exists():
                return cached
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
        viewer_original = self.viewer.current_video_original_path
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

    @staticmethod
    def _layout_settings_snapshot(settings: Settings) -> dict:
        # Everything except the volatile per-session fields: when this is
        # unchanged, no widget rebuilt below can look any different.
        snapshot = asdict(settings)
        for key in ("last_session", "window_geometry", "load_warning"):
            snapshot.pop(key, None)
        return snapshot

    def _reload_settings_from_viewer(self):
        # Every settings save lands here via settings_saved. The rebuild
        # below re-lists the Z: share on the UI thread (seconds when a clip
        # copy saturates the link), so it runs only when a field the
        # pickers/filters consume actually changed — and never during
        # shutdown. The Settings.load() must happen unconditionally: the
        # close path saves geometry/session onto self.settings afterwards,
        # and a stale object would clobber the viewer's flush.
        self.settings = Settings.load()
        if self._shutdown_in_progress:
            return
        snapshot = self._layout_settings_snapshot(self.settings)
        if snapshot == self._last_layout_snapshot:
            return
        self._last_layout_snapshot = snapshot

        marks: list[tuple[str, float]] = [("start", time.perf_counter())]

        def mark(label: str):
            marks.append((label, time.perf_counter()))
        self.date_picker.set_system_layout_settings(self.settings)
        mark("date_picker layout")
        self.overview_widget.set_system_layout_settings(self.settings)
        mark("overview layout")
        self.fleetwide_search_widget.set_settings(self.settings)
        mark("fleetwide settings")
        self.time_picker._static_tracks = self._build_static_tracks()
        mark("static tracks")
        current_parent = self.date_picker.parent_dir
        target_parent = Path(self.settings.last_parent) if self.settings.last_parent else None
        if target_parent:
            # String comparison first: .resolve()/.exists() on the Z: share
            # run on the UI thread and stall for seconds while a clip copy
            # saturates the link — this fires on EVERY settings autosave.
            same_parent = current_parent is not None and _path_key(
                current_parent
            ) == _path_key(target_parent)
            if same_parent:
                self.overview_widget.set_parent_dir(target_parent)
                self.fleetwide_search_widget.set_parent_dir(target_parent)
                mark("parent dirs (same)")
            elif target_parent.exists():  # network stat only on a real change
                mark("target_parent.exists")
                self.date_picker.set_parent_dir(target_parent)
                self.overview_widget.set_parent_dir(target_parent)
                self.fleetwide_search_widget.set_parent_dir(target_parent)
                mark("parent dirs (changed)")
            else:
                mark("target_parent.exists (missing)")
        self.overview_widget.refresh_layout()
        mark("refresh_layout")
        if (marks[-1][1] - marks[0][1]) > 0.1:
            steps = " ".join(
                f"{label}={((t - prev) * 1000):.0f}ms"
                for (label, t), (_, prev) in zip(marks[1:], marks[:-1])
            )
            print(f"[settings-reload] {steps}", flush=True)

    def _sync_settings_from_fleetwide_search(self):
        # Keep the viewer's embedded settings panels on the same settings
        # object so a later autosave cannot overwrite fleetwide searches.
        self.viewer.settings = self.settings
        self.viewer.settings_panel.settings = self.settings
        self.viewer.system_layout_panel.settings = self.settings

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
            }
            self._overview_nav_failsafe.start()
        else:
            self._cancel_overview_navigation()
        self.date_picker.select_pikpak_folder_and_day(pikpak_root, selected_day)

    def _cancel_overview_navigation(self) -> None:
        self._pending_overview_navigation = None
        self._overview_nav_failsafe.stop()

    def _on_items_changed_for_navigation(self) -> None:
        """Stage 1: once the target day's clips are on the timeline, open the
        clip containing the target moment. Signal-driven replacement for the
        old 150 ms polling state machine."""
        pending = self._pending_overview_navigation
        if not pending or pending.get("stage") != "load_timeline":
            return
        if self.time_picker.current_root != pending["root"]:
            return
        if self.time_picker._current_date != pending["day"]:
            return
        target_dt = pending["target_dt"]
        items = list(self.time_picker._items or [])
        video_items = [itm for itm in items if itm.kind == "video" and isinstance(itm.payload, Path)]
        if not video_items:
            return  # video partial not in yet; a later items_changed will bring it
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
            clip_item = previous_item or video_items[0]
        pending["clip_item"] = clip_item
        pending["stage"] = "await_clip"
        self.open_in_viewer(clip_item)
        # A cached clip opens synchronously, in which case clip_opened has
        # already fired inside open_in_viewer and cleared the pending state.

    def _on_clip_opened_for_navigation(self, opened_path: Path) -> None:
        """Stage 2: the clip is open (possibly after an async download) —
        force OCR sync and seek to the target moment."""
        pending = self._pending_overview_navigation
        if not pending or pending.get("stage") != "await_clip":
            return
        clip_item = pending.get("clip_item")
        if not isinstance(clip_item, TimelineItem):
            self._cancel_overview_navigation()
            return
        if Path(opened_path) != Path(clip_item.payload):
            # The user opened something else; abandon the navigation.
            self._cancel_overview_navigation()
            return
        target_dt = pending["target_dt"]
        clip_start_dt = ensure_utc(clip_item.start)
        clip_end_dt = ensure_utc(clip_item.end)
        seek_seconds = (target_dt - clip_start_dt).total_seconds()
        clip_duration_seconds = max(0.0, (clip_end_dt - clip_start_dt).total_seconds())
        if clip_duration_seconds > 0.0:
            seek_seconds = min(seek_seconds, clip_duration_seconds)
        seek_seconds = max(0.0, seek_seconds)
        self._cancel_overview_navigation()
        try:
            self.viewer._auto_sync_with_ocr(force=True)
        except Exception:
            pass
        self.viewer.seek_to_seconds(seek_seconds, pause=True)


if __name__ == "__main__":
    from logfather.ui.app_main import main

    main()
