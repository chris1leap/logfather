"""The playback-time maths in one place.

Three timelines meet in the viewer:

- **video seconds** — position in the open clip (frame / fps).
- **event seconds** — a log event's start relative to the first log line
  (``TimelineEvent.start``); the log/event timeline.
- **wall clock** — real local time, anchored by ``video_start_dt`` (the
  clip's OCR-read or filename-derived start).

Three corrections relate them:

- ``sync_offset`` — coarse log→video sync: the video second at which
  event t=0 occurs (``first_log_dt − video_start_dt``, or set manually).
- ``time_offset`` — the user's fine-tune drift from the offset spinbox /
  slider. Added on top of ``sync_offset`` for event↔video mapping, and
  subtracted from the displayed wall clock.
- ``ocr_frame_offset`` — whole frames of clock error found by OCR-ing
  the burned-in camera clock: the true wall time at video second t is
  ``video_start_dt + t + ocr_frame_offset/fps``.

Historically these formulas were written out longhand at each call site
with independently chosen signs. The methods here are the single
convention; callers must not re-derive them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TimeAlignment:
    fps: float = 0.0
    sync_offset: float = 0.0
    time_offset: float = 0.0
    ocr_frame_offset: int = 0

    @property
    def effective_offset(self) -> float:
        """Total event→video shift in seconds (coarse sync + user drift)."""
        return self.sync_offset + self.time_offset

    @property
    def ocr_correction(self) -> float:
        """The OCR clock correction in seconds (0.0 when fps is unknown)."""
        if self.fps > 0 and self.ocr_frame_offset:
            return float(self.ocr_frame_offset) / float(self.fps)
        return 0.0

    def event_to_video(self, event_seconds: float) -> float:
        """Video second at which a log event occurs (clamped to >= 0)."""
        return max(
            0.0,
            float(event_seconds) + self.effective_offset - self.ocr_correction,
        )

    def video_to_event(self, video_seconds: float) -> float:
        """Event-timeline second for a video position (inverse of the
        unclamped event_to_video)."""
        return float(video_seconds) + self.ocr_correction - self.effective_offset

    def clock_datetime(self, video_start_dt: datetime, video_seconds: float) -> datetime:
        """OCR-corrected wall-clock time at a video position — what the
        camera's burned-in clock reads. Ignores the user drift."""
        return video_start_dt + timedelta(
            seconds=float(video_seconds) + self.ocr_correction
        )

    def playback_datetime(self, video_start_dt: datetime, video_seconds: float) -> datetime:
        """Wall-clock time used for log-side lookups: the OCR-corrected
        clock minus the user drift."""
        return self.clock_datetime(video_start_dt, video_seconds) - timedelta(
            seconds=self.time_offset
        )

    def playback_datetime_from_filename(
        self, filename_dt: datetime, video_seconds: float
    ) -> datetime:
        """Fallback playback time when no OCR/video start is known: the
        filename timestamp plus position, minus the user drift."""
        return filename_dt + timedelta(seconds=float(video_seconds) - self.time_offset)

    def video_seconds_for_clock(self, video_start_dt: datetime, clock_dt: datetime) -> float:
        """Video second at which the camera clock reads ``clock_dt``
        (inverse of clock_datetime; used to slave a second camera to the
        primary's wall clock)."""
        return (clock_dt - video_start_dt).total_seconds() - self.ocr_correction
