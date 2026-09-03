"""Per-day clip-listing cache: skip re-listing immutable days on the share.

Listing one day folder on the WAN share (IONOS HiDrive) costs ~5s, and a
past day's clips never change — the same immutability argument as the
Elastic events cache. Listings are stored as small JSON files under
LOCALAPPDATA and expire after DAY_LISTING_TTL_DAYS, because retention
jobs CAN delete old clips from the share and a bounded lifetime lets
those deletions eventually surface.

Never cached:
- today (clips are still appearing), judged against the LOCAL date —
  day folders are local calendar days;
- empty listings — indistinguishable from an unreachable share, and
  caching one would pin a day as empty forever.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import date, datetime
from pathlib import Path

from logfather.core.timeline_model import load_day_files

DAY_LISTING_TTL_DAYS = 14


def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "cache" / "day_listings"
    return Path.home() / ".videolog_cache" / "day_listings"


def _cache_path(cache_root: Path, pikpak_root: Path, day: date) -> Path:
    digest = hashlib.sha1(str(pikpak_root).lower().encode("utf-8")).hexdigest()[:12]
    return cache_root / f"files_{pikpak_root.name}_{day:%Y%m%d}_{digest}.json"


def _read_cached_listing(cache_path: Path) -> list[Path] | None:
    try:
        if not cache_path.exists():
            return None
        age_days = (time.time() - cache_path.stat().st_mtime) / (24 * 60 * 60)
        if age_days > DAY_LISTING_TTL_DAYS:
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        files = data.get("files")
        if not isinstance(files, list) or not files:
            return None
        return [Path(entry) for entry in files if isinstance(entry, str)]
    except Exception:
        return None


def load_day_files_cached(
    pikpak_root: Path, day: date, cache_root: Path | None = None
) -> list[Path]:
    """load_day_files with a listing cache for past days."""
    if day >= datetime.now().date():
        return list(load_day_files(pikpak_root, day))
    root = cache_root if cache_root is not None else _default_cache_root()
    cache_path = _cache_path(root, pikpak_root, day)
    cached = _read_cached_listing(cache_path)
    if cached is not None:
        return cached
    listing = list(load_day_files(pikpak_root, day))
    if listing:
        try:
            root.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": datetime.now().isoformat(),
                "files": [str(p) for p in listing],
            }
            cache_path.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
        except Exception:
            pass
    return listing
