"""Small painted icons shared by windows (no image files to ship)."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

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
