"""
Dynamic Kibana CSV log downloader.

Uses the same structure as the "Copy POST URL" you pasted, but builds
jobParams dynamically for any time range and robot ID.

Dependencies (in your virtualenv):

    pip install requests prison
"""

import argparse
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import prison
import requests

# ================== CONFIG ==================

# Your Kibana base URL (from Elastic Cloud)
KIBANA_BASE = "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243"

# Index ID from the Discover view in your URL:
# index: fe43f3f0-cacf-11ec-892e-91a5fe3165bf
DISCOVER_INDEX_ID = "fe43f3f0-cacf-11ec-892e-91a5fe3165bf"

# Columns to export (from your URL)
COLUMNS = ["@timestamp_ros", "leap_robot_id", "source", "message", "state_name"]

# Default robot filter (can be overridden with --robot, or "" for ALL robots)
DEFAULT_ROBOT_ID = "35-2300-010"

# Kibana version / Discover title (from your URL)
KIBANA_VERSION = "9.0.2"
DISCOVER_TITLE = "Untitled Discover session"

# ---- Auth: choose ONE method and fill it in ----

# Option A: API key auth (recommended if you have one)
# Set the LOGFATHER_ELASTIC_API_KEY environment variable rather than
# hardcoding the key here — this file is committed to git.
API_KEY = os.environ.get("LOGFATHER_ELASTIC_API_KEY", "")

# Option B: basic auth (username/password)
BASIC_AUTH = None  # e.g. ("elastic", "your-password")


# ================== HELPERS ==================

def get_headers():
    headers = {
        "kbn-xsrf": "true",
    }
    if API_KEY:
        headers["Authorization"] = f"ApiKey {API_KEY}"
    return headers


def format_utc(dt: datetime) -> str:
    """
    Format datetime as strict ISO with milliseconds and 'Z' suffix.
    Example: 2025-12-03T14:43:00.474Z
    """
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def compute_time_range(args) -> tuple[str, str]:
    """
    Compute (gte_iso, lte_iso) from CLI args.

    - If --from-iso/--to-iso are given, use those.
    - Otherwise, use "last N minutes" ending at now.
    """
    if args.from_iso or args.to_iso:
        if not args.from_iso:
            raise SystemExit("If you specify --to-iso, you must also specify --from-iso.")

        def parse_iso(s: str) -> datetime:
            # Allow trailing 'Z'
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        gte_dt = parse_iso(args.from_iso)
        lte_dt = parse_iso(args.to_iso) if args.to_iso else datetime.now(timezone.utc)
        return format_utc(gte_dt), format_utc(lte_dt)

    # Relative range: last N minutes
    now = datetime.now(timezone.utc)
    gte = now - timedelta(minutes=args.minutes)
    return format_utc(gte), format_utc(now)


def build_job_params(gte_iso: str, lte_iso: str, robot_id: Optional[str]):
    """
    Build the jobParams structure as a Python dict.

    This mirrors the jobParams in your copied URL, but with dynamic gte/lte and robot_id.
    """
    # Time range filter
    time_filter = {
        "meta": {
            "field": "@timestamp_ros",
            "index": DISCOVER_INDEX_ID,
            "params": {},
        },
        "query": {
            "range": {
                "@timestamp_ros": {
                    "format": "strict_date_optional_time",
                    "gte": gte_iso,
                    "lte": lte_iso,
                }
            }
        },
    }

    filters = [time_filter]

    # Optional robot filter (includes "$state": {store: appState}, like your URL)
    if robot_id:
        robot_filter = {
            "$state": {"store": "appState"},
            "meta": {
                "alias": None,
                "disabled": False,
                "index": DISCOVER_INDEX_ID,
                "key": "leap_robot_id",
                "negate": False,
                "params": {"query": robot_id},
                "type": "phrase",
            },
            "query": {
                "match_phrase": {
                    "leap_robot_id": robot_id,
                }
            },
        }
        filters.append(robot_filter)

    job = {
        "browserTimezone": "Europe/London",
        "columns": COLUMNS,
        "objectType": "search",
        "searchSource": {
            "fields": [
                {"field": "@timestamp_ros", "include_unmapped": True},
                {"field": "leap_robot_id", "include_unmapped": True},
                {"field": "source", "include_unmapped": True},
                {"field": "message", "include_unmapped": True},
                {"field": "state_name", "include_unmapped": True},
            ],
            "filter": filters,
            "index": DISCOVER_INDEX_ID,
            "query": {"language": "kuery", "query": ""},
            "sort": [
                {
                    "@timestamp_ros": {
                        "format": "strict_date_optional_time",
                        "order": "desc",
                    }
                }
            ],
        },
        "title": DISCOVER_TITLE,
        "version": KIBANA_VERSION,
    }

    return job


