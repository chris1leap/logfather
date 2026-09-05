"""The Data window: how much data the fleet has, per system per day.

Opened from the top bar's Data button (Chris, 2026-09-05). Two workers
run in parallel - Elastic aggregations and the CCTV share scan - and each
fills its half of the window as it lands. A metric toggle switches the
stacked per-day bar chart and the systems x days grid between Elastic
document counts, clip counts and estimated clip bytes.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
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
from logfather.data.elastic_schema import robot_id_from_folder
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot

_METRICS = (
    ("elastic", "Elastic documents"),
    ("clips", "CCTV clips"),
    ("bytes", "CCTV size (est.)"),
)


def _series_colour(index: int) -> QColor:
    """Distinct, theme-friendly colours: golden-angle hue steps."""
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.55, 0.85)
    return colour


class _StackedBarChart(QWidget):
    """One stacked bar per day, a segment per system."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(280)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._days: list[date] = []
        self._series: list[tuple[str, QColor, dict[date, float]]] = []
        self._fmt: Callable[[float], str] = str
        self._empty_text = "No data loaded yet"

    def set_data(
        self,
        days: list[date],
        series: list[tuple[str, QColor, dict[date, float]]],
        fmt: Callable[[float], str],
        empty_text: str = "No data",
    ) -> None:
        self._days = list(days)
        self._series = list(series)
        self._fmt = fmt
        self._empty_text = empty_text
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BG_DEEP))
        left, right, top, bottom = 76, 16, 30, 36
        plot_left = rect.left() + left
        plot_right = rect.right() - right
        plot_top = rect.top() + top
        plot_bottom = rect.bottom() - bottom
        plot_w = max(1, plot_right - plot_left)
        plot_h = max(1, plot_bottom - plot_top)
        totals = [
            sum(values.get(day, 0.0) for _n, _c, values in self._series) for day in self._days
        ]
        if not self._days or not self._series or max(totals, default=0) <= 0:
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(rect, Qt.AlignCenter, self._empty_text)
            painter.end()
            return
        vmax = max(totals)
        # Horizontal guide lines with value labels.
        grid_pen = QPen(QColor(theme.BORDER))
        grid_pen.setWidth(1)
        painter.setFont(QFont(self.font().family(), max(8, self.font().pointSize() - 2)))
        for step in range(0, 5):
            frac = step / 4
            y = plot_bottom - frac * plot_h
            painter.setPen(grid_pen)
            painter.drawLine(plot_left, int(y), plot_right, int(y))
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QRectF(rect.left(), y - 10, left - 8, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                self._fmt(vmax * frac),
            )
        n = len(self._days)
        slot = plot_w / n
        bar_w = max(4.0, slot * 0.62)
        painter.setPen(Qt.NoPen)
        for i, day in enumerate(self._days):
            x = plot_left + i * slot + (slot - bar_w) / 2
            y_cursor = float(plot_bottom)
            for _name, colour, values in self._series:
                value = float(values.get(day, 0.0))
                if value <= 0:
                    continue
                h = value / vmax * plot_h
                painter.setBrush(QBrush(colour))
                painter.setPen(Qt.NoPen)
                painter.drawRect(QRectF(x, y_cursor - h, bar_w, h))
                y_cursor -= h
            total = totals[i]
            if total > 0:
                painter.setPen(QColor(theme.TEXT))
                painter.drawText(
                    QRectF(x - slot / 2, y_cursor - 20, bar_w + slot, 18),
                    Qt.AlignHCenter | Qt.AlignBottom,
                    self._fmt(total),
                )
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(
                QRectF(x - slot / 2, plot_bottom + 6, bar_w + slot, 20),
                Qt.AlignHCenter | Qt.AlignTop,
                day.strftime("%d/%m"),
            )
        axis_pen = QPen(QColor(theme.BORDER_LIGHT))
        painter.setPen(axis_pen)
        painter.drawLine(plot_left, plot_bottom, plot_right, plot_bottom)
        painter.end()


