"""プロジェクトの保存・バックアップ・ロック・素材パス解決（NAS 運用向け）。

- 保存は「一時ファイルに書いて置き換え」。NAS への書き込み中に回線が切れても元ファイルは壊れない
- 保存のたびに Attic（ローカル）へ世代バックアップ（Avid の Attic と同じ考え方）
- 共有フォルダのプロジェクトは .lock ファイルで同時編集を防ぐ（誰がどの PC で開いているか表示）
- 素材は「絶対パス → プロジェクトからの相対パス → パス置換ルール → 再リンク」の順で探す
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
                             QPushButton, QLabel, QFileDialog, QHeaderView, QAbstractItemView,
                             QInputDialog, QMessageBox)

from .model import Project, MediaItem
from .settings import SETTINGS, AppSettings

LOCK_STALE_SEC = 2 * 3600      # ハートビートが途絶えて2時間で「古いロック」とみなす


# ─────────────────────────────────────────────
#  保存
# ─────────────────────────────────────────────

def atomic_write(path: str, text: str):
    tmp = f"{path}.{AppSettings.host()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def update_rel_paths(project: Project, project_path: str):
    base = os.path.dirname(os.path.abspath(project_path))
    for m in project.media:
        try:
            m.rel_path = os.path.relpath(m.path, base)
        except ValueError:          # ドライブが違う
            m.rel_path = ""


def attic_backup(project_path: Optional[str], text: str) -> Optional[Path]:
    stem = Path(project_path).stem if project_path else "untitled"
    d = Path(SETTINGS.attic_dir) / stem
    try:
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{stem}_{datetime.now():%Y%m%d_%H%M%S}.tcut"
        p.write_text(text, encoding="utf-8")
        files = sorted(d.glob(f"{stem}_*.tcut"), reverse=True)
        for old in files[max(1, SETTINGS.attic_keep):]:
            try:
                old.unlink()
            except OSError:
                pass
        return p
    except OSError:
        return None


def list_attic(project_path: Optional[str]) -> List[Path]:
    stem = Path(project_path).stem if project_path else "untitled"
    d = Path(SETTINGS.attic_dir) / stem
    try:
        return sorted(d.glob(f"{stem}_*.tcut"), reverse=True)
    except OSError:
        return []


# ─────────────────────────────────────────────
#  ロック（共有プロジェクト）
# ─────────────────────────────────────────────

class ProjectLock:
    def __init__(self):
        self.path: Optional[str] = None

    @staticmethod
    def lock_path(project_path: str) -> str:
        return project_path + ".lock"

    @staticmethod
    def read(project_path: str) -> Optional[dict]:
        try:
            with open(ProjectLock.lock_path(project_path), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def is_ours(info: Optional[dict]) -> bool:
        return bool(info) and info.get("user") == SETTINGS.user_name and info.get("host") == AppSettings.host()

    @staticmethod
    def is_stale(info: Optional[dict]) -> bool:
        return bool(info) and time.time() - float(info.get("time", 0)) > LOCK_STALE_SEC

    def acquire(self, project_path: str) -> bool:
        info = {"user": SETTINGS.user_name, "host": AppSettings.host(), "pid": os.getpid(),
                "time": time.time(), "opened": datetime.now().isoformat(timespec="seconds")}
        try:
            atomic_write(self.lock_path(project_path), json.dumps(info, ensure_ascii=False))
            self.path = project_path
            return True
        except OSError:
            return False

    def heartbeat(self):
        if not self.path:
            return
        info = self.read(self.path)
        if info is None or self.is_ours(info):
            self.acquire(self.path)

    def release(self):
        if not self.path:
            return
        info = self.read(self.path)
        if info is None or self.is_ours(info):
            try:
                os.remove(self.lock_path(self.path))
            except OSError:
                pass
        self.path = None


def describe_lock(info: dict) -> str:
    t = datetime.fromtimestamp(float(info.get("time", 0))).strftime("%Y-%m-%d %H:%M")
    return f"{info.get('user', '?')}（PC: {info.get('host', '?')}、最終確認 {t}）"


# ─────────────────────────────────────────────
#  素材パスの解決 / 再リンク
# ─────────────────────────────────────────────

def resolve_media(project: Project, project_path: Optional[str]) -> List[MediaItem]:
    """見つからない素材を相対パス・パス置換で探す。最後まで見つからないものを返す。"""
    base = os.path.dirname(os.path.abspath(project_path)) if project_path else ""
    offline = []
    for m in project.media:
        if os.path.exists(m.path):
            continue
        cands = []
        if base and m.rel_path:
            cands.append(os.path.normpath(os.path.join(base, m.rel_path)))
        mapped = SETTINGS.map_path(m.path)
        if mapped != m.path:
            cands.append(mapped)
        if m.original_path:
            cands.append(m.original_path)
            cands.append(SETTINGS.map_path(m.original_path))
        hit = next((c for c in cands if c and os.path.exists(c)), None)
        if hit:
            m.path = hit
        else:
            offline.append(m)
    return offline


def search_folder(folder: str, items: List[MediaItem], limit_files: int = 200000) -> Dict[str, str]:
    """フォルダ以下からファイル名（とサイズ）が一致するものを探す。{media id: path}"""
    want: Dict[str, List[MediaItem]] = {}
    for m in items:
        want.setdefault(os.path.basename(m.path).lower(), []).append(m)
    found: Dict[str, str] = {}
    n = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            n += 1
            if n > limit_files:
                return found
            lst = want.get(f.lower())
            if not lst:
                continue
            p = os.path.join(root, f)
            for m in lst:
                if m.id in found:
                    continue
                try:
                    if m.file_size and os.path.getsize(p) != m.file_size:
                        continue
                except OSError:
                    continue
                found[m.id] = p
        if len(found) == len(items):
            break
    return found


class RelinkDialog(QDialog):
    """オフライン素材の再リンク。"""

    def __init__(self, parent, project: Project, offline: List[MediaItem]):
        super().__init__(parent)
        self.setWindowTitle("オフライン素材の再リンク")
        self.resize(820, 420)
        self.project = project
        self.items = list(offline)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("次の素材が見つかりません。NAS の接続やドライブ文字を確認するか、場所を指定してください。"))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["素材", "以前の場所", "状態"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        v.addWidget(self.table, 1)
        h = QHBoxLayout()
        for text, fn in (("フォルダから自動検索...", self._search), ("選択した素材の場所を指定...", self._pick),
                         ("パス置換ルールを追加...", self._add_map)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            h.addWidget(b)
        h.addStretch(1)
        ok = QPushButton("閉じる")
        ok.clicked.connect(self.accept)
        h.addWidget(ok)
        v.addLayout(h)
        self._fill()

    def _fill(self):
        self.table.setRowCount(len(self.items))
        for r, m in enumerate(self.items):
            ok = os.path.exists(m.path)
            self.table.setItem(r, 0, QTableWidgetItem(m.name))
            self.table.setItem(r, 1, QTableWidgetItem(m.path))
            self.table.setItem(r, 2, QTableWidgetItem("✓ リンク済み" if ok else "オフライン"))

    def _search(self):
        d = QFileDialog.getExistingDirectory(self, "素材を探すフォルダ（NAS の共有フォルダなど）")
        if not d:
            return
        todo = [m for m in self.items if not os.path.exists(m.path)]
        found = search_folder(d, todo)
        for m in todo:
            if m.id in found:
                m.path = found[m.id]
        self._fill()
        QMessageBox.information(self, "再リンク", f"{len(found)} / {len(todo)} 個の素材を見つけました。")

    def _pick(self):
        r = self.table.currentRow()
        if r < 0:
            return
        m = self.items[r]
        p, _ = QFileDialog.getOpenFileName(self, f"{m.name} の場所", os.path.dirname(m.path))
        if p:
            m.path = p
            # 同じフォルダにある他のオフライン素材もまとめて解決
            folder = os.path.dirname(p)
            for o in self.items:
                if not os.path.exists(o.path):
                    c = os.path.join(folder, os.path.basename(o.path))
                    if os.path.exists(c):
                        o.path = c
            self._fill()

    def _add_map(self):
        r = max(0, self.table.currentRow())
        old = self.items[r].path if self.items else ""
        src, ok = QInputDialog.getText(self, "パス置換ルール", "置換前（例: \\\\NAS\\share または Z:\\）:",
                                       text=os.path.dirname(old))
        if not ok or not src.strip():
            return
        dst = QFileDialog.getExistingDirectory(self, "置換後のフォルダ（この PC での場所）")
        if not dst:
            return
        SETTINGS.path_maps.append([src.strip(), dst])
        SETTINGS.save()
        for m in self.items:
            if not os.path.exists(m.path):
                c = SETTINGS.map_path(m.path)
                if os.path.exists(c):
                    m.path = c
        self._fill()
