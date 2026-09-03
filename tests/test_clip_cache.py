"""Unit tests for ClipCache.prune() — the one code path that deletes
user data, previously untested.

Each cached clip is a group of (clip, .meta.json, annotation json);
last-used is the newest mtime in the group. Prune deletes groups older
than CACHE_MAX_AGE_DAYS, then oldest-first until under CACHE_MAX_BYTES,
never touching protected (currently open) clips or the non-clip files
that live in the cache root.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logfather.data.clip_cache as clip_cache_mod
from logfather.data.clip_cache import ClipCache


@pytest.fixture
def cache(tmp_path):
    instance = ClipCache(root=tmp_path / "cache")
    yield instance
    instance.shutdown()


def _make_clip(cache: ClipCache, name: str, *, size: int = 10, age_days: float = 0.0,
               with_meta: bool = True, with_annotation: bool = False) -> Path:
    clip = cache.root / name
    clip.write_bytes(b"x" * size)
    companions = [clip]
    if with_meta:
        meta = cache.meta_path_for(clip)
        meta.write_text("{}", encoding="utf-8")
        companions.append(meta)
    if with_annotation:
        ann = cache.annotation_path_for(clip)
        ann.write_text("[]", encoding="utf-8")
        companions.append(ann)
    stamp = time.time() - age_days * 24 * 60 * 60
    for path in companions:
        os.utime(path, (stamp, stamp))
    return clip


class TestAgePrune:
    def test_expired_group_is_deleted_with_companions(self, cache):
        old = _make_clip(cache, "old.mp4", age_days=31, with_annotation=True)
        fresh = _make_clip(cache, "fresh.mp4", age_days=1)
        cache.prune()
        assert not old.exists()
        assert not cache.meta_path_for(old).exists()
        assert not cache.annotation_path_for(old).exists()
        assert fresh.exists()
        assert cache.meta_path_for(fresh).exists()

    def test_recent_touch_on_any_companion_keeps_the_group(self, cache):
        clip = _make_clip(cache, "clip.mp4", age_days=31)
        meta = cache.meta_path_for(clip)
        now = time.time()
        os.utime(meta, (now, now))  # last_used is the newest mtime in the group
        cache.prune()
        assert clip.exists()

    def test_protected_group_survives_age_prune(self, tmp_path):
        instance = ClipCache(
            protected_paths_provider=lambda: [str(tmp_path / "cache" / "open.mp4"), None],
            root=tmp_path / "cache",
        )
        try:
            protected = _make_clip(instance, "open.mp4", age_days=40)
            doomed = _make_clip(instance, "closed.mp4", age_days=40)
            instance.prune()
            assert protected.exists()
            assert not doomed.exists()
        finally:
            instance.shutdown()

    def test_non_clip_files_are_never_pruned(self, cache):
        offsets = cache.root / "ocr_offsets.json"
        offsets.write_text("{}", encoding="utf-8")
        stamp = time.time() - 365 * 24 * 60 * 60
        os.utime(offsets, (stamp, stamp))
        cache.prune()
        assert offsets.exists()

    def test_partial_downloads_are_not_groups(self, cache):
        part = cache.root / "clip.mp4.part"
        part.write_bytes(b"x")
        stamp = time.time() - 365 * 24 * 60 * 60
        os.utime(part, (stamp, stamp))
        cache.prune()
        assert part.exists()


class TestSizeCapPrune:
    def test_oldest_deleted_until_under_cap(self, cache, monkeypatch):
        monkeypatch.setattr(clip_cache_mod, "CACHE_MAX_BYTES", 100)
        oldest = _make_clip(cache, "a.mp4", size=60, age_days=3, with_meta=False)
        middle = _make_clip(cache, "b.mp4", size=60, age_days=2, with_meta=False)
        newest = _make_clip(cache, "c.mp4", size=60, age_days=1, with_meta=False)
        cache.prune()
        # 180 -> delete oldest (120) -> delete middle (60) -> under the cap.
        assert not oldest.exists()
        assert not middle.exists()
        assert newest.exists()

    def test_under_cap_deletes_nothing(self, cache, monkeypatch):
        monkeypatch.setattr(clip_cache_mod, "CACHE_MAX_BYTES", 1000)
        clip = _make_clip(cache, "a.mp4", size=60, age_days=3)
        cache.prune()
        assert clip.exists()

    def test_size_cap_skips_protected_group(self, tmp_path, monkeypatch):
        monkeypatch.setattr(clip_cache_mod, "CACHE_MAX_BYTES", 100)
        instance = ClipCache(
            protected_paths_provider=lambda: [str(tmp_path / "cache" / "open.mp4")],
            root=tmp_path / "cache",
        )
        try:
            protected = _make_clip(instance, "open.mp4", size=60, age_days=3, with_meta=False)
            younger = _make_clip(instance, "other.mp4", size=60, age_days=1, with_meta=False)
            instance.prune()
            assert protected.exists()
            assert not younger.exists()
        finally:
            instance.shutdown()

    def test_prune_survives_missing_root(self, tmp_path):
        instance = ClipCache(root=tmp_path / "cache")
        try:
            import shutil

            shutil.rmtree(instance.root)
            instance.prune()  # must not raise
        finally:
            instance.shutdown()
