"""End-to-end check that the Elastic API works with the app's own settings.

Loads the same settings file the app uses (~/.cctv_picker_settings.json),
queries the same endpoint and index pattern, and prints how many log lines
exist for the last 24 hours plus the five most recent entries.

Run:  .venv\\Scripts\\python.exe tools\\elastic_api_check.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import elastic_loader
from settings_store import Settings


def main() -> int:
    settings = Settings.load()
    if not settings.elastic_url or not settings.elastic_api_key:
        print("FAIL: no elastic_url/api key in ~/.cctv_picker_settings.json")
        return 1

    index = elastic_loader._normalize_index_id(None)
    url = elastic_loader._search_url(settings.elastic_url, index)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {settings.elastic_api_key}",
    }
    now = datetime.now(timezone.utc)
    body = {
        "size": 5,
        "sort": [{"@timestamp": "desc"}],
        "query": {
            "range": {"@timestamp": {"gte": (now - timedelta(hours=24)).isoformat(), "lte": now.isoformat()}}
        },
        "_source": ["@timestamp", "leap_robot_id", "system_id", "source", "message"],
        "track_total_hits": True,
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=20)
    except requests.RequestException as exc:
        print(f"FAIL: request error: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"FAIL: HTTP {resp.status_code}: {resp.text[:300]}")
        return 1

    data = resp.json()
    total = data["hits"]["total"]["value"]
    relation = "+" if data["hits"]["total"]["relation"] == "gte" else ""
    print(f"ELASTIC OK — {total}{relation} log lines in the last 24h")
    for hit in data["hits"]["hits"]:
        src = hit["_source"]
        robot = src.get("leap_robot_id") or src.get("system_id") or "?"
        msg = (src.get("message") or "").replace("\n", " ")[:70]
        print(f"  {src.get('@timestamp')}  {robot:<14} {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
