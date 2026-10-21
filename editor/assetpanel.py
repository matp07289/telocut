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


def download(url: str, dst: str, progress=None, cancel=None, timeout: float = 600) -> str:
    """少しずつファイルに書く（大きな動画でもメモリを使わない・途中で切れたら消す）。progress(済み, 全体)。"""
    tmp = dst + ".part"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("中止しました")
                buf = r.read(1 << 20)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)
        os.replace(tmp, dst)
        return dst
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


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
        from .assetbrowser import ServerBrowser
        self.browser = ServerBrowser(self)
        self.tabs.addTab(self.browser, "サーバー")
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
            cur = self.browser._gen if lw is self.browser.list else self._gen
            if gen == cur and row < lw.count():
                lw.item(row).setIcon(_icon(res))
        elif kind.startswith("br_"):
            b = self.browser
            {"br_info": lambda: b.on_info(res), "br_stats": lambda: b.on_stats(res),
             "br_dirs": lambda: b.on_dirs(tag[1], res), "br_vfolders": lambda: b.on_vfolders(res),
             "br_browse": lambda: b.on_browse(tag[1], res), "br_rows": lambda: b.on_rows(tag[1], res),
             "br_dup": lambda: b.on_dup(tag[1], res), "br_pv": lambda: b.on_preview(tag[1], res),
             "br_meta": lambda: b.on_meta(tag[1], res), "br_wave": lambda: b.on_wave(tag[1], res),
             "br_notes": lambda: b.on_notes(tag[1], res),
             "br_done": lambda: b.on_done(tag[1]),
             "br_import": lambda: b.on_import(tag[1], tag[2], tag[3], res)}[kind]()
        elif kind == "info":
            self._server_roots = list(res.get("roots") or [])
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

    # ── 木（フォルダ / マイフォルダ）──
    # ── 一覧 ──
    # ── プレビュー ──
    def _server(self) -> str:
        return self.browser.server()

    def register_psd_from_row(self, row):
        """PSD を立ち絵（キャラクター）として登録（口・目のレイヤーを自動で判定）。"""
        if not row:
            return
        self.msg.setText("PSD を読み込んでいます…")

        def work():
            from . import yukkuri
            paths, _c = self._fetch_local([row])
            return yukkuri.create_character_from_psd(Path(row["name"]).stem, paths[0])
        self.f.run(("character",), work)

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
                    download(f"{url}/api/assets/{a['id']}/file", p)
            out.append(p)
            if a.get("credit"):
                credits[p] = a["credit"]
        return out, credits

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
        self.f.run(("br_done", True), send)

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
