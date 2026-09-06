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
