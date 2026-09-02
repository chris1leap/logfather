"""About dialog: which build is running (linked to its GitHub commit), the
architecture diagram, and a plain-English summary of every file in the repo."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app_version import load_version_info

GITHUB_REPO_URL = "https://github.com/chris1leap/logfather"

# (display name, repo path, plain-English summary)
FILE_SUMMARIES: list[tuple[str, str, str]] = [
    ("Main_Window.py", "src/Main_Window.py",
     "The application shell: builds the window, hosts every panel, and routes signals "
     "between them. Also owns the Stop Report, conveyor overlays and pick-buffer loading."),
    ("Log_vid_gui.py", "src/Log_vid_gui.py",
     "The heart of the app: plays a CCTV clip side-by-side with the robot's Elastic logs, "
     "frame-aligned via OCR clock sync. Annotations, second camera, frame analysis, and "
     "export with overlays burned in."),
    ("Time_Picker.py", "src/Time_Picker.py",
     "The 24-hour timeline strip: each clip is a block, with coloured marks for Elastic "
     "events and SKU runs, a live playhead and click-to-open."),
    ("Date_Picker_frontend.py", "src/Date_Picker_frontend.py",
     "The left panel: system buttons grouped by customer, plus a calendar highlighting "
     "the days that actually have footage."),
    ("overview_widget.py", "src/overview_widget.py",
     "The live control-room board: one row per robot showing today's runs, stops and "
     "CCTV coverage, refreshing every minute. Click a row to jump the viewer there."),
    ("fleetwide_elastic_search_widget.py", "src/fleetwide_elastic_search_widget.py",
     "The fleet dashboard: run saved Elastic searches across every system over 1-90 days, "
     "with per-robot counts and bar charts split by operating vs stopped."),
    ("elastic_loader.py", "src/elastic_loader.py",
     "The single gateway to Elastic: builds and paginates every query, caches whole days, "
     "and handles both robot-ID conventions (leap_robot_id / system_id)."),
    ("elastic_errors.py", "src/elastic_errors.py",
     "One tiny exception class that lets a partly-failed Elastic query still deliver "
     "whatever data it managed to fetch."),
    ("settings_store.py", "src/settings_store.py",
     "Everything the app remembers - video root, Elastic key, the 15 condition queries, "
     "customers and fleetwide searches - saved to ~/.cctv_picker_settings.json."),
    ("settings_dialog.py", "src/settings_dialog.py",
     "The Settings UI: connection details, condition rows, and the customer/system layout "
     "tables. Embedded as tabs inside the viewer."),
    ("target_buffer_loader.py", "src/target_buffer_loader.py",
     "Replays 'new pick target' log messages to reconstruct the robot's pick queue at any "
     "instant of a clip."),
    ("target_buffer_widget.py", "src/target_buffer_widget.py",
     "The Targets side panel: an animated card per queued item, updating as the video "
     "plays, with tight/wide-gap highlighting."),
    ("target_scope_widget.py", "src/target_scope_widget.py",
     "A small radar window drawing recent pick targets in camera space. Currently dormant "
     "- the app never opens it."),
    ("conveyor_calibration.py", "src/conveyor_calibration.py",
     "The belt model: stores per-robot conveyor calibrations so queued targets can be "
     "drawn moving along the belt in the video."),
    ("conveyor_calibration_dialog.py", "src/conveyor_calibration_dialog.py",
     "The calibration wizard: click the same belt landmark on two frames a few seconds "
     "apart and it derives the on-screen belt velocity."),
    ("time_ocr.py", "src/time_ocr.py",
     "The clock reader: OCRs the burned-in CCTV timestamp (Tesseract) to pin each clip's "
     "true start time to the frame, with an interactive tuning tool."),
    ("app_version.py", "src/app_version.py",
     "Reports which build is running by reading version.json (stamped by build.ps1 with "
     "version and git commit); falls back to 'dev'."),
    ("about_page.py", "src/about_page.py",
     "This dialog: the version linked to its GitHub commit, the architecture diagram, and "
     "these file summaries."),
    ("tools/elastic-log-download.py", "tools/elastic-log-download.py",
     "Standalone command-line tool that asks Kibana to generate and download a CSV of "
     "logs for a robot and time range."),
    ("tools/smoke_test.py", "tools/smoke_test.py",
     "The after-every-edit safety check: imports all modules and builds the full window "
     "offscreen to catch breakage in seconds."),
    ("tools/elastic_api_check.py", "tools/elastic_api_check.py",
     "One command that proves the Elastic connection works, using the app's own settings."),
    ("tools/Vid_Frame_Differencing.py", "tools/Vid_Frame_Differencing.py",
     "Standalone motion-analysis prototype (frame differencing and optical flow); its "
     "maths was copied into the viewer, kept as reference."),
    ("tools/logs_to_srt.py", "tools/logs_to_srt.py",
     "Legacy one-shot script turning a CSV log export into video subtitles; superseded by "
     "live in-app alignment."),
    ("tests/", "tests",
     "Unit tests (pytest) for the fragile pure logic: robot-ID mapping, timestamp parsing "
     "and filename time extraction."),
    ("build.ps1 + spec/iss files", "build.ps1",
     "The release pipeline: stamps version.json, bundles 'The Logfather' with PyInstaller "
     "(optionally with Tesseract inside), then builds the Windows installer."),
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_diagram_path() -> str | None:
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "logfather_architecture.svg")
    candidates.append(_repo_root() / "assets" / "logfather_architecture.svg")
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _resolve_logo_path() -> str | None:
    candidates = []
    if getattr(sys, "_MEIPASS", None):
        candidates.append(Path(sys._MEIPASS) / "logfather.png")
    candidates.append(_repo_root() / "assets" / "logfather.png")
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def _current_commit() -> str | None:
    """Full SHA of the running code: live git in a source checkout, else the
    build-time SHA baked into version.json (frozen builds have no git)."""
    if not getattr(sys, "_MEIPASS", None):
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(_repo_root()),
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
    sha = str(load_version_info().get("git_sha") or "").strip()
    return sha or None


def _version_text() -> str:
    info = load_version_info()
    version = str(info.get("version") or "dev")
    if getattr(sys, "_MEIPASS", None):
        return f"v{version}"
    return "source checkout"


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("About The Logfather")
        self.resize(1080, 780)
        self.setStyleSheet(
            "QDialog { background: #12181f; }"
            "QLabel { color: #e8eef4; }"
            "QTabWidget::pane { border: 1px solid #2c3946; background: #12181f; }"
            "QTabBar::tab { background: #1a222b; color: #9fb0c0; padding: 6px 16px; }"
            "QTabBar::tab:selected { background: #24435f; color: #e8eef4; }"
            "QPushButton { background: #24435f; color: #e8eef4; border: 1px solid #5b9bd5;"
            " border-radius: 4px; padding: 5px 18px; }"
            "QScrollArea { border: none; background: #12181f; }"
            "QTextBrowser { background: #12181f; border: none; }"
        )

        header = QHBoxLayout()
        logo_path = _resolve_logo_path()
        if logo_path:
            logo = QLabel()
            logo.setPixmap(
                QPixmap(logo_path).scaled(
                    72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
            header.addWidget(logo)
        title_col = QVBoxLayout()
        title = QLabel("The Logfather")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        title_col.addWidget(title)
        subtitle = QLabel("CCTV video + Elastic log viewer")
        subtitle.setStyleSheet("color: #9fb0c0;")
        title_col.addWidget(subtitle)
        title_col.addWidget(self._build_version_label())
        header.addLayout(title_col)
        header.addStretch(1)

        tabs = QTabWidget()
        tabs.addTab(self._build_diagram_tab(), "How it fits together")
        tabs.addTab(self._build_files_tab(), "What each file does")

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)

        layout = QVBoxLayout()
        layout.addLayout(header)
        layout.addWidget(tabs, 1)
        layout.addLayout(close_row)
        self.setLayout(layout)

    def _build_version_label(self) -> QLabel:
        sha = _current_commit()
        parts = [f"Version: {_version_text()}"]
        if sha:
            short = sha[:7]
            parts.append(
                f'commit <a style="color:#5b9bd5" '
                f'href="{GITHUB_REPO_URL}/commit/{sha}">{short}</a>'
            )
        else:
            parts.append("commit unknown")
        label = QLabel(" &nbsp;&middot;&nbsp; ".join(parts))
        label.setTextFormat(Qt.RichText)
        label.setOpenExternalLinks(True)
        label.setStyleSheet("color: #9fb0c0;")
        return label

    def _build_diagram_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(False)
        diagram_path = _resolve_diagram_path()
        if diagram_path:
            svg = QSvgWidget(diagram_path)
            svg.setFixedSize(1180, 880)
            scroll.setWidget(svg)
        else:
            missing = QLabel("Architecture diagram not found (logfather_architecture.svg).")
            missing.setAlignment(Qt.AlignCenter)
            scroll.setWidget(missing)
        return scroll

    def _build_files_tab(self) -> QWidget:
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        rows = []
        for name, repo_path, summary in FILE_SUMMARIES:
            url = f"{GITHUB_REPO_URL}/blob/main/{repo_path}"
            rows.append(
                f'<p style="margin: 7px 0;">'
                f'<a style="color:#5b9bd5; font-weight:bold; text-decoration:none;" '
                f'href="{url}">{name}</a><br/>'
                f'<span style="color:#b8c4d0;">{summary}</span></p>'
            )
        browser.setHtml(
            '<div style="font-size: 13px;">'
            f'<p style="color:#9fb0c0;">Click a file name to open it on GitHub. '
            f'The full write-up lives in <a style="color:#5b9bd5" '
            f'href="{GITHUB_REPO_URL}/blob/main/docs/ARCHITECTURE.md">'
            "docs/ARCHITECTURE.md</a>.</p>" + "".join(rows) + "</div>"
        )
        return browser
