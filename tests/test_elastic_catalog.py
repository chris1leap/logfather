"""Elastic catalog: node descriptions and bucket parsing."""
from logfather.data.elastic_catalog import describe_source, parse_catalog


def test_describe_source_by_last_segment():
    kind, text = describe_source("/leap/manip1/monitor_node")
    assert kind == "Pick telemetry" and "pick" in text.lower()
    assert describe_source("quality_control_node")[0] == "Quality control"
    assert describe_source("/leap/crate_change/controller_node")[0] == "Machine control"
    assert describe_source("/leap/x/unknown_node") == ("Other", "")


def test_parse_catalog_shares_examples_and_fields():
    buckets = [
        {"key": "/leap/manip1/planner_node", "doc_count": 30, "ex": {"hits": {"hits": [
            {"_source": {"message": "New node state", "state_name": "idle", "source": "x", "@timestamp": "t", "system_id": "35-2300-007"}},
            {"_source": {"message": "New node state", "state_name": "planner_error", "sw_version": {"planner": "1.2"}}},
        ]}}},
        {"key": "/leap/manip1/monitor_node", "doc_count": 70, "ex": {"hits": {"hits": [
            {"_source": {"message": "timer_entry_created", "timer_id": 3, "run_id": "r"}},
            {"_source": {"message": "on_traj_end_infer_success\nextra", "traj_id": 9}},
        ]}}},
    ]
    catalog = parse_catalog(buckets, days=7)
    assert catalog.total_docs == 100
    assert [e.source for e in catalog.entries] == ["/leap/manip1/monitor_node", "/leap/manip1/planner_node"]
    monitor, planner = catalog.entries
    assert monitor.share == 0.7 and monitor.kind == "Pick telemetry"
    assert monitor.examples == ["timer_entry_created", "on_traj_end_infer_success extra"]
    assert monitor.fields == ["run_id", "timer_id", "traj_id"]
    assert planner.examples == ["New node state"]
    assert planner.fields == ["state_name", "sw_version", "system_id"]
    assert parse_catalog([]).total_docs == 0


def test_describe_field_and_pick_agg_field():
    from logfather.data.elastic_catalog import describe_field, parse_field_values, pick_agg_field

    assert "transition" in describe_field("state_name")
    assert describe_field("sw_version.planner") == "Software version of the planner package."
    assert "numeric" in describe_field("weird_thing", "float")
    caps = {"fields": {
        "state_name": {"text": {}}, "state_name.keyword": {"keyword": {}},
        "qc_confidence": {"float": {}}, "qc_model_loaded": {"boolean": {}},
        "sw_version.planner": {"text": {}}, "sw_version.planner.keyword": {"keyword": {}},
        "blob": {"text": {}},
    }}
    assert pick_agg_field("state_name", caps) == ("state_name.keyword", "text")
    assert pick_agg_field("qc_confidence", caps) == ("qc_confidence", "float")
    assert pick_agg_field("qc_model_loaded", caps) == ("qc_model_loaded", "boolean")
    assert pick_agg_field("sw_version", caps) == (None, "object")
    assert pick_agg_field("blob", caps) == (None, "text")
    assert pick_agg_field("missing", caps) == (None, "unknown")
    result = {"hits": {"total": {"value": 1000}}, "aggregations": {"with_field": {
        "doc_count": 800, "c": {"value": 3},
        "v": {"buckets": [{"key": "planner_ready", "doc_count": 500}, {"key": 1.5, "key_as_string": "1.5", "doc_count": 300}]},
        "st": {"count": 800, "min": 0.1, "max": 9.0, "avg": 2.5},
    }}}
    fv = parse_field_values(result, "x", "/n", 365, "float", "d")
    assert fv.node_docs == 1000 and fv.docs_with_field == 800 and fv.distinct == 3
    assert fv.values == [("planner_ready", 500), ("1.5", 300)]
    assert fv.stats == {"min": 0.1, "max": 9.0, "avg": 2.5}
