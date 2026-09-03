"""Filesystem anchors for the package layout.

Modules used to derive the repo/src roots from their own __file__; after
the ui/data/core split those relative hops differ per package depth, so
they all come from here instead. In a PyInstaller bundle, resources live
under sys._MEIPASS (see bundle_root)."""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent   # .../src/logfather
SRC_ROOT = PACKAGE_ROOT.parent                   # .../src
REPO_ROOT = SRC_ROOT.parent                      # repo checkout


def bundle_root() -> Path:
    """Where bundled data files land: _MEIPASS in a frozen build, src/ in dev."""
    return Path(getattr(sys, "_MEIPASS", SRC_ROOT))
