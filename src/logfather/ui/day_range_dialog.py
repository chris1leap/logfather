"""Shared From/To day-range picker with quick presets and span highlighting
(Overview's Choose days..., Errors / Stops window)."""
from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate
from PySide6.QtGui import QBrush, QColor, QPalette, QTextCharFormat
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from logfather.ui import theme

MAX_RANGE_DAYS = 365


class DayRangeDialog(QDialog):
    """From/To day pickers as plain calendars (Chris, 2026-09-05: one
    button opens this; simplest possible range selection)."""

    def __init__(self, initial: tuple[date, date], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Show data for days")
        self._highlighted: list[QDate] = []
        today = QDate.currentDate()
        layout = QVBoxLayout(self)
        # Quick presets above the calendars (Chris, 2026-09-05); each is
        # a span ending today, counted inclusively.
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Quick:"))
        for label, days in (
            ("Last 7 days", 7),
            ("Last month", 30),
            ("Last 3 months", 90),
            ("Last year", 365),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(lambda _checked=False, n=days: self._apply_preset(n))
            preset_row.addWidget(btn)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)
        cal_row = QHBoxLayout()
        cal_row.setSpacing(14)
        self._from_cal = QCalendarWidget()
        self._to_cal = QCalendarWidget()
        self._from_value = QLabel("")
        self._to_value = QLabel("")
        for cal, value_label, label_text, day_value in (
            (self._from_cal, self._from_value, "From", initial[0]),
            (self._to_cal, self._to_value, "To", initial[1]),
        ):
            cal.setGridVisible(True)
            # Future days are greyed out and unclickable, and the month
            # navigation cannot pass the current month (maximumDate).
            cal.setMinimumDate(today.addDays(-(MAX_RANGE_DAYS - 1)))
            cal.setMaximumDate(today)
            cal.setSelectedDate(QDate(day_value.year, day_value.month, day_value.day))
            # Endpoints (the calendar's own selection) in the bright accent
            # so they read distinctly from the ACCENT_DIM span fill below.
            pal = cal.palette()
            pal.setColor(QPalette.Highlight, QColor(theme.ACCENT))
            pal.setColor(QPalette.HighlightedText, QColor("#081018"))
            cal.setPalette(pal)
            column = QVBoxLayout()
            title_row = QHBoxLayout()
            title = QLabel(label_text)
            title.setStyleSheet("font-weight: bold;")
            value_label.setStyleSheet(f"color: {theme.ACCENT};")
            title_row.addWidget(title)
            title_row.addWidget(value_label)
            title_row.addStretch(1)
            column.addLayout(title_row)
            column.addWidget(cal)
            cal_row.addLayout(column)
        # A From after the current To drags To along with it.
        self._from_cal.clicked.connect(self._on_from_picked)
        self._from_cal.selectionChanged.connect(self._on_from_changed)
        self._to_cal.selectionChanged.connect(self._refresh_labels)
        layout.addLayout(cal_row)
        bottom_row = QHBoxLayout()
        self._total_label = QLabel("")
        self._total_label.setStyleSheet("font-weight: bold;")
        bottom_row.addWidget(self._total_label)
        bottom_row.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom_row.addWidget(buttons)
        layout.addLayout(bottom_row)
        self._refresh_labels()

    def _apply_preset(self, days: int):
        today = QDate.currentDate()
        self._to_cal.setSelectedDate(today)
        self._from_cal.setSelectedDate(today.addDays(-(days - 1)))
        self._refresh_labels()

    def _on_from_picked(self, qdate: QDate):
        if self._to_cal.selectedDate() < qdate:
            self._to_cal.setSelectedDate(qdate)
        self._refresh_labels()

    def _on_from_changed(self):
        self._on_from_picked(self._from_cal.selectedDate())

    def _refresh_labels(self):
        self._from_value.setText(
            self._from_cal.selectedDate().toPython().strftime("%d/%m/%Y")
        )
        self._to_value.setText(
            self._to_cal.selectedDate().toPython().strftime("%d/%m/%Y")
        )
        d1, d2 = self.selected_range()
        total = (d2 - d1).days + 1
        self._total_label.setText(
            f"Total: {total} day" + ("s" if total != 1 else "")
        )
        self._refresh_range_highlight(d1, d2)

    def _refresh_range_highlight(self, d1: date, d2: date):
        """Fill every day of the span in both calendars (Chris,
        2026-09-05); at most 61 days, so re-painting the lot is cheap."""
        blank = QTextCharFormat()
        for cal in (self._from_cal, self._to_cal):
            for qd in self._highlighted:
                cal.setDateTextFormat(qd, blank)
        self._highlighted = []
        span_fmt = QTextCharFormat()
        span_fmt.setBackground(QBrush(QColor(theme.ACCENT_DIM)))
        span_fmt.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
        day = d1
        while day <= d2:
            qd = QDate(day.year, day.month, day.day)
            for cal in (self._from_cal, self._to_cal):
                cal.setDateTextFormat(qd, span_fmt)
            self._highlighted.append(qd)
            day += timedelta(days=1)

    def selected_range(self) -> tuple[date, date]:
        d1 = self._from_cal.selectedDate().toPython()
        d2 = self._to_cal.selectedDate().toPython()
        return (min(d1, d2), max(d1, d2))
