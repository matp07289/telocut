"""YouTube にアップロードする画面と、アップロード履歴（TeloCut と TeloCut Assets の両方から使う）。"""
from __future__ import annotations

import datetime
import os
import threading
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QDateTime, Qt, QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                             QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QHeaderView,
                             QAbstractItemView)

from . import ytupload as YT
from .settings import SETTINGS


class _Sig(QObject):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    info = pyqtSignal(str, object)


def _run(sig: _Sig, tag: str, fn, *a):
    def work():
        try:
            sig.info.emit(tag, fn(*a))
        except Exception as ex:
            sig.info.emit(tag, ex)
    threading.Thread(target=work, daemon=True).start()


DEFAULT_TEMPLATES = [
    {"name": "シンプル", "description": "{title}\n\n#Shorts", "tags": ""},
    {"name": "ゆっくり解説", "description": "{title}\n\n■ 使用素材\n音声: VOICEVOX\n\n■ チャプター\n{chapters}\n\n#ゆっくり解説",
     "tags": "ゆっくり解説,ゆっくり"},
    {"name": "ゲーム実況", "description": "{title}\n\n▼ チャンネル登録よろしくお願いします\n\n{chapters}\n\n#ゲーム実況", "tags": "ゲーム実況"},
]


def templates() -> List[dict]:
    return list(getattr(SETTINGS, "yt_templates", None) or DEFAULT_TEMPLATES)


