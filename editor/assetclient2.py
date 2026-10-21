"""TeloCut Assets の機能（AssetClientWindow のミックスイン）。

変換: 変換を頼む・フォルダごと変換・アップロードして変換・変換ジョブ（中止・やり直し・結果を表示・終わったら知らせる）・レンダーノード
YouTube: 素材 / この PC のファイルをアップロード（ひな形・予約公開・サムネイル・再生リスト・続きから送る）・履歴
送る / 受け取る: 進み具合つきのアップロード（中止できる・アップロード先のフォルダ）・クリップボードの画像を送る・まとめて zip で受け取る
整理: まとめてタグ / 評価 / ラベル・共有リンクをコピー・一覧を CSV に
見張る: 新着の通知・監視フォルダの自動アップロード・タスクトレイ・接続の状態と自動再接続
使いやすさ: 接続先を複数登録・前回のフォルダで開く・ショートカット・TeloCut を起動・容量の内訳・最近の操作
"""
from __future__ import annotations

import csv
import datetime
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QProgressBar,
                             QPushButton, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout,
                             QAbstractItemView)

from .settings import SETTINGS


class _Sig(QObject):
    progress = pyqtSignal(str, int, int)        # 何を, 済み, 全体
    done = pyqtSignal(str, object)
    failed = pyqtSignal(str, str)
    bg = pyqtSignal(str, object)                 # 見張り（接続・新着）の結果


def _req(url: str, data=None, method=None, timeout=30):
    from .rendercluster import _req as r
    return r(url, data, method, timeout)


def upload_file(server: str, path: str, folder: str = "", progress=None, cancel: Optional[threading.Event] = None) -> dict:
    """1 つのファイルを少しずつ送る（大きな動画でもメモリを使わない）。戻り値: サーバーの素材。"""
    u = urllib.parse.urlparse(server)
    q = f"/api/upload?name={urllib.parse.quote(Path(path).name)}" + (f"&folder={urllib.parse.quote(folder)}" if folder else "")
    size = os.path.getsize(path)
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.hostname, u.port or 80, timeout=600)
    try:
        conn.putrequest("POST", q)
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-TeloCut", "1")
        conn.endheaders()
        sent = 0
        with open(path, "rb") as fp:
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("中止しました")
                buf = fp.read(1 << 20)
                if not buf:
                    break
                conn.send(buf)
                sent += len(buf)
                if progress:
                    progress(sent, size)
        r = conn.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError(f"サーバーが受け取りませんでした（{r.status}）: {body[:200].decode(errors='replace')}")
        return json.loads(body.decode() or "{}")
    finally:
        conn.close()


