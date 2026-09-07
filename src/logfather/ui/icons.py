"""Small painted icons shared by windows (no image files to ship)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap

from logfather.ui import theme


def zoom_glyph_icon(kind: str, size: int = 24) -> QIcon:
    """A plus or minus drawn symmetrically about the icon centre (Chris,
    2026-09-05: the glyph must sit exactly in the middle of the circle)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.TEXT_BRIGHT))
    s = float(size)
    bar = s * 0.14
    painter.drawRect(QRectF(s * 0.20, (s - bar) / 2, s * 0.60, bar))
    if kind == "plus":
        painter.drawRect(QRectF((s - bar) / 2, s * 0.20, bar, s * 0.60))
    painter.end()
    return QIcon(pm)


def gear_icon(size: int = 24) -> QIcon:
    """A settings gear: a ring with eight teeth and a hole (Chris,
    2026-09-07: the same gear top-right on every window)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    c = QPointF(s / 2, s / 2)
    tooth = QPen(QColor(theme.TEXT_BRIGHT))
    tooth.setWidthF(s * 0.16)
    tooth.setCapStyle(Qt.FlatCap)
    painter.setPen(tooth)
    for i in range(8):
        a = math.radians(i * 45)
        painter.drawLine(QPointF(c.x() + math.cos(a) * s * 0.28, c.y() + math.sin(a) * s * 0.28),
                         QPointF(c.x() + math.cos(a) * s * 0.46, c.y() + math.sin(a) * s * 0.46))
    ring = QPen(QColor(theme.TEXT_BRIGHT))
    ring.setWidthF(s * 0.17)
    painter.setPen(ring)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(c, s * 0.245, s * 0.245)
    painter.end()
    return QIcon(pm)


def arrow_icon(direction: str, size: int = 24) -> QIcon:
    """A clear left / right chevron for the step-through-time buttons
    (Chris, 2026-09-06: the style's stock arrow was too faint)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(theme.TEXT_BRIGHT))
    pen.setWidthF(size * 0.13)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    s = float(size)
    if direction == "left":
        points = [QPointF(s * 0.62, s * 0.22), QPointF(s * 0.36, s * 0.50), QPointF(s * 0.62, s * 0.78)]
    else:
        points = [QPointF(s * 0.38, s * 0.22), QPointF(s * 0.64, s * 0.50), QPointF(s * 0.38, s * 0.78)]
    painter.drawPolyline(points)
    painter.end()
    return QIcon(pm)


_QUESTION_ROWS = (
    ".#####.",
    "##...##",
    "##...##",
    "....##.",
    "...##..",
    "...##..",
    "...##..",
    ".......",
    "...##..",
    "...##..",
)


def question_block_icon(size: int = 40) -> QIcon:
    """A pixel-art question block in the style of the classic platform
    game (Chris, 2026-09-06): gold block, dark outline, corner rivets and
    a chunky pale ? with a shadow. Drawn on a 16x16 grid."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setPen(Qt.NoPen)
    cell = size / 16.0
    gold, shade, outline, pale = QColor("#f8b838"), QColor("#a85400"), QColor("#1a0c00"), QColor("#fff4d6")

    def px(x: int, y: int, colour: QColor) -> None:
        painter.setBrush(colour)
        painter.drawRect(QRectF(x * cell, y * cell, cell + 0.5, cell + 0.5))

    for y in range(16):
        for x in range(16):
            if x in (0, 15) or y in (0, 15):
                px(x, y, outline)
            elif x == 14 or y == 14:
                px(x, y, shade)
            else:
                px(x, y, gold)
    for x, y in ((1, 1), (13, 1), (1, 13), (13, 13)):
        px(x, y, shade)
    ox, oy = 4, 3
    for dy, row in enumerate(_QUESTION_ROWS):
        for dx, ch in enumerate(row):
            if ch == "#":
                px(ox + dx + 1, oy + dy + 1, shade)
    for dy, row in enumerate(_QUESTION_ROWS):
        for dx, ch in enumerate(row):
            if ch == "#":
                px(ox + dx, oy + dy, pale)
    painter.end()
    return QIcon(pm)


def calendar_icon(size: int = 24) -> QIcon:
    """A small calendar page for the Choose days button (Chris,
    2026-09-07): header bar, two rings, a grid of days."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    ink = QColor(theme.TEXT_BRIGHT)
    pen = QPen(ink)
    pen.setWidthF(max(1.5, s * 0.08))
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.66), s * 0.1, s * 0.1)
    painter.setPen(Qt.NoPen)
    painter.setBrush(ink)
    painter.drawRect(QRectF(s * 0.14, s * 0.2, s * 0.72, s * 0.18))
    for x in (0.34, 0.66):
        painter.drawRoundedRect(QRectF(s * x - s * 0.045, s * 0.08, s * 0.09, s * 0.22), s * 0.04, s * 0.04)
    for row in (0.5, 0.66):
        for col in (0.3, 0.5, 0.7):
            painter.drawRect(QRectF(s * col - s * 0.05, s * row, s * 0.1, s * 0.09))
    painter.end()
    return QIcon(pm)
