"""アセットパネル: お気に入り素材 / アセットサーバー / Web 画像検索 から素材を取り込む。

- お気に入り: 全プロジェクト共通のお気に入り素材（Resolve のお気に入りビン）。ビンの素材を★で登録
- サーバー: 自宅アセットサーバー（assetserver.py）を検索・★・アップロード・取り込み
  サーバー上のパスがこの PC から見えれば（NAS・パス置換ルール）そのまま使い、見えなければダウンロード
- Web 画像: Openverse（CC ライセンスの画像検索）。作者とライセンスをクレジットに残し、概要欄に出せる
"""
from __future__ import annotations

import json
import os
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QObject, QSize, QThread, pyqtSignal
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QComboBox, QPushButton, QLabel,
                             QListWidget, QListWidgetItem, QTabWidget, QMenu, QFileDialog, QCheckBox)

from . import media as MD
from .settings import SETTINGS

UA = {"User-Agent": "TeloCut/0.5 (video editor)"}
OPENVERSE = "https://api.openverse.org/v1/images/"


def _get(url: str, timeout=15, raw=False):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        b = r.read()
    return b if raw else json.loads(b.decode("utf-8"))


def network_drives() -> List[str]:
    """この PC のネットワークドライブ（例 ["Z:", "Y:"]）。切断中のドライブで止まらないよう種類だけで調べる。"""
    if os.name != "nt":
        return []
    import ctypes
    out = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            d = f"{chr(65 + i)}:"
            if ctypes.windll.kernel32.GetDriveTypeW(d + "\\") == 4:      # DRIVE_REMOTE
                out.append(d)
    return out


def drive_remote(drive: str) -> str:
    """ネットワークドライブのつなぎ先（例 Z: → \\\\192.168.0.10\\ES_General）。"""
    if os.name != "nt":
        return ""
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.c_ulong(1024)
    try:
        if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(n)) == 0:
            return buf.value
    except Exception:
        pass
    return ""


def guess_local_path(server_path: str, roots: List[str]) -> Optional[str]:
    """サーバー側のパス（/mnt/nas/...）を、ネットワークドライブの同じ場所に読み替えてみる。
    見つかったらパス置換ルールに追加する（次からはすぐ見つかる）。"""
    sp = server_path.replace("\\", "/")
    for root in sorted(roots, key=len, reverse=True):
        r = root.rstrip("/")
        if not sp.startswith(r + "/"):
            continue
        rel = sp[len(r) + 1:].replace("/", "\\")
        share = r.rsplit("/", 1)[-1].lower()
        # その共有（例 ES_Edit）につないでいるドライブを先に調べる
        drives = sorted(network_drives(), key=lambda d: 0 if drive_remote(d).lower().rstrip("\\").endswith("\\" + share) else 1)
        for d in drives:
            cand = f"{d}\\{rel}"
            if os.path.exists(cand):
                if not any(m[0].replace("\\", "/").rstrip("/") == r for m in SETTINGS.path_maps):
                    SETTINGS.path_maps = list(SETTINGS.path_maps) + [[r, d]]
                    SETTINGS.save()
                return cand
    return None


def _user() -> str:
    return SETTINGS.user_name or os.environ.get("USERNAME") or "user"


# ─────────────────────────────────────────────
#  お気に入り（ローカル・全プロジェクト共通）
# ─────────────────────────────────────────────
def is_favorite(path: str) -> bool:
    return any(f.get("path") == path for f in SETTINGS.favorites)


def set_favorite(path: str, on: bool, name: str = "", kind: str = "", credit: str = ""):
    SETTINGS.favorites = [f for f in SETTINGS.favorites if f.get("path") != path]
    if on:
        SETTINGS.favorites.insert(0, {"path": path, "name": name or Path(path).name, "kind": kind, "credit": credit})
    SETTINGS.save()


# ─────────────────────────────────────────────
#  Openverse
# ─────────────────────────────────────────────
def openverse_search(q: str, page: int = 1, commercial: bool = True, size: int = 20) -> List[dict]:
    params = {"q": q, "page": page, "page_size": min(size, 20)}   # 匿名利用は 20 件まで
    if commercial:
        params["license_type"] = "commercial"     # YouTube 収益化を考えて商用利用可だけ
    d = _get(OPENVERSE + "?" + urllib.parse.urlencode(params))
    out = []
    for r in d.get("results", []):
        lic = (r.get("license") or "").upper()
        lic = "CC0" if lic == "CC0" else ("Public Domain" if lic == "PDM" else f"CC {lic} {r.get('license_version') or ''}".strip())
        out.append({"title": r.get("title") or "image", "url": r.get("url"), "thumb": r.get("thumbnail") or r.get("url"),
                    "creator": r.get("creator") or "", "license": lic, "page": r.get("foreign_landing_url") or "",
                    "source": r.get("source") or "", "width": r.get("width"), "height": r.get("height")})
    return out


