# The Logfather — deep code review & refactoring plan (2026-09-02)

Three parallel deep-read reviews (viewer core / UI hub & panels / data layer),
consolidated. Line numbers are as of commit `2e9302a`. Companion to
`ARCHITECTURE.md` (what the code does); this file covers what's wrong with it
and how to break it into workable modules ahead of significant changes.

**Headline numbers**
- `Log_vid_gui.VideoLogViewer`: ~5,170 lines, ~190 methods, ~120 instance
  attributes, **18 distinct responsibilities** in one QWidget.
- `Main_Window.py`: 2,085 lines, of which ~250 are actual hub/routing.
- `elastic_loader.py`: 2,348 lines; ~600 are copy-pasted query boilerplate,
  ~470 are dead.
- Dead code across the repo: **~1,100+ lines** (verified unreferenced).
- Bare `except Exception` blocks: 100+ (44 in the UI hub files alone,
  33 in elastic_loader).
- Test coverage: one file, covering a handful of parsing helpers. None of the
  UI logic, none of the SKU state machines, none of the cache/prune logic.

---

## 1. Correctness bugs found during review (fix before/independent of refactor)

Ranked by blast radius.

1. **No `MainWindow.closeEvent`** — the only thread-stopping code lives in
   `TimePicker.closeEvent:886`, `OverviewWidget.closeEvent:669`,
   `Fleetwide...closeEvent:714`, but Qt only delivers close events to the
   top-level window, which has none. Every worker thread (plus
   `_BufferLoaderThread`) is still running while Qt tears down C++ objects —
   the likely source of exit-time crashes/hangs.
2. **`settings_store.load()` silently factory-resets on any exception**
   (`:281-288`) — a truncated JSON file costs the API key, customers, logos,
   layouts, and all 15 conditions with no message and no backup. The write is
   also non-atomic (`:313`, plain `write_text`), which is exactly how the
   malformed file gets created. Fix: temp-file + `os.replace`, keep a `.bak`,
   and surface load failures instead of resetting. `import_shareable`
   (`:330-340`) has the same failure: a malformed export imports as factory
   defaults over live settings.
3. **`Log_vid_gui._prune_cache` runs on up to 3 threads concurrently**
   (called from `_copy_to_cache:5794`, which runs on both `_cache_executor`
   and the 2-worker `_prefetch_executor`) while reading UI-thread state
   (`current_video_path`, `:5637`). Failures are swallowed (`:5658`,
   `:5682`); the symptom is silent eviction of the clip currently open, plus
   its annotation JSON.
4. **Partial Elastic results are cached as authoritative, forever.**
   `fetch_events` writes the day cache (`:1203`) *before* raising for
   warnings (`:1206`), and past-day caches never expire (`:805-808`). One
   timed-out condition = a permanently truncated timeline. The events cache
   also has **no schema version** in its digest (unlike the other two cache
   families), so serializer changes are invisible to existing installs.
5. **`transition_states` exists as three divergent lists** (`:518` 10 states,
   `:881` 8 — missing `system_stop`/`emergency_stop`, `:2219` 9). The SKU
   query filters on the short list, so timeline SKU bands don't terminate on
   `system_stop`/`emergency_stop` but the overview board does. Same day, two
   answers.
6. **`SYSTEM_ID_OVERRIDE` is a module global mutated from the UI thread and
   read in workers** (`elastic_loader:34,46`; read at `:1016,:1395,:1749`,
   `target_buffer_loader:117`). Switching system mid-load can attribute one
   robot's rows to another; the cache filename/digest can disagree; the
   override silently beats a real `pikpak_root`. Replace with an explicit
   `SystemRef(robot_id, pikpak_root)` argument.
7. **`_is_past_day` compares a local day to a UTC date**
   (`elastic_loader:801`). West of UTC, the in-progress day becomes "past"
   (and its cache permanent) as soon as UTC rolls over.
8. **Log-fetch dedupe key bug**: `_poll_log_future:6811-6818` sets
   `_active_log_request_key = None` then assigns it to
   `_loaded_log_request_key` — after a partial failure the same range
   refetches on every trigger.
9. **The "Downloading…" dialog can stick forever**: `_set_video_busy(True)`
   is only cleared via `_finish_pending_video_load`, which is skipped when
   the prefetch future was cancelled (`:5769`) or the source doesn't match.
10. **`_is_cached_copy_current` returns True when `stat()` raises**
    (`:5693`) — a dropped Z: mount makes every stale copy look current.
11. **Blocking work still on the UI thread**: `_get_cached_video_for_ocr`
    does a synchronous SMB clip copy (`:6403`, four callers); OCR
    (`analyze_video_offset`) runs Tesseract synchronously with
    `processEvents()` re-entrancy (`time_ocr:1467,1535`); the Stop Report
    build runs a 30k-hit Elastic fetch + one `cv2.VideoCapture` seek+decode
    per stop event synchronously (`Main_Window:1441,1595-1616`) and pumps
    `processEvents()` mid-build.