class YouTubeUploadDialog(QDialog):
    """YouTube にアップロード（ログイン・タイトル・説明・公開範囲・予約・サムネイル・再生リスト・進み具合）。"""

    def __init__(self, parent=None, path: str = "", title: str = "", description: str = "", tags: Optional[List[str]] = None,
                 chapters: str = "", resume: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("YouTube にアップロード")
        self.resize(640, 720)
        self.sig = _Sig()
        self.sig.progress.connect(self._on_progress)
        self.sig.done.connect(self._on_done)
        self.sig.failed.connect(self._on_failed)
        self.sig.info.connect(self._on_info)
        self.cancel_ev: Optional[threading.Event] = None
        self.resume = resume
        self.result: Optional[dict] = None
        self.chapters = chapters
        v = QVBoxLayout(self)
        # アカウント
        acc = QHBoxLayout()
        self.acc_label = QLabel("")
        self.client_btn = QPushButton("設定ファイルを選ぶ…")
        self.client_btn.setToolTip("Google Cloud で作った OAuth クライアント（デスクトップ アプリ）の JSON")
        self.client_btn.clicked.connect(self.choose_client)
        self.login_btn = QPushButton("Google にログイン")
        self.login_btn.clicked.connect(self.sign_in)
        self.logout_btn = QPushButton("ログアウト")
        self.logout_btn.clicked.connect(self.sign_out)
        for w in (self.acc_label, self.client_btn, self.login_btn, self.logout_btn):
            acc.addWidget(w, 1 if w is self.acc_label else 0)
        v.addLayout(acc)
        f = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit(path)
        b = QPushButton("選ぶ…")
        b.clicked.connect(self.choose_file)
        row.addWidget(self.path, 1)
        row.addWidget(b)
        f.addRow("動画ファイル", row)
        self.title = QLineEdit(title or (Path(path).stem if path else ""))
        self.title.setMaxLength(100)
        f.addRow("タイトル", self.title)
        trow = QHBoxLayout()
        self.tmpl = QComboBox()
        self.tmpl.addItem("（ひな形を使う）", None)
        for t in templates():
            self.tmpl.addItem(t["name"], t)
        self.tmpl.activated.connect(self.apply_template)
        save_t = QPushButton("ひな形として保存")
        save_t.clicked.connect(self.save_template)
        trow.addWidget(self.tmpl, 1)
        trow.addWidget(save_t)
        f.addRow("説明のひな形", trow)
        self.desc = QPlainTextEdit(description)
        self.desc.setPlaceholderText("説明（{title} {date} {chapters} が使えます）")
        self.desc.setMinimumHeight(140)
        f.addRow("説明", self.desc)
        self.tags = QLineEdit(", ".join(tags or []))
        self.tags.setPlaceholderText("タグ（, で区切る）")
        f.addRow("タグ", self.tags)
        self.cat = QComboBox()
        for k, lab in YT.CATEGORIES:
            self.cat.addItem(lab, k)
        f.addRow("カテゴリ", self.cat)
        self.privacy = QComboBox()
        for k, lab in YT.PRIVACY:
            self.privacy.addItem(lab, k)
        f.addRow("公開範囲", self.privacy)
        srow = QHBoxLayout()
        self.sched = QCheckBox("予約公開")
        self.when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600 * 24))
        self.when.setDisplayFormat("yyyy/MM/dd HH:mm")
        self.when.setCalendarPopup(True)
        self.when.setEnabled(False)
        self.sched.toggled.connect(self.when.setEnabled)
        srow.addWidget(self.sched)
        srow.addWidget(self.when, 1)
        f.addRow("公開日時", srow)
        self.kids = QCheckBox("子ども向けの動画")
        f.addRow("", self.kids)
        self.lang = QComboBox()
        for k, lab in (("ja", "日本語"), ("en", "英語"), ("", "指定しない")):
            self.lang.addItem(lab, k)
        f.addRow("言語", self.lang)
        th = QHBoxLayout()
        self.thumb = QLineEdit()
        self.thumb.setPlaceholderText("サムネイル画像（任意・2MB まで）")
        tb = QPushButton("選ぶ…")
        tb.clicked.connect(self.choose_thumb)
        th.addWidget(self.thumb, 1)
        th.addWidget(tb)
        f.addRow("サムネイル", th)
        pl = QHBoxLayout()
        self.playlist = QComboBox()
        self.playlist.addItem("（入れない）", "")
        lp = QPushButton("読み込む")
        lp.clicked.connect(self.load_playlists)
        pl.addWidget(self.playlist, 1)
        pl.addWidget(lp)
        f.addRow("再生リスト", pl)
        v.addLayout(f)
        self.note = QLabel("審査を通していない Google Cloud のクライアントで上げた動画は、YouTube の決まりで「非公開」になります。")
        self.note.setWordWrap(True)
        self.note.setObjectName("info")
        v.addWidget(self.note)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.msg)
        bb = QHBoxLayout()
        self.hist_btn = QPushButton("アップロード履歴")
        self.hist_btn.clicked.connect(lambda: YouTubeHistoryDialog(self).exec())
        self.go = QPushButton("アップロード")
        self.go.setDefault(True)
        self.go.clicked.connect(self.start)
        self.stop_btn = QPushButton("閉じる")
        self.stop_btn.clicked.connect(self._close_or_cancel)
        bb.addWidget(self.hist_btn)
        bb.addStretch(1)
        bb.addWidget(self.go)
        bb.addWidget(self.stop_btn)
        v.addLayout(bb)
        if resume:
            self._load_meta(resume.get("meta") or {})
            self.path.setText(resume.get("path", ""))
            self.go.setText("続きから送る")
        self.refresh_account()

    # ── アカウント ──
    def refresh_account(self):
        c = YT.client()
        if not c:
            self.acc_label.setText("① Google Cloud の OAuth クライアント（JSON）を読み込んでください")
        elif not YT.signed_in():
            self.acc_label.setText("② Google にログインしてください")
        else:
            self.acc_label.setText("✓ ログイン済み")
        self.login_btn.setEnabled(bool(c) and not YT.signed_in())
        self.logout_btn.setEnabled(YT.signed_in())
        self.go.setEnabled(bool(c) and YT.signed_in())

    def choose_client(self):
        p, _ = QFileDialog.getOpenFileName(self, "OAuth クライアントの JSON", "", "JSON (*.json)")
        if p:
            try:
                YT.install_client(p)
            except Exception as ex:
                QMessageBox.warning(self, "YouTube", str(ex))
            self.refresh_account()

    def sign_in(self):
        self.msg.setText("ブラウザで Google にログインして「許可」を押してください…")
        self.login_btn.setEnabled(False)
        _run(self.sig, "signin", YT.sign_in)

    def sign_out(self):
        YT.sign_out()
        self.refresh_account()

    def load_playlists(self):
        self.msg.setText("再生リストを読み込み中…")
        _run(self.sig, "playlists", YT.playlists)

    def _on_info(self, tag, res):
        if isinstance(res, Exception):
            self.msg.setText(str(res))
            self.refresh_account()
            return
        if tag == "signin":
            self.msg.setText("ログインしました")
            self.refresh_account()
        elif tag == "playlists":
            self.playlist.clear()
            self.playlist.addItem("（入れない）", "")
            for p in res:
                self.playlist.addItem(p["title"], p["id"])
            self.msg.setText(f"再生リスト {len(res)} 個")

    # ── 入力 ──
    def choose_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "動画ファイル", self.path.text(), "動画 (*.mp4 *.mov *.mkv *.webm *.avi *.m4v);;すべて (*)")
        if p:
            self.path.setText(p)
            if not self.title.text():
                self.title.setText(Path(p).stem)

    def choose_thumb(self):
        p, _ = QFileDialog.getOpenFileName(self, "サムネイル", "", "画像 (*.jpg *.jpeg *.png)")
        if p:
            self.thumb.setText(p)

    def apply_template(self, _i=None):
        t = self.tmpl.currentData()
        if not t:
            return
        self.desc.setPlainText(YT.fill_template(t.get("description", ""), title=self.title.text(),
                                                file=Path(self.path.text()).name, chapters=self.chapters))
        if t.get("tags") and not self.tags.text().strip():
            self.tags.setText(t["tags"])

    def save_template(self):
        name, ok = QInputDialog.getText(self, "ひな形として保存", "名前:")
        if not ok or not name.strip():
            return
        rows = [t for t in templates() if t["name"] != name.strip()]
        rows.append({"name": name.strip(), "description": self.desc.toPlainText(), "tags": self.tags.text()})
        SETTINGS.yt_templates = rows
        SETTINGS.save()
        self.tmpl.addItem(name.strip(), rows[-1])

    def meta(self) -> dict:
        m = {"title": self.title.text().strip(), "description": self.desc.toPlainText(),
             "tags": [t.strip() for t in self.tags.text().replace("、", ",").split(",") if t.strip()],
             "category": self.cat.currentData(), "privacy": self.privacy.currentData(), "for_kids": self.kids.isChecked(),
             "language": self.lang.currentData(), "thumbnail": self.thumb.text().strip(), "playlist": self.playlist.currentData() or ""}
        if self.sched.isChecked():
            m["publish_at"] = self.when.dateTime().toUTC().toString("yyyy-MM-ddTHH:mm:ss.000Z")
        return m

    def _load_meta(self, m: dict):
        self.title.setText(m.get("title", ""))
        self.desc.setPlainText(m.get("description", ""))
        self.tags.setText(", ".join(m.get("tags") or []))
        for cb, key in ((self.cat, "category"), (self.privacy, "privacy"), (self.lang, "language")):
            i = cb.findData(m.get(key))
            if i >= 0:
                cb.setCurrentIndex(i)
        self.kids.setChecked(bool(m.get("for_kids")))
        self.thumb.setText(m.get("thumbnail", ""))

    def check(self) -> str:
        p = self.path.text().strip()
        if not p or not os.path.isfile(p):
            return "動画ファイルを選んでください"
        if not self.title.text().strip():
            return "タイトルを入れてください"
        if self.thumb.text().strip() and not os.path.isfile(self.thumb.text().strip()):
            return "サムネイルの画像が見つかりません"
        if self.sched.isChecked() and self.when.dateTime() <= QDateTime.currentDateTime():
            return "予約公開の日時は、今より後にしてください"
        return ""

    # ── アップロード ──
    def start(self):
        err = self.check()
        if err:
            self.msg.setText(err)
            return None
        self.cancel_ev = threading.Event()
        self.go.setEnabled(False)
        self.stop_btn.setText("中止")
        self.bar.setVisible(True)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.msg.setText("アップロードの準備中…")
        path, meta, key = self.path.text().strip(), self.meta(), (self.resume or {}).get("key", "")

        def work():
            try:
                self.sig.done.emit(YT.upload(path, meta, lambda a, b: self.sig.progress.emit(a, b), self.cancel_ev, key))
            except Exception as ex:
                self.sig.failed.emit(str(ex))
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        return self._thread

    def _on_progress(self, a: int, b: int):
        self.bar.setValue(int(a * 1000 / max(1, b)))
        self.msg.setText(f"送信中… {a / 1e6:.1f} / {b / 1e6:.1f} MB")

    def _on_done(self, entry: dict):
        self.result = entry
        self.bar.setValue(1000)
        self.go.setEnabled(True)
        self.go.setText("アップロード")
        self.stop_btn.setText("閉じる")
        url = entry.get("url", "")
        if url:
            QApplication.clipboard().setText(url)
        notes = "\n".join(entry.get("notes") or [])
        self.msg.setText(f"アップロードしました: {url}（URL をコピーしました）" + (f"\n{notes}" if notes else ""))

    def _on_failed(self, err: str):
        self.go.setEnabled(True)
        self.go.setText("続きから送る" if any(r.get("status") == "failed" for r in YT.history()[:1]) else "アップロード")
        if self.go.text() == "続きから送る":
            self.resume = YT.history()[0]
        self.stop_btn.setText("閉じる")
        self.msg.setText(f"失敗しました: {err}")

    def _close_or_cancel(self):
        if self.cancel_ev is not None and self.stop_btn.text() == "中止":
            self.cancel_ev.set()
            self.msg.setText("中止しています…")
            return
        self.reject()


