"""Entry shim.

The real module lives at logfather/ui/Main_Window.py since the package
split; this shim keeps the long-standing entry points working unchanged:

    .venv/Scripts/python.exe src/Main_Window.py       (dev run, desktop shortcut)
    PyInstaller specs (Analysis(['src/Main_Window.py']))
"""
import sys
from pathlib import Path

_src = str(Path(__file__).resolve().parent)
if _src not in sys.path:
    sys.path.insert(0, _src)

from logfather.ui.Main_Window import MainWindow  # noqa: E402,F401

if __name__ == "__main__":
    from logfather.ui.app_main import main

    main()
