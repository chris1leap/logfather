"""ui_state_store: merge semantics, collapse map, corruption fallback."""
import json

from logfather.data.ui_state_store import (
    customer_collapsed_map,
    load_ui_state,
    set_customer_collapsed,
    update_ui_state,
)


def test_update_merges_and_preserves_other_keys(tmp_path):
    path = tmp_path / "ui_state.json"
    assert update_ui_state({"a": 1}, path=path)
    assert update_ui_state({"b": 2}, path=path)
    assert load_ui_state(path) == {"a": 1, "b": 2}


def test_set_customer_collapsed_round_trip(tmp_path):
    path = tmp_path / "ui_state.json"
    assert set_customer_collapsed("Acme", True, path=path)
    assert set_customer_collapsed("Bulk Foods", False, path=path)
    assert customer_collapsed_map(path) == {"Acme": True, "Bulk Foods": False}
    # Flipping one entry keeps the other.
    assert set_customer_collapsed("Acme", False, path=path)
    assert customer_collapsed_map(path) == {"Acme": False, "Bulk Foods": False}


def test_set_customer_collapsed_refuses_blank_name(tmp_path):
    path = tmp_path / "ui_state.json"
    assert not set_customer_collapsed("", True, path=path)
    assert not set_customer_collapsed("   ", True, path=path)
    assert not path.exists()


def test_corrupt_file_reads_as_empty_and_recovers(tmp_path):
    path = tmp_path / "ui_state.json"
    path.write_text("not json", encoding="utf-8")
    assert load_ui_state(path) == {}
    assert customer_collapsed_map(path) == {}
    # A write replaces the corrupt file wholesale.
    assert set_customer_collapsed("Acme", True, path=path)
    assert customer_collapsed_map(path) == {"Acme": True}


def test_collapse_map_ignores_malformed_entries(tmp_path):
    path = tmp_path / "ui_state.json"
    path.write_text(
        json.dumps({"customer_collapsed": {"Acme": 1, "": True, "Beta": False}}),
        encoding="utf-8",
    )
    assert customer_collapsed_map(path) == {"Acme": True, "Beta": False}
