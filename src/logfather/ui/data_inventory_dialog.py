"""The Data window: how much data the fleet has, per system per day.

Opened from the top bar's Data button (Chris, 2026-09-05). Two workers
run in parallel - Elastic aggregations and the CCTV share scan - and each
fills its half of the window as it lands. A metric toggle switches the
stacked per-day bar chart between Elastic document counts / storage and
CCTV clip counts / storage; hovering a bar segment shows every metric
for that system on that day.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
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
from logfather.ui.qt_worker import JobSlot

_HIDDEN_SYSTEMS_KEY = "data_hidden_systems"

_METRICS = (
    ("elastic", "Elastic documents"),
    ("elastic_bytes", "Elastic size"),
    ("clips", "CCTV clips"),
    ("bytes", "CCTV size"),
)

DetailFn = Callable[[str, date], str]


def _funnel_icon(size: int = 18) -> QIcon:
    """A filter funnel drawn in the theme's light ink."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    s = float(size)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(s * 0.10, s * 0.15),
                QPointF(s * 0.90, s * 0.15),
                QPointF(s * 0.58, s * 0.52),
                QPointF(s * 0.58, s * 0.88),
                QPointF(s * 0.42, s * 0.80),
                QPointF(s * 0.42, s * 0.52),
            ]
        )
    )
    painter.end()
    return QIcon(pm)


class _SystemFilterPopup(QWidget):
    """Tick boxes per system, grouped by customer with a rule between
    groups; stays open until a click lands outside (Chris, 2026-09-05)."""

    def __init__(
        self,
        groups: list[tuple[str, list[str]]],
        hidden: set[str],
        on_change: Callable[[str, bool], None],
        on_all: Callable[[bool], None],
        parent=None,
    ):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self._on_change = on_change
        self._on_all = on_all
        self._boxes: list[QCheckBox] = []
        self.setStyleSheet(
            f"QWidget {{ background-color: {theme.BG_RAISED}; }}"
            f"QLabel {{ color: {theme.TEXT}; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        head = QHBoxLayout()
        title = QLabel("Show systems")
        title.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_BRIGHT};")
        head.addWidget(title)
        head.addStretch(1)
        all_btn = QPushButton("All")
        none_btn = QPushButton("None")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        head.addWidget(all_btn)
        head.addWidget(none_btn)
        outer.addLayout(head)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        rows = QVBoxLayout(body)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)
        for index, (customer, systems) in enumerate(groups):
            if index:
                rule = QFrame()
                rule.setFrameShape(QFrame.HLine)
                rule.setStyleSheet(f"color: {theme.BORDER_LIGHT};")
                rows.addWidget(rule)
            if customer:
                label = QLabel(customer)
                label.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_MUTED};")
                rows.addWidget(label)
            for system in systems:
                box = QCheckBox(system)
                box.setChecked(system not in hidden)
                box.toggled.connect(
                    lambda checked, name=system: self._on_change(name, checked)
                )
                rows.addWidget(box)
                self._boxes.append(box)
        rows.addStretch(1)
        scroll.setWidget(body)
        scroll.setMaximumHeight(560)
        outer.addWidget(scroll)
        self.adjustSize()

    def _set_all(self, visible: bool) -> None:
        for box in self._boxes:
            box.blockSignals(True)
            box.setChecked(visible)
            box.blockSignals(False)
        self._on_all(visible)


def _series_colour(index: int) -> QColor:
    """Distinct pastel colours: golden-angle hue steps at low saturation
    (Chris, 2026-09-05: the saturated set read as neon on the dark
    ground). Alternating lightness keeps neighbours apart."""
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.32, 0.86 if index % 2 == 0 else 0.74)
    return colour


class _StackedBarChart(QWidget):
    """One stacked bar per day, a segment per system; hover a segment for
    that system/day's details (Chris, 2026-09-05)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._days: list[date] = []
        self._series: list[tuple[str, QColor, dict[date, float]]] = []
        self._fmt: Callable[[float], str] = str
        self._empty_text = "No data loaded yet"
        self._detail_fn: DetailFn | None = None
        # Filled during paint: (rect, name, day) per drawn segment.
        self._segments: list[tuple[QRectF, str, date]] = []
        self._hover_index: int | None = None

    def set_detail_provider(self, fn: DetailFn | None) -> None:
        self._detail_fn = fn

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
        self._hover_index = None
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()
        hit = None
        for index, (rect, _name, _day) in enumerate(self._segments):
            if rect.contains(pos):
                hit = index
                break
        if hit != self._hover_index:
            self._hover_index = hit
            self.update()
        if hit is None:
            QToolTip.hideText()
        else:
            rect, name, day = self._segments[hit]
            text = self._detail_fn(name, day) if self._detail_fn else f"{name} — {day:%d/%m/%Y}"
            QToolTip.showText(event.globalPosition().toPoint(), text, self, rect.toRect())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_index is not None:
            self._hover_index = None
            self.update()
        QToolTip.hideText()
        super().leaveEvent(event)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor(theme.BG_DEEP))
        self._segments = []
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
        for i, day in enumerate(self._days):
            x = plot_left + i * slot + (slot - bar_w) / 2
            y_cursor = float(plot_bottom)
            for name, colour, values in self._series:
                value = float(values.get(day, 0.0))
                if value <= 0:
                    continue
                h = value / vmax * plot_h
                seg = QRectF(x, y_cursor - h, bar_w, h)
                segment_index = len(self._segments)
                self._segments.append((seg, name, day))
                painter.setBrush(QBrush(colour))
                if segment_index == self._hover_index:
                    outline = QPen(QColor(theme.TEXT_BRIGHT))
                    outline.setWidth(2)
                    painter.setPen(outline)
                else:
                    painter.setPen(Qt.NoPen)
                painter.drawRect(seg)
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
        painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
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
        self._filter_popup: _SystemFilterPopup | None = None

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

        self._elastic_summary = QLabel("Elastic: not loaded")
        self._elastic_summary.setWordWrap(True)
        self._cctv_summary = QLabel("CCTV share: not loaded")
        self._cctv_summary.setWordWrap(True)
        layout.addWidget(self._elastic_summary)
        layout.addWidget(self._cctv_summary)

        controls = QHBoxLayout()
        self._filter_btn = QToolButton()
        self._filter_btn.setText("Filter")
        self._filter_btn.setIcon(_funnel_icon())
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
        self._days_label = QLabel(f"Last {INVENTORY_DAYS} days · hover a bar for details")
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
        self._chart.set_detail_provider(self._detail_for)
        layout.addWidget(self._chart, 1)

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
        popup = _SystemFilterPopup(
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
        legend_bits = [
            f'<span style="background-color:{colour.name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{name}'
            for name, colour, _values in series
        ]
        self._legend.setText("&nbsp;&nbsp;&nbsp;".join(legend_bits))

    def _detail_for(self, name: str, day: date) -> str:
        """Every metric for one system on one day, for the hover tooltip."""
        # Just the system and its figures: the day and the share of the
        # day are already visible on the chart (Chris, 2026-09-05).
        lines = [f"<b>{name}</b>"]
        inv = self._elastic
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
        cctv = self._cctv
        if cctv is not None and name in cctv.clips:
            clips = int(cctv.clips[name].get(day, 0))
            est = int(cctv.est_bytes.get(name, {}).get(day, 0))
            lines.append(f"CCTV: {clips:,} clips ≈ {format_bytes(est)} (est.)")
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
