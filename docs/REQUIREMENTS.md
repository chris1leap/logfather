# The Logfather — feature requirements backlog

Living list of agreed but not-yet-built functionality, kept separate from
the code-quality plan in `CODE_REVIEW_2026-09.md`. Add context and date
when adding items; move items to the changelog section when shipped.

## Open

- **Calibration window transport controls** (Chris, 2026-09-03): the
  conveyor calibration dialog currently mirrors whatever frame the main
  viewer shows, and the only way to choose the start/end frames is to
  scrub the main window underneath — which is not discoverable. Add
  transport controls to the dialog itself (at minimum step-back /
  step-forward buttons and a small scrub slider that drive the viewer's
  playhead), plus a visible current-playhead timestamp and a hint that
  the dialog follows the viewer.

## Shipped

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
