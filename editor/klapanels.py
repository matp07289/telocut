"""Premiere の KLA 系拡張パネル風の作業効率化パネル。

- コマンドパレット: すべての機能を名前で検索して実行。★を付けたものは上に並ぶ（Ctrl+Shift+P）
- エフェクトプリセット: 選んだクリップの見た目（スケール・位置・エフェクト・アニメなど）を保存して ★ で呼び出す
- テロップスタイル: よく使うテロップの装飾をワンクリックで適用（自分のスタイルも登録できる）
- スケールプリセット: 1〜10 の拡大率ボタン（ズームインの演出用）
- イージング: アニメーションの動き方（イーズアウト・バックなど）とズーム演出をワンクリックで
- Drag & Zoom: レコードモニター上で囲んだ範囲に向かってズーム（mainwindow.drag_zoom）
"""
from __future__ import annotations

import copy
from typing import List, Optional

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLineEdit, QListWidget,
                             QListWidgetItem, QPushButton, QLabel, QTabWidget, QWidget, QInputDialog,
                             QMenu, QToolButton, QComboBox, QSpinBox, QCheckBox)

from .settings import SETTINGS

# エフェクトプリセットとして保存するプロパティ（テキスト本文や長さは保存しない）
PRESET_KEYS = ("opacity", "scale", "pos_x", "pos_y", "rotation", "fade_in", "fade_out", "effects", "crop",
               "chroma", "anim", "style", "text_anim", "type_speed", "karaoke", "volume_db", "pan", "label")


# ─────────────────────────────────────────────
#  コマンドパレット
# ─────────────────────────────────────────────
def collect_actions(mw) -> List[tuple]:
    """(id, 表示名, カテゴリ, QAction) の一覧。登録コマンド + メニューにだけある項目。"""
    from .commands import COMMAND_INFO
    out, seen = [], set()
    for cid, a in mw.actions.items():
        info = COMMAND_INFO.get(cid)
        out.append((cid, info[1] if info else a.text(), info[2] if info else "", a))
        seen.add(id(a))

    def walk(menu, path):
        for a in menu.actions():
            if a.menu() is not None:
                walk(a.menu(), path + [a.menu().title().replace("&", "")])
            elif not a.isSeparator() and a.text() and id(a) not in seen:
                seen.add(id(a))
                name = a.text().replace("&", "").split("\t")[0]
                out.append(("menu:" + "/".join(path + [name]), name, " › ".join(path), a))
    for a in mw.menuBar().actions():
        if a.menu() is not None:
            walk(a.menu(), [a.menu().title().replace("&", "")])
    return out


def _match(q: str, text: str) -> int:
    """あいまい検索のスコア（0=一致しない）。部分一致 > 頭文字の順番一致。"""
    if not q:
        return 1
    t = text.lower()
    q = q.lower()
    if q in t:
        return 100 - t.index(q)
    i = 0
    for ch in t:
        if i < len(q) and ch == q[i]:
            i += 1
    return 10 if i == len(q) else 0


