"""ソース / レコード モニター（Avid Composer ウィンドウ風）。"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QRect, QRectF, pyqtSignal, QPointF
from PyQt6.QtGui import QPainter, QColor, QImage, QPen, QFont, QPolygonF
from PyQt6.QtWidgets import (QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QToolButton, QMenu)

from core.timecode import frames_to_timecode, timecode_to_frames
from .theme import C


class VideoView(QWidget):
    """フレームを縦横比を保って表示。セーフエリア表示可。"""

    clicked = pyqtSignal()
    zoom_rect = pyqtSignal(QRectF)      # Drag & Zoom: 画面上で囲んだ範囲（0〜1 の割合）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.zoom_drag = False
        self._drag0 = None
        self._drag1 = None
        self._img: Optional[QImage] = None
        self.aspect = 16 / 9
        self.safe_area = False
        self.grid = False                 # 三分割のガイド
        self.guide = ""                   # 画面比率・セーフエリアのガイド（title / shorts / 4:3 / 1:1 / 9:16）
        self.message = ""
        self.setMinimumSize(240, 135)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def _draw_guide(self, p: QPainter, r):
        """画面比率の範囲（外は暗く）・タイトルセーフ・ショートの UI（右のボタン・下の説明文）の位置。"""
        g = self.guide
        dim = QColor(0, 0, 0, 110)
        pen = QPen(QColor(255, 220, 80, 200), 1, Qt.PenStyle.DashLine)
        if g in ("4:3", "1:1", "9:16"):
            a, b = {"4:3": (4, 3), "1:1": (1, 1), "9:16": (9, 16)}[g]
            w = min(r.width(), int(r.height() * a / b))
            h = min(r.height(), int(r.width() * b / a))
            x = r.x() + (r.width() - w) // 2
            y = r.y() + (r.height() - h) // 2
            p.fillRect(r.x(), r.y(), x - r.x(), r.height(), dim)
            p.fillRect(x + w, r.y(), r.right() - (x + w) + 1, r.height(), dim)
            p.fillRect(x, r.y(), w, y - r.y(), dim)
            p.fillRect(x, y + h, w, r.bottom() - (y + h) + 1, dim)
            p.setPen(pen)
            p.drawRect(x, y, w, h)
        elif g == "title":
            p.setPen(pen)
            for f in (0.9, 0.8):
                w, h = int(r.width() * f), int(r.height() * f)
                p.drawRect(r.x() + (r.width() - w) // 2, r.y() + (r.height() - h) // 2, w, h)
        elif g == "shorts":
            # 縦動画で、アプリのボタン（右）・チャンネル名と説明（下）・上の検索などに隠れやすい所
            p.fillRect(r.x() + int(r.width() * 0.84), r.y() + int(r.height() * 0.42), int(r.width() * 0.16),
                       int(r.height() * 0.48), dim)
            p.fillRect(r.x(), r.y() + int(r.height() * 0.80), int(r.width() * 0.84), int(r.height() * 0.20), dim)
            p.fillRect(r.x(), r.y(), r.width(), int(r.height() * 0.08), dim)
            p.setPen(QColor(255, 220, 80, 220))
            p.drawText(r.x() + 8, r.y() + int(r.height() * 0.80) + 18, "説明・チャンネル名")
            p.drawText(r.x() + int(r.width() * 0.84) + 4, r.y() + int(r.height() * 0.42) + 18, "ボタン")

    def set_image(self, img: QImage):
        self._img = img
        self.update()

    def clear(self, message: str = ""):
        self._img = None
        self.message = message
        self.update()

    def video_rect(self) -> QRect:
        w, h = self.width(), self.height()
        if w / max(1, h) > self.aspect:
            vh = h
            vw = int(h * self.aspect)
        else:
            vw = w
            vh = int(w / self.aspect)
        return QRect((w - vw) // 2, (h - vh) // 2, vw, vh)

    def mousePressEvent(self, e):
        self.clicked.emit()
        if self.zoom_drag and e.button() == Qt.MouseButton.LeftButton:
            self._drag0 = self._drag1 = e.position()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag0 is not None:
            self._drag1 = e.position()
            self.update()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag0 is not None:
            r = self.video_rect()
            a, b = self._drag0, e.position()
            self._drag0 = self._drag1 = None
            self.update()
            x0, x1 = sorted(((a.x() - r.x()) / max(1, r.width()), (b.x() - r.x()) / max(1, r.width())))
            y0, y1 = sorted(((a.y() - r.y()) / max(1, r.height()), (b.y() - r.y()) / max(1, r.height())))
            if x1 - x0 > 0.02 and y1 - y0 > 0.02:
                self.zoom_rect.emit(QRectF(x0, y0, x1 - x0, y1 - y0))
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0c0c0d"))
        r = self.video_rect()
        if self._img is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self._img)
        else:
            p.fillRect(r, QColor("#000000"))
            if self.message:
                p.setPen(QColor(C["dim"]))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.message)
        if self.grid:
            p.setPen(QPen(QColor(255, 255, 255, 80), 1))
            for k in (1, 2):
                x = r.x() + r.width() * k // 3
                y = r.y() + r.height() * k // 3
                p.drawLine(x, r.y(), x, r.bottom())
                p.drawLine(r.x(), y, r.right(), y)
        if self.guide:
            self._draw_guide(p, r)
        if self.safe_area:
            p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.PenStyle.DashLine))
            for f in (0.9, 0.8):
                w, h = int(r.width() * f), int(r.height() * f)
                p.drawRect(r.x() + (r.width() - w) // 2, r.y() + (r.height() - h) // 2, w, h)
        if self._drag0 is not None and self._drag1 is not None:
            # 枠は画面の縦横比に合わせて表示（実際のズームも縦横比を保つ）
            a, b = self._drag0, self._drag1
            w = abs(b.x() - a.x())
            h = max(abs(b.y() - a.y()), w / self.aspect)
            w = h * self.aspect
            x = min(a.x(), b.x()) if b.x() >= a.x() else a.x() - w
            y = min(a.y(), b.y()) if b.y() >= a.y() else a.y() - h
            p.setPen(QPen(QColor("#ffcc33"), 2))
            p.drawRect(QRectF(x, y, w, h))
            p.setPen(QPen(QColor(255, 204, 51, 120), 1, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(a.x(), a.y(), b.x() - a.x(), b.y() - a.y()))
        p.end()


class PositionBar(QWidget):
    """再生位置バー（IN/OUT とマーカーを表示、クリック/ドラッグでスクラブ）。"""

    scrub = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self.duration = 1
        self.position = 0
        self.mark_in = -1
        self.mark_out = -1
        self.markers = []
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _x(self, f):
        f = max(0, min(f, self.duration))
        return 6 + (self.width() - 12) * f / max(1, self.duration)

    def _f(self, x):
        return int(round((x - 6) / max(1, self.width() - 12) * self.duration))

    def mousePressEvent(self, e):
        self.scrub.emit(max(0, min(self.duration, self._f(e.position().x()))))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.scrub.emit(max(0, min(self.duration, self._f(e.position().x()))))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C["panel2"]))
        y = self.height() // 2
        p.fillRect(6, y - 2, self.width() - 12, 4, QColor("#141416"))
        if self.mark_in >= 0 or self.mark_out >= 0:
            a = self._x(max(0, self.mark_in))
            b = self._x(self.mark_out if self.mark_out >= 0 else self.duration)
            p.fillRect(QRectF(a, y - 3, max(1, b - a), 6), QColor(C["accent"]))
        for m in self.markers:
            p.fillRect(QRectF(self._x(m) - 1, 2, 3, 5), QColor(C["marker"]))
        x = self._x(self.position)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(C["playhead"]))
        p.drawPolygon(QPolygonF([QPointF(x - 5, 1), QPointF(x + 5, 1), QPointF(x, 9)]))
        p.fillRect(QRectF(x - 1, 8, 2, self.height() - 10), QColor(C["playhead"]))
        p.end()


class MonitorPanel(QFrame):
    """モニター1面: 情報バー / 映像 / 位置バー / ボタン列。"""

    activated = pyqtSignal()
    action = pyqtSignal(str)          # ボタン操作をメインウィンドウへ

    def __init__(self, title: str, kind: str, player, parent=None):
        super().__init__(parent)
        self.kind = kind              # source / record
        self.player = player
        self.fps = 29.97
        self.mark_in = -1
        self.mark_out = -1
        self.tc_offset = 0            # 表示用のタイムコード開始オフセット（フレーム）
        self.setObjectName("monitorFrame")
        self.setProperty("active", "false")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)

        # 情報バー
        top = QHBoxLayout()
        self.title = QLabel(title)
        self.title.setStyleSheet("font-weight: bold; background: transparent;")
        self.name = QLabel("")
        self.name.setObjectName("info")
        # Avid のトラッキング表示（クリックで表示内容を選択）
        self.track_modes = ["io", "mas"]
        self.dur_label = QLabel("")
        self.dur_label.setObjectName("info")
        self.dur_label.setToolTip("クリックで表示内容を変更")
        self.dur_label.mousePressEvent = lambda e: self._mode_menu(0, e)
        self.tc = QLabel("00:00:00:00")
        self.tc.setObjectName("tc")
        self.tc.setToolTip("クリックで表示内容を変更 / タイムコードへ移動")
        self.tc.mousePressEvent = lambda e: self._mode_menu(1, e)
        self.name.setToolTip("クリックで切替" if kind == "record" else "")
        self.name.mousePressEvent = lambda e: self.action.emit("name_menu")
        self.fps_label = QLabel("")
        self.fps_label.setObjectName("info")
        top.addWidget(self.title)
        top.addWidget(self.name, 1)
        top.addWidget(self.fps_label)
        top.addWidget(self.dur_label)
        top.addWidget(self.tc)
        lay.addLayout(top)

        self.view = VideoView()
        self.view.clicked.connect(self.activated.emit)
        lay.addWidget(self.view, 1)

        self.bar = PositionBar()
        self.bar.scrub.connect(self._on_scrub)
        lay.addWidget(self.bar)

        # ボタン列（Avid 風）
        btns = QHBoxLayout()
        btns.setSpacing(2)

        def b(text, act, tip, obj=None, w=30):
            x = QPushButton(text)
            # 狭いときは縮む（コンポーザーの最小幅が大きくなりすぎないように）
            x.setMaximumWidth(w)
            x.setMinimumWidth(min(w, 20))
            x.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            x.setToolTip(tip)
            x.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if obj:
                x.setObjectName(obj)
            x.clicked.connect(lambda: (self.activated.emit(), self.action.emit(act)))
            btns.addWidget(x)
            return x

        b("{", "mark_in", "IN 点を設定 (I)")
        b("}", "mark_out", "OUT 点を設定 (O)")
        b("✕", "clear_marks", "IN/OUT を消去 (Alt+X)")
        btns.addSpacing(6)
        b("|◀", "prev_edit", "前の編集点へ (↑)")
        b("◀", "step_back", "1フレーム戻る (←)")
        self.play_btn = b("▶", "play", "再生 / 停止 (Space)", w=40)
        b("▶|", "step_fwd", "1フレーム進む (→)")
        b("▶▶|", "next_edit", "次の編集点へ (↓)", w=36)
        btns.addSpacing(6)
        b("⇥I", "goto_in", "IN 点へ移動 (Shift+I)", w=32)
        b("O⇤", "goto_out", "OUT 点へ移動 (Shift+O)", w=32)
        b("⟲", "loop", "ループ再生の切替", w=30)
        btns.addStretch(1)
        if kind == "source":
            b("⤵ 挿入", "insert", "スプライスイン（挿入）: 後ろを押し出して配置 (,)", "splice", 64)
            b("⤓ 上書き", "overwrite", "上書き: 既存クリップを置き換えて配置 (.)", "overwrite", 64)
        else:
            b("リフト", "lift", "リフト: IN-OUT を削除して隙間を残す (;)", "overwrite", 56)
            b("抽出", "extract", "抽出: IN-OUT を削除して詰める (')", "splice", 56)
            b("＋T", "add_telop", "再生ヘッドにテロップを追加 (T)", w=40)
        lay.addLayout(btns)

        player.position_changed.connect(self._on_position)
        player.image_changed.connect(self.view.set_image)
        player.state_changed.connect(lambda on: self.play_btn.setText("■" if on else "▶"))
        player.fps_changed.connect(self._on_fps)

    def _on_fps(self, fps: float, target: float):
        if fps < 0:
            self.fps_label.setText("")
            return
        ok = fps >= target * 0.92
        self.fps_label.setText(f"{'●' if ok else '▲ コマ落ち'} {fps:.1f}fps")
        self.fps_label.setStyleSheet(f"color: {'#5fbf6f' if ok else C['overwrite']}; background: transparent;")

    def set_overlay_title(self, text: str):
        """トリムモードの「A 側 / B 側」表示など。"""
        self.title.setText(text)

    def set_active(self, on: bool):
        self.setProperty("active", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_info(self, name: str, fps: float, duration: int, aspect: float):
        self.name.setText(name)
        self.fps = fps
        self.view.aspect = aspect
        self.bar.duration = max(1, duration)
        self.update_marks()

    def set_marks(self, a: int, b: int, markers=()):
        self.mark_in, self.mark_out = a, b
        self.bar.markers = list(markers)
        self.update_marks()

    MODES = [("mas", "マスター TC"), ("dur", "長さ"), ("io", "IN-OUT の長さ"),
             ("rem", "残り（OUT / 末尾まで）"), ("abs", "フレーム番号"), ("none", "表示しない")]

    def _mode_menu(self, slot: int, e):
        m = QMenu(self)
        for key, label in self.MODES:
            a = m.addAction(label, lambda k=key: self._set_mode(slot, k))
            a.setCheckable(True)
            a.setChecked(self.track_modes[slot] == key)
        m.addSeparator()
        m.addAction("タイムコードへ移動...", lambda: self.action.emit("goto_tc"))
        m.exec(e.globalPosition().toPoint())

    def _set_mode(self, slot, key):
        self.track_modes[slot] = key
        self._update_displays()

    def _display_text(self, mode: str) -> str:
        f = self.player.position
        dur = max(0, self.player.end_frame)
        fps = self.fps
        if mode == "mas":
            return frames_to_timecode(f + self.tc_offset, fps)
        if mode == "dur":
            return "長さ " + frames_to_timecode(dur, fps)
        if mode == "io":
            if self.mark_in >= 0 and self.mark_out > self.mark_in:
                return "I/O " + frames_to_timecode(self.mark_out - self.mark_in, fps)
            return "I/O --:--:--:--"
        if mode == "rem":
            end = self.mark_out if self.mark_out > f else dur
            return "残 " + frames_to_timecode(max(0, end - f), fps)
        if mode == "abs":
            return f"F {f}"
        return ""

    def _update_displays(self):
        self.dur_label.setText(self._display_text(self.track_modes[0]))
        self.tc.setText(self._display_text(self.track_modes[1]) or " ")

    def update_marks(self):
        self.bar.mark_in, self.bar.mark_out = self.mark_in, self.mark_out
        self._update_displays()
        self.bar.update()

    def _on_scrub(self, f: int):
        self.activated.emit()
        self.player.seek(f)

    def _on_position(self, f: int):
        self._update_displays()
        self.bar.position = f
        self.bar.duration = max(1, self.player.end_frame, f)
        self.bar.update()

    def mousePressEvent(self, e):
        self.activated.emit()
        super().mousePressEvent(e)
