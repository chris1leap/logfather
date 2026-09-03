import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from logfather.paths import REPO_ROOT, SRC_ROOT, bundle_root

DEFAULT_VERSION = "dev"


def _candidate_paths() -> list[Path]:
    return [
        bundle_root() / "version.json",
        SRC_ROOT / "version.json",
        REPO_ROOT / "version.json",
    ]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def version_from_git() -> dict | None:
    """Versions are 0.<commit count>, so every commit bumps the number and
    maps back to exactly one commit. Returns None outside a git checkout
    (e.g. a frozen build, which reads the stamped version.json instead)."""
    count = _git("rev-list", "--count", "HEAD")
    if not count or not count.isdigit():
        return None
    sha = _git("rev-parse", "--short", "HEAD") or ""
    return {"version": f"0.{int(count):03d}", "build_date": "", "git_sha": sha}


_cached_info: dict | None = None


def load_version_info() -> dict:
    global _cached_info
    if _cached_info is not None:
        return _cached_info
    _cached_info = _load_version_info_uncached()
    return _cached_info


def _load_version_info_uncached() -> dict:
    if not getattr(sys, "_MEIPASS", None):
        info = version_from_git()
        if info is not None:
            return info
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
