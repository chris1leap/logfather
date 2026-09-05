"""Shared painted charts: a stacked per-day bar chart with hover details
and an optional click handler (Data window, Errors / Stops window)."""
from __future__ import annotations

from datetime import date
from typing import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from logfather.ui import theme

DetailFn = Callable[[str, date], str]


class StackedBarChart(QWidget):
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
        # Optional click target for a segment (name, day); the cursor
        # turns into a hand over segments while one is set.
        self._click_fn: Callable[[str, date], None] | None = None
        # Filled during paint: (rect, name, day) per drawn segment.
        self._segments: list[tuple[QRectF, str, date]] = []
        self._hover_index: int | None = None

    def set_detail_provider(self, fn: DetailFn | None) -> None:
        self._detail_fn = fn

    def set_click_handler(self, fn: Callable[[str, date], None] | None) -> None:
        self._click_fn = fn
        if fn is None:
            self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._click_fn is not None:
            pos = event.position()
            for rect, name, day in self._segments:
                if rect.contains(pos):
                    self._click_fn(name, day)
                    event.accept()
                    return
        super().mousePressEvent(event)

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
        if self._click_fn is not None:
            self.setCursor(Qt.PointingHandCursor if hit is not None else Qt.ArrowCursor)
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
