"""One family of Grafana readings drawn as a strip under every system row
of the Overview: temperatures, currents (Chris, 2026-09-07/08). Each
channel owns its Data-box button and colour key, its fetch, its strip
height (dragged at the strip's bottom line) and the hover dots and box.

The owner is the OverviewWidget: it supplies the scene, the view, the
settings, the chosen span, and calls back for redraws.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QFontMetrics, QIcon, QPainterPath, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem
from PySide6.QtWidgets import QLabel, QMenu, QSizePolicy, QToolButton

from logfather.core.telemetry import window_stats
from logfather.data.telemetry_loader import fetch_fleet_signals
from logfather.data.ui_state_store import load_ui_state, update_ui_state
from logfather.ui import theme
from logfather.ui.qt_worker import JobSlot

STRIP_MIN, STRIP_MAX = 16, 240
GUIDE_COLOUR = "#ff8a65"


class SignalChannel(QObject):
    def __init__(
        self,
        owner,
        *,
        name: str,
        title: str,
        icon: QIcon,
        tooltip: str,
        choices: tuple[tuple[str, str], ...],
        colours: dict[str, str],
        unit: str,
        axis_unit: str,
        decimals: int,
        separator_before: str,
        ui_keys: str,
        ui_strip: str,
        default_strip_h: int,
        short: dict[str, str],
        loading_text: str,
        empty_text: str,
        axis_min: float | None = None,
        axis_title: str = "",
    ):
        super().__init__(owner)
        self.owner = owner
        self.name = name
        self.title = title
        self.choices = choices
        self.colours = colours
        self.unit = unit
        self.axis_unit = axis_unit
        self.decimals = decimals
        self.ui_keys = ui_keys
        self.ui_strip = ui_strip
        self.short = short
        self.loading_text = loading_text
        self.empty_text = empty_text
        # A fixed floor for the strip's scale (pressure reads from 0 bar,
        # Chris, 2026-09-08); None scales to the readings in the window.
        self.axis_min = axis_min
        # Written at the left of every strip (Chris, 2026-09-08).
        self.axis_title = axis_title or title
        state = load_ui_state()
        valid = [k for k, _label in choices]
        stored = state.get(ui_keys)
        self.keys: list[str] = [k for k in (stored if isinstance(stored, list) else []) if k in valid]
        try:
            self.strip_h = int(min(STRIP_MAX, max(STRIP_MIN, int(state.get(ui_strip)))))
        except (TypeError, ValueError):
            self.strip_h = default_strip_h
        self.data: dict[str, dict[str, object]] = {}
        self.window: tuple[datetime, datetime] | None = None
        self.keys_loaded: tuple[str, ...] = ()
        self.fetched_local: datetime | None = None
        self.slot = JobSlot(owner)
        self.edge_bands: list[tuple[float, float, float]] = []
        self.resize: tuple | None = None
        self.guide_item = None
        self.guide_label = None
        self.edge_hover = False
        self.hover_rows: list[tuple] = []
        self.hover_items: list = []
        # (robot, key) -> QPainterPath in (seconds since the epoch, value)
        # space, built once per data load; each redraw only places it
        # through a transform (the per-second live redraw was rebuilding
        # every segment in Python and froze the window, 2026-09-08).
        self._paths: dict[tuple[str, str], QPainterPath] = {}

        self.button = QToolButton()
        self.button.setIcon(icon)
        self.button.setIconSize(QSize(18, 18))
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.button.setToolTip(tooltip)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        menu = QMenu(self.button)
        self.actions: dict[str, QAction] = {}
        for key, label in choices:
            if key == separator_before:
                menu.addSeparator()
            action = QAction(label, owner)
            action.setCheckable(True)
            action.setChecked(key in self.keys)
            action.toggled.connect(lambda _checked=False: self.on_changed())
            menu.addAction(action)
            self.actions[key] = action
        menu.addSeparator()
        menu.addAction("Show all", lambda: self.set_all(True))
        menu.addAction("Hide all", lambda: self.set_all(False))
        self.button.setMenu(menu)
        self.key_label = QLabel("")
        self.key_label.setTextFormat(Qt.RichText)
        self.key_label.setStyleSheet(theme.MUTED_LABEL)
        self.refresh_label()

    # ---- selection ----------------------------------------------------------
    @property
    def active(self) -> bool:
        return bool(self.keys)

    def refresh_label(self) -> None:
        n = len(self.keys)
        self.button.setText(self.title if not n else f"{self.title} ({n})")
        bits = [
            f'<span style="background-color:{self.colours[key]};">&nbsp;&nbsp;&nbsp;</span>&nbsp;{label}'
            for key, label in self.choices if key in self.keys
        ]
        self.key_label.setText("&nbsp;&nbsp;".join(bits))
        self.key_label.setVisible(bool(bits))

    def set_all(self, on: bool) -> None:
        for action in self.actions.values():
            action.blockSignals(True)
            action.setChecked(on)
            action.blockSignals(False)
        self.on_changed()

    def on_changed(self) -> None:
        self.keys = [k for k, action in self.actions.items() if action.isChecked()]
        update_ui_state({self.ui_keys: list(self.keys)})
        self.refresh_label()
        self.owner._maybe_fetch_signals()
        self.owner._schedule_redraw()

    # ---- data ---------------------------------------------------------------
    def maybe_fetch(self, span: tuple[datetime, datetime], live: bool, fresh_for, now_local: datetime, force: bool = False) -> None:
        if not self.keys or self.slot.is_running():
            return
        wanted = tuple(k for k in self.keys if k not in self.keys_loaded)
        same_start = self.window is not None and self.window[0] == span[0]
        same_end = self.window is not None and self.window[1] == span[1]
        fresh = self.fetched_local is not None and (now_local - self.fetched_local) < fresh_for
        if not force and not wanted and same_start and (fresh if live else same_end):
            return
        keys = list(self.keys)
        settings = self.owner.settings
        self.slot.start(
            lambda job: fetch_fleet_signals(settings, keys, span[0], span[1], job),
            on_result=lambda result, keys=tuple(keys), span=span: self._on_loaded(result, keys, span, now_local),
            on_error=lambda message: self.owner.status_label.setText(f"{self.title}: {message}"),
        )

    def _on_loaded(self, result, keys, span, now_local) -> None:
        self.data = result or {}
        self._paths = {}
        self.keys_loaded = keys
        self.window = span
        self.fetched_local = now_local
        self.owner._schedule_redraw()

    # ---- drawing ------------------------------------------------------------
    def reset_for_redraw(self) -> None:
        self.edge_bands = []
        self.hover_rows = []
        self.hover_items = []
        self.guide_item = None
        self.guide_label = None

    def strip_height(self, zoom: float) -> int:
        return int(self.strip_h * zoom) if self.keys else 0

    def _fmt(self, value: float) -> str:
        return f"{value:.{self.decimals}f}{self.unit}"

    def draw_strip(self, state, rect: QRectF, window_start: datetime, window_end: datetime, scene_width: float, right_pad: float) -> None:
        scene = self.owner.scene
        bg = scene.addRect(rect, QPen(QColor("#31414d")), QBrush(QColor("#0b1014")))
        bg.setZValue(1)
        self.edge_bands.append((rect.bottom(), rect.left(), rect.right()))
        title_font = QFont()
        title_font.setPointSize(8)
        title_item = scene.addText(self.axis_title, title_font)
        title_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
        title_item.setPos(22, rect.top() + max(0.0, (rect.height() - title_item.boundingRect().height()) / 2))
        title_item.setZValue(4)
        tracks = self.data.get(state.robot_id or "", {})
        w0 = int(window_start.timestamp() * 1000)
        w1 = int(window_end.timestamp() * 1000)
        chosen = []
        for key, label in self.choices:
            track = tracks.get(key) if key in self.keys else None
            if track is None:
                continue
            stats = window_stats(track, w0, w1)
            if stats is not None:
                chosen.append((key, label, track, stats))
        if not chosen:
            waiting = self.slot.is_running() or not self.data
            bg.setToolTip(self.loading_text if waiting else self.empty_text)
            return
        lo = min(s[3][0] for s in chosen)
        hi = max(s[3][1] for s in chosen)
        if self.axis_min is not None:
            lo = min(self.axis_min, lo)
        if hi - lo < 1e-6:
            lo, hi = lo - 0.5, hi + 0.5
        inner_top = rect.top() + 2
        inner_h = rect.height() - 4
        span_s = max(1.0, (w1 - w0) / 1000.0)
        # Children of the background clip to it, so a day-long path shows
        # only the visible window.
        bg.setFlag(QGraphicsItem.ItemClipsChildrenToShape, True)
        sx = rect.width() / span_s
        sy = -inner_h / (hi - lo)
        transform = QTransform(sx, 0.0, 0.0, sy, rect.left() - (w0 / 1000.0) * sx, inner_top + inner_h - lo * sy)
        for key, _label, track, _stats in chosen:
            path = self._path_for(state.robot_id or "", key, track)
            pen = QPen(QColor(self.colours.get(key, "#ffffff")))
            pen.setWidthF(1.3)
            pen.setCosmetic(True)
            item = scene.addPath(path, pen)
            item.setParentItem(bg)
            item.setTransform(transform)
            item.setZValue(3.5)
            item.setAcceptedMouseButtons(Qt.NoButton)
        small = QFont()
        small.setPointSize(7)
        axis_fmt = "{:.0f}" if self.decimals <= 1 else "{:.1f}"
        # (temperatures whole degrees; amps and bar to one decimal)
        for text, y_pos in ((axis_fmt.format(hi) + self.axis_unit, rect.top() - 3), (axis_fmt.format(lo) + self.axis_unit, rect.bottom() - 13)):
            label_item = scene.addText(text, small)
            label_item.setDefaultTextColor(QColor(theme.TEXT_FAINT))
            label_item.setPos(rect.left() - 34, y_pos)
            label_item.setZValue(4)
        latest = " · ".join(
            f"{self.short.get(label, label.replace('Motor ', 'M'))} {axis_fmt.format(stats[2])}{self.axis_unit}"
            for _k, label, _t, stats in chosen
        )
        latest = self.owner._fit_text(latest, right_pad - 20, QFontMetrics(small)) or latest
        latest_item = scene.addText(latest, small)
        latest_item.setDefaultTextColor(QColor(theme.TEXT_MUTED))
        latest_item.setPos(scene_width - right_pad + 8, rect.top() - 4)
        latest_item.setZValue(4)
        self.hover_rows.append((rect, lo, hi, inner_top, inner_h, [(k, label, t) for k, label, t, _s in chosen], state.name))

    def _path_for(self, robot: str, key: str, track) -> QPainterPath:
        cached = self._paths.get((robot, key))
        if cached is not None:
            return cached
        path = QPainterPath()
        pen_down = False
        last_t = None
        for t, v in zip(track.times_ms, track.values):
            if v is None:
                pen_down = False
                continue
            if pen_down and last_t is not None and t - last_t > 5 * 60_000:
                pen_down = False  # a gap over five minutes breaks the line
            x = t / 1000.0
            if pen_down:
                path.lineTo(x, v)
            else:
                path.moveTo(x, v)
                pen_down = True
            last_t = t
        self._paths[(robot, key)] = path
        return path

    # ---- stretching by dragging the bottom line ----------------------------
    def edge_at(self, scene_pos) -> bool:
        x, y = float(scene_pos.x()), float(scene_pos.y())
        return any(abs(y - edge) <= 5 and x0 <= x <= x1 for edge, x0, x1 in self.edge_bands)

    def handle_resize(self, event) -> bool:
        """While dragging only a guide line moves; the rows are rebuilt once
        on release (rebuilding on every mouse move blanked the screen)."""
        owner = self.owner
        view, scene = owner.view, owner.scene
        event_type = event.type()
        if self.resize is not None:
            if event_type == QEvent.MouseMove:
                press_y, start_h, edge_y, x0, x1 = self.resize
                zoom = max(0.1, theme.zoom_factor())
                delta = (float(view.mapToScene(event.pos()).y()) - press_y) / zoom
                new_h = int(min(STRIP_MAX, max(STRIP_MIN, start_h + delta)))
                self.strip_h = new_h
                guide_y = edge_y + (new_h - start_h) * zoom
                if self.guide_item is None:
                    pen = QPen(QColor(GUIDE_COLOUR))
                    pen.setStyle(Qt.DashLine)
                    pen.setWidthF(1.5)
                    pen.setCosmetic(True)
                    self.guide_item = scene.addLine(x0, guide_y, x1, guide_y, pen)
                    self.guide_item.setZValue(20)
                    self.guide_label = scene.addText("")
                    self.guide_label.setDefaultTextColor(QColor(GUIDE_COLOUR))
                    self.guide_label.setZValue(20)
                self.guide_item.setLine(x0, guide_y, x1, guide_y)
                self.guide_label.setPlainText(f"{new_h} px")
                self.guide_label.setPos(x1 - 48, guide_y - 18)
                return True
            if event_type == QEvent.MouseButtonRelease:
                self.resize = None
                for item in (self.guide_item, self.guide_label):
                    if item is not None:
                        try:
                            scene.removeItem(item)
                        except RuntimeError:
                            pass
                self.guide_item = None
                self.guide_label = None
                view.viewport().unsetCursor()
                self.edge_hover = False
                update_ui_state({self.ui_strip: int(self.strip_h)})
                owner._redraw()
                return True
            return True
        if not self.edge_bands or owner._drag_candidate is not None:
            return False
        if event_type == QEvent.MouseMove:
            over = self.edge_at(view.mapToScene(event.pos()))
            if over != self.edge_hover:
                self.edge_hover = over
                if over:
                    view.viewport().setCursor(Qt.SizeVerCursor)
                else:
                    view.viewport().unsetCursor()
            return False
        if event_type == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            scene_pos = view.mapToScene(event.pos())
            if self.edge_at(scene_pos):
                y = float(scene_pos.y())
                edge_y, x0, x1 = min(self.edge_bands, key=lambda band: abs(band[0] - y))
                self.resize = (y, int(self.strip_h), edge_y, x0, x1)
                owner.hide_thumbnail_preview()
                return True
        return False

    # ---- hover: a dot per trace and a box of the readings ------------------
    def strip_under(self, y: float | None):
        if y is None:
            return None
        for row in self.hover_rows:
            if row[0].top() <= y <= row[0].bottom():
                return row
        return None

    def clear_hover(self) -> None:
        scene = self.owner.scene
        for item in self.hover_items:
            try:
                if item.scene() is not None:
                    scene.removeItem(item)
            except RuntimeError:
                pass
        self.hover_items = []

    def draw_hover(self, hover_x: float, hover_dt: datetime, hover_y: float | None, timeline_x: float, timeline_width: float, grid_top: float) -> bool:
        strip = self.strip_under(hover_y)
        if strip is None:
            return False
        scene = self.owner.scene
        rect, lo, hi, inner_top, inner_h, tracks, system_name = strip
        t_ms = int(hover_dt.timestamp() * 1000)
        lines = []
        for key, label, track in tracks:
            value = track.value_at(t_ms)
            if value is None:
                continue
            y_dot = inner_top + inner_h - (value - lo) / (hi - lo) * inner_h
            colour = self.colours.get(key, "#ffffff")
            dot = scene.addEllipse(hover_x - 3.5, y_dot - 3.5, 7, 7, QPen(QColor("#0b1014"), 1), QBrush(QColor(colour)))
            dot.setZValue(5)
            dot.setAcceptedMouseButtons(Qt.NoButton)
            self.hover_items.append(dot)
            lines.append(f'<span style="color:{colour};">{label} {self._fmt(value)}</span>')
        if not lines:
            return True
        # System and time on top, a rule, then the readings, in smaller type.
        when = hover_dt.astimezone().strftime("%H:%M:%S")
        head = f'<span style="color:{theme.TEXT_BRIGHT}; font-weight:bold;">{system_name}</span> <span style="color:{theme.TEXT_MUTED};">{when}</span>'
        text = scene.addText("")
        small = QFont(text.font())
        small.setPointSizeF(max(6.0, small.pointSizeF() * 0.7))
        text.setFont(small)
        text.setHtml("<div style='white-space:nowrap;'>" + head + f"<hr style='color:{theme.BORDER_LIGHT};'>" + "<br>".join(lines) + "</div>")
        text.setZValue(6.2)
        text.setAcceptedMouseButtons(Qt.NoButton)
        # The document needs its ideal width or the rule collapses to a dot.
        text.setTextWidth(text.document().idealWidth())
        bounds = text.boundingRect()
        box_w, box_h = bounds.width() + 8, bounds.height() + 6
        box_x = hover_x + 12
        if box_x + box_w > timeline_x + timeline_width:
            box_x = hover_x - 12 - box_w
        box_y = rect.top() - box_h - 4 if rect.top() - box_h - 4 >= grid_top else rect.bottom() + 4
        box = scene.addRect(QRectF(box_x, box_y, box_w, box_h), QPen(QColor(theme.BORDER_LIGHT)), QBrush(QColor(theme.BG_RAISED)))
        box.setZValue(6.1)
        box.setAcceptedMouseButtons(Qt.NoButton)
        text.setPos(box_x + 4, box_y + 3)
        self.hover_items.extend([box, text])
        return True
