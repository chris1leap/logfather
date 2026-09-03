"""Sense check: Elastic log volume per day and per machine, last 30 days.

Read-only aggregation queries using the app's own settings
(~/.cctv_picker_settings.json). Handles both robot-id conventions:
machines logging `leap_robot_id` and older ones logging only `system_id`
(counted once each — no double counting).

Also tries the index _stats API for on-disk size; the app's API key needs
the `monitor` cluster privilege for that part (it degrades gracefully).

Run:  .venv\\Scripts\\python.exe tools\\elastic_volume_check.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import logfather.data.elastic_loader as elastic_loader
from logfather.data.settings_store import Settings

DAYS = 30


def _fmt_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def main() -> int:
    settings = Settings.load()
    if not settings.elastic_url or not settings.elastic_api_key:
        print("FAIL: no elastic_url/api key in ~/.cctv_picker_settings.json")
        return 1
    index = elastic_loader._normalize_index_id(None)
    search_url = elastic_loader._search_url(settings.elastic_url, index)
    base = search_url.split("/" + index)[0]
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"ApiKey {settings.elastic_api_key}",
    }
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=DAYS)).isoformat()

    per_day_hist = {
        "date_histogram": {"field": "@timestamp", "calendar_interval": "day"}
    }
    body = {
        "size": 0,
        "query": {"range": {"@timestamp": {"gte": start, "lte": now.isoformat()}}},
        "track_total_hits": True,
        "aggs": {
            "by_leap": {
                "terms": {"field": "leap_robot_id.keyword", "size": 100},
                "aggs": {"per_day": per_day_hist},
            },
            "no_leap": {
                "filter": {"bool": {"must_not": [{"exists": {"field": "leap_robot_id"}}]}},
                "aggs": {
                    "by_sys": {
                        "terms": {"field": "system_id.keyword", "size": 100},
                        "aggs": {"per_day": per_day_hist},
                    },
                },
            },
            "per_day_total": per_day_hist,
        },
    }
    try:
        resp = requests.post(search_url, json=body, headers=headers, timeout=60)
    except requests.RequestException as exc:
        print(f"FAIL: request error: {exc}")
        return 1
    if resp.status_code != 200:
        print(f"FAIL: HTTP {resp.status_code}: {resp.text[:500]}")
        return 1
    data = resp.json()
    total = data["hits"]["total"]["value"]
    print(f"ELASTIC OK — {total:,} log lines in the last {DAYS} days "
          f"(aggregation took {data.get('took')}ms)\n")

    machines: list[tuple[str, int, int, str, int]] = []

    def collect(name: str, buckets: list[dict]) -> None:
        days = {b["key_as_string"][:10]: b["doc_count"] for b in buckets if b["doc_count"]}
        if not days:
            return
        worst = max(days, key=days.get)
        machines.append((name, sum(days.values()), len(days), worst, days[worst]))

    for b in data["aggregations"]["by_leap"]["buckets"]:
        collect(b["key"], b["per_day"]["buckets"])
    for b in data["aggregations"]["no_leap"]["by_sys"]["buckets"]:
        collect(f"{b['key'] or '(blank id)'} [system_id]", b["per_day"]["buckets"])

    machines.sort(key=lambda m: -m[1])
    print(f"{'machine':<32} {'total':>12} {'days':>5} {'avg/day':>10} busiest day")
    for name, count, days_active, worst, worst_count in machines:
        print(
            f"{name:<32} {count:>12,} {days_active:>5} {count // days_active:>10,} "
            f"{worst} ({worst_count:,})"
        )

    no_leap_total = data["aggregations"]["no_leap"]["doc_count"]
    sys_sum = sum(b["doc_count"] for b in data["aggregations"]["no_leap"]["by_sys"]["buckets"])
    if no_leap_total - sys_sum:
        print(f"{'(no robot id at all)':<32} {no_leap_total - sys_sum:>12,}")

    print("\nper-day fleet totals:")
    for b in data["aggregations"]["per_day_total"]["buckets"]:
        if b["doc_count"]:
            bar = "#" * max(1, int(b["doc_count"] / 100_000))
            print(f"  {b['key_as_string'][:10]} {b['doc_count']:>10,} {bar}")

    try:
        stats = requests.get(f"{base}/{index}/_stats/store,docs", headers=headers, timeout=30)
        if stats.status_code == 200:
            sdata = stats.json()
            total_bytes = sdata["_all"]["primaries"]["store"]["size_in_bytes"]
            docs_all = sdata["_all"]["primaries"]["docs"]["count"]
            print(f"\nindex store (primaries, all time): {_fmt_bytes(total_bytes)} "
                  f"across {docs_all:,} docs "
                  f"(~{total_bytes / max(1, docs_all):.0f} B/doc)")
        else:
            print(f"\nindex store size unavailable (HTTP {stats.status_code}; "
                  f"API key needs the 'monitor' privilege)")
    except Exception as exc:
        print(f"\nindex store size unavailable: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
