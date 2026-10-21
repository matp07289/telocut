"""ペン（板タブ・液タブ）で操作するとき。

- ペンを検出したら自動で「ペンモード」（設定: 自動 / いつも / 使わない）
- ペンは押した瞬間に少しぶれるので、ドラッグと判定する距離・ダブルクリックの間隔を広げる
- タイムラインのクリップの端（トリム）をつかみやすく（判定の幅を広げる）
- ホイールやキーボードが無くても使えるように、大きなボタンのペン用ツールバー（再生・JKL・コマ送り・分割・
  リップル削除・マーカー・ズーム・全体表示・元に戻す）
- サイドボタン（右クリック）・長押し（Windows Ink の右クリック）はそのまま使える
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QEvent, QObject, QSize, Qt
from PyQt6.QtWidgets import QApplication, QToolBar

from .settings import SETTINGS

PEN_DRAG = 14             # ドラッグと判定する距離（px）
PEN_DOUBLE_MS = 650       # ダブルクリックの間隔
PEN_EDGE = 12             # タイムラインの端（トリム）の判定幅

TOOLS = [("undo", "↶", "元に戻す"), ("redo", "↷", "やり直し"), None,
         ("shuttle_j", "◀◀", "逆再生（J）"), ("step_back_1", "◀1", "1 コマ戻る"), ("play_stop", "▶ / ■", "再生 / 停止"),
         ("step_fwd_1", "1▶", "1 コマ進む"), ("shuttle_l", "▶▶", "再生（L・倍速）"), None,
         ("prev_edit", "|◀", "前の編集点"), ("next_edit", "▶|", "次の編集点"), None,
         ("add_edit", "✂", "再生ヘッドで分割"), ("ripple_delete", "⌫", "リップル削除"), ("lift", "リフト", "リフト"),
         ("add_marker", "◆", "マーカー"), None,
         ("zoom_out", "－", "タイムライン縮小"), ("zoom_in", "＋", "タイムライン拡大"), ("zoom_fit", "全体", "全体を表示")]


class PenFilter(QObject):
    """アプリ全体でタブレット（ペン）の入力を見て、ペンを使い始めたら知らせる。"""

    def __init__(self, on_pen):
        super().__init__()
        self.on_pen = on_pen
        self.last = 0.0
        self.seen = False
        self.pressure = 0.0

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.Type.TabletPress, QEvent.Type.TabletMove, QEvent.Type.TabletEnterProximity):
            self.last = time.time()
            try:
                self.pressure = float(ev.pressure())
            except Exception:
                pass
            if not self.seen:
                self.seen = True
                self.on_pen()
        return False          # 横取りしない（Qt がマウスの操作に変えてくれる）


class PenState:
    def __init__(self):
        self.on = False
        self.saved = None

    def apply(self, on: bool):
        """ペン向けの調整をする / 元に戻す。"""
        app = QApplication.instance()
        from . import timeline as TL
        if on and not self.on:
            self.saved = (app.startDragDistance(), app.doubleClickInterval(), TL.EDGE)
            app.setStartDragDistance(PEN_DRAG)
            app.setDoubleClickInterval(PEN_DOUBLE_MS)
            TL.EDGE = PEN_EDGE
        elif not on and self.on and self.saved:
            app.setStartDragDistance(self.saved[0])
            app.setDoubleClickInterval(self.saved[1])
            TL.EDGE = self.saved[2]
        self.on = on


def make_toolbar(mw) -> QToolBar:
    tb = QToolBar("ペン用ツールバー", mw)
    tb.setObjectName("pen_toolbar")
    tb.setIconSize(QSize(32, 32))
    tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
    tb.setStyleSheet("QToolButton { min-width: 44px; min-height: 40px; font-size: 15px; padding: 2px 6px; }")
    for t in TOOLS:
        if t is None:
            tb.addSeparator()
            continue
        cid, text, tip = t
        a = tb.addAction(text)
        a.setToolTip(tip)
        a.triggered.connect(lambda _=False, c=cid: mw.run_command(c))
    return tb


def mode() -> str:
    return getattr(SETTINGS, "pen_mode", "auto")
