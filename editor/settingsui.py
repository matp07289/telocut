"""Avid のような 3 つの設定（ユーザー設定・プロジェクト設定・サイト設定 = 環境設定）、ライセンス、テンプレートの画面
（MainWindow のミックスイン）。"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QIcon, QPixmap, QPainter
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSlider, QSpinBox,
                             QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
                             QAbstractItemView)

from . import license as LIC
from . import model as M
from . import templates as TP
from . import theme as TH
from .settings import SETTINGS


def _swatch(name: str) -> QPixmap:
    c = TH.palette(name)
    pm = QPixmap(64, 20)
    p = QPainter(pm)
    for i, k in enumerate(("bg", "panel3", "accent", "text")):
        p.fillRect(i * 16, 0, 16, 20, QColor(c[k]))
    p.end()
    return pm


class _Pages(QDialog):
    """左に項目・右に中身（Avid の設定画面のように）。"""

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 560)
        v = QVBoxLayout(self)
        h = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.stack = QStackedWidget()
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        h.addWidget(self.nav)
        h.addWidget(self.stack, 1)
        v.addLayout(h, 1)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.accepted.connect(self.accept)
        self.bb.rejected.connect(self.reject)
        v.addWidget(self.bb)

    def page(self, title: str) -> QFormLayout:
        w = QWidget()
        f = QFormLayout(w)
        self.stack.addWidget(w)
        self.nav.addItem(title)
        if self.nav.count() == 1:
            self.nav.setCurrentRow(0)
        return f


# ─────────────────────────── ユーザー設定 ───────────────────────────
class UserSettingsDialog(_Pages):
    def __init__(self, mw):
        super().__init__(mw, f"ユーザー設定 — {SETTINGS.current_profile}")
        self.mw = mw
        self._orig = TH.current_choice()
        # インターフェース（配色）
        f = self.page("インターフェース（配色）")
        self.theme = QComboBox()
        for k, t in TH.THEMES.items():
            self.theme.addItem(QIcon(_swatch(k)), t["label"], k)
        self.theme.setCurrentIndex(max(0, self.theme.findData(SETTINGS.theme)))
        f.addRow("配色パターン", self.theme)
        self.bright = QSlider(Qt.Orientation.Horizontal)
        self.bright.setRange(-50, 50)
        self.bright.setValue(int(SETTINGS.theme_brightness))
        f.addRow("明るさ（暗い ← → 明るい）", self.bright)
        self.accent = SETTINGS.theme_accent
        h = QHBoxLayout()
        self.accent_btn = QPushButton("強調色を選ぶ...")
        self.accent_btn.clicked.connect(self._pick_accent)
        rs = QPushButton("配色パターンの色に戻す")
        rs.clicked.connect(lambda: (setattr(self, "accent", ""), self._preview()))
        h.addWidget(self.accent_btn)
        h.addWidget(rs)
        f.addRow("強調色", h)
        self.font_pt = QDoubleSpinBox()
        self.font_pt.setRange(7, 16)
        self.font_pt.setSingleStep(0.5)
        self.font_pt.setValue(float(SETTINGS.ui_font_pt))
        f.addRow("文字の大きさ", self.font_pt)
        self.follow = QCheckBox("Windows のライト / ダークに合わせる")
        self.follow.setChecked(SETTINGS.theme_follow_os)
        f.addRow(self.follow)
        note = QLabel("変えるとすぐ反映します（一部の画面は次に開いたとき）。「キャンセル」で元に戻ります。")
        note.setObjectName("info")
        f.addRow(note)
        for w in (self.theme,):
            w.currentIndexChanged.connect(lambda _i: self._preview())
        self.bright.valueChanged.connect(lambda _v: self._preview())
        self.font_pt.valueChanged.connect(lambda _v: self._preview())
        self.follow.toggled.connect(lambda _v: self._preview())
        # キーボード
        f = self.page("キーボード")
        f.addRow(QLabel(f"今のプリセット: {SETTINGS.keyboard_preset}（キーの割り当てはユーザーごと）"))
        kb = QPushButton("キーボード設定を開く...")
        kb.clicked.connect(lambda: self.mw.cmd_fns.get("keyboard_settings", lambda: None)())
        f.addRow(kb)
        # 編集
        f = self.page("編集・再生")
        self.proxy = QCheckBox("プロキシがあれば再生に使う")
        self.proxy.setChecked(SETTINGS.use_proxy)
        self.idle = QCheckBox("止まっている間に重い所を先にレンダリング")
        self.idle.setChecked(SETTINGS.idle_render)
        self.fxmode = QComboBox()
        self.fxmode.addItem("レイヤー（上から順に）", "stack")
        self.fxmode.addItem("ノード", "nodes")
        self.fxmode.setCurrentIndex(max(0, self.fxmode.findData(SETTINGS.fx_mode_default)))
        self.tmpl = QComboBox()
        self.tmpl.addItem("（使わない）", "")
        for t in TP.telop_templates():
            self.tmpl.addItem(t["name"], t["name"])
        self.tmpl.setCurrentIndex(max(0, self.tmpl.findData(SETTINGS.default_telop_template)))
        f.addRow(self.proxy)
        f.addRow(self.idle)
        f.addRow("エフェクトの管理方法（新しく付けるとき）", self.fxmode)
        f.addRow("テロップを足すときのテンプレート", self.tmpl)
        # 起動と保存
        f = self.page("起動と保存")
        self.uname = QLineEdit(SETTINGS.user_name)
        self.open_last = QCheckBox("起動したら前回のプロジェクトを開く（プロジェクトの選択を出さない）")
        self.open_last.setChecked(SETTINGS.open_last_project)
        self.autosave = QSpinBox()
        self.autosave.setRange(0, 120)
        self.autosave.setSuffix(" 分ごと（0 = しない）")
        self.autosave.setValue(SETTINGS.autosave_minutes)
        self.attic = QSpinBox()
        self.attic.setRange(1, 500)
        self.attic.setSuffix(" 世代")
        self.attic.setValue(SETTINGS.attic_keep)
        f.addRow("名前（コメント・制作管理に出る）", self.uname)
        f.addRow(self.open_last)
        f.addRow("自動保存", self.autosave)
        f.addRow("バックアップ（Attic）", self.attic)
        # 書き出しプリセット
        f = self.page("書き出しプリセット")
        self.presets = QListWidget()
        for p in SETTINGS.export_presets:
            self.presets.addItem(f"{p.get('name')}  （{p.get('preset')}）")
        f.addRow(QLabel("書き出し画面の「プリセット ▾」で選べます。書き出し画面の「今の設定を保存」で増やせます。"))
        f.addRow(self.presets)
        rm = QPushButton("選んだプリセットを消す")
        rm.clicked.connect(self._del_preset)
        f.addRow(rm)
        # ユーザー
        f = self.page("ユーザー（切り替え・作成）")
        self.users = QListWidget()
        for n in SETTINGS.profiles():
            it = QListWidgetItem(("● " if n == SETTINGS.current_profile else "　") + n)
            it.setData(Qt.ItemDataRole.UserRole, n)
            self.users.addItem(it)
        f.addRow(QLabel("ユーザーごとに、配色・キーボード・書き出しプリセット・テンプレートの既定などを持ちます（Avid のユーザー設定）。"))
        f.addRow(self.users)
        row = QHBoxLayout()
        for txt, fn in (("このユーザーに切り替え", self._switch), ("新しいユーザー...", lambda: self._new(False)),
                        ("今の設定を複製...", lambda: self._new(True)), ("削除", self._delete),
                        ("書き出し...", self._export), ("読み込み...", self._import)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            row.addWidget(b)
        f.addRow(row)

    def _pick_accent(self):
        c = QColorDialog.getColor(QColor(self.accent or TH.C.get("accent", "#7c6cf2")), self, "強調色")
        if c.isValid():
            self.accent = c.name()
            self._preview()

    def _preview(self):
        name = self.theme.currentData()
        if self.follow.isChecked():
            light = TH.windows_prefers_light()
            if light is not None:
                name = "light" if light else (name if name != "light" else "avid")
        TH.apply(QApplication.instance(), name, self.bright.value(), self.accent, self.font_pt.value())

    def _del_preset(self):
        r = self.presets.currentRow()
        if 0 <= r < len(SETTINGS.export_presets):
            del SETTINGS.export_presets[r]
            self.presets.takeItem(r)

    def _sel_user(self) -> str:
        it = self.users.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else ""

    def _switch(self):
        n = self._sel_user()
        if n and n != SETTINGS.current_profile:
            self.mw.switch_user(n)
            self.reject_no_revert = True
            self.done(2)

    def _new(self, copy: bool):
        n, ok = QInputDialog.getText(self, "ユーザー", "名前:")
        if ok and n.strip():
            self.mw.switch_user(n.strip(), copy)
            self.reject_no_revert = True
            self.done(2)

    def _delete(self):
        n = self._sel_user()
        if n and SETTINGS.delete_profile(n):
            self.users.takeItem(self.users.currentRow())
        elif n:
            QMessageBox.information(self, "ユーザー", "使っているユーザーは消せません（ほかに切り替えてから）。")

    def _export(self):
        p, _ = QFileDialog.getSaveFileName(self, "ユーザー設定を書き出し", f"{SETTINGS.current_profile}.telocut-user.json",
                                           "ユーザー設定 (*.json)")
        if p:
            SETTINGS.export_profile(p)

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, "ユーザー設定を読み込み", "", "ユーザー設定 (*.json)")
        if p:
            n = SETTINGS.import_profile(p)
            it = QListWidgetItem("　" + n)
            it.setData(Qt.ItemDataRole.UserRole, n)
            self.users.addItem(it)

    def accept(self):
        SETTINGS.theme = self.theme.currentData()
        SETTINGS.theme_brightness = self.bright.value()
        SETTINGS.theme_accent = self.accent
        SETTINGS.ui_font_pt = self.font_pt.value()
        SETTINGS.theme_follow_os = self.follow.isChecked()
        SETTINGS.use_proxy = self.proxy.isChecked()
        SETTINGS.idle_render = self.idle.isChecked()
        SETTINGS.fx_mode_default = self.fxmode.currentData()
        SETTINGS.default_telop_template = self.tmpl.currentData()
        SETTINGS.user_name = self.uname.text().strip() or SETTINGS.user_name
        SETTINGS.open_last_project = self.open_last.isChecked()
        SETTINGS.autosave_minutes = self.autosave.value()
        SETTINGS.attic_keep = self.attic.value()
        SETTINGS.save()
        TH.apply(QApplication.instance())
        super().accept()

    def reject(self):
        if not getattr(self, "reject_no_revert", False):
            n, b, a, f = self._orig
            TH.apply(QApplication.instance(), n, b, a, f)
        super().reject()


# ─────────────────────────── プロジェクト設定 ───────────────────────────
class ProjectSettingsDialog(_Pages):
    def __init__(self, mw):
        super().__init__(mw, f"プロジェクト設定 — {mw.project.name or Path(mw.project_path or '無題').stem}")
        self.mw = mw
        st = mw.project.settings
        seq = mw.project.sequence
        fmt = st.get("format") or {"width": seq.width, "height": seq.height, "fps": seq.fps, "start_tc": seq.start_tc}
        f = self.page("形式（新しいシーケンス）")
        self.w = QSpinBox()
        self.w.setRange(16, 16384)
        self.w.setValue(int(fmt["width"]))
        self.h = QSpinBox()
        self.h.setRange(16, 16384)
        self.h.setValue(int(fmt["height"]))
        self.fps = QComboBox()
        for v in (23.976, 24, 25, 29.97, 30, 50, 59.94, 60):
            self.fps.addItem(f"{v:g} fps", v)
        self.fps.setCurrentIndex(max(0, self.fps.findData(float(fmt["fps"]))))
        self.tc = QLineEdit(fmt.get("start_tc", "01:00:00:00"))
        f.addRow("幅 × 高さ", self._row(self.w, QLabel("×"), self.h))
        f.addRow("フレームレート", self.fps)
        f.addRow("開始タイムコード", self.tc)
        conf = QPushButton("今のシーケンスの形式を変える...")
        conf.clicked.connect(lambda: self.mw.cmd_fns.get("conform_sequence", lambda: None)())
        f.addRow(conf)
        # テロップ
        f = self.page("テロップ")
        self.tmpl = QComboBox()
        self.tmpl.addItem("（ユーザー設定のまま）", "")
        for t in TP.telop_templates():
            self.tmpl.addItem(t["name"], t["name"])
        self.tmpl.setCurrentIndex(max(0, self.tmpl.findData(st.get("default_telop_template", ""))))
        f.addRow("このプロジェクトでテロップを足すときのテンプレート", self.tmpl)
        f.addRow(QLabel("共通のデザイン（フォント・縁取りなど）は テロップエディター → デザイン で"))
        # 書き出し
        f = self.page("書き出し")
        from . import mediacodecs as MC
        self.preset = QComboBox()
        self.preset.addItem("（前回と同じ）", "")
        for p in MC.PRESETS:
            if p.vcodec:
                self.preset.addItem(f"{p.group}: {p.label}", p.id)
        self.preset.setCurrentIndex(max(0, self.preset.findData(st.get("export_preset", ""))))
        self.folder = QLineEdit(st.get("export_folder", ""))
        b = QPushButton("...")
        b.clicked.connect(lambda: (lambda d: self.folder.setText(d) if d else None)(QFileDialog.getExistingDirectory(self, "書き出し先")))
        f.addRow("書き出しの形式", self.preset)
        f.addRow("書き出し先のフォルダ", self._row(self.folder, b))
        # メモ・作業時間
        f = self.page("メモ・作業時間")
        self.notes = QPlainTextEdit(st.get("notes", ""))
        f.addRow("メモ（納期・注意点など。プロジェクトに保存）", self.notes)
        sec = float(st.get("work_seconds", 0)) + mw.work_session_seconds()
        self.worked = QLabel(f"{int(sec // 3600)} 時間 {int(sec // 60 % 60)} 分（この PC で開いていて操作していた時間）")
        f.addRow("作業時間", self.worked)
        rst = QPushButton("作業時間を 0 に")
        rst.clicked.connect(self._reset_time)
        f.addRow(rst)
        # テンプレート
        f = self.page("テンプレート")
        f.addRow(QLabel(f"このプロジェクトの元にしたテンプレート: {st.get('template') or '（なし）'}"))
        sv = QPushButton("このプロジェクトをテンプレートとして保存...")
        sv.clicked.connect(lambda: self.mw.cmd_fns.get("project_template_save", lambda: None)())
        f.addRow(sv)
        f.addRow(QLabel("トラックの構成・ビン・テロップのデザイン・話者 / 立ち絵・プロジェクト設定・テロップのひな形が入ります（素材は入りません）。"))

    @staticmethod
    def _row(*ws):
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in ws:
            h.addWidget(x)
        return w

    def _reset_time(self):
        self.mw.project.settings["work_seconds"] = 0
        self.mw._work_t0 = time.time()
        self.mw._work_active = 0.0
        self.worked.setText("0 時間 0 分")

    def accept(self):
        st = self.mw.project.settings
        self.mw.begin_edit()
        st["format"] = {"width": self.w.value(), "height": self.h.value(), "fps": self.fps.currentData(),
                        "start_tc": self.tc.text().strip() or "01:00:00:00"}
        st["default_telop_template"] = self.tmpl.currentData()
        st["export_preset"] = self.preset.currentData()
        st["export_folder"] = self.folder.text().strip()
        st["notes"] = self.notes.toPlainText()
        self.mw.end_edit("プロジェクト設定")
        super().accept()


# ─────────────────────────── ライセンス ───────────────────────────
class LicenseDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("ライセンス")
        self.resize(640, 560)
        v = QVBoxLayout(self)
        self.head = QLabel("")
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self.key = QPlainTextEdit()
        self.key.setPlaceholderText("購入したときのライセンスキー（TELOCUT- で始まる文字列）を貼り付け")
        self.key.setFixedHeight(70)
        v.addWidget(self.key)
        h = QHBoxLayout()
        for txt, fn in (("登録", self._install), ("Pro を 14 日間試す", self._trial), ("購入ページを開く", self._buy),
                        ("登録を外す", self._remove)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addLayout(h)
        v.addWidget(QLabel("できること"))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["機能", "無料版", "Pro", "Studio"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        rows = [("編集・テロップ・テンプレートを使う・ゆっくり・自動切り抜き", "free"),
                ("H.264 / H.265 の書き出し（1080p まで）", "free")] + \
               [(lab, need) for _k, (need, lab) in LIC.FEATURES.items()]
        self.table.setRowCount(len(rows))
        for r, (lab, need) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(lab))
            for c, ed in enumerate(("free", "pro", "studio"), 1):
                ok = LIC.RANK[ed] >= LIC.RANK[need]
                it = QTableWidgetItem("✓" if ok else ("透かし付き" if lab.startswith("自動切り抜き") and ed == "free" else "—"))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, it)
        v.addWidget(self.table, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.refresh()

    def refresh(self):
        lic = LIC.current(refresh=True)
        txt = f"<b>今のエディション: {lic.label}</b>"
        if lic.source == "key":
            txt += f"<br>登録者: {lic.name}（{lic.email}）　席数 {lic.seats}"
            txt += f"<br>期限: {lic.expires or 'なし（買い切り）'}" + (f"　無料アップデート: {lic.updates_until} まで" if lic.updates_until else "")
        elif lic.source == "free":
            txt += "<br>Pro の機能を 14 日間お試しできます（1 回だけ）。"
        self.head.setText(txt)

    def _install(self):
        lic = LIC.install_key(self.key.toPlainText())
        if lic.error:
            QMessageBox.warning(self, "ライセンス", lic.error)
            return
        QMessageBox.information(self, "ライセンス", f"{LIC.NAMES[lic.edition]} を登録しました。ありがとうございます！")
        self.mw._update_title()
        self.refresh()

    def _trial(self):
        if getattr(SETTINGS, "trial_started", ""):
            QMessageBox.information(self, "お試し", "お試しはもう使っています（1 回だけです）。")
        LIC.start_trial()
        self.mw._update_title()
        self.refresh()

    def _buy(self):
        if LIC.PURCHASE_URL:
            import webbrowser
            webbrowser.open(LIC.PURCHASE_URL)
        else:
            QMessageBox.information(self, "購入", "販売ページはまだ準備中です。")

    def _remove(self):
        LIC.remove_key()
        self.mw._update_title()
        self.refresh()


# ─────────────────────────── テロップのテンプレート ───────────────────────────
class TelopTemplateDialog(QDialog):
    """テンプレートを選ぶ → 差し込む文字を入れる → 再生ヘッドに置く。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("テロップのテンプレート")
        self.resize(760, 520)
        v = QVBoxLayout(self)
        h = QHBoxLayout()
        self.cat = QComboBox()
        self.lst = QListWidget()
        self.lst.currentRowChanged.connect(lambda _r: self._show())
        self.lst.itemDoubleClicked.connect(lambda _it: self.place())
        left = QVBoxLayout()
        left.addWidget(self.cat)
        left.addWidget(self.lst, 1)
        h.addLayout(left, 1)
        right = QVBoxLayout()
        self.pv = QLabel("")
        self.pv.setFixedSize(384, 216)
        self.pv.setStyleSheet("background:#000")
        self.pv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self.pv)
        self.form = QFormLayout()
        right.addLayout(self.form)
        self.dur = QDoubleSpinBox()
        self.dur.setRange(0.2, 120)
        self.dur.setSuffix(" 秒")
        right.addWidget(QLabel("長さ"))
        right.addWidget(self.dur)
        right.addStretch(1)
        h.addLayout(right, 1)
        v.addLayout(h, 1)
        bb = QDialogButtonBox()
        self.go = bb.addButton("再生ヘッドに置く", QDialogButtonBox.ButtonRole.AcceptRole)
        dl = bb.addButton("このテンプレートを削除", QDialogButtonBox.ButtonRole.ActionRole)
        bb.addButton(QDialogButtonBox.StandardButton.Close)
        self.go.clicked.connect(self.place)
        dl.clicked.connect(self._delete)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.items = []
        self.edits = {}
        self.cat.currentIndexChanged.connect(lambda _i: self._fill())
        self.reload()

    def reload(self):
        self.all = TP.telop_templates()
        cats = ["すべて"] + sorted({t.get("category", "その他") for t in self.all}, key=lambda c: (c != "マイテンプレート", c))
        self.cat.blockSignals(True)
        self.cat.clear()
        self.cat.addItems(cats)
        self.cat.blockSignals(False)
        self._fill()

    def _fill(self):
        c = self.cat.currentText()
        self.items = [t for t in self.all if c in ("すべて", "") or t.get("category") == c]
        self.lst.clear()
        for t in self.items:
            self.lst.addItem(("★ " if t.get("_user") else "") + t["name"])
        if self.items:
            self.lst.setCurrentRow(0)

    def current(self) -> Optional[dict]:
        r = self.lst.currentRow()
        return self.items[r] if 0 <= r < len(self.items) else None

    def _show(self):
        t = self.current()
        while self.form.rowCount():
            self.form.removeRow(0)
        self.edits = {}
        if not t:
            return
        for ph in TP.placeholders(t):
            e = QLineEdit(ph)
            self.edits[ph] = e
            self.form.addRow(ph, e)
            e.textChanged.connect(lambda _s: self._preview())
        self.dur.setValue(float(t.get("duration", 3.0)))
        self._preview()

    def values(self):
        return {k: e.text() for k, e in self.edits.items()}

    def _preview(self):
        t = self.current()
        if not t:
            return
        try:
            from .media import DecoderPool
            from .render import render_frame_np
            import numpy as np
            from PyQt6.QtGui import QImage
            seq0 = self.mw.project.sequence
            p = M.Project(design=self.mw.project.design)
            s = p.sequence
            s.width, s.height, s.fps = seq0.width, seq0.height, seq0.fps
            s.video_tracks[0].clips = [M.Clip("bg", "color", 0, 9999, props={"color": "#3a4a5a"})]
            TP.apply_telop(s, t, 0, self.values(), self.dur.value())
            f = int(min(s.duration - 1, max(1, s.fps * 1.0)))
            arr = np.ascontiguousarray(render_frame_np(s, f, p, DecoderPool(), 384 / max(1, s.width)))
            img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format.Format_RGB888).copy()
            self.pv.setPixmap(QPixmap.fromImage(img))
        except Exception as ex:
            self.pv.setText(f"プレビューできません: {ex}")

    def place(self):
        t = self.current()
        if not t:
            return None
        mw = self.mw
        mw.begin_edit()
        made = TP.apply_telop(mw.project.sequence, t, mw.record_player.position, self.values(), self.dur.value())
        mw.end_edit(f"テンプレート「{t['name']}」")
        mw.canvas.selected = {c.id for c in made}
        mw.canvas.selection_changed.emit()
        return made

    def _delete(self):
        t = self.current()
        if not t or not t.get("_user"):
            QMessageBox.information(self, "テンプレート", "組み込みのテンプレートは消せません。")
            return
        TP.delete_template("telop", t["name"])
        self.reload()


