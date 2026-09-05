"""The Data window: how much data the fleet has, per system per day.

Opened from the top bar's Data button (Chris, 2026-09-05). Two workers
run in parallel - Elastic aggregations and the CCTV share scan - and each
fills its half of the window as it lands. A metric toggle switches the
stacked per-day bar chart between Elastic document counts / storage and
CCTV clip counts / storage; hovering a bar segment shows every metric
for that system on that day.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from logfather.data.data_inventory import (
    CctvInventory,
    ElasticInventory,
    INVENTORY_DAYS,
    fetch_elastic_inventory,
    format_bytes,
    format_count,
    inventory_days,
    scan_cctv_inventory,
)
from logfather.data.settings_store import display_customer_name, system_group_sort_key
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.ui import theme
from logfather.ui.charts import StackedBarChart
from logfather.ui.qt_worker import JobSlot
from logfather.ui.system_filter import SystemFilterPopup, funnel_icon

_HIDDEN_SYSTEMS_KEY = "data_hidden_systems"

_METRICS = (
    ("elastic", "Elastic documents"),
    ("elastic_bytes", "Elastic size"),
    ("clips", "CCTV clips"),
    ("bytes", "CCTV size"),
)

def _series_colour(index: int) -> QColor:
    """Distinct pastel colours: golden-angle hue steps at low saturation
    (Chris, 2026-09-05: the saturated set read as neon on the dark
    ground). Alternating lightness keeps neighbours apart."""
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.32, 0.86 if index % 2 == 0 else 0.74)
    return colour


class DataInventoryDialog(QDialog):
    def __init__(
        self,
        settings_provider: Callable,
        parent_dir_provider: Callable[[], Path | None],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Data — fleet inventory")
        # A real resizable window with minimise/maximise, not a fixed
        # dialog (Chris, 2026-09-05); the chart stretches to fill it.
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowMinMaxButtonsHint
            | Qt.WindowCloseButtonHint
        )
        self.setSizeGripEnabled(True)
        self.setMinimumSize(720, 480)
        self.resize(1180, 720)
        self._settings_provider = settings_provider
        self._parent_dir_provider = parent_dir_provider
        self._elastic: ElasticInventory | None = None
        self._cctv: CctvInventory | None = None
        self._elastic_slot = JobSlot(self)
        self._cctv_slot = JobSlot(self)
        self._metric = "elastic"
        self._started = False
        # robot id -> system folder, once the CCTV scan names the folders.
        self._robot_to_system: dict[str, str] = {}
        # Systems un-ticked in the filter; per-user, remembered.
        stored = load_ui_state().get(_HIDDEN_SYSTEMS_KEY)
        self._hidden_systems: set[str] = (
            {str(s) for s in stored if str(s).strip()} if isinstance(stored, list) else set()
        )
        self._filter_popup: SystemFilterPopup | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro = QLabel(
            "Here is an overview of the data stored by PikPak systems. "
            "Elastic logs are stored continuously. "
            "CCTV footage is stored for the last 30 days only (currently)."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(intro)

        # Two headline tiles (Chris, 2026-09-05): Elastic total since its
        # oldest record, CCTV total currently on the share.
        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self._elastic_tile, self._elastic_tile_value, self._elastic_tile_sub = self._make_tile("Elastic total")
        self._cctv_tile, self._cctv_tile_value, self._cctv_tile_sub = self._make_tile("CCTV total")
        tiles.addWidget(self._elastic_tile, 1)
        tiles.addWidget(self._cctv_tile, 1)
        layout.addLayout(tiles)

        self._elastic_summary = QLabel("Elastic: not loaded")
        self._elastic_summary.setWordWrap(True)
        self._cctv_summary = QLabel("CCTV share: not loaded")
        self._cctv_summary.setWordWrap(True)
        layout.addWidget(self._elastic_summary)
        layout.addWidget(self._cctv_summary)

        controls = QHBoxLayout()
        self._filter_btn = QToolButton()
        self._filter_btn.setText("Filter")
        self._filter_btn.setIcon(funnel_icon())
        self._filter_btn.setIconSize(QSize(18, 18))
        self._filter_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._filter_btn.setToolTip("Choose which systems to show")
        self._filter_btn.clicked.connect(self._open_filter_popup)
        if self._hidden_systems:
            self._filter_btn.setText(f"Filter ({len(self._hidden_systems)} hidden)")
        controls.addWidget(self._filter_btn)
        controls.addSpacing(12)
        controls.addWidget(QLabel("Show:"))
        self._metric_group = QButtonGroup(self)
        self._metric_group.setExclusive(True)
        self._metric_buttons: dict[str, QPushButton] = {}
        for key, label in _METRICS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, k=key: self._set_metric(k))
            self._metric_group.addButton(btn)
            self._metric_buttons[key] = btn
            controls.addWidget(btn)
        self._metric_buttons["elastic"].setChecked(True)
        controls.addSpacing(16)
        self._days_label = QLabel("hover a bar for details · click a CCTV bar to open its folder")
        self._days_label.setStyleSheet(theme.MUTED_LABEL)
        controls.addWidget(self._days_label)
        controls.addStretch(1)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(theme.MUTED_LABEL)
        controls.addWidget(self._status_label)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(140)
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        controls.addWidget(self._progress)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.start)
        controls.addWidget(self._refresh_btn)
        # The 14-day section - filter, metric toggle, key and chart - sits
        # in one framed box (Chris, 2026-09-05).
        summary_box = QGroupBox(f"{INVENTORY_DAYS} day summary")
        summary_box.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; margin-top: 16px; padding: 10px 8px 8px 8px;"
            f" border: 1px solid {theme.BORDER_LIGHT}; border-radius: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px;"
            f" color: {theme.TEXT_BRIGHT}; }}"
        )
        box_layout = QVBoxLayout(summary_box)
        box_layout.setSpacing(8)
        box_layout.addLayout(controls)

        self._legend = QLabel("")
        self._legend.setWordWrap(True)
        box_layout.addWidget(self._legend)

        self._chart = StackedBarChart()
        self._chart.set_detail_provider(self._detail_for)
        self._chart.set_click_handler(None)
        box_layout.addWidget(self._chart, 1)
        layout.addWidget(summary_box, 1)

    @staticmethod
    def _make_tile(title: str):
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_RAISED}; border: 1px solid {theme.BORDER};"
            " border-radius: 6px; }"
            "QLabel { border: none; background: transparent; }"
        )
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 12, 18, 12)
        box.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        value_label = QLabel("—")
        value_font = QFont()
        value_font.setPointSizeF(value_font.pointSizeF() * 2.2)
        value_font.setBold(True)
        value_label.setFont(value_font)
        value_label.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        sub_label = QLabel("loading...")
        sub_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        box.addWidget(title_label)
        box.addWidget(value_label)
        box.addWidget(sub_label)
        return frame, value_label, sub_label

    # ---- lifecycle --------------------------------------------------------

    def start_if_needed(self) -> None:
        if not self._started:
            self.start()

    def start(self) -> None:
        self._started = True
        settings = self._settings_provider()
        parent_dir = self._parent_dir_provider()
        self._elastic_summary.setText("Elastic: loading...")
        self._progress.show()
        self._status_label.setText("Querying Elastic and the CCTV share...")
        self._elastic_slot.start(
            lambda job: fetch_elastic_inventory(settings, INVENTORY_DAYS, progress=job.emit_progress),
            on_result=self._on_elastic_result,
            on_error=self._on_elastic_error,
            on_progress=self._on_progress,
            on_finished=self._on_any_finished,
        )
        if parent_dir is None:
            self._cctv_summary.setText("CCTV share: no parent folder configured")
        else:
            self._cctv_summary.setText("CCTV share: scanning...")
            self._cctv_slot.start(
                lambda job: scan_cctv_inventory(
                    parent_dir,
                    INVENTORY_DAYS,
                    progress=job.emit_progress,
                    interrupted=job.interrupted,
                ),
                on_result=self._on_cctv_result,
                on_error=self._on_cctv_error,
                on_progress=self._on_progress,
                on_finished=self._on_any_finished,
            )

    def shutdown(self) -> None:
        self._elastic_slot.shutdown()
        self._cctv_slot.shutdown()

    def closeEvent(self, event):
        # Hide rather than destroy: reopening shows the last results.
        event.ignore()
        self.hide()

    # ---- worker callbacks -------------------------------------------------

    def _on_progress(self, message) -> None:
        self._status_label.setText(str(message or ""))

    def _on_any_finished(self) -> None:
        if not self._elastic_slot.is_running() and not self._cctv_slot.is_running():
            self._progress.hide()
            self._status_label.setText("Done")

    def _on_elastic_error(self, message: str) -> None:
        self._elastic_summary.setText(f"Elastic: failed — {message}")

    def _on_cctv_error(self, message: str) -> None:
        self._cctv_summary.setText(f"CCTV share: failed — {message}")

    def _on_elastic_result(self, inventory: ElasticInventory) -> None:
        self._elastic = inventory
        window_docs = sum(sum(per_day.values()) for per_day in inventory.counts.values())
        parts = []
        if inventory.total_docs is not None:
            total_text = f"{format_count(inventory.total_docs)} documents in total"
            if inventory.total_bytes:
                total_text += f" ≈ {format_bytes(inventory.total_bytes)}"
            parts.append(total_text)
        if inventory.oldest_ts is not None:
            age_days = (datetime.now(timezone.utc) - inventory.oldest_ts).days
            parts.append(
                f"oldest record {inventory.oldest_ts.astimezone():%d/%m/%Y} ({age_days} days ago)"
            )
        window_text = (
            f"last {len(inventory.days)} days: {format_count(window_docs)} documents "
            f"across {len(inventory.counts)} systems"
        )
        if inventory.bytes_per_doc:
            window_bytes = sum(
                sum(per_day.values()) * inventory.bytes_factor(robot)
                for robot, per_day in inventory.counts.items()
            )
            window_text += f" ≈ {format_bytes(window_bytes)}"
        parts.append(window_text)
        if inventory.bytes_basis:
            parts.append(f"sizes: {inventory.bytes_basis}")
        self._elastic_summary.setText("Elastic: " + " · ".join(parts))
        if inventory.total_bytes:
            self._elastic_tile_value.setText(format_bytes(inventory.total_bytes))
            since = (
                f"since {inventory.oldest_ts.astimezone():%d %b %Y}"
                if inventory.oldest_ts is not None
                else "all records"
            )
            est = "" if inventory.bytes_basis.startswith("index store") else " · estimated"
            self._elastic_tile_sub.setText(f"{since} · {format_count(inventory.total_docs or 0)} documents{est}")
        else:
            self._elastic_tile_value.setText("—")
            self._elastic_tile_sub.setText("size unavailable")
        self._rebuild_views()

    def _on_cctv_result(self, inventory: CctvInventory) -> None:
        self._cctv = inventory
        self._robot_to_system = {
            robot: system for system, robot in inventory.robot_ids.items() if robot
        }
        total_clips = sum(sum(v.values()) for v in inventory.clips.values())
        total_bytes = sum(sum(v.values()) for v in inventory.est_bytes.values())
        folder_counts = sorted(c for c in inventory.day_folders.values() if c)
        oldest_pairs = [(d, name) for name, d in inventory.oldest_day.items() if d is not None]
        parts = [f"{len(inventory.clips)} systems"]
        parts.append(
            f"last {len(inventory.days)} days: {total_clips:,} clips ≈ {format_bytes(total_bytes)} (est.)"
        )
        if folder_counts:
            median = folder_counts[len(folder_counts) // 2]
            parts.append(f"typically {median} day folders per system")
        if oldest_pairs:
            oldest_day, oldest_name = min(oldest_pairs)
            parts.append(f"oldest day folder {oldest_day:%d/%m/%Y} ({oldest_name})")
        self._cctv_summary.setText("CCTV share: " + " · ".join(parts))
        # Share total: each system's average bytes per day-with-clips in
        # the window, times the day folders it actually has on the share
        # (the retained ~30 days), summed across systems.
        share_total = 0.0
        for system, per_day in inventory.est_bytes.items():
            days_with_clips = sum(1 for v in inventory.clips.get(system, {}).values() if v)
            if not days_with_clips:
                continue
            per_day_avg = sum(per_day.values()) / days_with_clips
            share_total += per_day_avg * max(days_with_clips, inventory.day_folders.get(system, 0))
        folder_note = f"last {folder_counts[len(folder_counts) // 2]} days" if folder_counts else "current retention"
        self._cctv_tile_value.setText(format_bytes(share_total))
        self._cctv_tile_sub.setText(f"on the share ({folder_note}) · {len(inventory.clips)} systems · estimated")
        self._rebuild_views()

    # ---- system filter ----------------------------------------------------

    def _known_systems(self) -> list[str]:
        names: set[str] = set()
        if self._cctv is not None:
            names.update(self._cctv.clips.keys())
        if self._elastic is not None:
            names.update(self._robot_to_system.get(r, r) for r in self._elastic.counts)
        settings = self._settings_provider()
        return sorted(names, key=lambda n: system_group_sort_key(settings, n))

    def _system_groups(self) -> list[tuple[str, list[str]]]:
        settings = self._settings_provider()
        groups: list[tuple[str, list[str]]] = []
        for name in self._known_systems():
            customer = str(display_customer_name(settings, name) or "")
            if groups and groups[-1][0] == customer:
                groups[-1][1].append(name)
            else:
                groups.append((customer, [name]))
        return groups

    def _open_filter_popup(self) -> None:
        popup = SystemFilterPopup(
            self._system_groups(),
            self._hidden_systems,
            on_change=self._on_system_toggled,
            on_all=self._on_all_systems,
            parent=self,
        )
        self._filter_popup = popup
        anchor = self._filter_btn.mapToGlobal(QPoint(0, self._filter_btn.height()))
        popup.move(anchor)
        popup.show()

    def _on_system_toggled(self, name: str, visible: bool) -> None:
        if visible:
            self._hidden_systems.discard(name)
        else:
            self._hidden_systems.add(name)
        self._persist_hidden()
        self._rebuild_views()

    def _on_all_systems(self, visible: bool) -> None:
        if visible:
            self._hidden_systems.clear()
        else:
            self._hidden_systems.update(self._known_systems())
        self._persist_hidden()
        self._rebuild_views()

    def _persist_hidden(self) -> None:
        update_ui_state({_HIDDEN_SYSTEMS_KEY: sorted(self._hidden_systems)})
        count = len(self._hidden_systems)
        self._filter_btn.setText("Filter" if not count else f"Filter ({count} hidden)")

    # ---- views ------------------------------------------------------------

    def _set_metric(self, key: str) -> None:
        if key == self._metric:
            return
        self._metric = key
        self._rebuild_views()

    def _elastic_rows(self, as_bytes: bool) -> tuple[list[date], dict[str, dict[date, float]]]:
        rows: dict[str, dict[date, float]] = {}
        inv = self._elastic
        if inv is None or (as_bytes and not inv.bytes_per_doc):
            return inventory_days(datetime.now().date(), INVENTORY_DAYS), rows
        for robot, per_day in inv.counts.items():
            factor = inv.bytes_factor(robot) if as_bytes else 1.0
            name = self._robot_to_system.get(robot, robot)
            target = rows.setdefault(name, {})
            for day, count in per_day.items():
                target[day] = target.get(day, 0.0) + float(count) * factor
        return list(inv.days), rows

    def _row_names_and_values(self) -> tuple[list[date], list[tuple[str, dict[date, float]]]]:
        """Rows keyed by system folder name (robot ids from Elastic are
        joined onto their folder; unknown robots keep their id)."""
        if self._metric in ("elastic", "elastic_bytes"):
            days, rows = self._elastic_rows(as_bytes=self._metric == "elastic_bytes")
        else:
            days = inventory_days(datetime.now().date(), INVENTORY_DAYS)
            rows = {}
            if self._cctv is not None:
                days = list(self._cctv.days)
                source = self._cctv.clips if self._metric == "clips" else self._cctv.est_bytes
                for system, per_day in source.items():
                    rows[system] = {day: float(v) for day, v in per_day.items()}
        settings = self._settings_provider()
        ordered = sorted(
            ((name, values) for name, values in rows.items() if name not in self._hidden_systems),
            key=lambda kv: system_group_sort_key(settings, kv[0]),
        )
        return days, ordered

    def _value_formatter(self) -> Callable[[float], str]:
        if self._metric in ("bytes", "elastic_bytes"):
            return lambda v: format_bytes(v)
        if self._metric == "clips":
            return lambda v: f"{int(round(v)):,}"
        return lambda v: format_count(v)

    def _rebuild_views(self) -> None:
        days, rows = self._row_names_and_values()
        fmt = self._value_formatter()
        series = [
            (name, _series_colour(i), values) for i, (name, values) in enumerate(rows)
        ]
        empty = {
            "elastic": "Elastic data not loaded",
            "elastic_bytes": "Elastic size unavailable",
            "clips": "CCTV data not loaded",
            "bytes": "CCTV data not loaded",
        }[self._metric]
        self._chart.set_data(days, series, fmt, empty_text=empty)
        # CCTV bars open that system's day folder in Explorer on click
        # (Chris, 2026-09-05); Elastic bars have nowhere to go.
        self._chart.set_click_handler(
            self._open_day_folder if self._metric in ("clips", "bytes") else None
        )
        legend_bits = [
            f'<span style="background-color:{colour.name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{name}'
            for name, colour, _values in series
        ]
        self._legend.setText("&nbsp;&nbsp;&nbsp;".join(legend_bits))

    def _open_day_folder(self, name: str, day: date) -> None:
        parent_dir = self._parent_dir_provider()
        if parent_dir is None:
            return
        folder = Path(parent_dir) / name / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
        if not folder.is_dir():
            self._status_label.setText(f"No folder on the share for {name} on {day:%d/%m/%Y}")
            return
        try:
            os.startfile(str(folder))
            self._status_label.setText(f"Opened {folder}")
        except OSError as exc:
            self._status_label.setText(f"Could not open {folder}: {exc}")

    def _detail_for(self, name: str, day: date) -> str:
        """Every metric for one system on one day, for the hover tooltip."""
        # Just the system and its figures: the day and the share of the
        # day are already visible on the chart (Chris, 2026-09-05).
        lines = [f"<b>{name}</b>"]
        # Only the source the chart is showing (Chris, 2026-09-05): an
        # Elastic view says nothing about CCTV, and vice versa.
        showing_elastic = self._metric in ("elastic", "elastic_bytes")
        inv = self._elastic if showing_elastic else None
        if inv is not None:
            docs = 0
            est_bytes = 0.0
            factor_used = 0.0
            for robot, per_day in inv.counts.items():
                if self._robot_to_system.get(robot, robot) == name:
                    count = int(per_day.get(day, 0))
                    docs += count
                    factor_used = inv.bytes_factor(robot)
                    est_bytes += count * factor_used
            line = f"Elastic: {format_count(docs)} documents"
            if est_bytes and docs:
                line += f" ≈ {format_bytes(est_bytes)} (avg {factor_used:.0f} B/doc)"
            lines.append(line)
        cctv = None if showing_elastic else self._cctv
        if cctv is not None and name in cctv.clips:
            clips = int(cctv.clips[name].get(day, 0))
            est = int(cctv.est_bytes.get(name, {}).get(day, 0))
            lines.append(f"CCTV: {clips:,} clips ≈ {format_bytes(est)} (est.)")
            lines.append("<i>Click to open this day's folder</i>")
            oldest = cctv.oldest_day.get(name)
            folders = cctv.day_folders.get(name)
            if oldest is not None or folders:
                extra = []
                if folders:
                    extra.append(f"{folders} day folders on the share")
                if oldest is not None:
                    extra.append(f"oldest {oldest:%d/%m/%Y}")
                lines.append(" · ".join(extra))
        return "<br>".join(lines)