12. **Overview repaints are pathological**: `OverviewWidget._redraw` does
    `scene.clear()` + full rebuild (~300 lines) from 11 call sites including
    a 1 s timer and **every mouse-move** (`:683`), re-running the
    `_summarize_system` state machine per system per mouse-move. Same
    scene-clear-under-mouse-dispatch shape that crashed the timeline is
    latent in `_OverviewThumbItem.mousePressEvent:254`.
13. `settings_store` load mutates user data: blanked condition slots are
    re-filled with defaults (`:169-175`) and colours are force-overwritten by
    name-matching (`:176-185`); colours are positional so row insertion
    recolours everything below.
14. Four `thread.terminate()` calls (`Time_Picker:914`,
    `Date_Picker_frontend:454`, `overview_widget:863`, `fleetwide:719`) —
    terminating mid-`requests` call can corrupt state; prefer
    "stop waiting, leak deliberately".
15. Latent `NameError`: `target_buffer_widget.py:253` annotates
    `QVariantAnimation`, which is never imported (masked by
    `from __future__ import annotations`).

## 2. The Argus 1/2 problem (matters most for the planned changes)

Dual-schema handling is smeared, not isolated:
- **Identity**: `_build_robot_filters:402` emits 9 should-clauses (including
  the bogus `system_id.raw.keyword`) from **13 call sites**; reading identity
  back is a differently-ordered fallback (`:501`); fleetwide has a third
  bespoke shape (`:2198`). The overview sends ~180 should-clauses per page.
- **SKU**: `_extract_ui_selection:272` is a ~10-branch fallback ladder over
  both schemas; two more field-existence lists in queries; two compensating
  extra HTTP queries for Argus-2 (`:1517`, `:1572`), both error-swallowed.
- **Pick-queue join** forks on schema (`target_buffer_loader:236-277`).
- Raw `_source` dicts escape into widgets (`Main_Window:1374`,
  `Time_Picker:655`, `target_buffer_widget:48-81`), so a schema change means
  editing widgets. `TimelineItem.payload` carries two incompatible shapes
  under `kind="sku"`.

**The seam**: one new flat module `elastic_schema.py` owning field-name data,
`identity_filter(robot_ids)`, one `TRANSITION_STATES` list, and
`normalize(hit) -> LogEvent` (dataclass). Nothing else touches a raw Elastic
field name. Detect each robot's schema variant once and narrow the filters
(fleetwide already proves this works, `:2198-2218`).

## 3. Duplication worth collapsing

- **Five hand-rolled QThread loaders** (`Date_Picker_frontend.ScanThread:24`,
  `Main_Window._BufferLoaderThread:84`, `Time_Picker._LoadThread:1541`,
  `fleetwide.FleetwideSearchThread:374`, `overview._OverviewLoadThread:369`)
  with four different lifetime/staleness strategies (one is the
  disconnect-and-leak hack at `Main_Window:631-649`). → one `qt_worker.py`
  with a `Job` + `JobSlot` ("at most one live job, later wins").
- **Elastic boilerplate ×6-9**: preamble, ts-range fanout, `search_after`
  loops, `_source` lists, headers, error-text extraction — collapse into
  `elastic_client.py` (session, `search()`, `paginate()`, one retry ladder,
  typed truncation) + `elastic_queries.py` (pure dict builders).
- **Byte-identical copies**: the five analysis functions and
  `ScrubbableLabel`/`format_timecode` duplicated from
  `tools/Vid_Frame_Differencing.py` (ScrubbableLabel exists 3×, third in
  `time_ocr:111`); `time_ocr` duplicates five of its own algorithms between
  interactive and headless paths.
- **Triple-copied helpers that disagree**: robot-id derivation
  (`elastic_loader:234` / `overview:78` / `fleetwide:26` — middle one accepts
  different inputs); `_resolve_asset_path` ×3 with different candidate order;
  SKU label formatting ×3; `ensure_utc` re-implemented inline 5× with two
  identical if/else branches.
- Main-vs-secondary video paths in the viewer are ~60-line near-verbatim
  twins (OCR open, auto-sync, cache-stability polling).
- The SKU band state machine exists twice in elastic_loader; the dead copy
  (`:1211`) has already drifted from the live inline one (`:1625`).

## 4. Dead code inventory (verified unreferenced; safe deletes)

- `elastic_loader`: `_build_sku_items_from_event_items:1211` (140),
  `fetch_target_rate_histogram:1838` + its private cache layer `:143-231`,
  `fetch_overview_events:640`, `_logs_cache_path:119`, `_extract_ui_sku:341`
  — **~470 lines**.