# ─────────────────────────── MainWindow へ ───────────────────────────
class StudioMixin:
    def _studio_commands(self) -> dict:
        return {"user_settings": self.user_settings_dialog, "project_settings": self.project_settings_dialog,
                "license": self.license_dialog, "telop_template": self.telop_template_dialog,
                "telop_template_save": self.telop_template_save, "project_template_save": self.project_template_save,
                "biim_layout": self.biim_layout_dialog, "biim_split": self.biim_split}

    def _init_studio(self):
        # 作業時間（操作していた時間だけ数える）
        self._work_t0 = time.time()
        self._work_active = 0.0
        self._work_last_input = time.time()
        self._work_timer = QTimer(self)
        self._work_timer.setInterval(30_000)
        self._work_timer.timeout.connect(self._work_tick)
        self._work_timer.start()

    def _work_tick(self):
        if self.isActiveWindow() or QApplication.activeWindow() is not None:
            self._work_active += 30.0

    def work_session_seconds(self) -> float:
        return self._work_active

    def _flush_work_time(self):
        if self._work_active:
            self.project.settings["work_seconds"] = float(self.project.settings.get("work_seconds", 0)) + self._work_active
            self._work_active = 0.0

    def user_settings_dialog(self):
        dlg = UserSettingsDialog(self)
        dlg.show()
        self._user_dlg = dlg
        return dlg

    def project_settings_dialog(self):
        dlg = ProjectSettingsDialog(self)
        dlg.show()
        self._proj_dlg = dlg
        return dlg

    def license_dialog(self):
        dlg = LicenseDialog(self)
        dlg.show()
        self._lic_dlg = dlg
        return dlg

    def switch_user(self, name: str, copy_current: bool = False):
        """ユーザー設定を切り替える（配色・キーボードなどをすぐ反映）。"""
        SETTINGS.switch_profile(name, copy_current)
        TH.apply(QApplication.instance())
        from .commands import add_new_keys, preset_keymap
        self.apply_keymap(add_new_keys(dict(SETTINGS.keymap)) if SETTINGS.keymap else preset_keymap(SETTINGS.keyboard_preset))
        self._update_title()
        self.status.showMessage(f"ユーザー「{name}」に切り替えました", 5000)

    def telop_template_dialog(self):
        dlg = TelopTemplateDialog(self)
        dlg.show()
        self._tt_dlg = dlg
        return dlg

    def telop_template_save(self, name: str = ""):
        sel = list(self.canvas.selected_clips())
        if not any(c.kind in ("text", "shape") for _t, c in sel):
            QMessageBox.information(self, "テンプレートとして保存", "テロップ（と帯などの図形）を選んでください。")
            return None
        if not name:
            name, ok = QInputDialog.getText(self, "テンプレートとして保存", "名前（本文の {名前} は差し込む所になります）:")
            if not ok or not name.strip():
                return None
        t = TP.telop_from_clips(self.project.sequence, name.strip(), sel)
        path = TP.save_template("telop", t)
        self.status.showMessage(f"テンプレート「{name.strip()}」を保存しました", 5000)
        return path

    def project_template_save(self, name: str = ""):
        if not LIC.require(self, "template_save"):
            return None
        if not name:
            name, ok = QInputDialog.getText(self, "プロジェクトのテンプレート", "名前:", text=self.project.name or "")
            if not ok or not name.strip():
                return None
        path = TP.save_template("project", TP.project_template_from(self.project, name.strip()))
        self.status.showMessage(f"プロジェクトのテンプレート「{name.strip()}」を保存しました（新規作成で選べます）", 6000)
        return path

    def default_telop_template(self) -> Optional[dict]:
        name = self.project.settings.get("default_telop_template") or SETTINGS.default_telop_template
        return next((t for t in TP.telop_templates() if t["name"] == name), None) if name else None

    def biim_layout_dialog(self):
        from . import biim as B
        names = [lab for _k, lab in B.LAYOUTS]
        n, ok = QInputDialog.getItem(self, "素材の配置テンプレート", "配置:", names, 0, False)
        if ok:
            return self.biim_layout(B.LAYOUTS[names.index(n)][0])
        return None

    def biim_layout(self, kind: str):
        from . import biim as B
        sel = [c for _, c in self.canvas.selected_clips()]
        self.begin_edit()
        try:
            made = B.apply_layout(self.project, self.project.sequence, kind, sel, self.record_player.position)
        except ValueError as ex:
            self.end_edit(None)
            QMessageBox.information(self, "配置テンプレート", str(ex))
            return None
        self.end_edit("配置テンプレート: " + dict(B.LAYOUTS)[kind])
        msg = "配置しました。"
        if kind.startswith("biim"):
            msg += "「区間を追加」（メニュー）で、タイマーの下に区間名と区間タイムが出ます。"
            if not made.get("characters"):
                msg += " 立ち絵はキャラクターを登録すると置かれます。"
        self.status.showMessage(msg, 8000)
        return made

    def biim_split(self, name: str = ""):
        from . import biim as B
        if not name:
            name, ok = QInputDialog.getText(self, "区間を追加", "区間の名前（例: 1-1 草原）:")
            if not ok or not name.strip():
                return None
        self.begin_edit()
        mk = B.add_split(self.project.sequence, self.record_player.position, name.strip())
        self.end_edit(f"区間「{name.strip()}」")
        return mk
