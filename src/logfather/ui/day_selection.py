"""One date selection shared by the Overview, Errors / Stops and Data
windows (Chris, 2026-09-07): choose days in one and the others follow.

`range` is None for "live" (each window's own idea of now: today, or the
last 14 days ending today) or a (start, end) pair of dates. A window that
changes the selection passes itself as `source`, so it can ignore the
echo of its own change.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QObject, Signal

DayRange = Optional[tuple[date, date]]


class DaySelection(QObject):
    changed = Signal(object, object)  # (range or None, source window)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.range: DayRange = None

    def set(self, day_range: DayRange, source: object) -> None:
        if day_range == self.range:
            return
        self.range = day_range
        self.changed.emit(day_range, source)
