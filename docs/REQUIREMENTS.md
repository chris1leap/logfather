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

### 2026-09-07

- The mode switcher reads Overview | System Replay | Search (was Viewer
  and Fleetwide; Chris, 2026-09-07); the screens are unchanged.
- System Replay left panel retired (Chris): the old calendar and system
  list on the left, and its hover-reveal, are gone; Choose system and
  Choose date in the top bar do that job. The panel's logic (share
  scan, day highlighting) still drives those buttons behind the scenes.
- System Replay prompts (Chris): with no system chosen the Choose system
  button pulses; once a system is chosen the Choose date button pulses
  until a day is chosen; nothing pulses on the other screens.
- Pulses breathe (Chris): the Choose system / Choose date prompts and
  the Data window's stale Refresh fade between the raised ground and
  the accent fill over about three seconds (shared `ui/pulse.py`)
  instead of blinking on and off.
- Date before system (Chris): Choose date works with no system chosen;
  the day is held, shown on the button, and applied when a system is
  picked (the popup notes footage days appear once a system is chosen).
  The pulse then moves to whichever of the two is still unchosen.
- Choose system opens instantly (Chris): the menu uses the Overview's
  cached share listing instead of listing the WAN share on every click.
- Choose system / Choose date are wider with larger type, and the
  system menu has roomier, larger entries with bold customer headings
  (Chris, 2026-09-07).
- Viewer retention warning (Chris): choosing a day more than 30 days old
  shows "CCTV footage is deleted after 30 days" with a crossed-out camera
  icon where the footage would play (`core/retention.py`, tested
  boundary); the notice clears when a clip loads.
- Viewer Choose system button (Chris): funnel icon at the start; reads
  "Customer / PikPakNNN" with no line name, and the menu lists systems by
  name only.
- The system filter button reads "Systems" (with the funnel icon) on the
  Overview, and for consistency in Errors / Stops and the Data window
  (Chris, 2026-09-07); it is never elided, so "(N hidden)" stays whole.
- Data window scrolling (Chris): the same left / right arrows, zoom + / -
  and scrollbar as Errors / Stops (shared `ui/chart_scroll.py`); the
  chart opens on 14 days and scrolling past the oldest day loads seven
  more (hatched until they arrive), up to 90; the newest day is always
  today. The Elastic cache serves already-counted days.
- Data window tiles (Chris): the ? floats in the Elastic tile's corner so
  both tiles share the same spacing and the totals line up; the long
  summary lines under the tiles are gone, replaced by one short "Last 14
  days: ..." line at the foot of each tile.
- Overview controls (Chris): Live and Choose days sit right after the
  Systems button and never move; the Zoom 1h / 5h / All day trio follows
  them. Choose days carries a calendar icon and is highlighted while a
  chosen day or span is shown, as Live is while live (Errors / Stops
  too).
- Chart day axis (Chris): two lines instead of dd/mm per bar - the day
  number under each bar (thinned when bars are narrow) and the month
  name once per run of days, so 30+ days stay readable; both the Data
  chart and the Errors / Stops charts (which keep their month line on
  top and show day numbers only underneath).
- Newer-version notice (Chris): a running instance checks 90 s after
  start and every ten minutes whether the checkout's HEAD or origin/main
  (after a quiet fetch) is a newer version; if so an amber "v0.NNN
  available · Restart" pill appears in the top bar and the activity bar
  says so. Restart pulls (fast-forward) when the new code is only on
  GitHub, closes cleanly, then starts the new instance. Frozen builds
  without git skip the check (`core/app_version.py`, tested).
- Data chart Total / Per pick / Per hour running (Chris): per pick
  divides each system's daily figure (documents, Elastic size, clips,
  CCTV size) by its pick movements that day; per hour on divides by the
  hours the system was switched on that day, idle included (five-minute
  slots containing any document from it, times five; Chris, 2026-09-07); hover shows picks, running time and both per-unit figures. Picks come from "Picking products"
  (Argus 2) and "Successfully planned pick" (Argus 1) and are cached
  with the counts.
- Data window controls on two rows (Chris): Systems, Show metrics and
  Total / Per pick / Per hour on on the first; hint, status, Last
  updated, Refresh and Zoom on the second. Every button is fixed to its
  text width so nothing is cut off, "(1 hidden)" included.
- Data chart labels (Chris): right-click a bar segment for "Add label:
  PikPakNNN" / "Remove label"; a labelled segment gets an accent tag
  with the system name above its bar and a leader line down to it (tags
  stack when a day has several); the hover text says which applies. A
  tag can be dragged anywhere on the chart and its leader line follows;
  labels and their positions are remembered per user.
