"""Avid 風のプロジェクトウィンドウと「プロジェクトの選択」画面。

- プロジェクトウィンドウ: ビン / エフェクト / 設定 / フォーマット / 使用状況 のタブ
- プロジェクトの選択（起動時）: プライベート（この PC）/ 共有（NAS）/ 外部 からプロジェクトを選ぶ・作る
  プロジェクトは「プロジェクト名フォルダ / プロジェクト名.tcut」で管理（Avid のプロジェクトフォルダと同じ考え方）
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTabWidget, QTreeWidget,
                             QTreeWidgetItem, QToolButton, QLabel, QListWidget, QListWidgetItem,
                             QFormLayout, QLineEdit, QComboBox, QPushButton, QDialog, QRadioButton,
                             QButtonGroup, QFileDialog, QMessageBox, QInputDialog, QHeaderView,
                             QAbstractItemView, QDialogButtonBox, QGroupBox, QTextBrowser)

from core.timecode import normalize_timecode
from .projectio import ProjectLock, describe_lock
from .settings import SETTINGS
from .theme import C

RES = [("1920×1080 (FHD)", 1920, 1080), ("3840×2160 (4K UHD)", 3840, 2160),
       ("1280×720 (HD)", 1280, 720), ("1080×1920 (縦型)", 1080, 1920), ("1080×1080 (正方形)", 1080, 1080)]
FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]


# ─────────────────────────────────────────────
#  プロジェクトウィンドウ
# ─────────────────────────────────────────────

class ProjectPanel(QWidget):
    def __init__(self, mw, effects_widget):
        super().__init__()
        self.mw = mw
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: bold; background: transparent; padding: 2px 4px;")
        lay.addWidget(self.title)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # ビン
        tb = QWidget()
        v = QVBoxLayout(tb)
        v.setContentsMargins(2, 2, 2, 2)
        row = QHBoxLayout()
        for text, fn, tip in (("新規ビン", lambda: mw.new_bin(), "新しいビンを作る"),
                              ("名前変更", self._rename, "選択したビンの名前を変更"),
                              ("削除", self._delete, "選択したビンを削除")):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["ビン", "素材", "シーケンス"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setRootIsDecorated(False)
        self.tree.itemDoubleClicked.connect(lambda it, _c: mw.bin.open_bin(it.data(0, Qt.ItemDataRole.UserRole)))
        v.addWidget(self.tree, 1)
        hint = QLabel("ダブルクリックでビンを開く（ビンコンテナにタブで表示）")
        hint.setObjectName("info")
        v.addWidget(hint)
        self.tabs.addTab(tb, "ビン")

        # エフェクト（Avid ではプロジェクトウィンドウのタブ）
        self.tabs.addTab(effects_widget, "エフェクト")

        # 設定
        ts = QWidget()
        vs = QVBoxLayout(ts)
        self.settings_list = QListWidget()
        for label, fn in (("キーボード", mw.keyboard_settings), ("環境設定（キャッシュ・自動保存・NAS）", mw.preferences),
                          ("シーケンス設定", mw.sequence_settings), ("ワークスペース: シングル", mw.workspace_single),
                          ("ワークスペース: デュアルディスプレイ", mw.workspace_dual)):
            it = QListWidgetItem("⚙ " + label)
            it.setData(Qt.ItemDataRole.UserRole, fn)
            self.settings_list.addItem(it)
        self.settings_list.itemDoubleClicked.connect(lambda it: it.data(Qt.ItemDataRole.UserRole)())
        vs.addWidget(self.settings_list, 1)
        lab = QLabel("ダブルクリックで開く")
        lab.setObjectName("info")
        vs.addWidget(lab)
        self.tabs.addTab(ts, "設定")

        # フォーマット
        tf = QWidget()
        ff = QFormLayout(tf)
        self.fmt_name = QLabel("")
        self.fmt_raster = QLabel("")
        self.fmt_fps = QLabel("")
        self.fmt_tc = QLabel("")
        ff.addRow("プロジェクト名", self.fmt_name)
        ff.addRow("ラスター", self.fmt_raster)
        ff.addRow("フレームレート", self.fmt_fps)
        ff.addRow("開始タイムコード", self.fmt_tc)
        b = QPushButton("シーケンス設定を変更...")
        b.clicked.connect(mw.sequence_settings)
        ff.addRow(b)
        self.tabs.addTab(tf, "フォーマット")

        # 使用状況
        self.usage = QTextBrowser()
        self.tabs.addTab(self.usage, "使用状況")
        self.tabs.currentChanged.connect(lambda _i: self.refresh())

    def refresh(self):
        mw = self.mw
        proj = mw.project
        name = proj.name or (Path(mw.project_path).stem if mw.project_path else "無題")
        ro = "（読み取り専用）" if mw.read_only else ""
        self.title.setText(f"プロジェクト: {name}{ro}")
        sel = self._selected()
        self.tree.clear()
        cur = mw.bin.current_bin_id()
        for b in proj.bins:
            it = QTreeWidgetItem([("▸ " if b.id == cur else "") + b.name, str(len(b.ids("media"))),
                                  str(len(b.ids("sequence")))])
            it.setData(0, Qt.ItemDataRole.UserRole, b.id)
            self.tree.addTopLevelItem(it)
            if b.id == sel:
                it.setSelected(True)
        seq = proj.sequence
        self.fmt_name.setText(name)
        self.fmt_raster.setText(f"{seq.width} × {seq.height}")
        self.fmt_fps.setText(f"{seq.fps:g} fps")
        self.fmt_tc.setText(seq.start_tc)
        if self.tabs.currentIndex() == 4:
            lock = ProjectLock.read(mw.project_path) if mw.project_path else None
            offline = [m for m in proj.media if not os.path.exists(m.path)]
            self.usage.setHtml(
                f"<b>場所</b>: {mw.project_path or '（未保存）'}<br>"
                f"<b>ユーザー</b>: {SETTINGS.user_name} @ {SETTINGS.host()}<br>"
                f"<b>ロック</b>: {describe_lock(lock) if lock else 'なし'}<br><br>"
                f"ビン {len(proj.bins)} / 素材 {len(proj.media)}（オフライン {len(offline)}）/ "
                f"シーケンス {len(proj.sequences)}<br><br>"
                f"<b>キャッシュ（この PC）</b><br>音声: {SETTINGS.cache_dir}<br>"
                f"レンダー: {SETTINGS.render_dir}<br>プロキシ: {SETTINGS.proxy_dir}<br>Attic: {SETTINGS.attic_dir}")

    def _selected(self) -> Optional[str]:
        its = self.tree.selectedItems()
        return its[0].data(0, Qt.ItemDataRole.UserRole) if its else None

    def _rename(self):
        bid = self._selected()
        b = self.mw.project.bin_by_id(bid) if bid else None
        if b is None:
            return
        name, ok = QInputDialog.getText(self, "ビン名", "名前:", text=b.name)
        if ok and name.strip():
            self.mw.begin_edit()
            b.name = name.strip()
            self.mw.end_edit("ビン名を変更")

    def _delete(self):
        bid = self._selected()
        if bid:
            self.mw.delete_bin(bid)


# ─────────────────────────────────────────────
#  プロジェクトの選択（起動時）
# ─────────────────────────────────────────────

def list_projects(folder: str):
    """フォルダ直下の「名前/名前.tcut」と「*.tcut」を探す。"""
    out = []
    try:
        root = Path(folder)
        if not root.exists():
            return out
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            cand = None
            if p.is_dir():
                f = p / f"{p.name}.tcut"
                if f.exists():
                    cand = f
                else:
                    fs = list(p.glob("*.tcut"))
                    cand = fs[0] if fs else None
            elif p.suffix.lower() == ".tcut":
                cand = p
            if cand is not None:
                out.append(cand)
    except OSError:
        pass
    return out


class NewProjectDialog(QDialog):
    def __init__(self, parent, folder: str):
        super().__init__(parent)
        self.setWindowTitle("新規プロジェクト")
        f = QFormLayout(self)
        f.addRow(QLabel(f"作成場所: {folder}"))
        self.name = QLineEdit("新規プロジェクト")
        self.name.selectAll()
        f.addRow("プロジェクト名", self.name)
        self.res = QComboBox()
        for lab, w, h in RES:
            self.res.addItem(lab, (w, h))
        f.addRow("ラスター", self.res)
        self.fps = QComboBox()
        for v in FPS:
            self.fps.addItem(f"{v:g} fps", v)
        self.fps.setCurrentIndex(FPS.index(29.97))
        f.addRow("フレームレート", self.fps)
        self.tc = QLineEdit("01:00:00:00")
        f.addRow("開始タイムコード", self.tc)
        from . import templates as TP
        self.tmpl = QComboBox()
        for t in TP.project_templates():
            self.tmpl.addItem(("★ " if t.get("_user") else "") + t["name"], t)
        f.addRow("テンプレート", self.tmpl)
        note = QLabel("フレームレートは後から変更できません（Avid と同じ）。ラスターはシーケンスごとに変更できます。")
        note.setObjectName("info")
        note.setWordWrap(True)
        f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _ok(self):
        n = self.name.text().strip()
        if not n or any(ch in n for ch in '\\/:*?"<>|'):
            QMessageBox.warning(self, "新規プロジェクト", "プロジェクト名に使えない文字が含まれています。")
            return
        self.accept()

    def values(self):
        w, h = self.res.currentData()
        try:
            tc = normalize_timecode(self.tc.text(), self.fps.currentData())
        except ValueError:
            tc = "01:00:00:00"
        return self.name.text().strip(), w, h, self.fps.currentData(), tc, self.tmpl.currentData()


class SelectProjectDialog(QDialog):
    """起動時に表示（Avid の「プロジェクトの選択」）。

    結果: self.result_action = ("open", path) / ("new", path, name, w, h, fps, tc) / None
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("TeloCut — プロジェクトの選択")
        self.resize(760, 480)
        self.result_action = None
        from .assets import app_icon
        self.setWindowIcon(app_icon())
        root = QHBoxLayout(self)

        left = QVBoxLayout()
        logo = QLabel("TeloCut")
        logo.setStyleSheet(f"font-size: 22pt; font-weight: bold; color: {C['accent']}; background: transparent;")
        left.addWidget(logo)
        sub = QLabel("テロップに強い動画編集ソフト")
        sub.setObjectName("info")
        left.addWidget(sub)
        left.addSpacing(10)
        gb = QGroupBox("ユーザー")
        gl = QVBoxLayout(gb)
        self.user = QLineEdit(SETTINGS.user_name)
        gl.addWidget(self.user)
        left.addWidget(gb)
        gb2 = QGroupBox("フォルダ")
        g2 = QVBoxLayout(gb2)
        self.loc_group = QButtonGroup(self)
        self.loc_btns = {}
        for key, label in (("private", "プライベート（この PC）"), ("shared", "共有（NAS）"), ("external", "外部")):
            rb = QRadioButton(label)
            self.loc_group.addButton(rb)
            self.loc_btns[key] = rb
            rb.toggled.connect(lambda on, k=key: on and self._set_location(k))
            g2.addWidget(rb)
        self.loc_path = QLabel("")
        self.loc_path.setObjectName("info")
        self.loc_path.setWordWrap(True)
        g2.addWidget(self.loc_path)
        b = QPushButton("フォルダを変更...")
        b.clicked.connect(self._change_folder)
        g2.addWidget(b)
        left.addWidget(gb2)
        left.addStretch(1)
        root.addLayout(left, 2)

        right = QVBoxLayout()
        right.addWidget(QLabel("プロジェクト"))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self._open())
        right.addWidget(self.list, 1)
        row = QHBoxLayout()
        b = QPushButton("新規プロジェクト...")
        b.clicked.connect(self._new)
        row.addWidget(b)
        b = QPushButton("ファイルを参照...")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        row.addStretch(1)
        b = QPushButton("終了")
        b.clicked.connect(self.reject)
        row.addWidget(b)
        ok = QPushButton("開く")
        ok.setDefault(True)
        ok.clicked.connect(self._open)
        row.addWidget(ok)
        right.addLayout(row)
        root.addLayout(right, 3)

        loc = SETTINGS.last_location if SETTINGS.last_location in self.loc_btns else "private"
        self.loc_btns[loc].setChecked(True)
        self._set_location(loc)

    def _folder(self, key=None) -> str:
        key = key or self.location
        return {"private": SETTINGS.private_dir, "shared": SETTINGS.shared_dir,
                "external": SETTINGS.external_dir}.get(key, "")

    def _set_location(self, key: str):
        self.location = key
        folder = self._folder()
        if key == "private" and folder:
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self.loc_path.setText(folder or "（未設定: 「フォルダを変更...」で NAS の共有フォルダなどを指定）")
        self._fill()

    def _change_folder(self):
        d = QFileDialog.getExistingDirectory(self, "プロジェクトのフォルダ", self._folder() or str(Path.home()))
        if not d:
            return
        setattr(SETTINGS, {"private": "private_dir", "shared": "shared_dir", "external": "external_dir"}[self.location], d)
        SETTINGS.save()
        self._set_location(self.location)

    def _fill(self):
        self.list.clear()
        folder = self._folder()
        if not folder:
            return
        for p in list_projects(folder):
            info = ProjectLock.read(str(p))
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lock = ""
            if info and not ProjectLock.is_ours(info) and not ProjectLock.is_stale(info):
                lock = f"   🔒 {info.get('user')}（{info.get('host')}）が使用中"
            it = QListWidgetItem(f"{p.stem}    {mtime}{lock}")
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            self.list.addItem(it)
            if str(p) == SETTINGS.last_project:
                self.list.setCurrentItem(it)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)

    def _save_user(self):
        SETTINGS.user_name = self.user.text().strip() or SETTINGS.user_name
        SETTINGS.last_location = self.location
        SETTINGS.save()

    def _open(self):
        it = self.list.currentItem()
        if it is None:
            return
        path = it.data(Qt.ItemDataRole.UserRole)
        self._save_user()
        SETTINGS.last_project = path
        SETTINGS.save()
        self.result_action = ("open", path)
        self.accept()

    def _browse(self):
        p, _ = QFileDialog.getOpenFileName(self, "プロジェクトを開く", self._folder(),
                                           "TeloCut プロジェクト (*.tcut *.dvep)")
        if p:
            self._save_user()
            SETTINGS.last_project = p
            SETTINGS.save()
            self.result_action = ("open", p)
            self.accept()

    def _new(self):
        folder = self._folder()
        if not folder:
            QMessageBox.information(self, "新規プロジェクト", "先に「フォルダを変更...」で保存先を指定してください。")
            return
        dlg = NewProjectDialog(self, folder)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, w, h, fps, tc, tmpl = dlg.values()
        pdir = Path(folder) / name
        path = pdir / f"{name}.tcut"
        if path.exists():
            QMessageBox.warning(self, "新規プロジェクト", "同じ名前のプロジェクトがあります。")
            return
        self._save_user()
        SETTINGS.last_project = str(path)
        SETTINGS.save()
        self.result_action = ("new", str(path), name, w, h, fps, tc, tmpl)
        self.accept()
