"""ファームのジョブ（書き出し・トランスコードを複数 PC に振り分け）の画面と、ジョブを送る処理（MainWindow のミックスイン）。"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QLabel, QMenu, QMessageBox,
                             QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import mediacodecs as MC
from .production import _Net, _req
from .settings import SETTINGS

ST_JP = {"waiting": "待ち", "running": "処理中", "done": "完了", "error": "失敗", "cancelled": "中止"}
ST_COL = {"waiting": "#aaaaaa", "running": "#4f8fe0", "done": "#50b060", "error": "#e05050", "cancelled": "#888888"}
KIND_JP = {"export": "書き出し", "transcode": "変換"}


def _hms(sec) -> str:
    s = int(sec or 0)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class FarmPanel(QWidget):
    COLS = ["ジョブ", "種類", "状態", "進み", "残り", "ノード", "依頼"]

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.net = _Net(self)
        self.jobs: List[dict] = []
        self.nodes: List[dict] = []
        self._fetched_once = False
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        h = QHBoxLayout()
        for txt, tip, fn in (("⟳", "読み直す", self.reload), ("中止", "選んだジョブを中止", lambda: self._act("cancel")),
                             ("やり直す", "失敗・中止したジョブをもう一度", lambda: self._act("retry")),
                             ("優先 ↑", "先にやってもらう", lambda: self._prio(1)), ("優先 ↓", "後回し", lambda: self._prio(-1)),
                             ("結果を取り込む", "できたファイルをこの PC に持ってきてビンへ", self.import_selected)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        self.auto = QCheckBox("終わったら自動で取り込む")
        self.auto.setChecked(bool(mw.settings.value("farm_auto_import", True, type=bool)))
        self.auto.toggled.connect(lambda on: mw.settings.setValue("farm_auto_import", on))
        h.addWidget(self.auto)
        v.addLayout(h)
        sp = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        sp.addWidget(self.table)
        nw = QWidget()
        nv = QVBoxLayout(nw)
        nv.setContentsMargins(0, 0, 0, 0)
        nv.addWidget(QLabel("レンダーノード（右クリックで一時停止 / 受ける仕事を選ぶ）"))
        self.ntable = QTableWidget(0, 4)
        self.ntable.setHorizontalHeaderLabels(["ノード", "状態", "今の仕事", "受ける仕事"])
        self.ntable.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.ntable.verticalHeader().setVisible(False)
        self.ntable.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ntable.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.ntable.customContextMenuRequested.connect(self._node_menu)
        nv.addWidget(self.ntable)
        sp.addWidget(nw)
        sp.setSizes([260, 140])
        v.addWidget(sp, 1)
        self.stat = QLabel("")
        self.stat.setObjectName("info")
        self.stat.setWordWrap(True)
        v.addWidget(self.stat)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def server(self) -> str:
        return (SETTINGS.asset_server or "").rstrip("/")

    def _tick(self):
        # 自分が出したジョブが終わったら取り込むので、見えていなくても見張る（待っているジョブがあるときだけ）
        if self.isVisible() or self.mw.farm_watch_pending():
            self.reload()

    def reload(self):
        url = self.server()
        if not url:
            self.stat.setText("アセットサーバーの URL を、アセットパネルの「サーバー」タブで設定してください")
            return
        self.net.run(lambda: (_req(url + "/api/farm/jobs?limit=80"), _req(url + "/api/farm/nodes")), self._loaded,
                     lambda ex: self.stat.setText(f"サーバーに接続できません: {ex}"))

    def _loaded(self, res):
        self.jobs, self.nodes = res
        self.mw.farm_on_jobs(self.jobs)
        if not self.isVisible():
            return
        sel = {self.table.item(i.row(), 0).data(Qt.ItemDataRole.UserRole) for i in self.table.selectedIndexes()
               if self.table.item(i.row(), 0)}
        self.table.setRowCount(len(self.jobs))
        for r, j in enumerate(self.jobs):
            prio = f"（優先 {j['priority']:+d}）" if j.get("priority") else ""
            vals = [j["name"] + prio, KIND_JP.get(j["kind"], j["kind"]), ST_JP.get(j["status"], j["status"]),
                    f"{j['progress'] * 100:.0f}%（{j['done']}/{j['total']}）", _hms(j["eta"]) if j.get("eta") else "",
                    ", ".join(j.get("nodes") or []), f"{j.get('user', '')} {time.strftime('%m/%d %H:%M', time.localtime(j['created']))}"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                if c == 0:
                    it.setData(Qt.ItemDataRole.UserRole, j["id"])
                    if j.get("error"):
                        it.setToolTip(j["error"])
                if c == 2:
                    it.setForeground(QColor(ST_COL.get(j["status"], "#ccc")))
                self.table.setItem(r, c, it)
            if j["id"] in sel:
                self.table.selectRow(r)
        self.ntable.setRowCount(len(self.nodes))
        for r, n in enumerate(self.nodes):
            st = "⏸ 一時停止" if n.get("disabled") else ("🟢 稼働" if n.get("online") else "⚪ 停止")
            b = n.get("busy")
            vals = [n["name"], st, f"{b['job']}（{b['task']} {b['progress'] * 100:.0f}%）" if b else "",
                    ", ".join(n.get("kinds") or []) or "すべて"]
            for c, val in enumerate(vals):
                it = QTableWidgetItem(val)
                it.setData(Qt.ItemDataRole.UserRole, n["name"])
                self.ntable.setItem(r, c, it)
        on = sum(1 for n in self.nodes if n.get("online") and not n.get("disabled"))
        run = sum(1 for j in self.jobs if j["status"] in ("waiting", "running"))
        self.stat.setText(f"稼働中のノード {on} 台・処理待ち / 処理中のジョブ {run} 件")

    def _selected_jobs(self) -> List[dict]:
        ids = {self.table.item(i.row(), 0).data(Qt.ItemDataRole.UserRole) for i in self.table.selectedIndexes()
               if self.table.item(i.row(), 0)}
        return [j for j in self.jobs if j["id"] in ids]

    def _act(self, what: str):
        url = self.server()
        js = self._selected_jobs()
        self.net.run(lambda: [_req(f"{url}/api/farm/jobs/{j['id']}/{what}", {}) for j in js], lambda _r: self.reload())

    def _prio(self, d: int):
        url = self.server()
        js = self._selected_jobs()
        self.net.run(lambda: [_req(f"{url}/api/farm/jobs/{j['id']}", {"priority": int(j.get("priority", 0)) + d}, "PATCH")
                              for j in js], lambda _r: self.reload())

    def import_selected(self):
        for j in self._selected_jobs():
            if j["status"] == "done":
                self.mw.farm_import_result(j, "add")

    def _node_menu(self, pos):
        it = self.ntable.itemAt(pos)
        if not it:
            return
        name = it.data(Qt.ItemDataRole.UserRole)
        n = next((x for x in self.nodes if x["name"] == name), {})
        url = self.server()
        m = QMenu(self)
        m.addAction("再開" if n.get("disabled") else "一時停止（新しい仕事を受けない）",
                    lambda: self.net.run(lambda: _req(f"{url}/api/farm/nodes/{name}", {"disabled": not n.get("disabled")}),
                                         lambda _r: self.reload()))
        sub = m.addMenu("受ける仕事")
        for lab, kinds in (("すべて", []), ("書き出しだけ", ["render", "merge", "full"]), ("変換だけ", ["transcode"])):
            sub.addAction(lab, lambda k=kinds: self.net.run(lambda: _req(f"{url}/api/farm/nodes/{name}", {"kinds": k}),
                                                            lambda _r: self.reload()))
        m.exec(self.ntable.viewport().mapToGlobal(pos))


class FarmMixin:
    def _farmui_commands(self) -> dict:
        return {"farm_panel": lambda: self.show_dock(self.dock_farm), "farm_export": self.farm_export_dialog}

    def _init_farmui(self):
        self.farm_panel = FarmPanel(self)
        self.dock_farm = self._dock("ジョブ（ファーム）", self.farm_panel, Qt.DockWidgetArea.RightDockWidgetArea, "farm")
        self.farm_panel.setMinimumWidth(240)
        self.tabifyDockWidget(self.dock_insp, self.dock_farm)
        self.dock_farm.hide()
        self.docks.append(self.dock_farm)
        self._farm_watch: Dict[str, dict] = {}

    def _farm_server(self) -> str:
        url = (SETTINGS.asset_server or "").rstrip("/")
        if not url:
            QMessageBox.information(self, "ファーム", "アセットサーバーの URL を、アセットパネルの「サーバー」タブで設定してください。")
        return url

    def farm_watch_pending(self) -> bool:
        return any(not w.get("handled") for w in self._farm_watch.values())

    # ── 書き出しジョブ ──
    def farm_export_dialog(self):
        from .export import ExportDialog
        dlg = ExportDialog(self)
        i = dlg.mode.findData("farmjob")
        if i >= 0:
            dlg.mode.setCurrentIndex(i)
        dlg.show()
        self._farm_dlg = dlg

    def farm_submit_export(self, a: int, b: int, preset: str, local_path: str = "", alpha: bool = False,
                           quality: int = 18, chunks: int = 0, name: str = "", priority: int = 0) -> Optional[dict]:
        """今のシーケンスをファームの書き出しジョブとして送る（待たずに編集を続けられる）。"""
        url = self._farm_server()
        if not url:
            return None
        seq = self.project.sequence
        if chunks <= 0:
            from .rendercluster import online_nodes
            n = len(online_nodes(url))
            chunks = max(1, min(max(1, n) * 2, (b - a) // max(1, int(seq.fps * 4))))
        import socket
        j = _req(url + "/api/farm/export", {
            "name": name or seq.name, "project": self.project.to_json(), "a": a, "b": b, "preset": preset,
            "alpha": alpha, "quality": quality, "chunks": chunks, "fps": seq.fps, "user": SETTINGS.user_name,
            "client": socket.gethostname(), "priority": priority}, timeout=60)
        self._farm_watch[j["id"]] = {"how": "file", "path": local_path, "name": j["name"]}
        self.status.showMessage(f"ファームに送りました: {j['name']}（{chunks} 分割）。進み具合は「ジョブ（ファーム）」", 6000)
        return j

    # ── 変換ジョブ（この PC のファイル）──
    def farm_submit_transcode(self, media, preset: str, size: str, dst_dir: str, how: str) -> list:
        url = self._farm_server()
        if not url:
            return []
        items = [{"src_path": m.path, "name": m.name, "dst_dir": dst_dir} for m in media]
        jobs = _req(url + "/api/farm/transcode", {"items": items, "preset": preset, "size": size,
                                                  "user": SETTINGS.user_name}, timeout=60)
        for m, j in zip(media, jobs):
            self._farm_watch[j["id"]] = {"how": how, "media_id": m.id, "name": j["name"], "dst_dir": dst_dir}
        self.status.showMessage(f"{len(jobs)} 件の変換をファームに送りました", 6000)
        return jobs

    # ── 終わったジョブ ──
    def farm_on_jobs(self, jobs: List[dict]):
        for j in jobs:
            w = self._farm_watch.get(j["id"])
            if not w or w.get("handled"):
                continue
            if j["status"] in ("error", "cancelled"):
                w["handled"] = True
                if hasattr(self, "toast"):
                    self.toast.show_text(f"⚠ ファーム: 「{j['name']}」が{ST_JP[j['status']]}" + (f"（{j.get('error', '')[:80]}）" if j.get("error") else ""))
            elif j["status"] == "done":
                w["handled"] = True
                if self.farm_panel.auto.isChecked():
                    self.farm_import_result(j, w.get("how", "add"), w)
                elif hasattr(self, "toast"):
                    self.toast.show_text(f"✓ ファーム: 「{j['name']}」が完了しました")

    def farm_import_result(self, j: dict, how: str = "add", w: Optional[dict] = None):
        """できたファイルを使えるようにする（この PC から見える場所ならそのまま、見えなければダウンロード）。"""
        w = w or {}
        url = (SETTINGS.asset_server or "").rstrip("/")
        local = j.get("result_path", "") if j.get("result_path") and os.path.exists(j.get("result_path", "")) else ""
        if local and w.get("path") and os.path.normcase(os.path.abspath(local)) != os.path.normcase(os.path.abspath(w["path"])):
            import shutil                                # 見えるが、書き出しで指定した場所は別 → そこへコピー
            dst = w["path"]
            self.farm_panel.net.run(lambda: (os.makedirs(os.path.dirname(dst) or ".", exist_ok=True), shutil.copy2(local, dst), dst)[2],
                                    lambda p: self._farm_use(j, p, how, w),
                                    lambda ex: self.status.showMessage(f"コピーできません: {ex}", 6000))
            return
        if not local and j.get("result_asset"):
            if w.get("path"):
                dst = w["path"]
            else:
                base = (Path(self.project_path).parent / ("FarmOutput" if j["kind"] == "export" else "Transcoded")) \
                    if self.project_path else Path(SETTINGS.cache_dir) / "farm_output"
                base.mkdir(parents=True, exist_ok=True)
                dst = str(base / Path(j.get("result_path") or j["name"]).name)
            from .farm import _download

            def fin(p):
                self._farm_use(j, p, how, w)
            self.farm_panel.net.run(lambda: _download(f"{url}/api/assets/{j['result_asset']}/file", dst), fin,
                                    lambda ex: self.status.showMessage(f"ダウンロードできません: {ex}", 6000))
            return
        if local:
            self._farm_use(j, local, how, w)

    def _farm_use(self, j: dict, path: str, how: str, w: dict):
        if how == "replace" and w.get("media_id"):
            self.apply_transcoded([(w["media_id"], path)], "replace")
        elif how in ("add", "replace"):
            self.import_files([path])
        if hasattr(self, "toast"):
            self.toast.show_text(f"✓ ファーム: 「{j['name']}」ができました\n{path}")
        self.status.showMessage(f"ファームの結果: {path}", 8000)
