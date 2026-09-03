"""Shared asset lookup for bundled media (icons, splash, placeholder art).

Replaces three per-module copies of _resolve_asset_path that had drifted in
candidate ordering; a frozen build's sys._MEIPASS always wins here.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QImage


def resolve_asset_path(filename: str) -> str | None:
    try:
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(Path(sys._MEIPASS) / filename)
        candidates.append(Path(__file__).resolve().parent.parent / "assets" / filename)
        candidates.append(Path(__file__).resolve().parent / filename)
        candidates.append(Path(sys.executable).resolve().parent / filename)
        for path in candidates:
            if path.exists():
                return str(path)
    except Exception:
        return None
    return None


def load_placeholder_image() -> QImage | None:
    image_path = resolve_asset_path("Logfather Argus II.jpg")
    if not image_path:
        return None
    img = QImage(image_path)
    if img.isNull():
        return None
    return img
