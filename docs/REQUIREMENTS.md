# The Logfather — feature requirements backlog

Living list of agreed but not-yet-built functionality, kept separate from
the code-quality plan in `CODE_REVIEW_2026-09.md`. Add context and date
when adding items; move items to the changelog section when shipped.

## Open

(nothing at the moment)

## Shipped

- Calibration window transport controls (2026-09-03, Chris): the dialog
  now has −10/−1/+1/+10 frame-step buttons, a scrub slider that drives
  the main viewer's playhead (and follows it), a live playhead
  timestamp, and a hint that the preview mirrors the viewer. The dialog
  emits transport_step / transport_seek_fraction; TargetOverlayController
  applies them via scrub_by_frames / seek_to_seconds. Also fixed: the
  live frame feed now connects even when the dialog opens before the
  clip finishes loading.

- Session resume (2026-09-03, Chris): the app remembers the open system,
  day, and playhead time at shutdown and offers to restore them at
  startup ("Remember my choice" makes it always/never resume; stored as
  resume_on_startup / last_session in the settings file). Restoring
  reuses the signal-driven jump: select system+day, open the containing
  clip, OCR-sync, seek.

- Reverse-scrub handling in calibration (2026-09-03): capturing the end
  point at an earlier frame than the start now swaps the points into
  time-forward order instead of silently producing a belt that tracks in
  reverse (`resolve_tracking_line` in `conveyor_calibration_dialog.py`).
