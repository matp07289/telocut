"""アセットサーバーに変換（トランスコード）を頼む画面・変換ジョブの一覧・レンダーノードの一覧（TeloCut Assets で使う）。

変換はサーバーのファームのジョブになる（空いているレンダーノードかサーバー自身が行う）。結果はサーバーの
「アップロード（サーバー）/変換」か、サーバーの出力先（ES_Render など）に入る。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                             QVBoxLayout, QWidget)

STATUS = {"waiting": "待ち", "running": "変換中", "done": "完了", "error": "失敗", "cancelled": "中止"}


def _req(url: str, data=None, method=None, timeout=30):
    from .rendercluster import _req as r
    return r(url, data, method, timeout)


class _Sig(QObject):
    got = pyqtSignal(str, object)


def _bg(sig: _Sig, tag: str, fn, *a):
    def work():
        try:
            sig.got.emit(tag, fn(*a))
        except Exception as ex:
            sig.got.emit(tag, ex)
    threading.Thread(target=work, daemon=True).start()


class TranscodeDialog(QDialog):
    """選んだ素材の変換をサーバーに頼む（形式・大きさ・保存先・フォルダ名。前回の選び方を覚える）。"""

    def __init__(self, parent, server: str, ids: List[str], names: Optional[List[str]] = None, user: str = ""):
        super().__init__(parent)
        self.server, self.ids, self.user = server.rstrip("/"), list(ids), user
        self.qs = QSettings("DavinTelop", "TeloCutAssets")
        self.jobs: List[dict] = []
        self.setWindowTitle(f"変換を頼む（{len(self.ids)} 個）")
        self.resize(560, 300)
        self.sig = _Sig()
        self.sig.got.connect(self._got)
        f = QFormLayout(self)
        lab = QLabel("、".join((names or [])[:6]) + ("…" if names and len(names) > 6 else ""))
        lab.setWordWrap(True)
        f.addRow("素材", lab)
        self.preset = QComboBox()
        self.size = QComboBox()
        self.dest = QComboBox()
        self.sub = QLineEdit(str(self.qs.value("tc_sub", "") or ""))
        self.sub.setPlaceholderText("例: 2026-10_案件A（空なら「変換」の直下）")
        f.addRow("形式", self.preset)
        f.addRow("大きさ", self.size)
        f.addRow("保存先", self.dest)
        f.addRow("フォルダ名", self.sub)
        self.msg = QLabel("形式を読み込み中…")
        self.msg.setWordWrap(True)
        f.addRow(self.msg)
        self.bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setText("変換を頼む")
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.bb.accepted.connect(self.submit)
        self.bb.rejected.connect(self.reject)
        f.addRow(self.bb)
        _bg(self.sig, "presets", _req, self.server + "/api/presets", None, None, 20)

    def _got(self, tag, res):
        if isinstance(res, Exception):
            self.msg.setText(f"サーバーにつながりません: {res}")
            return
        if tag == "presets":
            self.fill(res)
        elif tag == "submit":
            self.jobs = list(res or [])
            self.accept()

    def fill(self, p: dict):
        last = self.qs.value("tc_preset", "")
        for x in p.get("presets", []):
            self.preset.addItem(f"{x['group']}｜{x['label']}" + (f"（{x['note']}）" if x.get("note") else ""), x["id"])
        i = self.preset.findData(last)
        if i >= 0:
            self.preset.setCurrentIndex(i)
        for x in p.get("sizes", []):
            self.size.addItem(x["label"], x["id"])
        i = self.size.findData(self.qs.value("tc_size", ""))
        if i >= 0:
            self.size.setCurrentIndex(i)
        for x in p.get("outputs", []):
            self.dest.addItem(x.get("label", x.get("id")), x.get("id"))
        i = self.dest.findData(self.qs.value("tc_dest", "output"))
        if i >= 0:
            self.dest.setCurrentIndex(i)
        self.msg.setText("変換はサーバーのファーム（空いている PC）が行います。終わると知らせます。")
        self.bb.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.preset.count() > 0)

    def body(self) -> dict:
        return {"ids": self.ids, "preset": self.preset.currentData(), "size": self.size.currentData() or "",
                "user": self.user, "dest": self.dest.currentData() or "", "sub": self.sub.text().strip()}

    def submit(self):
        b = self.body()
        for k, v in (("tc_preset", b["preset"]), ("tc_size", b["size"]), ("tc_dest", b["dest"]), ("tc_sub", b["sub"])):
            self.qs.setValue(k, v)
        self.msg.setText("頼んでいます…")
        _bg(self.sig, "submit", _req, self.server + "/api/transcode", b, None, 60)


class JobsPanel(QWidget):
    """変換ジョブの一覧（2 秒ごとに更新・中止・やり直し・結果を表示）。自分が頼んだものが終わると finished を出す。"""
    finished = pyqtSignal(dict)
    open_result = pyqtSignal(dict)

    def __init__(self, server_fn, user_fn=lambda: ""):
        super().__init__()
        self.server_fn, self.user_fn = server_fn, user_fn
        self.mine: Dict[str, str] = {}             # 自分が頼んだジョブ → 前の状態
        self.rows: List[dict] = []
        self.sig = _Sig()
        self.sig.got.connect(self._got)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 0)
        h = QHBoxLayout()
        self.info = QLabel("")
        self.info.setObjectName("info")
        for txt, fn in (("更新", self.refresh), ("中止", self.cancel_sel), ("やり直す", self.retry_sel),
                        ("結果を表示", self.show_result)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addWidget(self.info, 1)
        v.addLayout(h)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["素材", "形式", "状態", "進み具合", "担当", "頼んだ人"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda r, _c: self.show_result())
        v.addWidget(self.table, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def track(self, jobs: List[dict]):
        for j in jobs or []:
            self.mine[j["id"]] = j.get("status", "waiting")
        self.refresh()

    def _tick(self):
        if self.server_fn() and (self.isVisible() or any(s in ("waiting", "running") for s in self.mine.values())):
            self.refresh()

    def refresh(self):
        if self.server_fn():
            _bg(self.sig, "list", _req, self.server_fn() + "/api/transcode?limit=200", None, None, 15)

    def _got(self, tag, res):
        if isinstance(res, Exception):
            self.info.setText(f"サーバーにつながりません: {res}")
            return
        if tag == "list":
            self.fill(res)
        elif tag in ("cancel", "retry"):
            if tag == "retry":
                self.track(res)
            self.refresh()

    def fill(self, rows: List[dict]):
        self.rows = rows
        sel = self.current()
        self.table.setRowCount(len(rows))
        for i, j in enumerate(rows):
            vals = [j.get("name", ""), j.get("preset_label", ""), STATUS.get(j["status"], j["status"]), "",
                    ", ".join(j.get("nodes") or []), j.get("user", "")]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 2 and j["status"] == "error":
                    it.setToolTip(j.get("error", ""))
                self.table.setItem(i, c, it)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(100 if j["status"] == "done" else int(round(float(j.get("progress") or 0) * 100)))   # 進み具合は 0〜1
            bar.setFormat("%p%")
            self.table.setCellWidget(i, 3, bar)
            if sel and sel.get("id") == j["id"]:
                self.table.selectRow(i)
            old = self.mine.get(j["id"])
            if old is not None and old != j["status"]:
                self.mine[j["id"]] = j["status"]
                if j["status"] in ("done", "error", "cancelled"):
                    self.finished.emit(j)
        run = sum(1 for j in rows if j["status"] in ("waiting", "running"))
        self.info.setText(f"待ち・変換中 {run} 件 / 全 {len(rows)} 件")

    def current(self) -> Optional[dict]:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def cancel_sel(self):
        j = self.current()
        if j and j["status"] in ("waiting", "running"):
            _bg(self.sig, "cancel", _req, self.server_fn() + f"/api/transcode/{j['id']}/cancel", {}, None, 15)

    def retry_sel(self) -> bool:
        """失敗・中止したジョブを同じ形式で頼み直す。"""
        j = self.current()
        if not j or j["status"] not in ("error", "cancelled") or not j.get("asset_id"):
            self.info.setText("やり直せるのは、失敗・中止したサーバーの素材の変換だけです")
            return False
        body = {"ids": [j["asset_id"]], "preset": j.get("preset"), "size": j.get("size", ""), "user": self.user_fn(),
                "dest": j.get("dest", ""), "sub": j.get("sub", "")}
        _bg(self.sig, "retry", _req, self.server_fn() + "/api/transcode", body, None, 30)
        return True

    def show_result(self):
        j = self.current()
        if j and j["status"] == "done":
            self.open_result.emit(j)


class NodesDialog(QDialog):
    """レンダーノード（変換・書き出しをする PC）の状態。"""

    def __init__(self, parent, server: str):
        super().__init__(parent)
        self.setWindowTitle("レンダーノード")
        self.resize(640, 320)
        v = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名前", "状態", "仕事", "受ける仕事", "最後の連絡"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.table, 1)
        self.msg = QLabel("読み込み中…")
        v.addWidget(self.msg)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.sig = _Sig()
        self.sig.got.connect(self._got)
        _bg(self.sig, "nodes", _req, server.rstrip("/") + "/api/render/nodes", None, None, 15)

    def _got(self, _tag, res):
        if isinstance(res, Exception):
            self.msg.setText(f"読み込めません: {res}")
            return
        self.fill(res)

    def fill(self, nodes: List[dict]):
        self.table.setRowCount(len(nodes))
        for i, n in enumerate(nodes):
            seen = n.get("last_seen") or n.get("seen") or 0
            ago = f"{int(time.time() - float(seen))} 秒前" if seen else ""
            vals = [n.get("name", ""), "● オンライン" if n.get("online") else "○ オフライン", n.get("job") or n.get("task") or "",
                    ", ".join(n.get("kinds") or []) or "すべて", ago]
            for c, val in enumerate(vals):
                self.table.setItem(i, c, QTableWidgetItem(str(val)))
        self.msg.setText(f"{sum(1 for n in nodes if n.get('online'))} / {len(nodes)} 台がオンライン")
