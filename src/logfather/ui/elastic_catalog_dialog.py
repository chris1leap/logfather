"""The ? box on the Data window (Chris, 2026-09-06): what kinds of data
Elastic stores, one row per source node with a plain description, its
share of the last week's documents, example messages and the fields
those documents carry."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from logfather.data.data_inventory import format_count
from logfather.data.elastic_catalog import ElasticCatalog, fetch_catalog
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot


class ElasticCatalogDialog(QDialog):
    def __init__(self, settings_provider: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("What is stored in Elastic")
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(900, 520)
        self.resize(1320, 820)
        self._settings_provider = settings_provider
        self._slot = JobSlot(self)
        self._loaded = False
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intro = QLabel(
            "Every PikPak node writes its own documents to Elastic. Each row is one node: what it "
            "logs, how much of the last week's data it accounts for, example messages and the "
            "fields its documents carry. Every document also has the timestamp, the system id, "
            "the source node and the message."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(intro)
        self._status = QLabel("")
        self._status.setStyleSheet(theme.MUTED_LABEL)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        layout.addWidget(self._progress)
        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.setWordWrap(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Node", "Kind", "What it logs", "Share (7 days)", "Example messages", "Fields"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        layout.addWidget(self._table, 1)

    def start_if_needed(self) -> None:
        if self._loaded or self._slot.is_running():
            return
        self._progress.show()
        self._status.setText("Querying Elastic...")
        settings = self._settings_provider()
        self._slot.start(
            lambda job: fetch_catalog(settings, progress=job.emit_progress),
            on_result=self._on_result,
            on_error=lambda m: self._status.setText(f"Failed: {m}"),
            on_progress=lambda m: self._status.setText(str(m or "")),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self) -> None:
        self._slot.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _on_result(self, catalog: ElasticCatalog) -> None:
        self._loaded = True
        table = self._table
        table.setRowCount(len(catalog.entries))
        for r, e in enumerate(catalog.entries):
            cells = [
                e.source,
                e.kind,
                e.description or "—",
                f"{e.share * 100:.1f}%  ({format_count(e.docs)} docs)",
                "\n".join(e.examples) or "—",
                ", ".join(e.fields) or "—",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
        for c, w in enumerate((250, 130, 380, 150, 260)):
            table.setColumnWidth(c, w)
        table.resizeRowsToContents()
        self._status.setText(
            f"{format_count(catalog.total_docs)} documents in the last {catalog.days} days across "
            f"{len(catalog.entries)} nodes, largest first"
        )
