"""Application entry point: splash screen + main().

Extracted from Main_Window so the hub module is importable without the
bootstrap path. Run the app either way:

    .venv/Scripts/python.exe src/Main_Window.py
    .venv/Scripts/python.exe src/app_main.py
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget

from app_assets import resolve_asset_path as _resolve_asset_path
from app_version import format_version_label

SPLASH_IMAGE_FILENAME = "Logfather Argus II.jpg"


class FadeSplashScreen(QSplashScreen):
    def __init__(self, pixmap: QPixmap, flags=Qt.WindowType.Widget):
        super().__init__(pixmap, flags)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.valueChanged.connect(self._on_fade_value_changed)
        self._fade_anim.finished.connect(self._on_fade_finished)

    def fade_and_finish(self, widget: QWidget):
        try:
            widget.raise_()
            widget.activateWindow()
        except Exception:
            pass
        self.setWindowOpacity(1.0)
        self._fade_anim.start()

    def _on_fade_value_changed(self, value):
        try:
            self.setWindowOpacity(float(value))
        except Exception:
            pass

    def _on_fade_finished(self):
        self.close()



def main():
    app = QApplication(sys.argv)
    icon_path = _resolve_asset_path("logfather.ico")
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    splash = _build_splash_image()
    if splash is not None:
        splash.show()
        app.processEvents()
    from Main_Window import MainWindow

    win = MainWindow()
    win.resize(1400, 700)
    if splash is not None:
        win.show()
        splash.fade_and_finish(win)
    else:
        win.show()
    sys.exit(app.exec())

def _build_splash_image() -> QSplashScreen | None:
    try:
        image_path = _resolve_asset_path(SPLASH_IMAGE_FILENAME)
        if not image_path:
            return None
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            return None
        scaled = pixmap.scaled(
            max(1, int(pixmap.width() * 0.33)),
            max(1, int(pixmap.height() * 0.33)),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        framed = QPixmap(scaled.width() + 4, scaled.height() + 4)
        framed.fill(QColor("#10151a"))
        painter = QPainter(framed)
        painter.drawPixmap(2, 2, scaled)
        pen = QPen(QColor("#4a5560"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(1, 1, framed.width() - 3, framed.height() - 3)
        painter.end()
        splash = FadeSplashScreen(framed, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        splash.setFont(QFont("", 10))
        splash.showMessage(
            f"Loading...  {format_version_label()}",
            Qt.AlignBottom | Qt.AlignHCenter,
            QColor(255, 255, 255, 210),
        )
        return splash
    except Exception:
        return None





if __name__ == "__main__":
    main()
