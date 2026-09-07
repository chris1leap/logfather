from datetime import date, timedelta

from logfather.core.retention import CCTV_RETENTION_DAYS, FOOTAGE_DELETED_NOTICE, footage_expired


def test_footage_expired_boundary():
    today = date(2026, 9, 7)
    assert not footage_expired(today, today)
    assert not footage_expired(today - timedelta(days=CCTV_RETENTION_DAYS), today)
    assert footage_expired(today - timedelta(days=CCTV_RETENTION_DAYS + 1), today)
    assert not footage_expired(None, today)
    assert "30 days" in FOOTAGE_DELETED_NOTICE
