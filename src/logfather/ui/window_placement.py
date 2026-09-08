"""Placing the secondary windows (Data, Errors & Stops, Software...) so they
are always reachable (Chris, 2026-09-08: the Data window opened with its
title bar above the top of the screen and could not be moved or closed).

Qt centres a child top-level over its parent using the widget geometry,
which ignores the frame; on a screen shorter than the window's preferred
size that pushes the title bar off the top. So: shrink to the screen the
parent is on, centre over the parent, clamp, and once shown nudge the
real frame back on screen.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget


def _available(win: QWidget):
    parent = win.parentWidget()
    anchor = parent.window() if parent is not None else win
    screen = anchor.screen() if anchor is not None else None
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def nudge_frame_onscreen(win: QWidget) -> None:
    """Shift a shown window so its whole frame, title bar first, is visible."""
    if win.isMaximized() or win.isMinimized():
        return
    avail = _available(win)
    if avail is None:
        return
    frame = win.frameGeometry()
    dx = dy = 0
    if frame.right() > avail.right():
        dx = avail.right() - frame.right()
    if frame.bottom() > avail.bottom():
        dy = avail.bottom() - frame.bottom()
    if frame.left() + dx < avail.left():
        dx = avail.left() - frame.left()
    if frame.top() + dy < avail.top():
        dy = avail.top() - frame.top()
    if dx or dy:
        win.move(win.x() + dx, win.y() + dy)


def show_over_parent(win: QWidget, preferred_width: int, preferred_height: int, margin: int = 60) -> None:
    """Show `win` fitted to its parent's screen and centred over the parent.
    A window already visible is only raised, so its position is kept."""
    if not win.isVisible():
        avail = _available(win)
        if avail is not None:
            min_w, min_h = win.minimumWidth(), win.minimumHeight()
            width = max(min(preferred_width, avail.width() - margin), min(min_w, avail.width()))
            height = max(min(preferred_height, avail.height() - margin), min(min_h, avail.height()))
            win.resize(width, height)
            parent = win.parentWidget()
            anchor = parent.window().frameGeometry() if parent is not None else avail
            centre = anchor.center()
            # Leave room for the frame (title bar ~ 32 px, borders ~ 8 px).
            x = min(max(avail.left() + 8, centre.x() - width // 2), avail.right() - width - 8)
            y = min(max(avail.top() + 32, centre.y() - height // 2), avail.bottom() - height - 8)
            win.move(x, y)
    win.show()
    win.raise_()
    win.activateWindow()
    QTimer.singleShot(0, lambda: nudge_frame_onscreen(win))
