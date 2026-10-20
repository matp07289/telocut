"""デュアルディスプレイ対応: フルスクリーン出力とワークスペース配置。"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPainter, QColor, QImage, QGuiApplication
from PyQt6.QtWidgets import QWidget


def screens_sorted() -> list:
    return sorted(QGuiApplication.screens(), key=lambda s: (s.geometry().x(), s.geometry().y()))


def screen_names() -> List[str]:
    out = []
    for i, s in enumerate(screens_sorted()):
        g = s.geometry()
        out.append(f"画面 {i + 1}: {s.name()}  {g.width()}×{g.height()}")
    return out


class FullscreenOutput(QWidget):
    """クライアントモニター / フルスクリーン再生用の枠なしウィンドウ。Esc・ダブルクリックで閉じる。"""

    def __init__(self, mw):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.mw = mw
        self._img: Optional[QImage] = None
        self.aspect = 16 / 9
        self.setWindowTitle("TeloCut フルスクリーン")
        self.setStyleSheet("background: black;")
        self.setCursor(Qt.CursorShape.BlankCursor)

    def set_image(self, img: QImage):
        self._img = img
        self.update()

    def show_on(self, screen):
        g = screen.geometry()
        self.setGeometry(g)
        self.showFullScreen()
        self.windowHandle().setScreen(screen) if self.windowHandle() else None
        self.setGeometry(g)
        self.raise_()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self._img is not None:
            w, h = self.width(), self.height()
            if w / max(1, h) > self.aspect:
                vh, vw = h, int(h * self.aspect)
            else:
                vw, vh = w, int(w / self.aspect)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(QRect((w - vw) // 2, (h - vh) // 2, vw, vh), self._img)
        p.end()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.mw.toggle_fullscreen(False)
            return
        # 再生系のキーはメインウィンドウへ転送
        self.mw.keyPressEvent(e)

    def mouseDoubleClickEvent(self, _e):
        self.mw.toggle_fullscreen(False)


def ensure_docks_on_screen(mw, docks):
    """切断された画面に残ったフローティングパネルを戻す。"""
    geos = [s.availableGeometry() for s in QGuiApplication.screens()]
    for d in docks:
        if d.isFloating():
            g = d.frameGeometry()
            if not any(sg.intersects(g) for sg in geos):
                d.setFloating(False)
