"""Unit tests for logfather.core.time_alignment.TimeAlignment.

The sign conventions here are load-bearing: they were transcribed from
the viewer's original longhand formulas, so a failing test means the
central convention drifted from what the app shipped with.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from logfather.core.time_alignment import TimeAlignment

START = datetime(2026, 9, 1, 8, 0, 0)


class TestOffsets:
    def test_effective_offset_is_sync_plus_drift(self):
        a = TimeAlignment(fps=25.0, sync_offset=10.0, time_offset=-2.5)
        assert a.effective_offset == 7.5

    def test_ocr_correction_frames_over_fps(self):
        a = TimeAlignment(fps=25.0, ocr_frame_offset=50)
        assert a.ocr_correction == 2.0

    def test_ocr_correction_zero_without_fps(self):
        assert TimeAlignment(fps=0.0, ocr_frame_offset=50).ocr_correction == 0.0

    def test_ocr_correction_zero_without_offset(self):
        assert TimeAlignment(fps=25.0, ocr_frame_offset=0).ocr_correction == 0.0

    def test_negative_frame_offset_allowed(self):
        a = TimeAlignment(fps=10.0, ocr_frame_offset=-5)
        assert a.ocr_correction == -0.5


class TestEventVideoMapping:
    def test_event_to_video_adds_effective_minus_ocr(self):
        a = TimeAlignment(fps=25.0, sync_offset=100.0, time_offset=1.0, ocr_frame_offset=25)
        assert a.event_to_video(10.0) == 10.0 + 101.0 - 1.0

    def test_event_to_video_clamps_at_zero(self):
        a = TimeAlignment(fps=25.0, sync_offset=-100.0)
        assert a.event_to_video(10.0) == 0.0

    def test_video_to_event_inverts_unclamped(self):
        a = TimeAlignment(fps=30.0, sync_offset=42.0, time_offset=-0.75, ocr_frame_offset=-12)
        for event_seconds in (0.0, 3.2, 500.0):
            video = event_seconds + a.effective_offset - a.ocr_correction
            assert abs(a.video_to_event(video) - event_seconds) < 1e-9


class TestWallClock:
    def test_clock_datetime_applies_ocr_not_drift(self):
        a = TimeAlignment(fps=25.0, time_offset=5.0, ocr_frame_offset=50)
        assert a.clock_datetime(START, 10.0) == START + timedelta(seconds=12.0)

    def test_playback_datetime_subtracts_drift(self):
        a = TimeAlignment(fps=25.0, time_offset=5.0, ocr_frame_offset=50)
        assert a.playback_datetime(START, 10.0) == START + timedelta(seconds=7.0)

    def test_playback_from_filename_ignores_ocr(self):
        a = TimeAlignment(fps=25.0, time_offset=1.5, ocr_frame_offset=50)
        assert a.playback_datetime_from_filename(START, 10.0) == START + timedelta(seconds=8.5)

    def test_video_seconds_for_clock_inverts_clock_datetime(self):
        a = TimeAlignment(fps=12.0, ocr_frame_offset=7)
        t = 33.25
        clock = a.clock_datetime(START, t)
        assert abs(a.video_seconds_for_clock(START, clock) - t) < 1e-6


class TestSecondaryCameraComposition:
    def test_primary_clock_to_secondary_video(self):
        # The secondary-camera formula the viewer uses: primary video t →
        # wall clock → secondary video t, each camera with its own OCR fix.
        primary = TimeAlignment(fps=25.0, ocr_frame_offset=25)
        secondary = TimeAlignment(fps=10.0, ocr_frame_offset=-10)
        secondary_start = START + timedelta(seconds=30)
        clock = primary.clock_datetime(START, 60.0)  # START + 61s
        t2 = secondary.video_seconds_for_clock(secondary_start, clock)
        assert abs(t2 - (31.0 + 1.0)) < 1e-9  # (61 - 30) - (-1)
