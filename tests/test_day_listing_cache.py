"""Unit tests for the per-day clip-listing cache (past days only)."""
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logfather.data.day_listing_cache as dlc

PAST_DAY = date(2026, 8, 1)


def _make_share_day(tmp_path: Path, day: date, names: list[str]) -> Path:
    root = tmp_path / "share" / "PikPak012"
    day_dir = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
    day_dir.mkdir(parents=True)
    for name in names:
        (day_dir / name).write_bytes(b"x")
    return root


class TestDayListingCache:
    def test_past_day_is_cached_and_reused_without_the_share(self, tmp_path):
        cache_root = tmp_path / "cache"
        root = _make_share_day(tmp_path, PAST_DAY, ["a.mp4", "b.mp4"])
        first = dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        assert len(first) == 2
        # Remove the share entirely: the cached listing must still answer.
        import shutil

        shutil.rmtree(tmp_path / "share")
        second = dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        assert sorted(str(p) for p in second) == sorted(str(p) for p in first)

    def test_today_is_never_cached(self, tmp_path):
        cache_root = tmp_path / "cache"
        today = datetime.now().date()
        root = _make_share_day(tmp_path, today, ["a.mp4"])
        listing = dlc.load_day_files_cached(root, today, cache_root=cache_root)
        assert len(listing) == 1
        assert not cache_root.exists()

    def test_empty_listing_is_not_cached(self, tmp_path):
        cache_root = tmp_path / "cache"
        root = _make_share_day(tmp_path, PAST_DAY, [])
        assert dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root) == []
        assert not any(cache_root.glob("*.json")) if cache_root.exists() else True
        # Clips appearing later (e.g. a delayed upload) are then found.
        day_dir = root / f"{PAST_DAY.year:04d}" / f"{PAST_DAY.month:02d}" / f"{PAST_DAY.day:02d}"
        (day_dir / "late.mp4").write_bytes(b"x")
        assert len(dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)) == 1

    def test_expired_cache_rescans(self, tmp_path):
        cache_root = tmp_path / "cache"
        root = _make_share_day(tmp_path, PAST_DAY, ["a.mp4"])
        dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        [cache_file] = list(cache_root.glob("*.json"))
        stale = time.time() - (dlc.DAY_LISTING_TTL_DAYS + 1) * 24 * 60 * 60
        os.utime(cache_file, (stale, stale))
        day_dir = root / f"{PAST_DAY.year:04d}" / f"{PAST_DAY.month:02d}" / f"{PAST_DAY.day:02d}"
        (day_dir / "b.mp4").write_bytes(b"x")
        listing = dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        assert len(listing) == 2  # expired entry ignored, share re-listed

    def test_corrupt_cache_file_is_ignored(self, tmp_path):
        cache_root = tmp_path / "cache"
        root = _make_share_day(tmp_path, PAST_DAY, ["a.mp4"])
        dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        [cache_file] = list(cache_root.glob("*.json"))
        cache_file.write_text("{not json", encoding="utf-8")
        listing = dlc.load_day_files_cached(root, PAST_DAY, cache_root=cache_root)
        assert len(listing) == 1

    def test_different_roots_do_not_collide(self, tmp_path):
        cache_root = tmp_path / "cache"
        root_a = _make_share_day(tmp_path / "a", PAST_DAY, ["a.mp4"])
        root_b = _make_share_day(tmp_path / "b", PAST_DAY, ["b1.mp4", "b2.mp4"])
        assert len(dlc.load_day_files_cached(root_a, PAST_DAY, cache_root=cache_root)) == 1
        assert len(dlc.load_day_files_cached(root_b, PAST_DAY, cache_root=cache_root)) == 2
        assert len(dlc.load_day_files_cached(root_a, PAST_DAY, cache_root=cache_root)) == 1
