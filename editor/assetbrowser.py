"""アセットサーバーの素材を見る画面（アセットパネルの「サーバー」タブ）。

表示の種類:
  実フォルダ    … マウントしている構成そのまま（エクスプローラーと同じ。空フォルダ・素材以外のファイルも）
  マイフォルダ  … 自分で作る構成（サーバーで共有）
  検索 / 新着 / 最近使った / 重複 / 保存した検索
プレビュー: 動画・音声はその場で再生（Space・J/K/L・←→・I/O）、画像・PSD は大きく、音声は波形も
パス: Windows の形（Z:\\…）で表示し、コピー・エクスプローラーで表示・既定のアプリで開く
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QMimeData, QSize, QTimer, QUrl
from PyQt6.QtGui import QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QHeaderView,
                             QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
                             QPushButton, QSlider, QSplitter, QStackedWidget, QToolButton, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout, QWidget, QApplication)

from . import media as MD
from .settings import SETTINGS

LABELS = [("", "なし", "#00000000"), ("red", "赤", "#e05050"), ("orange", "橙", "#e09040"), ("yellow", "黄", "#d8c040"),
          ("green", "緑", "#50b060"), ("blue", "青", "#4f8fe0"), ("purple", "紫", "#9a6ad0")]
LABEL_COLOR = {k: c for k, _l, c in LABELS}
KIND_JP = {"video": "動画", "audio": "音声", "image": "画像", "project": "プロジェクト", "other": "ファイル", "folder": "フォルダ"}
VIEWS = [("browse", "実フォルダ（マウントと同じ構成）"), ("vfolders", "マイフォルダ（独自の構成）"), ("search", "検索"),
         ("new", "新着（7 日以内）"), ("recent", "最近使った素材"), ("dup", "重複ファイル"), ("saved", "保存した検索")]
COLS = ["名前", "種類", "長さ", "解像度", "サイズ", "更新日", "評価", "ラベル", "パス"]


def _get(url: str, timeout=15, raw=False):
    from .assetpanel import _get as g
    return g(url, timeout, raw)


def human_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u in ("B", "KB") else f"{n:.1f} {u}"
        n /= 1024
    return ""


def human_dur(sec) -> str:
    s = int(sec or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else (f"{s // 60}:{s % 60:02d}" if s else "")


# ─────────────────────────── パスの読み替え ───────────────────────────
_drive_cache: Dict[str, str] = {}


def _drives() -> Dict[str, str]:
    """{"Z:": "\\\\192.168.0.10\\ES_General", ...}（起動中は 1 回だけ調べる）"""
    if not _drive_cache:
        from .assetpanel import network_drives, drive_remote
        for d in network_drives():
            _drive_cache[d] = drive_remote(d)
        _drive_cache.setdefault("_", "")
    return {k: v for k, v in _drive_cache.items() if k != "_"}


def drive_for_root(root: str) -> str:
    share = root.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
    for d, remote in _drives().items():
        if remote.lower().rstrip("\\").endswith("\\" + share):
            return d
    return ""


def to_local(server_path: str, roots: List[str]) -> str:
    """サーバーのパス（/mnt/telocut/ES_General/…）→ この PC のパス（Z:\\…）。分からなければそのまま。"""
    p = SETTINGS.map_path(server_path)
    if p != server_path:
        return p
    sp = server_path.replace("\\", "/")
    for r in sorted(roots, key=len, reverse=True):
        r = r.replace("\\", "/").rstrip("/")
        if sp == r or sp.startswith(r + "/"):
            d = drive_for_root(r)
            if d:
                return d + "\\" + sp[len(r):].lstrip("/").replace("/", "\\")
    return server_path


def unc_path(local: str) -> str:
    """Z:\\a\\b → \\\\192.168.0.10\\ES_General\\a\\b"""
    if len(local) >= 2 and local[1] == ":":
        remote = _drives().get(local[:2].upper())
        if remote:
            return remote.rstrip("\\") + local[2:]
    return local


def from_local(path: str, roots: List[str]) -> Optional[str]:
    """この PC のパス（Z:\\… や \\\\IP\\共有\\…）→ サーバーのパス。パスを貼ってジャンプするとき用。"""
    p = path.strip().strip('"')
    if not p:
        return None
    if p.startswith("/"):
        return p.rstrip("/")
    norm = p.replace("\\", "/").rstrip("/")
    for r in roots:                       # サーバーが Windows のとき（素材フォルダのパスがそのまま使える）
        rr = r.replace("\\", "/").rstrip("/")
        if norm.lower() == rr.lower() or norm.lower().startswith(rr.lower() + "/"):
            return rr + norm[len(rr):]
    if len(p) >= 2 and p[1] == ":":
        p = unc_path(p[:2].upper() + p[2:])
    u = p.replace("/", "\\").rstrip("\\").lower()
    for r in roots:
        d = drive_for_root(r)
        remote = _drives().get(d, "") if d else ""
        if remote and (u == remote.lower().rstrip("\\") or u.startswith(remote.lower().rstrip("\\") + "\\")):
            rest = p.replace("/", "\\").rstrip("\\")[len(remote.rstrip("\\")):].lstrip("\\")
            return r.replace("\\", "/").rstrip("/") + ("/" + rest.replace("\\", "/") if rest else "")
    return None


# ─────────────────────────── 一覧（ドラッグでタイムライン・ビンへ）───────────────────────────
class _AssetList(QListWidget):
    def __init__(self, br):
        super().__init__()
        self.br = br

    def mimeData(self, items):
        return self.br._mime([it.data(Qt.ItemDataRole.UserRole) for it in items])


class _AssetTree(QTreeWidget):
    def __init__(self, br):
        super().__init__()
        self.br = br

    def mimeData(self, items):
        return self.br._mime([it.data(0, Qt.ItemDataRole.UserRole) for it in items])


class _FolderTree(QTreeWidget):
    """マイフォルダ表示のとき、一覧からドラッグされた素材をフォルダに追加する。"""

    def __init__(self, br):
        super().__init__()
        self.br = br
        self.setAcceptDrops(True)

    def _target(self, e):
        it = self.itemAt(e.position().toPoint())
        d = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        return d[1] if d and d[0] == "vf" else None

    def dragEnterEvent(self, e):
        (e.acceptProposedAction() if e.source() in (self.br.list, self.br.detail) and self.br.view() == "vfolders"
         else e.ignore())

    def dragMoveEvent(self, e):
        e.acceptProposedAction() if self._target(e) else e.ignore()

    def dropEvent(self, e):
        fid = self._target(e)
        if fid:
            self.br.add_to_vfolder(fid, self.br.selected())
            e.acceptProposedAction()


class _Waveform(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(48)
        self.peaks: List[float] = []
        self.pos = 0.0

    def set_peaks(self, peaks):
        self.peaks = peaks or []
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#141518"))
        if self.peaks:
            w, h = self.width(), self.height()
            p.setPen(QPen(QColor("#4fb39a"), 1))
            n = len(self.peaks)
            for x in range(w):
                v = self.peaks[min(n - 1, int(x / w * n))]
                y = int(v * (h / 2 - 1))
                p.drawLine(x, h // 2 - y, x, h // 2 + y)
            p.setPen(QPen(QColor("#ffcc33"), 1))
            x = int(self.pos * w)
            p.drawLine(x, 0, x, h)
        p.end()


# ─────────────────────────── 本体 ───────────────────────────
class ServerBrowser(QWidget):
    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.mw = panel.mw
        self.f = panel.f
        self.roots: List[str] = []
        self.rows: List[dict] = []
        self._vf: Dict[str, dict] = {}
        self._gen = 0
        self._search_gen = 0
        self._pending_tries = 0
        self.cur_dir = ""
        self.in_ms = self.out_ms = -1
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        v.addLayout(self._bar1())
        v.addLayout(self._bar2())
        v.addLayout(self._crumbs())
        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = _FolderTree(self)
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._tree_expand)
        self.tree.currentItemChanged.connect(lambda _c, _p: self.load())
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        split.addWidget(self.tree)
        self.stack = QStackedWidget()
        self.list = _AssetList(self)
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setWordWrap(True)
        self.list.setDragEnabled(True)
        self.detail = _AssetTree(self)
        self.detail.setColumnCount(len(COLS))
        self.detail.setHeaderLabels(COLS)
        self.detail.setRootIsDecorated(False)
        self.detail.setSortingEnabled(True)
        self.detail.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.detail.setDragEnabled(True)
        self.detail.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for w_ in (self.list, self.detail):
            w_.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            w_.customContextMenuRequested.connect(self._item_menu)
        self.list.itemDoubleClicked.connect(lambda it: self.open_row(it.data(Qt.ItemDataRole.UserRole)))
        self.detail.itemDoubleClicked.connect(lambda it, _c: self.open_row(it.data(0, Qt.ItemDataRole.UserRole)))
        self.list.currentItemChanged.connect(lambda c, _p: self.preview(c.data(Qt.ItemDataRole.UserRole) if c else None))
        self.detail.currentItemChanged.connect(lambda c, _p: self.preview(c.data(0, Qt.ItemDataRole.UserRole) if c else None))
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.detail)
        split.addWidget(self.stack)
        split.addWidget(self._preview_pane())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([210, 440, 300])
        v.addWidget(split, 1)
        self._set_icon_size(self.size_slider.value())
        # キー操作（パネル内）
        for key, fn in (("Space", self.play_pause), ("K", self.stop), ("L", lambda: self.shuttle(1)),
                        ("J", lambda: self.shuttle(-1)), ("Left", lambda: self.step(-1)), ("Right", lambda: self.step(1)),
                        ("I", lambda: self.mark("in")), ("O", lambda: self.mark("out")),
                        ("Return", lambda: self.import_selected()), ("Backspace", self.go_up)):
            QShortcut(QKeySequence(key), self, activated=fn, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(20000)
        self._stats_timer.timeout.connect(self.refresh_stats)

    # ── 上のバー ──
    def _bar1(self):
        h = QHBoxLayout()
        self.url = QLineEdit(SETTINGS.asset_server)
        self.url.setPlaceholderText("http://192.168.0.100:8770（アセットサーバー）")
        self.url.returnPressed.connect(self.connect_server)
        con = QPushButton("接続")
        con.clicked.connect(self.connect_server)
        self.stat = QLabel("")
        self.stat.setObjectName("info")
        resc = QToolButton()
        resc.setText("⟳")
        resc.setToolTip("このフォルダを今すぐ読み直す（新しいファイルを登録）")
        resc.clicked.connect(lambda: self.rescan(self.cur_dir))
        up = QPushButton("アップロード")
        up.setToolTip("ビンで選んでいる素材をサーバーに送る")
        up.clicked.connect(self.panel.server_upload)
        for w_ in (self.url, con, self.stat, resc, up):
            h.addWidget(w_, 1 if w_ is self.url else 0)
        return h

    def _bar2(self):
        h = QHBoxLayout()
        self.view_cb = QComboBox()
        for k, lab in VIEWS:
            self.view_cb.addItem(lab, k)
        self.view_cb.currentIndexChanged.connect(lambda _i: self.reload())
        self.q = QLineEdit()
        self.q.setPlaceholderText("絞り込み（名前・タグ・コメント）")
        self.q.returnPressed.connect(self.load)
        self.kind = QComboBox()
        for lab, k in (("すべて", ""), ("動画", "video"), ("画像・PSD", "image"), ("音声", "audio")):
            self.kind.addItem(lab, k)
        self.rating = QComboBox()
        for lab, k in (("★すべて", 0), ("★1 以上", 1), ("★3 以上", 3), ("★5", 5)):
            self.rating.addItem(lab, k)
        self.label_cb = QComboBox()
        self.label_cb.addItem("ラベル", "")
        for k, lab, _c in LABELS[1:]:
            self.label_cb.addItem(lab, k)
        self.allfiles = QCheckBox("素材以外も")
        self.allfiles.setToolTip("実フォルダで、素材以外のファイル（zip・txt など）も表示")
        self.sort_cb = QComboBox()
        for lab, k in (("名前順", "name"), ("新しい順", "mtime"), ("大きい順", "size"), ("種類順", "kind")):
            self.sort_cb.addItem(lab, k)
        self.mode = QComboBox()
        self.mode.addItem("サムネイル", 0)
        self.mode.addItem("詳細", 1)
        self.mode.currentIndexChanged.connect(lambda i: self.stack.setCurrentIndex(i))
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(60, 240)
        self.size_slider.setValue(120)
        self.size_slider.setFixedWidth(70)
        self.size_slider.setToolTip("サムネイルの大きさ")
        self.size_slider.valueChanged.connect(self._set_icon_size)
        save = QToolButton()
        save.setText("💾")
        save.setToolTip("今の絞り込みを「保存した検索」に保存")
        save.clicked.connect(self.save_search)
        for w_ in (self.kind, self.rating, self.label_cb, self.allfiles, self.sort_cb):
            sig = w_.toggled if isinstance(w_, QCheckBox) else w_.currentIndexChanged
            sig.connect(lambda *_: self.load())
        for w_ in (self.view_cb, self.q, self.kind, self.rating, self.label_cb, self.allfiles, self.sort_cb, save,
                   self.mode, self.size_slider):
            h.addWidget(w_, 1 if w_ is self.q else 0)
        return h

    def _crumbs(self):
        h = QHBoxLayout()
        upb = QToolButton()
        upb.setText("↑")
        upb.setToolTip("上のフォルダへ（Backspace）")
        upb.clicked.connect(self.go_up)
        self.crumb_box = QHBoxLayout()
        self.crumb_box.setSpacing(0)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("パスを貼って Enter で移動（Z:\\… や \\\\192.168.0.10\\… も可）")
        self.path_edit.returnPressed.connect(self.jump_to_path)
        h.addWidget(upb)
        h.addLayout(self.crumb_box)
        h.addWidget(self.path_edit, 1)
        return h

    def _set_icon_size(self, v):
        self.list.setIconSize(QSize(v, int(v * 0.6)))
        self.list.setGridSize(QSize(v + 14, int(v * 0.6) + 34))

    # ── プレビュー ──
    def _preview_pane(self):
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        w = QWidget()
        w.setMinimumWidth(240)
        w.setMaximumWidth(420)
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)
        self.pv_stack = QStackedWidget()
        self.pv_stack.setMinimumHeight(150)
        self.pv_img = QLabel("素材を選ぶと\nプレビュー")
        self.pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pv_img.setStyleSheet("background: #111;")
        self.video = QVideoWidget()
        self.pv_stack.addWidget(self.pv_img)
        self.pv_stack.addWidget(self.video)
        v.addWidget(self.pv_stack, 1)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.audio.setVolume(0.8)
        self.player.positionChanged.connect(self._pos_changed)
        self.player.durationChanged.connect(lambda d: self.seek.setRange(0, max(0, d)))
        self.player.mediaStatusChanged.connect(self._status_changed)
        self.player.errorOccurred.connect(lambda *_: self.time_lab.setText("再生できません"))
        self.wave = _Waveform()
        v.addWidget(self.wave)
        t = QHBoxLayout()
        self.play_btn = QToolButton()
        self.play_btn.setText("▶")
        self.play_btn.setToolTip("再生 / 停止（Space）")
        self.play_btn.clicked.connect(self.play_pause)
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(self.player.setPosition)
        self.time_lab = QLabel("0:00")
        self.time_lab.setObjectName("info")
        self.loop = QCheckBox("ループ")
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(80)
        self.vol.setFixedWidth(50)
        self.vol.setToolTip("音量")
        self.vol.valueChanged.connect(lambda x: self.audio.setVolume(x / 100))
        for w_ in (self.play_btn, self.seek, self.time_lab, self.loop, self.vol):
            t.addWidget(w_, 1 if w_ is self.seek else 0)
        v.addLayout(t)
        io = QHBoxLayout()
        bi = QToolButton()
        bi.setText("IN")
        bi.setToolTip("IN 点（I）")
        bi.clicked.connect(lambda: self.mark("in"))
        bo = QToolButton()
        bo.setText("OUT")
        bo.setToolTip("OUT 点（O）")
        bo.clicked.connect(lambda: self.mark("out"))
        self.io_lab = QLabel("")
        self.io_lab.setObjectName("info")
        rng = QPushButton("範囲を取り込み")
        rng.setToolTip("IN〜OUT だけを使う素材として取り込み、再生ヘッドに配置")
        rng.clicked.connect(self.import_range)
        for w_ in (bi, bo, self.io_lab, rng):
            io.addWidget(w_, 1 if w_ is self.io_lab else 0)
        v.addLayout(io)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.info)
        pr = QHBoxLayout()
        cp = QToolButton()
        cp.setText("パスをコピー ▾")
        cp.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        cm = QMenu(cp)
        cm.addAction("Windows のパス（Z:\\…）", lambda: self.copy_path("win"))
        cm.addAction("ネットワークのパス（\\\\IP\\共有\\…）", lambda: self.copy_path("unc"))
        cm.addAction("サーバー側のパス", lambda: self.copy_path("server"))
        cp.setMenu(cm)
        ex = QToolButton()
        ex.setText("📂")
        ex.setToolTip("エクスプローラーで表示")
        ex.clicked.connect(lambda: self.reveal(self.current()))
        op = QToolButton()
        op.setText("↗")
        op.setToolTip("既定のアプリで開く")
        op.clicked.connect(lambda: self.open_external(self.current()))
        pr.addWidget(cp)
        pr.addWidget(ex)
        pr.addWidget(op)
        pr.addStretch(1)
        v.addLayout(pr)
        f = QFormLayout()
        sr = QHBoxLayout()
        self.stars = []
        for k in range(1, 6):
            b = QToolButton()
            b.setText("☆")
            b.setAutoRaise(True)
            b.clicked.connect(lambda _=False, k=k: self.set_meta(rating=0 if self._rating == k else k))
            self.stars.append(b)
            sr.addWidget(b)
        sr.addStretch(1)
        self._rating = 0
        f.addRow("評価", sr)
        self.lab_cb = QComboBox()
        for k, lab, c in LABELS:
            self.lab_cb.addItem(lab, k)
        self.lab_cb.activated.connect(lambda _i: self.set_meta(label=self.lab_cb.currentData()))
        f.addRow("ラベル", self.lab_cb)
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("タグ（, 区切り）")
        self.tags.editingFinished.connect(lambda: self.set_meta(tags=[t.strip() for t in self.tags.text().replace("、", ",").split(",") if t.strip()]))
        f.addRow("タグ", self.tags)
        self.comment = QLineEdit()
        self.comment.editingFinished.connect(lambda: self.set_meta(comment=self.comment.text()))
        f.addRow("メモ", self.comment)
        v.addLayout(f)
        # タイムコード付きコメント（Web の画面と共有。ダブルクリックでその位置へ）
        self.notes = QListWidget()
        self.notes.setMaximumHeight(90)
        self.notes.setToolTip("タイムコード付きコメント（ブラウザの画面と共有）。ダブルクリックでその位置へ")
        self.notes.itemDoubleClicked.connect(lambda it: self.player.setPosition(int(it.data(Qt.ItemDataRole.UserRole) * 1000)))
        self.notes.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.notes.customContextMenuRequested.connect(self._note_menu)
        v.addWidget(self.notes)
        self.note_in = QLineEdit()
        self.note_in.setPlaceholderText("今の位置にコメント（Enter）")
        self.note_in.returnPressed.connect(self.add_note)
        v.addWidget(self.note_in)
        br = QHBoxLayout()
        for txt, tip, fn in (("取り込む", "ビンに取り込む（Enter）", lambda: self.import_selected()),
                             ("配置", "取り込んで再生ヘッドに配置", lambda: self.import_selected(place=True)),
                             ("ソース", "取り込んでソースモニターで開く", lambda: self.import_selected(source=True))):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            br.addWidget(b)
        v.addLayout(br)
        self.psd_btn = QPushButton("PSD を立ち絵（キャラクター）として登録")
        self.psd_btn.clicked.connect(lambda: self.panel.register_psd_from_row(self.current()))
        self.psd_btn.setVisible(False)
        v.addWidget(self.psd_btn)
        return w

    # ── 接続・状態 ──
    def server(self) -> str:
        u = self.url.text().strip().rstrip("/")
        if u and not u.startswith("http"):
            u = "http://" + u
        if u != SETTINGS.asset_server:
            SETTINGS.asset_server = u
            SETTINGS.save()
        return u

    def view(self) -> str:
        return self.view_cb.currentData()

    def connect_server(self):
        if not self.server():
            self.panel.msg.setText("アセットサーバーの URL を入れてください")
            return
        self.f.run(("br_info",), _get, self.server() + "/api/info", 20)
        self.refresh_stats()
        self._stats_timer.start()

    def refresh_stats(self):
        if self.server() and self.isVisible():
            self.f.run(("br_stats",), _get, self.server() + "/api/stats", 20)

    def on_info(self, res):
        self.roots = [r.replace("\\", "/").rstrip("/") for r in res.get("roots") or []]
        self.panel._server_roots = self.roots
        self.reload()
        self.share_drives()

    def share_drives(self):
        """この PC のドライブ割り当て（Z: = \\\\NAS\\共有）をサーバーに送る。ブラウザの画面がその名前で表示する。"""
        roots, url = list(self.roots), self.server()
        if not roots or not url:
            return

        def work():
            from .rendercluster import _req
            m = {}
            for r in roots:
                d = drive_for_root(r)
                if d:
                    m[r] = {"drive": d, "unc": _drives().get(d, "")}
            if m:
                _req(url + "/api/roots", m)
            return bool(m)
        self.f.run(("br_done", False), work)

    def on_stats(self, s):
        busy = "　読み取り中…" if s.get("scanning") or s.get("indexing") else ""
        k = s.get("kinds", {})
        self.stat.setText(f"素材 {s.get('total', 0)}（動画 {k.get('video', {}).get('count', 0)}・画像 "
                          f"{k.get('image', {}).get('count', 0)}・音声 {k.get('audio', {}).get('count', 0)}）"
                          f" {human_size(s.get('total_size'))}{busy}")

    # ── 木 ──
    def reload(self):
        self.tree.clear()
        self.rows = []
        self._fill([])
        v = self.view()
        self.tree.setVisible(v in ("browse", "vfolders", "saved"))
        self.allfiles.setEnabled(v == "browse")
        if not self.server() or not self.roots and v == "browse":
            if self.server() and not self.roots:
                self.f.run(("br_info",), _get, self.server() + "/api/info", 20)
            return
        if v == "browse":
            for r in self.roots:
                d = drive_for_root(r)
                name = r.rsplit("/", 1)[-1]
                if r.endswith("/uploads"):
                    name = "アップロード（サーバー）"
                it = QTreeWidgetItem([f"💽 {name}" + (f" ({d})" if d else "")])
                it.setData(0, Qt.ItemDataRole.UserRole, ("dir", r))
                it.setToolTip(0, to_local(r, self.roots))
                it.addChild(QTreeWidgetItem(["…"]))
                self.tree.addTopLevelItem(it)
            if self.tree.topLevelItemCount():
                self.tree.setCurrentItem(self.tree.topLevelItem(0))
        elif v == "vfolders":
            self.f.run(("br_vfolders",), _get, self.server() + "/api/vfolders", 30)
        elif v == "saved":
            for i, s in enumerate(SETTINGS.asset_saved_searches):
                it = QTreeWidgetItem([f"🔎 {s['name']}"])
                it.setData(0, Qt.ItemDataRole.UserRole, ("saved", i))
                self.tree.addTopLevelItem(it)
            if self.tree.topLevelItemCount():
                self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self.load()

    def _tree_expand(self, item):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d and d[0] == "dir" and item.childCount() == 1 and item.child(0).data(0, Qt.ItemDataRole.UserRole) is None:
            self.f.run(("br_dirs", item), _get, self.server() + "/api/browse?path=" + urllib.parse.quote(d[1]), 30)

    def on_dirs(self, item, res):
        item.takeChildren()
        for d in res.get("dirs", []):
            it = QTreeWidgetItem([f"📁 {d['name']}"])
            it.setData(0, Qt.ItemDataRole.UserRole, ("dir", d["path"]))
            it.addChild(QTreeWidgetItem(["…"]))
            item.addChild(it)

    def on_vfolders(self, rows):
        cur = self.tree.currentItem()
        keep = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self.tree.clear()
        self._vf = {r["id"]: r for r in rows}
        self.panel._vf = self._vf
        items = {}
        for r in rows:
            it = QTreeWidgetItem([f"📁 {r['name']}（{r['count']}）"])
            it.setData(0, Qt.ItemDataRole.UserRole, ("vf", r["id"]))
            items[r["id"]] = it
        for r in rows:
            par = items.get(r.get("parent") or "")
            (par.addChild(items[r["id"]]) if par is not None else self.tree.addTopLevelItem(items[r["id"]]))
        self.tree.expandAll()
        if not rows:
            hint = QTreeWidgetItem(["（右クリック →「新しいフォルダ」）"])
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(hint)
        target = items.get(keep[1]) if keep and keep[0] == "vf" else None
        if target or rows:
            self.tree.setCurrentItem(target or self.tree.topLevelItem(0))
        else:
            self._fill([])

    def _tree_menu(self, pos):
        it = self.tree.itemAt(pos)
        d = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        m = QMenu(self)
        if self.view() == "vfolders":
            fid = d[1] if d and d[0] == "vf" else ""
            m.addAction("新しいフォルダ", lambda: self.new_vfolder(""))
            if fid:
                m.addAction("この中に新しいフォルダ", lambda: self.new_vfolder(fid))
                m.addAction("名前を変更", lambda: self.rename_vfolder(fid))
                m.addAction("いちばん上に移動", lambda: self._req(f"/api/vfolders/{fid}", {"parent": ""}, "PATCH"))
                m.addSeparator()
                m.addAction("削除（素材ファイルは消えません）", lambda: self.delete_vfolder(fid))
        elif self.view() == "browse" and d and d[0] == "dir":
            m.addAction("このフォルダを読み直す", lambda: self.rescan(d[1]))
            m.addAction("下のフォルダも含めて読み直す", lambda: self.rescan(d[1], True))
            m.addAction("フォルダごと取り込み（ビンもフォルダ名で作る）", lambda: self.import_folder(d[1]))
            m.addSeparator()
            m.addAction("パスをコピー", lambda: QApplication.clipboard().setText(to_local(d[1], self.roots)))
            m.addAction("エクスプローラーで開く", lambda: self.reveal({"path": d[1], "kind": "folder"}))
        elif self.view() == "saved" and d and d[0] == "saved":
            m.addAction("この保存した検索を削除", lambda: self.delete_saved(d[1]))
        if not m.isEmpty():
            m.exec(self.tree.mapToGlobal(pos))

    # ── 一覧の読み込み ──
    def _params(self) -> dict:
        qs = {"q": self.q.text().strip(), "kind": self.kind.currentData() or "", "limit": 500,
              "order": self.sort_cb.currentData() or "name"}
        if self.rating.currentData():
            qs["rating_min"] = self.rating.currentData()
        if self.label_cb.currentData():
            qs["label"] = self.label_cb.currentData()
        return qs

    def load(self):
        url = self.server()
        if not url:
            return
        v = self.view()
        cur = self.tree.currentItem()
        d = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self._search_gen += 1
        g = self._search_gen
        if v == "browse":
            if not d or d[0] != "dir":
                return
            self.cur_dir = d[1]
            self._update_crumbs(d[1])
            q = "/api/browse?path=" + urllib.parse.quote(d[1]) + ("&all=1" if self.allfiles.isChecked() else "")
            self.f.run(("br_browse", g), _get, url + q, 40)
            return
        self._update_crumbs("")
        qs = self._params()
        if v == "vfolders":
            if not d or d[0] != "vf":
                return
            qs["vfolder"] = d[1]
        elif v == "new":
            qs.update({"since": 7 * 86400, "order": "added"})
        elif v == "recent":
            self._fill(self._recent_rows())
            return
        elif v == "dup":
            self.f.run(("br_dup", g), _get, url + "/api/duplicates", 60)
            return
        elif v == "saved":
            if not d or d[0] != "saved":
                return
            qs.update(SETTINGS.asset_saved_searches[d[1]]["params"])
        self.f.run(("br_rows", g), _get, url + "/api/assets?" + urllib.parse.urlencode(qs), 40)

    def on_browse(self, g, res):
        if g != self._search_gen:
            return
        rows = [{"kind": "folder", "name": d["name"], "path": d["path"]} for d in res.get("dirs", [])]
        rows += res.get("files", [])
        q = self.q.text().strip().lower()
        if q:
            rows = [r for r in rows if q in r["name"].lower() or q in " ".join(r.get("tags") or []).lower()]
        k = self.kind.currentData()
        if k:
            rows = [r for r in rows if r["kind"] in (k, "folder")]
        if self.rating.currentData():
            rows = [r for r in rows if r["kind"] == "folder" or (r.get("rating") or 0) >= self.rating.currentData()]
        if self.label_cb.currentData():
            rows = [r for r in rows if r["kind"] == "folder" or r.get("label") == self.label_cb.currentData()]
        self._fill(rows)
        if res.get("pending") and self._pending_tries < 6:
            # まだ登録中の素材がある → 少し後に読み直して、長さ・サムネイルを付ける
            self._pending_tries += 1
            QTimer.singleShot(1500, lambda: self.load() if g == self._search_gen else None)
        else:
            self._pending_tries = 0

    def on_rows(self, g, rows):
        if g == self._search_gen:
            self._fill(rows)

    def on_dup(self, g, groups):
        if g != self._search_gen:
            return
        rows = []
        for grp in groups:
            for it in grp["items"]:
                it = dict(it)
                it["tags"] = json.loads(it.get("tags") or "[]") if isinstance(it.get("tags"), str) else it.get("tags")
                it["dup"] = len(grp["items"])
                rows.append(it)
        self._fill(rows, keep_order=True)

    def _sorted(self, rows):
        k = self.sort_cb.currentData()
        folders = [r for r in rows if r["kind"] == "folder"]
        files = [r for r in rows if r["kind"] != "folder"]
        key = {"name": lambda r: r["name"].lower(), "mtime": lambda r: -(r.get("mtime") or 0),
               "size": lambda r: -(r.get("size") or 0), "kind": lambda r: (r["kind"], r["name"].lower())}.get(k)
        return sorted(folders, key=lambda r: r["name"].lower()) + (sorted(files, key=key) if key else files)

    def _fill(self, rows, keep_order=False):
        self._gen += 1
        self.rows = rows if keep_order else self._sorted(rows)
        self.list.clear()
        self.detail.clear()
        self.detail.setSortingEnabled(False)
        used = self._used_paths()
        url = self.server()
        icons = {"folder": "📁", "video": "🎬", "audio": "🎵", "image": "🖼", "project": "📄", "other": "📄"}
        n_thumb = 0
        for i, r in enumerate(self.rows):
            local = to_local(r["path"], self.roots)
            r["local"] = local
            is_used = os.path.normcase(local) in used
            psd = r["name"].lower().endswith((".psd", ".psb"))
            stars = "★" * int(r.get("rating") or 0)
            mark = ("✓ " if is_used else "") + (f"[重複{r['dup']}] " if r.get("dup") else "")
            it = QListWidgetItem(f"{mark}{icons.get(r['kind'], '')}{' PSD' if psd else ''} {r['name']}"
                                 + (f"\n{stars}" if stars else ""))
            it.setData(Qt.ItemDataRole.UserRole, r)
            it.setToolTip(local + ("\n（このプロジェクトで使用中）" if is_used else ""))
            col = LABEL_COLOR.get(r.get("label") or "")
            if r.get("label"):
                it.setBackground(QColor(col).darker(180))
            if r.get("missing"):
                it.setForeground(QColor("#c55"))
            self.list.addItem(it)
            ti = QTreeWidgetItem([mark + r["name"], KIND_JP.get(r["kind"], r["kind"]), human_dur(r.get("duration")),
                                  f"{r['width']}×{r['height']}" if r.get("width") else "", human_size(r.get("size")) if r["kind"] != "folder" else "",
                                  time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])) if r.get("mtime") else "",
                                  stars, dict((k, l) for k, l, _c in LABELS).get(r.get("label") or "", ""), local])
            ti.setData(0, Qt.ItemDataRole.UserRole, r)
            if r.get("label"):
                ti.setForeground(7, QColor(col))
            self.detail.addTopLevelItem(ti)
            if r.get("id") and r["kind"] in ("video", "image") and n_thumb < 150:
                n_thumb += 1
                self.f.run(("thumb", self._gen, self.list, i), _get, f"{url}/api/assets/{r['id']}/thumb", 40, True)
        self.detail.setSortingEnabled(True)
        nf = sum(1 for r in self.rows if r["kind"] != "folder")
        nd = len(self.rows) - nf
        self.panel.msg.setText(f"ファイル {nf} 件" + (f"・フォルダ {nd}" if nd else "")
                               + ("（500 件まで表示）" if nf >= 500 else ""))

    def _used_paths(self) -> set:
        return {os.path.normcase(m.path) for m in self.mw.project.media}

    def _recent_rows(self) -> List[dict]:
        out = []
        for r in SETTINGS.asset_recent:
            ext = Path(r["path"]).suffix.lower()
            kind = "video" if ext in MD.VIDEO_EXT else ("audio" if ext in MD.AUDIO_EXT else "image")
            out.append({"name": Path(r["path"]).name, "path": r.get("server_path") or r["path"], "kind": kind,
                        "id": r.get("id"), "mtime": r.get("time"), "size": r.get("size")})
        return out

    # ── パンくず・移動 ──
    def _update_crumbs(self, server_path: str):
        while self.crumb_box.count():
            w_ = self.crumb_box.takeAt(0).widget()
            if w_:
                w_.deleteLater()
        if not server_path:
            self.path_edit.setText("")
            return
        local = to_local(server_path, self.roots)
        self.path_edit.setText(local)
        root = next((r for r in sorted(self.roots, key=len, reverse=True)
                     if server_path == r or server_path.startswith(r + "/")), "")
        if not root:
            return
        d = drive_for_root(root)
        parts = [(f"{root.rsplit('/', 1)[-1]}" + (f" ({d})" if d else ""), root)]
        rest = server_path[len(root):].strip("/")
        acc = root
        for p in [x for x in rest.split("/") if x]:
            acc += "/" + p
            parts.append((p, acc))
        for i, (lab, sp) in enumerate(parts):
            b = QToolButton()
            b.setText(lab + (" ›" if i < len(parts) - 1 else ""))
            b.setAutoRaise(True)
            b.clicked.connect(lambda _=False, sp=sp: self.go_to(sp))
            self.crumb_box.addWidget(b)

    def go_to(self, server_path: str):
        """実フォルダ表示で、そのフォルダを開く（木もたどって開く）。"""
        if self.view() != "browse":
            self.view_cb.setCurrentIndex(self.view_cb.findData("browse"))
        root = next((r for r in sorted(self.roots, key=len, reverse=True)
                     if server_path == r or server_path.startswith(r + "/")), None)
        if root is None:
            self.panel.msg.setText("サーバーの素材フォルダの外です")
            return
        top = next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[1] == root), None)
        if top is None:
            return
        rest = [x for x in server_path[len(root):].split("/") if x]
        self._walk_to(top, rest)

    def _walk_to(self, item, rest):
        if not rest:
            self.tree.setCurrentItem(item)
            return
        if item.childCount() == 1 and item.child(0).data(0, Qt.ItemDataRole.UserRole) is None:
            d = item.data(0, Qt.ItemDataRole.UserRole)
            res = _get(self.server() + "/api/browse?path=" + urllib.parse.quote(d[1]), 30)
            self.on_dirs(item, res)
        item.setExpanded(True)
        nxt = next((item.child(i) for i in range(item.childCount())
                    if (item.child(i).data(0, Qt.ItemDataRole.UserRole) or ("", ""))[1].rsplit("/", 1)[-1] == rest[0]), None)
        if nxt is None:
            self.tree.setCurrentItem(item)
            self.panel.msg.setText(f"フォルダが見つかりません: {rest[0]}")
            return
        self._walk_to(nxt, rest[1:])

    def jump_to_path(self):
        sp = from_local(self.path_edit.text(), self.roots)
        if sp is None:
            self.panel.msg.setText("このパスはサーバーの素材フォルダの外です（つないでいる共有の中のパスを入れてください）")
            return
        self.go_to(sp)

    def go_up(self):
        if self.view() == "browse" and self.cur_dir and self.cur_dir not in self.roots:
            self.go_to(self.cur_dir.rsplit("/", 1)[0])

    def open_row(self, r):
        if not r:
            return
        if r["kind"] == "folder":
            self.go_to(r["path"])
        else:
            self.import_rows([r])

    # ── 選択 ──
    def selected(self) -> List[dict]:
        if self.stack.currentIndex() == 0:
            return [it.data(Qt.ItemDataRole.UserRole) for it in self.list.selectedItems()]
        return [it.data(0, Qt.ItemDataRole.UserRole) for it in self.detail.selectedItems()]

    def current(self) -> Optional[dict]:
        return getattr(self, "_cur", None)

    def _mime(self, rows) -> QMimeData:
        md = QMimeData()
        urls = [QUrl.fromLocalFile(r["local"]) for r in rows if r and r["kind"] != "folder" and os.path.exists(r.get("local", ""))]
        md.setUrls(urls)
        return md

    # ── プレビュー・再生 ──
    def preview(self, r):
        self._cur = r
        self.player.stop()
        self.player.setSource(QUrl())
        self.in_ms = self.out_ms = -1
        self.io_lab.setText("")
        self.wave.set_peaks([])
        self.wave.setVisible(False)
        self.psd_btn.setVisible(False)
        self.notes.clear()
        if not r:
            return
        url = self.server()
        if r.get("id"):
            self.f.run(("br_notes", r["id"]), _get, f"{url}/api/assets/{r['id']}/notes", 20)
        local = r.get("local") or to_local(r["path"], self.roots)
        self._fill_meta(r)
        info = [f"<b>{r['name']}</b>", KIND_JP.get(r["kind"], r["kind"])]
        if r.get("width"):
            info.append(f"{r['width']}×{r['height']}")
        if r.get("duration"):
            info.append(human_dur(r["duration"]))
        if r["kind"] != "folder":
            info.append(human_size(r.get("size")))
        if r.get("mtime"):
            info.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(r["mtime"])))
        shown = local.replace("/", "/\u200b").replace("\\", "\\\u200b")
        info.append(f"<span style='color:#888'>{shown}</span>")
        self.info.setText("<br>".join(info))
        self._info_base = self.info.text()
        if r["kind"] in ("video", "audio"):
            src = QUrl.fromLocalFile(local) if os.path.exists(local) else (
                QUrl(f"{url}/api/assets/{r['id']}/file") if r.get("id") else QUrl())
            self.pv_stack.setCurrentIndex(1 if r["kind"] == "video" else 0)
            if r["kind"] == "audio":
                self.pv_img.setText("🎵")
            self.player.setSource(src)
            self.play_btn.setText("▶")
            if r.get("id"):
                self.f.run(("br_meta", r["id"]), _get, f"{url}/api/assets/{r['id']}/meta", 60)
                if r["kind"] == "audio" or (r.get("duration") or 0) < 3600:
                    self.f.run(("br_wave", r["id"]), _get, f"{url}/api/assets/{r['id']}/wave?bins=600", 120)
        elif r["kind"] == "image":
            self.pv_stack.setCurrentIndex(0)
            self.pv_img.setText("読み込み中…")
            self.psd_btn.setVisible(r["name"].lower().endswith((".psd", ".psb")))
            if r.get("id"):
                self._pv_id = r["id"]
                self.f.run(("br_pv", r["id"]), _get, f"{url}/api/assets/{r['id']}/preview?size=640", 60, True)
                self.f.run(("br_meta", r["id"]), _get, f"{url}/api/assets/{r['id']}/meta", 60)
            elif os.path.exists(local) and not local.lower().endswith((".psd", ".psb")):
                self._show_pixmap(QPixmap(local))
        else:
            self.pv_stack.setCurrentIndex(0)
            self.pv_img.setText("📁" if r["kind"] == "folder" else "（プレビューなし）")

    def _show_pixmap(self, pm: QPixmap):
        if pm.isNull():
            self.pv_img.setText("（プレビューなし）")
            return
        self.pv_img.setPixmap(pm.scaled(max(50, self.pv_img.width() - 4), max(50, self.pv_img.height() - 4),
                                        Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def on_preview(self, aid, data):
        if self._cur and self._cur.get("id") == aid:
            self._show_pixmap(QPixmap.fromImage(QImage.fromData(data)))

    def on_meta(self, aid, m):
        if not self._cur or self._cur.get("id") != aid or not m:
            return
        extra = []
        if m.get("video"):
            vv = m["video"]
            extra.append(f"映像: {vv['codec']} {vv['width']}×{vv['height']} {vv['fps']:.3g}fps {vv.get('pix_fmt', '')}")
        if m.get("audio"):
            a = m["audio"]
            extra.append(f"音声: {a['codec']} {a['rate']}Hz {a['channels']}ch")
        if m.get("bitrate"):
            extra.append(f"ビットレート: {m['bitrate'] / 1e6:.1f} Mbps")
        if m.get("image"):
            extra.append(f"{m['image']['format']} {m['image']['mode']}")
        if extra:
            self.info.setText(self._info_base + "<br><span style='color:#aaa'>" + "<br>".join(extra) + "</span>")

    def on_notes(self, aid, notes):
        if not self._cur or self._cur.get("id") != aid:
            return
        self.notes.clear()
        for n in notes or []:
            t = float(n.get("t") or 0)
            it = QListWidgetItem(f"{int(t // 60):02d}:{t % 60:05.2f}  {n.get('user') or '名無し'}: {n.get('text', '')}")
            it.setData(Qt.ItemDataRole.UserRole, t)
            it.setData(Qt.ItemDataRole.UserRole + 1, n.get("id"))
            self.notes.addItem(it)

    def add_note(self):
        r = self._cur
        text = self.note_in.text().strip()
        if not r or not r.get("id") or not text:
            return
        from .rendercluster import _req
        url, aid = self.server(), r["id"]
        t = self.player.position() / 1000.0 if r["kind"] in ("video", "audio") else 0.0
        user = SETTINGS.user_name
        self.note_in.clear()
        self.f.run(("br_notes", aid), lambda: (_req(f"{url}/api/assets/{aid}/notes", {"t": t, "user": user, "text": text}),
                                               _get(f"{url}/api/assets/{aid}/notes", 20))[1])

    def _note_menu(self, pos):
        it = self.notes.itemAt(pos)
        if not it or not self._cur:
            return
        m = QMenu(self)
        m.addAction("削除", lambda: self._delete_note(it.data(Qt.ItemDataRole.UserRole + 1)))
        m.exec(self.notes.mapToGlobal(pos))

    def _delete_note(self, nid):
        from .rendercluster import _req
        url, aid = self.server(), self._cur["id"]
        self.f.run(("br_notes", aid), lambda: (_req(f"{url}/api/notes/{nid}", None, method="DELETE"),
                                               _get(f"{url}/api/assets/{aid}/notes", 20))[1])

    def on_wave(self, aid, peaks):
        if self._cur and self._cur.get("id") == aid and peaks:
            self.wave.set_peaks(peaks)
            self.wave.setVisible(True)

    def _pos_changed(self, ms):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        dur = self.player.duration()
        self.time_lab.setText(f"{human_dur(ms / 1000) or '0:00'} / {human_dur(dur / 1000) or '0:00'}")
        if dur:
            self.wave.pos = ms / dur
            self.wave.update()
        if self.out_ms > 0 and ms >= self.out_ms and self.loop.isChecked():
            self.player.setPosition(max(0, self.in_ms))

    def _status_changed(self, st):
        from PyQt6.QtMultimedia import QMediaPlayer
        if st == QMediaPlayer.MediaStatus.EndOfMedia and self.loop.isChecked():
            self.player.setPosition(max(0, self.in_ms))
            self.player.play()
        elif st == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_btn.setText("▶")

    def play_pause(self):
        from PyQt6.QtMultimedia import QMediaPlayer
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("▶")
        else:
            self.player.setPlaybackRate(1.0)
            self.player.play()
            self.play_btn.setText("❚❚")

    def stop(self):
        self.player.pause()
        self.play_btn.setText("▶")

    def shuttle(self, d):
        rate = self.player.playbackRate()
        rate = (min(4.0, rate * 2) if rate >= 1 else 1.0) if d > 0 else 1.0
        if d < 0:
            self.player.setPosition(max(0, self.player.position() - 2000))      # 逆再生の代わりに 2 秒戻す
        self.player.setPlaybackRate(rate)
        self.player.play()
        self.play_btn.setText("❚❚")

    def step(self, d):
        self.player.pause()
        self.play_btn.setText("▶")
        fps = self.mw.project.sequence.fps or 30
        self.player.setPosition(max(0, self.player.position() + int(d * 1000 / fps)))

    def mark(self, which):
        ms = self.player.position()
        if which == "in":
            self.in_ms = ms
        else:
            self.out_ms = ms
        a = human_dur(self.in_ms / 1000) if self.in_ms >= 0 else "—"
        b = human_dur(self.out_ms / 1000) if self.out_ms >= 0 else "—"
        self.io_lab.setText(f"IN {a or '0:00'}  OUT {b or '0:00'}")

    # ── メタデータ（評価・ラベル・タグ・メモ）──
    def _fill_meta(self, r):
        self._rating = int(r.get("rating") or 0)
        for k, b in enumerate(self.stars, 1):
            b.setText("★" if k <= self._rating else "☆")
        self.lab_cb.setCurrentIndex(max(0, self.lab_cb.findData(r.get("label") or "")))
        self.tags.setText(", ".join(r.get("tags") or []))
        self.comment.setText(r.get("comment") or "")
        on = bool(r.get("id"))
        for w_ in self.stars + [self.lab_cb, self.tags, self.comment]:
            w_.setEnabled(on)

    def set_meta(self, **kw):
        rows = [r for r in self.selected() if r and r.get("id")] or ([self._cur] if self._cur and self._cur.get("id") else [])
        if not rows:
            return
        if "rating" in kw:
            self._rating = kw["rating"]
            for k, b in enumerate(self.stars, 1):
                b.setText("★" if k <= self._rating else "☆")
        for r in rows:
            r.update(kw)
            self._req(f"/api/assets/{r['id']}", kw, "PATCH", refresh=False)
        self.panel.msg.setText(f"{len(rows)} 件の情報を更新しました")

    # ── サーバーへの変更 ──
    def _req(self, path, data=None, method=None, refresh=True):
        from .rendercluster import _req
        url = self.server()
        self.f.run(("br_done", refresh), lambda: _req(url + path, data, method=method))

    def on_done(self, refresh):
        if refresh and self.view() == "vfolders":
            self.f.run(("br_vfolders",), _get, self.server() + "/api/vfolders", 30)
        elif refresh:
            self.load()

    def rescan(self, path, recursive=False):
        if not path:
            self.panel.msg.setText("実フォルダ表示でフォルダを選んでください")
            return
        self._req("/api/scan?path=" + urllib.parse.quote(path) + ("&recursive=1" if recursive else ""), {}, "POST")
        self.panel.msg.setText("読み直しています…（新しいファイルは少し後に一覧へ出ます）")
        self._pending_tries = 0
        QTimer.singleShot(2000, self.load)

    def new_vfolder(self, parent):
        name, ok = QInputDialog.getText(self, "マイフォルダ", "フォルダ名:")
        if ok and name.strip():
            self._req("/api/vfolders", {"name": name.strip(), "parent": parent})

    def rename_vfolder(self, fid):
        name, ok = QInputDialog.getText(self, "名前を変更", "フォルダ名:", text=self._vf.get(fid, {}).get("name", ""))
        if ok and name.strip():
            self._req(f"/api/vfolders/{fid}", {"name": name.strip()}, "PATCH")

    def delete_vfolder(self, fid):
        if QMessageBox.question(self, "マイフォルダ", f"「{self._vf.get(fid, {}).get('name', '')}」と中のフォルダを削除しますか？\n"
                                "（素材ファイルそのものは消えません）") == QMessageBox.StandardButton.Yes:
            self._req(f"/api/vfolders/{fid}", None, "DELETE")

    def add_to_vfolder(self, fid, rows, on=True):
        ids = [r["id"] for r in rows if r and r.get("id")]
        if ids:
            self._req(f"/api/vfolders/{fid}/items", {"asset_ids": ids, "on": on})
            self.panel.msg.setText(f"{len(ids)} 個を{'マイフォルダに追加' if on else 'マイフォルダから外'}しました")

    def save_search(self):
        name, ok = QInputDialog.getText(self, "検索を保存", "名前:", text=self.q.text().strip() or "保存した検索")
        if not ok or not name.strip():
            return
        params = {k: v for k, v in self._params().items() if k not in ("limit",)}
        SETTINGS.asset_saved_searches = [s for s in SETTINGS.asset_saved_searches if s["name"] != name.strip()] + \
            [{"name": name.strip(), "params": params}]
        SETTINGS.save()
        self.panel.msg.setText(f"「{name.strip()}」を保存しました（表示 →「保存した検索」）")

    def delete_saved(self, i):
        if 0 <= i < len(SETTINGS.asset_saved_searches):
            del SETTINGS.asset_saved_searches[i]
            SETTINGS.save()
            self.reload()

    # ── パス・外部 ──
    def copy_path(self, kind):
        r = self.current()
        if not r:
            return
        local = r.get("local") or to_local(r["path"], self.roots)
        txt = {"win": local, "unc": unc_path(local), "server": r["path"]}[kind]
        QApplication.clipboard().setText(txt)
        self.panel.msg.setText(f"コピーしました: {txt}")

    def reveal(self, r):
        if not r:
            return
        local = r.get("local") or to_local(r["path"], self.roots)
        if not os.path.exists(local):
            self.panel.msg.setText(f"この PC からは見えません: {local}")
            return
        import subprocess
        if r["kind"] == "folder" or os.path.isdir(local):
            subprocess.Popen(["explorer", local])
        else:
            subprocess.Popen(["explorer", "/select,", local])

    def open_external(self, r):
        if not r:
            return
        local = r.get("local") or to_local(r["path"], self.roots)
        if os.path.exists(local) and os.name == "nt":
            os.startfile(local)

    # ── 取り込み ──
    def import_selected(self, place=False, source=False):
        rows = [r for r in self.selected() if r and r["kind"] != "folder"] or \
               ([self._cur] if self._cur and self._cur["kind"] != "folder" else [])
        self.import_rows(rows, place, source)

    def import_rows(self, rows, place=False, source=False, marks=None):
        rows = [r for r in rows if r]
        if not rows:
            return
        for r in rows:
            SETTINGS.asset_recent = [x for x in SETTINGS.asset_recent if x.get("path") != r.get("local")][:199]
            SETTINGS.asset_recent.insert(0, {"path": r.get("local") or r["path"], "server_path": r["path"],
                                             "id": r.get("id"), "time": time.time(), "size": r.get("size")})
        SETTINGS.save()
        self.panel.msg.setText("取り込み中…")
        self.f.run(("br_import", place, source, marks), self.panel._fetch_local, rows)

    def on_import(self, place, source, marks, res):
        paths, credits = res
        seq = self.mw.project.sequence

        def after(items):
            for m in items:
                if m.path in credits:
                    m.credit = credits[m.path]
                if marks:
                    m.mark_in = int(marks[0] / 1000 * seq.fps)
                    m.mark_out = int(marks[1] / 1000 * seq.fps)
            if source and items:
                self.mw.load_source(items[0].id)
        self.mw.import_files(paths, place=(None, None, False) if place else None, after=after)
        self.panel.msg.setText(f"{len(paths)} 個を取り込みました" + ("（IN〜OUT の範囲）" if marks else ""))
        QTimer.singleShot(1500, lambda: self._fill(self.rows, keep_order=True))     # ✓ 使用中マークを更新

    def import_range(self):
        r = self._cur
        if not r or r["kind"] not in ("video", "audio"):
            return
        a = max(0, self.in_ms)
        b = self.out_ms if self.out_ms > a else self.player.duration()
        if b <= a:
            self.panel.msg.setText("IN / OUT を打ってください（I / O キー）")
            return
        self.import_rows([r], place=True, marks=(a, b))

    def import_folder(self, server_path):
        """フォルダの素材をまとめて取り込み、ビンもフォルダ名（下のフォルダは「親/子」）で作る。"""
        local = to_local(server_path, self.roots)
        if not os.path.isdir(local):
            self.panel.msg.setText(f"この PC からフォルダが見えません: {local}")
            return
        exts = MD.VIDEO_EXT | MD.AUDIO_EXT | MD.IMAGE_EXT
        groups: Dict[str, List[str]] = {}
        base = Path(local)
        n = 0
        for dp, _ds, fs in os.walk(local):
            files = [os.path.join(dp, f) for f in sorted(fs) if Path(f).suffix.lower() in exts and not f.startswith(".")]
            if files:
                rel = Path(dp).relative_to(base)
                name = base.name + ("" if str(rel) == "." else "/" + str(rel).replace("\\", "/"))
                groups[name] = files
                n += len(files)
            if n > 2000:
                break
        if not n:
            self.panel.msg.setText("取り込める素材がありません")
            return
        if n > 200 and QMessageBox.question(self, "フォルダごと取り込み", f"{n} 個の素材を取り込みます。よろしいですか？") \
                != QMessageBox.StandardButton.Yes:
            return
        for name, files in groups.items():
            self.mw.import_files(files, bin_id=self.mw.named_bin(name))
        self.panel.msg.setText(f"{n} 個を {len(groups)} 個のビンに取り込んでいます…")

    # ── 右クリック ──
    def _item_menu(self, pos):
        rows = [r for r in self.selected() if r]
        if not rows:
            return
        files = [r for r in rows if r["kind"] != "folder"]
        src = self.list if self.stack.currentIndex() == 0 else self.detail
        m = QMenu(self)
        if files:
            m.addAction("取り込む", lambda: self.import_rows(files))
            m.addAction("取り込んで再生ヘッドに配置", lambda: self.import_rows(files, place=True))
            m.addAction("ソースモニターで開く", lambda: self.import_rows(files[:1], source=True))
        if len(rows) == 1 and rows[0]["kind"] == "folder":
            m.addAction("開く", lambda: self.go_to(rows[0]["path"]))
            m.addAction("フォルダごと取り込み（ビンもフォルダ名で作る）", lambda: self.import_folder(rows[0]["path"]))
        if len(files) == 1 and files[0]["name"].lower().endswith((".psd", ".psb")):
            m.addAction("PSD を立ち絵（キャラクター）として登録", lambda: self.panel.register_psd_from_row(files[0]))
        m.addSeparator()
        if len(rows) == 1:
            self._cur = rows[0]
            pm = m.addMenu("パスをコピー")
            pm.addAction("Windows のパス", lambda: self.copy_path("win"))
            pm.addAction("ネットワークのパス", lambda: self.copy_path("unc"))
            pm.addAction("サーバー側のパス", lambda: self.copy_path("server"))
            m.addAction("エクスプローラーで表示", lambda: self.reveal(rows[0]))
            if rows[0]["kind"] != "folder":
                m.addAction("既定のアプリで開く", lambda: self.open_external(rows[0]))
        if any(r.get("id") for r in files):
            rm = m.addMenu("評価")
            for k in range(0, 6):
                rm.addAction("★" * k if k else "評価なし", lambda k=k: self.set_meta(rating=k))
            lm = m.addMenu("ラベル")
            for k, lab, _c in LABELS:
                lm.addAction(lab, lambda k=k: self.set_meta(label=k))
            vm = m.addMenu("マイフォルダに追加")
            if self._vf:
                for fid, r in self._vf.items():
                    vm.addAction(r["name"], lambda fid=fid: self.add_to_vfolder(fid, files))
            else:
                vm.addAction("（「マイフォルダ」表示でフォルダを作ってください）").setEnabled(False)
            cur = self.tree.currentItem()
            d = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
            if self.view() == "vfolders" and d and d[0] == "vf":
                m.addAction("このマイフォルダから外す", lambda: self.add_to_vfolder(d[1], files, on=False))
        m.exec(src.viewport().mapToGlobal(pos))
