import sys
import csv
from pathlib import Path
from datetime import timedelta, datetime

import cv2
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QFileDialog, QDoubleSpinBox, QMessageBox,
    QSlider, QSizePolicy, QListWidget, QListWidgetItem,
    QCheckBox, QScrollArea
)


# -------- CONFIG FOR CSV→SUBTITLES --------

TIME_COLUMN = "@timestamp_ros"
TEXT_COLUMNS = ["source", "state_name", "message"]

SOURCE_COLUMN = "source"
MESSAGE_COLUMN = "message"

# Example: "16 Nov, 2025 @ 13:17:37.529"
TIMESTAMP_FORMAT = "%d %b, %Y @ %H:%M:%S.%f"

CSV_SUBTITLE_DURATION_SECONDS = 1.0


# -------- SUBTITLE CUES --------

class SubtitleCue:
    def __init__(self, index, start, end, text):
        self.index = index
        self.start = start  # timedelta (relative)
        self.end = end      # timedelta (relative)
        self.text = text


def format_srt_time(td: timedelta) -> str:
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


def get_subtitle_at_time(cues, t: float, offset_seconds: float) -> str:
    t_td = timedelta(seconds=t) - timedelta(seconds=offset_seconds)
    for cue in cues:
        if cue.start <= t_td <= cue.end:
            return cue.text
    return ""


# -------- CSV → CUES --------

def parse_csv_timestamp(ts_str: str) -> datetime:
    ts_str = ts_str.strip()
    return datetime.strptime(ts_str, TIMESTAMP_FORMAT)


def build_subtitle_text_from_row(row: dict) -> str:
    parts = []
    for col in TEXT_COLUMNS:
        value = row.get(col, "")
        if value is None:
            continue
        value = str(value).strip()
        if value and value != "-":
            parts.append(value)
    return " | ".join(parts)


