"""The gear button that sits top-right on every window (Chris, 2026-09-07):
one dropdown with Data sources, Settings, Systems, Readme, the zoom row and
About. Secondary windows add their own items above the shared ones.

The window that owns the dialogs (the main window) is the `host`; the other
windows receive it and only forward clicks.
"""
from __future__ import annotations

from typing import Iterable, Protocol

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget, QWidgetAction

from logfather.ui import theme
from logfather.ui.icons import gear_icon, zoom_glyph_icon


class GearHost(Protocol):
    def open_data_sources(self) -> None: ...
    def open_settings(self) -> None: ...
    def open_systems(self) -> None: ...
    def open_readme(self) -> None: ...
    def change_zoom(self, delta: float) -> None: ...
    def open_about(self) -> None: ...


def build_gear_button(parent: QWidget, host: GearHost, extra_actions: Iterable[QAction] = ()) -> QToolButton:
    btn = QToolButton(parent)
    btn.setIcon(gear_icon())
    btn.setIconSize(QSize(20, 20))
    btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
    btn.setToolTip("Data sources, Settings, Systems, Readme, Zoom, About")
    btn.setStyleSheet(theme.GEAR_BUTTON)
    btn.setPopupMode(QToolButton.InstantPopup)
    btn.setCursor(Qt.PointingHandCursor)
    menu = QMenu(btn)
    menu.addAction("Data sources…", host.open_data_sources)
    menu.addAction("Settings…", host.open_settings)
    menu.addAction("Systems…", host.open_systems)
    menu.addAction("Readme", host.open_readme)
    extras = list(extra_actions)
    if extras:
        menu.addSeparator()
        for action in extras:
            menu.addAction(action)
    menu.addSeparator()
    zoom_action, refresh_zoom = _zoom_row_action(menu, host)
    menu.addAction(zoom_action)
    menu.addSeparator()
    menu.addAction("About", host.open_about)
    menu.aboutToShow.connect(refresh_zoom)
    btn.setMenu(menu)
    return btn


def _zoom_row_action(menu: QMenu, host: GearHost):
    """Single menu row: (-) Zoom (+). The buttons keep the menu open so
    several steps can be taken in a row (Chris, 2026-09-05)."""
    row = QWidget(menu)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(12, 4, 12, 4)
    layout.setSpacing(10)

    def circle_button(kind: str, delta: float, tip: str) -> QToolButton:
        b = QToolButton(row)
        b.setIcon(zoom_glyph_icon(kind))
        b.setIconSize(QSize(theme.ZOOM_CIRCLE_SIZE - 10, theme.ZOOM_CIRCLE_SIZE - 10))
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setStyleSheet(theme.ZOOM_CIRCLE_BUTTON)
        b.setFixedSize(theme.ZOOM_CIRCLE_SIZE, theme.ZOOM_CIRCLE_SIZE)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _checked=False, d=delta: (host.change_zoom(d), refresh()))
        return b

    out_btn = circle_button("minus", -theme.ZOOM_STEP, "Zoom out (Ctrl+-)")
    label = QLabel("Zoom", row)
    label.setAlignment(Qt.AlignCenter)
    in_btn = circle_button("plus", theme.ZOOM_STEP, "Zoom in (Ctrl+=)")
    layout.addWidget(out_btn)
    layout.addWidget(label, 1)
    layout.addWidget(in_btn)

    def refresh() -> None:
        pct = f"{theme.zoom_factor() * 100:.0f}%"
        label.setToolTip(f"Current zoom {pct}. Ctrl+0 resets to 100%.")
        out_btn.setEnabled(theme.zoom_factor() > theme.ZOOM_MIN)
        in_btn.setEnabled(theme.zoom_factor() < theme.ZOOM_MAX)

    action = QWidgetAction(menu)
    action.setDefaultWidget(row)
    refresh()
    return action, refresh
