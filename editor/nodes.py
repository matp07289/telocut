"""エフェクトの「ノード」管理（DaVinci Resolve の Fusion / Blender のコンポジター風）。

クリップごとに エフェクトの管理方法 を選べる:
  レイヤー（スタック）… props["effects"] を上から順に掛ける（AviUtl / Premiere 風）
  ノード             … props["fx_graph"] のノードをつないで掛ける（分岐・合成・マット）

グラフの形:
  {"nodes": {id: {"type": ..., "x": .., "y": .., ...}}, "links": [[出力元 id, 入力先 id, 入力番号], ...]}
ノードの種類:
  input   クリップの映像（入力なし）
  output  最終結果（入力 0）
  effect  エフェクト 1 つ（入力 0）。"fx": {"name", "params", "on", "keys"} … レイヤー方式と同じ形
  merge   合成（入力 0 = 下 / 背景、入力 1 = 上 / 前景）。params: mode（ブレンドモード）, opacity, x, y
  matte   トラックマット（入力 0 = 映像、入力 1 = マット）。params: source（alpha / luma）, invert
  solid   単色（入力なし）。params: color, opacity
"""
from __future__ import annotations

import copy
import math
import uuid
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from . import fxlib
from .fxlib import P

NODE_INPUTS = {"input": 0, "output": 1, "effect": 1, "merge": 2, "matte": 2, "solid": 0}
NODE_LABEL = {"input": "入力（クリップ）", "output": "出力", "effect": "エフェクト", "merge": "合成（マージ）",
              "matte": "マット（トラックマット）", "solid": "単色"}
INPUT_NAMES = {"merge": ["下（背景）", "上（前景）"], "matte": ["映像", "マット"], "effect": ["入力"], "output": ["入力"]}


def node_params(node: dict) -> List[P]:
    """ノードのパラメーター定義（effect は fxlib のもの）。"""
    t = node.get("type")
    if t == "effect":
        e = fxlib.get(node.get("fx", {}).get("name", ""))
        return e.params if e else []
    if t == "merge":
        from .render import BLEND_MODES
        return [P("mode", "ブレンドモード", "通常", choices=[lab for _, lab in BLEND_MODES]),
                P("opacity", "不透明度", 1.0, 0, 1), P("x", "ずらす X", 0.0, -2000, 2000, 1, suffix=" px"),
                P("y", "ずらす Y", 0.0, -2000, 2000, 1, suffix=" px")]
    if t == "matte":
        return [P("source", "マットに使う", "アルファ", choices=["アルファ", "明るさ"]), P("invert", "反転", False)]
    if t == "solid":
        return [P("color", "色", "#000000"), P("opacity", "不透明度", 1.0, 0, 1)]
    return []


def node_title(node: dict) -> str:
    if node.get("type") == "effect":
        e = fxlib.get(node.get("fx", {}).get("name", ""))
        return e.name if e else node.get("fx", {}).get("name", "?")
    return NODE_LABEL.get(node.get("type"), node.get("type"))


def _nid() -> str:
    return uuid.uuid4().hex[:8]


# ─────────────────────────── 作成・変換 ───────────────────────────

def new_graph() -> dict:
    return {"nodes": {"in": {"type": "input", "x": 0, "y": 0}, "out": {"type": "output", "x": 420, "y": 0}},
            "links": [["in", "out", 0]]}


def from_stack(effects: list) -> dict:
    """レイヤー（スタック）→ ノード: 入力 → エフェクト… → 出力 の 1 本の鎖にする。"""
    g = {"nodes": {"in": {"type": "input", "x": 0, "y": 0}}, "links": []}
    prev = "in"
    x = 0
    for fx in effects or []:
        if not fxlib.get(fx.get("name", "")):
            continue
        x += 210
        nid = _nid()
        g["nodes"][nid] = {"type": "effect", "fx": copy.deepcopy(fx), "x": x, "y": 0}
        g["links"].append([prev, nid, 0])
        prev = nid
    g["nodes"]["out"] = {"type": "output", "x": x + 210, "y": 0}
    g["links"].append([prev, "out", 0])
    return g