def load_csv_as_cues_and_filters(path: Path):
    """
    Returns:
      - cues: list[SubtitleCue] with relative times
      - display_rows: list[str] for the log panel
      - source_keys: list[str] for each cue
      - message_keys: list[str] for each cue
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

            text = build_subtitle_text_from_row(row)
            if not text:
                continue

            source_key = str(row.get(SOURCE_COLUMN, "")).strip()
            message_key = str(row.get(MESSAGE_COLUMN, "")).strip()

            rows.append((dt, text, source_key, message_key))

    if not rows:
        raise ValueError("No valid rows with timestamps and text found in CSV")

    rows.sort(key=lambda x: x[0])
    t0 = rows[0][0]

    cues = []
    display_rows = []
    source_keys = []
    message_keys = []

    for i, (dt, text, source_key, message_key) in enumerate(rows, start=1):
        start = dt - t0
        end = start + timedelta(seconds=CSV_SUBTITLE_DURATION_SECONDS)
        cues.append(SubtitleCue(i, start, end, text))
        display_rows.append(f"{dt.strftime('%H:%M:%S.%f')[:-3]}  |  {text}")
        source_keys.append(source_key)
        message_keys.append(message_key)

    return cues, display_rows, source_keys, message_keys


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
                self._scrub_callback(-1)  # scroll up = previous frame
            elif delta < 0:
                self._scrub_callback(1)   # scroll down = next frame
            event.accept()
        else:
            super().wheelEvent(event)


# -------- GUI APPLICATION --------

class VideoSubtitleSync(QWidget):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Video + Log Viewer")

        # Video state
        self.cap = None
        self.fps = 25.0
        self.frame_count = 0
        self.current_frame = 0
        self.playing = False

        self.last_qimage: QImage | None = None

        # All cues/logs from CSV (before filtering)
        self.all_cues: list[SubtitleCue] = []
        self.all_log_display_rows: list[str] = []
        self.all_source_keys: list[str] = []
        self.all_message_keys: list[str] = []

        # Active (filtered) cues/logs
        self.cues: list[SubtitleCue] = []
        self.log_display_rows: list[str] = []

        # Filter checkboxes: key -> QCheckBox
        self.source_checkboxes: dict[str, QCheckBox] = {}
        self.message_checkboxes: dict[str, QCheckBox] = {}

        # Offsets
        self.sync_offset = 0.0
        self.subtitle_offset = 0.0

        # First log time (string like "HH:MM:SS.mmm")
        self.first_log_time_str: str | None = None

        # Timer for playback
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)

        # ----- LEFT FILTER PANEL: SOURCE + MESSAGE -----

        # Source filter
        self.source_label = QLabel(f"Filter by {SOURCE_COLUMN}")
        self.source_label.setWordWrap(True)

        self.source_container_widget = QWidget()
        self.source_layout_inner = QVBoxLayout(self.source_container_widget)
        self.source_layout_inner.addStretch(1)

        self.source_scroll = QScrollArea()
        self.source_scroll.setWidgetResizable(True)
        self.source_scroll.setWidget(self.source_container_widget)
        self.source_scroll.setMinimumWidth(220)

        self.source_all_btn = QPushButton("All")
        self.source_none_btn = QPushButton("None")
        self.source_all_btn.clicked.connect(self.select_all_sources)
        self.source_none_btn.clicked.connect(self.select_no_sources)

        source_buttons_layout = QHBoxLayout()
        source_buttons_layout.addWidget(self.source_all_btn)
        source_buttons_layout.addWidget(self.source_none_btn)

        # Message filter
        self.message_label = QLabel(f"Filter by {MESSAGE_COLUMN}")
        self.message_label.setWordWrap(True)

        self.message_container_widget = QWidget()
        self.message_layout_inner = QVBoxLayout(self.message_container_widget)
        self.message_layout_inner.addStretch(1)

        self.message_scroll = QScrollArea()
        self.message_scroll.setWidgetResizable(True)
        self.message_scroll.setWidget(self.message_container_widget)
        self.message_scroll.setMinimumWidth(220)

        self.message_all_btn = QPushButton("All")
        self.message_none_btn = QPushButton("None")
        self.message_all_btn.clicked.connect(self.select_all_messages)
        self.message_none_btn.clicked.connect(self.select_no_messages)

        message_buttons_layout = QHBoxLayout()
        message_buttons_layout.addWidget(self.message_all_btn)
        message_buttons_layout.addWidget(self.message_none_btn)

        filter_panel_layout = QVBoxLayout()
        filter_panel_layout.addWidget(self.source_label)
        filter_panel_layout.addWidget(self.source_scroll)
        filter_panel_layout.addLayout(source_buttons_layout)
        filter_panel_layout.addSpacing(12)
        filter_panel_layout.addWidget(self.message_label)
        filter_panel_layout.addWidget(self.message_scroll)
        filter_panel_layout.addLayout(message_buttons_layout)

        # ----- MIDDLE: VIDEO + CONTROLS -----

        self.video_label = ScrubbableLabel("No video loaded")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumSize(400, 250)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.video_label.set_scrub_callback(self.scrub_by_frames)

        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.on_slider_moved)
        self.seek_slider.sliderPressed.connect(self.pause)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setAlignment(Qt.AlignCenter)
        self.subtitle_label.setStyleSheet("font-size: 16px;")

        self.info_label = QLabel("Time: 00:00:00,000 | Frame: 0")

        self.open_video_btn = QPushButton("Open Video")
        self.open_csv_btn = QPushButton("Open CSV Log")

        self.play_pause_btn = QPushButton("Play")
        self.step_back_btn = QPushButton("Step -1 frame")
        self.step_forward_btn = QPushButton("Step +1 frame")

        self.offset_spin = QDoubleSpinBox()
        self.offset_spin.setRange(-3600.0, 3600.0)
        self.offset_spin.setSingleStep(0.1)
        self.offset_spin.setDecimals(3)
        self.offset_spin.setValue(0.0)
        self.offset_spin.valueChanged.connect(self.offset_changed)

        self.open_video_btn.clicked.connect(self.open_video)
        self.open_csv_btn.clicked.connect(self.open_csv)
        self.play_pause_btn.clicked.connect(self.toggle_play_pause)
        self.step_back_btn.clicked.connect(self.step_back)
        self.step_forward_btn.clicked.connect(self.step_forward)

        top_controls_layout = QHBoxLayout()
        top_controls_layout.addWidget(self.open_video_btn)
        top_controls_layout.addWidget(self.open_csv_btn)

        playback_layout = QHBoxLayout()
        playback_layout.addWidget(self.play_pause_btn)
        playback_layout.addWidget(self.step_back_btn)
        playback_layout.addWidget(self.step_forward_btn)

        offset_layout = QHBoxLayout()
        offset_layout.addWidget(QLabel("Subtitle fine offset (seconds):"))
        offset_layout.addWidget(self.offset_spin)

        middle_layout = QVBoxLayout()
        middle_layout.addLayout(top_controls_layout)
        middle_layout.addWidget(self.video_label)
        middle_layout.addWidget(self.seek_slider)
        middle_layout.addWidget(self.subtitle_label)
        middle_layout.addWidget(self.info_label)
        middle_layout.addLayout(playback_layout)
        middle_layout.addLayout(offset_layout)

        # ----- RIGHT: LOG WINDOW -----

        self.log_label = QLabel("Log entries")
        self.log_list = QListWidget()
        self.log_list.setSelectionMode(QListWidget.MultiSelection)
        self.log_list.setUniformItemSizes(False)
        self.log_list.setFixedWidth(400)

        self.log_list.setStyleSheet("""
            QListWidget::item:selected {
                background-color: red;
                color: white;
            }
        """)

        # Click log → jump video
        self.log_list.itemClicked.connect(self.goto_log_entry_time)

        self.sync_start_btn = QPushButton("Sync logs to current video (first log)")
        self.sync_start_btn.clicked.connect(self.sync_logs_to_current_video_first_log)

        right_layout = QVBoxLayout()
        right_layout.addWidget(self.log_label)
        right_layout.addWidget(self.log_list)
        right_layout.addWidget(self.sync_start_btn)

        # ----- MAIN LAYOUT -----

        root_layout = QHBoxLayout()
        root_layout.addLayout(filter_panel_layout, stretch=0)
        root_layout.addLayout(middle_layout, stretch=3)
        root_layout.addLayout(right_layout, stretch=0)

        self.setLayout(root_layout)
        self.setMinimumSize(1300, 650)

    # ---- Sync button label ----

    def update_sync_button_label(self):
        """Update the sync button text to include the first log time (if known)."""
        if self.first_log_time_str:
            self.sync_start_btn.setText(
                f"Sync logs to current video (first log: {self.first_log_time_str})"
            )
        else:
            self.sync_start_btn.setText("Sync logs to current video (first log)")

    # ---- Video handling ----

    def open_video(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open Video", "", "Video Files (*.mp4 *.mov *.avi *.mkv);;All Files (*)"
        )
        if not file_path:
            return
        if self.cap is not None:
            self.cap.release()

        self.cap = cv2.VideoCapture(file_path)
        if not self.cap.isOpened():
            QMessageBox.critical(self, "Error", "Failed to open video.")
            self.cap = None
            return

        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        self.current_frame = 0
        self.seek_slider.setRange(0, max(0, self.frame_count - 1))
        self.show_frame(self.current_frame)

        # Refresh sync button text (in case a CSV is already loaded)
        self.update_sync_button_label()

    def open_csv(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Open CSV Log", "", "CSV Files (*.csv);;All Files (*)"
        )
        if not file_path:
            return
        path = Path(file_path)
        try:
            (
                self.all_cues,
                self.all_log_display_rows,
                self.all_source_keys,
                self.all_message_keys,
            ) = load_csv_as_cues_and_filters(path)

            self.sync_offset = 0.0
            self.subtitle_offset = 0.0
            self.offset_spin.setValue(0.0)

            self.build_filter_checkboxes()
            self.apply_filters()

            # Set first log time label from the first row, if available
            if self.all_log_display_rows:
                first_row = self.all_log_display_rows[0]
                # our format is "HH:MM:SS.mmm  |  text"
                self.first_log_time_str = first_row.split("  |", 1)[0].strip()
            else:
                self.first_log_time_str = None

            self.update_sync_button_label()

            QMessageBox.information(
                self,
                "Subtitles",
                f"Loaded {len(self.all_cues)} log entries from CSV."
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load CSV log: {e}")
            self.all_cues = []
            self.all_log_display_rows = []
            self.all_source_keys = []
            self.all_message_keys = []
            self.cues = []
            self.log_display_rows = []
            self.populate_log_list()
            self.clear_filter_checkboxes()
            self.first_log_time_str = None
            self.update_sync_button_label()

    # ---- Filter UI helpers ----

    def clear_filter_checkboxes(self):
        # clear sources
        for i in reversed(range(self.source_layout_inner.count())):
            item = self.source_layout_inner.itemAt(i)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.source_layout_inner.addStretch(1)
        self.source_checkboxes.clear()

        # clear messages
        for i in reversed(range(self.message_layout_inner.count())):
            item = self.message_layout_inner.itemAt(i)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.message_layout_inner.addStretch(1)
        self.message_checkboxes.clear()

    def build_filter_checkboxes(self):
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

        # Now that both exist, update which messages should be visible based on sources
        self.update_message_visibility_from_sources()

    def update_message_visibility_from_sources(self):
        """
        Do NOT rebuild message widgets, just show/hide them according to
        which messages actually appear in the currently allowed sources.
        """
        if not self.all_cues or not self.message_checkboxes:
            return

        source_filter_active = bool(self.source_checkboxes)
        if source_filter_active:
            allowed_sources = {
                key for key, cb in self.source_checkboxes.items() if cb.isChecked()
            }
        else:
            allowed_sources = set()

        # Find which messages appear in allowed sources
        messages_used = set()
        for src, msg in zip(self.all_source_keys, self.all_message_keys):
            if source_filter_active and src not in allowed_sources:
                continue
            if msg:
                messages_used.add(msg)

        # Show/hide message checkboxes
        for msg_val, cb in self.message_checkboxes.items():
            cb.setVisible(msg_val in messages_used)

    def on_source_checkbox_changed(self, _state):
        # sources changed → update message visibility → apply filters
        self.update_message_visibility_from_sources()
        self.apply_filters()

    def on_message_checkbox_changed(self, _state):
        self.apply_filters()

    def select_all_sources(self):
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        # After bulk change, update messages & filter once
        self.update_message_visibility_from_sources()
        self.apply_filters()

    def select_no_sources(self):
        checkboxes = list(self.source_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.update_message_visibility_from_sources()
        self.apply_filters()

    def select_all_messages(self):
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.apply_filters()

    def select_no_messages(self):
        checkboxes = list(self.message_checkboxes.values())
        for cb in checkboxes:
            cb.blockSignals(True)
            cb.setChecked(False)
            cb.blockSignals(False)
        self.apply_filters()

    def apply_filters(self):
        if not self.all_cues:
            return

        allowed_sources = {
            key for key, cb in self.source_checkboxes.items() if cb.isChecked()
        }
        allowed_messages = {
            key for key, cb in self.message_checkboxes.items()
            if cb.isChecked() and cb.isVisible()
        }

        source_filter_active = bool(self.source_checkboxes)
        message_filter_active = any(cb.isVisible() for cb in self.message_checkboxes.values())

        self.cues = []
        self.log_display_rows = []

        for cue, row_text, src, msg in zip(
            self.all_cues,
            self.all_log_display_rows,
            self.all_source_keys,
            self.all_message_keys,
        ):
            if source_filter_active and src not in allowed_sources:
                continue
            if message_filter_active and msg not in allowed_messages:
                continue
            self.cues.append(cue)
            self.log_display_rows.append(row_text)

        self.populate_log_list()

        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_info_and_subtitle(t, self.current_frame)
            self.update_log_highlight(t)

    # ---- Log list ----

    def populate_log_list(self):
        self.log_list.clear()
        for row in self.log_display_rows:
            item = QListWidgetItem(row)
            self.log_list.addItem(item)

    def goto_log_entry_time(self, item: QListWidgetItem):
        """Click a log row → jump video to that log's time."""
        if self.cap is None or not self.cues:
            return
        row = self.log_list.row(item)
        if row < 0 or row >= len(self.cues):
            return

        cue = self.cues[row]
        # video time = cue.start + effective offset
        t = cue.start.total_seconds() + self.effective_offset()
        if t < 0:
            t = 0.0

        if self.fps <= 0:
            frame = 0
        else:
            frame = int(round(t * self.fps))

        if self.frame_count > 0:
            frame = max(0, min(self.frame_count - 1, frame))

        self.pause()
        self.current_frame = frame
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

    def step_back(self):
        self.scrub_by_frames(-1)

    def step_forward(self):
        self.scrub_by_frames(1)

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

    def show_frame(self, frame_index):
        if self.cap is None:
            return

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ret, frame = self.cap.read()
        if not ret:
            return

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = frame_rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        self.last_qimage = qimg

        self.update_video_label()

        if self.frame_count > 0:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(frame_index)
            self.seek_slider.blockSignals(False)

        t = frame_index / self.fps if self.fps > 0 else 0.0
        self.update_info_and_subtitle(t, frame_index)
        self.update_log_highlight(t)

    def update_video_label(self):
        if self.last_qimage is None:
            return
        pixmap = QPixmap.fromImage(self.last_qimage)
        self.video_label.setPixmap(
            pixmap.scaled(
                self.video_label.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_video_label()

    # ---- Subtitles + log highlighting ----

    def effective_offset(self) -> float:
        return self.sync_offset + self.subtitle_offset

    def update_info_and_subtitle(self, t_seconds: float, frame_index: int):
        td = timedelta(seconds=t_seconds)
        time_str = format_srt_time(td)
        self.info_label.setText(f"Time: {time_str} | Frame: {frame_index}")

        if self.cues:
            text = get_subtitle_at_time(self.cues, t_seconds, self.effective_offset())
            self.subtitle_label.setText(text)
        else:
            self.subtitle_label.setText("")

    def update_log_highlight(self, t_seconds: float):
        if not self.cues or self.log_list.count() == 0:
            return

        t_td = timedelta(seconds=t_seconds) - timedelta(seconds=self.effective_offset())

        self.log_list.blockSignals(True)
        self.log_list.clearSelection()

        active_indices = [
            i for i, cue in enumerate(self.cues) if cue.start <= t_td <= cue.end
        ]

        indices_to_highlight: list[int] = []

        if active_indices:
            indices_to_highlight = active_indices
        else:
            prev_idx = None
            next_idx = None

            for i, cue in enumerate(self.cues):
                if cue.start > t_td:
                    next_idx = i
                    prev_idx = i - 1 if i > 0 else None
                    break

            if next_idx is None and self.cues:
                prev_idx = len(self.cues) - 1

            indices_to_highlight = [
                idx for idx in (prev_idx, next_idx) if idx is not None
            ]

        if indices_to_highlight:
            for idx in indices_to_highlight:
                item = self.log_list.item(idx)
                if item is not None:
                    item.setSelected(True)

            first_idx = indices_to_highlight[0]
            self.log_list.scrollToItem(
                self.log_list.item(first_idx),
                QListWidget.PositionAtCenter
            )

        self.log_list.blockSignals(False)

    # ---- Offsets & syncing ----

    def offset_changed(self, val: float):
        self.subtitle_offset = float(val)
        if self.cap is not None:
            t = self.current_frame / self.fps if self.fps > 0 else 0.0
            self.update_info_and_subtitle(t, self.current_frame)
            self.update_log_highlight(t)

    def sync_logs_to_current_video_first_log(self):
        if not self.cues:
            QMessageBox.warning(
                self,
                "No logs",
                "Load a CSV log and make sure at least one source/message filter is enabled."
            )
            return
        if self.cap is None:
            QMessageBox.warning(
                self,
                "No video",
                "Open a video file first."
            )
            return

        first_cue = self.cues[0]
        first_start_secs = first_cue.start.total_seconds()

        t_current = self.current_frame / self.fps if self.fps > 0 else 0.0

        self.sync_offset = t_current - first_start_secs
        self.subtitle_offset = 0.0
        self.offset_spin.setValue(0.0)

        self.update_info_and_subtitle(t_current, self.current_frame)
        self.update_log_highlight(t_current)

        QMessageBox.information(
            self,
            "Synced",
            "Logs are now aligned so that the FIRST visible log entry "
            "matches the CURRENT video frame."
        )


def main():
    app = QApplication(sys.argv)
    win = VideoSubtitleSync()
    win.resize(1400, 700)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