class DataInventoryDialog(QDialog):
    def __init__(
        self,
        settings_provider: Callable,
        parent_dir_provider: Callable[[], Path | None],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Data — fleet inventory")
        self.resize(1180, 820)
        self._settings_provider = settings_provider
        self._parent_dir_provider = parent_dir_provider
        self._elastic: ElasticInventory | None = None
        self._cctv: CctvInventory | None = None
        self._elastic_slot = JobSlot(self)
        self._cctv_slot = JobSlot(self)
        self._metric = "elastic"
        self._started = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self._elastic_summary = QLabel("Elastic: not loaded")
        self._elastic_summary.setWordWrap(True)
        self._cctv_summary = QLabel("CCTV share: not loaded")
        self._cctv_summary.setWordWrap(True)
        layout.addWidget(self._elastic_summary)
        layout.addWidget(self._cctv_summary)

        controls = QHBoxLayout()
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
        self._days_label = QLabel(f"Last {INVENTORY_DAYS} days")
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
        layout.addLayout(controls)

        self._legend = QLabel("")
        self._legend.setWordWrap(True)
        layout.addWidget(self._legend)

        self._chart = _StackedBarChart()
        layout.addWidget(self._chart, 3)

        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self._table, 2)

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
            parts.append(f"{format_count(inventory.total_docs)} documents in total")
        if inventory.oldest_ts is not None:
            age_days = (datetime.now(timezone.utc) - inventory.oldest_ts).days
            parts.append(
                f"oldest record {inventory.oldest_ts.astimezone():%d/%m/%Y} ({age_days} days ago)"
            )
        parts.append(
            f"last {len(inventory.days)} days: {format_count(window_docs)} documents "
            f"across {len(inventory.counts)} systems"
        )
        self._elastic_summary.setText("Elastic: " + " · ".join(parts))
        self._rebuild_views()

    def _on_cctv_result(self, inventory: CctvInventory) -> None:
        self._cctv = inventory
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
        self._rebuild_views()

    # ---- views ------------------------------------------------------------

    def _set_metric(self, key: str) -> None:
        if key == self._metric:
            return
        self._metric = key
        self._rebuild_views()

    def _row_names_and_values(self) -> tuple[list[date], list[tuple[str, dict[date, float]]]]:
        """Rows keyed by system folder name (robot ids from Elastic are
        joined onto their folder; unknown robots keep their id)."""
        days = inventory_days(datetime.now().date(), INVENTORY_DAYS)
        rows: dict[str, dict[date, float]] = {}
        if self._metric == "elastic":
            if self._elastic is None:
                return days, []
            days = list(self._elastic.days)
            robot_to_system: dict[str, str] = {}
            if self._cctv is not None:
                for system, robot in self._cctv.robot_ids.items():
                    if robot:
                        robot_to_system[robot] = system
            for robot, per_day in self._elastic.counts.items():
                name = robot_to_system.get(robot, robot)
                target = rows.setdefault(name, {})
                for day, count in per_day.items():
                    target[day] = target.get(day, 0.0) + float(count)
        else:
            if self._cctv is None:
                return days, []
            days = list(self._cctv.days)
            source = self._cctv.clips if self._metric == "clips" else self._cctv.est_bytes
            for system, per_day in source.items():
                rows[system] = {day: float(v) for day, v in per_day.items()}
        ordered = sorted(rows.items(), key=lambda kv: kv[0].lower())
        return days, ordered

    def _value_formatter(self) -> Callable[[float], str]:
        if self._metric == "bytes":
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
            "clips": "CCTV data not loaded",
            "bytes": "CCTV data not loaded",
        }[self._metric]
        self._chart.set_data(days, series, fmt, empty_text=empty)
        legend_bits = [
            f'<span style="background-color:{colour.name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{name}'
            for name, colour, _values in series
        ]
        self._legend.setText("&nbsp;&nbsp;&nbsp;".join(legend_bits))
        self._fill_table(days, rows, fmt)

    def _fill_table(self, days: list[date], rows: list[tuple[str, dict[date, float]]], fmt) -> None:
        table = self._table
        table.clear()
        table.setColumnCount(len(days) + 2)
        table.setRowCount(len(rows) + 1)
        headers = ["System"] + [d.strftime("%d/%m") for d in days] + ["Total"]
        table.setHorizontalHeaderLabels(headers)
        all_values = [v for _n, values in rows for v in values.values()]
        vmax = max(all_values) if all_values else 0.0
        day_totals = {d: 0.0 for d in days}
        for r, (name, values) in enumerate(rows):
            name_item = QTableWidgetItem(name)
            table.setItem(r, 0, name_item)
            row_total = 0.0
            for c, day in enumerate(days, start=1):
                value = float(values.get(day, 0.0))
                row_total += value
                day_totals[day] += value
                table.setItem(r, c, self._cell(value, vmax, fmt))
            total_item = QTableWidgetItem(fmt(row_total))
            total_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            total_item.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
            table.setItem(r, len(days) + 1, total_item)
        # Totals row.
        last = len(rows)
        label = QTableWidgetItem("Total")
        label.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
        table.setItem(last, 0, label)
        grand = 0.0
        for c, day in enumerate(days, start=1):
            grand += day_totals[day]
            item = QTableWidgetItem(fmt(day_totals[day]))
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            item.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
            table.setItem(last, c, item)
        grand_item = QTableWidgetItem(fmt(grand))
        grand_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grand_item.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
        table.setItem(last, len(days) + 1, grand_item)
        table.resizeRowsToContents()

    @staticmethod
    def _cell(value: float, vmax: float, fmt) -> QTableWidgetItem:
        item = QTableWidgetItem(fmt(value) if value > 0 else "–")
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if vmax > 0 and value > 0:
            # Heat shade between the deep well and the accent fill.
            frac = min(1.0, value / vmax)
            low = QColor(theme.BG_DEEP)
            high = QColor(theme.ACCENT_DIM)
            shade = QColor(
                int(low.red() + (high.red() - low.red()) * frac),
                int(low.green() + (high.green() - low.green()) * frac),
                int(low.blue() + (high.blue() - low.blue()) * frac),
            )
            item.setBackground(QBrush(shade))
        return item
