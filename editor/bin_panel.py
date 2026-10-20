"""ビン（素材管理）とエフェクトパレット。"""
from __future__ import annotations

import json
import os
import threading
from typing import List

from PyQt6.QtCore import Qt, QSize, QMimeData, QThread, pyqtSignal, QByteArray
from PyQt6.QtGui import QIcon, QPixmap, QDrag, QImage, QColor, QPainter
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
                             QToolButton, QLineEdit, QLabel, QMenu, QTreeWidget,
                             QTreeWidgetItem, QAbstractItemView, QSplitter, QTableWidget,
                             QTableWidgetItem, QHeaderView, QTabWidget)

from core.timecode import frames_to_timecode
from . import media as MD
from .model import MediaItem
from .timeline import MIME_MEDIA, MIME_GEN, MIME_FX
from .theme import C


class ImportWorker(QThread):
    """解析 → 音声キャッシュ → 波形 → サムネイル をバックグラウンドで行う。"""

    item_ready = pyqtSignal(object, object)     # MediaItem, QImage or None
    item_progress = pyqtSignal(str)
    failed = pyqtSignal(str, str)
    audio_ready = pyqtSignal(str)               # media id

    def __init__(self, paths: List[str]):
        super().__init__()
        self.paths = paths
        self.cancel = threading.Event()

    def run(self):
        for p in self.paths:
            if self.cancel.is_set():
                return
            try:
                self.item_progress.emit(f"解析中: {p}")
                item = MD.probe(p)
            except Exception as ex:
                self.failed.emit(p, str(ex))
                continue
            th = MD.thumbnail(item)
            qimg = None
            if th is not None:
                data = th.tobytes("raw", "RGB")
                qimg = QImage(data, th.width, th.height, th.width * 3,
                              QImage.Format.Format_RGB888).copy()
            self.item_ready.emit(item, qimg)
            if item.has_audio:
                self.item_progress.emit(f"音声キャッシュ作成中: {item.name}")
                if MD.build_audio_cache(item.path, self.cancel):
                    MD.build_peaks(item.path)
                    self.audio_ready.emit(item.id)
        self.item_progress.emit("")


MIME_BINITEM = "application/x-telocut-binitem"