class YouTubeHistoryDialog(QDialog):
    """アップロード履歴（URL を開く・コピー・失敗したものを続きから送る）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("YouTube アップロード履歴")
        self.resize(760, 420)
        v = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["日時", "タイトル", "状態", "URL"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.open_row(r))
        v.addWidget(self.table, 1)
        h = QHBoxLayout()
        for txt, fn in (("開く", lambda: self.open_row(self.table.currentRow())), ("URL をコピー", self.copy_url),
                        ("続きから送る", self.resume_row), ("履歴から消す", self.remove_row)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        close = QPushButton("閉じる")
        close.clicked.connect(self.reject)
        h.addWidget(close)
        v.addLayout(h)
        self.refresh()

    def refresh(self):
        self.rows = YT.history()
        st = {"done": "✓ 完了", "failed": "✗ 失敗（続きから送れます）", "uploading": "送信中 / 中断"}
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            for c, val in enumerate((r.get("time", "").replace("T", " "), r.get("title", ""),
                                     st.get(r.get("status"), r.get("status", "")), r.get("url", ""))):
                self.table.setItem(i, c, QTableWidgetItem(val))

    def _row(self, i: int) -> Optional[dict]:
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def open_row(self, i: int):
        r = self._row(i)
        if r and r.get("url"):
            QDesktopServices.openUrl(QUrl(r["url"]))

    def copy_url(self):
        r = self._row(self.table.currentRow())
        if r and r.get("url"):
            QApplication.clipboard().setText(r["url"])

    def resume_row(self):
        r = self._row(self.table.currentRow())
        if r and r.get("status") != "done":
            YouTubeUploadDialog(self.parent(), resume=r).exec()
            self.refresh()

    def remove_row(self):
        r = self._row(self.table.currentRow())
        if r:
            YT._save_history([x for x in YT.history() if x.get("key") != r.get("key")])
            self.refresh()