- `conveyor_calibration`: the homography half (`:26-241`, ~215 lines).
- `Time_Picker`: entire clip-range context-menu/drag family
  (`_show_timeline_context_menu:1032` etc. + `clip_range_export_requested`
  signal) — ~180 lines; `refresh_cache_status:1450`; no-op lines `:413-414`,
  `:552`, render-hint no-op `:278`.
- `Main_Window`: `_export_timeline_clip_range:1648` (87),
  `_recheck_ocr_offset`, `_find_horizontal_splitter`, `_scope`/track-toggle
  branches, `DISABLE_CLIP_LOG_LOADING`, 6 unused imports,
  `TargetScopeWidget` import (class never instantiated).
- `Log_vid_gui`: viewer-level tray-popout twins (`:2642`, `:4965`),
  `open_video`/`open_additional_cctv`/`open_csv` + the whole CSV ingest
  chain, `get_log_text_at_time`, `adjust_offset`, `recheck_ocr_offset:6543`,
  `_update_load_filters_button` (body is `return`), unreachable AND-mode in
  custom filters, double-`except` in `_is_path_in_cache:5951`.
- `target_buffer_widget._ClearBanner`, `.set_targets`; unused `_TargetCard`
  params; `settings_store.customer_sort_key`'s dead `settings` param.

## 5. Refactoring plan (flat src/ throughout)

### Stage 0 — bug fixes first (small, independent) — DONE 2026-09-03
closeEvent for MainWindow (`4dc7238`); atomic settings write + backup +
loud load failure (`bc6cf0b`); don't cache partial Elastic days + surface
page-cap truncation + schema-version the events-cache digest (`8d4d3c1`);
unify `TRANSITION_STATES` (`a7d0e10`); fix `_is_past_day` to local date
(`1f44382`); fix the log-dedupe-key and busy-dialog-stick bugs
(`baf2ca1`); replace the four `terminate()` calls with parked threads
(`74fcbc9`, seeds `qt_worker.py`).

### Stage 1 — zero-risk extractions + purge (mostly mechanical)
1. Delete the dead-code inventory (§4) — ~1,100 lines.
   **DONE 2026-09-03** (`50ea9dc`, `12de32c`, `bf5c2b8`, `350dd28`,
   `b5b81dc`, `9281c6d`): ~1,230 lines removed across elastic_loader,
   conveyor_calibration (homography kept as raw data pass-through),
   Time_Picker, Main_Window, Log_vid_gui, target_buffer_widget.
   Item 2 (hasattr purge) DONE (`5565cb5`). Item 3 extractions DONE
   2026-09-03: frame_analysis (`1cb20e4`), app_assets (`c139025`),
   viewer_widgets (`263d822`), annotated_video_widget (`a2803ac`),
   log_events (`039539c`), clip_cache with the prune-race fix (`073ee4b`),
   app_main (`da6a070`). Log_vid_gui: 6,900 → ~4,900 lines; Main_Window:
   2,085 → ~1,850. Job/JobSlot + all five loader ports DONE 2026-09-03
   (`af91557` date scan, `1bd9308` buffer, `6ce4bd2` fleetwide,
   `cabb5e2` overview, `cf789c1` timeline) — one worker pattern, no
   terminate(), no keep-alive hacks, no request-id staleness protocols.
   **STAGE 1 COMPLETE.** Stage 2 progress 2026-09-03: elastic_schema.py
   — the single Argus 1/2 seam, 11 tests (`0bc9fdd`, `8a7aa2e`);
   elastic_client.py session/URL/headers (`8c22aee`); sku_timeline.py
   band state machine as a pure function, 9 tests (`fdddfab`);
   stop_report.py out of Main_Window (`8887f70`);
   target_overlay_controller.py (`1a0b288`); signal-driven overview nav
   (`3f80309`); overview hover fix (`18f86e4`); paginate/retry-ladder
   unification DONE 2026-09-03 (`3bed7c4` paginate() + 17 tests, then
   the six loop ports `6117c1c` SKU, `587a1d0` log range, `db60390`
   overview, `cc0bb18` target buffer, `d8962d5` per-condition events,
   `b857c68` fleetwide — each verified against live Elastic with
   identical before/after output digests on fixed PikPak012 2026-09-01
   windows). **STAGE 2 COMPLETE.**
2. Delete the 49 `hasattr`/`getattr` capability probes in Main_Window —
   they hide typos; the ~20-method viewer interface they conceal becomes
   visible and documentable.
