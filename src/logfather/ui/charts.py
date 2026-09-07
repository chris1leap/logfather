"""Shared painted charts: a stacked per-day bar chart with hover details
and an optional click handler (Data window, Errors / Stops window)."""
from __future__ import annotations

import math
import time
from datetime import date
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QMenu, QSizePolicy, QToolTip, QWidget

from logfather.ui import theme

DetailFn = Callable[[str, date], str]


def day_heading(day: date) -> str:
    """'Mon 18th' - the label above each day's cluster (Chris, 2026-09-06)."""
    n = day.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{day:%a} {n}{suffix}"


class StackedBarChart(QWidget):
    """One stacked bar per day, a segment per system; hover a segment for
    that system/day's details (Chris, 2026-09-05)."""

    # Horizontal scrolling (Chris, 2026-09-05): with a minimum slot width
    # set, a long range no longer squeezes onto one screen; the chart
    # reports its scroll state for an external scrollbar and says when
    # the wheel pushes past either end so more days can be loaded.
    scroll_changed = Signal(int, int, int)  # offset, maximum, page
    edge_reached = Signal(str)  # "older" | "newer"
    # Labels (Chris, 2026-09-07): a segment can carry a tag naming its
    # system, added or removed from its right-click menu. Emits the whole
    # set of (name, day) pairs whenever it changes, for the owner to keep.
    labels_changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(320)
        self._min_slot = 0.0
        self._offset = 0.0
        # (name, day) -> (dx, dy): the tag's drag offset from its default
        # place (Chris, 2026-09-07: labels can be moved once added).
        self._labels: dict[tuple[str, date], tuple[float, float]] = {}
        self._labels_enabled = False
        self._label_rects: list[tuple[QRectF, tuple[str, date]]] = []
        self._drag_label: tuple[str, date] | None = None
        self._drag_start: QPointF | None = None
        self._drag_origin: tuple[float, float] = (0.0, 0.0)
        self._pending: set[date] = set()
        self._last_edge = 0.0
        self._last_scroll_state: tuple[int, int, int] | None = None
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
        # Grouped mode (Chris, 2026-09-05: Errors / Stops): one bar per
        # series side by side inside each day, so one system standing out
        # or climbing is obvious. Each series keeps its slot even at zero.
        self._grouped = False

    def set_grouped(self, grouped: bool) -> None:
        self._grouped = bool(grouped)
        self.update()

    def set_slot_width(self, px: float) -> None:
        """Fixed width per day; 0 fits every day on screen. Fixed means
        loading more days scrolls rather than shrinking the bars (Chris,
        2026-09-06: the wheel must scroll, never look like a zoom)."""
        self._min_slot = max(0.0, float(px))
        self.update()

    def set_pending(self, days: set[date]) -> None:
        """Days drawn as loading placeholders (no bars yet)."""
        self._pending = set(days or ())
        self.update()

    def _plot_width(self) -> float:
        return max(1.0, float(self.rect().width() - 76 - 16))

    def slot_width(self) -> float:
        if self._min_slot > 0:
            return self._min_slot
        return self._plot_width() / max(1, len(self._days))

    def nudge(self, fraction: float) -> None:
        """Scroll by a fraction of the visible width (negative = older),
        landing on a whole day and moving at least one day (Chris,
        2026-09-06: never stop halfway through a day); at an end, ask for
        more days instead."""
        before = self._offset
        target = self._snap(self._offset + fraction * self._plot_width())
        if abs(target - before) < 0.5 and fraction:
            target = self._snap(before + (self.slot_width() if fraction > 0 else -self.slot_width()))
        self.set_offset(target)
        if abs(self._offset - before) < 0.5:
            now = time.monotonic()
            if now - self._last_edge > 0.7:
                self._last_edge = now
                self.edge_reached.emit("newer" if fraction > 0 else "older")
        self._emit_scroll_state()

    def max_offset(self) -> float:
        return max(0.0, self.slot_width() * len(self._days) - self._plot_width())

    def offset(self) -> float:
        return self._offset

    def _snap(self, px: float) -> float:
        """The nearest day boundary; the very end (latest day flush right)
        is allowed as it is."""
        limit = self.max_offset()
        px = min(max(0.0, float(px)), limit)
        slot = self.slot_width()
        if slot <= 0 or px >= limit - 0.5:
            return px
        return min(limit, round(px / slot) * slot)

    def set_offset(self, px: float) -> None:
        new = self._snap(px)
        if abs(new - self._offset) > 0.5:
            self._offset = new
            self.update()

    def scroll_to_end(self) -> None:
        self.set_offset(self.max_offset())

    def _emit_scroll_state(self) -> None:
        state = (int(self._offset), int(self.max_offset()), int(self._plot_width()))
        if state != self._last_scroll_state:
            self._last_scroll_state = state
            self.scroll_changed.emit(*state)

    def wheelEvent(self, event):
        if self._min_slot <= 0 or not self._days:
            super().wheelEvent(event)
            return
        delta = event.angleDelta().x() or event.angleDelta().y()
        if delta == 0:
            return
        # Wheel down / right moves to later days; never a zoom.
        event.accept()
        self.nudge(-0.2 if delta > 0 else 0.2)

    def set_detail_provider(self, fn: DetailFn | None) -> None:
        self._detail_fn = fn

    def set_labels_enabled(self, enabled: bool) -> None:
        self._labels_enabled = bool(enabled)

    def set_labels(self, labels) -> None:
        """Accepts (name, day) pairs or (name, day, dx, dy) with a drag
        offset, or a {(name, day): (dx, dy)} mapping."""
        out: dict[tuple[str, date], tuple[float, float]] = {}
        items = labels.items() if isinstance(labels, dict) else [(entry, None) for entry in (labels or ())]
        for entry, offset in items:
            entry = tuple(entry)
            if len(entry) < 2 or not isinstance(entry[1], date):
                continue
            if offset is None:
                offset = (float(entry[2]), float(entry[3])) if len(entry) >= 4 else (0.0, 0.0)
            out[(str(entry[0]), entry[1])] = (float(offset[0]), float(offset[1]))
        self._labels = out
        self.update()

    def labels(self) -> dict[tuple[str, date], tuple[float, float]]:
        return dict(self._labels)

    def has_label(self, name: str, day: date) -> bool:
        return (name, day) in self._labels

    def toggle_label(self, name: str, day: date) -> None:
        key = (name, day)
        if key in self._labels:
            self._labels.pop(key)
        else:
            self._labels[key] = (0.0, 0.0)
        self.update()
        self.labels_changed.emit(dict(self._labels))

    def _label_at(self, pos) -> tuple[str, date] | None:
        for rect, key in self._label_rects:
            if rect.contains(pos):
                return key
        return None

    def mouseReleaseEvent(self, event):
        if self._drag_label is not None:
            self._drag_label = None
            self._drag_start = None
            self.labels_changed.emit(dict(self._labels))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):
        if not self._labels_enabled:
            super().contextMenuEvent(event)
            return
        pos = event.pos()
        for rect, name, day in self._segments:
            if rect.contains(pos):
                menu = QMenu(self)
                action = menu.addAction("Remove label" if self.has_label(name, day) else f"Add label: {name}")
                action.triggered.connect(lambda _checked=False, n=name, d=day: self.toggle_label(n, d))
                menu.exec(event.globalPos())
                event.accept()
                return
        super().contextMenuEvent(event)

    def _paint_labels(self, painter: QPainter, plot_top: float) -> None:
        """A tag above each labelled segment's bar with a leader line down
        to the segment; several labels on one day stack upwards."""
        if not self._labels:
            return
        self._label_rects = []
        by_day: dict[date, list[tuple[QRectF, str]]] = {}
        bar_top: dict[date, float] = {}
        for rect, name, day in self._segments:
            bar_top[day] = min(bar_top.get(day, rect.top()), rect.top())
            if (name, day) in self._labels:
                by_day.setdefault(day, []).append((rect, name))
        font = QFont(self.font().family(), max(8, self.font().pointSize() - 2))
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        for day, items in by_day.items():
            top = bar_top.get(day, plot_top)
            for i, (rect, name) in enumerate(items):
                w = metrics.horizontalAdvance(name) + 12
                h = metrics.height() + 4
                cx = rect.center().x()
                y = top - 22 - i * (h + 4)
                if y - h >= 2:
                    tag = QRectF(cx - w / 2, y - h, w, h)
                else:
                    # No room above a tall bar: hang the tag beside it.
                    tag = QRectF(rect.right() + 8, plot_top + 4 + i * (h + 4), w, h)
                dx, dy = self._labels.get((name, day), (0.0, 0.0))
                tag.translate(dx, dy)
                # Keep the tag inside the widget however far it was dragged.
                tag.moveLeft(min(max(tag.left(), 2.0), max(2.0, self.width() - w - 2)))
                tag.moveTop(min(max(tag.top(), 2.0), max(2.0, self.height() - h - 2)))
                self._label_rects.append((tag, (name, day)))
                target = QPointF(cx, rect.center().y())
                anchor = self._edge_point(tag, target)
                painter.setPen(QPen(QColor(theme.ACCENT)))
                painter.drawLine(anchor, target)
                painter.setBrush(QBrush(QColor(theme.ACCENT)))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(tag, 4, 4)
                painter.setPen(QColor("#081018"))
                painter.drawText(tag, Qt.AlignCenter, name)

    @staticmethod
    def _edge_point(tag: QRectF, target: QPointF) -> QPointF:
        """Where the line from the tag's centre to the target leaves the
        tag, so the leader starts on its edge wherever it was dragged."""
        c = tag.center()
        dx, dy = target.x() - c.x(), target.y() - c.y()
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return c
        hw, hh = tag.width() / 2, tag.height() / 2
        tx = hw / abs(dx) if dx else float("inf")
        ty = hh / abs(dy) if dy else float("inf")
        t = min(tx, ty, 1.0)
        return QPointF(c.x() + dx * t, c.y() + dy * t)

    def set_click_handler(self, fn: Callable[[str, date], None] | None) -> None:
        self._click_fn = fn
        if fn is None:
            self.unsetCursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._labels:
            key = self._label_at(event.position())
            if key is not None:
                self._drag_label = key
                self._drag_start = event.position()
                self._drag_origin = self._labels.get(key, (0.0, 0.0))
                self.setCursor(Qt.ClosedHandCursor)
                event.accept()
                return
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
        if self._drag_label is not None and self._drag_start is not None:
            dx = self._drag_origin[0] + (pos.x() - self._drag_start.x())
            dy = self._drag_origin[1] + (pos.y() - self._drag_start.y())
            self._labels[self._drag_label] = (dx, dy)
            self.update()
            event.accept()
            return
        if self._labels and self._label_at(pos) is not None:
            self.setCursor(Qt.OpenHandCursor)
            QToolTip.showText(event.globalPosition().toPoint(), "Drag to move this label", self)
            return
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
        left, right, top, bottom = 76, 16, 30, 50  # two axis lines: day, month
        if self._grouped:
            top = 52  # room for the month / year line above the day headings
        plot_left = rect.left() + left
        plot_right = rect.right() - right
        plot_top = rect.top() + top
        plot_bottom = rect.bottom() - bottom
        plot_w = max(1, plot_right - plot_left)
        plot_h = max(1, plot_bottom - plot_top)
        totals = [
            sum(values.get(day, 0.0) for _n, _c, values in self._series) for day in self._days
        ]
        if not self._days or (not self._pending and (not self._series or max(totals, default=0) <= 0)):
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(rect, Qt.AlignCenter, self._empty_text)
            painter.end()
            self._emit_scroll_state()
            return
        if self._grouped:
            vmax = max(
                float(values.get(day, 0.0)) for _n, _c, values in self._series for day in self._days
            )
        else:
            vmax = max(totals)
        vmax = max(vmax, 1e-9)
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
        slot = self.slot_width()
        self._offset = min(self._offset, self.max_offset())
        origin = plot_left - self._offset
        painter.setClipRect(QRectF(plot_left, rect.top(), plot_w + 1, rect.height()))
        self._paint_pending(painter, origin, plot_top, plot_bottom, slot)
        if self._grouped:
            self._paint_grouped(painter, origin, plot_bottom, plot_h, slot, vmax, totals)
            painter.setClipping(False)
            painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
            painter.drawLine(plot_left, plot_bottom, plot_right, plot_bottom)
            painter.end()
            self._emit_scroll_state()
            return
        bar_w = max(4.0, slot * 0.62)
        for i, day in enumerate(self._days):
            x = origin + i * slot + (slot - bar_w) / 2
            if x + slot < plot_left or x - slot > plot_right:
                continue
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
        self._paint_day_axis(painter, origin, slot, plot_bottom, plot_left, plot_right)
        self._paint_labels(painter, plot_top)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
        painter.drawLine(plot_left, plot_bottom, plot_right, plot_bottom)
        painter.end()
        self._emit_scroll_state()

    def _paint_pending(self, painter, origin, plot_top, plot_bottom, slot) -> None:
        if not self._pending:
            return
        brush = QBrush(QColor(theme.BORDER_LIGHT), Qt.BDiagPattern)
        for i, day in enumerate(self._days):
            if day not in self._pending:
                continue
            x = origin + i * slot
            column = QRectF(x + 2, plot_top, max(1.0, slot - 4), plot_bottom - plot_top)
            painter.setPen(Qt.NoPen)
            painter.setBrush(brush)
            painter.drawRect(column)
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(column, Qt.AlignCenter, "loading" if slot >= 44 else "…")

    def _month_runs(self) -> list[tuple[int, int]]:
        runs: list[tuple[int, int]] = []
        for i, day in enumerate(self._days):
            if runs and (self._days[runs[-1][0]].year, self._days[runs[-1][0]].month) == (day.year, day.month):
                runs[-1] = (runs[-1][0], i)
            else:
                runs.append((i, i))
        return runs

    def _paint_day_axis(self, painter, origin, slot, plot_bottom, visible_left: float, visible_right: float, months: bool = True) -> None:
        """Two axis lines (Chris, 2026-09-07: dd/mm per bar crowded once
        past 14 days): the day number under each bar, thinned when bars
        are narrow, and the month name once per run of days, kept inside
        the visible part of the run."""
        small = QFont(self.font().family(), max(8, self.font().pointSize() - 2))
        painter.setFont(small)
        every = max(1, int(math.ceil(24.0 / max(1.0, slot))))
        painter.setPen(QColor(theme.TEXT_MUTED))
        for i, day in enumerate(self._days):
            x = origin + i * slot
            if x + slot < visible_left or x > visible_right:
                continue
            if i % every:
                continue
            painter.drawText(QRectF(x - slot, plot_bottom + 4, slot * 3, 16), Qt.AlignHCenter | Qt.AlignTop, str(day.day))
        if not months:
            return
        for start, end in self._month_runs():
            x0 = origin + start * slot
            x1 = origin + (end + 1) * slot
            lo, hi = max(x0, visible_left), min(x1, visible_right)
            if hi - lo < 8:
                continue
            painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
            if x0 >= visible_left:
                painter.drawLine(int(x0), int(plot_bottom + 2), int(x0), int(plot_bottom + 34))
            painter.setPen(QColor(theme.TEXT))
            month = self._days[start]
            label = month.strftime("%B %Y") if hi - lo >= 130 else (month.strftime("%b %Y") if hi - lo >= 60 else month.strftime("%b"))
            painter.drawText(QRectF(lo, plot_bottom + 20, hi - lo, 16), Qt.AlignCenter, label)

    def _paint_month_line(self, painter, origin, slot, y_top: float, visible_left: float, visible_right: float) -> None:
        """'September 2026' over each run of days in one month, kept
        inside the visible part of the run while scrolling; a faint tick
        marks each month boundary (Chris, 2026-09-06)."""
        runs = self._month_runs()
        bold = QFont(painter.font())
        bold.setBold(True)
        for start, end in runs:
            x0 = origin + start * slot
            x1 = origin + (end + 1) * slot
            lo, hi = max(x0, visible_left), min(x1, visible_right)
            if hi - lo < 10:
                continue
            painter.setPen(QPen(QColor(theme.BORDER_LIGHT)))
            if x0 >= visible_left:
                painter.drawLine(int(x0), int(y_top), int(x0), int(y_top + 18))
            painter.setFont(bold)
            painter.setPen(QColor(theme.TEXT_BRIGHT))
            label = self._days[start].strftime("%B %Y") if hi - lo >= 120 else self._days[start].strftime("%b %Y")
            painter.drawText(QRectF(lo, y_top, hi - lo, 18), Qt.AlignCenter, label)
        painter.setFont(QFont(self.font().family(), max(8, self.font().pointSize() - 2)))

    def _paint_grouped(self, painter, origin, plot_bottom, plot_h, slot, vmax, totals) -> None:
        k = max(1, len(self._series))
        cluster_w = slot * 0.8
        gap = 1.0 if cluster_w / k > 4 else 0.0
        bar_w = max(1.5, cluster_w / k - gap)
        visible_left = origin + self._offset
        visible_right = visible_left + self._plot_width()
        self._paint_month_line(painter, origin, slot, plot_bottom - plot_h - 46, visible_left, visible_right)
        for i, day in enumerate(self._days):
            x0 = origin + i * slot + (slot - cluster_w) / 2
            if x0 + slot < visible_left or x0 - slot > visible_right:
                continue
            if day in self._pending:
                continue
            for j, (name, colour, values) in enumerate(self._series):
                value = float(values.get(day, 0.0))
                x = x0 + j * (bar_w + gap)
                if value <= 0:
                    continue
                h = max(1.0, value / vmax * plot_h)
                seg = QRectF(x, plot_bottom - h, bar_w, h)
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
            # The day, not the total, heads each cluster (Chris, 2026-09-06).
            if slot >= 34:
                painter.setPen(QColor(theme.TEXT))
                painter.drawText(
                    QRectF(x0 - slot / 2, plot_bottom - plot_h - 22, cluster_w + slot, 18),
                    Qt.AlignHCenter | Qt.AlignBottom,
                    day_heading(day) if slot >= 64 else f"{day:%a} {day.day}",
                )
        self._paint_day_axis(painter, origin, slot, plot_bottom, visible_left, visible_right, months=False)
        self._paint_labels(painter, plot_bottom - plot_h)
