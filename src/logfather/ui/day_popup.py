"""A single-day calendar popup for the viewer's Choose date button
(Chris, 2026-09-05): days with footage are highlighted, the current day
is marked, the future is off limits. Emits the chosen day and hides."""
from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QTextCharFormat
from PySide6.QtWidgets import QCalendarWidget, QLabel, QVBoxLayout, QWidget

from logfather.ui import theme


class DayPopup(QWidget):
    day_chosen = Signal(object)  # date

    def __init__(self, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setStyleSheet(
            f"QWidget {{ background-color: {theme.BG_RAISED}; }}"
            f"QLabel {{ color: {theme.TEXT}; background: transparent; }}"
        )
        self._available: set[date] = set()
        self._selected: date | None = None
        self._formatted: set[date] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        self._title = QLabel("Choose a day")
        self._title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: bold;")
        layout.addWidget(self._title)
        self._hint = QLabel("")
        self._hint.setStyleSheet(theme.MUTED_LABEL)
        layout.addWidget(self._hint)
        self._calendar = QCalendarWidget()
        self._calendar.setGridVisible(True)
        self._calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self._calendar.setMaximumDate(QDate.currentDate())
        self._calendar.clicked.connect(self._on_clicked)
        self._calendar.currentPageChanged.connect(lambda _y, _m: self._refresh())
        layout.addWidget(self._calendar)
        base = self._calendar.weekdayTextFormat(Qt.Monday)
        self._normal_fmt = QTextCharFormat(base)
        self._available_fmt = QTextCharFormat(base)
        self._available_fmt.setBackground(QBrush(QColor(theme.ACCENT_DIM)))
        self._available_fmt.setForeground(QBrush(QColor(theme.TEXT_BRIGHT)))
        self._available_fmt.setFontWeight(QFont.Bold)
        self._selected_fmt = QTextCharFormat(base)
        self._selected_fmt.setBackground(QBrush(QColor(theme.ACCENT)))
        self._selected_fmt.setForeground(QBrush(QColor("#081018")))
        self._selected_fmt.setFontWeight(QFont.Bold)

    def open_for(
        self,
        title: str,
        available: set[date],
        selected: date | None,
        anchor: QPoint,
        scanning: bool = False,
    ) -> None:
        self._title.setText(title or "Choose a day")
        self._available = set(available or ())
        self._selected = selected
        if scanning:
            self._hint.setText("Still listing the share - highlighted days may be incomplete")
        elif self._available:
            self._hint.setText(f"{len(self._available)} days with footage are highlighted")
        else:
            self._hint.setText("No footage found for this system yet")
        self._calendar.setMaximumDate(QDate.currentDate())
        target = selected or (max(self._available) if self._available else date.today())
        self._calendar.setCurrentPage(target.year, target.month)
        self._calendar.setSelectedDate(QDate(target.year, target.month, target.day))
        self._refresh()
        self.adjustSize()
        self.move(anchor)
        self.show()

    def _refresh(self) -> None:
        for day in self._formatted:
            self._calendar.setDateTextFormat(QDate(day.year, day.month, day.day), QTextCharFormat())
        self._formatted = set()
        year, month = self._calendar.yearShown(), self._calendar.monthShown()
        for day in self._available:
            if abs((day.year - year) * 12 + (day.month - month)) <= 1:
                self._calendar.setDateTextFormat(QDate(day.year, day.month, day.day), self._available_fmt)
                self._formatted.add(day)
        if self._selected is not None:
            self._calendar.setDateTextFormat(
                QDate(self._selected.year, self._selected.month, self._selected.day), self._selected_fmt
            )
            self._formatted.add(self._selected)

    def _on_clicked(self, qd: QDate) -> None:
        day = date(qd.year(), qd.month(), qd.day())
        if day > date.today():
            return
        self.hide()
        self.day_chosen.emit(day)