3. New flat modules by pure move:
   - `frame_analysis.py` (analysis maths; also un-forks the tools prototype)
   - `log_events.py` (LogEvent + parsing/formatting)
   - `viewer_widgets.py` (ScrubbableLabel, sliders, marker bar, log model)
   - `annotated_video_widget.py` (840-line widget; add 3 public accessors)
   - `clip_cache.py` (cache root, executors, prefetch, prune — keep thin
     forwarders on the viewer for Main_Window's existing bindings; fix §1.3
     while moving)
   - `qt_worker.py` (Job/JobSlot; then port the five loaders one at a time)
   - `app_main.py` (splash + main(); Main_Window stops being an entry point)
   - shared `ensure_utc` / `_resolve_asset_path` / robot-id helpers.

### Stage 2 — the seams that enable big changes
- **elastic_loader split** → `elastic_client.py`, `elastic_schema.py` (§2),
  `elastic_queries.py`, `elastic_cache.py`, `sku_timeline.py` (band state
  machine as a pure function), with `elastic_loader.py` left as a thin
  façade re-exporting the five public entry points. Est. 2,350 → ~1,100
  lines, ~700 of them testable without a network.
- **`stop_report.py`** — the ~505-line feature out of Main_Window, with its
  Elastic fetch + thumbnail decode moved onto a Job (fixes §1.11's UI
  freeze).
- **`target_overlay_controller.py`** — buffer loading, calibration, gap
  logic, overlay building (~410 lines out of Main_Window); this is the piece
  most likely to change with belt-model work.
- **`log_filter_panel.py`**, **`analysis_panel.py`**, **`clip_annotations.py`**,
  **`playback_overlay.py`**, **`clip_export.py`**, **`elastic_log_session.py`**
  out of the viewer (Tier B; each has a defined state boundary — see the
  viewer table in §7).
- Replace the overview→viewer 150 ms polling state machine
  (`Main_Window:1890-2009`) with signal-driven completion.
- Overview/timeline scene rebuilds → build-once + mutate-in-place (the
  `mark_video_cached` pattern); persistent hover items.

### Stage 3 — risky cores, last
Progress 2026-09-03: `ocr_offset_store.py` extracted (`7f9aa53`);
viewer `__init__` split into 8 ordered build sections (`d082d2f`);
MainWindow `__init__` split into 4 (`eb2acca`). Remaining below:
TimeAlignment, VideoSource, Tesseract-to-worker is DONE (`249f8a2`).
- `VideoSource` wrapping cap/fps/seek-vs-grab (de-duplicates main/secondary;
  touches the hot path — do only with the sequential-read invariant tested).
- A `TimeAlignment` value object (`sync_offset`, `time_offset`,
  `ocr_frame_offset`, `fps`) replacing the triplicated, sign-flipped
  playback-time maths (three different drift conventions today).
- OCR: extract the offset-cache JSON layer now (`ocr_offset_store.py`,
  free); leave sync orchestration in place; move Tesseract to a worker.
- Split the viewer/Main_Window `__init__`s in place
  (`_build_widgets`/`_build_layout`/`_wire_signals`) — 825 and 219 lines
  respectively.

### Test targets (pure today, zero coverage)
`_extract_ui_selection`; the SKU band state machine; overview
`_summarize_system` (149 lines — highest-value single test in the repo);
`build_events_from_rows`; `_prune_cache` (deletes user data);
`_compute_gap_target_ids`; `_categorize_stop_event`; custom-filter matching;
query-builder snapshots; `TimelineItem` (de)serialization round-trip;
fleetwide dedup/bucket/classification walks; settings backfill rules.

## 6. Sizing

Stage 0+1 ≈ 2–3 sessions, low risk, immediately makes files readable.
Stage 2 ≈ a week of sessions, done one seam at a time with the smoke test +
growing pytest suite between each. Stage 3 only as needed by the actual
feature work. End state: no file over ~1,500 lines, the Argus schema in one
place, one worker pattern, and the pure logic (~2,000 lines of it) under
test.

## 7. Where the viewer's 5,170 lines actually go

| Concern | ~Lines | Destination |
|---|---|---|
| `__init__` construction | 825 | split in place |
| Log filtering UI | 663 | `log_filter_panel.py` |
| Cache management | 460 | `clip_cache.py` |
| Analysis / optical flow | 420 | `frame_analysis.py` + `analysis_panel.py` |
| OCR sync + offset cache | 390 | `ocr_offset_store.py` now; rest later |
| Secondary video | 357 | stays; de-duplicated via `VideoSource` |
| Clip open/download lifecycle | 365 | stays (subtle; move with VideoSource) |
| Event markers + time alignment | 300 | `TimeAlignment` + stays |
| Annotations | 235 | `clip_annotations.py` |
| Playback + scrubbing | 215 | stays (core) |
| Popouts, chrome, settings, export, HUD, Elastic session | ~880 | `clip_export.py`, `elastic_log_session.py`, `playback_overlay.py`, misc |
