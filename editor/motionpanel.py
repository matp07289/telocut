"""モーションパネル（AE 風: 親子付け・3D・マスク・モーションブラー・エクスプレッション・図形・カメラ）と
グラフエディター（キーフレームの値を曲線で編集）。"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from . import model as M
from .motion import EXPR_PROPS, check_expr

VISUAL = ("video", "image", "text", "color", "character", "shape", "null", "camera")
SHAPES = [("band", "帯（角丸四角）"), ("rect", "四角"), ("ellipse", "楕円"), ("star", "星"), ("polygon", "多角形"),
          ("line", "線"), ("arrow", "矢印"), ("progress", "進行バー"), ("countdown", "カウントダウン")]
EXPR_LABEL = {"pos_x": "位置 X", "pos_y": "位置 Y", "scale": "拡大", "rotation": "回転", "opacity": "不透明度", "z": "奥行き Z"}
EXPR_PRESETS = [("（よく使う式）", ""), ("ゆらゆら揺れる", "wiggle(2, 20)"), ("激しく揺れる", "wiggle(8, 12)"),
                ("回り続ける（1 秒 90°）", "value + time*90"), ("ふわふわ上下", "value + sin(time*3)*15"),
                ("点滅（不透明度）", "1 if int(time*4) % 2 == 0 else 0.2"), ("鼓動（拡大）", "value * (1 + abs(sin(time*6))*0.08)"),
                ("右へ流れ続ける", "value + time*200"), ("2 秒ごとに繰り返す", "value + loop(2)*100")]
KEY_PROPS = [("pos_x", "位置 X"), ("pos_y", "位置 Y"), ("scale", "拡大"), ("rotation", "回転"), ("opacity", "不透明度"),
             ("z", "奥行き Z")]
EASES = [("linear", "直線"), ("ease_in", "イーズイン"), ("ease_out", "イーズアウト"), ("ease_in_out", "イーズインアウト"),
         ("back", "行き過ぎて戻る"), ("bounce", "バウンド"), ("hold", "停止（次のキーまで同じ値）")]


def _spin(lo, hi, val=0.0, dec=1, step=1.0, suffix=""):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSingleStep(step)
    s.setValue(val)
    if suffix:
        s.setSuffix(suffix)
    s.setKeyboardTracking(False)
    return s


class MotionPanel(QScrollArea):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.setWidgetResizable(True)
        w = QWidget()
        self.setWidget(w)
        v = QVBoxLayout(w)
        v.setContentsMargins(6, 6, 6, 6)
        self.head = QLabel("クリップを選んでください")
        self.head.setWordWrap(True)
        v.addWidget(self.head)
        self._loading = False
        self.clip: Optional[M.Clip] = None

        # 親子付け・3D
        g = QGroupBox("親子付け・3D")
        f = QFormLayout(g)
        self.parent_cb = QComboBox()
        self.parent_cb.setToolTip("親の位置・拡大・回転に付いていきます（AE のペアレント）")
        self.parent_cb.activated.connect(lambda _i: self._set("parent", self.parent_cb.currentData() or None, "親を設定"))
        f.addRow("親", self.parent_cb)
        self.l3d = QCheckBox("3D レイヤー（カメラで遠近が付く）")
        self.l3d.toggled.connect(lambda on: self._set("layer3d", bool(on) or None, "3D レイヤー"))
        f.addRow(self.l3d)
        self.z = _spin(-20000, 20000, 0, 0, 50, " px")
        self.z.valueChanged.connect(lambda val: self._set("z", float(val), "奥行き"))
        f.addRow("奥行き Z（＋で奥）", self.z)
        v.addWidget(g)
        self.g_parent = g

        # カメラ
        g = QGroupBox("カメラ")
        f = QFormLayout(g)
        self.cam_z = _spin(-20000, 20000, 0, 0, 50, " px")
        self.cam_z.valueChanged.connect(lambda val: self._set("pos_z", float(val), "カメラの前後"))
        self.cam_zoom = _spin(0.1, 10, 1, 2, 0.05, " 倍")
        self.cam_zoom.valueChanged.connect(lambda val: self._set("zoom", float(val), "カメラのズーム"))
        f.addRow("前後（＋で前へ）", self.cam_z)
        f.addRow("ズーム", self.cam_zoom)
        f.addRow(QLabel("パン・ロールはエフェクトエディターの位置・回転で（キーフレームも可）"))
        v.addWidget(g)
        self.g_cam = g

        # マスク
        g = QGroupBox("マスク")
        f = QFormLayout(g)
        self.mask_shape = QComboBox()
        for k, lab in (("", "なし"), ("rect", "四角"), ("ellipse", "楕円")):
            self.mask_shape.addItem(lab, k)
        self.mask_shape.activated.connect(lambda _i: self._mask_changed())
        f.addRow("形", self.mask_shape)
        self.mask_rect = [_spin(-100, 200, v_, 1, 1, " %") for v_ in (25, 25, 50, 50)]
        row = QHBoxLayout()
        for lab, sp in zip(("X", "Y", "幅", "高"), self.mask_rect):
            row.addWidget(QLabel(lab))
            row.addWidget(sp)
            sp.valueChanged.connect(lambda _v: self._mask_changed())
        f.addRow(row)
        self.mask_feather = _spin(0, 500, 0, 0, 2, " px")
        self.mask_feather.valueChanged.connect(lambda _v: self._mask_changed())
        self.mask_expand = _spin(-500, 500, 0, 0, 2, " px")
        self.mask_expand.valueChanged.connect(lambda _v: self._mask_changed())
        self.mask_inv = QCheckBox("反転（外側を見せる）")
        self.mask_inv.toggled.connect(lambda _v: self._mask_changed())
        f.addRow("ぼかし", self.mask_feather)
        f.addRow("広げる", self.mask_expand)
        f.addRow(self.mask_inv)
        v.addWidget(g)
        self.g_mask = g

        # モーションブラー
        g = QGroupBox("モーションブラー")
        f = QFormLayout(g)
        self.mb = QCheckBox("動いている間だけブレを付ける")
        self.mb.toggled.connect(lambda on: self._set("motion_blur", bool(on) or None, "モーションブラー"))
        self.mb_sh = _spin(0, 720, 180, 0, 15, "°")
        self.mb_sh.valueChanged.connect(lambda val: self._set("shutter", float(val), "シャッター角"))
        self.mb_n = QSpinBox()
        self.mb_n.setRange(2, 32)
        self.mb_n.setValue(8)
        self.mb_n.valueChanged.connect(lambda val: self._set("mb_samples", int(val), "サンプル数"))
        f.addRow(self.mb)
        f.addRow("シャッター角", self.mb_sh)
        f.addRow("サンプル数", self.mb_n)
        v.addWidget(g)
        self.g_mb = g

        # エクスプレッション
        g = QGroupBox("エクスプレッション（式で動かす）")
        f = QFormLayout(g)
        self.expr = {}
        for k in EXPR_PROPS:
            e = QLineEdit()
            e.setPlaceholderText("例: wiggle(2, 20)")
            e.editingFinished.connect(lambda k=k: self._expr_changed(k))
            self.expr[k] = e
            f.addRow(EXPR_LABEL[k], e)
        pr = QHBoxLayout()
        self.preset = QComboBox()
        for lab, ex in EXPR_PRESETS:
            self.preset.addItem(lab, ex)
        self.preset_target = QComboBox()
        for k in EXPR_PROPS:
            self.preset_target.addItem(EXPR_LABEL[k], k)
        b = QPushButton("入れる")
        b.clicked.connect(self._apply_preset)
        pr.addWidget(self.preset, 1)
        pr.addWidget(self.preset_target)
        pr.addWidget(b)
        f.addRow(pr)
        self.expr_err = QLabel("value = 元の値・time = 秒・frame・wiggle(回数, 幅)・sin / cos・loop(秒)")
        self.expr_err.setWordWrap(True)
        self.expr_err.setObjectName("info")
        f.addRow(self.expr_err)
        v.addWidget(g)
        self.g_expr = g

        # 図形
        g = QGroupBox("図形（シェイプレイヤー）")
        f = QFormLayout(g)
        self.shape = QComboBox()
        for k, lab in SHAPES:
            self.shape.addItem(lab, k)
        self.shape.activated.connect(lambda _i: self._set("shape", self.shape.currentData(), "図形の種類"))
        f.addRow("種類", self.shape)
        self.shape_rect = [_spin(-100, 200, v_, 1, 1, " %") for v_ in (30, 30, 40, 40)]
        row = QHBoxLayout()
        for lab, sp in zip(("X", "Y", "幅", "高"), self.shape_rect):
            row.addWidget(QLabel(lab))
            row.addWidget(sp)
            sp.valueChanged.connect(lambda _v: self._set("rect", [s.value() for s in self.shape_rect], "図形の大きさ"))
        f.addRow(row)
        self.fill_col = QLineEdit()
        self.fill_col.editingFinished.connect(lambda: self._set("color", self.fill_col.text().strip() or "#000000", "塗りの色"))
        self.fill_op = _spin(0, 1, 0.6, 2, 0.05)
        self.fill_op.valueChanged.connect(lambda val: self._set("fill_opacity", float(val), "塗りの不透明度"))
        self.stroke_col = QLineEdit()
        self.stroke_col.editingFinished.connect(lambda: self._set("stroke_color", self.stroke_col.text().strip() or "#FFFFFF", "線の色"))
        self.stroke_w = _spin(0, 200, 0, 0, 1, " px")
        self.stroke_w.valueChanged.connect(lambda val: self._set("stroke_width", float(val), "線の太さ"))
        self.sides = QSpinBox()
        self.sides.setRange(3, 24)
        self.sides.valueChanged.connect(lambda val: self._set("sides", int(val), "頂点の数"))
        self.radius = _spin(0, 50, 0, 1, 0.5, " %")
        self.radius.valueChanged.connect(lambda val: self._set("radius", float(val), "角の丸み"))
        f.addRow("塗りの色", self.fill_col)
        f.addRow("塗りの不透明度（0 で線だけ）", self.fill_op)
        f.addRow("線の色", self.stroke_col)
        f.addRow("線の太さ", self.stroke_w)
        f.addRow("頂点（星・多角形）", self.sides)
        f.addRow("角の丸み", self.radius)
        v.addWidget(g)
        self.g_shape = g
        v.addStretch(1)
        self._sig = None
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self._poll)
        self.timer.start()
        mw.canvas.selection_changed.connect(lambda: self.load(force=True))
        self.load(force=True)

    # ── 読み込み ──
    def _current(self) -> Optional[M.Clip]:
        sel = [c for _, c in self.mw.canvas.selected_clips() if c.kind in VISUAL]
        return sel[0] if sel else None

    def _poll(self):
        if self.isVisible():
            self.load()

    def load(self, force=False):
        c = self._current()
        sig = (c.id, repr(sorted((c.props or {}).items(), key=lambda kv: kv[0]))) if c else None
        if sig == self._sig and not force:
            return
        self._sig = sig
        self.clip = c
        for g in (self.g_parent, self.g_cam, self.g_mask, self.g_mb, self.g_expr, self.g_shape):
            g.setVisible(c is not None)
        if c is None:
            self.head.setText("映像・画像・テロップ・図形・ヌル・カメラのクリップを選んでください")
            return
        self._loading = True
        try:
            kind = {"null": "ヌル（親専用・見えない）", "camera": "カメラ", "shape": "図形", "text": "テロップ"}.get(c.kind, c.kind)
            self.head.setText(f"<b>{c.name or kind}</b>（{kind}）")
            seq = self.mw.project.sequence
            self.parent_cb.clear()
            self.parent_cb.addItem("なし", "")
            for t in seq.video_tracks:
                for o in t.clips:
                    if o.id != c.id and o.kind in VISUAL and o.kind != "camera":
                        self.parent_cb.addItem(f"{t.name}: {(o.name or o.prop('text', '') or o.kind)[:20]}", o.id)
            i = self.parent_cb.findData(c.props.get("parent") or "")
            self.parent_cb.setCurrentIndex(max(0, i))
            is_cam = c.kind == "camera"
            self.g_cam.setVisible(is_cam)
            self.g_mask.setVisible(c.kind not in ("null", "camera"))
            self.g_mb.setVisible(c.kind not in ("null", "camera", "text"))
            self.g_shape.setVisible(c.kind == "shape")
            self.l3d.setVisible(not is_cam)
            self.z.setVisible(not is_cam)
            self.l3d.setChecked(bool(c.props.get("layer3d")))
            self.z.setValue(float(c.props.get("z", 0) or 0))
            self.cam_z.setValue(float(c.props.get("pos_z", 0) or 0))
            self.cam_zoom.setValue(float(c.props.get("zoom", 1) or 1))
            mk = c.props.get("mask") or {}
            self.mask_shape.setCurrentIndex(max(0, self.mask_shape.findData(mk.get("shape", ""))))
            for sp, val in zip(self.mask_rect, mk.get("rect") or [25, 25, 50, 50]):
                sp.setValue(float(val))
            self.mask_feather.setValue(float(mk.get("feather", 0) or 0))
            self.mask_expand.setValue(float(mk.get("expand", 0) or 0))
            self.mask_inv.setChecked(bool(mk.get("invert")))
            self.mb.setChecked(bool(c.props.get("motion_blur")))
            self.mb_sh.setValue(float(c.props.get("shutter", 180) or 180))
            self.mb_n.setValue(int(c.props.get("mb_samples", 8) or 8))
            ex = c.props.get("expr") or {}
            for k, e in self.expr.items():
                e.setText(ex.get(k, ""))
            if c.kind == "shape":
                self.shape.setCurrentIndex(max(0, self.shape.findData(c.prop("shape", "band"))))
                for sp, val in zip(self.shape_rect, c.prop("rect") or [0, 78, 100, 22]):
                    sp.setValue(float(val))
                self.fill_col.setText(c.prop("color", "#000000"))
                self.fill_op.setValue(float(c.prop("fill_opacity", 0.6)))
                self.stroke_col.setText(c.props.get("stroke_color", "#FFFFFF"))
                self.stroke_w.setValue(float(c.props.get("stroke_width", 0) or 0))
                self.sides.setValue(int(c.props.get("sides", 5) or 5))
                self.radius.setValue(float(c.prop("radius", 0) or 0))
        finally:
            self._loading = False

    # ── 変更 ──
    def _set(self, key, value, label):
        if self._loading or self.clip is None:
            return
        c = self.mw.project.sequence.find(self.clip.id)[1]
        if c is None:
            return
        if key == "parent" and value:
            # 親のループを防ぐ
            seq, p, seen = self.mw.project.sequence, value, set()
            while p and p not in seen:
                if p == c.id:
                    self.expr_err.setText("⚠ 子孫を親にはできません")
                    self.load(force=True)
                    return
                seen.add(p)
                par = seq.find(p)[1]
                p = par.props.get("parent") if par else None
        self.mw.begin_edit()
        if value is None:
            c.props.pop(key, None)
        else:
            c.props[key] = value
        self.mw.end_edit(label, reload_inspector=False)
        self._sig = None

    def _mask_changed(self):
        shape = self.mask_shape.currentData()
        if not shape:
            self._set("mask", None, "マスクを外す")
            return
        self._set("mask", {"shape": shape, "rect": [s.value() for s in self.mask_rect],
                           "feather": self.mask_feather.value(), "expand": self.mask_expand.value(),
                           "invert": self.mask_inv.isChecked()}, "マスク")

    def _expr_changed(self, k):
        if self._loading or self.clip is None:
            return
        src = self.expr[k].text().strip()
        if src:
            err = check_expr(src)
            if err:
                self.expr_err.setText(f"⚠ {EXPR_LABEL[k]}: {err}")
                return
        c = self.mw.project.sequence.find(self.clip.id)[1]
        ex = dict(c.props.get("expr") or {})
        if (ex.get(k) or "") == src:
            return
        if src:
            ex[k] = src
        else:
            ex.pop(k, None)
        self.expr_err.setText(f"{EXPR_LABEL[k]} の式を{'設定' if src else '外'}しました")
        self._set("expr", ex or None, "エクスプレッション")

    def _apply_preset(self):
        ex = self.preset.currentData()
        if not ex:
            return
        k = self.preset_target.currentData()
        if k == "opacity" and "wiggle" in ex:
            ex = "max(0, min(1, wiggle(3, 0.3)))"
        self.expr[k].setText(ex)
        self._expr_changed(k)


class GraphEditor(QWidget):
    """選んだクリップのキーフレームを曲線で表示・編集（点をドラッグ・ダブルクリックで追加・右クリックで削除 / 補間）。"""
    PAD = 28

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        h = QHBoxLayout()
        self.prop = QComboBox()
        for k, lab in KEY_PROPS:
            self.prop.addItem(lab, k)
        self.prop.currentIndexChanged.connect(lambda _i: self.update())
        h.addWidget(QLabel("値"))
        h.addWidget(self.prop)
        self.info = QLabel("")
        self.info.setObjectName("info")
        h.addWidget(self.info, 1)
        v.addLayout(h)
        self.canvas = _GraphCanvas(self)
        v.addWidget(self.canvas, 1)
        mw.canvas.selection_changed.connect(self.canvas.update)

    def clip(self) -> Optional[M.Clip]:
        sel = [c for _, c in self.mw.canvas.selected_clips() if c.kind in VISUAL]
        return sel[0] if sel else None

    def key(self) -> str:
        return self.prop.currentData()


class _GraphCanvas(QWidget):
    def __init__(self, ge: GraphEditor):
        super().__init__()
        self.ge = ge
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self.drag = -1
        self._range = (0.0, 1.0)
        self._editing = False

    # 座標変換
    def _keys(self):
        c = self.ge.clip()
        if c is None:
            return None, []
        return c, sorted((c.prop("keys") or {}).get(self.ge.key()) or [], key=lambda k: k[0])

    def _vrange(self, c, ks):
        from .render import prop_at
        vals = [float(k[1]) for k in ks] or [float(prop_at(c, self.ge.key(), c.start) or 0)]
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-6:
            lo, hi = lo - 1, hi + 1
        m = (hi - lo) * 0.15
        return lo - m, hi + m

    def _xy(self, c, rel, val):
        P = GraphEditor.PAD
        w, h = self.width() - P * 2, self.height() - P * 2
        lo, hi = self._range
        return QPointF(P + rel / max(1, c.duration - 1) * w, P + (1 - (val - lo) / (hi - lo)) * h)

    def _inv(self, c, pt):
        P = GraphEditor.PAD
        w, h = self.width() - P * 2, self.height() - P * 2
        lo, hi = self._range
        rel = round((pt.x() - P) / max(1, w) * max(1, c.duration - 1))
        val = lo + (1 - (pt.y() - P) / max(1, h)) * (hi - lo)
        return max(0, min(c.duration - 1, rel)), val

    def paintEvent(self, _e):
        from .render import key_value
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#18181b"))
        c, ks = self._keys()
        P = GraphEditor.PAD
        if c is None:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "クリップを選ぶと、キーフレームの動きを曲線で表示します")
            self.ge.info.setText("")
            return
        if not self.drag >= 0:
            self._range = self._vrange(c, ks)
        lo, hi = self._range
        p.setPen(QPen(QColor("#333"), 1))
        for i in range(5):
            y = P + i * (self.height() - P * 2) / 4
            p.drawLine(P, int(y), self.width() - P, int(y))
            p.setPen(QColor("#777"))
            p.drawText(2, int(y) + 4, f"{hi - (hi - lo) * i / 4:.4g}")
            p.setPen(QPen(QColor("#333"), 1))
        # 再生ヘッド
        f = self.ge.mw.record_player.position - c.start
        if 0 <= f < c.duration:
            x = self._xy(c, f, lo).x()
            p.setPen(QPen(QColor("#e05050"), 1))
            p.drawLine(int(x), P, int(x), self.height() - P)
        if ks:
            path = QPainterPath()
            n = max(2, self.width() - P * 2)
            for i in range(n + 1):
                rel = i / n * max(1, c.duration - 1)
                pt = self._xy(c, rel, float(key_value(ks, rel)))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.setPen(QPen(QColor("#a99cff"), 2))
            p.drawPath(path)
            for i, k in enumerate(ks):
                pt = self._xy(c, k[0], float(k[1]))
                p.setBrush(QColor("#ffd257") if i == self.drag else QColor("#fff"))
                p.setPen(QPen(QColor("#000"), 1))
                p.drawRect(QRectF(pt.x() - 4, pt.y() - 4, 8, 8))
        else:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "ダブルクリックでキーフレームを追加")
        self.ge.info.setText(f"キー {len(ks)} 個　ドラッグで時間と値・右クリックで補間 / 削除")

    def _hit(self, c, ks, pos) -> int:
        for i, k in enumerate(ks):
            pt = self._xy(c, k[0], float(k[1]))
            if abs(pt.x() - pos.x()) <= 6 and abs(pt.y() - pos.y()) <= 6:
                return i
        return -1

    def _write(self, c, ks, label=None):
        keys = dict(c.prop("keys") or {})
        if ks:
            keys[self.ge.key()] = sorted(ks, key=lambda k: k[0])
        else:
            keys.pop(self.ge.key(), None)
        c.props["keys"] = keys
        if label:
            self.ge.mw.end_edit(label, reload_inspector=True)
            self._editing = False
        else:
            self.ge.mw.preview_changed()
        self.update()

    def mousePressEvent(self, e):
        c, ks = self._keys()
        if c is None:
            return
        i = self._hit(c, ks, e.position())
        if e.button() == Qt.MouseButton.RightButton and i >= 0:
            m = QMenu(self)
            for k, lab in EASES:
                a = m.addAction(("● " if (ks[i][2] if len(ks[i]) > 2 else "linear") == k else "　") + lab)
                a.triggered.connect(lambda _=False, k=k, i=i: self._set_ease(i, k))
            m.addSeparator()
            m.addAction("このキーを削除", lambda i=i: self._delete(i))
            m.exec(e.globalPosition().toPoint())
            return
        if e.button() == Qt.MouseButton.LeftButton and i >= 0:
            self.drag = i
            self.ge.mw.begin_edit()
            self._editing = True

    def mouseMoveEvent(self, e):
        if self.drag < 0:
            return
        c, ks = self._keys()
        if c is None or self.drag >= len(ks):
            return
        rel, val = self._inv(c, e.position())
        if any(j != self.drag and int(k[0]) == rel for j, k in enumerate(ks)):
            rel = int(ks[self.drag][0])
        ks[self.drag] = [rel, round(val, 3)] + list(ks[self.drag][2:])
        ks.sort(key=lambda k: k[0])
        self.drag = next(j for j, k in enumerate(ks) if int(k[0]) == rel)
        self._write(c, ks)

    def mouseReleaseEvent(self, _e):
        if self.drag >= 0:
            c, ks = self._keys()
            self.drag = -1
            if c is not None:
                self._write(c, ks, "キーフレームを動かす")

    def mouseDoubleClickEvent(self, e):
        from .render import prop_at
        c, ks = self._keys()
        if c is None:
            return
        rel, val = self._inv(c, e.position())
        if not ks:
            val = float(prop_at(c, self.ge.key(), c.start + rel) or (1.0 if self.ge.key() in ("scale", "opacity") else 0.0))
        self.ge.mw.begin_edit()
        ks = [k for k in ks if int(k[0]) != rel] + [[rel, round(val, 3), "ease_in_out"]]
        self._write(c, ks, "キーフレームを追加")

    def _set_ease(self, i, ez):
        c, ks = self._keys()
        self.ge.mw.begin_edit()
        ks[i] = [ks[i][0], ks[i][1], ez]
        self._write(c, ks, "補間を変更")

    def _delete(self, i):
        c, ks = self._keys()
        self.ge.mw.begin_edit()
        del ks[i]
        self._write(c, ks, "キーフレームを削除")
