"""software_history pure logic: span building and merging."""
from datetime import datetime, timedelta, timezone

from logfather.data.software_history import (
    DatedValue,
    SystemSoftware,
    VersionSpan,
    build_spans,
    clean_sha,
    commit_owners,
    merge_adjacent,
    system_display_name,
)

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


def d(days):
    return T0 + timedelta(days=days)


def test_display_name_and_sha_cleaning():
    assert system_display_name("35-2300-007") == "PikPak007"
    assert system_display_name("35-2300-011-workshop") == "PikPak011 (workshop)"
    assert system_display_name("weird") == "weird"
    assert clean_sha('"bc93d01"\n') == "bc93d01"


def test_build_spans_labels_commits_with_covering_version():
    versions = [DatedValue("3.1.0", d(0), d(30), 100), DatedValue("3.1.1", d(30), d(180), 500)]
    commits = [
        DatedValue("aaa", d(0), d(20), 10, "deploy"),
        DatedValue("bbb", d(20), d(60), 20, "tmp-x"),
        DatedValue("ccc", d(60), d(180), 30, "tmp2-x"),
    ]
    spans = build_spans("planner", versions, commits)
    assert [(s.version, s.commit, s.branch) for s in spans] == [
        ("3.1.0", "aaa", "deploy"),
        ("3.1.1", "bbb", "tmp-x"),  # midpoint day 40 falls in 3.1.1
        ("3.1.1", "ccc", "tmp2-x"),
    ]
    assert spans[1].node_starts == 20


def test_build_spans_without_commits_uses_versions():
    versions = [DatedValue("2.4.1", d(10), d(50), 5), DatedValue("2.4.0", d(0), d(10), 3)]
    spans = build_spans("targeting", versions, [])
    assert [(s.version, s.commit, s.start) for s in spans] == [("2.4.0", "", d(0)), ("2.4.1", "", d(10))]


def test_build_spans_falls_back_to_largest_overlap():
    versions = [DatedValue("1.0", d(0), d(10), 1), DatedValue("2.0", d(40), d(50), 1)]
    commits = [DatedValue("x", d(8), d(45), 4, "b")]  # midpoint day 26.5 in neither
    spans = build_spans("argus", versions, commits)
    assert spans[0].version == "2.0"  # overlaps 5 days with 2.0, 2 with 1.0


def test_merge_adjacent_joins_same_version_and_commit():
    versions = [DatedValue("1.0", d(0), d(100), 1)]
    commits = [DatedValue("x", d(0), d(10), 3, "b"), DatedValue("x", d(10), d(20), 2, "b"), DatedValue("y", d(20), d(30), 1, "b")]
    merged = merge_adjacent(build_spans("argus", versions, commits))
    assert [(s.commit, s.start, s.end, s.node_starts) for s in merged] == [("x", d(0), d(20), 5), ("y", d(20), d(30), 1)]


def test_commit_owners_finds_unique_commits():
    def sysm(name, commits):
        spans = [VersionSpan("targeting", "2.4.1", c, "b", d(0), d(10), 1) for c in commits]
        return SystemSoftware(name, name, "Argus 2", d(0), d(10), spans)

    owners = commit_owners([sysm("PikPak006", ["82e6c1c"]), sysm("PikPak007", ["82e6c1c", "c0c5175"])])
    assert owners[("targeting", "82e6c1c")] == {"PikPak006", "PikPak007"}
    assert owners[("targeting", "c0c5175")] == {"PikPak007"}