class ClientFeatures:
    # ─────────────────────────── はじめに ───────────────────────────
    def _init_features(self):
        self.sig = _Sig()
        self.sig.progress.connect(self._on_progress)
        self.sig.done.connect(self._on_done)
        self.sig.failed.connect(self._on_failed)
        self.sig.bg.connect(self._on_bg)
        self._cancel: Optional[threading.Event] = None
        # 変換ジョブのタブ
        from .assetjobs import JobsPanel
        self.jobs = JobsPanel(self.server, lambda: SETTINGS.user_name or "")
        self.jobs.finished.connect(self._job_finished)
        self.jobs.open_result.connect(self.open_job_result)
        self.panel.tabs.addTab(self.jobs, "変換ジョブ")
        # ステータスバー: 進み具合・中止・接続
        self.bar = QProgressBar()
        self.bar.setMaximumWidth(220)
        self.bar.setVisible(False)
        self.stop_btn = QToolButton()
        self.stop_btn.setText("中止")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.cancel_transfer)
        self.conn_label = QLabel("")
        self.conn_label.setObjectName("info")
        for w in (self.bar, self.stop_btn, self.conn_label):
            self.statusBar().addPermanentWidget(w)
        self._build_extra_toolbar()
        self._build_extra_menus()
        self._shortcuts()
        self._init_tray()
        self._was_online = None
        self._conn_timer = QTimer(self)
        self._conn_timer.setInterval(30_000)
        self._conn_timer.timeout.connect(self.check_connection)
        self._conn_timer.start()
        self._new_since = time.time()
        self._new_timer = QTimer(self)
        self._new_timer.setInterval(60_000)
        self._new_timer.timeout.connect(self.check_new)
        self._new_timer.start()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(10_000)
        self._watch_timer.timeout.connect(self.check_watch_folder)
        self._watch_timer.start()
        QTimer.singleShot(1500, self.restore_place)
        QTimer.singleShot(800, self.check_connection)

    def server(self) -> str:
        return (SETTINGS.asset_server or "").rstrip("/")

    def selected_rows(self) -> List[dict]:
        return [r for r in self.browser.selected() if r and r.get("kind") != "folder"]

    def _need(self, rows, what: str) -> bool:
        if not self.server():
            self.panel.msg.setText("アセットサーバーの URL を入れて「接続」してください")
            return False
        if not rows:
            self.panel.msg.setText(f"{what}素材を選んでください")
            return False
        return True

    # ─────────────────────────── 画面の部品 ───────────────────────────
    def _build_extra_toolbar(self):
        tb = self.addToolBar("サーバー")
        tb.setObjectName("server_toolbar")
        tb.setMovable(False)
        tb.addWidget(QLabel(" 接続先: "))
        self.profile_cb = QComboBox()
        self.profile_cb.setMinimumWidth(160)
        self.profile_cb.setToolTip("登録した接続先（家・事務所など）を切り替える")
        self.profile_cb.activated.connect(lambda _i: self.switch_profile(self.profile_cb.currentData()))
        tb.addWidget(self.profile_cb)
        add = QToolButton()
        add.setText("＋")
        add.setToolTip("今の接続先を登録")
        add.clicked.connect(self.add_profile)
        tb.addWidget(add)
        self._fill_profiles()
        tb.addSeparator()
        tb.addWidget(QLabel(" アップロード先: "))
        self.up_folder = QLineEdit(str(self.qs.value("upload_folder", "") or ""))
        self.up_folder.setPlaceholderText("（アップロードの直下）例: 2026-10/撮影")
        self.up_folder.setMaximumWidth(220)
        self.up_folder.editingFinished.connect(lambda: self.qs.setValue("upload_folder", self.up_folder.text().strip()))
        tb.addWidget(self.up_folder)
        tb.addSeparator()
        for txt, tip, fn in (("🔁 変換", "選んだ素材の変換をサーバーに頼む（Ctrl+T）", self.transcode_selected),
                             ("▶ YouTube", "選んだ動画を YouTube にアップロード（Ctrl+Y）", self.youtube_selected),
                             ("⬇ zip", "選んだ素材をまとめて zip で受け取る（Ctrl+Shift+D）", self.download_zip),
                             ("🎬 TeloCut", "TeloCut を起動", self.launch_telocut)):
            b = QToolButton()
            b.setText(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tb.addWidget(b)

    def _build_extra_menus(self):
        mb = self.menuBar()
        first = mb.actions()[1] if len(mb.actions()) > 1 else None
        m = QMenu("素材", self)
        mb.insertMenu(first, m) if first else mb.addMenu(m)
        for txt, key, fn in (("変換を頼む…", "Ctrl+T", self.transcode_selected),
                             ("フォルダごと変換…", "", self.transcode_folder),
                             ("アップロードして変換…", "", self.upload_and_transcode),
                             (None, None, None),
                             ("YouTube にアップロード…", "Ctrl+Y", self.youtube_selected),
                             ("この PC の動画を YouTube にアップロード…", "", self.youtube_local),
                             ("YouTube アップロード履歴", "", self.youtube_history),
                             (None, None, None),
                             ("まとめて zip で受け取る", "Ctrl+Shift+D", self.download_zip),
                             ("クリップボードの画像をアップロード", "", self.paste_upload),
                             (None, None, None),
                             ("まとめてタグを付ける…", "", self.bulk_tags),
                             ("まとめて評価・ラベル…", "", self.bulk_rating),
                             ("共有リンクをコピー", "Ctrl+L", self.copy_links),
                             ("一覧を CSV に書き出す…", "", self.export_csv)):
            if txt is None:
                m.addSeparator()
                continue
            a = m.addAction(txt, fn)
            if key:
                a.setShortcut(QKeySequence(key))
        s = QMenu("サーバー", self)
        mb.insertMenu(first, s) if first else mb.addMenu(s)
        s.addAction("変換ジョブ", lambda: self.panel.tabs.setCurrentWidget(self.jobs))
        s.addAction("レンダーノード", self.show_nodes)
        s.addAction("容量の内訳", self.show_stats)
        s.addAction("最近の操作", self.show_activity)
        s.addSeparator()
        s.addAction("接続先を登録…", self.add_profile)
        s.addAction("接続先を削除…", self.remove_profile)
        s.addSeparator()
        self.watch_act = s.addAction("監視フォルダ（自動アップロード）…", self.set_watch_folder)
        self.notify_act = QAction("新着を知らせる", self, checkable=True)
        self.notify_act.setChecked(str(self.qs.value("notify_new", "1")) == "1")
        self.notify_act.toggled.connect(lambda on: self.qs.setValue("notify_new", "1" if on else "0"))
        s.addAction(self.notify_act)
        self.tray_act = QAction("閉じてもタスクトレイに残る", self, checkable=True)
        self.tray_act.setChecked(str(self.qs.value("tray", "0")) == "1")
        self.tray_act.toggled.connect(lambda on: self.qs.setValue("tray", "1" if on else "0"))
        s.addAction(self.tray_act)

    def _shortcuts(self):
        for key, fn in (("Ctrl+F", lambda: (self.panel.tabs.setCurrentWidget(self.browser), self.browser.q.setFocus())),
                        ("F5", lambda: self.browser.load()), ("Ctrl+U", lambda: self.panel.server_upload()),
                        ("Ctrl+V", self.paste_upload)):
            QShortcut(QKeySequence(key), self, activated=fn)

    def extra_item_menu(self, m: QMenu, rows: List[dict]):
        """素材の右クリックに足す（変換・YouTube・共有リンク・まとめて…）。"""
        files = [r for r in rows if r.get("kind") != "folder"]
        folders = [r for r in rows if r.get("kind") == "folder"]
        m.addSeparator()
        if files:
            m.addAction("変換を頼む…", self.transcode_selected)
            if any(r.get("kind") == "video" for r in files):
                m.addAction("YouTube にアップロード…", self.youtube_selected)
            m.addAction("まとめて zip で受け取る", self.download_zip)
            m.addAction("共有リンクをコピー", self.copy_links)
            m.addAction("まとめてタグを付ける…", self.bulk_tags)
            m.addAction("まとめて評価・ラベル…", self.bulk_rating)
        if len(folders) == 1:
            m.addAction("フォルダごと変換…", lambda: self.transcode_folder(folders[0]["path"]))

    # ─────────────────────────── 送る / 受け取る ───────────────────────────
    def _start(self, tag: str, fn, *a):
        if self._cancel is not None:
            self.panel.msg.setText("ほかの送受信が終わるまで待ってください")
            return False
        self._cancel = threading.Event()
        self.bar.setVisible(True)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.stop_btn.setVisible(True)

        def work():
            try:
                self.sig.done.emit(tag, fn(*a))
            except Exception as ex:
                self.sig.failed.emit(tag, str(ex))
        threading.Thread(target=work, daemon=True).start()
        return True

    def _end_transfer(self):
        self._cancel = None
        self.bar.setVisible(False)
        self.stop_btn.setVisible(False)

    def cancel_transfer(self):
        if self._cancel is not None:
            self._cancel.set()
            self.panel.msg.setText("中止しています…")

    def _on_progress(self, what: str, a: int, b: int):
        self.bar.setValue(int(a * 1000 / max(1, b)) if b else 0)
        self.bar.setFormat(f"{what} {a / 1e6:.1f}/{b / 1e6:.1f} MB" if b else f"{what} {a / 1e6:.1f} MB")

    def upload_files(self, paths: List[str], then: str = "") -> bool:
        """ファイルをアップロード（進み具合・中止・アップロード先のフォルダ）。then="transcode" なら終わったら変換の画面。"""
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths or not self.server():
            if not self.server():
                self.panel.msg.setText("アセットサーバーの URL を入れて「接続」してください")
            return False
        folder = self.up_folder.text().strip()
        server = self.server()
        total = sum(os.path.getsize(p) for p in paths)

        def work():
            out, base = [], 0
            for p in paths:
                out.append(upload_file(server, p, folder, lambda a, _b, base=base: self.sig.progress.emit("送信", base + a, total),
                                       self._cancel))
                base += os.path.getsize(p)
            return out
        self.panel.msg.setText(f"{len(paths)} 個をアップロード中…" + (f"（{folder}）" if folder else ""))
        return self._start("upload:" + then, work)

    def download_zip(self) -> bool:
        rows = self.selected_rows()
        if not self._need(rows, "受け取る"):
            return False
        name = f"assets_{datetime.datetime.now():%Y%m%d_%H%M%S}"
        url = f"{self.server()}/api/zip?ids={','.join(r['id'] for r in rows)}&name={urllib.parse.quote(name)}"
        dst = os.path.join(self.download_dir(), name + ".zip")
        from .assetpanel import download
        self.panel.msg.setText(f"{len(rows)} 個を zip で受け取り中…")
        return self._start("zip", download, url, dst, lambda a, b: self.sig.progress.emit("受信", a, b), self._cancel_ev_proxy())

    def _cancel_ev_proxy(self):
        """_start の前に作る中止の合図（_start がすぐ同じものを使う）。"""
        class _P:
            def __init__(s, w):
                s.w = w

            def is_set(s):
                return s.w._cancel is not None and s.w._cancel.is_set()
        return _P(self)

    def paste_upload(self) -> Optional[str]:
        """クリップボードの画像（スクリーンショットなど）を PNG にしてアップロード。"""
        img = QApplication.clipboard().image()
        if img.isNull():
            self.panel.msg.setText("クリップボードに画像がありません")
            return None
        d = Path(self.download_dir()) / "clipboard"
        d.mkdir(parents=True, exist_ok=True)
        p = str(d / f"clip_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
        img.save(p)
        self.upload_files([p])
        return p

    def _on_done(self, tag: str, res):
        self._end_transfer()
        if tag.startswith("upload"):
            ids = [r.get("id") for r in res or [] if r and r.get("id")]
            self.panel.msg.setText(f"{len(ids)} 個をアップロードしました")
            self.notify("アップロード完了", f"{len(ids)} 個をサーバーに送りました")
            self.browser.on_done(True)
            if tag == "upload:transcode" and ids:
                self.transcode_ids(ids, [r.get("name", "") for r in res])
        elif tag == "zip":
            self.panel.msg.setText(f"受け取りました: {res}")
            self.notify("受け取り完了", os.path.basename(str(res)))
        elif tag == "yt_fetch":
            self._open_yt_dialog(*res)

    def _on_failed(self, tag: str, err: str):
        self._end_transfer()
        self.panel.msg.setText(("中止しました" if "中止" in err else f"失敗しました: {err}"))

    # ─────────────────────────── 変換 ───────────────────────────
    def transcode_selected(self):
        rows = [r for r in self.selected_rows() if r.get("kind") in ("video", "audio")]
        if not self._need(rows, "変換する動画・音声の"):
            return None
        return self.transcode_ids([r["id"] for r in rows], [r["name"] for r in rows])

    def transcode_ids(self, ids: List[str], names: List[str]):
        from .assetjobs import TranscodeDialog
        dlg = TranscodeDialog(self, self.server(), ids, names, SETTINGS.user_name or "")
        self._tc_dialog = dlg
        if dlg.exec():
            self.jobs.track(dlg.jobs)
            self.panel.msg.setText(f"{len(dlg.jobs)} 件の変換を頼みました（「変換ジョブ」タブで進み具合）")
        return dlg

    def transcode_folder(self, server_path: str = ""):
        """フォルダの中の動画・音声をまとめて変換（下のフォルダも）。"""
        path = server_path or self.browser.cur_dir
        if not path or not self.server():
            self.panel.msg.setText("実フォルダ表示でフォルダを開いてください")
            return None
        from .assetpanel import _get
        rows = [r for r in _get(f"{self.server()}/api/assets?under={urllib.parse.quote(path)}&limit=2000", 30)
                if r.get("kind") in ("video", "audio")]
        if not rows:
            self.panel.msg.setText("このフォルダに動画・音声がありません")
            return None
        return self.transcode_ids([r["id"] for r in rows], [r["name"] for r in rows])

    def upload_and_transcode(self, paths: Optional[List[str]] = None) -> bool:
        if paths is None:
            paths, _ = QFileDialog.getOpenFileNames(self, "アップロードして変換するファイル", "", "動画・音声 (*.mp4 *.mov *.mxf *.mkv *.avi *.wav *.mp3 *.m4a);;すべて (*)")
        return self.upload_files(list(paths or []), then="transcode")

    def _job_finished(self, j: dict):
        st = {"done": "変換が終わりました", "error": "変換に失敗しました", "cancelled": "変換を中止しました"}.get(j["status"], "")
        self.notify(st, f"{j.get('name', '')}（{j.get('preset_label', '')}）")
        self.panel.msg.setText(f"{st}: {j.get('name', '')}" + (f" — {j.get('error')}" if j.get("error") else ""))

    def open_job_result(self, j: dict):
        """変換の結果を開く（サーバーのフォルダへ移動・この PC から見えるならエクスプローラーでも）。"""
        out = (j.get("out_path") or "").replace("\\", "/")         # Windows のサーバーは \ で返す
        if not out:
            return None
        from .assetbrowser import to_local
        self.panel.tabs.setCurrentWidget(self.browser)
        self.browser.go_to(out.rsplit("/", 1)[0])
        local = to_local(out, self.browser.roots)
        if local and os.path.exists(local):
            self._reveal(local)
        return out

    def show_nodes(self):
        from .assetjobs import NodesDialog
        if self.server():
            NodesDialog(self, self.server()).exec()

    # ─────────────────────────── YouTube ───────────────────────────
    def youtube_selected(self):
        rows = [r for r in self.selected_rows() if r.get("kind") == "video"]
        if not rows:
            return self.youtube_local()
        r = rows[0]
        self.panel.msg.setText("動画を用意しています…")
        return self._start("yt_fetch", lambda: (self.panel._fetch_local([r])[0][0], r))

    def youtube_local(self):
        p, _ = QFileDialog.getOpenFileName(self, "YouTube にアップロードする動画", self.download_dir(),
                                           "動画 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v)")
        if p:
            self._open_yt_dialog(p, {"name": os.path.basename(p), "tags": []})

    def _open_yt_dialog(self, path: str, row: dict):
        from .ytdialog import YouTubeUploadDialog
        dlg = YouTubeUploadDialog(self, path, Path(row.get("name", path)).stem, tags=row.get("tags") or [])
        self._yt_dialog = dlg
        dlg.show()
        return dlg

    def youtube_history(self):
        from .ytdialog import YouTubeHistoryDialog
        YouTubeHistoryDialog(self).exec()

    # ─────────────────────────── 整理 ───────────────────────────
    def bulk_tags(self, tags: Optional[str] = None) -> int:
        rows = self.selected_rows()
        if not self._need(rows, "タグを付ける"):
            return 0
        if tags is None:
            tags, ok = QInputDialog.getText(self, "まとめてタグを付ける", "付けるタグ（, で区切る。先頭に - を付けると外す）:")
            if not ok:
                return 0
        add = [t.strip() for t in tags.replace("、", ",").split(",") if t.strip() and not t.strip().startswith("-")]
        rem = [t.strip()[1:] for t in tags.replace("、", ",").split(",") if t.strip().startswith("-")]
        r = _req(self.server() + "/api/assets/bulk", {"ids": [x["id"] for x in rows], "set": {"tags_add": add, "tags_remove": rem}})
        self.browser.on_done(True)
        self.panel.msg.setText(f"{len(rows)} 個のタグを変えました")
        return int((r or {}).get("updated", len(rows)) if isinstance(r, dict) else len(rows))

    def bulk_rating(self, rating: Optional[int] = None, label: Optional[str] = None) -> int:
        rows = self.selected_rows()
        if not self._need(rows, "評価する"):
            return 0
        if rating is None and label is None:
            from .assetbrowser import LABELS
            dlg = QDialog(self)
            dlg.setWindowTitle("まとめて評価・ラベル")
            f = QFormLayout(dlg)
            rc = QComboBox()
            rc.addItem("（変えない）", -1)
            for i in range(6):
                rc.addItem("★" * i if i else "なし", i)
            lc = QComboBox()
            lc.addItem("（変えない）", None)
            for k, lab, _c in LABELS:
                lc.addItem(lab, k)
            f.addRow("評価", rc)
            f.addRow("ラベル", lc)
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            f.addRow(bb)
            if not dlg.exec():
                return 0
            rating = rc.currentData() if rc.currentData() >= 0 else None
            label = lc.currentData()
        st = {}
        if rating is not None:
            st["rating"] = int(rating)
        if label is not None:
            st["label"] = label
        if not st:
            return 0
        _req(self.server() + "/api/assets/bulk", {"ids": [x["id"] for x in rows], "set": st})
        self.browser.on_done(True)
        self.panel.msg.setText(f"{len(rows)} 個の評価・ラベルを変えました")
        return len(rows)

    def copy_links(self) -> str:
        """ブラウザ版でその素材を開くリンク（チャットで「この素材」と伝えるときに）。"""
        rows = self.selected_rows()
        if not self._need(rows, "リンクをコピーする"):
            return ""
        txt = "\n".join(f"{r['name']}  {self.server()}/#v=search&id={r['id']}" for r in rows)
        QApplication.clipboard().setText(txt)
        self.panel.msg.setText(f"{len(rows)} 個のリンクをコピーしました")
        return txt

    def export_csv(self, path: str = "") -> str:
        rows = [r for r in self.browser.rows if r]
        if not rows:
            self.panel.msg.setText("一覧が空です")
            return ""
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "一覧を CSV に", os.path.join(self.download_dir(), "assets.csv"), "CSV (*.csv)")
        if not path:
            return ""
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["名前", "種類", "長さ(秒)", "幅", "高さ", "サイズ(byte)", "評価", "ラベル", "タグ", "パス"])
            for r in rows:
                w.writerow([r.get("name"), r.get("kind"), r.get("duration") or "", r.get("width") or "", r.get("height") or "",
                            r.get("size") or "", r.get("rating") or "", r.get("label") or "", ",".join(r.get("tags") or []),
                            r.get("path")])
        self.panel.msg.setText(f"{len(rows)} 件を書き出しました: {path}")
        return path

    # ─────────────────────────── 見張る ───────────────────────────
    def _init_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("TeloCut Assets")
        m = QMenu()
        m.addAction("開く", self.show_normal)
        m.addAction("アップロード…", lambda: self.panel.server_upload())
        m.addSeparator()
        m.addAction("終了", self.quit_app)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda reason: self.show_normal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def show_normal(self):
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.instance().quit()

    def notify(self, title: str, text: str):
        self.statusBar().showMessage(f"{title}: {text}", 8000)
        if self.tray is not None and (not self.isVisible() or self.isMinimized() or not self.isActiveWindow()):
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 6000)
        else:
            QApplication.beep()

    def tray_close(self, e) -> bool:
        """閉じる → タスクトレイに残す設定なら隠すだけ（True = 閉じない）。"""
        if self.tray is not None and self.tray_act.isChecked() and not getattr(self, "_quitting", False):
            self.hide()
            self.tray.showMessage("TeloCut Assets", "タスクトレイで動いています（右クリック → 終了）", QSystemTrayIcon.MessageIcon.Information, 3000)
            e.ignore()
            return True
        return False

    def _bg(self, tag: str, fn):
        threading.Thread(target=lambda: self.sig.bg.emit(tag, fn()), daemon=True).start()

    def _on_bg(self, tag: str, res):
        if tag == "conn":
            self.apply_connection(res)
        elif tag == "new":
            self.apply_new(res)

    def ping(self) -> bool:
        try:
            _req(self.server() + "/api/info", timeout=5)
            return True
        except Exception:
            return False

    def check_connection(self):
        """30 秒ごとにサーバーを確かめる（裏で）。切れていたら表示、戻ったら自動でつなぎ直す。"""
        if not self.server():
            self.conn_label.setText("○ 未接続")
            return
        self._bg("conn", self.ping)

    def apply_connection(self, ok: bool) -> bool:
        url = self.server()
        self.conn_label.setText("● 接続中" if ok else "✕ サーバーにつながりません")
        self.conn_label.setStyleSheet("" if ok else "color: #ff6b5b;")
        if ok and self._was_online is False:
            self.browser.connect_server()
            self.notify("サーバーに再接続しました", url)
        self._was_online = ok
        return ok

    def fetch_new(self) -> List[dict]:
        """前に確かめたあとにサーバーに来た素材（since は「何秒前から」）。"""
        since = self._new_since
        self._new_since = time.time()
        try:
            from .assetpanel import _get
            rows = _get(f"{self.server()}/api/assets?since={max(1, int(time.time() - since) + 1)}&limit=200", 15)
        except Exception:
            return []
        return [r for r in rows or [] if float(r.get("added") or 0) >= since - 1]

    def check_new(self):
        if self.server() and self.notify_act.isChecked():
            self._bg("new", self.fetch_new)

    def apply_new(self, rows: List[dict]) -> int:
        """新着を知らせる。"""
        if rows:
            names = "、".join(r["name"] for r in rows[:3]) + ("…" if len(rows) > 3 else "")
            self.notify(f"新しい素材 {len(rows)} 個", names)
        return len(rows)

    def set_watch_folder(self):
        d = QFileDialog.getExistingDirectory(self, "監視フォルダ（新しいファイルを自動でアップロード。キャンセル = やめる）",
                                             str(self.qs.value("watch_dir", "") or ""))
        self.qs.setValue("watch_dir", d or "")
        self.qs.setValue("watch_seen", json.dumps(self._watch_snapshot(d)) if d else "[]")
        self.panel.msg.setText(f"監視フォルダ: {d}（新しく入ったファイルを自動でアップロード）" if d else "監視フォルダをやめました")

    def _watch_snapshot(self, d: str) -> List[str]:
        out = []
        for dp, _ds, fs in os.walk(d):
            for f in fs:
                if not f.startswith(".") and not f.endswith((".part", ".tmp", ".crdownload")):
                    p = os.path.join(dp, f)
                    try:
                        out.append(f"{p}|{os.path.getsize(p)}")
                    except OSError:
                        pass
        return out

    def check_watch_folder(self) -> List[str]:
        """監視フォルダに新しく入ったファイル（大きさが 10 秒変わらないもの）をアップロード。"""
        d = str(self.qs.value("watch_dir", "") or "")
        if not d or not os.path.isdir(d) or self._cancel is not None or not self.server():
            return []
        try:
            seen = set(json.loads(self.qs.value("watch_seen", "[]") or "[]"))
        except ValueError:
            seen = set()
        now = self._watch_snapshot(d)
        prev = getattr(self, "_watch_prev", set())
        self._watch_prev = set(now)
        new = [x for x in now if x not in seen and x in prev]       # 前回と同じ大きさ = 書き込みが終わった
        if not new:
            return []
        self.qs.setValue("watch_seen", json.dumps(sorted(seen | set(new))))
        paths = [x.rsplit("|", 1)[0] for x in new]
        self.upload_files(paths)
        return paths

    # ─────────────────────────── 使いやすさ ───────────────────────────
    def _profiles(self) -> List[dict]:
        try:
            return json.loads(self.qs.value("profiles", "[]") or "[]")
        except ValueError:
            return []

    def _fill_profiles(self):
        self.profile_cb.blockSignals(True)
        self.profile_cb.clear()
        rows = self._profiles()
        if not rows and SETTINGS.asset_server:
            rows = [{"name": "いつもの", "url": SETTINGS.asset_server}]
        cur = (SETTINGS.asset_server or "").rstrip("/")
        if cur and all(p["url"] != cur for p in rows):           # 登録していない接続先は「今の接続先」として先頭に
            self.profile_cb.addItem(f"今の接続先（{cur}）", cur)
        for p in rows:
            self.profile_cb.addItem(f"{p['name']}（{p['url']}）", p["url"])
        i = self.profile_cb.findData(cur)
        if i >= 0:
            self.profile_cb.setCurrentIndex(i)
        self.profile_cb.blockSignals(False)

    def add_profile(self, name: str = "", url: str = ""):
        url = (url or self.browser.url.text().strip() or SETTINGS.asset_server).rstrip("/")
        if not url:
            self.panel.msg.setText("URL を入れてから登録してください")
            return None
        if not name:
            name, ok = QInputDialog.getText(self, "接続先を登録", f"{url} の名前（例: 家・事務所）:")
            if not ok or not name.strip():
                return None
        rows = [p for p in self._profiles() if p["url"] != url] + [{"name": name.strip(), "url": url}]
        self.qs.setValue("profiles", json.dumps(rows, ensure_ascii=False))
        self._fill_profiles()
        return rows

    def remove_profile(self):
        rows = self._profiles()
        if not rows:
            return
        names = [f"{p['name']}（{p['url']}）" for p in rows]
        n, ok = QInputDialog.getItem(self, "接続先を削除", "削除する接続先:", names, 0, False)
        if ok:
            del rows[names.index(n)]
            self.qs.setValue("profiles", json.dumps(rows, ensure_ascii=False))
            self._fill_profiles()

    def switch_profile(self, url: str):
        if not url:
            return
        SETTINGS.asset_server = url
        SETTINGS.save()
        self.browser.url.setText(url)
        self.browser.connect_server()
        self._was_online = None
        QTimer.singleShot(500, self.check_connection)
        self.panel.msg.setText(f"接続先を {url} にしました")

    def restore_place(self):
        """前回開いていたフォルダ・表示に戻す（つながってから）。"""
        last = str(self.qs.value("last_dir", "") or "")
        if not last or not self.browser.roots:
            if last and getattr(self, "_restore_tries", 0) < 10 and self.server():
                self._restore_tries = getattr(self, "_restore_tries", 0) + 1
                QTimer.singleShot(1000, self.restore_place)
            return
        self.browser.go_to(last)

    def remember_place(self):
        if self.browser.cur_dir:
            self.qs.setValue("last_dir", self.browser.cur_dir)

    def launch_telocut(self) -> Optional[list]:
        """TeloCut を起動（同じフォルダの TeloCut.exe・ソースからなら telocut.py）。"""
        if self.telocut_running():
            self.panel.msg.setText("TeloCut はもう起動しています")
            return None
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).with_name("TeloCut.exe")
            args = [str(exe)]
        else:
            args = [sys.executable, str(Path(__file__).resolve().parents[1] / "telocut.py")]
        if getattr(sys, "frozen", False) and not Path(args[0]).exists():
            self.panel.msg.setText("TeloCut.exe が同じフォルダにありません")
            return None
        subprocess.Popen(args, close_fds=True)
        self.panel.msg.setText("TeloCut を起動しています…")
        return args

    def _table_dialog(self, title: str, headers: List[str], rows: List[list], note: str = ""):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(720, 420)
        v = QVBoxLayout(dlg)
        t = QTableWidget(len(rows), len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for i, r in enumerate(rows):
            for c, val in enumerate(r):
                t.setItem(i, c, QTableWidgetItem(str(val)))
        v.addWidget(t, 1)
        if note:
            v.addWidget(QLabel(note))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        self._last_table = t
        return dlg

    def show_stats(self, run: bool = True):
        """容量の内訳（種類ごとの数と大きさ）。"""
        from .assetbrowser import human_size, KIND_JP
        s = _req(self.server() + "/api/stats", timeout=15)
        kinds = s.get("kinds", {})
        total = max(1, s.get("total_size") or 1)
        rows = [[KIND_JP.get(k, k), v.get("count", 0), human_size(v.get("size") or 0),
                 "█" * int(round((v.get("size") or 0) / total * 30)) + f" {((v.get('size') or 0) / total * 100):.0f}%"]
                for k, v in sorted(kinds.items(), key=lambda kv: -(kv[1].get("size") or 0))]
        dlg = self._table_dialog("容量の内訳", ["種類", "数", "大きさ", "割合"], rows,
                                 f"合計 {s.get('total', 0)} 個・{human_size(s.get('total_size') or 0)}")
        if run:
            dlg.exec()
        return rows

    def show_activity(self, run: bool = True):
        """サーバーの最近の操作（だれが・いつ・何を）。"""
        acts = _req(self.server() + "/api/activity?limit=200", timeout=15)
        rows = [[datetime.datetime.fromtimestamp(float(a.get("time") or 0)).strftime("%m/%d %H:%M") if a.get("time") else "",
                 a.get("user", ""), a.get("kind", ""), a.get("text", "") or a.get("target", "")] for a in acts]
        dlg = self._table_dialog("最近の操作", ["日時", "人", "種類", "内容"], rows)
        if run:
            dlg.exec()
        return rows
