# The Logfather — feature requirements

Living record of agreed functionality: what is open, and what has shipped
(with the date and who asked). Kept separate from the code-quality plan in
`CODE_REVIEW_2026-09.md`. Updated with every shipped feature (Chris,
2026-09-05: keep this regularly updated).

## Open

- Elastic real on-disk sizes in the Data window (2026-09-05): needs the
  app's API key granted the `view_index_metadata` (or `monitor`) index
  privilege on `logstash-*,pikpak,pikpak-*`; the code already prefers
  `_cat/indices` and falls back to sampled estimates until then.
- Elastic log-volume reduction at source (2026-09-05, from the PikPak010
  audit): enumerate templated messages/node names/states, drop constant
  fields, one numeric timestamp, store planner payloads once, pair
  start/end events; index-side `best_compression` + keyword mappings.
  Company decision; see the audit report.
- CCTV retention beyond ~30 days (2026-09-05): company decision on the
  share; the app assumes nothing about 30 days except the 14-day
  day-listing cache TTL and the overview's 14-day clip-scan cutoff.

## Shipped

### 2026-09-05

- Software window (Chris): top-bar Software button opens a timeline -
  one block per PikPak system, one lane per package (argus, planner,
  targeting, actuators, sensors, infeed, crate_change, behaviour), a bar
  per dated span labelled "version (commit)", hover for branch, dates
  and node-start count; 30d/90d/6mo/1yr ranges. Built from
  `sw_version.*` and the health node's "Node git details" documents
  (`data/software_history.py`, tested span logic). Argus 1 systems show
  as rows with no data, since they log no version or commit fields.
- Viewer log filters survive a reload (Chris): the source / state /
  message tick boxes the user unticked are remembered when the panels
  are rebuilt, and if filters were loaded before a reload (Refresh, or
  opening another clip) they load and apply again automatically with
  the same ticks.
- Overview drag-and-drop ordering (Chris): press-and-drag a company bar
  or a machine's name to reorder; an accent line shows where it will
  land; machines stay within their company. Order remembered per user
  and used both for display and for the load sequence, so the table
  fills top to bottom. Only the ▲/▼ arrow toggles a company's collapse
  now - the rest of the bar is the drag handle. Clicking a machine name
  opens it in the viewer.
- Overview machine filter + progressive loading (Chris): funnel button
  opens a per-customer tick list (shared `ui/system_filter.py`);
  unticked systems are neither scanned nor fetched; selection remembered
  per user. Full loads emit the rows immediately and fill each system in
  as its clips and events land ("Loading..." status until then);
  in-session incremental refreshes keep the quiet fleet-wide tail.
- App-wide zoom (Chris): ⋯ menu Zoom in / out / reset, Ctrl+= / Ctrl+- /
  Ctrl+0, 60–200% in 10% steps on top of the 30% base scale; scales the
  application font (text + buttons) live; remembered per user; overview
  row geometry follows.
- Data window (Chris): top-bar Data button. Intro text, two headline
  tiles (Elastic total since oldest record; CCTV total on the share,
  estimated from the retained day folders), Elastic documents / Elastic
  size / CCTV clips / CCTV size metrics, stacked per-day bars for the
  last 14 days in pastel colours (filter, toggle, key and chart framed
  in a "14 day summary" box), hover details for the shown source
  only, funnel filter grouped by customer (remembered), click a CCTV bar
  to open that day's folder in Explorer, resizable/maximisable window.
  Elastic sizes: real index store size when the key may read it, else
  per-system sampled document sizes.
- Overview day/range filter (Chris): Live vs "Choose days…" dialog with
  From/To calendars, quick presets (Last 7 days / month / 3 months /
  year), span highlighting, selected-date readouts, day total, no future
  dates. Historic mode loads once (immutable), summaries cut at range
  end, no now/updated markers; ranges over 14 days skip clip listings;
  event chunks ≤ 1 day; day/week/4-week ticks with dd/mm labels.
- Overview presentation (Chris): "Zoom" label for 1h/5h/All Day; All Day
  spans from the first data minus 30 min; blue last-update line with a
  sticky "updated HH:MM:SS" label; sticky column headers; now-clock on
  an opaque patch; customer bars in accent blue, name-then-arrow
  centred, no logos; machines indented; ▲/▼ collapse arrows with hover
  highlight; collapse state remembered per user (shared with the date
  picker); name column sized to the widest label; loading narrated in
  stages with ETAs in the bottom activity bar.
- Overview is the default screen (Chris); switcher order Overview |
  Viewer | Fleetwide; session resume returns to the screen the session
  was saved on.
- Overview event cache (Chris): today's raw events persisted per robot
  under LOCALAPPDATA; a fresh session fetches only the tail; switching
  into Overview no longer refetches the whole day.
- Type ~30% larger app-wide (Chris); calibration transport 50% larger
  with white play/pause icons, frame-count summary removed.
- Maximised window state restored reliably (Chris): geometry captured on
  the periodic session save too, `showMaximized()` at startup.
- Desktop shortcut renames itself to "Logfather (v0.NNN)" at each launch.

### 2026-09-04

- UI redesign Stage A (Chris): global dark theme (Fusion + palette +
  base stylesheet), darker background ramp, all legacy colours merged
  onto canonical tokens, checked styles on the accent family.
- UI redesign Stage B (Chris): mode-contextual top bar (Viewer/Overview/
  Fleetwide switcher; Calibrate/Track/Targets only in viewer mode with a
  clip loaded; About behind ⋯); Settings/Systems/Readme behind a gear
  dialog; playback bar reduced to Play/Sync/Overlays/Stop Report/Fit/
  Refresh with Sync and Overlays strips; calc LCD in the Sync strip;
  system label hidden outside viewer mode.

### 2026-09-03

- Calibration window transport controls (Chris): −10/−1/+1/+10 frame
  steps, scrub slider driving and following the viewer, live playhead
  timestamp; live frame feed connects even when the dialog opens before
  the clip loads.
- Session resume (Chris): remembers system/day/playhead and always asks
  at startup; the always/never option was removed the same day.
- Reverse-scrub handling in calibration: end point earlier than start is
  swapped into time-forward order (`resolve_tracking_line`).
