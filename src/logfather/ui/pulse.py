"""A gentle attention pulse for one button at a time (Chris, 2026-09-07:
the on/off blink read as an alarm). The background breathes from the
raised ground to the accent-dim fill and back over about three seconds
with an ease-in-out curve; nothing flashes."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QVariantAnimation
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QAbstractButton

from logfather.ui import theme


def _blend(a: str, b: str, t: float) -> str:
    ca, cb = QColor(a), QColor(b)
    return QColor(
        round(ca.red() + (cb.red() - ca.red()) * t),
        round(ca.green() + (cb.green() - ca.green()) * t),
        round(ca.blue() + (cb.blue() - ca.blue()) * t),
    ).name()


class Pulser(QObject):
    def __init__(self, parent=None, period_ms: int = 3000):
        super().__init__(parent)
        self._target: QAbstractButton | None = None
        self._base_style = ""
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(period_ms)
        self._anim.setStartValue(0.0)
        self._anim.setKeyValueAt(0.5, 1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._step)

    def target(self) -> QAbstractButton | None:
        return self._target

    def set_target(self, btn: QAbstractButton | None) -> None:
        if btn is self._target:
            return
        self.stop()
        self._target = btn
        if btn is not None:
            self._base_style = btn.styleSheet()
            self._anim.start()

    def stop(self) -> None:
        self._anim.stop()
        if self._target is not None:
            self._target.setStyleSheet(self._base_style)
        self._target = None

    def _step(self, value) -> None:
        btn = self._target
        if btn is None:
            return
        t = float(value)
        kind = btn.metaObject().className()
        fill = _blend(theme.BG_RAISED, theme.ACCENT_DIM, t)
        border = _blend(theme.BORDER_LIGHT, theme.ACCENT, t)
        btn.setStyleSheet(
            self._base_style
            + f" {kind} {{ background-color: {fill}; border: 1px solid {border}; color: {theme.TEXT_BRIGHT}; }}"
        )
