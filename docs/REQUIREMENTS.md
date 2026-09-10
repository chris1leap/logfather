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

### 2026-09-10

- Timeline scrolling and zoom (Chris, 2026-09-10): the mouse wheel over the
  System Replay timeline scrolls it left and right, Shift+wheel scrolls the
  reading strips, Ctrl+wheel zooms about the cursor (between the whole day
  and one second per pixel). The view's minimum height dropped from 260 to
  110 px so the horizontal scrollbar is no longer clipped when the timeline
  is collapsed.
- Info text toggle (Chris, 2026-09-10): an "Info text" button on the
  playback bar, next to Overlays, hides or shows the green pick-rate / SKU /
  tray / tool text drawn over the CCTV image (main view and pop-out). On by
  default; the choice is remembered in ui_state as viewer_status_text.

### 2026-09-08

- Log-time clock back on the top row (Chris, 2026-09-08): the green
  computed log time sits between the clip position and the frame
  counter again; it had moved into the Sync strip on 2026-09-04.
- Fits a laptop screen (Chris, on site, 2026-09-08): the right column
  (log tabs plus the Data boxes) no longer imposes its ~780 px minimum
  height, so the timeline and activity bar stay on screen at 1463x866.
- Clip download progress (Chris, 2026-09-08): the activity bar and the
  'Loading clip' dialog show downloaded / total MB, percentage, MB/s
  and time remaining.
- Secondary windows fit the screen (Chris, 2026-09-08): the Data,
  Errors & Stops and Software windows open sized to the screen the main
  window is on, centred over it with the title bar kept visible, and are
  nudged back on screen after showing (the Data window had opened with
  its title bar above the top and could not be moved or closed).
- System Replay event ticks (Chris, 2026-09-08): each condition-track
  mark has a 10 px hit area with a hand cursor; hover shows the exact
  time to the millisecond, the full message, node / state / severity and
  any SKU details; a click opens the clip covering that moment, seeks to
  it and the logs follow.
- System Replay readings (Chris, 2026-09-08): the same Data and Additional
  data boxes sit above the day timeline, and every ticked family is drawn
  as a strip under the tracks for the chosen system and day, with the
  cursor dropping dots and a box of values, and drag-to-stretch. The
  boxes and channels are one shared component (`SignalBoxes`); the
  Overview and the Replay each remember their own selection.
