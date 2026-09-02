import json
import sys
from datetime import datetime
from pathlib import Path

DEFAULT_VERSION = "dev"


def _candidate_paths() -> list[Path]:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return [
        base / "version.json",
        Path(__file__).resolve().parent / "version.json",
        Path(__file__).resolve().parent.parent / "version.json",
    ]


def load_version_info() -> dict:
    for path in _candidate_paths():
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {
        "version": DEFAULT_VERSION,
        "build_date": "",
        "git_sha": "",
    }


def format_version_label() -> str:
    info = load_version_info()
    version = str(info.get("version") or DEFAULT_VERSION)
    git_sha = str(info.get("git_sha") or "")
    parts = [f"v{version}"]
    if git_sha:
        parts.append(git_sha)
    return ", ".join(parts)


def format_version_suffix() -> str:
    label = format_version_label()
    return f" ({label})" if label else ""