def to_stack(g: dict) -> Optional[list]:
    """ノード → レイヤー: 入力から出力までが分岐のない 1 本の鎖なら、その順のエフェクト一覧。違えば None。"""
    nodes = g.get("nodes", {})
    out_id = next((k for k, n in nodes.items() if n.get("type") == "output"), None)
    if out_id is None:
        return None
    chain = []
    cur = out_id
    seen = set()
    while True:
        ins = [l for l in g.get("links", []) if l[1] == cur]
        if len(ins) != 1:
            return None
        src = ins[0][0]
        if src in seen:
            return None
        seen.add(src)
        n = nodes.get(src)
        if n is None:
            return None
        if n["type"] == "input":
            break
        if n["type"] != "effect":
            return None
        chain.append(copy.deepcopy(n["fx"]))
        cur = src
    used = {l[0] for l in g.get("links", [])}
    extra = [k for k, n in nodes.items() if n["type"] not in ("input", "output") and k not in seen]
    if extra:
        return None
    return list(reversed(chain))


def append_effect(g: dict, fx: dict) -> str:
    """出力の直前にエフェクトのノードを差し込む。"""
    nodes = g["nodes"]
    out_id = next(k for k, n in nodes.items() if n["type"] == "output")
    link = next((l for l in g["links"] if l[1] == out_id), None)
    nid = _nid()
    ox, oy = nodes[out_id].get("x", 400), nodes[out_id].get("y", 0)
    nodes[nid] = {"type": "effect", "fx": copy.deepcopy(fx), "x": ox, "y": oy}
    nodes[out_id]["x"] = ox + 210
    if link is not None:
        g["links"].remove(link)
        g["links"].append([link[0], nid, 0])
    g["links"].append([nid, out_id, 0])
    return nid


def graph_animated(g: dict) -> bool:
    return any(n.get("type") == "effect" and fxlib.is_animated([n.get("fx", {})]) for n in g.get("nodes", {}).values())


def _expands(g: dict) -> bool:
    for n in g.get("nodes", {}).values():
        if n.get("type") == "effect":
            e = fxlib.get(n.get("fx", {}).get("name", ""))
            if e is not None and e.expands:
                return True
        if n.get("type") == "merge" and ((n.get("params") or {}).get("x") or (n.get("params") or {}).get("y")):
            return True
    return False


# ─────────────────────────── 評価 ───────────────────────────

