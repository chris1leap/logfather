"""End-to-end check that the Grafana API works with the app's own settings,
and a discovery of what a dashboard actually queries.

Loads ~/.cctv_picker_settings.json (grafana_url, grafana_token; the token may
also come from LOGFATHER_GRAFANA_TOKEN), then prints: the Grafana version, the
org the token belongs to, every data source (name, type, uid), and for one
dashboard its template variables and every panel query with the data source
it runs against. That answers "where does Grafana read from today".

Run:  .venv\\Scripts\\python.exe tools\\grafana_check.py [dashboard uid or URL]
Default dashboard: ge98whx (Actuators issues).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logfather.core.grafana import dashboard_uid_from_url, panel_queries, template_variables, variables_used
from logfather.data import grafana_client as gc
from logfather.data.settings_store import Settings

DEFAULT_DASHBOARD = "ge98whx"


def main(argv: list[str]) -> int:
    settings = Settings.load()
    uid = DEFAULT_DASHBOARD
    if len(argv) > 1:
        uid = dashboard_uid_from_url(argv[1]) or argv[1]
    print(f"Grafana: {gc.base_url(settings)}")
    try:
        info = gc.health(settings)
        print(f"  reachable, version {info.get('version')}")
    except gc.GrafanaError as exc:
        print(f"FAIL: {exc}")
        return 1
    if not gc.token(settings):
        print("FAIL: no Grafana token. Paste a service-account token into Settings, or set LOGFATHER_GRAFANA_TOKEN.")
        return 1
    try:
        org = gc.org(settings)
        print(f"  token OK, org: {org.get('name')} (id {org.get('id')})")
        sources = gc.list_datasources(settings)
    except gc.GrafanaError as exc:
        print(f"FAIL: {exc}")
        return 1
    by_uid = {s.get("uid"): s for s in sources}
    by_name = {s.get("name"): s for s in sources}
    print(f"\nData sources ({len(sources)}):")
    for s in sources:
        flag = "  (default)" if s.get("isDefault") else ""
        print(f"  {s.get('name'):<32} {s.get('type'):<28} uid={s.get('uid')}{flag}")

    try:
        dash = gc.get_dashboard(settings, uid)
    except gc.GrafanaError as exc:
        print(f"\nFAIL loading dashboard {uid}: {exc}")
        return 1
    print(f"\nDashboard: {dash.get('title')}  (uid {dash.get('uid')}, {len(list(panel_queries(dash)))} queries)")
    variables = template_variables(dash)
    if variables:
        print("  Variables:")
        for v in variables:
            ds = v.datasource.name or v.datasource.uid or ""
            print(f"    ${v.name:<16} {v.type:<10} current={v.current!r:<24} {ds}  {v.query[:80]}")
    for pq in panel_queries(dash):
        ds = pq.datasource
        source = by_uid.get(ds.uid) or by_name.get(ds.name) or {}
        kind = ds.type or source.get("type") or "?"
        name = source.get("name") or ds.name or ds.uid or "(panel default)"
        used = ", ".join("$" + n for n in variables_used(pq.query))
        print(f"\n  [{pq.panel_id}] {pq.panel_title or '(untitled)'}  ({pq.panel_type})  ref {pq.ref_id}  -> {name} [{kind}]")
        for line in (pq.query or "(no query text; raw target keys: " + ", ".join(sorted(pq.raw)) + ")").splitlines():
            print(f"      {line}")
        if used:
            print(f"      uses {used}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