class CommandPalette(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("コマンドパレット")
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(560, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.q = QLineEdit()
        self.q.setPlaceholderText("機能を検索（例: 文字起こし / ショート / マーカー / 書き出し）  Enter で実行・Ctrl+D で★")
        self.q.textChanged.connect(self.refresh)
        lay.addWidget(self.q)
        self.list = QListWidget()
        self.list.itemActivated.connect(self.run_item)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.list, 1)
        self.hint = QLabel("★ = お気に入り（右クリックか Ctrl+D で切り替え）")
        self.hint.setStyleSheet("color: #888")
        lay.addWidget(self.hint)
        self.items = collect_actions(mw)
        self.refresh()
        self.q.setFocus()

    def refresh(self):
        q = self.q.text().strip()
        fav = SETTINGS.palette_favorites
        scored = []
        for cid, name, cat, a in self.items:
            s = max(_match(q, name), _match(q, cat) // 2, _match(q, cid) // 2)
            if s:
                star = cid in fav
                scored.append((not star, -s, fav.index(cid) if star else 0, name, cid, cat, a))
        scored.sort(key=lambda x: x[:4])
        self.list.clear()
        for _ns, _s, _i, name, cid, cat, a in scored[:200]:
            keys = " / ".join(k.toString() for k in a.shortcuts())
            it = QListWidgetItem(("★ " if cid in fav else "　 ") + name + (f"   — {cat}" if cat else "") + (f"   [{keys}]" if keys else ""))
            it.setData(Qt.ItemDataRole.UserRole, (cid, a))
            if not a.isEnabled():
                it.setForeground(QColor("#777"))
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def toggle_fav(self, it=None):
        it = it or self.list.currentItem()
        if it is None:
            return
        cid = it.data(Qt.ItemDataRole.UserRole)[0]
        fav = SETTINGS.palette_favorites
        if cid in fav:
            fav.remove(cid)
        else:
            fav.append(cid)
        SETTINGS.save()
        row = self.list.currentRow()
        self.refresh()
        self.list.setCurrentRow(min(row, self.list.count() - 1))

    def _menu(self, pos):
        it = self.list.itemAt(pos)
        if it is None:
            return
        m = QMenu(self)
        m.addAction("実行", lambda: self.run_item(it))
        m.addAction("★ お気に入りを切り替え", lambda: self.toggle_fav(it))
        m.exec(self.list.mapToGlobal(pos))

    def run_item(self, it):
        a = it.data(Qt.ItemDataRole.UserRole)[1]
        self.accept()
        if a.isEnabled():
            a.trigger()

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
            self.list.keyPressEvent(e)
            return
        if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.list.currentItem():
                self.run_item(self.list.currentItem())
            return
        if k == Qt.Key.Key_D and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.toggle_fav()
            return
        super().keyPressEvent(e)


# ─────────────────────────────────────────────
#  プリセットパネル
# ─────────────────────────────────────────────
def _style_icon(style: dict, text: str = "あA") -> QIcon:
    pm = QPixmap(96, 48)
    bg = QColor(style.get("bg_color", "#3a3a3a")) if style.get("bg_enabled") else QColor("#3a3a3a")
    pm.fill(bg)
    p = QPainter(pm)
    f = QFont()
    f.setPixelSize(26)
    f.setBold(bool(style.get("bold", True)))
    p.setFont(f)
    sw = min(4, int(style.get("stroke_width", 0) or 0) // 2)
    if sw:
        p.setPen(QColor(style.get("stroke_color", "#000000")))
        for dx in (-sw, 0, sw):
            for dy in (-sw, 0, sw):
                p.drawText(pm.rect().translated(dx, dy), Qt.AlignmentFlag.AlignCenter, text)
    p.setPen(QColor(style.get("font_color", "#FFFFFF")))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return QIcon(pm)


class PresetPanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        self.msg = QLabel("")
        self.msg.setStyleSheet("color: #999")
        lay.addWidget(self.msg)
        self.tabs.addTab(self._fx_tab(), "エフェクト")
        self.tabs.addTab(self._style_tab(), "テロップ")
        self.tabs.addTab(self._motion_tab(), "スケール / 動き")

    # ── 共通 ──
    def _targets(self, kinds=None):
        sel = [c for _, c in self.mw.canvas.selected_clips()]
        if kinds:
            sel = [c for c in sel if c.kind in kinds]
        if not sel:
            self.msg.setText("タイムラインでクリップを選んでください")
        return sel

    def _apply(self, label: str, fn, kinds=None):
        sel = self._targets(kinds)
        if not sel:
            return
        self.mw.begin_edit()
        for c in sel:
            fn(c)
        self.mw.end_edit(label)
        self.mw.invalidate_render() if hasattr(self.mw, "invalidate_render") else None
        self.msg.setText(f"{label}: {len(sel)} 個")

    # ── エフェクトプリセット ──
    def _fx_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        row = QHBoxLayout()
        self.fx_q = QLineEdit()
        self.fx_q.setPlaceholderText("プリセットを検索")
        self.fx_q.textChanged.connect(self.refresh_fx)
        self.fx_star = QCheckBox("★のみ")
        self.fx_star.toggled.connect(self.refresh_fx)
        save = QPushButton("＋ 選択クリップから保存")
        save.clicked.connect(self.save_fx)
        row.addWidget(self.fx_q, 1)
        row.addWidget(self.fx_star)
        row.addWidget(save)
        v.addLayout(row)
        self.fx_list = QListWidget()
        self.fx_list.itemDoubleClicked.connect(lambda it: self.apply_fx(it.data(Qt.ItemDataRole.UserRole)))
        self.fx_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.fx_list.customContextMenuRequested.connect(self._fx_menu)
        v.addWidget(self.fx_list, 1)
        v.addWidget(QLabel("ダブルクリックで選択クリップに適用・右クリックで★/名前変更/削除"))
        self.refresh_fx()
        return w

    def refresh_fx(self):
        q = self.fx_q.text().strip().lower()
        self.fx_list.clear()
        presets = sorted(SETTINGS.effect_presets, key=lambda p: (not p.get("star"), p.get("name", "")))
        for p in presets:
            if (q and q not in p["name"].lower()) or (self.fx_star.isChecked() and not p.get("star")):
                continue
            keys = ", ".join(k for k in p["props"] if k not in ("label",))
            it = QListWidgetItem(("★ " if p.get("star") else "☆ ") + p["name"])
            it.setToolTip(keys)
            it.setData(Qt.ItemDataRole.UserRole, p)
            self.fx_list.addItem(it)

    def save_fx(self):
        sel = self._targets()
        if not sel:
            return
        c = sel[0]
        name, ok = QInputDialog.getText(self, "エフェクトプリセット", "名前:", text=f"{c.name or c.kind} のプリセット")
        if not ok or not name.strip():
            return
        props = {k: copy.deepcopy(c.props[k]) for k in PRESET_KEYS if k in c.props}
        SETTINGS.effect_presets = [p for p in SETTINGS.effect_presets if p["name"] != name.strip()]
        SETTINGS.effect_presets.append({"name": name.strip(), "star": False, "kind": c.kind, "props": props})
        SETTINGS.save()
        self.refresh_fx()
        self.msg.setText(f"保存しました: {name}")

    def apply_fx(self, p: dict):
        props = p["props"]

        def fn(c):
            for k, v in props.items():
                if k == "style" and c.kind != "text":
                    continue
                c.props[k] = copy.deepcopy(v)
        self._apply(f"プリセット「{p['name']}」", fn)

    def _fx_menu(self, pos):
        it = self.fx_list.itemAt(pos)
        if it is None:
            return
        p = it.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)
        m.addAction("適用", lambda: self.apply_fx(p))

        def star():
            p["star"] = not p.get("star")
            SETTINGS.save()
            self.refresh_fx()

        def rename():
            n, ok = QInputDialog.getText(self, "名前を変更", "名前:", text=p["name"])
            if ok and n.strip():
                p["name"] = n.strip()
                SETTINGS.save()
                self.refresh_fx()

        def delete():
            SETTINGS.effect_presets = [x for x in SETTINGS.effect_presets if x is not p]
            SETTINGS.save()
            self.refresh_fx()
        m.addAction("★ を切り替え", star)
        m.addAction("名前を変更", rename)
        m.addAction("削除", delete)
        m.exec(self.fx_list.mapToGlobal(pos))

    # ── テロップスタイル ──
    def _style_tab(self) -> QWidget:
        from .youtube import TELOP_STYLES, TEXT_ANIMS
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        self.style_list = QListWidget()
        self.style_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.style_list.setIconSize(QSize(96, 48))
        self.style_list.setGridSize(QSize(120, 84))
        self.style_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.style_list.setWordWrap(True)
        self.style_list.itemDoubleClicked.connect(lambda it: self.apply_style(it.data(Qt.ItemDataRole.UserRole)))
        for name, st in TELOP_STYLES:
            it = QListWidgetItem(_style_icon(st), name)
            it.setData(Qt.ItemDataRole.UserRole, (name, st))
            self.style_list.addItem(it)
        v.addWidget(self.style_list, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("文字アニメ"))
        self.anim_combo = QComboBox()
        for lab, k in TEXT_ANIMS:
            self.anim_combo.addItem(lab, k)
        ab = QPushButton("適用")
        ab.clicked.connect(self.apply_text_anim)
        row.addWidget(self.anim_combo, 1)
        row.addWidget(ab)
        v.addLayout(row)
        v.addWidget(QLabel("ダブルクリックで選択テロップに適用（本文はそのまま）"))
        return w

    def apply_style(self, ns):
        name, st = ns
        self._apply(f"スタイル「{name}」", lambda c: c.props.__setitem__("style", {**(c.prop("style") or {}), **st}),
                    kinds=("text",))

    def apply_text_anim(self):
        k = self.anim_combo.currentData()
        self._apply("文字アニメ", lambda c: c.props.__setitem__("text_anim", k), kinds=("text",))

    # ── スケール / 動き ──
    def _motion_tab(self) -> QWidget:
        from .render import EASES
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        v.addWidget(QLabel("スケールプリセット（1 = 100%、数字が大きいほど寄る）"))
        g = QGridLayout()
        self.scale_steps = [1.0, 1.1, 1.2, 1.3, 1.45, 1.6, 1.8, 2.0, 2.5, 3.0]
        for i, s in enumerate(self.scale_steps):
            b = QToolButton()
            b.setText(f"{i + 1}\n{int(s * 100)}%")
            b.setMinimumSize(52, 40)
            b.clicked.connect(lambda _=False, s=s: self.set_scale(s))
            g.addWidget(b, i // 5, i % 5)
        v.addLayout(g)
        row = QHBoxLayout()
        self.zoom_anim = QCheckBox("ズームをアニメーションで")
        self.zoom_len = QSpinBox()
        self.zoom_len.setRange(0, 600)
        self.zoom_len.setValue(8)
        self.zoom_len.setSuffix(" f")
        self.zoom_len.setSpecialValueText("全体")
        row.addWidget(self.zoom_anim)
        row.addWidget(QLabel("長さ"))
        row.addWidget(self.zoom_len)
        v.addLayout(row)
        v.addWidget(QLabel("イージング（アニメーションの動き方）"))
        eg = QGridLayout()
        names = {"linear": "直線", "ease_in": "イーズイン", "ease_out": "イーズアウト", "ease_in_out": "イン・アウト",
                 "back": "バック", "bounce": "バウンス"}
        for i, k in enumerate(EASES):
            b = QToolButton()
            b.setIcon(self._ease_icon(k))
            b.setIconSize(QSize(44, 28))
            b.setText(names.get(k, k))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.clicked.connect(lambda _=False, k=k: self.set_ease(k))
            eg.addWidget(b, i // 3, i % 3)
        v.addLayout(eg)
        dz = QPushButton("Drag & Zoom（レコードモニターで範囲を囲む）")
        dz.setCheckable(True)
        dz.toggled.connect(lambda on: self.mw.set_drag_zoom(on) if hasattr(self.mw, "set_drag_zoom") else None)
        self.drag_btn = dz
        v.addWidget(dz)
        quick = QHBoxLayout()
        for lab, an in (("ポン（拡大して戻る）", {"scale": [1.25, 1.0], "_ease": "back", "_len": 8}),
                        ("じわっと寄る", {"scale": [1.0, 1.12], "_ease": "ease_in_out"}),
                        ("解除", {})):
            b = QPushButton(lab)
            b.clicked.connect(lambda _=False, an=an, lab=lab: self._apply(lab, lambda c: c.props.__setitem__(
                "anim", {k: ([v[0] * float(c.prop('scale', 1.0)), v[1] * float(c.prop('scale', 1.0))] if k == "scale" else v)
                         for k, v in an.items()}), kinds=("video", "image", "text", "character", "shape", "color")))
            quick.addWidget(b)
        v.addLayout(quick)
        v.addStretch(1)
        return w

    @staticmethod
    def _ease_icon(k: str) -> QIcon:
        from .render import ease
        pm = QPixmap(44, 28)
        pm.fill(QColor("#2a2a2a"))
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor("#ffcc33"))
        pts = [(2 + 40 * i / 20, 25 - 21 * ease(k, i / 20)) for i in range(21)]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            p.drawLine(int(x0), int(y0), int(x1), int(y1))
        p.end()
        return QIcon(pm)

    def set_scale(self, s: float):
        animate = self.zoom_anim.isChecked()

        def fn(c):
            cur = float(c.prop("scale", 1.0) or 1.0)
            if animate:
                an = {"scale": [cur, s], "_ease": "ease_out"}
                if self.zoom_len.value():
                    an["_len"] = self.zoom_len.value()
                c.props["anim"] = an
            else:
                c.props["scale"] = s
                an = dict(c.prop("anim") or {})
                an.pop("scale", None)
                c.props["anim"] = an if any(not k.startswith("_") for k in an) else {}
        self._apply(f"スケール {int(s * 100)}%", fn, kinds=("video", "image", "text", "character", "shape", "color"))

    def set_ease(self, k: str):
        def fn(c):
            an = dict(c.prop("anim") or {})
            if not any(not x.startswith("_") for x in an):
                return
            an["_ease"] = k
            c.props["anim"] = an
        self._apply("イージング", fn)
