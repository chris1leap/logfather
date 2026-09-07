"""CCTV retention on the share (Chris, 2026-09-07): footage older than
CCTV_RETENTION_DAYS has been deleted, so the viewer says so instead of
showing an empty video area."""
from __future__ import annotations

from datetime import date

CCTV_RETENTION_DAYS = 30
FOOTAGE_DELETED_NOTICE = f"CCTV footage is deleted after {CCTV_RETENTION_DAYS} days"


def footage_expired(day: date | None, today: date | None = None) -> bool:
    """True when the day is more than the retention window in the past."""
    if day is None:
        return False
    ref = today or date.today()
    return (ref - day).days > CCTV_RETENTION_DAYS