class BinView(QWidget):
    """ビン1つ分（Avid のビンウィンドウ）。テキスト表示 / フレーム表示。"""

    COLS = ["名前", "長さ", "開始", "種類", "解像度", "fps", "状態", "場所"]

    def __init__(self, container, bin_id: str):
        super().__init__()
        self.container = container
        self.mw = container.mw
        self.bin_id = bin_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        bar = QHBoxLayout()
        self.text_btn = QToolButton()
        self.text_btn.setText("☰ テキスト")
        self.text_btn.setCheckable(True)
        self.frame_btn = QToolButton()
        self.frame_btn.setText("▦ フレーム")
        self.frame_btn.setCheckable(True)
        for b in (self.text_btn, self.frame_btn):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            bar.addWidget(b)
        self.text_btn.clicked.connect(lambda: self.set_view("text"))
        self.frame_btn.clicked.connect(lambda: self.set_view("frame"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎 このビンを検索")
        self.search.textChanged.connect(self._filter)
        bar.addWidget(self.search, 1)
        lay.addLayout(bar)

        self.table = _BinTable(self)
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setIconSize(QSize(40, 22))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        for i, wdt in enumerate((200, 86, 86, 60, 86, 50, 80, 200)):
            self.table.setColumnWidth(i, wdt)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.itemDoubleClicked.connect(lambda it: self._open(self.table.item(it.row(), 0)))
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(lambda pos: self._menu(self.table.viewport().mapToGlobal(pos)))
        lay.addWidget(self.table, 1)

        self.grid = _BinGrid(self)
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(128, 72))
        self.grid.setGridSize(QSize(140, 108))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setWordWrap(True)
        self.grid.itemDoubleClicked.connect(self._open)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(lambda pos: self._menu(self.grid.viewport().mapToGlobal(pos)))
        lay.addWidget(self.grid, 1)
        self.status = QLabel("")
        self.status.setObjectName("info")
        lay.addWidget(self.status)
        b = self.bin
        self.set_view(b.view if b else "text", save=False)

    @property
    def bin(self):
        return self.mw.project.bin_by_id(self.bin_id)

    def set_view(self, view: str, save: bool = True):
        self.text_btn.setChecked(view == "text")
        self.frame_btn.setChecked(view == "frame")
        self.table.setVisible(view == "text")
        self.grid.setVisible(view == "frame")
        if save and self.bin:
            self.bin.view = view
        self.refresh()

    def _rows(self):
        b = self.bin
        if b is None:
            return []
        proj = self.mw.project
        fps = proj.sequence.fps
        out = []
        for it in b.items:
            if it.get("type") == "media":
                m = proj.media_by_id(it["id"])
                if m is None:
                    continue
                dur = frames_to_timecode(int(m.duration_s * fps), fps) if m.kind in ("video", "audio") else "静止画"
                flags = []
                if not os.path.exists(m.path):
                    flags.append("⚠オフライン")
                if m.original_path:
                    flags.append("ローカル")
                if m.kind == "video" and MD.has_proxy(m.path):
                    flags.append("プロキシ")
                kind = {"video": "映像", "audio": "音声", "image": "静止画"}.get(m.kind, m.kind)
                res = f"{m.width}×{m.height}" if m.width else ""
                out.append(("media", m.id, m.name, dur, "00:00:00:00", kind, res,
                            f"{m.fps:.2f}" if m.fps else "", " ".join(flags), m.path,
                            self.container.icon_for(m)))
            elif it.get("type") == "sequence":
                sq = proj.sequence_by_id(it["id"])
                if sq is None:
                    continue
                active = sq is proj.sequence
                out.append(("sequence", sq.id, sq.name, frames_to_timecode(sq.duration, sq.fps), sq.start_tc,
                            "シーケンス", f"{sq.width}×{sq.height}", f"{sq.fps:g}",
                            "● 表示中" if active else "", "", self.container.seq_icon()))
        return out

    def refresh(self):
        sel = set(self.selected_ids())
        rows = self._rows()
        q = self.search.text().strip().lower()
        if q:
            rows = [r for r in rows if q in r[2].lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            typ, iid = row[0], row[1]
            for c, val in enumerate(row[2:10]):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setIcon(row[10])
                    it.setData(Qt.ItemDataRole.UserRole, (typ, iid))
                    if typ == "sequence":
                        f = it.font()
                        f.setBold(True)
                        it.setFont(f)
                self.table.setItem(r, c, it)
            if iid in sel:
                self.table.selectRow(r)
        self.table.setSortingEnabled(True)
        self.grid.clear()
        for row in rows:
            it = QListWidgetItem(row[10], f"{row[2]}\n{row[3]}")
            it.setData(Qt.ItemDataRole.UserRole, (row[0], row[1]))
            it.setToolTip(row[9] or row[2])
            self.grid.addItem(it)
            if row[1] in sel:
                it.setSelected(True)
        n_m = sum(1 for r in rows if r[0] == "media")
        n_s = len(rows) - n_m
        self.status.setText(f"素材 {n_m} / シーケンス {n_s}")

    def _filter(self, _t):
        self.refresh()

    def selected(self):
        """[(type, id)]"""
        out = []
        if self.table.isVisible():
            rows = sorted({i.row() for i in self.table.selectedIndexes()})
            for r in rows:
                it = self.table.item(r, 0)
                if it is not None:
                    out.append(it.data(Qt.ItemDataRole.UserRole))
        else:
            out = [it.data(Qt.ItemDataRole.UserRole) for it in self.grid.selectedItems()]
        return [x for x in out if x]

    def selected_ids(self, typ: str = "media"):
        return [i for t, i in self.selected() if t == typ]

    def _open(self, it):
        if it is None:
            return
        data = it.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        typ, iid = data
        if typ == "media":
            self.container.open_in_source.emit(iid)
        else:
            self.mw.load_sequence(iid)

    def _menu(self, gpos):
        sel = self.selected()
        mids = [i for t, i in sel if t == "media"]
        sids = [i for t, i in sel if t == "sequence"]
        mw = self.mw
        m = QMenu(self)
        if mids:
            m.addAction("ソースモニターで開く", lambda: self.container.open_in_source.emit(mids[0]))
            m.addAction("再生ヘッドに上書き配置", lambda: mw.place_media_at(mids, None, None, False))
            m.addAction("再生ヘッドに挿入", lambda: mw.place_media_at(mids, None, None, True))
            m.addSeparator()
            m.addAction("プロキシを作成", lambda: mw.make_proxies([x for x in mw.project.media if x.id in mids]))
            m.addAction("トランスコード（DNxHR / ProRes など）...",
                        lambda: mw.transcode_dialog([x for x in mw.project.media if x.id in mids]))
            m.addAction("使用箇所を表示...", mw.media_usage_dialog)
            m.addAction("ローカルにコピー（NAS → この PC）", lambda: mw.copy_local(mids))
            m.addAction("元の場所（NAS）に戻す", lambda: mw.restore_original(mids))
            m.addAction("オフライン素材を再リンク...", mw.relink_media)
            m.addSeparator()
        if sids:
            m.addAction("レコードモニターに表示", lambda: mw.load_sequence(sids[0]))
            m.addAction("シーケンスを複製", lambda: mw.duplicate_sequence(sids[0]))
            m.addAction("名前を変更...", lambda: mw.rename_sequence(sids[0]))
            m.addSeparator()
        if sel:
            mv = m.addMenu("別のビンへ移動")
            for b in mw.project.bins:
                if b.id != self.bin_id:
                    mv.addAction(b.name, lambda bid=b.id: mw.move_to_bin([i for _, i in sel], bid))
            m.addAction("削除", lambda: mw.delete_bin_items(sel))
            m.addSeparator()
        m.addAction("新規シーケンス", lambda: mw.new_sequence(self.bin_id))
        m.addAction("素材を読み込み...", self.container.import_requested.emit)
        m.exec(gpos)


def _bin_mime(view):
    sel = view.selected()
    md = QMimeData()
    mids = [i for t, i in sel if t == "media"]
    if mids:
        md.setData(MIME_MEDIA, QByteArray(",".join(mids).encode()))
    md.setData(MIME_BINITEM, QByteArray(json.dumps({"from": view.bin_id, "items": sel}).encode()))
    return md


class _DropMixin:
    def _accept(self, e):
        md = e.mimeData()
        if md.hasUrls() or md.hasFormat(MIME_BINITEM):
            e.acceptProposedAction()
            return True
        return False

    def _drop(self, e):
        md = e.mimeData()
        v = self.view
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            v.mw.import_files(paths, bin_id=v.bin_id)
        elif md.hasFormat(MIME_BINITEM):
            d = json.loads(bytes(md.data(MIME_BINITEM)).decode())
            if d.get("from") != v.bin_id:
                v.mw.move_to_bin([i for _, i in d.get("items", [])], v.bin_id)
        e.acceptProposedAction()


class _BinTable(QTableWidget, _DropMixin):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

    def startDrag(self, actions):
        md = _bin_mime(self.view)
        d = QDrag(self)
        d.setMimeData(md)
        d.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)

    def dragEnterEvent(self, e):
        if not self._accept(e):
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if not self._accept(e):
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        self._drop(e)


class _BinGrid(QListWidget, _DropMixin):
    def __init__(self, view):
        super().__init__()
        self.view = view
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setMovement(QListWidget.Movement.Static)

    def startDrag(self, actions):
        md = _bin_mime(self.view)
        d = QDrag(self)
        d.setMimeData(md)
        it = self.currentItem()
        if it is not None:
            d.setPixmap(it.icon().pixmap(96, 54))
        d.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)

    def dragEnterEvent(self, e):
        if not self._accept(e):
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if not self._accept(e):
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        self._drop(e)


