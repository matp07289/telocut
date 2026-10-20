"""追加パネル: マーカー / テロップ一覧 / 履歴 / ビデオスコープ / オーディオミキサー / 大型タイムコード。"""
from __future__ import annotations

import time
from typing import List, Optional

import numpy as np
from PyQt6.QtCore import Qt, QTimer, QRectF, pyqtSignal
from PyQt6.QtGui import QPainter, QColor, QImage, QFont, QPen, QLinearGradient
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                             QPushButton, QLabel, QListWidget, QListWidgetItem, QComboBox,
                             QSlider, QToolButton, QHeaderView, QAbstractItemView, QColorDialog,
                             QMenu, QSizePolicy, QScrollArea, QFrame)

from core.timecode import frames_to_timecode, normalize_timecode, timecode_to_frames
from . import model as M
from .theme import C

MARKER_COLORS = [("黄", "#E0B000"), ("赤", "#D8463C"), ("緑", "#3FAE5A"), ("青", "#4F8FE0"),
                 ("紫", "#9A6AE0"), ("白", "#E8E8E8")]


# ─────────────────────────────────────────────
#  マーカー
# ─────────────────────────────────────────────

class MarkerPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._loading = False
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        for text, fn, tip in (("＋ 追加", mw.add_marker, "再生ヘッドにマーカーを追加"),
                              ("削除", self._delete, "選択したマーカーを削除"),
                              ("◀", lambda: self._jump(-1), "前のマーカーへ"),
                              ("▶", lambda: self._jump(1), "次のマーカーへ")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["タイムコード", "色", "名前", "コメント"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 96)
        self.table.setColumnWidth(1, 36)
        self.table.setColumnWidth(2, 120)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._double)
        self.table.itemChanged.connect(self._edited)
        v.addWidget(self.table, 1)
        hint = QLabel("ダブルクリック: 移動（色の列はクリックで色変更）/ 名前・コメントは直接編集")
        hint.setObjectName("info")
        hint.setWordWrap(True)
        v.addWidget(hint)

    def refresh(self):
        seq = self.mw.project.sequence
        off = self.mw.tc_offset(seq)
        self._loading = True
        self.table.setRowCount(len(seq.markers))
        for r, mk in enumerate(seq.markers):
            it = QTableWidgetItem(frames_to_timecode(mk.frame + off, seq.fps))
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            it.setData(Qt.ItemDataRole.UserRole, r)
            self.table.setItem(r, 0, it)
            ci = QTableWidgetItem("●")
            ci.setForeground(QColor(mk.color))
            ci.setFlags(ci.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 1, ci)
            self.table.setItem(r, 2, QTableWidgetItem(mk.name))
            self.table.setItem(r, 3, QTableWidgetItem(getattr(mk, "comment", "")))
        self._loading = False

    def _double(self, r, c):
        seq = self.mw.project.sequence
        if r >= len(seq.markers):
            return
        if c == 1:
            m = QMenu(self)
            for name, col in MARKER_COLORS:
                m.addAction(f"● {name}", lambda col=col, r=r: self._set_color(r, col))
            m.exec(self.cursor().pos())
            return
        if c == 0:
            self.mw.seek_record(seq.markers[r].frame)

    def _set_color(self, r, col):
        self.mw.begin_edit()
        self.mw.project.sequence.markers[r].color = col
        self.mw.end_edit("マーカーの色")

    def _edited(self, it):
        if self._loading:
            return
        seq = self.mw.project.sequence
        r = it.row()
        if r >= len(seq.markers) or it.column() not in (2, 3):
            return
        self.mw.begin_edit()
        if it.column() == 2:
            seq.markers[r].name = it.text()
        else:
            seq.markers[r].comment = it.text()
        self.mw.end_edit("マーカーを編集", reload_inspector=False)

    def _delete(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self.mw.begin_edit()
        seq = self.mw.project.sequence
        for r in rows:
            if r < len(seq.markers):
                del seq.markers[r]
        self.mw.end_edit("マーカーを削除")

    def _jump(self, d):
        seq = self.mw.project.sequence
        f = self.mw.record_player.position
        pts = sorted(m.frame for m in seq.markers)
        cand = [p for p in pts if (p > f if d > 0 else p < f)]
        if cand:
            self.mw.seek_record(cand[0] if d > 0 else cand[-1])


# ─────────────────────────────────────────────
#  テロップ一覧（DavinTelop 統合の第一歩）
# ─────────────────────────────────────────────

class TelopListPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._loading = False
        self._ids: List[str] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        for text, fn, tip in (("＋ 追加", lambda: mw.add_telop(), "再生ヘッドにテロップを追加"),
                              ("削除", self._delete, "選択したテロップを削除"),
                              ("プリセット...", self._preset, "選択したテロップに DavinTelop プリセットを適用"),
                              ("SRT 書き出し", mw.export_srt, "テロップを SRT で書き出し")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.count = QLabel("")
        self.count.setObjectName("info")
        row.addWidget(self.count)
        v.addLayout(row)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["開始", "終了", "トラック", "テキスト"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for i, w in enumerate((96, 96, 50)):
            self.table.setColumnWidth(i, w)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._edited)
        self.table.cellDoubleClicked.connect(self._double)
        self.table.itemSelectionChanged.connect(self._select)
        v.addWidget(self.table, 1)
        hint = QLabel("テキスト・開始・終了は直接編集できます（Enter で確定）。ダブルクリックでその位置へ移動")
        hint.setObjectName("info")
        hint.setWordWrap(True)
        v.addWidget(hint)

    def _texts(self):
        seq = self.mw.project.sequence
        out = []
        for t in seq.video_tracks:
            for c in t.clips:
                if c.kind == "text":
                    out.append((t, c))
        out.sort(key=lambda tc: (tc[1].start, tc[0].name))
        return out

    def refresh(self):
        if self.table.state() == QAbstractItemView.State.EditingState:
            return
        seq = self.mw.project.sequence
        off = self.mw.tc_offset(seq)
        items = self._texts()
        self._loading = True
        self._ids = [c.id for _, c in items]
        self.table.setRowCount(len(items))
        for r, (t, c) in enumerate(items):
            self.table.setItem(r, 0, QTableWidgetItem(frames_to_timecode(c.start + off, seq.fps)))
            self.table.setItem(r, 1, QTableWidgetItem(frames_to_timecode(c.end + off, seq.fps)))
            ti = QTableWidgetItem(t.name)
            ti.setFlags(ti.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 2, ti)
            self.table.setItem(r, 3, QTableWidgetItem(c.prop("text", "").replace("\n", "⏎")))
        self.count.setText(f"{len(items)} 件")
        self._loading = False

    def _clip(self, r):
        if r < 0 or r >= len(self._ids):
            return None, None
        return self.mw.project.sequence.find(self._ids[r])

    def _edited(self, it):
        if self._loading:
            return
        t, c = self._clip(it.row())
        if c is None:
            return
        seq = self.mw.project.sequence
        off = self.mw.tc_offset(seq)
        self.mw.begin_edit()
        try:
            if it.column() == 3:
                c.props["text"] = it.text().replace("⏎", "\n")
            elif it.column() in (0, 1):
                f = timecode_to_frames(normalize_timecode(it.text(), seq.fps), seq.fps) - off
                if it.column() == 0 and 0 <= f < c.end:
                    c.duration, c.start = c.end - f, f
                elif it.column() == 1 and f > c.start:
                    c.duration = f - c.start
                t.sort()
        except ValueError:
            pass
        self.mw.end_edit("テロップを編集")

    def _double(self, r, _c):
        t, c = self._clip(r)
        if c is not None:
            self.mw.seek_record(c.start)

    def _select(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        ids = {self._ids[r] for r in rows if r < len(self._ids)}
        if ids and ids != self.mw.canvas.selected:
            self.mw.canvas.selected = set(ids)
            self.mw.canvas.selection_changed.emit()
            self.mw.canvas.update()

    def _delete(self):
        rows = {i.row() for i in self.table.selectedIndexes()}
        ids = {self._ids[r] for r in rows if r < len(self._ids)}
        if not ids:
            return
        self.mw.begin_edit()
        M.delete_clips(self.mw.project.sequence, ids, ripple=False)
        self.mw.canvas.selected -= ids
        self.mw.end_edit("テロップを削除")

    def _preset(self):
        try:
            from core.preset import PresetManager
            names = PresetManager().list_presets()
        except Exception:
            names = []
        m = QMenu(self)
        if not names:
            m.addAction("（DavinTelop のプリセットがありません）").setEnabled(False)
        for n in names:
            m.addAction(n, lambda n=n: (self._select(), self.mw.apply_effect({"type": "telop_preset", "name": n})))
        m.exec(self.cursor().pos())


# ─────────────────────────────────────────────
#  履歴
# ─────────────────────────────────────────────

class HistoryPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.list = QListWidget()
        self.list.itemClicked.connect(self._clicked)
        v.addWidget(self.list, 1)
        hint = QLabel("クリックした時点まで戻す / やり直す（灰色はやり直し可能な操作）")
        hint.setObjectName("info")
        hint.setWordWrap(True)
        v.addWidget(hint)

    def refresh(self):
        self.list.clear()
        self.list.addItem(QListWidgetItem("（開いた時点）"))
        for label, _ in self.mw._undo:
            self.list.addItem(QListWidgetItem(label))
        cur = self.list.count() - 1
        for label, _ in reversed(self.mw._redo):
            it = QListWidgetItem(label)
            it.setForeground(QColor(C["dim"]))
            self.list.addItem(it)
        self.list.setCurrentRow(cur)

    def _clicked(self, it):
        target = self.list.row(it)
        cur = len(self.mw._undo)
        while cur > target and self.mw._undo:
            self.mw.undo()
            cur -= 1
        while cur < target and self.mw._redo:
            self.mw.redo()
            cur += 1


# ─────────────────────────────────────────────
#  ビデオスコープ
# ─────────────────────────────────────────────

class ScopeView(QWidget):
    def __init__(self):
        super().__init__()
        self._img: Optional[QImage] = None
        self.setMinimumHeight(140)

    def set_image(self, img: Optional[QImage]):
        self._img = img
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0b0b0c"))
        if self._img is not None:
            p.drawImage(self.rect(), self._img)
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        for k in range(1, 4):
            y = int(self.height() * k / 4)
            p.drawLine(0, y, self.width(), y)
        p.end()


def _qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class ScopesPanel(QWidget):
    """レコードモニターの映像から、波形・RGB パレード・ヒストグラム・ベクトルスコープを表示。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(["波形（輝度）", "RGB パレード", "ヒストグラム", "ベクトルスコープ"])
        self.mode.currentIndexChanged.connect(lambda _i: self._update())
        row.addWidget(self.mode)
        row.addStretch(1)
        v.addLayout(row)
        self.view = ScopeView()
        v.addWidget(self.view, 1)
        self._last = None
        self._t = 0.0
        self._pending = False
        mw.record_player.image_changed.connect(self._on_image)

    def _on_image(self, img: QImage):
        self._last = img
        if not self.isVisible():
            return
        now = time.time()
        if now - self._t > 0.2:
            self._t = now
            self._update()
        elif not self._pending:
            self._pending = True
            QTimer.singleShot(220, self._deferred)

    def _deferred(self):
        self._pending = False
        self._update()

    def showEvent(self, e):
        super().showEvent(e)
        self._update()

    def _frame(self) -> Optional[np.ndarray]:
        img = self._last
        if img is None:
            return None
        img = img.convertToFormat(QImage.Format.Format_RGB888).scaled(320, 180)
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        arr = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width() * 3]
        return arr.reshape(img.height(), img.width(), 3).copy()

    def _update(self):
        arr = self._frame()
        if arr is None:
            self.view.set_image(None)
            return
        mode = self.mode.currentIndex()
        H = 200
        if mode in (0, 1):
            h, w = arr.shape[:2]
            if mode == 0:
                y = (0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]).astype(np.int32)
                chans = [(y, (200, 255, 200))]
                width = w
            else:
                chans = [(arr[..., i].astype(np.int32), col) for i, col in
                         enumerate(((255, 80, 80), (80, 255, 80), (90, 140, 255)))]
                width = w * 3
            out = np.zeros((H, width, 3), np.float32)
            for k, (ch, col) in enumerate(chans):
                xs = np.tile(np.arange(w), h) + (k * w if mode == 1 else 0)
                ys = (H - 1 - ch.reshape(-1) * (H - 1) // 255)
                acc = np.zeros((H, width), np.float32)
                np.add.at(acc, (ys, xs), 1.0)
                acc = np.clip(acc / (h * 0.08), 0, 1)
                for c in range(3):
                    out[..., c] += acc * col[c]
            self.view.set_image(_qimage(np.clip(out, 0, 255).astype(np.uint8)))
        elif mode == 2:
            out = np.zeros((H, 256, 3), np.uint8)
            for i, col in enumerate(((255, 80, 80), (80, 255, 80), (90, 140, 255))):
                hist = np.bincount(arr[..., i].reshape(-1), minlength=256).astype(np.float32)
                hist = hist / max(1.0, hist.max()) * (H - 1)
                for x in range(256):
                    hgt = int(hist[x])
                    if hgt:
                        out[H - hgt:, x] = np.maximum(out[H - hgt:, x], np.array(col, np.uint8) // 2)
            self.view.set_image(_qimage(out))
        else:
            S = 200
            r, g, b = [arr[..., i].astype(np.float32) / 255 for i in range(3)]
            y = 0.2126 * r + 0.7152 * g + 0.0722 * b
            cb = (b - y) / 1.8556
            cr = (r - y) / 1.5748
            xs = np.clip((cb * 1.6 + 0.5) * (S - 1), 0, S - 1).astype(np.int32).reshape(-1)
            ys = np.clip((0.5 - cr * 1.6) * (S - 1), 0, S - 1).astype(np.int32).reshape(-1)
            acc = np.zeros((S, S), np.float32)
            np.add.at(acc, (ys, xs), 1.0)
            acc = np.clip(acc / 6.0, 0, 1)
            out = np.zeros((S, S, 3), np.uint8)
            out[..., 1] = (acc * 255).astype(np.uint8)
            out[..., 0] = (acc * 180).astype(np.uint8)
            yy, xx = np.ogrid[:S, :S]
            ring = np.abs(np.sqrt((xx - S / 2) ** 2 + (yy - S / 2) ** 2) - S * 0.45) < 0.8
            out[ring] = (70, 70, 80)
            self.view.set_image(_qimage(out))


# ─────────────────────────────────────────────
#  オーディオミキサー + メーター
# ─────────────────────────────────────────────

class LevelMeter(QWidget):
    def __init__(self, vertical=True):
        super().__init__()
        self.vertical = vertical
        self.l = self.r = -90.0
        self.peak_l = self.peak_r = -90.0
        self._peak_t = 0.0
        if vertical:
            self.setFixedWidth(22)
            self.setMinimumHeight(100)
        else:
            self.setFixedHeight(8)
            self.setMinimumWidth(60)

    def set_levels(self, l, r):
        self.l, self.r = l, r
        now = time.time()
        if l > self.peak_l or r > self.peak_r or now - self._peak_t > 1.5:
            self.peak_l, self.peak_r = max(l, -90), max(r, -90)
            self._peak_t = now
        self.update()

    @staticmethod
    def _norm(db):
        return max(0.0, min(1.0, (db + 60) / 60))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#101012"))
        W, H = self.width(), self.height()
        for i, (db, pk) in enumerate(((self.l, self.peak_l), (self.r, self.peak_r))):
            v = self._norm(db)
            if self.vertical:
                x = 2 + i * (W - 2) // 2
                w = (W - 6) // 2
                g = QLinearGradient(0, H, 0, 0)
                g.setColorAt(0.0, QColor("#2fae5a"))
                g.setColorAt(0.75, QColor("#d6b23a"))
                g.setColorAt(1.0, QColor("#d8463c"))
                p.fillRect(QRectF(x, H * (1 - v), w, H * v), g)
                py = H * (1 - self._norm(pk))
                p.fillRect(QRectF(x, py, w, 2), QColor("#ffffff"))
            else:
                y = 1 + i * (H // 2)
                h = max(1, H // 2 - 1)
                g = QLinearGradient(0, 0, W, 0)
                g.setColorAt(0.0, QColor("#2fae5a"))
                g.setColorAt(0.75, QColor("#d6b23a"))
                g.setColorAt(1.0, QColor("#d8463c"))
                p.fillRect(QRectF(0, y, W * v, h), g)
        p.end()


class _Strip(QFrame):
    def __init__(self, mw, idx: int):
        super().__init__()
        self.mw = mw
        self.idx = idx
        self.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        self.name = QLabel("")
        self.name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.name.setStyleSheet("font-weight: bold; background: transparent;")
        v.addWidget(self.name)
        self.pan = QSlider(Qt.Orientation.Horizontal)
        self.pan.setRange(-100, 100)
        self.pan.setToolTip("パン（ダブルクリックで中央）")
        self.pan.valueChanged.connect(self._pan)
        v.addWidget(self.pan)
        h = QHBoxLayout()
        self.vol = QSlider(Qt.Orientation.Vertical)
        self.vol.setRange(-600, 120)
        self.vol.setMinimumHeight(120)
        self.vol.valueChanged.connect(self._vol)
        h.addWidget(self.vol)
        v.addLayout(h, 1)
        self.db = QLabel("0.0 dB")
        self.db.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.db.setObjectName("info")
        v.addWidget(self.db)
        bh = QHBoxLayout()
        self.m = QToolButton()
        self.m.setText("M")
        self.m.setCheckable(True)
        self.m.clicked.connect(lambda on: self._flag("muted", on))
        self.s = QToolButton()
        self.s.setText("S")
        self.s.setCheckable(True)
        self.s.clicked.connect(lambda on: self._flag("solo", on))
        bh.addWidget(self.m)
        bh.addWidget(self.s)
        v.addLayout(bh)
        self._loading = False

    def track(self):
        seq = self.mw.project.sequence
        return seq.audio_tracks[self.idx] if self.idx < len(seq.audio_tracks) else None

    def load(self):
        t = self.track()
        if t is None:
            return
        self._loading = True
        self.name.setText(t.name)
        self.vol.setValue(int(round(t.volume_db * 10)))
        self.pan.setValue(int(round(t.pan * 100)))
        self.db.setText(f"{t.volume_db:+.1f} dB")
        self.m.setChecked(t.muted)
        self.s.setChecked(t.solo)
        self._loading = False

    def _vol(self, v):
        t = self.track()
        if t is None or self._loading:
            return
        t.volume_db = v / 10.0
        self.db.setText(f"{t.volume_db:+.1f} dB")
        self.mw.mixer_changed()

    def _pan(self, v):
        t = self.track()
        if t is None or self._loading:
            return
        t.pan = v / 100.0
        self.mw.mixer_changed()

    def _flag(self, key, on):
        t = self.track()
        if t is None:
            return
        setattr(t, key, on)
        self.mw.mixer_changed()
        self.mw.canvas.update()

    def mouseDoubleClickEvent(self, e):
        self.pan.setValue(0)


class MixerPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        h = QHBoxLayout(self)
        h.setContentsMargins(4, 4, 4, 4)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.row = QHBoxLayout(self.inner)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.area.setWidget(self.inner)
        h.addWidget(self.area, 1)
        mv = QVBoxLayout()
        lab = QLabel("マスター")
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mv.addWidget(lab)
        self.meter = LevelMeter(True)
        mv.addWidget(self.meter, 1, Qt.AlignmentFlag.AlignHCenter)
        self.peak = QLabel("-∞")
        self.peak.setObjectName("info")
        self.peak.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mv.addWidget(self.peak)
        h.addLayout(mv)
        self.strips: List[_Strip] = []
        mw.record_player.levels_changed.connect(self._levels)

    def refresh(self):
        n = len(self.mw.project.sequence.audio_tracks)
        while len(self.strips) > n:
            s = self.strips.pop()
            s.setParent(None)
            s.deleteLater()
        while len(self.strips) < n:
            s = _Strip(self.mw, len(self.strips))
            self.strips.append(s)
            self.row.addWidget(s)
        for s in self.strips:
            s.load()

    def _levels(self, l, r):
        self.meter.set_levels(l, r)
        m = max(l, r)
        self.peak.setText(f"{m:.1f} dB" if m > -89 else "-∞")


# ─────────────────────────────────────────────
#  大型タイムコード
# ─────────────────────────────────────────────

class TimecodeWindow(QWidget):
    def __init__(self, mw):
        super().__init__(None, Qt.WindowType.Tool)
        self.mw = mw
        self.setWindowTitle("タイムコード")
        self.resize(520, 140)
        v = QVBoxLayout(self)
        self.label = QLabel("00:00:00:00")
        f = QFont("Consolas")
        f.setPointSize(48)
        f.setBold(True)
        self.label.setFont(f)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("color: #e8e8ee; background: #0c0c0e;")
        v.addWidget(self.label)
        self.sub = QLabel("")
        self.sub.setObjectName("info")
        self.sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.sub)
        mw.record_player.position_changed.connect(self._update)
        self._update(mw.record_player.position)

    def _update(self, f):
        seq = self.mw.project.sequence
        self.label.setText(frames_to_timecode(f + self.mw.tc_offset(seq), seq.fps))
        self.sub.setText(f"{seq.name}   長さ {frames_to_timecode(seq.duration, seq.fps)}")
