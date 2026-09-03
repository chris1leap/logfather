"""Unit tests for the pure-logic helpers the app depends on most.

Run with:  .venv\\Scripts\\python.exe -m pytest
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import elastic_loader
from Time_Picker import parse_time_from_name


class TestExtractRobotId:
    def test_pikpak_folder_maps_to_robot_id(self):
        assert elastic_loader._extract_robot_id(Path("Z:/public/PikPak012")) == "35-2300-012"

    def test_three_trailing_digits_required(self):
        assert elastic_loader._extract_robot_id(Path("Z:/public/PikPak")) is None

    def test_unrelated_folder_with_digits_still_maps(self):
        # Any folder ending in three digits is treated as a system folder.
        assert elastic_loader._extract_robot_id(Path("Z:/public/Spare003")) == "35-2300-003"


class TestExtractHitRobotId:
    def test_prefers_leap_robot_id(self):
        doc = {"leap_robot_id": "35-2300-010", "system_id": "35-2300-999"}
        assert elastic_loader._extract_hit_robot_id(doc) == "35-2300-010"

    def test_falls_back_to_system_id(self):
        assert elastic_loader._extract_hit_robot_id({"system_id": "35-2300-013"}) == "35-2300-013"

    def test_strips_whitespace(self):
        assert elastic_loader._extract_hit_robot_id({"leap_robot_id": " 35-2300-010 "}) == "35-2300-010"

    def test_empty_doc_returns_none(self):
        assert elastic_loader._extract_hit_robot_id({}) is None
        assert elastic_loader._extract_hit_robot_id({"leap_robot_id": "  "}) is None


class TestParseTs:
    def test_zulu_suffix(self):
        ts = elastic_loader._parse_ts("2026-09-02T15:44:47.562Z")
        assert ts == datetime(2026, 9, 2, 15, 44, 47, 562000, tzinfo=timezone.utc)

    def test_explicit_offset(self):
        ts = elastic_loader._parse_ts("2026-09-02T16:44:47+01:00")
        assert ts is not None
        assert ts.astimezone(timezone.utc).hour == 15

    def test_garbage_returns_none(self):
        assert elastic_loader._parse_ts("not a timestamp") is None


class TestEnsureUtc:
    def test_naive_becomes_utc(self):
        dt = elastic_loader._ensure_utc(datetime(2026, 9, 2, 12, 0, 0))
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 12

    def test_aware_is_converted(self):
        from datetime import timedelta, timezone as tz

        plus_two = tz(timedelta(hours=2))
        dt = elastic_loader._ensure_utc(datetime(2026, 9, 2, 12, 0, 0, tzinfo=plus_two))
        assert dt.hour == 10


class TestSearchUrl:
    def test_kibana_url_converted_to_es_endpoint(self):
        url = elastic_loader._search_url(
            "https://leap-deployment.kb.europe-west2.gcp.elastic-cloud.com:9243", "logstash-*"
        )
        assert ".es.europe-west2" in url
        assert ".kb." not in url
        assert "/logstash-*/_search" in url

    def test_trailing_slash_stripped(self):
        url = elastic_loader._search_url("https://example.com:9243/", "idx")
        assert url.startswith("https://example.com:9243/idx/_search")


class TestParseTimeFromName:
    def test_real_cctv_filename(self):
        ts = parse_time_from_name(Path("PikPak010 -Line 1-_00_20260901052919.mp4"))
        assert ts is not None
        assert (ts.year, ts.month, ts.day) == (2026, 9, 1)
        assert (ts.hour, ts.minute, ts.second) == (5, 29, 19)

    def test_filename_without_timestamp(self):
        assert parse_time_from_name(Path("clip.mp4")) is None

    def test_invalid_date_digits(self):
        assert parse_time_from_name(Path("cam_99999999999999.mp4")) is None


class _FakeCap:
    """Stands in for cv2.VideoCapture; counts grab() calls."""

    def __init__(self, grab_results=None):
        self.grab_calls = 0
        self._grab_results = list(grab_results or [])

    def grab(self):
        self.grab_calls += 1
        if self._grab_results:
            return self._grab_results.pop(0)
        return True


class TestPositionCaptureSequential:
    def _fn(self):
        from Log_vid_gui import _position_capture_sequential
        return _position_capture_sequential

    def test_next_frame_needs_no_work(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 100) is True
        assert cap.grab_calls == 0

    def test_out_of_sequence_requires_seek(self):
        cap = _FakeCap()
        assert self._fn()(cap, False, 100, 100) is False
        assert cap.grab_calls == 0

    def test_backward_jump_requires_seek(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 50) is False
        assert cap.grab_calls == 0

    def test_small_forward_jump_grabs(self):
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 103) is True
        assert cap.grab_calls == 3

    def test_large_forward_jump_requires_seek(self):
        from Log_vid_gui import MAX_GRAB_SKIP_FRAMES
        cap = _FakeCap()
        assert self._fn()(cap, True, 100, 100 + MAX_GRAB_SKIP_FRAMES + 1) is False
        assert cap.grab_calls == 0

    def test_boundary_forward_jump_grabs(self):
        from Log_vid_gui import MAX_GRAB_SKIP_FRAMES
        cap = _FakeCap()
        assert self._fn()(cap, True, 0, MAX_GRAB_SKIP_FRAMES) is True
        assert cap.grab_calls == MAX_GRAB_SKIP_FRAMES

    def test_failed_grab_falls_back_to_seek(self):
        cap = _FakeCap(grab_results=[True, False])
        assert self._fn()(cap, True, 100, 103) is False
        assert cap.grab_calls == 2


class TestEventsCacheSkuComplete:
    def _paths(self, tmp_path):
        return tmp_path / "events.json"

    def test_round_trip_preserves_flag(self, tmp_path):
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        elastic_loader._save_events_cache(cache, [], sku_complete=True)
        result = elastic_loader._load_events_cache(cache, yesterday)
        assert result is not None
        items, sku_complete = result
        assert items == []
        assert sku_complete is True

    def test_default_flag_is_false(self, tmp_path):
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        elastic_loader._save_events_cache(cache, [])
        _, sku_complete = elastic_loader._load_events_cache(cache, yesterday)
        assert sku_complete is False

    def test_legacy_cache_without_flag_reads_incomplete(self, tmp_path):
        import json as _json
        from datetime import date, timedelta
        cache = tmp_path / "events.json"
        yesterday = date.today() - timedelta(days=1)
        cache.write_text(_json.dumps({"generated_at": "x", "items": []}), encoding="utf-8")
        result = elastic_loader._load_events_cache(cache, yesterday)
        assert result is not None
        _, sku_complete = result
        assert sku_complete is False

    def test_is_past_day_uses_local_date(self):
        # Days are local calendar days: today (local) must never be "past",
        # even when the UTC date has already rolled over.
        from datetime import date, timedelta
        today_local = date.today()
        assert elastic_loader._is_past_day(today_local - timedelta(days=1)) is True
        assert elastic_loader._is_past_day(today_local) is False
        assert elastic_loader._is_past_day(today_local + timedelta(days=1)) is False


class TestSettingsResilience:
    def test_save_and_load_round_trip(self, tmp_path):
        from settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "secret-key"
        s.save(path)
        loaded = Settings.load(path)
        assert loaded.elastic_api_key == "secret-key"
        assert loaded.load_warning is None

    def test_second_save_keeps_backup(self, tmp_path):
        from settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "v1"
        s.save(path)
        s.elastic_api_key = "v2"
        s.save(path)
        bak = tmp_path / "settings.json.bak"
        assert bak.exists()
        assert '"v1"' in bak.read_text()

    def test_corrupt_file_restores_from_backup(self, tmp_path):
        from settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.elastic_api_key = "good-key"
        s.save(path)
        s.save(path)  # ensure .bak holds the good content
        path.write_text("{ this is not json", encoding="utf-8")
        loaded = Settings.load(path)
        assert loaded.elastic_api_key == "good-key"
        assert loaded.load_warning is not None
        assert list(tmp_path.glob("settings.json.corrupt-*"))

    def test_corrupt_file_without_backup_warns_and_defaults(self, tmp_path):
        from settings_store import Settings, _default_conditions
        path = tmp_path / "settings.json"
        path.write_text("garbage", encoding="utf-8")
        loaded = Settings.load(path)
        assert loaded.elastic_api_key is None
        assert loaded.load_warning is not None
        assert len(loaded.conditions) == len(_default_conditions())
        assert list(tmp_path.glob("settings.json.corrupt-*"))

    def test_load_warning_not_persisted(self, tmp_path):
        from settings_store import Settings
        path = tmp_path / "settings.json"
        s = Settings.load(path)
        s.load_warning = "boom"
        s.save(path)
        assert "load_warning" not in path.read_text()

    def test_malformed_import_raises_and_preserves(self, tmp_path):
        import pytest
        from settings_store import Settings, SHAREABLE_EXPORT_FORMAT
        export = tmp_path / "export.json"
        export.write_text(
            '{"_format": "%s", "conditions": 42}' % SHAREABLE_EXPORT_FORMAT,
            encoding="utf-8",
        )
        s = Settings.load(tmp_path / "none.json")
        s.elastic_url = "https://keep-me.example"
        with pytest.raises(ValueError):
            s.import_shareable(export)
        assert s.elastic_url == "https://keep-me.example"


class TestFrameAnalysis:
    def _frames(self):
        import numpy as np
        base = np.zeros((40, 60, 3), dtype=np.uint8)
        moved = base.copy()
        moved[10:20, 20:40] = 255
        return base, moved

    def test_pixel_diff_shape_and_signal(self):
        import numpy as np
        from frame_analysis import compute_pixel_diff_view
        base, moved = self._frames()
        out = compute_pixel_diff_view(
            moved, base, gain=1.0, threshold=10, heatmap=False, overlay=False, alpha=0.5
        )
        assert out.shape == base.shape and out.dtype == np.uint8
        assert out.sum() > 0  # the changed region must register

    def test_pixel_diff_identical_frames_dark(self):
        from frame_analysis import compute_pixel_diff_view
        base, _ = self._frames()
        out = compute_pixel_diff_view(
            base, base.copy(), gain=1.0, threshold=10, heatmap=False, overlay=False, alpha=0.5
        )
        assert int(out.sum()) == 0

    def test_optical_flow_shape(self):
        import numpy as np
        from frame_analysis import compute_optical_flow_view
        base, moved = self._frames()
        out = compute_optical_flow_view(
            moved, base, gain=1.0, min_motion=0, heatmap=False, overlay=False,
            alpha=0.5, arrows=False, arrow_step=16, arrow_scale=1.0, compute_scale=1.0,
        )
        assert out.shape == base.shape and out.dtype == np.uint8