- Data chart order (Chris): Argus 1 systems first, then Argus 2, each in
  the usual customer order; generation read from which id field carries
  the bulk of a system's documents and kept in the local cache.

### 2026-09-05

- Viewer top bar (Chris): a Choose system button (menu of every system on
  the share, grouped by customer, current one ticked) and a Choose date
  button (one-month calendar popup; days with footage highlighted, no
  future days). Once chosen the buttons read the selection, replacing the
  Customer / Line / System label. The hover-reveal left panel stays.
- Data window, local Elastic cache (Chris): per-day document counts are
  kept under LOCALAPPDATA; a refresh counts only the days not yet cached
  (today always), reuses the oldest-record date and the sampled document
  sizes for a week. The window opens on the saved figures with "Last
  updated: <date>" beside Refresh; when they are not from today the
  Refresh button pulses until clicked.
- Data window Elastic tile reads "18 Mar 2022 – <last day with data>"
  rather than "since 18 Mar 2022" (Chris, 2026-09-06).
- Data window ? box (Chris, 2026-09-06): a pixel-art question block
  (gold, in the style of the classic platform game) in the Elastic
  tile's top-right corner opens "What is stored in Elastic" - one row per
  source node with a plain description of what it logs, its share of the
  last week's documents, example messages and the fields its documents
  carry, the fields in two side-by-side columns in the widest column
  (`data/elastic_catalog.py`, curated node descriptions + one live
  aggregation). Each field is a link: clicking it opens what the field is
  (curated note, or one derived from its mapping type), how often the
  node's documents carry it, and every value it has held over the last
  year with counts and shares (top 300; numbers also get min / avg /
  max; objects and bare text fall back to the latest 200 documents)
  (Chris, 2026-09-06). Enumerated fields get a Meaning column and a
  fuller note: syslog severity 0 emergency ... 4 warning, 6
  informational, 7 debug; facility always 3 = daemon; host a
  placeholder (Chris, 2026-09-06).
- Data window, click an Elastic bar (Chris): opens Kibana Discover in the
  browser on that system and local day (either robot-id field), so the
  actual documents can be read.
- Errors / Stops window (Chris): top-bar button; the Overview's machine
  filter and Live / Choose days picker (defaults to the last 7 days).
  Two daily charts with one bar per system inside each day (same colour
  and slot every day), so a system with far more errors, or a sudden
  rise, stands out (Chris: was stacked by category); hover a bar for
  that system's breakdown by kind (emergency, protective, operator,
  caution) or category (planner, targeting, motion, sensors,
  drives/power, crate change, system). Totals above, a per-system table
  below (stoppages, errors, most common error). Long ranges scroll
  sideways (one scrollbar for both charts, wheel over a chart too)
  rather than squeezing onto one screen; scrolling past either end
  loads seven more days, drawn as hatched "loading" columns until they
  arrive (Chris). A freshly chosen range is fitted to the screen and the
  width per day then locked, so loading more days scrolls rather than
  shrinking the bars; the wheel always scrolls, never zooms; Zoom + / -
  circles at the top right are the only way to change the width per day
  (never the text size); arrow buttons either side of each chart step
  back / forward by a fifth of the view, always landing on a whole day
  (wheel too), and load more days at the ends (Chris, 2026-09-06). The per-system colour key is hidden by default;
  "Show PikPak key" in the window's top-right ⋯ menu shows it
  (remembered per user; hover still names the system) (Chris,
  2026-09-06). The Live button carries the current date, "Live (Sun 6
  Sep)", here and on the Overview (Chris, 2026-09-06). A month / year
  line ("September 2026") sits above the day headings, one label per
  run of days in a month, staying in view while scrolling (Chris,
  2026-09-06). Clicking a bar opens that system and day in the viewer
  (Chris, 2026-09-06). Exact
  Elastic aggregations over both robot-id fields
  (`data/errors_stops.py`, classification tested). The stacked bar chart
  and day-range dialog moved to shared modules (`ui/charts.py`,
  `ui/day_range_dialog.py`).
- Software window (Chris): top-bar Software button opens a timeline -
  one block per PikPak system, one lane per package (argus, planner,
  targeting, actuators, sensors, infeed, crate_change, behaviour), a bar
  per dated span labelled "version (commit)", hover for branch, dates
  and node-start count; 30d/90d/6mo/1yr ranges. Built from
  `sw_version.*` and the health node's "Node git details" documents
  (`data/software_history.py`, tested span logic). Argus 1 systems show
  as rows with no data, since they log no version or commit fields.
  A commit that no other system runs gets a red outline. The raw facts
  are cached under LOCALAPPDATA; a refresh queries only the days since
  the cache was written, and a range the cache already covers is
  re-clipped locally without a query.
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
