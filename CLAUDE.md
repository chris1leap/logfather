# The Logfather — dev notes

CCTV video + Elastic log viewer (PySide6/OpenCV), originally from a colleague.
Chris directs the work and reviews visually; Claude implements, tests, and commits.

## Run / test commands

```
.venv\Scripts\python.exe src\Main_Window.py        # run the app
.venv\Scripts\python.exe tools\smoke_test.py       # after EVERY edit: imports + offscreen window build
.venv\Scripts\python.exe -m pytest                 # unit tests
.venv\Scripts\python.exe tools\elastic_api_check.py  # live Elastic query with the app's settings
```

## Repo layout

`src/` all app modules (flat imports — they must stay in one directory) · `assets/`
icons/splash media/diagram · `docs/` architecture notes · `tools/` smoke test, Elastic
check, standalone scripts · `tests/` pytest · `legacy/` old variants, don't touch.
Deep architecture map: `docs/ARCHITECTURE.md`.

## Per-change loop

1. Small, single-purpose change.
2. Smoke test, then pytest. Both must pass before showing Chris.
3. Relaunch the app (background) for Chris's visual check.
4. Commit once verified and push immediately (no need to ask). When a function with pure logic is
   touched, add/extend its tests in `tests/`.

## Gotchas — learned the hard way (2026-09-02)

- **Settings live at `~/.cctv_picker_settings.json`** (home dir, leading dot).
  The `cctv_picker_settings.json` in this repo folder is the colleague's export;
  the app NEVER reads it. Back up the home file before editing it.
- **Video root**: the CCTV share is `Z:/public` on Chris's machine (IONOS HiDrive);
  the colleague maps the same share as `Y:`. Clip layout: `PikPak<NNN>/YYYY/MM/DD/*.mp4`,
  filenames carry `YYYYMMDDHHMMSS` local time.
- **Robot IDs**: folder `PikPak012` ↔ robot `35-2300-012`. Elastic docs carry the id
  in `leap_robot_id` OR `system_id` — always handle both (see `_extract_hit_robot_id`).
- **Elastic**: settings hold a Kibana URL; queries go to the ES host (`.kb.` → `.es.`),
  index pattern `logstash-*,pikpak,pikpak-*`. Never commit API keys — the standalone
  downloader reads `LOGFATHER_ELASTIC_API_KEY` from the environment.
- **No BOM in JSON**: PowerShell `Set-Content -Encoding utf8` writes a BOM that the
  app's `json.loads(path.read_text())` rejects, silently resetting all settings.
  Edit JSON with the Edit tool or `[IO.File]::WriteAllText` with `UTF8Encoding($false)`.
- **One app instance at a time**: settings autosave; a stale instance holding old
  settings can overwrite the file. Check for running instances before diagnosing
  "settings not applied" and kill stale ones hard (skips save-on-exit).
- `git push` may be blocked by the permission classifier when the session's working
  directory is elsewhere — give Chris a Run-button command with a `C:/...` path
  (works in any shell; `/c/...` only works in Git Bash).

## Repo hygiene

- `*_backup.py`, `*_old.py`, `*_no_*.py` variants are gitignored leftovers — don't
  import them, don't extend them.
- `build.ps1` builds the Windows exe/installer (only for releases, not dev).
- Tesseract OCR binary is optional; OCR features degrade gracefully without it.
