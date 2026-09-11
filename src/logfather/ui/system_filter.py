"""Shared system popups, grouped by customer with a rule between groups
(Chris, 2026-09-05 / 07): SystemFilterPopup ticks many systems on and
off (Overview, Errors / Stops, Data); SystemPickerPopup chooses one
(System Replay). Same shell, same look."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from logfather.ui import theme


def funnel_icon(size: int = 18) -> QIcon:
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


POPUP_STYLE = (
    f"QWidget {{ background-color: {theme.BG_RAISED}; }}"
    f"QLabel {{ color: {theme.TEXT}; }}"
)


def _grouped_body(groups: list[tuple[str, list[str]]], make_row: Callable[[str], QWidget]) -> QScrollArea:
    """The scrolling list shared by both popups: a bold customer heading,
    one row widget per system, a rule between customers."""
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
            rows.addWidget(make_row(system))
    rows.addStretch(1)
    scroll.setWidget(body)
    scroll.setMaximumHeight(560)
    return scroll


class SystemPickerPopup(QWidget):
    """One system, chosen with a click (System Replay's Choose system,
    Chris 2026-09-07: the same box as the Overview's filter). The current
    system is highlighted; on_pick fires with the name and the popup
    closes."""

    def __init__(
        self,
        groups: list[tuple[str, list[str]]],
        current: str | None,
        on_pick: Callable[[str], None],
        parent=None,
        title: str = "Choose system",
    ):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self._on_pick = on_pick
        self.setStyleSheet(POPUP_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        head = QLabel(title)
        head.setStyleSheet(f"font-weight: bold; color: {theme.TEXT_BRIGHT};")
        outer.addWidget(head)

        def make_row(system: str) -> QWidget:
            btn = QPushButton(system)
            btn.setCheckable(True)
            btn.setChecked(system == current)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { text-align: left; padding: 6px 14px; border: 1px solid transparent; background: transparent; }"
                f"QPushButton:hover {{ background-color: {theme.BG_HOVER}; }}"
                f"QPushButton:checked {{ background-color: {theme.ACCENT_DIM}; border-color: {theme.ACCENT_BORDER}; color: {theme.TEXT_BRIGHT}; }}"
            )
            btn.clicked.connect(lambda _checked=False, name=system: self._pick(name))
            return btn

        outer.addWidget(_grouped_body(groups, make_row))
        self.setMinimumWidth(300)
        self.adjustSize()

    def _pick(self, name: str) -> None:
        self.hide()
        self._on_pick(name)


class SystemFilterPopup(QWidget):
    """Tick boxes per system, grouped by customer with a rule between
    groups; stays open until a click lands outside. on_change fires per
    tick, on_all for All/None, on_closed once when the popup hides."""

    def __init__(
        self,
        groups: list[tuple[str, list[str]]],
        hidden: set[str],
        on_change: Callable[[str, bool], None],
        on_all: Callable[[bool], None],
        parent=None,
        on_closed: Callable[[], None] | None = None,
    ):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self._on_change = on_change
        self._on_all = on_all
        self._on_closed = on_closed
        self._boxes: list[QCheckBox] = []
        self.setStyleSheet(POPUP_STYLE)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        head = QHBoxLayout()
        title = QLabel("Show PikPaks")
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

        def make_row(system: str) -> QWidget:
            box = QCheckBox(system)
            box.setChecked(system not in hidden)
            box.toggled.connect(lambda checked, name=system: self._on_change(name, checked))
            self._boxes.append(box)
            return box

        outer.addWidget(_grouped_body(groups, make_row))
        # OK applies the ticks now (closing the popup is what applies
        # them); before, only clicking elsewhere did (Chris, 2026-09-08).
        foot = QHBoxLayout()
        foot.addStretch(1)
        ok_btn = QPushButton("Update")
        ok_btn.setDefault(True)
        ok_btn.setStyleSheet(theme.PRIMARY_ACTION_BUTTON)
        ok_btn.clicked.connect(self.hide)
        foot.addWidget(ok_btn)
        outer.addLayout(foot)
        self.adjustSize()

    def _set_all(self, visible: bool) -> None:
        for box in self._boxes:
            box.blockSignals(True)
            box.setChecked(visible)
            box.blockSignals(False)
        self._on_all(visible)

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._on_closed is not None:
            callback, self._on_closed = self._on_closed, None
            callback()
