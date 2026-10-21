"""立ち絵メーカーの画面（部品を選ぶ・目と口を割り当てる・まばたき / 口パクの設定・プレビュー・テンプレート）。"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter,
                             QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import tachie as T

PREVIEW_KEYS = [("closed", "口を閉じた顔"), ("half", "半開き"), ("open", "口を開けた顔"), ("blink", "まばたき（閉じ）"),
                ("blink_half", "まばたき（半目）")] + [(k, f"口「{lab}」") for k, lab in T.VOWELS] + [("anim", "▶ 動かして見る")]


def guess_name(path: str) -> str:
    """「ずんだもん立ち絵素材2.3」「春日部つむぎ立ち絵_公式_v2.0」→「ずんだもん」「春日部つむぎ」"""
    import re
    from pathlib import Path
    p = Path(path)
    name = p.name if p.is_dir() else p.stem
    for w in ("立ち絵素材", "立ち絵", "_公式", "公式", "素材"):
        name = name.replace(w, "")
    name = re.sub(r"[_\s\-]*v?\d+(\.\d+)*$", "", name).strip("_ -")
    return name or "キャラクター"


def _pix(im, h: int = 420) -> QPixmap:
    im = im.convert("RGBA")
    w = max(1, int(im.width * h / max(1, im.height)))
    im = im.resize((w, h))
    a = np.ascontiguousarray(np.asarray(im))
    q = QImage(a.data, a.shape[1], a.shape[0], a.shape[1] * 4, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(q)


class TachieMakerDialog(QDialog):
    """立ち絵の部品 → 目パチ・口パクの画像。テンプレートとして保存・タイムラインに置く。"""

    def __init__(self, mw, source: str = ""):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("立ち絵メーカー（目パチ・口パク）")
        self.resize(1180, 760)
        self.model: Optional[T.Model] = None
        self.state: Dict[str, object] = {}
        self.imgs: Dict[str, object] = {}
        self.character: Optional[dict] = None
        self._loading = False
        v = QVBoxLayout(self)
        # 読み込み・テンプレート
        top = QHBoxLayout()
        self.src = QLineEdit(source)
        self.src.setPlaceholderText("立ち絵の素材（PsdSplitter の JSON・PSD・PNG のフォルダ）")
        b1 = QPushButton("ファイル（JSON / PSD）…")
        b1.clicked.connect(self.pick_file)
        b2 = QPushButton("フォルダ…")
        b2.clicked.connect(self.pick_folder)
        top.addWidget(self.src, 1)
        top.addWidget(b1)
        top.addWidget(b2)
        top.addSpacing(16)
        top.addWidget(QLabel("テンプレート:"))
        self.tmpl = QComboBox()
        self.tmpl.setMinimumWidth(180)
        self.fill_templates()
        top.addWidget(self.tmpl)
        for txt, fn in (("使う", self.use_template), ("削除", self.delete_template), ("書き出し…", self.export_template),
                        ("読み込み…", self.import_template)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            top.addWidget(b)
        v.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        # 部品（衣装・表情）
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["部品（チェック = 表示。◉ は 1 つだけ選ぶ）"])
        self.tree.itemChanged.connect(self._item_changed)
        split.addWidget(self.tree)
        # プレビュー
        mid = QWidget()
        mv = QVBoxLayout(mid)
        self.pv_kind = QComboBox()
        for k, lab in PREVIEW_KEYS:
            self.pv_kind.addItem(lab, k)
        self.pv_kind.currentIndexChanged.connect(lambda _i: self.show_preview())
        mv.addWidget(self.pv_kind)
        self.preview = QLabel("素材を読み込んでください")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(360, 440)
        self.preview.setStyleSheet("background: #2a2a33;")
        mv.addWidget(self.preview, 1)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        mv.addWidget(self.msg)
        split.addWidget(mid)
        # 目・口・設定
        right = QWidget()
        rv = QVBoxLayout(right)
        ge = QGroupBox("目パチ")
        fe = QFormLayout(ge)
        self.eye_group = QComboBox()
        self.eye_open, self.eye_half, self.eye_closed = QComboBox(), QComboBox(), QComboBox()
        self.eye_group.currentIndexChanged.connect(lambda _i: self._fill_options("eye"))
        fe.addRow("目のグループ", self.eye_group)
        fe.addRow("開いた目", self.eye_open)
        fe.addRow("半目（任意）", self.eye_half)
        fe.addRow("閉じた目", self.eye_closed)
        self.blink_int = QDoubleSpinBox()
        self.blink_int.setRange(1.0, 20.0)
        self.blink_int.setSuffix(" 秒ごと")
        self.blink_jit = QDoubleSpinBox()
        self.blink_jit.setRange(0.0, 10.0)
        self.blink_jit.setSuffix(" 秒のばらつき")
        self.blink_frames = QSpinBox()
        self.blink_frames.setRange(1, 15)
        self.blink_frames.setSuffix(" コマ閉じる")
        self.double = QDoubleSpinBox()
        self.double.setRange(0.0, 1.0)
        self.double.setSingleStep(0.05)
        fe.addRow("まばたきの間隔", self.blink_int)
        fe.addRow("", self.blink_jit)
        fe.addRow("閉じている長さ", self.blink_frames)
        fe.addRow("2 回続けてまばたき", self.double)
        rv.addWidget(ge)
        gm = QGroupBox("口パク")
        fm = QFormLayout(gm)
        self.mouth_group = QComboBox()
        self.mouth_group.currentIndexChanged.connect(lambda _i: self._fill_options("mouth"))
        fm.addRow("口のグループ", self.mouth_group)
        self.mouth: Dict[str, QComboBox] = {}
        for key, lab in [("closed", "閉じた口"), ("half", "半開き"), ("open", "開いた口")] + [(k, f"「{l}」の口") for k, l in T.VOWELS]:
            cb = QComboBox()
            self.mouth[key] = cb
            fm.addRow(lab, cb)
        self.open_db, self.half_db = QDoubleSpinBox(), QDoubleSpinBox()
        for s in (self.open_db, self.half_db):
            s.setRange(-80.0, 0.0)
            s.setSuffix(" dB")
        fm.addRow("開く音の大きさ", self.open_db)
        fm.addRow("半開きの音の大きさ", self.half_db)
        self.vowel = QCheckBox("母音（あいうえお）に合わせる（音声合成のセリフ）")
        self.bounce = QCheckBox("しゃべると少し揺れる")
        fm.addRow(self.vowel)
        fm.addRow(self.bounce)
        rv.addWidget(gm)
        for w in [self.eye_open, self.eye_half, self.eye_closed] + list(self.mouth.values()):
            w.currentIndexChanged.connect(lambda _i: self._schedule())
        rv.addStretch(1)
        split.addWidget(right)
        split.setSizes([300, 460, 400])
        v.addWidget(split, 1)
        bot = QHBoxLayout()
        bot.addWidget(QLabel("名前"))
        self.name = QLineEdit()
        bot.addWidget(self.name, 1)
        self.save_t = QPushButton("テンプレートとして保存")
        self.save_t.clicked.connect(self.save_template)
        self.make = QPushButton("キャラクターを作る")
        self.make.clicked.connect(lambda: self.create(False))
        self.place = QPushButton("作ってタイムラインに置く")
        self.place.clicked.connect(lambda: self.create(True))
        close = QPushButton("閉じる")
        close.clicked.connect(self.reject)
        for b in (self.save_t, self.make, self.place, close):
            bot.addWidget(b)
        v.addLayout(bot)
        self._set_params(T.DEFAULT_PARAMS)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.rebuild)
        self._anim = QTimer(self)
        self._anim.setInterval(120)
        self._anim.timeout.connect(self._anim_tick)
        self._anim_i = 0
        if source:
            QTimer.singleShot(0, lambda: self.load(source))

    # ── 読み込み ──
    def pick_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "立ち絵の素材", self.src.text(), "立ち絵 (*.json *.psd *.psb)")
        if p:
            self.load(p)

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "立ち絵の素材のフォルダ", self.src.text())
        if d:
            self.load(d)

    def load(self, path: str) -> bool:
        self.src.setText(path)
        try:
            self.model = T.load(path)
        except Exception as ex:
            self.model = None
            self.msg.setText(f"読み込めません: {ex}")
            return False
        self.state = self.model.default_state()
        if not self.name.text():
            self.name.setText(guess_name(path))
        self.fill_tree()
        self.fill_roles(T.guess_roles(self.model, self.state))
        self.msg.setText(f"{self.model.kind.upper()}・{self.model.canvas[0]}×{self.model.canvas[1]}"
                         + ("\n" + "\n".join(self.model.warnings[:3]) if self.model.warnings else ""))
        self.rebuild()
        return True

    def fill_tree(self):
        self._loading = True
        self.tree.clear()

        def add(parent_item, node: T.Part):
            for c in node.children:
                label = ("◉ " if c.radio else "") + c.label + ("（いつも）" if c.force else "")
                it = QTreeWidgetItem([label])
                it.setData(0, Qt.ItemDataRole.UserRole, c.path)
                if not c.force:
                    it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    if c.radio:
                        on = self.state.get("sel:" + node.path) == c.path
                    else:
                        on = bool(self.state.get(c.path, c.visible))
                    it.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                (parent_item.addChild(it) if parent_item is not None else self.tree.addTopLevelItem(it))
                if c.children:
                    add(it, c)
        add(None, self.model.root)
        self.tree.expandToDepth(0)
        self._loading = False

    def _item_changed(self, it: QTreeWidgetItem, _col: int):
        if self._loading or self.model is None:
            return
        part = self.model.find(it.data(0, Qt.ItemDataRole.UserRole))
        if part is None:
            return
        on = it.checkState(0) == Qt.CheckState.Checked
        if part.radio:
            parent = part.path.rsplit("/", 1)[0] if "/" in part.path else ""
            if on:
                self.state["sel:" + parent] = part.path
            self._loading = True                       # ほかの ◉ のチェックを外す
            par_item = it.parent()
            sibs = [par_item.child(i) for i in range(par_item.childCount())] if par_item else \
                   [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
            for s in sibs:
                sp = self.model.find(s.data(0, Qt.ItemDataRole.UserRole))
                if sp is not None and sp.radio and s is not it:
                    s.setCheckState(0, Qt.CheckState.Unchecked)
            if not on:
                it.setCheckState(0, Qt.CheckState.Checked)       # ◉ は外せない（別のものを選ぶ）
            self._loading = False
        else:
            self.state[part.path] = on
        self._schedule()

    # ── 目と口 ──
    def fill_roles(self, roles: dict):
        self._loading = True
        groups = self.model.groups() if self.model else []
        for cb, key in ((self.eye_group, "eye_group"), (self.mouth_group, "mouth_group")):
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("（なし）", "")
            for g in groups:
                cb.addItem(g.path, g.path)
            cb.setCurrentIndex(max(0, cb.findData(roles.get(key, ""))))
            cb.blockSignals(False)
        self._fill_options("eye", roles.get("eye", {}))
        self._fill_options("mouth", roles.get("mouth", {}))
        self._loading = False

    def _fill_options(self, which: str, chosen: Optional[dict] = None):
        g = self.model.find((self.eye_group if which == "eye" else self.mouth_group).currentData() or "") if self.model else None
        opts = [c for c in (g.children if g else []) if c.radio]
        boxes = {"open": self.eye_open, "half": self.eye_half, "closed": self.eye_closed} if which == "eye" else self.mouth
        for key, cb in boxes.items():
            cur = (chosen or {}).get(key, cb.currentData())
            cb.blockSignals(True)
            cb.clear()
            cb.addItem("（なし）", "")
            for o in opts:
                cb.addItem(o.label, o.path)
            cb.setCurrentIndex(max(0, cb.findData(cur)))
            cb.blockSignals(False)
        self._schedule()

    def roles(self) -> dict:
        return {"eye_group": self.eye_group.currentData() or "", "mouth_group": self.mouth_group.currentData() or "",
                "eye": {"open": self.eye_open.currentData() or "", "half": self.eye_half.currentData() or "",
                        "closed": self.eye_closed.currentData() or ""},
                "mouth": {k: cb.currentData() or "" for k, cb in self.mouth.items()}}

    def params(self) -> dict:
        return {"blink_interval": self.blink_int.value(), "blink_jitter": self.blink_jit.value(),
                "blink_frames": self.blink_frames.value(), "double_blink": self.double.value(),
                "open_db": self.open_db.value(), "half_db": self.half_db.value(),
                "vowel_sync": self.vowel.isChecked(), "bounce": self.bounce.isChecked()}

    def _set_params(self, p: dict):
        p = {**T.DEFAULT_PARAMS, **(p or {})}
        self.blink_int.setValue(float(p["blink_interval"]))
        self.blink_jit.setValue(float(p["blink_jitter"]))
        self.blink_frames.setValue(int(p["blink_frames"]))
        self.double.setValue(float(p["double_blink"]))
        self.open_db.setValue(float(p["open_db"]))
        self.half_db.setValue(float(p["half_db"]))
        self.vowel.setChecked(bool(p["vowel_sync"]))
        self.bounce.setChecked(bool(p["bounce"]))

    # ── 作る・見る ──
    def _schedule(self):
        if not self._loading:
            self._timer.start()

    def rebuild(self) -> Dict[str, object]:
        if self.model is None:
            return {}
        try:
            self.imgs = T.build_images(self.model, self.state, self.roles())
        except Exception as ex:
            self.imgs = {}
            self.msg.setText(str(ex))
        self.show_preview()
        return self.imgs

    def show_preview(self):
        k = self.pv_kind.currentData()
        if k == "anim":
            self._anim.start()
            return
        self._anim.stop()
        im = self.imgs.get(k) or self.imgs.get("closed")
        if im is not None:
            self.preview.setPixmap(_pix(im, max(200, self.preview.height() - 10)))

    def _anim_tick(self):
        """口パク（閉 → 半 → 開 → 半）とまばたきを順に見せる。"""
        seq = ["closed", "half", "open", "half", "closed", "closed", "blink_half", "blink", "blink_half", "closed"]
        self._anim_i = (self._anim_i + 1) % len(seq)
        k = seq[self._anim_i]
        im = self.imgs.get(k) or self.imgs.get("blink" if k.startswith("blink") else "open") or self.imgs.get("closed")
        if im is not None:
            self.preview.setPixmap(_pix(im, max(200, self.preview.height() - 10)))

    def create(self, place: bool) -> Optional[dict]:
        if not self.imgs:
            self.rebuild()
        if not self.imgs:
            QMessageBox.warning(self, "立ち絵メーカー", self.msg.text() or "画像を作れませんでした")
            return None
        ch = T.save_character(self.name.text().strip() or "キャラクター", self.imgs, self.params(), self.src.text())
        self.character = ch
        self.mw.add_character(ch, place)
        self.accept()
        return ch

    # ── テンプレート ──
    def fill_templates(self):
        self.tmpl.clear()
        for t in T.templates():
            self.tmpl.addItem(t["name"], t)

    def save_template(self):
        if not self.imgs:
            self.rebuild()
        if not self.imgs:
            return None
        name, ok = QInputDialog.getText(self, "テンプレートとして保存", "テンプレートの名前（例: ずんだもん 通常 / ずんだもん 怒り）:",
                                        text=self.name.text())
        if not ok or not name.strip():
            return None
        d = T.save_template(name.strip(), self.model, self.state, self.roles(), self.params(), self.imgs)
        self.fill_templates()
        self.tmpl.setCurrentIndex(max(0, self.tmpl.findText(name.strip())))
        self.msg.setText(f"テンプレートを保存しました: {d}")
        return d

    def use_template(self) -> Optional[dict]:
        """テンプレートを読み込む: 元の素材があれば部品・割り当てを戻して直せる。無ければそのまま作れる画像を使う。"""
        t = self.tmpl.currentData()
        if not t:
            return None
        self.name.setText(t["name"])
        self._set_params(t.get("params", {}))
        if t.get("source") and self.load(t["source"]):
            self.state.update(t.get("state", {}))
            self.fill_tree()
            self.fill_roles(t.get("roles", {}))
            self.rebuild()
        else:
            from PIL import Image
            from pathlib import Path
            self.model = None
            self.imgs = {k: Image.open(Path(t["dir"]) / f"{k}.png").convert("RGBA") for k in t.get("images", [])
                         if (Path(t["dir"]) / f"{k}.png").exists()}
            self.msg.setText("元の素材が見つからないので、テンプレートの画像を使います（部品の変更はできません）")
            self.show_preview()
        return t

    def delete_template(self):
        t = self.tmpl.currentData()
        if t and QMessageBox.question(self, "テンプレートを削除", f"「{t['name']}」を削除しますか？") == QMessageBox.StandardButton.Yes:
            T.delete_template(t["name"])
            self.fill_templates()

    def export_template(self):
        t = self.tmpl.currentData()
        if not t:
            return
        p, _ = QFileDialog.getSaveFileName(self, "テンプレートを書き出し", f"{t['name']}.zip", "zip (*.zip)")
        if p:
            self.msg.setText(f"書き出しました: {T.export_template(t['name'], p)}")

    def import_template(self):
        p, _ = QFileDialog.getOpenFileName(self, "テンプレートを読み込み", "", "zip (*.zip)")
        if p:
            try:
                name = T.import_template(p)
            except Exception as ex:
                QMessageBox.warning(self, "テンプレート", str(ex))
                return
            self.fill_templates()
            self.tmpl.setCurrentIndex(max(0, self.tmpl.findText(name)))
