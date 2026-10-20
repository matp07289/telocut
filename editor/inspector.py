"""インスペクタ（エフェクトコントロール）。選択クリップのプロパティを編集。"""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
                             QDoubleSpinBox, QSpinBox, QPlainTextEdit, QGroupBox, QPushButton,
                             QComboBox, QScrollArea, QColorDialog, QCheckBox)

from core.timecode import frames_to_timecode
from .theme import C

KEY_PROPS = [("scale", "スケール", 100, "%"), ("pos_x", "位置 X", 1, "px"), ("pos_y", "位置 Y", 1, "px"),
             ("rotation", "回転", 1, "°"), ("opacity", "不透明度", 100, "%")]
EASE_CHOICES = [("linear", "直線"), ("ease_in_out", "イーズイン・アウト"), ("ease_out", "イーズアウト"),
                ("ease_in", "イーズイン"), ("back", "バック"), ("hold", "停止（次まで固定）")]


def set_key(keys: list, rel: int, value, ease_name: str = "linear") -> list:
    """rel フレームのキーを追加 / 置き換え（同じフレームなら値を更新）。"""
    out = [k for k in keys if int(k[0]) != int(rel)]
    old = next((k for k in keys if int(k[0]) == int(rel)), None)
    out.append([int(rel), value, (old[2] if old and len(old) > 2 else ease_name)])
    return sorted(out, key=lambda k: k[0])


FX_NAMES = {"mono": "白黒", "sepia": "セピア", "brightness": "明るさ", "contrast": "コントラスト",
            "saturation": "彩度", "invert": "色反転"}


class ColorPick(QPushButton):
    def __init__(self, on_change):
        super().__init__()
        self._color = "#FFFFFF"
        self._cb = on_change
        self.setFixedWidth(60)
        self.clicked.connect(self._pick)

    def set(self, c: str):
        self._color = c or "#FFFFFF"
        self.setStyleSheet(f"background: {self._color}; border: 1px solid {C['border']};")

    def _pick(self):
        c = QColorDialog.getColor(QColor(self._color), self, "色を選択")
        if c.isValid():
            self.set(c.name())
            self._cb(c.name())


