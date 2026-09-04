"""Per-user UI state, kept OUT of the settings file.

Settings objects are copied and re-saved whole from several widgets
(viewer autosave, fleetwide, the reload path), so a field added to
Settings can be silently clobbered by a stale copy's next save. This
store is a separate tiny JSON under LOCALAPPDATA — per-Windows-user,
read-modify-written atomically on each change, surviving restarts.

First tenant: which customer groups the user has collapsed (Chris,
2026-09-05). Defaults for customers the user never touched still come
from Settings.customer_start_collapsed (the Systems dialog).
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


def _default_state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "VideoLogViewer" / "ui_state.json"
    return Path.home() / ".videolog_ui_state.json"


def load_ui_state(path: Path | None = None) -> dict:
    p = path if path is not None else _default_state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def update_ui_state(fields: dict, path: Path | None = None) -> bool:
    """Merge `fields` into the stored state (read-modify-write, atomic
    replace). Returns False on any IO problem — state is best-effort."""
    p = path if path is not None else _default_state_path()
    state = load_ui_state(p)
    state.update(fields)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def customer_collapsed_map(path: Path | None = None) -> dict[str, bool]:
    raw = load_ui_state(path).get("customer_collapsed")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): bool(value)
        for key, value in raw.items()
        if str(key or "").strip()
    }


def set_customer_collapsed(
    customer: str, collapsed: bool, path: Path | None = None
) -> bool:
    key = str(customer or "").strip()
    if not key:
        return False
    current = customer_collapsed_map(path)
    current[key] = bool(collapsed)
    return update_ui_state({"customer_collapsed": current}, path=path)
