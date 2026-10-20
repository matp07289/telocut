"""キーボード設定（Avid Media Composer のキーボード設定ウィンドウ風）。

- 仮想キーボードに、右のコマンドパレットからコマンドをドラッグして割り当て
- 上の「レイヤー」で Shift / Ctrl / Alt の割り当てを切替（Shift / Ctrl を押している間も切替わる）
- キーをクリックすると、そのキーの割り当てを下に表示。右クリックで解除
- 「一覧」タブで検索・キー入力による割り当て
- プリセット（Avid 標準 / Premiere 風）の適用、書き出し / 読み込み（他の PC と共有）
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QMimeData, QByteArray, pyqtSignal, QSize
from PyQt6.QtGui import QDrag, QKeySequence, QColor, QPainter, QPen, QFont
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QWidget, QLabel,
                             QPushButton, QComboBox, QTabWidget, QListWidget, QListWidgetItem,
                             QTableWidget, QTableWidgetItem, QLineEdit, QMenu, QFileDialog,
                             QMessageBox, QButtonGroup, QToolButton, QAbstractItemView,
                             QHeaderView, QSplitter, QKeySequenceEdit, QFrame)

from .commands import (COMMAND_INFO, CATEGORIES, PRESETS, preset_keymap, conflicts,
                       is_avid_standard)
from .theme import C

MIME_CMD = "application/x-telocut-cmd"

# (表示, キー名, 幅)
_F_ROW = [("Esc", "Escape", 1.2)] + [(f"F{i}", f"F{i}", 1) for i in range(1, 13)]
LAYOUTS = {
    "jis": [
        _F_ROW,
        [(c, c, 1) for c in "1234567890"] + [("-", "-", 1), ("^", "^", 1), ("¥", "¥", 1), ("BS", "Backspace", 1.6)],
        [("Tab", "Tab", 1.5)] + [(c, c, 1) for c in "QWERTYUIOP"] + [("@", "@", 1), ("[", "[", 1), ("Enter", "Return", 1.4)],
        [("Caps", "", 1.8)] + [(c, c, 1) for c in "ASDFGHJKL"] + [(";", ";", 1), (":", ":", 1), ("]", "]", 1), ("", "", 1.1)],
        [("Shift", "", 2.3)] + [(c, c, 1) for c in "ZXCVBNM"] + [(",", ",", 1), (".", ".", 1), ("/", "/", 1), ("\\", "\\", 1), ("Shift", "", 1.7)],
        [("", "", 3.5), ("Space", "Space", 6.0), ("", "", 0.5), ("Ins", "Insert", 1), ("Del", "Delete", 1),
         ("Home", "Home", 1), ("End", "End", 1), ("PgUp", "PgUp", 1), ("PgDn", "PgDown", 1)],
        [("", "", 12.0), ("←", "Left", 1), ("↑", "Up", 1), ("↓", "Down", 1), ("→", "Right", 1)],
    ],
    "us": [
        _F_ROW,
        [("`", "`", 1)] + [(c, c, 1) for c in "1234567890"] + [("-", "-", 1), ("=", "=", 1), ("BS", "Backspace", 2)],
        [("Tab", "Tab", 1.5)] + [(c, c, 1) for c in "QWERTYUIOP"] + [("[", "[", 1), ("]", "]", 1), ("\\", "\\", 1.5)],
        [("Caps", "", 1.8)] + [(c, c, 1) for c in "ASDFGHJKL"] + [(";", ";", 1), ("'", "'", 1), ("Enter", "Return", 2.2)],
        [("Shift", "", 2.3)] + [(c, c, 1) for c in "ZXCVBNM"] + [(",", ",", 1), (".", ".", 1), ("/", "/", 1), ("Shift", "", 2.7)],
        [("", "", 3.5), ("Space", "Space", 6.0), ("", "", 0.5), ("Ins", "Insert", 1), ("Del", "Delete", 1),
         ("Home", "Home", 1), ("End", "End", 1), ("PgUp", "PgUp", 1), ("PgDn", "PgDown", 1)],
        [("", "", 12.0), ("←", "Left", 1), ("↑", "Up", 1), ("↓", "Down", 1), ("→", "Right", 1)],
    ],
}
LAYERS = ["", "Shift+", "Ctrl+", "Alt+", "Ctrl+Shift+", "Ctrl+Alt+"]
LAYER_NAMES = ["なし", "Shift", "Ctrl", "Alt", "Ctrl+Shift", "Ctrl+Alt"]
CAT_COLORS = {"再生・移動": "#3f6fb5", "マーク": "#2f8f8f", "編集": "#b5873f", "トラック": "#6a6a9a",
              "トリム": "#a54a6a", "ツール": "#5f8f3f", "レンダリング": "#8a5fb0",
              "ファイル": "#6a6a6a", "ウィンドウ": "#6a6a6a", "オーディオ": "#2f8f6a",
              "書き出し": "#7a6a3a"}


def norm(k: str) -> str:
    """キー表記を正規化（大文字小文字・表記ゆれを吸収）。"""
    s = QKeySequence(k).toString(QKeySequence.SequenceFormat.PortableText)
    return s or k


class KeyCap(QFrame):
    """キー1つ。コマンドのドロップを受け付ける。"""

    clicked = pyqtSignal(str)
    dropped = pyqtSignal(str, str)      # key, command id
    cleared = pyqtSignal(str)

    def __init__(self, label: str, key: str, width: float):
        super().__init__()
        self.label = label
        self.key = key
        self.cmd: Optional[str] = None
        self.selected = False
        self.setFixedSize(int(46 * width), 46)
        self.setAcceptDrops(bool(key))
        if not key:
            self.setEnabled(False)

    def set_cmd(self, cmd: Optional[str]):
        self.cmd = cmd
        info = COMMAND_INFO.get(cmd) if cmd else None
        self.setToolTip(f"{self.label}: {info[1]}" if info else self.label)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        r = self.rect().adjusted(1, 1, -2, -2)
        if not self.key:
            if self.label:
                p.setPen(QColor(C["border"]))
                p.setBrush(QColor(C["panel2"]))
                p.drawRoundedRect(r, 4, 4)
                p.setPen(QColor(C["dim"]))
                p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.label)
            p.end()
            return
        info = COMMAND_INFO.get(self.cmd) if self.cmd else None
        bg = QColor(CAT_COLORS.get(info[2], C["panel3"])) if info else QColor(C["panel3"])
        p.setPen(QPen(QColor(C["selected"] if self.selected else C["border"]), 2 if self.selected else 1))
        p.setBrush(bg)
        p.drawRoundedRect(r, 4, 4)
        f = QFont(self.font())
        f.setPointSizeF(7)
        p.setFont(f)
        p.setPen(QColor("#e8e8ee"))
        p.drawText(r.adjusted(4, 2, 0, 0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self.label)
        if info:
            f.setPointSizeF(8)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(r.adjusted(2, 12, -2, 0), Qt.AlignmentFlag.AlignCenter, info[3])
        p.end()

    def mousePressEvent(self, e):
        if not self.key:
            return
        if e.button() == Qt.MouseButton.RightButton:
            m = QMenu(self)
            m.addAction("割り当てを解除", lambda: self.cleared.emit(self.key))
            m.exec(e.globalPosition().toPoint())
        else:
            self.clicked.emit(self.key)

    def dragEnterEvent(self, e):
        if e.mimeData().hasFormat(MIME_CMD):
            e.acceptProposedAction()

    def dropEvent(self, e):
        cmd = bytes(e.mimeData().data(MIME_CMD)).decode()
        self.dropped.emit(self.key, cmd)
        e.acceptProposedAction()


class _CmdList(QListWidget):
    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)

    def startDrag(self, actions):
        it = self.currentItem()
        if it is None:
            return
        md = QMimeData()
        md.setData(MIME_CMD, QByteArray(it.data(Qt.ItemDataRole.UserRole).encode()))
        d = QDrag(self)
        d.setMimeData(md)
        d.exec(Qt.DropAction.CopyAction)


class KeyboardDialog(QDialog):
    def __init__(self, parent, keymap: Dict[str, List[str]], preset: str, layout: str = "jis"):
        super().__init__(parent)
        self.setWindowTitle("キーボード設定")
        self.resize(1180, 760)
        self.keymap = {k: [norm(x) for x in v] for k, v in keymap.items()}
        self.preset = preset
        self.layout_name = layout if layout in LAYOUTS else "jis"
        self.layer = 0
        self.caps: Dict[str, KeyCap] = {}
        self.sel_key: Optional[str] = None

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("プリセット:"))
        self.preset_box = QComboBox()
        for k, v in PRESETS.items():
            self.preset_box.addItem(v, k)
        self.preset_box.setCurrentIndex(max(0, list(PRESETS).index(preset) if preset in PRESETS else 0))
        top.addWidget(self.preset_box)
        b = QPushButton("このプリセットに戻す")
        b.clicked.connect(self._apply_preset)
        top.addWidget(b)
        top.addSpacing(16)
        top.addWidget(QLabel("配列:"))
        self.lay_box = QComboBox()
        self.lay_box.addItem("日本語 (JIS)", "jis")
        self.lay_box.addItem("英語 (US)", "us")
        self.lay_box.setCurrentIndex(0 if self.layout_name == "jis" else 1)
        self.lay_box.currentIndexChanged.connect(self._change_layout)
        top.addWidget(self.lay_box)
        top.addStretch(1)
        for txt, fn in (("書き出し...", self._export), ("読み込み...", self._import)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            top.addWidget(b)
        root.addLayout(top)

        layer_row = QHBoxLayout()
        layer_row.addWidget(QLabel("レイヤー:"))
        self.layer_group = QButtonGroup(self)
        for i, name in enumerate(LAYER_NAMES):
            tb = QToolButton()
            tb.setText(name)
            tb.setCheckable(True)
            tb.setChecked(i == 0)
            tb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            tb.clicked.connect(lambda _=False, i=i: self.set_layer(i))
            self.layer_group.addButton(tb, i)
            layer_row.addWidget(tb)
        hint = QLabel("　右のコマンドをキーにドラッグして割り当て / 右クリックで解除 / Shift・Ctrl を押すとレイヤー表示が切替わります")
        hint.setObjectName("info")
        layer_row.addWidget(hint)
        layer_row.addStretch(1)
        root.addLayout(layer_row)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.kb_host = QWidget()
        self.kb_layout = QVBoxLayout(self.kb_host)
        self.kb_layout.setSpacing(2)
        lv.addWidget(self.kb_host)
        self.detail = QLabel("キーをクリックすると、全レイヤーの割り当てを表示します")
        self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(70)
        self.detail.setStyleSheet(f"background: {C['bg']}; padding: 6px; border: 1px solid {C['border']};")
        lv.addWidget(self.detail)
        self.conflict_label = QLabel("")
        self.conflict_label.setWordWrap(True)
        self.conflict_label.setStyleSheet(f"color: {C['overwrite']}; background: transparent;")
        lv.addWidget(self.conflict_label)
        lv.addStretch(1)
        split.addWidget(left)

        # 右: コマンドパレット / 一覧
        self.tabs = QTabWidget()
        pal = QTabWidget()
        pal.setTabPosition(QTabWidget.TabPosition.West)
        for cat in CATEGORIES:
            lst = _CmdList()
            for cid, (c_id, name, ccat, short) in COMMAND_INFO.items():
                if ccat == cat:
                    it = QListWidgetItem(f"[{short}]  {name}")
                    it.setData(Qt.ItemDataRole.UserRole, cid)
                    lst.addItem(it)
            pal.addTab(lst, cat)
        self.tabs.addTab(pal, "コマンドパレット")

        listw = QWidget()
        lw = QVBoxLayout(listw)
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔎 コマンド名・キーで検索")
        self.search.textChanged.connect(self._fill_table)
        lw.addWidget(self.search)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["コマンド", "キー", "区分"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._capture_for_row(r))
        lw.addWidget(self.table, 1)
        row = QHBoxLayout()
        b = QPushButton("キーを入力して追加...")
        b.clicked.connect(lambda: self._capture_for_row(self.table.currentRow()))
        row.addWidget(b)
        b = QPushButton("このコマンドの割り当てを全解除")
        b.clicked.connect(self._clear_row)
        row.addWidget(b)
        row.addStretch(1)
        lw.addLayout(row)
        note = QLabel("区分: 「Avid 標準」= Media Composer 標準配列と同じ /「TeloCut」= 標準で未割り当ての機能に独自に割り当て")
        note.setObjectName("info")
        note.setWordWrap(True)
        lw.addWidget(note)
        self.tabs.addTab(listw, "一覧")
        split.addWidget(self.tabs)
        split.setSizes([800, 380])
        root.addWidget(split, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        ok = QPushButton("OK")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("キャンセル")
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        root.addLayout(btns)

        self._build_keyboard()
        self._fill_table()

    # ── キーボード描画 ─────────────────────────
    def _build_keyboard(self):
        while self.kb_layout.count():
            it = self.kb_layout.takeAt(0)
            w = it.layout()
            if w is not None:
                while w.count():
                    ww = w.takeAt(0).widget()
                    if ww:
                        ww.deleteLater()
        self.caps.clear()
        for row in LAYOUTS[self.layout_name]:
            h = QHBoxLayout()
            h.setSpacing(2)
            for label, key, width in row:
                cap = KeyCap(label, key, width)
                if key:
                    self.caps[key] = cap
                    cap.clicked.connect(self._on_key_clicked)
                    cap.dropped.connect(self._on_drop)
                    cap.cleared.connect(self._on_clear)
                h.addWidget(cap)
            h.addStretch(1)
            self.kb_layout.addLayout(h)
        self._refresh_caps()

    def _cmd_for(self, full_key: str) -> Optional[str]:
        nk = norm(full_key).lower()
        for cid, keys in self.keymap.items():
            if any(k.lower() == nk for k in keys):
                return cid
        return None

    def _refresh_caps(self):
        prefix = LAYERS[self.layer]
        for key, cap in self.caps.items():
            cap.selected = key == self.sel_key
            cap.set_cmd(self._cmd_for(prefix + key))
        dup = conflicts(self.keymap)
        if dup:
            items = [f"{k}: " + " / ".join(COMMAND_INFO[c][1] for c in v if c in COMMAND_INFO)
                     for k, v in list(dup.items())[:6]]
            self.conflict_label.setText("⚠ 重複している割り当て（どちらも動作しません）: " + "　".join(items))
        else:
            self.conflict_label.setText("")

    def set_layer(self, i: int):
        self.layer = i
        self.layer_group.button(i).setChecked(True)
        self._refresh_caps()
        if self.sel_key:
            self._on_key_clicked(self.sel_key)

    def keyPressEvent(self, e):
        if isinstance(self.focusWidget(), (QLineEdit, QKeySequenceEdit)):
            return super().keyPressEvent(e)
        mods = e.modifiers()
        layer = self._layer_from_mods(mods, e.key())
        if layer is not None:
            self.set_layer(layer)
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt):
            self.set_layer(self._layer_from_mods(e.modifiers(), None) or 0)
        super().keyReleaseEvent(e)

    @staticmethod
    def _layer_from_mods(mods, key):
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        if key is not None and key not in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt):
            return None
        if ctrl and shift:
            return 4
        if ctrl and alt:
            return 5
        if ctrl:
            return 2
        if shift:
            return 1
        if alt:
            return 3
        return 0

    def _on_key_clicked(self, key: str):
        self.sel_key = key
        lines = []
        for i, pre in enumerate(LAYERS):
            cid = self._cmd_for(pre + key)
            if cid:
                tag = "Avid 標準" if is_avid_standard(cid, norm(pre + key)) else "TeloCut"
                lines.append(f"<b>{pre}{key}</b>: {COMMAND_INFO[cid][1]}  <span style='color:{C['dim']}'>({tag})</span>")
        self.detail.setText("<br>".join(lines) if lines else f"<b>{key}</b>: 割り当てなし（コマンドをドラッグ）")
        self._refresh_caps()

    def _on_drop(self, key: str, cmd: str):
        full = norm(LAYERS[self.layer] + key)
        # 既存の割り当てを外してから付ける
        for cid in list(self.keymap):
            self.keymap[cid] = [k for k in self.keymap[cid] if k.lower() != full.lower()]
        self.keymap.setdefault(cmd, []).append(full)
        self._refresh_caps()
        self._fill_table()
        self._on_key_clicked(key)

    def _on_clear(self, key: str):
        full = norm(LAYERS[self.layer] + key).lower()
        for cid in list(self.keymap):
            self.keymap[cid] = [k for k in self.keymap[cid] if k.lower() != full]
        self._refresh_caps()
        self._fill_table()

    # ── 一覧 ───────────────────────────────
    def _fill_table(self, *_):
        q = self.search.text().strip().lower() if hasattr(self, "search") else ""
        rows = []
        for cid, (_, name, cat, _) in COMMAND_INFO.items():
            keys = self.keymap.get(cid, [])
            ks = " , ".join(keys)
            if q and q not in name.lower() and q not in ks.lower() and q not in cat.lower():
                continue
            tag = "Avid 標準" if keys and all(is_avid_standard(cid, k) for k in keys) else \
                ("Avid 標準 + TeloCut" if any(is_avid_standard(cid, k) for k in keys) else
                 ("TeloCut" if keys else ""))
            rows.append((cid, name, ks, tag))
        self.table.setRowCount(len(rows))
        for r, (cid, name, ks, tag) in enumerate(rows):
            it = QTableWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, cid)
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, QTableWidgetItem(ks))
            self.table.setItem(r, 2, QTableWidgetItem(tag))

    def _capture_for_row(self, row: int):
        if row < 0:
            return
        cid = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        dlg = QDialog(self)
        dlg.setWindowTitle("キーを入力")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"「{COMMAND_INFO[cid][1]}」に割り当てるキーを押してください"))
        ed = QKeySequenceEdit()
        ed.setMaximumSequenceLength(1) if hasattr(ed, "setMaximumSequenceLength") else None
        v.addWidget(ed)
        h = QHBoxLayout()
        okb = QPushButton("追加")
        okb.clicked.connect(dlg.accept)
        h.addStretch(1)
        h.addWidget(okb)
        v.addLayout(h)
        ed.setFocus()
        if dlg.exec() and not ed.keySequence().isEmpty():
            k = ed.keySequence().toString(QKeySequence.SequenceFormat.PortableText).split(",")[0].strip()
            for c in list(self.keymap):
                self.keymap[c] = [x for x in self.keymap[c] if x.lower() != k.lower()]
            self.keymap.setdefault(cid, []).append(k)
            self._fill_table()
            self._refresh_caps()

    def _clear_row(self):
        r = self.table.currentRow()
        if r < 0:
            return
        cid = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        self.keymap[cid] = []
        self._fill_table()
        self._refresh_caps()

    # ── プリセット・入出力 ───────────────────
    def _apply_preset(self):
        self.preset = self.preset_box.currentData()
        self.keymap = {k: [norm(x) for x in v] for k, v in preset_keymap(self.preset).items()}
        self._refresh_caps()
        self._fill_table()

    def _change_layout(self, _i):
        self.layout_name = self.lay_box.currentData()
        self._build_keyboard()

    def _export(self):
        p, _ = QFileDialog.getSaveFileName(self, "キーボード設定を書き出し", "telocut_keyboard.json",
                                           "キーボード設定 (*.json)")
        if p:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"app": "TeloCut", "type": "keyboard", "preset": self.preset,
                           "keymap": self.keymap}, f, ensure_ascii=False, indent=1)

    def _import(self):
        p, _ = QFileDialog.getOpenFileName(self, "キーボード設定を読み込み", "", "キーボード設定 (*.json)")
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            km = d.get("keymap", {})
            self.keymap = {k: [norm(x) for x in v] for k, v in km.items() if k in COMMAND_INFO}
            self.preset = d.get("preset", self.preset)
        except (OSError, ValueError) as ex:
            QMessageBox.warning(self, "読み込み", str(ex))
            return
        self._refresh_caps()
        self._fill_table()

    def result_keymap(self) -> Dict[str, List[str]]:
        return {k: v for k, v in self.keymap.items() if v}
