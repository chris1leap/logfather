"""The Errors / Stops window (Chris, 2026-09-05): line stoppages per day
and errors per day by category, for the systems and days chosen with
the same filter and day picker as the Overview.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QScrollBar,
    QAbstractItemView,
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
)

from logfather.data.elastic_schema import robot_id_from_folder
from logfather.data.errors_stops import (
    ERROR_CATEGORY_ORDER,
    STOP_KIND_ORDER,
    ErrorsStopsData,
    day_list,
    categorize_error,
    fetch_errors_stops,
    stop_kind,
)
from logfather.data.settings_store import display_customer_name, system_group_sort_key
from logfather.data.software_history import system_display_name
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.ui import theme
from logfather.ui.charts import StackedBarChart
from logfather.ui.day_range_dialog import MAX_RANGE_DAYS, DayRangeDialog
from logfather.ui.qt_worker import JobSlot
from logfather.ui.system_filter import SystemFilterPopup, funnel_icon

_HIDDEN_KEY = "errors_hidden_systems"

# Stop kinds keep their meaning in colour: red-ish for emergency, amber
# for protective, blue for operator, yellow for caution (all pastel).
_STOP_COLOURS = {
    "Emergency stop": QColor("#d98a8a"),
    "Protective stop": QColor("#d9b07a"),
    "Operator stop": QColor("#86a9d1"),
    "Caution": QColor("#d6cf7f"),
}


def _category_colour(index: int) -> QColor:
    hue = (index * 137.508) % 360.0
    colour = QColor()
    colour.setHsvF(hue / 360.0, 0.32, 0.86 if index % 2 == 0 else 0.74)
    return colour


class ErrorsStopsWindow(QDialog):
    def __init__(
        self,
        settings_provider: Callable,
        known_systems_provider: Callable[[], list[str]],
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Errors / Stops")
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(860, 600)
        self.resize(1280, 880)
        self._settings_provider = settings_provider
        self._known_systems_provider = known_systems_provider
        self._slot = JobSlot(self)
        self._extend_slot = JobSlot(self)
        self._data: ErrorsStopsData | None = None
        self._pending: set[date] = set()
        self._extending: tuple[date, date] | None = None
        self._syncing_scroll = False
        self._started = False
        today = datetime.now().date()
        self._day_range: tuple[date, date] = (today - timedelta(days=6), today)
        stored = load_ui_state().get(_HIDDEN_KEY)
        self._hidden: set[str] = {str(n) for n in stored if str(n).strip()} if isinstance(stored, list) else set()
        self._filter_dirty = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intro = QLabel(
            "Line stoppages and errors per day for the chosen systems and days. Stops are the "
            "emergency, protective, operator and caution states; errors are every error or "
            "failure state, grouped by the part of the system that raised it. Hover a bar for "
            "the breakdown. Each day shows one bar per system, so a system with far more "
            "errors than the rest, or a sudden rise, stands out. Scroll the charts sideways; "
            "scrolling past either end loads more days."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.filter_btn = QToolButton()
        self.filter_btn.setIcon(funnel_icon())
        self.filter_btn.setIconSize(QSize(18, 18))
        self.filter_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.filter_btn.setToolTip("Choose which systems to include")
        self.filter_btn.clicked.connect(self._open_filter)
        controls.addWidget(self.filter_btn)
        controls.addSpacing(12)
        self.live_btn = QPushButton("Live (today)")
        self.live_btn.setCheckable(True)
        self.live_btn.setToolTip("Today only")
        self.live_btn.clicked.connect(self._on_live)
        controls.addWidget(self.live_btn)
        self.pick_days_btn = QPushButton("")
        self.pick_days_btn.setToolTip("Choose a day or a span of days")
        self.pick_days_btn.clicked.connect(self._on_pick_days)
        controls.addWidget(self.pick_days_btn)
        controls.addStretch(1)
        self._status = QLabel("")
        self._status.setStyleSheet(theme.MUTED_LABEL)
        controls.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setFixedWidth(140)
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        controls.addWidget(self._progress)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.start)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)
        self._stops_tile, self._stops_value, self._stops_sub = self._make_tile("Line stoppages")
        self._errors_tile, self._errors_value, self._errors_sub = self._make_tile("Errors")
        tiles.addWidget(self._stops_tile, 1)
        tiles.addWidget(self._errors_tile, 1)
        layout.addLayout(tiles)

        self._label_to_robot: dict[str, str] = {}
        self._stops_chart = StackedBarChart()
        self._stops_chart.setMinimumHeight(220)
        self._stops_chart.set_grouped(True)
        self._stops_chart.set_detail_provider(lambda label, day: self._detail("stops", label, day))
        self._stops_legend = QLabel("")
        layout.addWidget(self._boxed("Line stoppages per day", self._stops_legend, self._stops_chart), 3)

        self._errors_chart = StackedBarChart()
        self._errors_chart.setMinimumHeight(220)
        self._errors_chart.set_grouped(True)
        self._errors_chart.set_detail_provider(lambda label, day: self._detail("errors", label, day))
        self._errors_legend = QLabel("")
        layout.addWidget(self._boxed("Errors per day", self._errors_legend, self._errors_chart), 3)
        # One scrollbar drives both charts; the wheel over either chart
        # scrolls too, and pushing past an end loads seven more days.
        self._scrollbar = QScrollBar(Qt.Horizontal)
        self._scrollbar.setToolTip("Scroll through the days; keep going past the end to load more")
        self._scrollbar.valueChanged.connect(self._on_scrollbar)
        layout.addWidget(self._scrollbar)
        for chart in (self._stops_chart, self._errors_chart):
            chart.scroll_changed.connect(self._on_chart_scrolled)
            chart.edge_reached.connect(self._on_edge)

        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setMaximumHeight(240)
        layout.addWidget(self._boxed("By system", None, self._table), 2)
        self._refresh_labels()

    # ---- widgets ----------------------------------------------------------

    @staticmethod
    def _make_tile(title: str):
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_RAISED}; border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            "QLabel { border: none; background: transparent; }"
        )
        box = QVBoxLayout(frame)
        box.setContentsMargins(18, 10, 18, 10)
        box.setSpacing(2)
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-weight: bold;")
        value = QLabel("—")
        font = QFont()
        font.setPointSizeF(font.pointSizeF() * 2.0)
        font.setBold(True)
        value.setFont(font)
        value.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        sub = QLabel("")
        sub.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        box.addWidget(title_label)
        box.addWidget(value)
        box.addWidget(sub)
        return frame, value, sub

    @staticmethod
    def _boxed(title: str, legend: QLabel | None, body) -> QGroupBox:
        box = QGroupBox(title)
        box.setStyleSheet(
            f"QGroupBox {{ font-weight: bold; margin-top: 14px; padding: 8px 6px 6px 6px; border: 1px solid {theme.BORDER_LIGHT}; border-radius: 6px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {theme.TEXT_BRIGHT}; }}"
        )
        inner = QVBoxLayout(box)
        inner.setSpacing(4)
        if legend is not None:
            legend.setWordWrap(True)
            inner.addWidget(legend)
        inner.addWidget(body, 1)
        return box

    # ---- selection ---------------------------------------------------------

    def _refresh_labels(self):
        start, end = self._day_range
        today = datetime.now().date()
        live = start == end == today
        self.live_btn.setChecked(live)
        if start == end:
            self.pick_days_btn.setText(start.strftime("%d/%m/%Y") if not live else "Choose days…")
        else:
            self.pick_days_btn.setText(f"{start:%d/%m} – {end:%d/%m/%Y}")
        count = len(self._hidden)
        self.filter_btn.setText("Filter" if not count else f"Filter ({count} hidden)")

    def _on_live(self):
        today = datetime.now().date()
        self._day_range = (today, today)
        self._refresh_labels()
        self.start()

    def _on_pick_days(self):
        dialog = DayRangeDialog(self._day_range, self)
        if dialog.exec() != QDialog.Accepted:
            return
        start, end = dialog.selected_range()
        today = datetime.now().date()
        end = min(end, today)
        start = min(start, end)
        self._day_range = (start, end)
        self._refresh_labels()
        self.start()

    def _groups(self) -> list[tuple[str, list[str]]]:
        settings = self._settings_provider()
        groups: list[tuple[str, list[str]]] = []
        for name in self._known_systems_provider():
            customer = str(display_customer_name(settings, name) or "")
            if groups and groups[-1][0] == customer:
                groups[-1][1].append(name)
            else:
                groups.append((customer, [name]))
        return groups

    def _open_filter(self):
        self._filter_dirty = False
        popup = SystemFilterPopup(self._groups(), self._hidden, on_change=self._on_toggle, on_all=self._on_all, parent=self, on_closed=self._on_filter_closed)
        popup.move(self.filter_btn.mapToGlobal(QPoint(0, self.filter_btn.height())))
        popup.show()

    def _on_toggle(self, name: str, visible: bool):
        (self._hidden.discard if visible else self._hidden.add)(name)
        self._filter_dirty = True
        update_ui_state({_HIDDEN_KEY: sorted(self._hidden)})
        self._refresh_labels()

    def _on_all(self, visible: bool):
        if visible:
            self._hidden.clear()
        else:
            self._hidden.update(self._known_systems_provider())
        self._filter_dirty = True
        update_ui_state({_HIDDEN_KEY: sorted(self._hidden)})
        self._refresh_labels()

    def _on_filter_closed(self):
        if self._filter_dirty:
            self._filter_dirty = False
            self.start()

    def _selected_robots(self) -> set[str] | None:
        if not self._hidden:
            return None
        robots = set()
        for name in self._known_systems_provider():
            if name not in self._hidden:
                robot = robot_id_from_folder(name)
                if robot:
                    robots.add(robot)
        return robots

    # ---- loading -----------------------------------------------------------

    def start_if_needed(self):
        if not self._started:
            self.start()

    def start(self):
        self._started = True
        settings = self._settings_provider()
        start, end = self._day_range
        robots = self._selected_robots()
        self._progress.show()
        self._status.setText("Querying Elastic...")
        self._slot.start(
            lambda job: fetch_errors_stops(settings, start, end, robots, progress=job.emit_progress),
            on_result=self._on_result,
            on_error=lambda m: self._status.setText(f"Failed: {m}"),
            on_progress=lambda m: self._status.setText(str(m or "")),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self):
        self._slot.shutdown()
        self._extend_slot.shutdown()

    # ---- scrolling and extending the range ---------------------------------

    def _on_chart_scrolled(self, offset: int, maximum: int, page: int):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        try:
            self._scrollbar.setRange(0, maximum)
            self._scrollbar.setPageStep(max(1, page))
            self._scrollbar.setSingleStep(max(1, page // 10))
            self._scrollbar.setValue(offset)
            self._scrollbar.setVisible(maximum > 0)
            for chart in (self._stops_chart, self._errors_chart):
                if chart is not self.sender():
                    chart.set_offset(offset)
        finally:
            self._syncing_scroll = False

    def _on_scrollbar(self, value: int):
        if self._syncing_scroll:
            return
        self._syncing_scroll = True
        try:
            for chart in (self._stops_chart, self._errors_chart):
                chart.set_offset(value)
        finally:
            self._syncing_scroll = False

    def _on_edge(self, direction: str):
        if self._data is None or self._extending is not None or self._slot.is_running():
            return
        start, end = self._day_range
        today = datetime.now().date()
        if direction == "older":
            room = MAX_RANGE_DAYS - ((end - start).days + 1)
            if room <= 0:
                self._status.setText(f"Range is already {MAX_RANGE_DAYS} days")
                return
            chunk_start = start - timedelta(days=min(7, room))
            chunk = (chunk_start, start - timedelta(days=1))
        else:
            if end >= today:
                return
            chunk = (end + timedelta(days=1), min(today, end + timedelta(days=7)))
        self._extending = chunk
        self._pending = set(day_list(*chunk))
        self._day_range = (min(start, chunk[0]), max(end, chunk[1]))
        self._refresh_labels()
        self._render()
        if direction == "older":
            # Keep the days that were on screen where they were.
            shift = len(self._pending) * self._stops_chart.slot_width()
            for chart in (self._stops_chart, self._errors_chart):
                chart.set_offset(chart.offset() + shift)
        else:
            for chart in (self._stops_chart, self._errors_chart):
                chart.scroll_to_end()
        settings = self._settings_provider()
        robots = self._selected_robots()
        self._progress.show()
        self._status.setText(f"Loading {chunk[0]:%d/%m} – {chunk[1]:%d/%m}...")
        self._extend_slot.start(
            lambda job: fetch_errors_stops(settings, chunk[0], chunk[1], robots, progress=job.emit_progress),
            on_result=self._on_extended,
            on_error=self._on_extend_error,
            on_progress=lambda m: self._status.setText(str(m or "")),
            on_finished=self._on_extend_finished,
        )

    def _on_extended(self, chunk: ErrorsStopsData):
        if self._data is None:
            self._data = chunk
        else:
            self._data.merge(chunk)
        self._pending = set()
        self._render()

    def _on_extend_error(self, message: str):
        self._status.setText(f"Failed to load more days: {message}")
        if self._extending is not None:
            chunk = self._extending
            self._pending = set()
            # Drop the failed chunk from the range again.
            start, end = self._day_range
            if chunk[1] < start + timedelta(days=(end - start).days) and chunk[0] == start:
                self._day_range = (chunk[1] + timedelta(days=1), end)
            elif chunk[1] == end:
                self._day_range = (start, chunk[0] - timedelta(days=1))
            self._refresh_labels()
            self._render()

    def _on_extend_finished(self):
        self._extending = None
        self._progress.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    # ---- rendering ---------------------------------------------------------

    def _system_label(self, robot: str) -> str:
        for name in self._known_systems_provider():
            if robot_id_from_folder(name) == robot:
                return name
        return system_display_name(robot)

    def _on_result(self, data: ErrorsStopsData):
        self._data = data
        self._pending = set()
        self._render()
        for chart in (self._stops_chart, self._errors_chart):
            chart.scroll_to_end()

    def _render(self):
        data = self._data
        if data is None:
            return
        days = sorted(set(data.days) | self._pending)
        loaded_days = data.days
        stops = data.stop_series()
        errors = data.error_series()
        total_stops = sum(sum(v.values()) for v in stops.values())
        total_errors = sum(sum(v.values()) for v in errors.values())
        n_days = max(1, len(loaded_days))
        self._stops_value.setText(f"{total_stops:,}")
        self._stops_sub.setText(f"{total_stops / n_days:.1f} per day · " + " · ".join(f"{k.lower()} {sum(v.values()):,}" for k, v in stops.items() if sum(v.values())))
        self._errors_value.setText(f"{total_errors:,}")
        top = sorted(((sum(v.values()), k) for k, v in errors.items() if sum(v.values())), reverse=True)[:3]
        self._errors_sub.setText(f"{total_errors / n_days:.0f} per day · " + " · ".join(f"{k} {n:,}" for n, k in top))
        fmt = lambda v: f"{int(round(v)):,}"
        # One bar per system, the same colour and slot in every day.
        robots = self._ordered_robots(set(data.system_series("stops")) | set(data.system_series("errors")))
        colours = {robot: _category_colour(i) for i, robot in enumerate(robots)}
        self._label_to_robot = {self._system_label(r): r for r in robots}
        legend = "&nbsp;&nbsp;".join(
            f'<span style="background-color:{colours[r].name()};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{self._system_label(r)}'
            for r in robots
        )
        # Enough width for every system's bar to be seen; longer ranges
        # scroll instead of squeezing (Chris, 2026-09-05).
        min_slot = max(44.0, len(robots) * 5.0 + 12.0)
        for table, chart, legend_label, empty in (
            ("stops", self._stops_chart, self._stops_legend, "No stoppages in this range"),
            ("errors", self._errors_chart, self._errors_legend, "No errors in this range"),
        ):
            per_system = data.system_series(table)
            series = [
                (self._system_label(r), colours[r], {d: float(n) for d, n in per_system.get(r, {}).items()})
                for r in robots
            ]
            chart.set_min_slot(min_slot)
            chart.set_pending(self._pending)
            chart.set_data(days, series, fmt, empty_text=empty)
            legend_label.setText(legend)
        self._fill_table(data)
        if not self._pending:
            self._status.setText(f"{len(loaded_days)} day{'s' if len(loaded_days) != 1 else ''} · {total_stops:,} stoppages · {total_errors:,} errors")

    def _ordered_robots(self, robots: set[str]) -> list[str]:
        """Display order of the share listing; unknown robots after."""
        known = self._known_systems_provider()
        rank = {robot_id_from_folder(name): i for i, name in enumerate(known)}
        return sorted(robots, key=lambda r: (rank.get(r, len(rank)), r))

    def _fill_table(self, data: ErrorsStopsData):
        per = data.per_system()
        settings = self._settings_provider()
        rows = sorted(per.items(), key=lambda kv: (-kv[1]["stops"], -kv[1]["errors"]))
        table = self._table
        table.clear()
        table.setColumnCount(5)
        table.setRowCount(len(rows))
        table.setHorizontalHeaderLabels(["System", "Customer", "Stoppages", "Errors", "Most common error"])
        for r, (robot, entry) in enumerate(rows):
            name = self._system_label(robot)
            top_state, top_n = entry["top_error"]
            values = [name, display_customer_name(settings, name.split(" ")[0]), f"{entry['stops']:,}", f"{entry['errors']:,}",
                      f"{top_state} ({top_n:,})" if top_state else "–"]
            for c, text in enumerate(values):
                item = QTableWidgetItem(text)
                if c in (2, 3):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
        table.resizeRowsToContents()

    def _detail(self, table: str, label: str, day: date) -> str:
        """One system on one day: total, then by kind (stops) or category
        (errors), then the top states."""
        if self._data is None:
            return label
        robot = self._label_to_robot.get(label, label)
        states = self._data.system_day_states(table, robot, day)
        total = sum(states.values())
        noun = "stoppages" if table == "stops" else "errors"
        lines = [f"<b>{label}</b> — {day:%A %d/%m/%Y}: {total:,} {noun}"]
        groups: dict[str, int] = {}
        for state, n in states.items():
            key = (stop_kind(state) or "Other") if table == "stops" else categorize_error(state)
            groups[key] = groups.get(key, 0) + n
        for key, n in sorted(groups.items(), key=lambda kv: -kv[1]):
            lines.append(f"{key}: {n:,}")
        top = sorted(states.items(), key=lambda kv: -kv[1])[:4]
        if top and table == "errors":
            lines.append("<i>" + ", ".join(f"{s} {n:,}" for s, n in top) + "</i>")
        return "<br>".join(lines)
