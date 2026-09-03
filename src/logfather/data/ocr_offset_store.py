"""Persistence for OCR-derived clip time offsets (Stage 3, review doc).

One JSON file per camera family ({"offsets": {key: {offset_seconds,
frame_offset[, source]}}}). Extracted from VideoLogViewer, which kept this
as five methods threading an optional cache_path through every call. Key
strings are composed by the caller (they need UI-side filename helpers).
"""
from __future__ import annotations

import json
from pathlib import Path


class OcrOffsetStore:
    def __init__(self, path: Path | None = None):
        self.path = path

    def _load(self) -> dict:
        if not self.path or not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        if not self.path:
            return
        try:
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get(self, key: str) -> dict | None:
        offsets = self._load().get("offsets", {})
        if not isinstance(offsets, dict):
            return None
        item = offsets.get(key)
        return item if isinstance(item, dict) else None

    def set(
        self,
        key: str,
        offset_seconds: float,
        frame_offset: int,
        *,
        source: str | None = None,
    ) -> None:
        data = self._load()
        offsets = data.get("offsets")
        if not isinstance(offsets, dict):
            offsets = {}
            data["offsets"] = offsets
        entry = {
            "offset_seconds": float(offset_seconds),
            "frame_offset": int(frame_offset),
        }
        if source:
            entry["source"] = source
        offsets[key] = entry
        self._save(data)
