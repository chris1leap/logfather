from logfather.core.grafana import (
    DatasourceRef,
    dashboard_uid_from_url,
    datasource_ref,
    frames_to_series,
    grafana_base,
    iter_panels,
    panel_queries,
    query_body,
    query_text,
    substitute_variables,
    template_variables,
    unwrap_dashboard,
    variables_used,
)

DASH_URL = (
    "https://leapmonitoring.grafana.net/d/ge98whx/actuators-issues"
    "?folderUid=fcc247&from=2026-08-17T14:08:46.720Z&var-SystemId=$__all"
)


def test_grafana_base_keeps_scheme_and_host_only():
    assert grafana_base(DASH_URL) == "https://leapmonitoring.grafana.net"
    assert grafana_base("leapmonitoring.grafana.net/") == "https://leapmonitoring.grafana.net"
    assert grafana_base("") == ""
    assert grafana_base("   ") == ""


def test_dashboard_uid_from_url():
    assert dashboard_uid_from_url(DASH_URL) == "ge98whx"
    assert dashboard_uid_from_url("https://x.grafana.net/d/abc123") == "abc123"
    assert dashboard_uid_from_url("https://x.grafana.net/dashboards") is None


def test_unwrap_dashboard_accepts_api_envelope_and_bare_model():
    model = {"title": "T", "panels": []}
    assert unwrap_dashboard({"dashboard": model, "meta": {}}) is model
    assert unwrap_dashboard(model) is model
    assert unwrap_dashboard(None) == {}


def test_datasource_ref_shapes():
    assert datasource_ref(None) == DatasourceRef()
    assert datasource_ref("Influx") == DatasourceRef(name="Influx")
    assert datasource_ref({"uid": "u1", "type": "prometheus"}) == DatasourceRef(uid="u1", type="prometheus")


def test_iter_panels_flattens_rows_and_legacy_rows():
    dash = {
        "panels": [
            {"id": 1, "type": "timeseries"},
            {"id": 2, "type": "row", "panels": [{"id": 3, "type": "stat"}]},
        ],
        "rows": [{"panels": [{"id": 4, "type": "graph"}]}],
    }
    assert sorted(p["id"] for p in iter_panels(dash)) == [1, 3, 4]


def test_query_text_priority():
    assert query_text({"expr": "up"}) == "up"
    assert query_text({"query": "SELECT 1", "rawSql": "x"}) == "SELECT 1"
    assert query_text({"rawSql": "  SELECT 2 "}) == "SELECT 2"
    assert query_text({"hide": True}) == ""


def test_panel_queries_inherit_panel_datasource_and_skip_hidden():
    dash = {
        "dashboard": {
            "panels": [
                {
                    "id": 7,
                    "title": "Servo temp",
                    "type": "timeseries",
                    "datasource": {"uid": "inf1", "type": "influxdb"},
                    "targets": [
                        {"refId": "A", "query": 'SELECT mean("temp") FROM "servo" WHERE $timeFilter AND system=~/^$SystemId$/'},
                        {"refId": "B", "query": "hidden", "hide": True},
                        {"refId": "C", "datasource": {"uid": "pg1", "type": "postgres"}, "rawSql": "select 1"},
                    ],
                }
            ]
        }
    }
    out = panel_queries(dash)
    assert [q.ref_id for q in out] == ["A", "C"]
    assert out[0].datasource == DatasourceRef(uid="inf1", type="influxdb")
    assert out[0].panel_title == "Servo temp"
    assert out[1].datasource.uid == "pg1" and out[1].datasource.type == "postgres"
    assert variables_used(out[0].query) == ["timeFilter", "SystemId"]


def test_template_variables_reads_current_and_flags():
    dash = {
        "templating": {
            "list": [
                {"name": "SystemId", "type": "query", "query": {"query": 'show tag values with key="system"'}, "current": {"value": ["$__all"]}, "multi": True, "includeAll": True},
                {"name": "MotorId", "type": "custom", "query": "1,2,3", "current": {"text": "1", "value": "1"}},
            ]
        }
    }
    vs = template_variables(dash)
    assert vs[0].name == "SystemId" and vs[0].multi and vs[0].include_all and vs[0].current == ["$__all"]
    assert vs[0].query.startswith("show tag values")
    assert vs[1].current == "1" and vs[1].query == "1,2,3"


def test_variables_used_and_substitute():
    q = "temp{system=~\"$SystemId\", motor=\"${MotorId:regex}\"} and [[Other]] $__interval"
    assert variables_used(q) == ["SystemId", "MotorId", "Other"]
    out = substitute_variables(q, {"SystemId": ["35-2300-007", "35-2300-012"], "MotorId": 2})
    assert 'system=~"(35-2300-007|35-2300-012)"' in out
    assert 'motor="2"' in out
    assert "[[Other]]" in out and "$__interval" in out


def test_query_body_keys_follow_datasource_type():
    assert query_body(DatasourceRef(uid="p", type="prometheus"), "up")["expr"] == "up"
    inf = query_body(DatasourceRef(uid="i", type="influxdb"), "SELECT 1")
    assert inf["query"] == "SELECT 1" and inf["rawQuery"] is True
    assert query_body(DatasourceRef(uid="s", type="postgres"), "select 1")["rawSql"] == "select 1"
    body = query_body(DatasourceRef(uid="x", type="mystery"), "q", ref_id="B", extra={"foo": 1})
    assert body["refId"] == "B" and body["foo"] == 1 and body["query"] == "q"


def test_frames_to_series_flattens_dataframe_json():
    response = {
        "results": {
            "A": {
                "frames": [
                    {
                        "schema": {
                            "fields": [
                                {"name": "Time", "type": "time"},
                                {"name": "temp", "type": "number", "labels": {"system": "007"}},
                                {"name": "note", "type": "string"},
                            ]
                        },
                        "data": {"values": [[1000, 2000, None, 3000], [1.5, None, 9.9, 2.5], ["a", "b", "c", "d"]]},
                    },
                    {"schema": {"fields": [{"name": "x", "type": "number"}]}, "data": {"values": [[1]]}},
                ]
            }
        }
    }
    series = frames_to_series(response)
    assert len(series) == 1
    s = series[0]
    assert s.name == "temp {system=007}" and s.ref_id == "A" and s.labels == {"system": "007"}
    assert s.times_ms == [1000, 2000, 3000]
    assert s.values == [1.5, None, 2.5]


def test_frames_to_series_prefers_display_name():
    response = {"results": {"A": {"frames": [{"schema": {"fields": [{"name": "t", "type": "time"}, {"name": "v", "type": "number", "config": {"displayNameFromDS": "PikPak 007 servo 2"}}]}, "data": {"values": [[1], [2.0]]}}]}}}
    assert frames_to_series(response)[0].name == "PikPak 007 servo 2"