class BinContainer(QWidget):
    """Avid のビンコンテナ: 開いているビンをタブで並べる。"""

    open_in_source = pyqtSignal(str)
    import_requested = pyqtSignal()

    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self._icons = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        bar = QHBoxLayout()
        b = QToolButton()
        b.setText("＋ 読み込み")
        b.setToolTip("素材を読み込み（ファイルをビンへドラッグしても可）")
        b.clicked.connect(self.import_requested.emit)
        bar.addWidget(b)
        b = QToolButton()
        b.setText("＋ シーケンス")
        b.clicked.connect(lambda: self.mw.new_sequence(self.current_bin_id()))
        bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        lay.addWidget(self.tabs, 1)
        self.setAcceptDrops(True)

    # ── タブ ─────────────────────────────────
    def open_bin(self, bin_id: str):
        for i in range(self.tabs.count()):
            if self.tabs.widget(i).bin_id == bin_id:
                self.tabs.setCurrentIndex(i)
                return
        b = self.mw.project.bin_by_id(bin_id)
        if b is None:
            return
        v = BinView(self, bin_id)
        self.tabs.addTab(v, "🗀 " + b.name)
        self.tabs.setCurrentWidget(v)

    def close_tab(self, i: int):
        w = self.tabs.widget(i)
        self.tabs.removeTab(i)
        w.deleteLater()

    def reset(self):
        while self.tabs.count():
            self.close_tab(0)
        for b in self.mw.project.bins[:1]:
            self.open_bin(b.id)

    def current_view(self):
        return self.tabs.currentWidget()

    def current_bin_id(self):
        v = self.current_view()
        if v is not None:
            return v.bin_id
        return self.mw.project.bins[0].id if self.mw.project.bins else None

    # ── 互換 API ─────────────────────────────
    def refresh(self):
        for i in range(self.tabs.count() - 1, -1, -1):
            v = self.tabs.widget(i)
            b = v.bin
            if b is None:
                self.close_tab(i)
                continue
            self.tabs.setTabText(i, "🗀 " + b.name)
            v.refresh()
        if self.tabs.count() == 0 and self.mw.project.bins:
            self.open_bin(self.mw.project.bins[0].id)

    def selected_ids(self) -> List[str]:
        v = self.current_view()
        return v.selected_ids("media") if v is not None else []

    def set_icon(self, media_id: str, img):
        if img is not None:
            self._icons[media_id] = QIcon(QPixmap.fromImage(img))

    def icon_for(self, m: MediaItem) -> QIcon:
        if m.id in self._icons:
            return self._icons[m.id]
        pm = QPixmap(128, 72)
        pm.fill(QColor(C["clip_audio"] if m.kind == "audio" else C["panel3"]))
        p = QPainter(pm)
        p.setPen(QColor("white"))
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "♪ 音声" if m.kind == "audio" else m.kind)
        p.end()
        return QIcon(pm)

    def seq_icon(self) -> QIcon:
        if "__seq__" not in self._icons:
            pm = QPixmap(128, 72)
            pm.fill(QColor("#2a2d3a"))
            p = QPainter(pm)
            for i, col in enumerate((C["clip_text"], C["clip_video"], C["clip_audio"])):
                p.fillRect(10 + i * 8, 14 + i * 16, 100 - i * 14, 12, QColor(col))
            p.end()
            self._icons["__seq__"] = QIcon(pm)
        return self._icons["__seq__"]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        self.mw.import_files(paths, bin_id=self.current_bin_id())