def _fit(im: Image.Image, size) -> Image.Image:
    """キャンバスの大きさにそろえる（中央に置く / はみ出しは切る）。"""
    if im.size == size:
        return im
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(im.convert("RGBA"), ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
    return out


def _params(node: dict) -> dict:
    p = {q.key: q.default for q in node_params(node)}
    p.update(node.get("params") or {})
    return p


def _merge(bg: Image.Image, fg: Image.Image, p: dict, ctx) -> Image.Image:
    from .render import BLEND_MODES, blend_rgb
    mode = next((k for k, lab in BLEND_MODES if lab == p["mode"] or k == p["mode"]), "normal")
    dx, dy = int(float(p["x"]) * ctx.scale), int(float(p["y"]) * ctx.scale)
    if dx or dy:
        moved = Image.new("RGBA", fg.size, (0, 0, 0, 0))
        moved.paste(fg, (dx, dy))
        fg = moved
    b = np.asarray(bg).astype(np.float32) / 255
    f = np.asarray(fg).astype(np.float32) / 255
    fa = f[..., 3:4] * float(p["opacity"])
    ba = b[..., 3:4]
    col = blend_rgb(b[..., :3], f[..., :3], mode) if mode != "normal" else f[..., :3]
    # 背景が透明な所では前景の色そのまま
    col = col * ba + f[..., :3] * (1 - ba)
    out_a = fa + ba * (1 - fa)
    rgb = (col * fa + b[..., :3] * ba * (1 - fa)) / np.maximum(out_a, 1e-6)
    return Image.fromarray(np.clip(np.dstack([rgb, out_a]) * 255 + 0.5, 0, 255).astype(np.uint8), "RGBA")


def _matte(src: Image.Image, matte: Image.Image, p: dict) -> Image.Image:
    m = matte.getchannel("A") if p["source"] == "アルファ" else matte.convert("L")
    if p["invert"]:
        from PIL import ImageOps
        m = ImageOps.invert(m)
    from PIL import ImageChops
    out = src.copy()
    out.putalpha(ImageChops.multiply(src.getchannel("A"), m))
    return out


def evaluate(g: dict, im: Image.Image, ctx) -> Image.Image:
    """ノードグラフを評価して出力ノードの画像を返す（キャンバスは入力の大きさ。広がるエフェクトがあれば余白付き）。"""
    nodes = g.get("nodes", {})
    links = g.get("links", [])
    out_id = next((k for k, n in nodes.items() if n.get("type") == "output"), None)
    if out_id is None:
        return im
    im = im.convert("RGBA")
    if _expands(g):
        pad = int(max(im.size) * 0.25)
        big = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
        big.paste(im, (pad, pad))
        im = big
    size = im.size
    empty = Image.new("RGBA", size, (0, 0, 0, 0))
    memo: Dict[str, Image.Image] = {}
    busy = set()

    def inp(nid, port):
        l = next((l for l in links if l[1] == nid and int(l[2]) == port), None)
        return run(l[0]) if l else None

    def run(nid) -> Image.Image:
        if nid in memo:
            return memo[nid]
        if nid in busy or nid not in nodes:
            return empty                       # 輪になっている / 存在しない
        busy.add(nid)
        n = nodes[nid]
        t = n.get("type")
        try:
            if t == "input":
                r = im
            elif t == "output":
                r = inp(nid, 0) or empty
            elif t == "effect":
                src = inp(nid, 0) or empty
                r = _fit(fxlib.apply(src, n.get("fx", {}), ctx) or src, size)
            elif t == "merge":
                r = _merge(inp(nid, 0) or empty, inp(nid, 1) or empty, _params(n), ctx)
            elif t == "matte":
                a = inp(nid, 0) or empty
                m = inp(nid, 1)
                r = _matte(a, m, _params(n)) if m is not None else a
            elif t == "solid":
                p = _params(n)
                from .fx_builtin import _rgb
                r = Image.new("RGBA", size, _rgb(p["color"]) + (int(255 * float(p["opacity"])),))
            else:
                r = empty
        except Exception:
            r = empty
        busy.discard(nid)
        memo[nid] = r
        return r

    return run(out_id)


# ─────────────────────────── 編集画面 ───────────────────────────

from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal          # noqa: E402
from PyQt6.QtGui import QBrush, QColor, QPainterPath, QPen, QFont, QAction  # noqa: E402
from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGraphicsView, QGraphicsScene,  # noqa: E402
                             QGraphicsRectItem, QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsSimpleTextItem,
                             QGraphicsItem, QMenu, QLabel, QPushButton, QScrollArea, QFormLayout, QSplitter,
                             QCheckBox, QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit, QFileDialog)

NODE_W, NODE_H = 170, 54
TYPE_COLOR = {"input": "#2f6f4f", "output": "#7a3b3b", "effect": "#3a4f7a", "merge": "#6a4f8a", "matte": "#7a6a2f",
              "solid": "#555555"}


class _Port(QGraphicsEllipseItem):
    def __init__(self, node_item, index, is_output):
        super().__init__(-6, -6, 12, 12, node_item)
        self.node_item, self.index, self.is_output = node_item, index, is_output
        self.setBrush(QBrush(QColor("#ffcc33" if is_output else "#dddddd")))
        self.setPen(QPen(QColor("#222"), 1))
        self.setZValue(2)
        self.setAcceptHoverEvents(True)