def build_post_url(gte_iso: str, lte_iso: str, robot_id: Optional[str]) -> str:
    """
    Build the full /api/reporting/generate/csv_searchsource?jobParams=... URL
    dynamically, including the Rison-encoded jobParams.
    """
    job_params = build_job_params(gte_iso, lte_iso, robot_id)
    rison_str = prison.dumps(job_params)
    encoded = urllib.parse.quote(rison_str, safe="")
    return f"{KIBANA_BASE}/api/reporting/generate/csv_searchsource?jobParams={encoded}"


def queue_csv_job(gte_iso: str, lte_iso: str, robot_id: Optional[str]):
    """
    POST to the reporting endpoint and queue the CSV job.
    Returns the JSON with `path` for downloading.
    """
    headers = get_headers()
    auth = BASIC_AUTH if BASIC_AUTH else None

    post_url = build_post_url(gte_iso, lte_iso, robot_id)
    print("POST URL:", post_url)

    resp = requests.post(post_url, headers=headers, auth=auth)
    resp.raise_for_status()
    data = resp.json()
    print("Job queued:", data)
    return data


def download_csv(report_path: str, out_file: str, max_attempts: int = 60, delay_seconds: int = 2):
    """
    Poll the download endpoint until the report is ready (HTTP 200),
    or give up after max_attempts.
    """
    headers = get_headers()
    auth = BASIC_AUTH if BASIC_AUTH else None
    url = f"{KIBANA_BASE}{report_path}"

    for attempt in range(1, max_attempts + 1):
        resp = requests.get(url, headers=headers, auth=auth, stream=True)

        if resp.status_code == 503:
            print(f"[{attempt}/{max_attempts}] Report not ready (503). Retrying in {delay_seconds}s...")
            time.sleep(delay_seconds)
            continue

        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            print(f"Download failed with status {resp.status_code}: {resp.text}")
            raise e

        out_path = Path(out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        print(f"Saved CSV to {out_path.resolve()}")
        return

    raise RuntimeError("Report never became available (max attempts reached).")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Download CSV logs from Kibana Discover via reporting API."
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=10,
        help="Time window in minutes ending now (ignored if --from-iso is used). Default: 10.",
    )
    parser.add_argument(
        "--from-iso",
        type=str,
        help="Start time in ISO8601 (e.g. 2025-12-03T14:43:00.474Z). If set, overrides --minutes.",
    )
    parser.add_argument(
        "--to-iso",
        type=str,
        help="End time in ISO8601 (e.g. 2025-12-03T14:44:42.745Z). Optional; defaults to 'now' if omitted.",
    )
    parser.add_argument(
        "--robot",
        type=str,
        default=DEFAULT_ROBOT_ID,
        help="Robot ID to filter on (empty string for ALL robots). Default: %(default)s",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=".",
        help="Directory to save the CSV into. Default: current directory.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    robot_id = args.robot if args.robot else None
    gte_iso, lte_iso = compute_time_range(args)

    print(f"Requesting logs from {gte_iso} to {lte_iso} for robot {robot_id or 'ALL'}")

    job = queue_csv_job(gte_iso, lte_iso, robot_id)
    path = job.get("path")
    if not path:
        raise SystemExit(f"No 'path' in response: {job}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    robot_part = (robot_id or "all").replace("/", "_")
    out_name = f"logs-{robot_part}-{timestamp}.csv"
    out_file = str(Path(args.output_dir) / out_name)

    download_csv(path, out_file)
