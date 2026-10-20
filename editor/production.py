"""制作管理（ショットの進み具合・担当・期限）と更新履歴のパネル、Dynamic Link のコマンド（MainWindow のミックスイン）。"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSplitter,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.timecode import frames_to_timecode, timecode_to_frames
from . import model as M
from .settings import SETTINGS

STATUS = [("todo", "未着手", "#888888"), ("wip", "作業中", "#4f8fe0"), ("review", "レビュー待ち", "#d8c040"),
          ("fix", "修正", "#e05050"), ("done", "OK", "#50b060")]
ST_JP = {k: j for k, j, _c in STATUS}
ST_COL = {k: c for k, _j, c in STATUS}


class _Net:
    """サーバーとのやり取りを別スレッドで（結果は GUI スレッドで受け取る）。"""

    def __init__(self, owner):
        self.q: "queue.Queue" = queue.Queue()
        self.t = QTimer(owner)
        self.t.setInterval(100)
        self.t.timeout.connect(self._drain)
        self.t.start()

    def run(self, fn, cb=None, err=None):
        def work():
            try:
                r = fn()
                self.q.put((cb, r, None))
            except Exception as ex:
                self.q.put((err, None, ex))
        threading.Thread(target=work, daemon=True).start()

    def _drain(self):
        while True:
            try:
                cb, r, ex = self.q.get_nowait()
            except queue.Empty:
                return
            try:
                if ex is not None:
                    if cb:
                        cb(ex)
                elif cb:
                    cb(r)
            except RuntimeError:
                pass


def _req(url, data=None, method=None, timeout=10):
    from .rendercluster import _req as r
    return r(url, data, method=method, timeout=timeout)


class ProductionPanel(QWidget):
    COLS = ["ショット", "状態", "担当", "期限", "IN", "OUT", "メモ"]

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.net = _Net(self)
        self.shots: List[dict] = []
        self._loading = False
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        h = QHBoxLayout()
        h.addWidget(QLabel("プロジェクト"))
        self.proj = QLineEdit()
        self.proj.setPlaceholderText("（このプロジェクトの名前）")
        self.proj.editingFinished.connect(self.reload)
        h.addWidget(self.proj, 1)
        for txt, tip, fn in (("⟳", "読み直す", self.reload), ("ブラウザ", "ブラウザの制作管理を開く", self.open_web)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addLayout(h)
        h2 = QHBoxLayout()
        for txt, tip, fn in (("＋ ショット", "IN〜OUT（無ければ再生ヘッド）でショットを追加", self.add_shot),
                             ("マーカーから", "マーカーごとに 1 ショット（次のマーカーまで）", self.from_markers),
                             ("選択クリップから", "選んだクリップごとに 1 ショット", self.from_clips),
                             ("削除", "選んだショットを削除", self.delete_selected)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h2.addWidget(b)
        h2.addStretch(1)
        v.addLayout(h2)
        sp = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._edited)
        self.table.cellDoubleClicked.connect(self._jump)
        sp.addWidget(self.table)
        self.feed = QListWidget()
        sp.addWidget(self.feed)
        sp.setSizes([300, 120])
        v.addWidget(sp, 1)
        self.stat = QLabel("")
        self.stat.setObjectName("info")
        self.stat.setWordWrap(True)
        v.addWidget(self.stat)
        self.timer = QTimer(self)
        self.timer.setInterval(15000)
        self.timer.timeout.connect(lambda: self.reload() if self.isVisible() else None)
        self.timer.start()

    def server(self) -> str:
        return (SETTINGS.asset_server or "").rstrip("/")

    def project_name(self) -> str:
        if self.proj.text().strip():
            return self.proj.text().strip()
        if self.mw.project_path:
            return Path(self.mw.project_path).stem
        return self.mw.project.name or ""

    def showEvent(self, e):
        super().showEvent(e)
        self.reload()

    def _need_server(self) -> bool:
        if not self.server():
            self.stat.setText("アセットサーバーの URL を、アセットパネルの「サーバー」タブで設定してください")
            return False
        return True

    def reload(self):
        if not self._need_server():
            return
        url, proj = self.server(), self.project_name()
        import urllib.parse
        q = urllib.parse.quote(proj)
        self.net.run(lambda: (_req(f"{url}/api/shots?project={q}"), _req(f"{url}/api/activity?limit=40&project={q}")),
                     self._loaded, lambda ex: self.stat.setText(f"サーバーに接続できません: {ex}"))

    def _loaded(self, res):
        self.shots, acts = res
        self._loading = True
        try:
            self.table.setRowCount(len(self.shots))
            for r, s in enumerate(self.shots):
                vals = [s["name"], None, s.get("assignee", ""), s.get("due", ""), s.get("tc_in", ""), s.get("tc_out", ""),
                        s.get("note", "")]
                for c, val in enumerate(vals):
                    if c == 1:
                        cb = QComboBox()
                        for k, j, col in STATUS:
                            cb.addItem(j, k)
                        cb.setCurrentIndex(max(0, cb.findData(s.get("status", "todo"))))
                        cb.setStyleSheet(f"color: {ST_COL.get(s.get('status'), '#ccc')}")
                        cb.activated.connect(lambda _i, cb=cb, sid=s["id"]: self.update_shot(sid, {"status": cb.currentData()}))
                        self.table.setCellWidget(r, 1, cb)
                        continue
                    it = QTableWidgetItem(str(val or ""))
                    it.setData(Qt.ItemDataRole.UserRole, s["id"])
                    self.table.setItem(r, c, it)
            self.feed.clear()
            for a in acts:
                self.feed.addItem(f"{time.strftime('%m/%d %H:%M', time.localtime(a['time']))}  {a['text']}"
                                  + (f"（{a['user']}）" if a.get("user") else ""))
            done = sum(1 for s in self.shots if s.get("status") == "done")
            self.stat.setText(f"{len(self.shots)} ショット・OK {done}　状態はそのまま変更・ダブルクリックで IN へ移動")
        finally:
            self._loading = False

    def _edited(self, it: QTableWidgetItem):
        if self._loading:
            return
        key = {0: "name", 2: "assignee", 3: "due", 4: "tc_in", 5: "tc_out", 6: "note"}.get(it.column())
        sid = it.data(Qt.ItemDataRole.UserRole)
        if key and sid:
            self.update_shot(sid, {key: it.text()})

    def update_shot(self, sid: str, d: dict):
        url = self.server()
        d = {**d, "user": SETTINGS.user_name}
        self.net.run(lambda: _req(f"{url}/api/shots/{sid}", d, "PATCH"), lambda _r: self.reload(),
                     lambda ex: self.stat.setText(f"保存できません: {ex}"))

    def _tc(self, f: int) -> str:
        seq = self.mw.project.sequence
        return frames_to_timecode(f + self.mw.tc_offset(seq), seq.fps)

    def _post_items(self, items: List[dict]):
        if not self._need_server() or not items:
            return
        url = self.server()
        for x in items:
            x.setdefault("project", self.project_name())
            x.setdefault("seq", self.mw.project.sequence.name)
        self.net.run(lambda: _req(f"{url}/api/shots", {"items": items, "user": SETTINGS.user_name}, timeout=30),
                     lambda r: (self.stat.setText(f"{len(r)} ショットを追加しました"), self.reload()),
                     lambda ex: self.stat.setText(f"追加できません: {ex}"))

    def add_shot(self, name: str = ""):
        seq = self.mw.project.sequence
        a, b = (seq.mark_in, seq.mark_out) if 0 <= seq.mark_in < seq.mark_out else (self.mw.record_player.position, -1)
        if not name:
            name, ok = QInputDialog.getText(self, "ショットを追加", "名前:", text=f"S{len(self.shots) + 1:03d}")
            if not ok:
                return
        self._post_items([{"name": name or "ショット", "tc_in": self._tc(a), "tc_out": self._tc(b) if b >= 0 else "",
                           "assignee": SETTINGS.user_name}])

    def shot_items_from_markers(self) -> List[dict]:
        seq = self.mw.project.sequence
        ms = sorted(seq.markers, key=lambda m: m.frame)
        out = []
        for i, m in enumerate(ms):
            end = ms[i + 1].frame if i + 1 < len(ms) else seq.duration
            out.append({"name": m.name or f"S{i + 1:03d}", "tc_in": self._tc(m.frame), "tc_out": self._tc(end),
                        "note": m.comment})
        return out

    def from_markers(self):
        items = self.shot_items_from_markers()
        if not items:
            self.stat.setText("マーカーがありません")
            return
        self._post_items(items)

    def from_clips(self):
        clips = sorted((c for _, c in self.mw.canvas.selected_clips()), key=lambda c: c.start)
        self._post_items([{"name": c.name or c.prop("text", "") or f"S{i + 1:03d}", "tc_in": self._tc(c.start),
                           "tc_out": self._tc(c.end)} for i, c in enumerate(clips)])

    def delete_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        ids = [self.shots[r]["id"] for r in rows if r < len(self.shots)]
        if not ids or QMessageBox.question(self, "ショットの削除", f"{len(ids)} ショットを削除しますか？") \
                != QMessageBox.StandardButton.Yes:
            return
        url, u = self.server(), SETTINGS.user_name
        import urllib.parse
        self.net.run(lambda: [_req(f"{url}/api/shots/{i}?user={urllib.parse.quote(u)}", None, "DELETE") for i in ids],
                     lambda _r: self.reload())

    def _jump(self, r, c):
        if c == 1 or r >= len(self.shots):
            return
        tc = self.shots[r].get("tc_in") or ""
        seq = self.mw.project.sequence
        try:
            f = timecode_to_frames(tc, seq.fps) - self.mw.tc_offset(seq)
        except Exception:
            return
        self.mw.seek_record(max(0, f))

    def open_web(self):
        if self._need_server():
            import webbrowser
            webbrowser.open(self.server() + "/#v=browse")


class UpdatePanel(QWidget):
    """更新履歴（自動同期）と Dynamic Link の状態。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.addWidget(QLabel("素材の更新（他のアプリで保存されると自動で反映・前の版を保存）"))
        self.events = QListWidget()
        self.events.currentItemChanged.connect(lambda *_: self._show_versions())
        v.addWidget(self.events, 2)
        v.addWidget(QLabel("この素材の版（新しい順）"))
        self.vers = QListWidget()
        v.addWidget(self.vers, 1)
        h = QHBoxLayout()
        for txt, tip, fn in (("この版を使う", "素材をこの版のファイルに切り替える（元のファイルはそのまま）", self.use_version),
                             ("元のファイルに戻す", "元のファイルをこの版で上書きする", self.restore_version),
                             ("最新に戻す", "「この版を使う」をやめて元の場所のファイルに戻す", self.back_to_latest)):
            b = QPushButton(txt)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            h.addWidget(b)
        v.addLayout(h)
        v.addWidget(QLabel("Dynamic Link（After Effects / Blender をそのまま置いたクリップ）"))
        self.links = QListWidget()
        v.addWidget(self.links, 1)
        h2 = QHBoxLayout()
        for txt, fn in (("つなぐ...", lambda: self.mw.dynlink_import()), ("レンダリングし直す", self.rerender),
                        ("元のアプリで開く", self.open_src)):
            b = QPushButton(txt)
            b.clicked.connect(fn)
            h2.addWidget(b)
        v.addLayout(h2)
        self.stat = QLabel("")
        self.stat.setObjectName("info")
        self.stat.setWordWrap(True)
        v.addWidget(self.stat)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.timer.start()
        self._sig = None

    def refresh(self, force=False):
        h = self.mw.history
        evs = h.events(300)
        dl = self.mw.dynlink
        sig = (len(evs), evs[0]["time"] if evs else 0, tuple(sorted((k, v["status"], v["time"]) for k, v in dl.state.items())),
               len(dl.linked()))
        if sig == self._sig and not force:
            return
        self._sig = sig
        cur = self.events.currentRow()
        self.events.clear()
        for e in evs:
            it = QListWidgetItem(f"{time.strftime('%m/%d %H:%M:%S', time.localtime(e['time']))}  {e['name']}  "
                                 f"［{e.get('app', '')}］ {e.get('detail', '')}")
            it.setData(Qt.ItemDataRole.UserRole, e)
            self.events.addItem(it)
        if 0 <= cur < self.events.count():
            self.events.setCurrentRow(cur)
        self.links.clear()
        for m in dl.linked():
            st = dl.state.get(m.id, {})
            mark = {"rendering": "⟳", "ok": "✓", "error": "⚠", "waiting": "…"}.get(st.get("status"), "・")
            it = QListWidgetItem(f"{mark} {m.name}  {st.get('msg', '')}")
            it.setData(Qt.ItemDataRole.UserRole, m.id)
            self.links.addItem(it)

    def _media(self) -> Optional[M.MediaItem]:
        it = self.events.currentItem()
        if not it:
            return None
        return self.mw.project.media_by_id(it.data(Qt.ItemDataRole.UserRole).get("media_id"))

    def _show_versions(self):
        self.vers.clear()
        m = self._media()
        if m is None:
            return
        for vv in self.mw.history.versions(m.id):
            it = QListWidgetItem(f"{time.strftime('%m/%d %H:%M:%S', time.localtime(vv['time']))}  {vv['name']}  "
                                 f"{vv['size'] / 1024:.0f} KB" + ("  ← 使用中" if os.path.normcase(vv["path"]) == os.path.normcase(m.path) else ""))
            it.setData(Qt.ItemDataRole.UserRole, vv["path"])
            self.vers.addItem(it)

    def _version(self) -> Optional[str]:
        it = self.vers.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def use_version(self):
        m, p = self._media(), self._version()
        if not m or not p:
            self.stat.setText("更新と版を選んでください")
            return
        self.mw.use_media_version(m, p)
        self._show_versions()

    def back_to_latest(self):
        m = self._media()
        if m:
            self.mw.media_back_to_latest(m)
            self._show_versions()

    def restore_version(self):
        m, p = self._media(), self._version()
        if not m or not p:
            self.stat.setText("更新と版を選んでください")
            return
        self.mw.restore_media_version(m, p)
        self._show_versions()

    def _link(self) -> Optional[M.MediaItem]:
        it = self.links.currentItem()
        return self.mw.project.media_by_id(it.data(Qt.ItemDataRole.UserRole)) if it else None

    def rerender(self):
        m = self._link()
        if m:
            self.mw.dynlink.render(m)

    def open_src(self):
        m = self._link()
        if m:
            self.mw.dynlink.open_source(m)