def credit_line(r: dict) -> str:
    who = f"{r['creator']} / " if r.get("creator") else ""
    return f"「{r['title']}」 {who}{r['license']} {r.get('page', '')}".strip()


class _Fetcher(QObject):
    """一覧やサムネイルを別スレッドで取ってくる。サムネイルは同時に 4 つまで（サーバーを混ませない）。"""
    done = pyqtSignal(object, object)        # tag, result
    error = pyqtSignal(object, str)
    _thumb_sem = threading.Semaphore(4)

    def run(self, tag, fn, *args):
        is_thumb = isinstance(tag, tuple) and tag and tag[0] == "thumb"

        def work():
            if is_thumb:
                self._thumb_sem.acquire()
            try:
                self.done.emit(tag, fn(*args))
            except Exception as ex:
                if not is_thumb:                 # サムネイルが取れないだけならエラーは出さない
                    self.error.emit(tag, str(ex))
            finally:
                if is_thumb:
                    self._thumb_sem.release()
        threading.Thread(target=work, daemon=True).start()


def _icon(data: bytes) -> QIcon:
    img = QImage.fromData(data)
    return QIcon(QPixmap.fromImage(img)) if not img.isNull() else QIcon()


from PyQt6.QtWidgets import QTreeWidget as _QTreeWidget   # noqa: E402


class _FolderTree(_QTreeWidget):
    """マイフォルダ表示のとき、素材の一覧からドラッグされたものをフォルダに追加する。"""

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

    def _target(self, e):
        it = self.itemAt(e.position().toPoint())
        d = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        return d[1] if d and d[0] == "vf" else None

    def dragEnterEvent(self, e):
        if e.source() is self.panel.srv_list and self.panel._view() == "vfolders":
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.source() is self.panel.srv_list and self._target(e):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        fid = self._target(e)
        if fid:
            assets = [it.data(Qt.ItemDataRole.UserRole) for it in self.panel.srv_list.selectedItems()]
            self.panel.add_to_vfolder(fid, assets)
            e.acceptProposedAction()


class AssetPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.f = _Fetcher()
        self.f.done.connect(self._fetched)
        self.f.error.connect(self._error)
        self._gen = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)
        self.tabs.addTab(self._fav_tab(), "★ お気に入り")
        self.tabs.addTab(self._server_tab(), "サーバー")
        self.tabs.addTab(self._web_tab(), "Web 画像")
        self.refresh_favorites()

    # ── 共通 ──────────────────────────────
    def _error(self, tag, e: str):
        if "timed out" in e or "10060" in e or "Connection refused" in e or "10061" in e:
            self.msg.setText(f"サーバーに接続できません（{self._server() or 'URL 未設定'}）。"
                             "サーバー PC / VM が動いているか、URL とポート番号を確認してください")
        else:
            self.msg.setText(f"エラー: {e}")

    def _list(self) -> QListWidget:
        lw = QListWidget()
        lw.setViewMode(QListWidget.ViewMode.IconMode)
        lw.setIconSize(QSize(120, 72))
        lw.setGridSize(QSize(132, 100))
        lw.setResizeMode(QListWidget.ResizeMode.Adjust)
        lw.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        lw.setWordWrap(True)
        lw.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        return lw

    def _fetched(self, tag, res):
        kind = tag[0]
        if kind == "thumb":
            _, gen, lw, row = tag
            if isinstance(gen, tuple):            # プレビュー / コマ送り
                self._show_preview(gen[1], gen[0], row, res)
            elif gen == self._gen and row < lw.count():
                lw.item(row).setIcon(_icon(res))
        elif kind == "server":
            if len(tag) > 1 and tag[1] != getattr(self, "_search_gen", 0):
                return                             # 後から別の検索をした
            self._show_server(res)
        elif kind == "info":
            self._server_roots = list(res.get("roots") or [])
        elif kind == "folders":
            self._show_folders(tag[1], res)
        elif kind == "vfolders":
            self._show_vfolders(res)
        elif kind == "vfdone":
            if tag[1] == "refresh_list":
                self.server_search()
            self.f.run(("vfolders",), _get, f"{self._server()}/api/vfolders", 30) if self._view() == "vfolders" \
                else self.f.run(("vfolders_quiet",), _get, f"{self._server()}/api/vfolders", 30)
        elif kind == "vfolders_quiet":
            self._vf = {r["id"]: r for r in res}
        elif kind == "web":
            self._show_web(res)
        elif kind == "import":
            paths, credits = res
            self._import(paths, credits, place=bool(tag[1]) if len(tag) > 1 else False)
        elif kind == "character":
            self.mw.begin_edit()
            self.mw.project.characters = [c for c in self.mw.project.characters if c.get("id") != res["id"]] + [res]
            self.mw.end_edit(f"立ち絵を登録: {res['name']}")
            self.msg.setText(f"立ち絵「{res['name']}」を登録しました（セリフパネル・台本から生成で選べます）")
            if hasattr(self.mw, "voice_panel"):
                self.mw.voice_panel.refresh()

    def _import(self, paths: List[str], credits: Optional[dict] = None, place: bool = False):
        credits = credits or {}

        def after(items):
            for m in items:
                if m.path in credits:
                    m.credit = credits[m.path]
        self.mw.import_files(paths, place=(None, None, False) if place else None, after=after)
        self.msg.setText(f"{len(paths)} 個を取り込みました")

    # ── お気に入り ────────────────────────
    def _fav_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.fav_q = QLineEdit()
        self.fav_q.setPlaceholderText("お気に入りを絞り込み")
        self.fav_q.textChanged.connect(self.refresh_favorites)
        add = QPushButton("＋ 選択中の素材")
        add.setToolTip("ビンで選んでいる素材をお気に入りに追加")
        add.clicked.connect(self.add_selected_favorites)
        addf = QPushButton("＋ ファイル")
        addf.clicked.connect(self._add_fav_files)
        row.addWidget(self.fav_q, 1)
        row.addWidget(add)
        row.addWidget(addf)
        v.addLayout(row)
        self.fav_list = self._list()
        self.fav_list.itemDoubleClicked.connect(lambda it: self._import([it.data(Qt.ItemDataRole.UserRole)["path"]],
                                                                        {it.data(Qt.ItemDataRole.UserRole)["path"]: it.data(Qt.ItemDataRole.UserRole).get("credit", "")}))
        self.fav_list.customContextMenuRequested.connect(self._fav_menu)
        v.addWidget(self.fav_list, 1)
        return w

    def refresh_favorites(self):
        q = self.fav_q.text().strip().lower() if hasattr(self, "fav_q") else ""
        self.fav_list.clear()
        for f in SETTINGS.favorites:
            if q and q not in (f.get("name", "") + f.get("path", "")).lower():
                continue
            it = QListWidgetItem(f.get("name") or Path(f["path"]).name)
            it.setData(Qt.ItemDataRole.UserRole, f)
            it.setToolTip(f["path"] + ("\n" + f["credit"] if f.get("credit") else ""))
            if not os.path.exists(f["path"]):
                it.setForeground(Qt.GlobalColor.red)
            else:
                ext = Path(f["path"]).suffix.lower()
                if ext in MD.IMAGE_EXT and ext != ".psd":
                    it.setIcon(QIcon(QPixmap(f["path"]).scaled(120, 72, Qt.AspectRatioMode.KeepAspectRatio)))
            self.fav_list.addItem(it)

    def add_selected_favorites(self):
        ids = self.mw.bin.selected_ids()
        for mid in ids:
            m = self.mw.project.media_by_id(mid)
            if m:
                set_favorite(m.path, True, m.name, m.kind, m.credit)
        self.refresh_favorites()
        self.msg.setText(f"{len(ids)} 個をお気に入りに追加しました" if ids else "ビンで素材を選んでください")

    def _add_fav_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "お気に入りに追加")
        for p in paths:
            set_favorite(p, True)
        self.refresh_favorites()

    def _fav_menu(self, pos):
        items = self.fav_list.selectedItems()
        if not items:
            return
        fs = [it.data(Qt.ItemDataRole.UserRole) for it in items]
        m = QMenu(self)
        m.addAction("取り込む", lambda: self._import([f["path"] for f in fs], {f["path"]: f.get("credit", "") for f in fs}))
        m.addAction("取り込んで再生ヘッドに配置", lambda: self._import([f["path"] for f in fs], {f["path"]: f.get("credit", "") for f in fs}, place=True))
        m.addAction("フォルダを開く", lambda: os.startfile(os.path.dirname(fs[0]["path"])) if os.name == "nt" else None)
        m.addSeparator()
        m.addAction("お気に入りから外す", lambda: ([set_favorite(f["path"], False) for f in fs], self.refresh_favorites()))
        m.exec(self.fav_list.mapToGlobal(pos))

    # ── サーバー ──────────────────────────
    # 表示: 「フォルダ（NAS と同じ構成）」「マイフォルダ（独自の構成・サーバーで共有）」「検索」
    #   左: フォルダの木 / 中: 素材（サムネイル）/ 右: プレビュー（動画はコマ送り・PSD は大きく）
    VIEWS = [("folders", "フォルダ（NAS と同じ構成）"), ("vfolders", "マイフォルダ（独自の構成）"), ("search", "検索（すべて）")]

    def _server_tab(self) -> QWidget:
        from PyQt6.QtWidgets import QSplitter, QTreeWidget
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        r1 = QHBoxLayout()
        self.srv = QLineEdit(SETTINGS.asset_server)
        self.srv.setPlaceholderText("http://192.168.0.100:8770（アセットサーバー）")
        self.srv.returnPressed.connect(self.server_reload)
        con = QPushButton("接続")
        con.clicked.connect(self.server_reload)
        r1.addWidget(self.srv, 1)
        r1.addWidget(con)
        r2 = QHBoxLayout()
        self.srv_view = QComboBox()
        for k, lab in self.VIEWS:
            self.srv_view.addItem(lab, k)
        self.srv_view.currentIndexChanged.connect(lambda _i: self.server_reload())
        self.srv_q = QLineEdit()
        self.srv_q.setPlaceholderText("絞り込み（名前・タグ・コメント）")
        self.srv_q.returnPressed.connect(self.server_search)
        self.srv_kind = QComboBox()
        for lab, k in (("すべて", ""), ("動画", "video"), ("画像・PSD", "image"), ("音声", "audio")):
            self.srv_kind.addItem(lab, k)
        self.srv_kind.currentIndexChanged.connect(lambda _i: self.server_search())
        self.srv_sub = QCheckBox("下のフォルダも")
        self.srv_sub.setToolTip("選んだフォルダの中のフォルダの素材もまとめて表示")
        self.srv_sub.toggled.connect(lambda _v: self.server_search())
        self.srv_fav = QCheckBox("★のみ")
        self.srv_fav.toggled.connect(lambda _v: self.server_search())
        up = QPushButton("アップロード")
        up.setToolTip("ビンで選んでいる素材をサーバーに送る")
        up.clicked.connect(self.server_upload)
        for x in (self.srv_view, self.srv_q, self.srv_kind, self.srv_sub, self.srv_fav, up):
            r2.addWidget(x, 1 if x is self.srv_q else 0)
        v.addLayout(r1)
        v.addLayout(r2)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.srv_tree = _FolderTree(self)
        self.srv_tree.setHeaderHidden(True)
        self.srv_tree.itemExpanded.connect(self._tree_expand)
        self.srv_tree.currentItemChanged.connect(lambda _c, _p: self.server_search())
        self.srv_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.srv_tree.customContextMenuRequested.connect(self._tree_menu)
        split.addWidget(self.srv_tree)
        self.srv_list = self._list()
        self.srv_list.setDragEnabled(True)
        self.srv_list.itemDoubleClicked.connect(lambda it: self.server_import([it]))
        self.srv_list.customContextMenuRequested.connect(self._srv_menu)
        self.srv_list.currentItemChanged.connect(lambda cur, _p: self._preview(cur))
        split.addWidget(self.srv_list)
        split.addWidget(self._preview_pane())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)          # 余った幅は素材の一覧に
        split.setStretchFactor(2, 0)
        split.setSizes([200, 420, 240])
        v.addWidget(split, 1)
        return w

    def _preview_pane(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(200)
        w.setMaximumWidth(360)
        v = QVBoxLayout(w)
        v.setContentsMargins(4, 0, 0, 0)
        self.pv_img = QLabel("素材を選ぶと\nプレビュー")
        self.pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pv_img.setMinimumSize(200, 120)
        self.pv_img.setStyleSheet("background: #111; border: 1px solid #333;")
        v.addWidget(self.pv_img)
        self.pv_strip = QHBoxLayout()
        self.pv_frames = []
        for _ in range(5):
            lab = QLabel()
            lab.setFixedSize(46, 26)
            lab.setStyleSheet("background: #111;")
            self.pv_frames.append(lab)
            self.pv_strip.addWidget(lab)
        v.addLayout(self.pv_strip)
        self.pv_info = QLabel("")
        self.pv_info.setWordWrap(True)
        self.pv_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.pv_info)
        row = QHBoxLayout()
        b1 = QPushButton("取り込む")
        b1.clicked.connect(lambda: self.server_import(self.srv_list.selectedItems()))
        b2 = QPushButton("配置")
        b2.setToolTip("取り込んで再生ヘッドの位置に置く")
        b2.clicked.connect(lambda: self.server_import(self.srv_list.selectedItems(), place=True))
        row.addWidget(b1)
        row.addWidget(b2)
        v.addLayout(row)
        self.pv_char = QPushButton("PSD を立ち絵（キャラクター）として登録")
        self.pv_char.clicked.connect(self.register_psd_character)
        self.pv_char.setVisible(False)
        v.addWidget(self.pv_char)
        v.addStretch(1)
        return w

    def _server(self) -> str:
        url = self.srv.text().strip().rstrip("/")
        if url and not url.startswith("http"):
            url = "http://" + url
        if url != SETTINGS.asset_server:
            SETTINGS.asset_server = url
            SETTINGS.save()
        return url

    def _view(self) -> str:
        return self.srv_view.currentData()

    # ── 木（フォルダ / マイフォルダ）──
    def server_reload(self):
        """表示の種類に合わせて左の木を作り直す。"""
        url = self._server()
        if not url:
            self.msg.setText("アセットサーバーの URL を入れてください")
            return
        self.srv_tree.clear()
        self.f.run(("info",), _get, f"{url}/api/info", 20)
        view = self._view()
        self.srv_tree.setVisible(view != "search")
        self.srv_sub.setVisible(view == "folders")
        if view == "folders":
            self.f.run(("folders", None), _get, f"{url}/api/folders", 30)
        elif view == "vfolders":
            self.f.run(("vfolders",), _get, f"{url}/api/vfolders", 30)
        else:
            self.server_search()

    def _folder_item(self, f: dict):
        from PyQt6.QtWidgets import QTreeWidgetItem
        it = QTreeWidgetItem([f"{f['name']}（{f['count']}）"])
        it.setData(0, Qt.ItemDataRole.UserRole, ("dir", f["path"]))
        it.setToolTip(0, f["path"])
        if f.get("has_children"):
            it.addChild(QTreeWidgetItem(["…"]))          # 開いたときに読み込む
        return it

    def _show_folders(self, parent_item, res: dict):
        if parent_item is None:
            self.srv_tree.clear()
            for f in res["folders"]:
                self.srv_tree.addTopLevelItem(self._folder_item(f))
            if self.srv_tree.topLevelItemCount():
                first = self.srv_tree.topLevelItem(0)
                first.setExpanded(True)
                self.srv_tree.setCurrentItem(first)
            return
        parent_item.takeChildren()
        for f in res["folders"]:
            parent_item.addChild(self._folder_item(f))

    def _tree_expand(self, item):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if not d or d[0] != "dir":
            return
        if item.childCount() == 1 and item.child(0).data(0, Qt.ItemDataRole.UserRole) is None:
            self.f.run(("folders", item), _get, f"{self._server()}/api/folders?parent=" + urllib.parse.quote(d[1]), 30)

    def _show_vfolders(self, rows: list):
        from PyQt6.QtWidgets import QTreeWidgetItem
        cur = self.srv_tree.currentItem()
        keep = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        self.srv_tree.clear()
        self._vf = {r["id"]: r for r in rows}
        items = {}
        for r in rows:
            it = QTreeWidgetItem([f"📁 {r['name']}（{r['count']}）"])
            it.setData(0, Qt.ItemDataRole.UserRole, ("vf", r["id"]))
            items[r["id"]] = it
        for r in rows:
            par = items.get(r.get("parent") or "")
            (par.addChild(items[r["id"]]) if par is not None else self.srv_tree.addTopLevelItem(items[r["id"]]))
        self.srv_tree.expandAll()
        if not rows:
            hint = QTreeWidgetItem(["（右クリック →「新しいフォルダ」）"])
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self.srv_tree.addTopLevelItem(hint)
        target = items.get(keep[1]) if keep and keep[0] == "vf" else None
        self.srv_tree.setCurrentItem(target or (self.srv_tree.topLevelItem(0) if rows else None))
        if not rows:
            self.srv_list.clear()

    def _vf_req(self, path: str, data=None, method=None, then=None):
        from .rendercluster import _req
        url = self._server()

        def work():
            r = _req(url + path, data, method=method)
            return r
        self.f.run(("vfdone", then), work)

    def _tree_menu(self, pos):
        if self._view() != "vfolders":
            return
        it = self.srv_tree.itemAt(pos)
        d = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        fid = d[1] if d and d[0] == "vf" else ""
        m = QMenu(self)
        m.addAction("新しいフォルダ", lambda: self._new_vfolder(""))
        if fid:
            m.addAction("この中に新しいフォルダ", lambda: self._new_vfolder(fid))
            m.addAction("名前を変更", lambda: self._rename_vfolder(fid))
            m.addAction("いちばん上に移動", lambda: self._vf_req(f"/api/vfolders/{fid}", {"parent": ""}, "PATCH"))
            m.addSeparator()
            m.addAction("削除（素材ファイルは消えません）", lambda: self._delete_vfolder(fid))
        m.exec(self.srv_tree.mapToGlobal(pos))

    def _new_vfolder(self, parent: str):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "マイフォルダ", "フォルダ名:")
        if ok and name.strip():
            self._vf_req("/api/vfolders", {"name": name.strip(), "parent": parent})

    def _rename_vfolder(self, fid: str):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "名前を変更", "フォルダ名:", text=self._vf.get(fid, {}).get("name", ""))
        if ok and name.strip():
            self._vf_req(f"/api/vfolders/{fid}", {"name": name.strip()}, "PATCH")

    def _delete_vfolder(self, fid: str):
        from PyQt6.QtWidgets import QMessageBox
        if QMessageBox.question(self, "マイフォルダ", f"「{self._vf.get(fid, {}).get('name', '')}」と中のフォルダを削除しますか？\n"
                                "（素材ファイルそのものは消えません）") == QMessageBox.StandardButton.Yes:
            self._vf_req(f"/api/vfolders/{fid}", None, "DELETE")

    def add_to_vfolder(self, fid: str, assets: list, on: bool = True):
        ids = [a["id"] for a in assets]
        if ids:
            self._vf_req(f"/api/vfolders/{fid}/items", {"asset_ids": ids, "on": on},
                         then="refresh_list" if not on else None)
            self.msg.setText(f"{len(ids)} 個を{'マイフォルダに追加' if on else 'マイフォルダから外'}しました")

    # ── 一覧 ──
    def server_search(self):
        url = self._server()
        if not url:
            return
        qs = {"q": self.srv_q.text().strip(), "kind": self.srv_kind.currentData() or "", "limit": 500, "order": "name"}
        if self.srv_fav.isChecked():
            qs["fav_user"] = _user()
        view = self._view()
        cur = self.srv_tree.currentItem()
        d = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        if view == "folders":
            if not d or d[0] != "dir":
                return
            qs["under" if self.srv_sub.isChecked() or qs["q"] else "dir"] = d[1]
        elif view == "vfolders":
            if not d or d[0] != "vf":
                return
            qs["vfolder"] = d[1]
        else:
            qs["order"] = "added"
        self.msg.setText("読み込み中…")
        self._search_gen = getattr(self, "_search_gen", 0) + 1     # 古い検索の結果が後から届いても使わない
        self.f.run(("server", self._search_gen), _get, f"{url}/api/assets?" + urllib.parse.urlencode(qs), 40)

    def _show_server(self, rows: list):
        self._gen += 1
        self.srv_list.clear()
        url = self._server()
        icons = {"video": "🎬", "audio": "🎵", "image": "🖼", "project": "📄"}
        for i, a in enumerate(rows):
            psd = a["name"].lower().endswith((".psd", ".psb"))
            it = QListWidgetItem(f"{icons.get(a['kind'], '')}{' PSD' if psd else ''} {a['name']}")
            it.setData(Qt.ItemDataRole.UserRole, a)
            dur = f" {a['duration']:.1f}秒" if a.get("duration") else ""
            it.setToolTip(f"{a['path']}\n{a['kind']} {a.get('width') or ''}×{a.get('height') or ''}{dur}")
            self.srv_list.addItem(it)
            if a["kind"] in ("video", "image") and i < 120:      # 先頭 120 件だけサムネイル
                self.f.run(("thumb", self._gen, self.srv_list, i), _get, f"{url}/api/assets/{a['id']}/thumb", 40, True)
        self.msg.setText(f"{len(rows)} 件" + ("（500 件まで表示）" if len(rows) >= 500 else ""))

    # ── プレビュー ──
    def _preview(self, it):
        self._pv_gen = getattr(self, "_pv_gen", 0) + 1
        for lab in self.pv_frames:
            lab.clear()
        if it is None:
            return
        a = it.data(Qt.ItemDataRole.UserRole)
        url = self._server()
        size = a.get("size") or 0
        info = [f"<b>{a['name']}</b>", {"video": "動画", "audio": "音声", "image": "画像"}.get(a["kind"], a["kind"])]
        if a.get("width"):
            info.append(f"{a['width']}×{a['height']}")
        if a.get("duration"):
            s = int(a["duration"])
            info.append(f"{s // 60}:{s % 60:02d}")
        info.append(f"{size / 1024 / 1024:.1f} MB" if size > 1024 * 1024 else f"{size // 1024} KB")
        local = SETTINGS.map_path(a["path"])
        shown = (local if os.path.exists(local) else a["path"]).replace("/", "/\u200b").replace("\\", "\\\u200b")
        info.append(f"<span style='color:#888'>{shown}</span>")       # 区切りで折り返せるように
        self.pv_info.setText("<br>".join(info))
        psd = a["name"].lower().endswith((".psd", ".psb"))
        self.pv_char.setVisible(psd)
        self.pv_img.setText("読み込み中…")
        if a["kind"] in ("video", "image"):
            self.f.run(("thumb", ("pv", self._pv_gen), None, 0), _get, f"{url}/api/assets/{a['id']}/preview?size=640", 60, True)
        else:
            self.pv_img.setText("🎵" if a["kind"] == "audio" else "（プレビューなし）")
        for lab in self.pv_frames:
            lab.setVisible(a["kind"] == "video")
        if a["kind"] == "video" and a.get("duration"):
            for k in range(5):
                t = a["duration"] * (k + 0.5) / 5
                self.f.run(("thumb", ("strip", self._pv_gen), None, k), _get,
                           f"{url}/api/assets/{a['id']}/thumb?t={t:.1f}&size=160", 60, True)

    def _show_preview(self, gen, kind, k, data: bytes):
        if gen != getattr(self, "_pv_gen", 0):
            return
        pm = QPixmap.fromImage(QImage.fromData(data))
        if pm.isNull():
            return
        if kind == "pv":
            self.pv_img.setPixmap(pm.scaled(self.pv_img.width() - 4, self.pv_img.height() - 4,
                                            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.pv_frames[k].setPixmap(pm.scaled(46, 26, Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation))

    def _local_path(self, a: dict) -> Optional[str]:
        """サーバーの素材がこの PC から直接見えるならそのパス。
        パス置換ルールが無くても、ネットワークドライブ（Z: など）に同じファイルがあれば見つけてルールを自動で覚える。"""
        for p in (a["path"], SETTINGS.map_path(a["path"])):
            if p and os.path.exists(p):
                return p
        return guess_local_path(a["path"], getattr(self, "_server_roots", None) or ["/mnt/nas"])

    def _fetch_local(self, rows: list):
        """素材をこの PC で使える場所に（直接見えなければダウンロード）。戻り値: (パス, クレジット)"""
        from .integrations import work_dir
        url = self._server()
        out, credits = [], {}
        d = work_dir(self.mw, "ServerAssets")
        for a in rows:
            p = self._local_path(a)
            if p is None:
                p = str(d / a["name"])
                if not os.path.exists(p) or os.path.getsize(p) != a.get("size"):
                    data = _get(f"{url}/api/assets/{a['id']}/file", 600, True)
                    Path(p).write_bytes(data)
            out.append(p)
            if a.get("credit"):
                credits[p] = a["credit"]
        return out, credits

    def server_import(self, items, place=False):
        rows = [it.data(Qt.ItemDataRole.UserRole) for it in items]
        if not rows:
            return
        self.msg.setText("取り込み中…")
        self.f.run(("import", place), self._fetch_local, rows)

    def register_psd_character(self):
        """選んだ PSD を立ち絵（キャラクター）として登録（口・目のレイヤーを自動で判定）。"""
        it = self.srv_list.currentItem()
        if it is None:
            return
        a = it.data(Qt.ItemDataRole.UserRole)
        self.msg.setText("PSD を読み込んでいます…")

        def work():
            from .yukkuri import create_character_from_psd
            paths, _c = self._fetch_local([a])
            return create_character_from_psd(Path(a["name"]).stem, paths[0])
        self.f.run(("character",), work)

    def _srv_menu(self, pos):
        items = self.srv_list.selectedItems()
        if not items:
            return
        url = self._server()
        assets = [it.data(Qt.ItemDataRole.UserRole) for it in items]
        m = QMenu(self)
        m.addAction("取り込む", lambda: self.server_import(items))
        m.addAction("取り込んで再生ヘッドに配置", lambda: self.server_import(items, place=True))
        if len(assets) == 1 and assets[0]["name"].lower().endswith((".psd", ".psb")):
            m.addAction("PSD を立ち絵（キャラクター）として登録", self.register_psd_character)
        vf = getattr(self, "_vf", {})
        sub = m.addMenu("マイフォルダに追加")
        if vf:
            for fid, r in vf.items():
                sub.addAction(r["name"], lambda fid=fid: self.add_to_vfolder(fid, assets))
        else:
            sub.addAction("（「マイフォルダ」表示でフォルダを作ってください）").setEnabled(False)
        cur = self.srv_tree.currentItem()
        d = cur.data(0, Qt.ItemDataRole.UserRole) if cur else None
        if self._view() == "vfolders" and d and d[0] == "vf":
            m.addAction("このマイフォルダから外す", lambda: self.add_to_vfolder(d[1], assets, on=False))
        if len(assets) == 1:
            lp = SETTINGS.map_path(assets[0]["path"])
            if os.path.exists(lp) and os.name == "nt":
                m.addAction("エクスプローラーで表示", lambda: __import__("subprocess").Popen(["explorer", "/select,", lp]))

        def fav(on):
            from .rendercluster import _req
            for a in assets:
                _req(url + "/api/favorites", {"user": _user(), "asset_id": a["id"], "on": on})
            self.msg.setText("★を更新しました")
        m.addSeparator()
        m.addAction("★ サーバーのお気に入りに追加", lambda: fav(True))
        m.addAction("★ を外す", lambda: fav(False))
        m.exec(self.srv_list.mapToGlobal(pos))

    def server_upload(self):
        url = self._server()
        ids = self.mw.bin.selected_ids()
        ms = [m for m in (self.mw.project.media_by_id(i) for i in ids) if m and os.path.exists(m.path)]
        if not url or not ms:
            self.msg.setText("サーバー URL と、ビンで送る素材を選んでください")
            return

        def send():
            from .rendercluster import _req
            for m in ms:
                with open(m.path, "rb") as fp:
                    r = _req(f"{url}/api/upload?name={urllib.parse.quote(Path(m.path).name)}", fp.read(), timeout=600)
                if m.tags or m.credit:
                    _req(f"{url}/api/assets/{r['id']}", {"tags": m.tags, "credit": m.credit}, method="PATCH")
            return []
        self.msg.setText(f"{len(ms)} 個をアップロード中…")
        self.f.run(("server",), lambda: (send(), _get(f"{url}/api/assets?limit=300"))[1])

    # ── Web 画像（Openverse） ─────────────
    def _web_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        r = QHBoxLayout()
        self.web_q = QLineEdit()
        self.web_q.setPlaceholderText("画像を検索（英語がよく当たる。例: mountain sunset）")
        self.web_q.returnPressed.connect(self.web_search)
        self.web_comm = QCheckBox("商用利用可のみ")
        self.web_comm.setChecked(True)
        go = QPushButton("検索")
        go.clicked.connect(self.web_search)
        r.addWidget(self.web_q, 1)
        r.addWidget(self.web_comm)
        r.addWidget(go)
        v.addLayout(r)
        self.web_list = self._list()
        self.web_list.itemDoubleClicked.connect(lambda it: self.web_import([it]))
        self.web_list.customContextMenuRequested.connect(self._web_menu)
        v.addWidget(self.web_list, 1)
        note = QLabel("Openverse（CC ライセンス画像）。取り込むと作者・ライセンスがクレジットに入り、概要欄の書き出しに使われます。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888")
        v.addWidget(note)
        return w

    def web_search(self):
        q = self.web_q.text().strip()
        if q:
            self.msg.setText("検索中…")
            self.f.run(("web",), openverse_search, q, 1, self.web_comm.isChecked())

    def _show_web(self, rows: list):
        self._gen += 1
        self.web_list.clear()
        for i, r in enumerate(rows):
            it = QListWidgetItem(r["title"][:30])
            it.setData(Qt.ItemDataRole.UserRole, r)
            it.setToolTip(credit_line(r) + (f"\n{r['width']}×{r['height']}" if r.get("width") else ""))
            self.web_list.addItem(it)
            self.f.run(("thumb", self._gen, self.web_list, i), _get, r["thumb"], 15, True)
        self.msg.setText(f"{len(rows)} 件")

    def web_import(self, items, place=False):
        rows = [it.data(Qt.ItemDataRole.UserRole) for it in items]

        def fetch():
            import re
            from .integrations import work_dir
            d = work_dir(self.mw, "Web画像")
            out, credits = [], {}
            for r in rows:
                data = _get(r["url"], 60, True)
                ext = Path(urllib.parse.urlparse(r["url"]).path).suffix.lower()
                if ext not in MD.IMAGE_EXT:
                    ext = ".jpg"
                name = re.sub(r"[^\w\-]+", "_", r["title"])[:40].strip("_") or "image"
                p = d / f"{name}{ext}"
                k = 1
                while p.exists():
                    p = d / f"{name}_{k}{ext}"
                    k += 1
                p.write_bytes(data)
                out.append(str(p))
                credits[str(p)] = credit_line(r)
            return out, credits
        self.msg.setText("ダウンロード中…")
        self.f.run(("import",), fetch)

    def _web_menu(self, pos):
        items = self.web_list.selectedItems()
        if not items:
            return
        m = QMenu(self)
        m.addAction("取り込む", lambda: self.web_import(items))
        r = items[0].data(Qt.ItemDataRole.UserRole)
        if r.get("page"):
            import webbrowser
            m.addAction("元のページを開く", lambda: webbrowser.open(r["page"]))
        m.exec(self.web_list.mapToGlobal(pos))