- Data box: Picks (Chris, 2026-09-08), first row above Temps: picks per
  minute, scaled from 0. Argus 2 from Grafana's
  targeting_products_picked_per_min; Argus 1 (no such metric) from the
  pick messages in Elastic (`data/pick_rate.py`: one date_histogram,
  20 s buckets up to two days, then 5 / 30 minutes; each point is the
  trailing minute's rate; Chris, 2026-09-08).
- Additional data: CAN bus family (Chris, 2026-09-08): CAN bus errors, CAN
  errors near power event, CAN frames seen. Not cumulative counters: the
  health node reports a count per window and restarts, so they are drawn
  as reported rather than rate()d (rate() on them explodes).
- Overview Additional box (Chris, 2026-09-08), right of Data: one menu
  ticks CPU load (CCU / RCU, %), Memory (CCU / RCU, %), RCU to CCU clock
  offset (s), Motor halting errors (summed over motors) and Argus log
  queue (lines waiting); each ticked family is its own strip with the
  same hover box and drag-to-stretch, and a combined colour key sits
  under the button. These metrics are Overview-only (`in_replay=False`)
  so the System Replay Telemetry tab's day load stays quick.
- Overview air pressure (Chris, 2026-09-08): a Pressure row in the Data
  box (gauge icon) with the one reading, sensors_air_pressure in bar,
  drawn as a third strip under currents with the same hover box and
  drag-to-stretch. The System Replay Telemetry tab gains an Air pressure
  chart too.
- Overview currents (Chris, 2026-09-08): a Currents row in the Data box
  (lightning icon) works exactly like Temps: tick Highest motor (largest
  magnitude across fitted motors) or Motor 1/2/3/5/6, a colour key
  appears, a second strip under the temperature strip draws the traces
  in amps, hover drops a dot per trace and shows a box with the values,
  and the strip stretches by dragging its bottom line. Both channels
  share one class (`ui/overview_signals.py`) and one fleet fetch.
- Overview temperatures (Chris, 2026-09-08): Temps and its colour key sit on
  their own row under Systems in a box labelled Data. Hovering a strip drops
  a dot on each trace at the hover line and the hover label shows just those
  temperatures; the min/max/latest tooltip is gone.
- Temps menu lists each fitted motor (1, 2, 3, 5, 6) below the sensors and
  Hottest motor (Chris, 2026-09-08); slots 0 and 4 read a flat zero on every
  system over 30 days, so they are not offered. Each is its own colour and
  one fleet query filtered on motor_id. The hover box over a strip carries
  the system and time, a rule, then the readings in the key's colours with
  °C, in smaller type.

### 2026-09-07

- Overview temperatures (Chris, 2026-09-07): a Temperatures menu next to
  Systems ticks which readings to show (CPU, RCU, GPU, brake resistor,
  hottest motor); any ticked adds a strip under every system's lane with
  those lines over the visible window, the range in °C at the left, the
  latest values in the right column and min/max/latest on hover. One
  Grafana query per reading for the whole fleet; live mode refreshes
  every five minutes, a chosen span loads once. The choice is remembered.
  The button is "Temps" with a thermometer icon, a colour key appears
  beside it while any reading is on, and dragging a strip's bottom line
  stretches every strip (16 to 240 px, remembered).
- Data window: choose a date (Chris, 2026-09-07). "Last 14 days" and a
  calendar "Choose days…" button (a day or a span, newest 90 days at
  most) next to Systems; Elastic, Grafana and CCTV are all fetched for
  that span and the summary title names it. The date selection is shared:
  choosing days in the Overview, Errors / Stops or Data sets the other two
  the same (`ui/day_selection.py`); Live in any of them returns all to
  live.
- Overview opens on All Day instead of 1h (Chris, 2026-09-07); 1h and 5h
  remain a click away.
- Settings window (Settings / Systems / Readme tabs) is a real window
  with a title-bar close, a Close button, a size grip, and opens sized to
  the screen and centred over the main window (Chris, 2026-09-07: it ran
  off the page with no way to close).
- Data window, Grafana (Chris, 2026-09-07): a third tile between Elastic
  and CCTV with the telemetry retained in Grafana Cloud (13-month
  retention, from the stack's own samples-per-second history), active
  series, metric count and a last-14-days foot line; "Grafana samples"
  and "Grafana size" chart views per system per day (a 30 s subquery over
  count_over_time), click a bar to open the Actuators issues dashboard on
  that system and day. Sizes are samples × 1.5 bytes and say "estimated".
  A ? on the tile opens "What is stored in Grafana": every metric with a
  system label, its family and meaning, series per generation; click a
  metric for its series over the last day (min, average, max, latest).
  `data/grafana_inventory.py`, `data/grafana_catalog.py`,
  `ui/grafana_catalog_dialog.py`.
- Telemetry tab in System Replay (Chris, 2026-09-07): choosing a system
  and day fetches that robot's day from Grafana Cloud's Prometheus (CPU,
  RCU, GPU and brake-resistor temperatures; per-motor temperature and
  current; 30 s samples) through Grafana's query API, and draws one chart
  per group with the playhead across it and a hover readout. Unfitted
  motors (flat zero) are dropped; Argus 2 runs are stitched into one
  track. `core/telemetry.py` (pure), `data/telemetry_loader.py`,
  `ui/telemetry_strip.py`. The Data sources Grafana Test also probes the
  telemetry source and names the permission to grant when it is denied.
- Telemetry row on the day timeline (Chris, 2026-09-07): a "Telemetry"
  track under SKU draws the hottest motor's temperature through the day
  (CPU temperature when a system has no motor readings), breaking at gaps
  over five minutes; hover it for the day's range, the tab for values.
- Gear menu on every window (Chris, 2026-09-07): the top-right button on
  the main window, Errors / Stops, Data and Software is the same gear
  (`ui/gear_menu.py`) with Data sources, Settings, Systems, Readme, the
  zoom row and About; Errors / Stops keeps "Show PikPak key" above them.
  The old "⋯" overflow and the gear in the System Replay tab corner are
  gone. **Data sources** (`ui/data_sources_dialog.py`) holds the CCTV
  share, Elastic URL + API key and Grafana URL + token, each with a Test
  button that runs off the UI thread; those fields left the Settings tab.
- Grafana groundwork (Chris, 2026-09-07): Settings gains Grafana URL and
  Grafana token (a service-account token, stored like the Elastic key and
  never exported); `core/grafana.py` parses dashboard JSON (panels in
  rows, data source refs, query text per source type, $variables) and
  flattens DataFrame-JSON query results; `data/grafana_client.py` talks
  to Grafana's API and proxies queries through `/api/ds/query` so the
  app never needs to know the backend; `tools/grafana_check.py` prints
  the data sources and every query on a dashboard, answering "where does
  Grafana read from today".
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
- Choose system opens the same grouped box as the Overview's Systems
  filter (shared `ui/system_filter.py`: SystemPickerPopup, one choice,
  current system highlighted; SystemFilterPopup, many ticks); the top
  buttons are unchanged (Chris, 2026-09-07).
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
  Right-clicking the tag itself offers "Delete label" (Chris).
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