class _Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setStyleSheet("background: rgba(20,20,24,230); color: #fff; border: 1px solid #7c6cf2; border-radius: 6px;"
                           " padding: 8px 12px;")
        self.setWordWrap(True)
        self.setMaximumWidth(420)
        self.hide()
        self.t = QTimer(self)
        self.t.setSingleShot(True)
        self.t.timeout.connect(self.hide)

    def show_text(self, text: str, ms: int = 4500):
        self.setText(text)
        self.adjustSize()
        p = self.parentWidget()
        self.move(p.width() - self.width() - 24, p.height() - self.height() - 48)
        self.raise_()
        self.show()
        self.t.start(ms)


class ProductionMixin:
    def _production_commands(self) -> dict:
        return {
            "dynlink_import": self.dynlink_import, "dynlink_rerender": self.dynlink_rerender_selected,
            "dynlink_open": self.dynlink_open_selected,
            "update_panel": lambda: self.show_dock(self.dock_updates),
            "production_panel": lambda: self.show_dock(self.dock_prod),
            "shots_from_markers": lambda: (self.show_dock(self.dock_prod), self.prod_panel.from_markers()),
        }

    def _init_production(self):
        from .dynlink import DynLink
        R = Qt.DockWidgetArea.RightDockWidgetArea
        self._hist = None
        self.dynlink = DynLink(self)
        self.dynlink.changed.connect(self._on_media_changed)
        self.update_panel = UpdatePanel(self)
        self.prod_panel = ProductionPanel(self)
        self.dock_updates = self._dock("更新履歴（自動同期）", self.update_panel, R, "updates")
        self.dock_prod = self._dock("制作管理", self.prod_panel, R, "production")
        for d in (self.dock_updates, self.dock_prod):
            d.widget().setMinimumWidth(240)
            self.tabifyDockWidget(self.dock_insp, d)
            d.hide()
        self.docks += [self.dock_updates, self.dock_prod]
        self.toast = _Toast(self)
        self.watcher.changed.connect(lambda mid, msg: self.toast.show_text("🔄 " + msg) if mid else None)
        self.dynlink.changed.connect(lambda mid, msg: self.toast.show_text("🔗 " + msg))

    def _build_production_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "連携・管理")
        for c in ("dynlink_import", "dynlink_rerender", "dynlink_open", None, "update_panel", None,
                  "production_panel", "shots_from_markers"):
            m.addSeparator() if c is None else self._a(m, c)

    # ── 更新履歴 ──
    @property
    def history(self):
        from .history import History
        base = (Path(self.project_path).parent / ".telocut_history") if getattr(self, "project_path", None) \
            else Path(SETTINGS.cache_dir) / "history"
        h = getattr(self, "_hist", None)
        if h is None or h.base != base:
            h = History(base, getattr(SETTINGS, "version_limit_mb", 300))
            h.listeners.append(self._history_event)
            self._hist = h
        return h

    def _history_event(self, ev: dict):
        """更新をアセットサーバーの「最近の動き」にも送る（チームで共有）。"""
        url = (SETTINGS.asset_server or "").rstrip("/")
        if not url:
            return
        proj = Path(self.project_path).stem if self.project_path else ""
        d = {"user": SETTINGS.user_name, "host": ev.get("host", ""), "project": proj, "kind": ev["kind"],
             "target": ev.get("path", ""), "text": f"{ev['name']} を更新（{ev.get('app', '')}）{ev.get('detail', '')}".strip()}

        def send():
            try:
                _req(url + "/api/activity", d, timeout=5)
            except Exception:
                pass
        threading.Thread(target=send, daemon=True).start()

    def _on_version(self, m: M.MediaItem) -> bool:
        return os.path.normcase(m.path).startswith(os.path.normcase(str(self.history.base)))

    def use_media_version(self, m: M.MediaItem, path: str):
        self.begin_edit()
        if not self._on_version(m):
            m.original_path = m.path
        m.path = path
        self.end_edit(f"{m.name}: 前の版を使う")
        self._after_media_swap(m)

    def media_back_to_latest(self, m: M.MediaItem):
        if not m.original_path or not self._on_version(m):
            return
        self.begin_edit()
        m.path, m.original_path = m.original_path, ""
        self.end_edit(f"{m.name}: 最新の版に戻す")
        self._after_media_swap(m)

    def restore_media_version(self, m: M.MediaItem, path: str):
        import shutil
        target = m.original_path if (m.original_path and self._on_version(m)) else m.path
        if QMessageBox.question(self, "元のファイルに戻す", f"{target}\nをこの版で上書きします。よろしいですか？\n"
                                "（今のファイルも版として残ります）") != QMessageBox.StandardButton.Yes:
            return
        self.history.snapshot(m.id, target)
        shutil.copy2(path, target)
        os.utime(target, None)
        if os.path.normcase(m.path) != os.path.normcase(target):
            self.begin_edit()
            m.path, m.original_path = target, ""
            self.end_edit(f"{m.name}: 版を元のファイルに戻す")
        self.history.log("restore", m.id, m.name, target, "TeloCut", f"{Path(path).name} の版に戻す")
        self._after_media_swap(m)

    def _after_media_swap(self, m: M.MediaItem):
        from . import media as MD
        MD.invalidate(m.path)
        try:
            st = os.stat(m.path)
            m.file_size, m.mtime = st.st_size, st.st_mtime
        except OSError:
            pass
        self.watcher._sig.pop(m.id, None)
        self._on_media_changed(m.id, f"{m.name} を切り替えました")

    # ── Dynamic Link ──
    def dynlink_import(self, path: str = "", comp: Optional[str] = None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Dynamic Link でつなぐ", "",
                                                  "After Effects / Blender (*.aep *.aepx *.blend)")
        if not path:
            return None
        if comp is None:
            is_ae = path.lower().endswith((".aep", ".aepx"))
            comp, ok = QInputDialog.getText(self, "Dynamic Link", "コンポジション名（空ならレンダーキューの 1 番目）:" if is_ae
                                            else "シーン名（空なら保存したときのシーン）:")
            if not ok:
                return None
        m = self.dynlink.link(path, comp.strip())
        self.show_dock(self.dock_updates)
        self.status.showMessage("Dynamic Link: 裏でレンダリングしています。向こうで保存するたびに自動で更新します", 6000)
        return m

    def _sel_linked(self) -> List[M.MediaItem]:
        ids = {c.media_id for _, c in self.canvas.selected_clips() if c.media_id}
        return [m for m in self.dynlink.linked() if m.id in ids]

    def dynlink_rerender_selected(self):
        ms = self._sel_linked()
        if not ms:
            QMessageBox.information(self, "Dynamic Link", "Dynamic Link のクリップを選んでください。")
        for m in ms:
            self.dynlink.render(m)

    def dynlink_open_selected(self):
        ms = self._sel_linked()
        if not ms:
            QMessageBox.information(self, "Dynamic Link", "Dynamic Link のクリップを選んでください。")
        for m in ms:
            self.dynlink.open_source(m)
