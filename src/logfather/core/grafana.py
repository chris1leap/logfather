"""Pure Grafana helpers: dashboard JSON parsing, URL normalising, and the
DataFrame-JSON shape that /api/ds/query returns. No Qt, no network.

Grafana's dashboard model, the parts we care about:
  dashboard.panels[]            top-level panels; a panel of type "row" may
                                carry its own panels[] (collapsed rows)
  panel.datasource              None, a name string, or {"uid": .., "type": ..}
  panel.targets[]               one query each; the query text lives under a
                                key that depends on the data source type
                                (expr for Prometheus, query for InfluxDB /
                                Elasticsearch, rawSql for SQL)
  dashboard.templating.list[]   the $variables the queries refer to
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

QUERY_KEYS = ("expr", "query", "rawSql", "rawQuery", "target", "queryText")
_DASH_UID_RE = re.compile(r"/d/([A-Za-z0-9_-]+)(?:/|$)")


@dataclass
class DatasourceRef:
    uid: Optional[str] = None
    type: Optional[str] = None
    name: Optional[str] = None


@dataclass
class PanelQuery:
    panel_id: int
    panel_title: str
    panel_type: str
    ref_id: str
    datasource: DatasourceRef
    query: str
    raw: dict = field(default_factory=dict)


@dataclass
class TemplateVariable:
    name: str
    type: str
    query: str = ""
    datasource: DatasourceRef = field(default_factory=DatasourceRef)
    current: Any = None
    multi: bool = False
    include_all: bool = False


@dataclass
class Series:
    name: str
    times_ms: list[int]
    values: list[Optional[float]]
    labels: dict = field(default_factory=dict)
    ref_id: str = ""


def grafana_base(url: str) -> str:
    """Scheme and host only, so a pasted dashboard link works as the setting."""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    if not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def dashboard_uid_from_url(url: str) -> Optional[str]:
    match = _DASH_UID_RE.search(url or "")
    return match.group(1) if match else None


def unwrap_dashboard(payload: dict) -> dict:
    """/api/dashboards/uid/<uid> wraps the model in {"dashboard": ..}; accept both."""
    if isinstance(payload, dict) and isinstance(payload.get("dashboard"), dict):
        return payload["dashboard"]
    return payload if isinstance(payload, dict) else {}


def datasource_ref(obj: Any) -> DatasourceRef:
    if obj is None:
        return DatasourceRef()
    if isinstance(obj, str):
        return DatasourceRef(name=obj)
    if isinstance(obj, dict):
        return DatasourceRef(uid=obj.get("uid"), type=obj.get("type"), name=obj.get("name"))
    return DatasourceRef()


def iter_panels(dashboard: dict) -> Iterable[dict]:
    """Every panel, including the ones nested inside collapsed rows.

    Rows themselves are skipped (they carry no queries); a row's own panels
    are yielded after it. Old dashboards keep panels under rows[].panels.
    """
    top = list(dashboard.get("panels") or [])
    for row in dashboard.get("rows") or []:
        top.extend(row.get("panels") or [])
    for panel in top:
        if not isinstance(panel, dict):
            continue
        if panel.get("type") == "row":
            for inner in panel.get("panels") or []:
                if isinstance(inner, dict):
                    yield inner
            continue
        yield panel


def query_text(target: dict) -> str:
    for key in QUERY_KEYS:
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def panel_queries(dashboard: dict) -> list[PanelQuery]:
    dashboard = unwrap_dashboard(dashboard)
    out: list[PanelQuery] = []
    for panel in iter_panels(dashboard):
        panel_ds = datasource_ref(panel.get("datasource"))
        for target in panel.get("targets") or []:
            if not isinstance(target, dict) or target.get("hide"):
                continue
            ds = datasource_ref(target.get("datasource"))
            if ds.uid is None and ds.name is None:
                ds = panel_ds
            elif ds.type is None:
                ds.type = panel_ds.type
            out.append(
                PanelQuery(
                    panel_id=int(panel.get("id") or 0),
                    panel_title=str(panel.get("title") or ""),
                    panel_type=str(panel.get("type") or ""),
                    ref_id=str(target.get("refId") or ""),
                    datasource=ds,
                    query=query_text(target),
                    raw=target,
                )
            )
    return out


def template_variables(dashboard: dict) -> list[TemplateVariable]:
    dashboard = unwrap_dashboard(dashboard)
    out: list[TemplateVariable] = []
    for item in (dashboard.get("templating") or {}).get("list") or []:
        if not isinstance(item, dict):
            continue
        query = item.get("query")
        if isinstance(query, dict):
            query = query.get("query") or query.get("expr") or ""
        current = item.get("current")
        if isinstance(current, dict):
            current = current.get("value")
        out.append(
            TemplateVariable(
                name=str(item.get("name") or ""),
                type=str(item.get("type") or ""),
                query=str(query or ""),
                datasource=datasource_ref(item.get("datasource")),
                current=current,
                multi=bool(item.get("multi")),
                include_all=bool(item.get("includeAll")),
            )
        )
    return out


def variables_used(query: str) -> list[str]:
    """$Name, ${Name}, ${Name:fmt} and [[Name]] references, in order, unique."""
    found: list[str] = []
    for match in re.finditer(r"\$\{([A-Za-z0-9_]+)(?::[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)|\[\[([A-Za-z0-9_]+)\]\]", query or ""):
        name = match.group(1) or match.group(2) or match.group(3)
        if name and name not in found and not name.startswith("__"):
            found.append(name)
    return found


def substitute_variables(query: str, values: dict[str, Any]) -> str:
    """Replace dashboard variables with concrete values. Lists join as a
    Grafana-style regex alternation (a|b), which is what most multi-value
    queries expect; callers with other formats substitute themselves."""

    def render(name: str) -> Optional[str]:
        if name not in values:
            return None
        value = values[name]
        if isinstance(value, (list, tuple, set)):
            return "(" + "|".join(str(v) for v in value) + ")"
        return str(value)

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2) or match.group(3)
        rendered = render(name)
        return match.group(0) if rendered is None else rendered

    return re.sub(r"\$\{([A-Za-z0-9_]+)(?::[^}]*)?\}|\$([A-Za-z_][A-Za-z0-9_]*)|\[\[([A-Za-z0-9_]+)\]\]", repl, query or "")


def query_body(datasource: DatasourceRef, query: str, ref_id: str = "A", extra: Optional[dict] = None) -> dict:
    """One entry for the queries[] list of POST /api/ds/query, keyed the way
    each data source type expects its query text."""
    kind = (datasource.type or "").lower()
    body: dict[str, Any] = {"refId": ref_id, "datasource": {"uid": datasource.uid, "type": datasource.type}}
    if kind == "prometheus":
        body.update({"expr": query, "format": "time_series"})
    elif kind == "influxdb":
        body.update({"query": query, "rawQuery": True, "resultFormat": "time_series"})
    elif kind in ("postgres", "grafana-postgresql-datasource", "mysql", "mssql"):
        body.update({"rawSql": query, "format": "time_series", "rawQuery": True})
    elif kind == "elasticsearch":
        body.update({"query": query})
    elif kind == "loki":
        body.update({"expr": query})
    else:
        body.update({"query": query, "expr": query})
    if extra:
        body.update(extra)
    return body


def _field_name(field_obj: dict, index: int) -> str:
    config = field_obj.get("config") or {}
    display = config.get("displayNameFromDS") or config.get("displayName")
    if display:
        return str(display)
    labels = field_obj.get("labels") or {}
    name = str(field_obj.get("name") or f"field{index}")
    if labels:
        return name + " " + "{" + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    return name


def frames_to_series(response: dict) -> list[Series]:
    """Flatten a /api/ds/query response (DataFrame JSON) into simple series.

    Each frame has schema.fields[] and data.values[] (one list per field).
    The first time-typed field is the x axis; every number field is a series.
    """
    out: list[Series] = []
    for ref_id, result in (response.get("results") or {}).items():
        for frame in result.get("frames") or []:
            fields = (frame.get("schema") or {}).get("fields") or []
            values = (frame.get("data") or {}).get("values") or []
            time_index = next((i for i, f in enumerate(fields) if f.get("type") == "time"), None)
            if time_index is None or time_index >= len(values):
                continue
            times = [int(t) for t in values[time_index] if t is not None]
            for i, f in enumerate(fields):
                if i == time_index or f.get("type") != "number" or i >= len(values):
                    continue
                vals = [None if v is None else float(v) for v in values[i]]
                if len(vals) != len(values[time_index]):
                    continue
                out.append(
                    Series(
                        name=_field_name(f, i),
                        times_ms=times,
                        values=[v for v, t in zip(vals, values[time_index]) if t is not None],
                        labels=dict(f.get("labels") or {}),
                        ref_id=str(ref_id),
                    )
                )
    return out