# ─────────────────────────────────────────────
#  エフェクトパレット
# ─────────────────────────────────────────────

def effect_catalog():
    cat = [
        ("ビデオ トランジション", [
            ("クロスディゾルブ（0.5秒）", {"type": "dissolve", "sec": 0.5}),
            ("クロスディゾルブ（1秒）", {"type": "dissolve", "sec": 1.0}),
            ("ディゾルブを解除", {"type": "dissolve", "sec": 0}),
            ("フェードイン（0.5秒）", {"type": "fade", "in": 0.5}),
            ("フェードアウト（0.5秒）", {"type": "fade", "out": 0.5}),
            ("フェードイン/アウト（0.5秒）", {"type": "fade", "in": 0.5, "out": 0.5}),
            ("フェードイン/アウト（1秒）", {"type": "fade", "in": 1.0, "out": 1.0}),
            ("フェードを解除", {"type": "fade", "in": 0, "out": 0, "clear": True}),
        ]),
        ("カラー", [
            ("白黒", {"type": "effect", "name": "mono"}),
            ("セピア", {"type": "effect", "name": "sepia"}),
            ("明るさ +20%", {"type": "effect", "name": "brightness", "value": 1.2}),
            ("明るさ -20%", {"type": "effect", "name": "brightness", "value": 0.8}),
            ("コントラスト +20%", {"type": "effect", "name": "contrast", "value": 1.2}),
            ("彩度 50%", {"type": "effect", "name": "saturation", "value": 0.5}),
            ("彩度 150%", {"type": "effect", "name": "saturation", "value": 1.5}),
            ("色反転", {"type": "effect", "name": "invert"}),
            ("ぼかし（弱）", {"type": "effect", "name": "blur", "value": 0.4}),
            ("ぼかし（強）", {"type": "effect", "name": "blur", "value": 1.5}),
            ("シャープ", {"type": "effect", "name": "sharpen", "value": 1.0}),
            ("エフェクトをすべて解除", {"type": "effect_clear"}),
        ]),
        ("モーション", [
            ("ピクチャー・イン・ピクチャー（右上 35%）", {"type": "motion", "scale": 0.35, "pos_x": 560, "pos_y": -300}),
            ("ピクチャー・イン・ピクチャー（左下 35%）", {"type": "motion", "scale": 0.35, "pos_x": -560, "pos_y": 300}),
            ("全画面にリセット", {"type": "motion", "scale": 1.0, "pos_x": 0, "pos_y": 0, "rotation": 0}),
            ("Ken Burns（ゆっくりズームイン）", {"type": "anim", "anim": {"scale": [1.0, 1.15]}}),
            ("Ken Burns（ゆっくりズームアウト）", {"type": "anim", "anim": {"scale": [1.15, 1.0]}}),
            ("左からスライドイン", {"type": "anim", "anim": {"pos_x": [-1920, 0]}}),
            ("アニメーションを解除", {"type": "anim", "anim": {}}),
        ]),
        ("速度", [
            ("50%（スロー）", {"type": "speed", "value": 0.5}),
            ("200%（早回し）", {"type": "speed", "value": 2.0}),
            ("逆再生", {"type": "speed", "value": -1.0}),
            ("100% に戻す", {"type": "speed", "value": 1.0}),
        ]),
        ("オーディオ", [
            ("音量 -6dB", {"type": "volume", "delta": -6}),
            ("音量 +6dB", {"type": "volume", "delta": 6}),
            ("音量を 0dB に戻す", {"type": "volume", "set": 0}),
            ("オーディオフェード（0.3秒）", {"type": "fade", "in": 0.3, "out": 0.3}),
        ]),
        ("ジェネレーター（ドラッグで配置）", [
            ("テロップ（テキスト）", {"type": "gen", "kind": "text"}),
            ("調整レイヤー（下のトラック全体にエフェクト）", {"type": "gen", "kind": "adjust"}),
            ("図形: 楕円", {"type": "gen", "kind": "shape", "shape": "ellipse"}),
            ("図形: 星", {"type": "gen", "kind": "shape", "shape": "star"}),
            ("図形: 矢印", {"type": "gen", "kind": "shape", "shape": "arrow"}),
            ("ヌル（親専用）", {"type": "gen", "kind": "null"}),
            ("カメラ（3D レイヤー用）", {"type": "gen", "kind": "camera"}),
            ("カラーマット（黒）", {"type": "gen", "kind": "color", "color": "#000000"}),
            ("カラーマット（白）", {"type": "gen", "kind": "color", "color": "#FFFFFF"}),
            ("カラーマット（青）", {"type": "gen", "kind": "color", "color": "#1F3A8A"}),
        ]),
    ]
    # プラグインのエフェクト（組み込み + plugins フォルダ）
    try:
        from . import fxlib
        fxlib.load()
        order = ["カラー", "スタイライズ", "ゆがみ", "3D", "演出", "マスク・枠"]
        cats = fxlib.categories()
        for cname in order + sorted(k for k in cats if k not in order):
            if cname in cats:
                cat.append((f"🧩 {cname}", [(e.name + ("" if e.source == "組み込み" else f"  〔{e.source}〕"),
                                             {"type": "plugin", "name": e.id})
                                            for e in sorted(cats[cname], key=lambda e: e.name)]))
    except Exception:
        pass
    # DavinTelop のデザインプリセット
    try:
        from core.preset import PresetManager
        pm = PresetManager()
        presets = [(f"🎨 {n}", {"type": "telop_preset", "name": n}) for n in pm.list_presets()]
    except Exception:
        presets = []
    cat.append(("テロップ スタイル（DavinTelop プリセット）",
                presets or [("（プリセットなし: DavinTelop で保存すると表示）", {"type": "none"})]))
    return cat


