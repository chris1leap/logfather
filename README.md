# The Logfather

Desktop GUI for browsing CCTV clips by date/time, playing video, and aligning clips with Elastic logs and custom event markers. Built with PySide6 and OpenCV.

## Features
- Date/time picker with timeline tracks for video and events
- Video playback with scrubbing, time offsets, and OCR-assisted sync
- Elastic log fetch and filtering (sources, states, messages)
- SKU timeline track sourced from UI-node logs, with manual-mode segments and tray/tool context
- Live Overview mode for today's systems with `1h`, `5h`, and `All Day` windows, grouped by customer and ordered by production line
- Fleetwide Elastic Search dashboard with configurable searches, 1/7/30/90-day graphs, occurrence counts, and serial/customer filtering
- Fleetwide occurrence filtering for all events, events during inferred operation, or events during startup/stopped periods
- Fleetwide results hide systems whose selected occurrence mode is zero, as well as query-failure cards
- Fleetwide graph buckets use Elasticsearch-aligned fixed boundaries with vertical grid lines and cursor start/end times
- Fleetwide occurrence counts apply a 30-second per-system cooldown to suppress duplicate servo-level records
- Fleetwide graphs use stacked bars (red during operation, orange during startup/stopped), and search buttons support multi-select OR queries
- Fleetwide system cards automatically use two columns on wide maximized/full-screen layouts
- Default fleetwide example tracks exact `update_info.keyword` occurrences of `Error Reset or No Error :: N/A`
- Overview-to-main click-through that waits for the selected system/day timeline to finish loading, opens the matching clip, and seeks directly to the clicked time
- Customer and production-line metadata management in the right-panel `Systems` tab, including customer logo support
- Grouped date picker with collapsible customer sections, customer logos, and per-system "today" shortcut buttons
- Custom filter presets and named conditions
- Fifteen configurable Elastic search conditions with corresponding event-marker tracks
- Frame analysis (frame differencing + optical flow) with overlay, side-by-side, or popout display
- Additional CCTV clip support (secondary video)
- Stop Report view with stop-event thumbnails and click-through navigation to the exact clip/time
- On-video feed-rate overlay (PPM) computed from `Adding new target to queue` log events, plus current SKU / tray / tool overlay
- Conveyor target tracking overlay with line-based calibration, per-target detail cards, and baked export support
- Seek-bar clip range marking with draggable in/out markers and clip export
- Exported clip segments can bake in the current annotation layer and on-video status overlay
- Local cache for faster playback and OCR
- README tab inside the Settings dialog
- Animated splash screen shown during startup (Argus II). Click the splash to play the full intro.
- Argus II placeholder artwork when no video is loaded

## Project Structure
- `Main_Window.py` - App entry point, wiring between date picker, time picker, and viewer
- `Log_vid_gui.py` - Main video/log viewer UI and playback logic
- `Time_Picker.py` - Timeline view and clip loading
- `Date_Picker_frontend.py` - Date picker UI
- `overview_widget.py` - Multi-system live overview board for the current day
- `fleetwide_elastic_search_widget.py` - Fleetwide Elastic search dashboard, system cards, graphs, and search settings
- `elastic_loader.py` / `elastic_errors.py` - Elastic query + error handling
- `settings_store.py` / `settings_dialog.py` - Settings model + UI
- `time_ocr.py` / `time_ocr_settings.json` - OCR sync tooling and settings
- `Vid_Frame_Differencing.py` / `logs_to_srt.py` - Utilities (optional)
- `VideoLogViewer.iss` - Inno Setup packaging script

## Requirements
- Python 3.10+ (tested with 3.12)
- PySide6 (Qt 6)
- OpenCV (`opencv-python`)
- pytesseract
- Tesseract OCR (bundled for the installer build; required for OCR sync features)
- Other dependencies are standard library unless noted above