class Inspector(QScrollArea):
    def __init__(self, mw, parent=None):
        super().__init__(parent)
        self.mw = mw
        self.setWidgetResizable(True)
        self._loading = False
        self._clips = []
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(500)
        self._commit_timer.timeout.connect(self._commit)
        self._editing = False

        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(6, 6, 6, 6)
        self.header = QLabel("クリップを選択してください")
        self.header.setWordWrap(True)
        self.header.setStyleSheet("font-weight: bold; background: transparent;")
        lay.addWidget(self.header)
        self.sub = QLabel("")
        self.sub.setObjectName("info")
        lay.addWidget(self.sub)

        # テロップ
        self.g_text = QGroupBox("テロップ")
        ft = QVBoxLayout(self.g_text)
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(70)
        self.text.setPlaceholderText("テロップの文字（改行可）")
        self.text.textChanged.connect(lambda: self._set_prop("text", self.text.toPlainText()))
        ft.addWidget(self.text)
        f2 = QFormLayout()
        self.t_size = QSpinBox()
        self.t_size.setRange(0, 400)
        self.t_size.setSpecialValueText("共通")
        self.t_size.valueChanged.connect(lambda v: self._set_style("font_size", v or None))
        f2.addRow("文字サイズ", self.t_size)
        crow = QHBoxLayout()
        self.t_color = ColorPick(lambda c: self._set_style("font_color", c))
        self.t_stroke = ColorPick(lambda c: self._set_style("stroke_color", c))
        crow.addWidget(QLabel("文字"))
        crow.addWidget(self.t_color)
        crow.addWidget(QLabel("縁"))
        crow.addWidget(self.t_stroke)
        crow.addStretch(1)
        f2.addRow("色", crow)
        self.t_pos = QComboBox()
        self.t_pos.addItems(["共通", "上", "中央", "下"])
        self.t_pos.currentIndexChanged.connect(
            lambda i: self._set_style("position_v", [None, "top", "middle", "bottom"][i]))
        f2.addRow("位置", self.t_pos)
        self.t_preset = QComboBox()
        self.t_preset.activated.connect(self._apply_preset)
        f2.addRow("プリセット", self.t_preset)
        rb = QPushButton("個別スタイルを解除")
        rb.clicked.connect(lambda: self._set_prop("style", {}))
        f2.addRow("", rb)
        ft.addLayout(f2)
        lay.addWidget(self.g_text)

        # モーション
        self.g_motion = QGroupBox("モーション")
        fm = QFormLayout(self.g_motion)
        self.pos_x = self._dspin(-10000, 10000, 1, 0, "px")
        self.pos_y = self._dspin(-10000, 10000, 1, 0, "px")
        self.scale = self._dspin(1, 1000, 1, 0, "%")
        self.rot = self._dspin(-360, 360, 1, 1, "°")
        self.pos_x.valueChanged.connect(lambda v: self._set_prop("pos_x", v))
        self.pos_y.valueChanged.connect(lambda v: self._set_prop("pos_y", v))
        self.scale.valueChanged.connect(lambda v: self._set_prop("scale", v / 100))
        self.rot.valueChanged.connect(lambda v: self._set_prop("rotation", v))
        fm.addRow("位置 X", self.pos_x)
        fm.addRow("位置 Y", self.pos_y)
        fm.addRow("スケール", self.scale)
        fm.addRow("回転", self.rot)
        lay.addWidget(self.g_motion)

        # 不透明度・フェード
        self.g_op = QGroupBox("不透明度 / フェード")
        fo = QFormLayout(self.g_op)
        self.opacity = self._dspin(0, 100, 1, 0, "%")
        self.opacity.valueChanged.connect(lambda v: self._set_prop("opacity", v / 100))
        fo.addRow("不透明度", self.opacity)
        from .render import BLEND_MODES
        self.blend = QComboBox()
        for k, lab in BLEND_MODES:
            self.blend.addItem(lab, k)
        self.blend.setToolTip("下のレイヤーとの合成方法（AE / Premiere / AviUtl の描画モード）")
        self.blend.currentIndexChanged.connect(lambda _i: self._set_prop("blend", self.blend.currentData()))
        fo.addRow("ブレンドモード", self.blend)
        self.fade_in = QSpinBox()
        self.fade_in.setRange(0, 100000)
        self.fade_in.setSuffix(" F")
        self.fade_in.valueChanged.connect(lambda v: self._set_prop("fade_in", v))
        self.fade_out = QSpinBox()
        self.fade_out.setRange(0, 100000)
        self.fade_out.setSuffix(" F")
        self.fade_out.valueChanged.connect(lambda v: self._set_prop("fade_out", v))
        fo.addRow("フェードイン", self.fade_in)
        fo.addRow("フェードアウト", self.fade_out)
        lay.addWidget(self.g_op)

        # キーフレーム（After Effects 風）
        self.g_keys = QGroupBox("キーフレーム")
        fkk = QFormLayout(self.g_keys)
        self.key_prop = QComboBox()
        for k, lab, _m, _s in KEY_PROPS:
            self.key_prop.addItem(lab, k)
        self.key_prop.currentIndexChanged.connect(lambda _i: self._load_keys())
        self.key_ease = QComboBox()
        for k, lab in EASE_CHOICES:
            self.key_ease.addItem(lab, k)
        fkk.addRow("プロパティ", self.key_prop)
        fkk.addRow("次のキーまでの動き", self.key_ease)
        kr = QHBoxLayout()
        for txt, tip, fn in (("◆ 追加", "再生ヘッドの位置に、今の値でキーフレームを打つ", self.add_key),
                             ("◇ 削除", "再生ヘッドの位置のキーフレームを消す", self.remove_key),
                             ("◀", "前のキーフレームへ", lambda: self.jump_key(-1)),
                             ("▶", "次のキーフレームへ", lambda: self.jump_key(1)),
                             ("全消去", "このプロパティのキーフレームをすべて消す", self.clear_keys)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            kr.addWidget(b)
        fkk.addRow(kr)
        self.key_info = QLabel("")
        self.key_info.setObjectName("info")
        self.key_info.setWordWrap(True)
        fkk.addRow(self.key_info)
        lay.addWidget(self.g_keys)

        # 音声
        self.g_audio = QGroupBox("オーディオ")
        fa = QFormLayout(self.g_audio)
        self.vol = self._dspin(-60, 24, 0.5, 1, "dB")
        self.vol.valueChanged.connect(lambda v: self._set_prop("volume_db", v))
        fa.addRow("音量", self.vol)
        self.pan = self._dspin(-100, 100, 5, 0, "")
        self.pan.setToolTip("−100 = 左 / 0 = 中央 / 100 = 右")
        self.pan.valueChanged.connect(lambda v: self._set_prop("pan", v / 100))
        fa.addRow("パン", self.pan)
        lay.addWidget(self.g_audio)

        # カラーマット
        self.g_color = QGroupBox("カラーマット")
        fc = QFormLayout(self.g_color)
        self.matte = ColorPick(lambda c: self._set_prop("color", c))
        fc.addRow("色", self.matte)
        lay.addWidget(self.g_color)

        # 速度
        self.g_speed = QGroupBox("速度")
        fsp = QFormLayout(self.g_speed)
        self.speed = self._dspin(1, 1000, 5, 0, "%")
        self.speed.valueChanged.connect(lambda _v: self._set_speed())
        fsp.addRow("再生速度", self.speed)
        sr = QHBoxLayout()
        self.reverse = QCheckBox("逆再生")
        self.reverse.toggled.connect(lambda _on: self._set_speed())
        self.freeze = QCheckBox("フリーズフレーム")
        self.freeze.toggled.connect(lambda _on: self._set_speed())
        sr.addWidget(self.reverse)
        sr.addWidget(self.freeze)
        sr.addStretch(1)
        fsp.addRow(sr)
        note = QLabel("速度を変えてもクリップの長さはそのまま（使う素材の範囲が変わります）")
        note.setObjectName("info")
        note.setWordWrap(True)
        fsp.addRow(note)
        lay.addWidget(self.g_speed)

        # クロップ
        self.g_crop = QGroupBox("クロップ")
        fcr = QFormLayout(self.g_crop)
        self.crop = []
        for lab in ("左", "上", "右", "下"):
            sp_ = self._dspin(0, 49, 1, 0, "%")
            sp_.valueChanged.connect(lambda _v: self._set_prop("crop", [x.value() for x in self.crop]))
            self.crop.append(sp_)
            fcr.addRow(lab, sp_)
        lay.addWidget(self.g_crop)

        # クロマキー
        self.g_key = QGroupBox("クロマキー（グリーンバック）")
        fk = QFormLayout(self.g_key)
        self.key_on = QCheckBox("有効")
        self.key_on.toggled.connect(lambda _on: self._set_key())
        fk.addRow(self.key_on)
        self.key_col = ColorPick(lambda c: self._set_key())
        fk.addRow("キー色", self.key_col)
        self.key_tol = self._dspin(1, 100, 1, 0, "%")
        self.key_tol.valueChanged.connect(lambda _v: self._set_key())
        fk.addRow("許容量", self.key_tol)
        self.key_soft = self._dspin(1, 100, 1, 0, "%")
        self.key_soft.valueChanged.connect(lambda _v: self._set_key())
        fk.addRow("境界のぼかし", self.key_soft)
        lay.addWidget(self.g_key)

        # アニメーション（開始 → 終了）
        self.g_anim = QGroupBox("アニメーション（開始 → 終了）")
        fan = QFormLayout(self.g_anim)
        self.anim_on = QCheckBox("有効（Ken Burns・スライドイン等）")
        self.anim_on.toggled.connect(lambda _on: self._set_anim())
        fan.addRow(self.anim_on)
        self.anim = {}
        for key, lab, lo, hi, suf in (("scale", "スケール", 1, 1000, "%"), ("pos_x", "位置 X", -10000, 10000, "px"),
                                      ("pos_y", "位置 Y", -10000, 10000, "px"), ("opacity", "不透明度", 0, 100, "%")):
            r = QHBoxLayout()
            a0 = self._dspin(lo, hi, 1, 0, suf)
            a1 = self._dspin(lo, hi, 1, 0, suf)
            a0.valueChanged.connect(lambda _v: self._set_anim())
            a1.valueChanged.connect(lambda _v: self._set_anim())
            r.addWidget(a0)
            r.addWidget(QLabel("→"))
            r.addWidget(a1)
            self.anim[key] = (a0, a1)
            fan.addRow(lab, r)
        er = QHBoxLayout()
        self.anim_ease = QComboBox()
        for k, lab in (("linear", "直線"), ("ease_out", "イーズアウト（だんだん遅く）"), ("ease_in", "イーズイン（だんだん速く）"),
                       ("ease_in_out", "イーズイン・アウト"), ("back", "バック（行き過ぎて戻る）"), ("bounce", "バウンス")):
            self.anim_ease.addItem(lab, k)
        self.anim_ease.currentIndexChanged.connect(lambda _i: self._set_anim())
        self.anim_len = QSpinBox()
        self.anim_len.setRange(0, 100000)
        self.anim_len.setSpecialValueText("クリップ全体")
        self.anim_len.setSuffix(" f")
        self.anim_len.valueChanged.connect(lambda _v: self._set_anim())
        er.addWidget(self.anim_ease, 1)
        er.addWidget(self.anim_len)
        fan.addRow("イージング", er)
        lay.addWidget(self.g_anim)

        # クリップカラー
        self.g_label = QGroupBox("クリップカラー")
        fl = QHBoxLayout(self.g_label)
        for col in ("", "#d8463c", "#e5902a", "#e5c02a", "#3fae5a", "#2fa3b5", "#4f6fe0", "#9a6ae0", "#c050a0"):
            b = QPushButton("✕" if not col else "")
            b.setFixedSize(22, 22)
            if col:
                b.setStyleSheet(f"background: {col}; border: 1px solid #222;")
            b.setToolTip("既定の色" if not col else col)
            b.clicked.connect(lambda _=False, col=col: self._set_prop("label", col))
            fl.addWidget(b)
        fl.addStretch(1)
        lay.addWidget(self.g_label)

        # エフェクト一覧
        self.g_fx = QGroupBox("エフェクト")
        self.fx_outer = QVBoxLayout(self.g_fx)
        mr = QHBoxLayout()
        mr.addWidget(QLabel("管理方法"))
        self.fx_mode = QComboBox()
        self.fx_mode.addItem("レイヤー（上から順に掛ける）", "stack")
        self.fx_mode.addItem("ノード（分岐・合成）", "nodes")
        self.fx_mode.currentIndexChanged.connect(self._fx_mode_changed)
        mr.addWidget(self.fx_mode, 1)
        ne = QPushButton("ノードエディター")
        ne.clicked.connect(lambda: self.mw.show_node_editor() if hasattr(self.mw, "show_node_editor") else None)
        mr.addWidget(ne)
        self.fx_outer.addLayout(mr)
        self.fx_lay = QVBoxLayout()
        self.fx_outer.addLayout(self.fx_lay)
        lay.addWidget(self.g_fx)
        lay.addStretch(1)
        self.load([])

    def _dspin(self, lo, hi, step, dec, suffix):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(dec)
        s.setSuffix(" " + suffix)
        s.setKeyboardTracking(False)
        return s

    # ── 読み込み ─────────────────────────────
    def load(self, clips):
        self._commit_now()
        self._clips = [c for c in clips]
        self._loading = True
        try:
            for g in (self.g_text, self.g_motion, self.g_op, self.g_audio, self.g_color, self.g_fx,
                      self.g_speed, self.g_crop, self.g_key, self.g_anim, self.g_label, self.g_keys):
                g.setVisible(False)
            if not clips:
                self.header.setText("クリップを選択してください")
                self.sub.setText("タイムラインでクリップを選ぶと、位置・スケール・不透明度・\n"
                                 "フェード・音量・テロップの文字などを編集できます")
                return
            c = clips[0]
            seq = self.mw.project.sequence
            kinds = {x.kind for x in clips}
            name = {"video": "ビデオ", "audio": "オーディオ", "image": "静止画",
                    "text": "テロップ", "color": "カラーマット"}
            title = (c.name or c.prop("text", "")).replace("\n", " ")[:40]
            self.header.setText(f"{name.get(c.kind, c.kind)}: {title}" if len(clips) == 1
                                else f"{len(clips)} 個のクリップ（まとめて変更）")
            off = self.mw.tc_offset(seq)
            self.sub.setText(f"開始 {frames_to_timecode(c.start + off, seq.fps)}   "
                             f"長さ {frames_to_timecode(c.duration, seq.fps)}")
            visual = kinds & {"video", "image", "text", "color", "character", "shape", "adjust", "null", "camera"}
            if visual:
                from .render import prop_at
                f = self.mw.record_player.position
                pv = lambda k, d: float(prop_at(c, k, f) if prop_at(c, k, f) is not None else d)
                self.g_motion.setVisible(c.kind != "adjust")
                self.g_op.setVisible(True)
                self.g_keys.setVisible(True)
                self.pos_x.setValue(pv("pos_x", 0))
                self.pos_y.setValue(pv("pos_y", 0))
                self.scale.setValue(pv("scale", 1.0) * 100)
                self.rot.setValue(pv("rotation", 0))
                self.opacity.setValue(pv("opacity", 1.0) * 100)
                self.blend.setCurrentIndex(max(0, self.blend.findData(c.prop("blend") or "normal")))
                self._load_keys()
            if "audio" in kinds:
                self.g_audio.setVisible(True)
                self.g_op.setVisible(True)
                self.vol.setValue(float(c.prop("volume_db", 0.0)) if c.kind == "audio" else
                                  float(next(x for x in clips if x.kind == "audio").prop("volume_db", 0.0)))
            self.fade_in.setValue(int(c.prop("fade_in", 0) or 0))
            self.fade_out.setValue(int(c.prop("fade_out", 0) or 0))
            if kinds == {"text"}:
                self.g_text.setVisible(True)
                if len(clips) == 1:
                    if self.text.toPlainText() != c.prop("text", ""):
                        self.text.setPlainText(c.prop("text", ""))
                    self.text.setEnabled(True)
                else:
                    self.text.setPlainText("（複数選択中: 文字は個別に編集）")
                    self.text.setEnabled(False)
                st = c.prop("style") or {}
                d = self.mw.text_design(c)
                self.t_size.setValue(int(st.get("font_size", 0) or 0))
                self.t_color.set(st.get("font_color", d.font_color))
                self.t_stroke.set(st.get("stroke_color", d.stroke_color))
                self.t_pos.setCurrentIndex({"top": 1, "middle": 2, "bottom": 3}.get(st.get("position_v"), 0))
                self.t_preset.clear()
                self.t_preset.addItem("（DavinTelop プリセットを適用）")
                try:
                    from core.preset import PresetManager
                    self.t_preset.addItems(PresetManager().list_presets())
                except Exception:
                    pass
            if kinds == {"color"}:
                self.g_color.setVisible(True)
                self.matte.set(c.prop("color", "#000000"))
            self.g_label.setVisible(True)
            if kinds & {"video", "audio"}:
                self.g_speed.setVisible(True)
                sp = float(c.prop("speed", 1.0) or 0.0)
                self.speed.setValue(abs(sp) * 100 if sp else 100)
                self.reverse.setChecked(sp < 0)
                self.freeze.setChecked(sp == 0)
            if "audio" in kinds:
                ac = c if c.kind == "audio" else next(x for x in clips if x.kind == "audio")
                self.pan.setValue(float(ac.prop("pan", 0.0) or 0.0) * 100)
            if visual:
                self.g_anim.setVisible(True)
                an = c.prop("anim") or {}
                self.anim_on.setChecked(bool(an))
                defaults = {"scale": (float(c.prop("scale", 1.0)) * 100, float(c.prop("scale", 1.0)) * 115),
                            "pos_x": (float(c.prop("pos_x", 0)), float(c.prop("pos_x", 0))),
                            "pos_y": (float(c.prop("pos_y", 0)), float(c.prop("pos_y", 0))),
                            "opacity": (float(c.prop("opacity", 1.0)) * 100, float(c.prop("opacity", 1.0)) * 100)}
                i = self.anim_ease.findData(an.get("_ease", "linear"))
                self.anim_ease.setCurrentIndex(max(0, i))
                self.anim_len.setValue(int(an.get("_len", 0) or 0))
                for key, (a0, a1) in self.anim.items():
                    v = an.get(key)
                    if v:
                        mul = 100 if key in ("scale", "opacity") else 1
                        a0.setValue(float(v[0]) * mul)
                        a1.setValue(float(v[1]) * mul)
                    else:
                        a0.setValue(defaults[key][0])
                        a1.setValue(defaults[key][1])
            if kinds & {"video", "image"}:
                self.g_crop.setVisible(True)
                cr = c.prop("crop") or [0, 0, 0, 0]
                for sp_, v in zip(self.crop, cr):
                    sp_.setValue(float(v))
                self.g_key.setVisible(True)
                ck = c.prop("chroma") or {}
                self.key_on.setChecked(bool(ck))
                self.key_col.set(ck.get("color", "#00FF00"))
                self.key_tol.setValue(float(ck.get("tol", 0.25)) * 100)
                self.key_soft.setValue(float(ck.get("soft", 0.1)) * 100)
            self._load_effects(c)
        finally:
            self._loading = False

    def _load_effects(self, c):
        while self.fx_lay.count():
            w = self.fx_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        fx = c.prop("effects") or []
        if c.kind == "audio":
            return
        self.g_fx.setVisible(True)
        node_mode = c.prop("fx_mode") == "nodes"
        self.fx_mode.setCurrentIndex(1 if node_mode else 0)
        if node_mode:
            g = c.prop("fx_graph") or {}
            n = sum(1 for x in g.get("nodes", {}).values() if x.get("type") not in ("input", "output"))
            lab = QLabel(f"ノード {n} 個で管理しています。ノードエディターで編集します。")
            lab.setObjectName("info")
            lab.setWordWrap(True)
            self.fx_lay.addWidget(lab)
            return
        if not fx:
            lab = QLabel("なし（エフェクトパレットからドラッグ）")
            lab.setObjectName("info")
            self.fx_lay.addWidget(lab)
            return
        from . import fxlib
        for i, e in enumerate(fx):
            plug = fxlib.get(e.get("name", ""))
            if plug is not None:
                self.fx_lay.addWidget(self._plugin_fx_widget(i, e, plug, len(fx)))
                continue
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(QLabel(FX_NAMES.get(e.get("name"), e.get("name"))), 1)
            if "value" in e:
                sp = QDoubleSpinBox()
                sp.setRange(0, 300)
                sp.setSuffix(" %")
                sp.setValue(float(e["value"]) * 100)
                sp.setKeyboardTracking(False)
                sp.valueChanged.connect(lambda v, i=i: self._set_fx_value(i, v / 100))
                h.addWidget(sp)
            rm = QPushButton("✕")
            rm.setFixedWidth(26)
            rm.clicked.connect(lambda _=False, i=i: self._remove_fx(i))
            h.addWidget(rm)
            self.fx_lay.addWidget(row)

    # ── 変更 ────────────────────────────────
    def _begin(self):
        if not self._editing:
            self.mw.begin_edit()
            self._editing = True
        self._commit_timer.start()

    def _commit(self):
        if self._editing:
            self._editing = False
            self.mw.end_edit("プロパティ変更", reload_inspector=False)

    def _commit_now(self):
        if self._commit_timer.isActive():
            self._commit_timer.stop()
        self._commit()

    def _targets(self, key=None):
        seq = self.mw.project.sequence
        out = []
        for c in self._clips:
            _, cur = seq.find(c.id)
            if cur is None:
                continue
            if key == "volume_db" and cur.kind != "audio":
                continue
            if key in ("pos_x", "pos_y", "scale", "rotation", "opacity", "crop", "chroma", "anim") \
                    and cur.kind == "audio":
                continue
            if key == "pan" and cur.kind != "audio":
                continue
            out.append(cur)
        return out

    def _set_prop(self, key, value):
        if self._loading:
            return
        self._begin()
        f = self.mw.record_player.position
        for c in self._targets(key):
            if key == "text" and len(self._clips) > 1:
                continue
            keys = (c.prop("keys") or {}).get(key)
            if keys:
                rel = max(0, min(c.duration - 1, f - c.start))
                c.props["keys"] = {**c.prop("keys"), key: set_key(keys, rel, value, self.key_ease.currentData())}
            else:
                c.props[key] = value
        self.mw.preview_changed()
        if key in dict((k, 1) for k, *_ in KEY_PROPS):
            self._load_keys()

    # ── キーフレーム ──
    def _key_cur(self):
        k = self.key_prop.currentData()
        mul = next(m for kk, _l, m, _s in KEY_PROPS if kk == k)
        w = {"scale": self.scale, "pos_x": self.pos_x, "pos_y": self.pos_y, "rotation": self.rot,
             "opacity": self.opacity}[k]
        return k, w.value() / mul if mul == 100 else w.value()

    def _load_keys(self):
        if not self._clips:
            return
        c = self.mw.project.sequence.find(self._clips[0].id)[1]
        if c is None:
            return
        k = self.key_prop.currentData()
        ks = (c.prop("keys") or {}).get(k) or []
        fps = self.mw.project.sequence.fps
        rel = self.mw.record_player.position - c.start
        here = any(int(x[0]) == rel for x in ks)
        lab = dict((kk, l) for kk, l, *_ in KEY_PROPS)
        others = [lab[kk] for kk, v in (c.prop("keys") or {}).items() if v and kk in lab and kk != k]
        self.key_info.setText(
            (f"{len(ks)} 個: " + "、".join(f"{x[0] / fps:.2f}秒" for x in ks[:8]) + ("…" if len(ks) > 8 else "")
             if ks else "キーフレームなし（◆ で今の値を打つと、値を変えるたびにその時刻のキーが更新されます）")
            + ("　◆ 再生ヘッドの位置にキーあり" if here else "")
            + (f"\nほかにキーがあるもの: {'、'.join(others)}" if others else ""))

    def _edit_keys(self, label, fn):
        self._commit_now()
        self.mw.begin_edit()
        f = self.mw.record_player.position
        for c in self._targets("scale"):
            ks = dict(c.prop("keys") or {})
            fn(c, ks, max(0, min(c.duration - 1, f - c.start)))
            c.props["keys"] = {k: v for k, v in ks.items() if v}
            if not c.props["keys"]:
                c.props.pop("keys")
        self.mw.end_edit(label, reload_inspector=False)
        self._load_keys()

    def add_key(self):
        k, v = self._key_cur()
        ez = self.key_ease.currentData()
        self._edit_keys("キーフレームを追加", lambda c, ks, rel: ks.__setitem__(k, set_key(ks.get(k) or [], rel, v, ez)))

    def remove_key(self):
        k = self.key_prop.currentData()
        self._edit_keys("キーフレームを削除", lambda c, ks, rel: ks.__setitem__(
            k, [x for x in (ks.get(k) or []) if int(x[0]) != rel]))

    def clear_keys(self):
        k = self.key_prop.currentData()

        def fn(c, ks, rel):
            # 消す前に、今見えている値を固定値として残す
            from .render import prop_at
            v = prop_at(c, k, self.mw.record_player.position)
            ks.pop(k, None)
            if v is not None:
                c.props[k] = v
        self._edit_keys("キーフレームを全消去", fn)

    def jump_key(self, d):
        if not self._clips:
            return
        c = self.mw.project.sequence.find(self._clips[0].id)[1]
        frames = sorted({c.start + int(x[0]) for ks in (c.prop("keys") or {}).values() for x in ks})
        f = self.mw.record_player.position
        nxt = [x for x in frames if (x > f if d > 0 else x < f)]
        if nxt:
            self.mw.seek_record(nxt[0] if d > 0 else nxt[-1])
            self.load(self._clips)

    # ── エフェクトの管理方法 ──
    def _fx_mode_changed(self):
        if self._loading or not self._clips:
            return
        from .nodes import set_fx_mode
        c = self.mw.project.sequence.find(self._clips[0].id)[1]
        if c is not None and not set_fx_mode(self.mw, c, self.fx_mode.currentData()):
            self._loading = True
            self.fx_mode.setCurrentIndex(self.fx_mode.findData("nodes"))
            self._loading = False
        if self.fx_mode.currentData() == "nodes" and hasattr(self.mw, "show_node_editor"):
            self.mw.show_node_editor()

    def _set_speed(self):
        if self._loading:
            return
        if self.freeze.isChecked():
            v = 0.0
        else:
            v = self.speed.value() / 100 * (-1 if self.reverse.isChecked() else 1)
        self._begin()
        for c in self._targets():
            if c.kind in ("video", "audio"):
                c.props["speed"] = v
        self.mw.preview_changed()

    def _set_key(self):
        if self._loading:
            return
        val = {"color": self.key_col._color, "tol": self.key_tol.value() / 100,
               "soft": self.key_soft.value() / 100} if self.key_on.isChecked() else None
        self._set_prop("chroma", val)

    def _set_anim(self):
        if self._loading:
            return
        an = {}
        if self.anim_on.isChecked():
            for key, (a0, a1) in self.anim.items():
                mul = 100 if key in ("scale", "opacity") else 1
                v0, v1 = a0.value() / mul, a1.value() / mul
                an[key] = [v0, v1]
            if self.anim_ease.currentData() != "linear":
                an["_ease"] = self.anim_ease.currentData()
            if self.anim_len.value():
                an["_len"] = self.anim_len.value()
        self._set_prop("anim", an)

    def _set_style(self, key, value):
        if self._loading:
            return
        self._begin()
        for c in self._targets():
            if c.kind != "text":
                continue
            st = dict(c.prop("style") or {})
            if value in (None, ""):
                st.pop(key, None)
            else:
                st[key] = value
            c.props["style"] = st
        self.mw.preview_changed()

    def _apply_preset(self, idx):
        if idx <= 0:
            return
        name = self.t_preset.itemText(idx)
        self.mw.apply_effect({"type": "telop_preset", "name": name})
        self.t_preset.setCurrentIndex(0)

    def _plugin_fx_widget(self, i, e, plug, count):
        """プラグインのエフェクト 1 つ分: 名前・ON/OFF・上下・削除 と パラメーター。"""
        from .fxlib import params_of
        box = QGroupBox()
        v = QVBoxLayout(box)
        v.setContentsMargins(6, 4, 6, 4)
        head = QHBoxLayout()
        on = QCheckBox(plug.name)
        on.setChecked(bool(e.get("on", True)))
        on.setToolTip(plug.description or plug.name)
        on.toggled.connect(lambda val, i=i: self._set_fx_field(i, "on", bool(val)))
        head.addWidget(on, 1)
        for txt, d in (("▲", -1), ("▼", 1)):
            b = QPushButton(txt)
            b.setFixedWidth(24)
            b.setEnabled(0 <= i + d < count)
            b.clicked.connect(lambda _=False, i=i, d=d: self._move_fx(i, d))
            head.addWidget(b)
        rm = QPushButton("✕")
        rm.setFixedWidth(24)
        rm.clicked.connect(lambda _=False, i=i: self._remove_fx(i))
        head.addWidget(rm)
        v.addLayout(head)
        form = QFormLayout()
        vals = params_of(e)
        for prm in plug.params:
            cur = vals.get(prm.key, prm.default)
            if prm.kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(cur))
                w.toggled.connect(lambda val, i=i, k=prm.key: self._set_fx_param(i, k, bool(val)))
            elif prm.kind == "color":
                w = ColorPick(lambda col, i=i, k=prm.key: self._set_fx_param(i, k, col))
                w.set(str(cur))
            elif prm.kind == "choice":
                w = QComboBox()
                w.addItems(prm.choices)
                w.setCurrentText(str(cur))
                w.currentTextChanged.connect(lambda val, i=i, k=prm.key: self._set_fx_param(i, k, val))
            elif prm.kind == "file":
                from pathlib import Path as _P
                w = QPushButton(_P(str(cur)).name if cur else "ファイルを選ぶ...")
                w.setToolTip(str(cur))
                w.clicked.connect(lambda _=False, i=i, k=prm.key, lab=prm.label: self._pick_fx_file(i, k, lab))
            elif prm.kind == "int":
                w = QSpinBox()
                w.setRange(int(prm.min), int(prm.max))
                w.setValue(int(cur))
                w.setSuffix(prm.suffix)
                w.setKeyboardTracking(False)
                w.valueChanged.connect(lambda val, i=i, k=prm.key: self._set_fx_param(i, k, int(val)))
            else:
                w = QDoubleSpinBox()
                w.setRange(float(prm.min), float(prm.max))
                w.setSingleStep(float(prm.step))
                w.setDecimals(0 if prm.step >= 1 else 2)
                w.setValue(float(cur))
                w.setSuffix(prm.suffix)
                w.setKeyboardTracking(False)
                w.valueChanged.connect(lambda val, i=i, k=prm.key: self._set_fx_param(i, k, float(val)))
            if prm.kind in ("float", "int"):
                ks = (e.get("keys") or {}).get(prm.key) or []
                kb = QPushButton("◆" if ks else "◇")
                kb.setFixedWidth(26)
                kb.setToolTip(f"キーフレーム {len(ks)} 個。クリックで再生ヘッドの位置に今の値でキーを打つ"
                              f"（右クリックで全消去）")
                kb.clicked.connect(lambda _=False, i=i, k=prm.key, wd=w: self._fx_key(i, k, wd.value()))
                kb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                kb.customContextMenuRequested.connect(lambda _p, i=i, k=prm.key: self._fx_key(i, k, None))
                row = QHBoxLayout()
                row.addWidget(w, 1)
                row.addWidget(kb)
                form.addRow(prm.label, row)
            else:
                form.addRow(prm.label, w)
        v.addLayout(form)
        return box

    def _pick_fx_file(self, i, key, label):
        from PyQt6.QtWidgets import QFileDialog
        p, _ = QFileDialog.getOpenFileName(self, label, "", "LUT (*.cube);;すべてのファイル (*.*)")
        if p:
            self._set_fx_param(i, key, p)
            self._commit_now()
            self.load(self._clips)

    def _fx_key(self, i, key, value):
        """エフェクトのパラメーターのキーフレーム（value=None なら全消去）。"""
        self._commit_now()
        self.mw.begin_edit()
        f = self.mw.record_player.position
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            if i >= len(fx):
                continue
            keys = dict(fx[i].get("keys") or {})
            if value is None:
                keys.pop(key, None)
            else:
                keys[key] = set_key(keys.get(key) or [], max(0, f - c.start), value)
            fx[i] = dict(fx[i], keys=keys)
            c.props["effects"] = fx
        self.mw.end_edit("エフェクトのキーフレーム")

    def _set_fx_param(self, i, key, val):
        if self._loading:
            return
        self._begin()
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            if i < len(fx):
                ks = (fx[i].get("keys") or {}).get(key)
                if ks and isinstance(val, (int, float)) and not isinstance(val, bool):
                    rel = max(0, self.mw.record_player.position - c.start)
                    fx[i] = dict(fx[i], keys={**fx[i]["keys"], key: set_key(ks, rel, val)})
                else:
                    fx[i] = dict(fx[i], params={**(fx[i].get("params") or {}), key: val})
                c.props["effects"] = fx
        self.mw.preview_changed()

    def _set_fx_field(self, i, key, val):
        if self._loading:
            return
        self._begin()
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            if i < len(fx):
                fx[i] = dict(fx[i], **{key: val})
                c.props["effects"] = fx
        self.mw.preview_changed()

    def _move_fx(self, i, d):
        self._commit_now()
        self.mw.begin_edit()
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            j = i + d
            if 0 <= i < len(fx) and 0 <= j < len(fx):
                fx[i], fx[j] = fx[j], fx[i]
                c.props["effects"] = fx
        self.mw.end_edit("エフェクトの順番")

    def _set_fx_value(self, i, v):
        if self._loading:
            return
        self._begin()
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            if i < len(fx):
                fx[i] = dict(fx[i], value=v)
                c.props["effects"] = fx
        self.mw.preview_changed()

    def _remove_fx(self, i):
        self._commit_now()
        self.mw.begin_edit()
        for c in self._targets():
            fx = list(c.prop("effects") or [])
            if i < len(fx):
                del fx[i]
                c.props["effects"] = fx
        self.mw.end_edit("エフェクト削除")
