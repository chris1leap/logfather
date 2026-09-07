"""Data sources dialog (Chris, 2026-09-07): the one place for where the app
reads from: the CCTV share, Elastic, and Grafana. Each has a Test button
that runs off the UI thread and reports in a sentence.

Saving writes onto the viewer's Settings object and calls the viewer's
save, so the usual settings_saved path (main window reload, parent dir
re-applied) runs exactly as it does for the Settings tab.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import requests
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from logfather.core.grafana import grafana_base
from logfather.data import grafana_client
from logfather.data.settings_store import GRAFANA_URL_DEFAULT, Settings
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot


def _test_cctv(path_text: str) -> str:
    if not path_text:
        return "No folder set."
    root = Path(path_text)
    if not root.exists():
        return "Folder not found."
    systems = [p.name for p in root.iterdir() if p.is_dir() and p.name.lower().startswith("pikpak")]
    return f"OK: {len(systems)} PikPak folders."


def _test_elastic(url: str, api_key: str) -> str:
    if not url:
        return "No URL set."
    if not api_key:
        return "No API key set."
    base = url.strip().rstrip("/").replace(".kb.", ".es.")
    resp = requests.get(base + "/", headers={"Authorization": f"ApiKey {api_key}"}, timeout=15)
    if resp.status_code == 200:
        version = ((resp.json() or {}).get("version") or {}).get("number", "")
        return f"OK: Elasticsearch {version}".rstrip()
    if resp.status_code in (401, 403):
        return f"Rejected (HTTP {resp.status_code}): check the API key."
    return f"HTTP {resp.status_code}."


def _test_grafana(url: str, token: str) -> str:
    probe = Settings(grafana_url=grafana_base(url) or GRAFANA_URL_DEFAULT, grafana_token=token or None)
    info = grafana_client.health(probe)
    if not token:
        return f"Reachable (Grafana {info.get('version', '')}), but no token set."
    org = grafana_client.org(probe)
    return f"OK: Grafana {info.get('version', '')}, org {org.get('name', '')}."


class DataSourcesDialog(QDialog):
    def __init__(self, settings: Settings, on_save: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data sources")
        self.setMinimumWidth(640)
        self._settings = settings
        self._on_save = on_save
        self._slots: dict[str, JobSlot] = {k: JobSlot(self) for k in ("cctv", "elastic", "grafana")}
        self._status: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        intro = QLabel("Where the Logfather reads from. Test each one before saving.")
        intro.setStyleSheet(theme.MUTED_LABEL)
        layout.addWidget(intro)

        # CCTV share
        self.cctv_edit = QLineEdit(settings.last_parent or "")
        self.cctv_edit.setPlaceholderText(r"Z:\public  (the folder holding PikPak001, PikPak002, ...)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.cctv_edit, 1)
        row.addWidget(browse)
        layout.addWidget(self._group("CCTV share", "cctv", [("PikPak parent folder", row)],
                                     "The network share with one folder per system; clips are kept there for about 30 days."))

        # Elastic
        self.elastic_url_edit = QLineEdit(settings.elastic_url or "")
        self.elastic_key_edit = QLineEdit(settings.elastic_api_key or "")
        self.elastic_key_edit.setEchoMode(QLineEdit.Password)
        self.elastic_key_edit.setPlaceholderText("API key (base64 id:secret)")
        layout.addWidget(self._group("Elastic", "elastic",
                                     [("Kibana or Elasticsearch URL", self.elastic_url_edit), ("API key", self.elastic_key_edit)],
                                     "Robot logs: errors, stops, picks, state changes. A Kibana URL is converted to the Elasticsearch endpoint."))

        # Grafana
        self.grafana_url_edit = QLineEdit(settings.grafana_url or "")
        self.grafana_url_edit.setPlaceholderText(GRAFANA_URL_DEFAULT)
        self.grafana_token_edit = QLineEdit(settings.grafana_token or "")
        self.grafana_token_edit.setEchoMode(QLineEdit.Password)
        self.grafana_token_edit.setPlaceholderText("service account token (Viewer role)")
        layout.addWidget(self._group("Grafana", "grafana",
                                     [("Grafana URL", self.grafana_url_edit), ("Token", self.grafana_token_edit)],
                                     "Telemetry: temperatures and motor currents, read through Grafana's own API so the backend never matters here."))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setDefault(True)
        save.setStyleSheet(theme.PRIMARY_ACTION_BUTTON)
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _group(self, title: str, key: str, rows: list[tuple[str, object]], hint: str) -> QGroupBox:
        box = QGroupBox(title)
        outer = QVBoxLayout(box)
        hint_label = QLabel(hint)
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet(theme.HINT_LABEL)
        outer.addWidget(hint_label)
        form = QFormLayout()
        for label, widget in rows:
            if isinstance(widget, QWidget):
                form.addRow(label, widget)
            else:
                form.addRow(label, widget)
        outer.addLayout(form)
        foot = QHBoxLayout()
        status = QLabel("")
        status.setStyleSheet(theme.MUTED_LABEL)
        status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._status[key] = status
        test = QPushButton("Test")
        test.clicked.connect(lambda _checked=False, k=key: self._run_test(k))
        foot.addWidget(status, 1)
        foot.addWidget(test)
        outer.addLayout(foot)
        return box

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select the folder holding the PikPak folders")
        if folder:
            self.cctv_edit.setText(folder)

    def _run_test(self, key: str) -> None:
        status = self._status[key]
        status.setText("Testing…")
        if key == "cctv":
            path_text = self.cctv_edit.text().strip()
            fn = lambda _job: _test_cctv(path_text)
        elif key == "elastic":
            url, api_key = self.elastic_url_edit.text().strip(), self.elastic_key_edit.text().strip()
            fn = lambda _job: _test_elastic(url, api_key)
        else:
            url, token = self.grafana_url_edit.text().strip(), self.grafana_token_edit.text().strip()
            fn = lambda _job: _test_grafana(url, token)
        self._slots[key].start(
            fn,
            on_result=lambda text, s=status: s.setText(str(text)),
            on_error=lambda message, s=status: s.setText(f"Failed: {message}"),
        )

    def _save(self) -> None:
        s = self._settings
        s.last_parent = self.cctv_edit.text().strip() or None
        s.elastic_url = self.elastic_url_edit.text().strip() or None
        s.elastic_api_key = self.elastic_key_edit.text().strip() or None
        s.grafana_url = grafana_base(self.grafana_url_edit.text()) or None
        s.grafana_token = self.grafana_token_edit.text().strip() or None
        self._on_save()
        self.accept()
