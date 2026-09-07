"""The ? on the Data window's Grafana tile: what is stored in Grafana, one
row per metric; click a row for that metric's series over the last day."""
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

from logfather.data.grafana_catalog import FAMILIES, GrafanaCatalog, MetricDetail, fetch_grafana_catalog, fetch_metric_detail
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot


class MetricDetailDialog(QDialog):
    def __init__(self, settings_provider: Callable, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(640, 420)
        self.resize(900, 640)
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
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(["Series (labels)", "Samples", "Min", "Average", "Max", "Latest"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 6):
            header.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        layout.addWidget(self._table, 1)

    def show_metric(self, name: str) -> None:
        self.setWindowTitle(name)
        self._heading.setText(name)
        self._description.setText("")
        self._summary.setText("Querying Grafana for the last 24 hours...")
        self._table.setRowCount(0)
        self._progress.show()
        self.show()
        self.raise_()
        self.activateWindow()
        settings = self._settings_provider()
        self._slot.start(
            lambda job: fetch_metric_detail(settings, name),
            on_result=self._on_result,
            on_error=lambda m: self._summary.setText(f"Failed: {m}"),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self) -> None:
        self._slot.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _on_result(self, detail: MetricDetail) -> None:
        self._description.setText(detail.meaning)
        self._summary.setText(f"{len(detail.rows)} series over the last {detail.hours} hours, one sample a minute at most in this view.")
        table = self._table
        table.setRowCount(len(detail.rows))
        for r, row in enumerate(detail.rows):
            labels = ", ".join(f"{k}={v}" for k, v in sorted(row.labels.items()))
            cells = (labels, f"{row.samples:,}", f"{row.minimum:.3g}", f"{row.average:.3g}", f"{row.maximum:.3g}", f"{row.last:.3g}")
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                table.setItem(r, c, item)
        table.resizeRowsToContents()


class GrafanaCatalogDialog(QDialog):
    def __init__(self, settings_provider: Callable, parent=None):
        super().__init__(parent)
        self.setWindowTitle("What is stored in Grafana")
        self.setWindowFlags(Qt.Window | Qt.WindowTitleHint | Qt.WindowMinMaxButtonsHint | Qt.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.setMinimumSize(900, 520)
        self.resize(1240, 820)
        self._settings_provider = settings_provider
        self._slot = JobSlot(self)
        self._loaded = False
        self._detail: MetricDetailDialog | None = None
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        families = " · ".join(f"<b>{label}</b>: {text}" for label, text in FAMILIES.values() if label != "Alerts")
        intro = QLabel(
            "Grafana Cloud's Prometheus holds the robots' numeric telemetry: every 30 seconds each "
            "system reports a few hundred readings, each a number against a metric name and labels "
            "(the system id, and a motor id where there is one). Nothing textual lives here; the logs are "
            "in Elastic. Each row is one metric. Click a row for its series over the last day.<br><br>"
            + families
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
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
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setWordWrap(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["Metric", "Family", "What it is", "Argus 1 series", "Argus 2 series"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._table.cellClicked.connect(self._on_row_clicked)
        layout.addWidget(self._table, 1)

    def start_if_needed(self) -> None:
        if self._loaded or self._slot.is_running():
            return
        self._progress.show()
        self._status.setText("Querying Grafana...")
        settings = self._settings_provider()
        self._slot.start(
            lambda job: fetch_grafana_catalog(settings, progress=job.emit_progress),
            on_result=self._on_result,
            on_error=lambda m: self._status.setText(f"Failed: {m}"),
            on_progress=lambda m: self._status.setText(str(m or "")),
            on_finished=lambda: self._progress.hide(),
        )

    def shutdown(self) -> None:
        self._slot.shutdown()
        if self._detail is not None:
            self._detail.shutdown()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def _on_result(self, catalog: GrafanaCatalog) -> None:
        self._loaded = True
        metrics = catalog.metrics
        total1 = sum(m.argus1_series for m in metrics)
        total2 = sum(m.argus2_series for m in metrics)
        self._status.setText(f"{len(metrics)} metrics with a system label · {total1:,} Argus 1 series and {total2:,} Argus 2 series reporting in the last day")
        table = self._table
        table.setRowCount(len(metrics))
        for r, m in enumerate(metrics):
            cells = (m.name, m.family, m.meaning, f"{m.argus1_series:,}" if m.argus1_series else "—", f"{m.argus2_series:,}" if m.argus2_series else "—")
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c >= 3:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 0:
                    item.setForeground(Qt.GlobalColor.white)
                table.setItem(r, c, item)
        table.resizeRowsToContents()

    def _on_row_clicked(self, row: int, _column: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        if self._detail is None:
            self._detail = MetricDetailDialog(self._settings_provider, parent=self)
        self._detail.show_metric(item.text())
