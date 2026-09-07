"""Shared sideways scrolling for the day charts (Errors / Stops, Data):
one scrollbar and one locked width-per-day across several charts, the
arrow buttons that step back / forward in time, the zoom + / - that
change the width per day, and an edge signal for loading more days
(Chris, 2026-09-06 / 07)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QSize, Qt, Signal
from PySide6.QtWidgets import QScrollBar, QSizePolicy, QToolButton

from logfather.ui import theme
from logfather.ui.charts import StackedBarChart
from logfather.ui.icons import arrow_icon, zoom_glyph_icon

ZOOM_STEP = 1.25
NUDGE_FRACTION = 0.2


class ChartScroller(QObject):
    edge_reached = Signal(str)  # "older" | "newer"

    def __init__(self, charts: list[StackedBarChart], rerender: Callable[[], None], parent=None):
        super().__init__(parent)
        self._charts = list(charts)
        self._rerender = rerender
        self._syncing = False
        # Width per day in pixels: a freshly loaded range is fitted to the
        # screen and then locked, so loading more days scrolls rather than
        # shrinking the bars; only zoom changes it. None = fit next time.
        self.slot_px: float | None = None
        self._n_series = 1
        self.scrollbar = QScrollBar(Qt.Horizontal)
        self.scrollbar.setToolTip("Scroll through the days; keep going past the end to load more")
        self.scrollbar.valueChanged.connect(self._on_scrollbar)
        for chart in self._charts:
            chart.scroll_changed.connect(self._on_chart_scrolled)
            chart.edge_reached.connect(self.edge_reached.emit)

    # ---- width per day ------------------------------------------------------

    def fit_next(self) -> None:
        self.slot_px = None

    @staticmethod
    def _floor(n_series: int) -> float:
        return n_series * 2.0 + 6.0

    def apply(self, days: list, n_series: int, pending: set) -> float:
        """Lock (or fit) the width per day and push it, with the pending
        days, to every chart. Call before each chart's set_data."""
        self._n_series = max(1, n_series)
        if self.slot_px is None:
            plot_w = max(600.0, self._charts[0]._plot_width())
            self.slot_px = max(self._floor(self._n_series), plot_w / max(1, len(days)))
        for chart in self._charts:
            chart.set_slot_width(self.slot_px)
            chart.set_pending(pending)
        return self.slot_px

    def zoom(self, step: int) -> None:
        if self.slot_px is None:
            return
        factor = ZOOM_STEP if step > 0 else 1.0 / ZOOM_STEP
        floor = self._floor(self._n_series)
        ceiling = max(floor, self._charts[0]._plot_width())
        new = min(ceiling, max(floor, self.slot_px * factor))
        if abs(new - self.slot_px) < 0.5:
            return
        # Keep the day at the centre of the view where it is.
        chart = self._charts[0]
        centre_days = (chart.offset() + chart._plot_width() / 2) / max(1.0, chart.slot_width())
        self.slot_px = new
        self._rerender()
        for c in self._charts:
            c.set_offset(centre_days * new - c._plot_width() / 2)

    # ---- moving ---------------------------------------------------------------

    def nudge(self, fraction: float) -> None:
        self._charts[0].nudge(fraction)

    def scroll_to_end(self) -> None:
        for chart in self._charts:
            chart.scroll_to_end()

    def shift_prepended(self, n_days: int) -> None:
        """After n older days were added at the front, keep the days that
        were on screen where they were."""
        shift = n_days * self._charts[0].slot_width()
        for chart in self._charts:
            chart.set_offset(chart.offset() + shift)

    def _on_chart_scrolled(self, offset: int, maximum: int, page: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self.scrollbar.setRange(0, maximum)
            self.scrollbar.setPageStep(max(1, page))
            self.scrollbar.setSingleStep(max(1, page // 10))
            self.scrollbar.setValue(offset)
            self.scrollbar.setVisible(maximum > 0)
            sender = self.sender()
            for chart in self._charts:
                if chart is not sender:
                    chart.set_offset(offset)
        finally:
            self._syncing = False

    def _on_scrollbar(self, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for chart in self._charts:
                chart.set_offset(value)
        finally:
            self._syncing = False

    # ---- buttons --------------------------------------------------------------

    def arrow_button(self, direction: str, tip: str) -> QToolButton:
        """A tall chevron button for one side of a chart; steps a fifth of
        the view, repeats while held, loads more days at the ends."""
        btn = QToolButton()
        btn.setIcon(arrow_icon(direction, 40))
        btn.setIconSize(QSize(32, 32))
        btn.setToolTip(tip)
        btn.setFixedWidth(42)
        btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        btn.setAutoRepeat(True)
        btn.setAutoRepeatInterval(180)
        btn.setStyleSheet(
            f"QToolButton {{ border: 1px solid {theme.BORDER}; border-radius: 4px; background: {theme.BG_RAISED}; }}"
            f"QToolButton:hover {{ background: {theme.BG_HOVER}; border-color: {theme.ACCENT}; }}"
            f"QToolButton:pressed {{ background: {theme.BORDER}; }}"
        )
        fraction = -NUDGE_FRACTION if direction == "left" else NUDGE_FRACTION
        btn.clicked.connect(lambda _checked=False: self.nudge(fraction))
        return btn

    def zoom_button(self, glyph: str, tip: str, step: int) -> QToolButton:
        btn = QToolButton()
        btn.setIcon(zoom_glyph_icon(glyph))
        btn.setIconSize(QSize(theme.ZOOM_CIRCLE_SIZE - 10, theme.ZOOM_CIRCLE_SIZE - 10))
        btn.setStyleSheet(theme.ZOOM_CIRCLE_BUTTON)
        btn.setFixedSize(theme.ZOOM_CIRCLE_SIZE, theme.ZOOM_CIRCLE_SIZE)
        btn.setToolTip(tip)
        btn.setAutoRepeat(True)
        btn.clicked.connect(lambda _checked=False: self.zoom(step))
        return btn