class EffectPalette(QWidget):
    """Avid の Effect Palette 風。ダブルクリックで選択クリップに適用、ドラッグでクリップへ。"""

    apply = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎 エフェクトを検索")
        self.search.textChanged.connect(self._filter)
        lay.addWidget(self.search)
        self.tree = _FxTree()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._on_double)
        lay.addWidget(self.tree, 1)
        hint = QLabel("ダブルクリック: 選択クリップに適用\nドラッグ: クリップへ / ジェネレーターはトラックへ")
        hint.setObjectName("info")
        lay.addWidget(hint)
        self.reload()

    def reload(self):
        self.tree.clear()
        for cat, items in effect_catalog():
            top = QTreeWidgetItem([cat])
            top.setFlags(top.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            for name, fx in items:
                it = QTreeWidgetItem([name])
                it.setData(0, Qt.ItemDataRole.UserRole, json.dumps(fx, ensure_ascii=False))
                top.addChild(it)
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)

    def _filter(self, text):
        t = text.strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            any_vis = False
            for j in range(top.childCount()):
                ch = top.child(j)
                vis = not t or t in ch.text(0).lower()
                ch.setHidden(not vis)
                any_vis |= vis
            top.setHidden(not any_vis)

    def _on_double(self, it, _col):
        data = it.data(0, Qt.ItemDataRole.UserRole)
        if data:
            self.apply.emit(json.loads(data))


class _FxTree(QTreeWidget):
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)

    def startDrag(self, actions):
        it = self.currentItem()
        if it is None:
            return
        data = it.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        fx = json.loads(data)
        md = QMimeData()
        if fx.get("type") == "gen":
            md.setData(MIME_GEN, QByteArray(data.encode()))
        else:
            md.setData(MIME_FX, QByteArray(data.encode()))
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.DropAction.CopyAction)
