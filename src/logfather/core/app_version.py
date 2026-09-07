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


def _git(*args: str, timeout: float = 5) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True,
            text=True,
            timeout=timeout,
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


# ------------------------------------------------------- update checking
# (Chris, 2026-09-07: an older instance left open should learn that a
# newer version exists and say so.)


def version_number(version: str | None) -> int | None:
    """0.207 -> 207; None for dev / malformed."""
    text = str(version or "").strip().lstrip("v")
    if not text.startswith("0."):
        return None
    tail = text[2:]
    return int(tail) if tail.isdigit() else None


def is_newer(candidate: str | None, current: str | None) -> bool:
    a, b = version_number(candidate), version_number(current)
    return a is not None and b is not None and a > b


def latest_available_version(fetch: bool = True, remote: str = "origin", branch: str = "main") -> dict | None:
    """The newest version reachable from this checkout: the local HEAD
    (someone committed or pulled) or the remote branch after a fetch.
    None outside a git checkout or when nothing can be read."""
    local = version_from_git()
    if local is None:
        return None
    best = dict(local, source="this checkout")
    if fetch:
        _git("fetch", "--quiet", remote, branch, timeout=25)
    count = _git("rev-list", "--count", f"{remote}/{branch}")
    if count and count.isdigit():
        remote_version = f"0.{int(count):03d}"
        if is_newer(remote_version, best["version"]):
            sha = _git("rev-parse", "--short", f"{remote}/{branch}") or ""
            best = {"version": remote_version, "build_date": "", "git_sha": sha, "source": f"{remote}/{branch}"}
    return best