## Setup
1. Create a virtual environment:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   ```
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Run the app:
   ```
   python Main_Window.py
   ```
4. For local OCR (non-bundled), install Tesseract and ensure it's on PATH.

## Usage
- Pick a root folder and date to populate the timeline.
- Click a video block to load it in the viewer.
- Use the timeline markers and log filters to align events.
- Use the `Fit` button next to `Refresh` to zoom the timeline to the available clips.
- Use the `Overview` button to open the live multi-system overview for today; systems are grouped by customer and ordered by production line.
- Hover over overview to see a live time cursor and click a timeline row to open the corresponding main-view clip/time.
- Overview click-through now seeks from the selected clip's timeline span immediately; OCR can still run afterward without changing the initial jump.
- Use the top-left `Fleetwide Search` button to compare configured Elastic occurrences across every system folder. Select one or more named search buttons to OR their queries together, then click `Run Search`; changing the selection or date range does not query Elastic automatically.
- Choose a `1`, `7`, `30`, or `90` day range, then filter cards by serial number or customer. Search definitions are managed from the dashboard's `Search Settings` page.
- Fleetwide occurrence modes show all matches, matches during inferred operation, or matches during startup/stopped periods. Operation starts 60 seconds after `start_pnp` and ends on stop, manual, caution, emergency-stop, protective-stop, or shutdown events.
- Fleetwide counts suppress duplicate servo records with a 30-second per-search cooldown. Stacked graph bars are red during operation and orange during startup/stopped periods; hover a bucket for its time range and category counts.
- Fleetwide cards with zero occurrences or failed queries are hidden. Wide maximized/full-screen layouts automatically arrange the remaining cards into two columns.
- Use the right-panel `Systems` tab to define customers, assign systems to customers/lines, and paste customer logos from the clipboard.
- Use `Stop Report` to generate a day report of robot stop events with thumbnails; click a thumbnail to jump the main viewer to that event.
- Clip-range Elastic logs now auto-load in the background when a clip is opened.
- SKU selections appear as a dedicated track; manual mode shows in orange until the next SKU selection.
- The lower marker bar can render a standout red marker as a small triangle to make the key event easier to spot while scrubbing.
- Right-click the seek bar under the video to set clip start/end markers, drag them to adjust, and export the selected range.
- Seek-bar clip export bakes in the current annotations and on-video overlay text. If `ffmpeg` is available, source audio is muxed back into the exported file.
- Frame analysis controls live in the annotation popout; choose Frame Diff or Optical Flow and set display mode.
- Optional: run OCR sync via the "Sync Time" button.
- Optional: enable auto-open OCR to pop the OCR tool when no cached offset exists; it will run and close automatically on success.
- Additional CCTV clips can auto-sync via OCR when opened (same setting applies).
- Settings autosave after edits, and the `Settings` tab also includes a manual `Save Settings` button.
- Settings includes a Readme tab for quick reference.
- SIM Logs button loads Elastic logs for system id `35-2300-SIM` on any selected day.

## Versioning
- Builds embed `version.json` with version, build date, and git SHA.
- `build.ps1` auto-generates a date-based version if none is provided.
- The splash screen shows the version + git SHA (build date is omitted in the splash label).
- The Readme tab loads `README.md` from the app folder (bundled at build time for the exe).

## Packaging (Windows)
- `build.ps1` auto-selects the spec file:
  - `VideoLogViewer_tesseract.spec` (if present)
  - otherwise `VideoLogViewer.spec`
- `VideoLogViewer.spec` bundles runtime assets used by the UI:
  - `Logfather animated splash screen Argus II.mp4`
  - `Logfather Argus II.jpg`
  - `logfather.png` and `logfather.ico`
- Build app bundle (and installer if Inno Setup is available):
  ```
  .\build.ps1
  ```
- `build.ps1` will stop any running `dist\The Logfather\The Logfather.exe` before building.
- If `dist\The Logfather` is locked, the script retries cleanup and then falls back to `dist\build_<timestamp>`.
- If `iscc` is not installed/on PATH, the script skips installer creation and still completes the app bundle build.

## Settings & Cache
- User settings are stored at:
  - Windows: `C:\Users\<you>\.cctv_picker_settings.json`
- OCR auto-sync option:
  - `Auto-sync logs using OCR` runs OCR without opening the tool and auto-opens the OCR tool when an offset is missing.
- Settings save behavior:
  - most fields autosave shortly after edits
  - the `PikPak parent` field also saves when browsing for a folder or when the field loses focus
  - the `Save Settings` button forces an immediate write to disk
- Video cache (local copies) is stored at:
  - Windows: `%LOCALAPPDATA%\VideoLogViewer\cache`
- Elastic event cache is stored under:
  - Windows: `%LOCALAPPDATA%\VideoLogViewer\cache\elastic_events`
- Cache pruning is automatic:
  - cached clips older than `30` days are removed even if the cache is not full
  - cache size is capped at `30 GB` using least-recently-used pruning
- Viewer cache controls:
  - `Clear Cache` removes all local cache content, including staged videos and Elastic event cache
  - `Clear Event Cache` removes only cached Elastic event results
- OCR settings: `time_ocr_settings.json`

## Notes
- If you access clips on a network drive, the app will cache locally for stability.
- The Additional CCTV view is optional and can be loaded from the UI.

## Troubleshooting
- **Crash on video load:** Clear the cache and retry. If it persists, test with a local clip.
- **No logs:** Check Elastic settings (URL, index, timestamp field) in settings.
- **Logs are one hour out from video around BST/DST:** The app now interprets clip filename timestamps in `Europe/London` local time and queries Elastic using local-day boundaries. If a previously viewed day still looks wrong, clear the Elastic event cache and reload.
- **Log list times in the right sidebar look one hour out:** The visible `Log entries` timestamps are now formatted for `Europe/London` display time while keeping the underlying sync math unchanged.
- **The green playhead line on the day timeline is one hour out:** The timeline playhead now treats naive viewer playback times as `Europe/London` local time before converting to UTC scene position.
- **Bundled OCR reports missing `eng.traineddata` after reinstalling Tesseract:** The bundled app now forces `TESSDATA_PREFIX` to its bundled `tessdata` folder so stale system-level Tesseract paths do not override the packaged runtime.
- **SKU / tray / tool overlay is about one hour out:** The on-video SKU overlay now treats naive playback times as `Europe/London` local time before converting to UTC for timeline-item matching.
- **Clicking a log/event lands slightly off the shown event time:** Event jumps now apply the OCR frame offset as well as the coarse/fine log offset so the jump target matches the displayed calculated clock more closely.
- **Using the drift control makes events look even more out of sync:** The viewer clock now stays tied to OCR/video time while the drift control adjusts only log alignment, so the calculated clock, log jumps, and overlay context move consistently again.
- **OCR issues:** Open the OCR ROI tool and adjust the region.

## To Do
- Refine the end of a manual run when the system is switched off so manual spans terminate more accurately.
- Collapse by default is working on the main view but not on the overview.
- A system with no Elastic events can load the wrong data; for example, PikPak 11 loading EVG data.
- Add a settings toggle to enable/disable verbose timeline and Elastic performance logs.
- In the stop report - Stack thumbnails that are in the same clip

## History
- **08/02/2026:** Intermittent crash when changing clips (`python.exe` faulting in `Qt6Widgets.dll`, exception `0xc0000005`). Resolved by guarding timeline cursor/playhead updates during clip/timeline rebuilds and validating that QGraphics items still belong to the active scene before reuse.
- **08/02/2026:** Improved small-screen usability by shrinking the date picker/calendar/buttons and adding hover-reveal side panels (left date picker and right logs/filters).
- **08/02/2026:** Added Bird's Eye annotation tool and popout view with live updates, edit handles, and playback controls to manage tray perspective.
- **12/02/2026:** Added SKU track derived from `/leap/manip1/ui_node` logs, with manual-mode segments and tray/tool details.
- **13/02/2026:** Added README tab in Settings and a startup splash screen.
- **13/02/2026:** Added build version display on the README tab.
- **13/02/2026:** Renamed the app to The Logfather and updated the splash artwork.
- **13/02/2026:** Added frame analysis (diff/optical flow) with overlay, side-by-side, and popout modes.
- **13/02/2026:** Show the splash artwork when no video is loaded and clear the view when switching PikPak or day.
- **13/02/2026:** Added a `Fit` button to reframe the timeline and improved PikPak button readability in light themes.
- **09/03/2026:** Optimised timeline loading and Elastic query flow (connection reuse, cache usage, reduced UI blocking, and loader throughput improvements), and restored SKU/manual compatibility across Argus 1.x and 2.x log schemas.
- **10/03/2026:** Added Stop Report with stop-event thumbnails, clip pre-caching for report generation, operator-stop inclusion, and click-through navigation to exact event times.
- **10/03/2026:** Refined playback controls with fixed-slot segment-style time/frame displays and subtle side-panel slide animations.
- **10/03/2026:** Added delayed hover-expand timeline behavior, faster large-log list reset on clip change, and automatic background Elastic log loading per clip.
- **10/03/2026:** Added on-video PPM stats overlay from `Adding new target to queue` events (`Now`, `Avg60s`, `AvgAll`) and Stop Report SKU mapping from timeline intervals.
- **19/03/2026:** Added live Overview mode for today's systems with animated time-range switching, grouped customer sections, and customer ordering by production line.
- **19/03/2026:** Added right-panel `Systems` management for customer/line assignments and customer logos, and regrouped both overview and date picker by customer metadata.
- **19/03/2026:** Expanded stop/bar termination handling to include shutdown messages and `system_shutdown` service calls from Elastic data.
- **20/03/2026:** Added overview click-through into the main view with clip/time resolution, OCR-synced seeking, and hover time cursor feedback.
- **20/03/2026:** Refined the grouped date picker with collapsible customer sections, logo headers, compact customer rows, and today shortcut buttons.
- **20/03/2026:** Added current system context in the top bar, extended the on-video status overlay with SKU / tray / tool, and added automatic cache pruning for GDPR/size control.
- **20/03/2026:** Added customer-level `Start Collapsed` defaults in the Systems tab and applied grouped customer expand/collapse behavior across the date picker and overview.
- **20/03/2026:** Added overview loading video/progress feedback, enabled autosave for Settings and Systems changes, and reduced heavy reloads during settings updates.
- **07/04/2026:** Keyed Elastic event cache by the selected PikPak path, added a dedicated `Clear Event Cache` action, and rejected stale timeline loads when the selected PikPak changes mid-load.
- **07/04/2026:** Fixed UK BST/DST alignment by treating clip filename timestamps as `Europe/London` local time and converting selected local calendar days to UTC for Elastic queries and timeline placement.
- **07/04/2026:** Moved clip-range selection to the seek bar, added draggable in/out markers plus export actions, and enabled baked-overlay clip export including annotations and on-video status text.
- **07/04/2026:** Corrected viewer log sync/display timezone handling so clip events, seek-bar markers, and right-sidebar `Log entries` stay aligned in UK local time.
- **15/04/2026:** Fixed the day-timeline green playhead marker for UK BST, restored an explicit `Save Settings` button, and tightened settings persistence for the `PikPak parent` field.
- **15/04/2026:** Forced bundled OCR builds to use their packaged `tessdata` path so separate Tesseract installs do not break `eng.traineddata` lookup.
- **24/04/2026:** Hardened overview click-through so it waits for long timeline/event loads, opens the resolved clip more reliably, and seeks directly from the selected clip span instead of waiting on OCR time sync.
- **24/04/2026:** Refined seek-bar marker rendering so only the lower marker bar uses a red triangle for high-priority markers; the upper bar remains line-based.
- **24/04/2026:** Aligned event-jump scrubbing with the displayed OCR-adjusted clock by applying `ocr_frame_offset` during event seeks and adjacent-event navigation.
- **24/04/2026:** Corrected SKU / tray / tool overlay matching for UK local playback times so the on-video status overlay no longer drifts by an hour around BST.
- **19/05/2026:** Fixed drift-control sync regression where fine log offset adjustments also shifted the displayed playback clock and overlay context, making events appear more out of sync instead of less.
- **19/05/2026:** Added line-based conveyor target tracking, gap alerts, richer target overlay/detail output, and export support for tracked target annotations.
- **25/08/2026:** Added the Fleetwide Elastic Search dashboard with configurable multi-select OR searches, 1/7/30/90-day ranges, customer/serial filtering, responsive system cards, and persisted search settings.
- **25/08/2026:** Added operation-aware fleetwide occurrence classification, 30-second duplicate suppression, optimized Argus 1/2 Elastic queries, zero/error card hiding, and UTC-aligned stacked bucket charts with hover details.
- **25/08/2026:** Expanded configurable Elastic timeline conditions and corresponding marker tracks from 10 to 15, with a scrollable settings editor.
- **25/08/2026:** Added target-rate heat strips to the day timeline and selected clip, and kept tracked overlay timing aligned with the viewer drift offset.

## Attribution
This project uses the following third-party libraries and tools:
- PySide6 (Qt for Python)
- Qt (GUI framework)
- OpenCV (`opencv-python`)
- NumPy
- Requests
- pytesseract
- Tesseract OCR
- Inno Setup (Windows installer)

## Tesseract OCR
OCR sync requires Tesseract. For the packaged installer build, Tesseract is bundled. For local development runs, install Tesseract and ensure it is available on PATH. Download here:
```
https://github.com/tesseract-ocr/tesseract
```
When the packaged build uses the bundled OCR runtime, it also forces `TESSDATA_PREFIX` to the bundled `tessdata` directory so separate Tesseract installs do not override the packaged language data.
