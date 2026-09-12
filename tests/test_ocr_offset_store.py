"""The OCR offset store must never lose cached offsets: atomic writes and
an unreadable file set aside, not replaced (Chris, 2026-09-12)."""
import json

from logfather.data.ocr_offset_store import OcrOffsetStore


def test_set_get_remove_round_trip(tmp_path):
    store = OcrOffsetStore(tmp_path / "offsets.json")
    store.set("clip-a", 1.25, 3)
    store.set("clip-b", -0.5, -1, source="additional")
    assert store.get("clip-a") == {"offset_seconds": 1.25, "frame_offset": 3}
    assert store.get("clip-b")["source"] == "additional"
    store.remove("clip-a")
    assert store.get("clip-a") is None
    assert store.get("clip-b") is not None


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    store = OcrOffsetStore(tmp_path / "offsets.json")
    store.set("clip-a", 1.0, 1)
    assert [p.name for p in tmp_path.iterdir()] == ["offsets.json"]
    assert json.loads((tmp_path / "offsets.json").read_text())["offsets"]["clip-a"]["frame_offset"] == 1


def test_unreadable_file_is_set_aside_not_overwritten(tmp_path):
    path = tmp_path / "offsets.json"
    path.write_text('{"offsets": {"clip-a": {"offset_seconds": 2.0, "frame_offset": 4}', encoding="utf-8")  # truncated
    store = OcrOffsetStore(path)
    assert store.get("clip-a") is None
    aside = [p for p in tmp_path.iterdir() if p.name.startswith("offsets.json.corrupt-")]
    assert len(aside) == 1
    assert "clip-a" in aside[0].read_text(encoding="utf-8")  # the old content survives
    store.set("clip-b", 1.0, 1)
    assert store.get("clip-b") is not None
    assert len([p for p in tmp_path.iterdir() if p.name.startswith("offsets.json.corrupt-")]) == 1


def test_missing_file_and_no_path_are_empty(tmp_path):
    assert OcrOffsetStore(tmp_path / "none.json").get("x") is None
    store = OcrOffsetStore(None)
    store.set("x", 1.0, 1)
    assert store.get("x") is None
