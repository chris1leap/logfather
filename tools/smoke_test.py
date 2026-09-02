"""Smoke test: prove the app still starts after a change, without showing a window.

Imports every application module (catches syntax errors, broken imports, and
module-level mistakes), then constructs the full MainWindow with Qt running
offscreen (catches broken wiring in widget constructors).

Run after every edit:

    .venv\\Scripts\\python.exe tools\\smoke_test.py

Exits 0 and prints SMOKE PASS on success; any failure prints the traceback
and exits non-zero.
"""
import importlib
import os
import sys
import traceback
from pathlib import Path

# Run Qt without a display and keep the real user settings untouched.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MODULES = [
    "app_version",
    "about_page",
    "settings_store",
    "elastic_errors",
    "elastic_loader",
    "conveyor_calibration",
    "time_ocr",
    "Time_Picker",
    "Date_Picker_frontend",
    "settings_dialog",
    "conveyor_calibration_dialog",
    "target_buffer_loader",
    "target_buffer_widget",
    "target_scope_widget",
    "overview_widget",
    "fleetwide_elastic_search_widget",
    "Log_vid_gui",
    "Main_Window",
]


def main() -> int:
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            print(f"IMPORT FAILED: {name}", flush=True)
            traceback.print_exc()
            return 1
    print(f"imports OK ({len(MODULES)} modules)", flush=True)

    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        import Main_Window

        app = QApplication.instance() or QApplication(sys.argv)
        window = Main_Window.MainWindow()
        window.show()
        QTimer.singleShot(2500, app.quit)
        app.exec()
        window.close()
    except Exception:
        print("WINDOW CONSTRUCTION FAILED")
        traceback.print_exc()
        return 1

    print("SMOKE PASS", flush=True)
    # Background QThreads (folder scans, log fetches) may still be running;
    # a smoke check doesn't wait for them.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