class _NodeItem(QGraphicsRectItem):
    def __init__(self, editor, nid, node):
        super().__init__(0, 0, NODE_W, NODE_H)
        self.editor, self.nid = editor, nid
        self.setBrush(QBrush(QColor(TYPE_COLOR.get(node["type"], "#444"))))
        self.setPen(QPen(QColor("#111"), 1))
        self.setFlags(QGraphicsItem.GraphicsItemFlag.ItemIsMovable | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
                      QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setPos(node.get("x", 0), node.get("y", 0))
        title = QGraphicsSimpleTextItem(node_title(node), self)
        title.setBrush(QBrush(QColor("#ffffff")))
        f = QFont()
        f.setBold(True)
        title.setFont(f)
        title.setPos(8, 6)
        sub = QGraphicsSimpleTextItem(NODE_LABEL.get(node["type"], ""), self)
        sub.setBrush(QBrush(QColor("#cccccc")))
        sub.setPos(8, 30)
        if node.get("type") == "effect" and not node.get("fx", {}).get("on", True):
            self.setOpacity(0.5)
        n_in = NODE_INPUTS.get(node["type"], 0)
        self.inputs = []
        for i in range(n_in):
            p = _Port(self, i, False)
            p.setPos(0, NODE_H * (i + 1) / (n_in + 1))
            p.setToolTip((INPUT_NAMES.get(node["type"]) or [""] * n_in)[i])
            self.inputs.append(p)
        self.output = None
        if node["type"] != "output":
            self.output = _Port(self, 0, True)
            self.output.setPos(NODE_W, NODE_H / 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.editor._update_links()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        self.editor._moved(self)

    def mouseDoubleClickEvent(self, e):
        self.editor._toggle_node(self.nid)


class _Scene(QGraphicsScene):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        self._drag = None
        self._tmp = None

    def mousePressEvent(self, e):
        it = self.itemAt(e.scenePos(), self.views()[0].transform())
        if isinstance(it, _Port):
            self._drag = it
            self._tmp = QGraphicsPathItem()
            self._tmp.setPen(QPen(QColor("#ffcc33"), 2, Qt.PenStyle.DashLine))
            self.addItem(self._tmp)
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            a = self._drag.scenePos()
            self._tmp.setPath(_curve(a, e.scenePos()) if self._drag.is_output else _curve(e.scenePos(), a))
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self.removeItem(self._tmp)
            target = next((i for i in self.items(e.scenePos()) if isinstance(i, _Port)), None)
            src = self._drag
            self._drag = self._tmp = None
            if target is not None and target.is_output != src.is_output:
                o, i = (src, target) if src.is_output else (target, src)
                if o.node_item is not i.node_item:
                    self.editor.connect(o.node_item.nid, i.node_item.nid, i.index)
            elif target is None and not src.is_output:
                self.editor.disconnect_input(src.node_item.nid, src.index)
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.editor._context_menu(e.screenPos(), e.scenePos())


def _curve(a: QPointF, b: QPointF) -> QPainterPath:
    p = QPainterPath(a)
    dx = max(40.0, abs(b.x() - a.x()) / 2)
    p.cubicTo(QPointF(a.x() + dx, a.y()), QPointF(b.x() - dx, b.y()), b)
    return p


class NodeEditor(QWidget):
    """選んだクリップのノードグラフを編集する（ドック）。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.clip_id: Optional[str] = None
        self._items: Dict[str, _NodeItem] = {}
        self._links: List[QGraphicsPathItem] = []
        self._editing = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.title = QLabel("クリップを選んでください")
        self.mode = QComboBox()
        self.mode.addItem("レイヤー（スタック）", "stack")
        self.mode.addItem("ノード", "nodes")
        self.mode.setToolTip("このクリップのエフェクトの管理方法")
        self.mode.currentIndexChanged.connect(self._mode_changed)
        add = QPushButton("＋ ノード")
        add.clicked.connect(lambda: self._context_menu(self.mapToGlobal(add.geometry().bottomLeft()), QPointF(200, 120)))
        fit = QPushButton("全体表示")
        fit.clicked.connect(self.fit)
        top.addWidget(self.title, 1)
        top.addWidget(QLabel("管理方法"))
        top.addWidget(self.mode)
        top.addWidget(add)
        top.addWidget(fit)
        lay.addLayout(top)
        split = QSplitter(Qt.Orientation.Horizontal)
        self.scene = _Scene(self)
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHints(self.view.renderHints())
        self.view.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.view.setBackgroundBrush(QBrush(QColor("#1d1f23")))
        self.scene.selectionChanged.connect(self._selection)
        split.addWidget(self.view)
        self.side = QScrollArea()
        self.side.setWidgetResizable(True)
        self.side.setMinimumWidth(220)
        split.addWidget(self.side)
        split.setSizes([600, 260])
        lay.addWidget(split, 1)
        hint = QLabel("ポートをドラッグでつなぐ / 入力ポートから空き地へドラッグで外す / 右クリックでノード追加 / "
                      "Delete で削除 / ダブルクリックで ON・OFF")
        hint.setObjectName("info")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        self.setMinimumWidth(240)
        from PyQt6.QtGui import QShortcut, QKeySequence
        QShortcut(QKeySequence("Delete"), self.view, activated=self.delete_selected,
                  context=Qt.ShortcutContext.WidgetShortcut)

    # ── データ ──
    def clip(self):
        if not self.clip_id:
            return None
        return self.mw.project.sequence.find(self.clip_id)[1]

    def graph(self) -> Optional[dict]:
        c = self.clip()
        return c.prop("fx_graph") if c is not None else None

    def load_clip(self, clip):
        self.clip_id = clip.id if clip is not None else None
        self.refresh()

    def refresh(self):
        self.scene.clear()
        self._items, self._links = {}, []
        c = self.clip()
        self.mode.blockSignals(True)
        if c is None:
            self.title.setText("クリップを選んでください")
            self.mode.setEnabled(False)
            self.mode.blockSignals(False)
            self._show_params(None)
            return
        self.mode.setEnabled(True)
        node_mode = c.prop("fx_mode") == "nodes"
        self.mode.setCurrentIndex(1 if node_mode else 0)
        self.mode.blockSignals(False)
        self.title.setText(f"{c.name or c.kind}（{c.kind}）")
        if not node_mode:
            t = self.scene.addSimpleText("このクリップは「レイヤー（スタック）」で管理しています。\n"
                                         "管理方法を「ノード」にすると、今のエフェクトがノードになります。")
            t.setBrush(QBrush(QColor("#aaaaaa")))
            self._show_params(None)
            return
        g = c.prop("fx_graph") or new_graph()
        for nid, n in g["nodes"].items():
            it = _NodeItem(self, nid, n)
            self.scene.addItem(it)
            self._items[nid] = it
        self._update_links()
        self._show_params(None)

    def _update_links(self):
        for l in self._links:
            self.scene.removeItem(l)
        self._links = []
        g = self.graph()
        if not g:
            return
        for src, dst, port in g.get("links", []):
            a, b = self._items.get(src), self._items.get(dst)
            if a is None or b is None or a.output is None or int(port) >= len(b.inputs):
                continue
            path = QGraphicsPathItem(_curve(a.output.scenePos(), b.inputs[int(port)].scenePos()))
            path.setPen(QPen(QColor("#e0e0e0"), 2))
            path.setZValue(-1)
            self.scene.addItem(path)
            self._links.append(path)

    def fit(self):
        r = self.scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
        self.view.fitInView(r, Qt.AspectRatioMode.KeepAspectRatio)

    # ── 変更（取り消しできるように begin_edit / end_edit）──
    def _edit(self, label, fn):
        c = self.clip()
        if c is None:
            return
        self.mw.begin_edit()
        g = copy.deepcopy(c.prop("fx_graph") or new_graph())
        fn(g)
        c.props["fx_graph"] = g
        self.mw.end_edit(label)
        self.refresh()

    def _mode_changed(self):
        c = self.clip()
        if c is None:
            return
        want = self.mode.currentData()
        set_fx_mode(self.mw, c, want)
        self.refresh()

    def connect(self, src, dst, port):
        def fn(g):
            g["links"] = [l for l in g["links"] if not (l[1] == dst and int(l[2]) == port)]
            g["links"].append([src, dst, port])
        self._edit("ノードをつなぐ", fn)

    def disconnect_input(self, dst, port):
        self._edit("ノードを外す", lambda g: g.__setitem__(
            "links", [l for l in g["links"] if not (l[1] == dst and int(l[2]) == port)]))

    def add_node(self, typ, pos: QPointF, fx_id: str = ""):
        def fn(g):
            n = {"type": typ, "x": pos.x(), "y": pos.y()}
            if typ == "effect":
                e = fxlib.get(fx_id)
                n["fx"] = {"name": fx_id, "params": e.defaults() if e else {}, "on": True}
            g["nodes"][_nid()] = n
        self._edit("ノードを追加", fn)

    def delete_selected(self):
        ids = [it.nid for it in self.scene.selectedItems() if isinstance(it, _NodeItem)]
        ids = [i for i in ids if (self.graph() or {}).get("nodes", {}).get(i, {}).get("type") not in ("input", "output")]
        if not ids:
            return

        def fn(g):
            # 消したノードの前後をつなぎ直す（1 入力 1 出力のとき）
            for nid in ids:
                ins = [l for l in g["links"] if l[1] == nid and int(l[2]) == 0]
                outs = [l for l in g["links"] if l[0] == nid]
                g["links"] = [l for l in g["links"] if nid not in (l[0], l[1])]
                if ins:
                    for o in outs:
                        g["links"].append([ins[0][0], o[1], o[2]])
                g["nodes"].pop(nid, None)
        self._edit("ノードを削除", fn)

    def _toggle_node(self, nid):
        n = (self.graph() or {}).get("nodes", {}).get(nid)
        if n and n.get("type") == "effect":
            self._edit("ノードの ON / OFF", lambda g: g["nodes"][nid]["fx"].__setitem__(
                "on", not g["nodes"][nid]["fx"].get("on", True)))

    def _moved(self, item):
        g = self.graph()
        if not g or item.nid not in g["nodes"]:
            return
        n = g["nodes"][item.nid]
        if (n.get("x"), n.get("y")) != (item.pos().x(), item.pos().y()):
            n["x"], n["y"] = item.pos().x(), item.pos().y()       # 位置だけは取り消し履歴に積まない
            self.mw._set_dirty()

    def _context_menu(self, screen_pos, scene_pos):
        if self.clip() is None or self.mode.currentData() != "nodes":
            return
        m = QMenu(self)
        cats = fxlib.categories()
        em = m.addMenu("エフェクト")
        for cname in sorted(cats):
            sub = em.addMenu(cname)
            for e in sorted(cats[cname], key=lambda e: e.name):
                sub.addAction(e.name, lambda eid=e.id: self.add_node("effect", scene_pos, eid))
        m.addAction("合成（マージ・ブレンドモード）", lambda: self.add_node("merge", scene_pos))
        m.addAction("マット（トラックマット）", lambda: self.add_node("matte", scene_pos))
        m.addAction("単色", lambda: self.add_node("solid", scene_pos))
        m.exec(screen_pos.toPoint() if hasattr(screen_pos, "toPoint") else screen_pos)

    # ── パラメーター ──
    def _selection(self):
        sel = [it for it in self.scene.selectedItems() if isinstance(it, _NodeItem)]
        self._show_params(sel[0].nid if len(sel) == 1 else None)

    def _show_params(self, nid):
        w = QWidget()
        form = QFormLayout(w)
        g = self.graph() or {}
        n = g.get("nodes", {}).get(nid) if nid else None
        if n is None:
            form.addRow(QLabel("ノードを選ぶとパラメーターを編集できます"))
            self.side.setWidget(w)
            return
        form.addRow(QLabel(f"<b>{node_title(n)}</b>"))
        vals = fxlib.params_of(n["fx"]) if n["type"] == "effect" else _params(n)
        for prm in node_params(n):
            form.addRow(prm.label, self._param_widget(nid, prm, vals.get(prm.key, prm.default)))
        self.side.setWidget(w)

    def _param_widget(self, nid, prm: P, cur):
        def setv(v):
            self._set_param(nid, prm.key, v)
        if prm.kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(cur))
            w.toggled.connect(lambda v: setv(bool(v)))
        elif prm.kind == "choice":
            w = QComboBox()
            w.addItems(prm.choices)
            w.setCurrentText(str(cur))
            w.currentTextChanged.connect(setv)
        elif prm.kind == "color":
            from .inspector import ColorPick
            w = ColorPick(setv)
            w.set(str(cur))
        elif prm.kind == "file":
            w = QPushButton(str(cur).split("/")[-1].split("\\")[-1] or "選ぶ...")
            w.clicked.connect(lambda: (lambda p: setv(p) if p else None)(
                QFileDialog.getOpenFileName(self, prm.label, "", "LUT (*.cube);;すべて (*.*)")[0]))
        elif prm.kind == "int":
            w = QSpinBox()
            w.setRange(int(prm.min), int(prm.max))
            w.setValue(int(cur))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v: setv(int(v)))
        else:
            w = QDoubleSpinBox()
            w.setRange(float(prm.min), float(prm.max))
            w.setSingleStep(float(prm.step))
            w.setDecimals(0 if prm.step >= 1 else 2)
            w.setValue(float(cur))
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda v: setv(float(v)))
        return w

    def _set_param(self, nid, key, val):
        c = self.clip()
        if c is None:
            return
        self.mw.begin_edit()
        g = copy.deepcopy(c.prop("fx_graph"))
        n = g["nodes"][nid]
        if n["type"] == "effect":
            n["fx"]["params"] = {**(n["fx"].get("params") or {}), key: val}
        else:
            n["params"] = {**(n.get("params") or {}), key: val}
        c.props["fx_graph"] = g
        self.mw.end_edit("ノードのパラメーター", reload_inspector=False)


def set_fx_mode(mw, clip, mode: str) -> bool:
    """クリップのエフェクトの管理方法を切り替える（中身は自動で変換）。"""
    from PyQt6.QtWidgets import QMessageBox
    cur = "nodes" if clip.prop("fx_mode") == "nodes" else "stack"
    if mode == cur:
        return True
    mw.begin_edit()
    if mode == "nodes":
        if not clip.prop("fx_graph") or to_stack(clip.prop("fx_graph")) != (clip.prop("effects") or []):
            clip.props["fx_graph"] = from_stack(clip.prop("effects") or [])
        clip.props["fx_mode"] = "nodes"
        mw.end_edit("エフェクトをノードで管理")
        return True
    chain = to_stack(clip.prop("fx_graph") or new_graph())
    if chain is None:
        r = QMessageBox.question(mw, "レイヤーに戻す",
                                 "ノードに分岐や合成があるため、レイヤー（上から順）では同じ結果になりません。\n"
                                 "ノードはそのまま残し（使わない状態にして）、レイヤーのエフェクトに切り替えますか？")
        if r != QMessageBox.StandardButton.Yes:
            mw.end_edit(None)
            return False
    else:
        clip.props["effects"] = chain
    clip.props["fx_mode"] = "stack"
    mw.end_edit("エフェクトをレイヤーで管理")
    return True
