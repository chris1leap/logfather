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
from logfather.data.elastic_catalog import ElasticCatalog, FieldValues, fetch_catalog, fetch_field_values
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot


class FieldValuesDialog(QDialog):
    """One field on one node (Chris, 2026-09-06): what it is, how often
    it is present, and every value it has held over the last year."""

    def __init__(self, settings_provider: Callable, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(640, 460)
        self.resize(820, 700)
        self._settings_provider = settings_provider
        self._slot = JobSlot(self)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self._heading = QLabel("")
        self._heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: bold; font-size: 16px;")
        layout.addWidget(self._heading)
        self._description = QLabel("")
        self._description.setWordWrap(True)
        self._description.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        layout.addWidget(self._description)
        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(theme.MUTED_LABEL)
        layout.addWidget(self._summary)
        self._progress = QProgressBar()
        self._progress.setFixedHeight(8)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 0)
        self._progress.hide()
        layout.addWidget(self._progress)
        self._table = QTableWidget()
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels(["Value", "Documents", "Share"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, 1)

    def show_field(self, source: str, field: str) -> None:
        node = source.rstrip("/").split("/")[-1]
        self.setWindowTitle(f"{field} — {node}")
        self._heading.setText(f"{field}  <span style='color:{theme.TEXT_MUTED}; font-weight:normal;'>on {source}</span>")
        self._description.setText("")
        self._summary.setText("Querying Elastic for the last year (this can take a while)...")
        self._table.setRowCount(0)
        self._progress.show()
        self.show()
        self.raise_()
        self.activateWindow()
        settings = self._settings_provider()
        self._slot.start(
            lambda job: fetch_field_values(settings, source, field, progress=job.emit_progress),
            on_result=self._on_result,
            on_error=lambda m: self._summary.setText(f"Failed: {m}"),
            on_progress=lambda m: self._summary.setText(str(m or "")),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self) -> None:
        self._slot.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _on_result(self, fv: FieldValues) -> None:
        self._description.setText(fv.description)
        parts = []
        if fv.node_docs:
            parts.append(f"present in {fv.docs_with_field / fv.node_docs * 100:.0f}% of this node's {format_count(fv.node_docs)} documents over the last {fv.days} days")
        else:
            parts.append(f"{format_count(fv.docs_with_field)} documents carry it over the last {fv.days} days")
        parts.append(f"type {fv.es_type}")
        if fv.distinct is not None:
            shown = min(len(fv.values), fv.distinct)
            parts.append(f"{fv.distinct:,} distinct values" + (f" (top {shown:,} shown)" if fv.distinct > len(fv.values) else ""))
        if fv.stats:
            parts.append("min {min:g} · avg {avg:.4g} · max {max:g}".format(**{k: float(v) for k, v in fv.stats.items()}))
        if fv.subfields:
            parts.append("sub-fields: " + ", ".join(fv.subfields))
        if fv.sampled:
            parts.append("values from the latest 200 documents, not an exact count")
        self._summary.setText(" · ".join(parts))
        table = self._table
        table.setRowCount(len(fv.values))
        base = fv.docs_with_field or sum(n for _v, n in fv.values) or 1
        for r, (value, count) in enumerate(fv.values):
            for c, text in enumerate((value, f"{count:,}", f"{count / base * 100:.1f}%")):
                item = QTableWidgetItem(text)
                if c:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
        table.resizeRowsToContents()


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
        self._catalog: ElasticCatalog | None = None
        self._field_dialog: FieldValuesDialog | None = None
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        intro = QLabel(
            "Every PikPak node writes its own documents to Elastic. Each row is one node: what it "
            "logs, how much of the last week's data it accounts for, example messages and the "
            "fields its documents carry. Every document also has the timestamp, the system id, "
            "the source node and the message. Click a field for what it is and the values it "
            "has held over the last year."
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
        if self._field_dialog is not None:
            self._field_dialog.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _fields_widget(self, fields: list[str], row: int) -> QLabel:
        half = (len(fields) + 1) // 2
        left, right = fields[:half], fields[half:]

        def link(name: str) -> str:
            if not name:
                return ""
            return f"<a href='{row}|{name}' style='color:{theme.ACCENT}; text-decoration:none;'>{name}</a>"

        rows = "".join(
            f"<tr><td style='padding-right:18px'>{link(a)}</td><td>{link(b)}</td></tr>"
            for a, b in zip(left, right + [""] * (len(left) - len(right)))
        )
        label = QLabel(f"<table cellspacing='0'>{rows}</table>" if fields else "—")
        label.setTextFormat(Qt.RichText)
        label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        label.setContentsMargins(4, 2, 4, 2)
        label.setStyleSheet("background: transparent;")
        label.setOpenExternalLinks(False)
        label.setCursor(Qt.PointingHandCursor)
        label.linkActivated.connect(self._on_field_link)
        return label

    def _on_field_link(self, href: str) -> None:
        row_text, _sep, field = str(href).partition("|")
        if self._catalog is None or not field:
            return
        try:
            source = self._catalog.entries[int(row_text)].source
        except (ValueError, IndexError):
            return
        if self._field_dialog is None:
            self._field_dialog = FieldValuesDialog(self._settings_provider, parent=self)
        self._field_dialog.show_field(source, field)

    def _on_result(self, catalog: ElasticCatalog) -> None:
        self._loaded = True
        self._catalog = catalog
        table = self._table
        table.setRowCount(len(catalog.entries))
        for r, e in enumerate(catalog.entries):
            cells = [
                e.source,
                e.kind,
                e.description or "—",
                f"{e.share * 100:.1f}%  ({format_count(e.docs)} docs)",
                "\n".join(e.examples) or "—",
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c == 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
            # Fields in two side-by-side columns (Chris, 2026-09-06): the
            # longest text, so it gets the widest column and half the rows.
            table.setItem(r, 5, QTableWidgetItem(""))
            table.setCellWidget(r, 5, self._fields_widget(e.fields, r))
        for c, w in enumerate((220, 120, 320, 140, 230)):
            table.setColumnWidth(c, w)
        table.resizeRowsToContents()
        self._status.setText(
            f"{format_count(catalog.total_docs)} documents in the last {catalog.days} days across "
            f"{len(catalog.entries)} nodes, largest first"
        )
