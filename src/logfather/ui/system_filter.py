"""Shared system filter: a funnel-icon button's popup of tick boxes per
system, grouped by customer (Chris, 2026-09-05). Used by the Data window
and the Overview; each keeps its own hidden set."""
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

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._on_closed is not None:
            callback, self._on_closed = self._on_closed, None
            callback()
