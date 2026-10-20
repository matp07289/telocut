"""レンダーキャッシュとバックグラウンドレンダリング（Avid の「レンダリング」相当）。

- キャッシュのキーは render.frame_signature()（フレームの見た目を決める情報のハッシュ）。
  クリップを変更すると、そのクリップが映っているフレームだけが自動的に「未レンダリング」に戻る。
- RAM（直近の数百フレーム）とローカルディスク（JPEG）の2段。ディスクは容量上限を超えたら古い順に削除。
- NAS で作業していても、レンダー結果は各 PC のローカルに置く（settings.render_dir）。
"""
from __future__ import annotations

import copy
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image
from PyQt6.QtCore import QThread, pyqtSignal

from .media import DecoderPool
from .model import Project, Sequence
from .settings import SETTINGS
from . import render


class RenderCache:
    def __init__(self):
        self.root = Path(SETTINGS.render_dir)
        self._ram: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._disk: set = set()
        self._lock = threading.Lock()
        self._scanned = False
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            keys = {p.stem for p in self.root.glob("*/*.jpg")}
        except OSError:
            keys = set()
        with self._lock:
            self._disk |= keys
            self._scanned = True

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.jpg"

    def has(self, key: str) -> bool:
        return key in self._ram or key in self._disk

    def on_disk(self, key: str) -> bool:
        return key in self._disk

    def get(self, key: str) -> Optional[np.ndarray]:
        with self._lock:
            arr = self._ram.get(key)
            if arr is not None:
                self._ram.move_to_end(key)
                return arr
            disk = key in self._disk
        if not disk:
            return None
        try:
            with Image.open(self._path(key)) as im:
                arr = np.asarray(im.convert("RGB"))
        except OSError:
            with self._lock:
                self._disk.discard(key)
            return None
        self.put_ram(key, arr)
        return arr

    def put_ram(self, key: str, arr: np.ndarray):
        with self._lock:
            self._ram[key] = arr
            self._ram.move_to_end(key)
            while len(self._ram) > max(10, SETTINGS.ram_cache_frames):
                self._ram.popitem(last=False)

    def put_disk(self, key: str, arr: np.ndarray):
        p = self._path(key)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            Image.fromarray(arr, "RGB").save(str(tmp), "JPEG", quality=92)
            os.replace(tmp, p)
            with self._lock:
                self._disk.add(key)
        except OSError:
            pass
        self.put_ram(key, arr)

    def clear_ram(self):
        with self._lock:
            self._ram.clear()

    def clear_disk(self):
        with self._lock:
            keys = list(self._disk)
            self._disk.clear()
            self._ram.clear()
        for k in keys:
            try:
                self._path(k).unlink()
            except OSError:
                pass

    def disk_usage(self) -> int:
        total = 0
        try:
            for p in self.root.glob("*/*.jpg"):
                total += p.stat().st_size
        except OSError:
            pass
        return total

    def prune(self):
        """容量上限を超えたら古いものから削除。"""
        limit = int(SETTINGS.render_cache_gb * 1024 ** 3)
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.root.glob("*/*.jpg")]
        except OSError:
            return
        total = sum(s for _, s, _ in files)
        if total <= limit:
            return
        for _, size, p in sorted(files):
            try:
                p.unlink()
            except OSError:
                continue
            with self._lock:
                self._disk.discard(p.stem)
            total -= size
            if total <= limit * 0.9:
                break


CACHE = RenderCache()


def snapshot(project: Project) -> Project:
    """スレッドで安全に読めるよう、プロジェクトを複製（シーケンスは現在のものだけ）。"""
    p = copy.copy(project)
    seq = copy.deepcopy(project.sequence)
    p.media = [copy.copy(m) for m in project.media]
    p.design = dict(project.design or {})
    p.sequences = [seq]
    p.current = 0
    return p


def segments(seq: Sequence) -> List[Tuple[int, int]]:
    """編集点で区切った区間のリスト。"""
    pts = seq.edit_points()
    end = seq.duration
    return [(a, b) for a, b in zip(pts, pts[1:]) if b > a and a < end]


class BackgroundRenderer(QThread):
    """指定範囲の「重い」フレームを並列でレンダリングしてディスクキャッシュへ。"""

    progress = pyqtSignal(int, int)
    finished_render = pyqtSignal(int, bool)     # 作成したフレーム数, 中止されたか

    def __init__(self, project: Project, a: int, b: int, scale: float, only_heavy: bool = True):
        super().__init__()
        self.project = snapshot(project)
        self.seq = self.project.sequence
        self.a, self.b = max(0, a), max(a, b)
        self.scale = scale
        self.only_heavy = only_heavy
        self.cancel = threading.Event()

    def frames_to_render(self) -> List[int]:
        out = []
        use_proxy = SETTINGS.use_proxy
        for f in range(self.a, self.b):
            if self.only_heavy and render.complexity(self.seq, f, self.project) == 0:
                continue
            key = render.frame_signature(self.seq, f, self.project, self.scale, use_proxy)
            if not CACHE.on_disk(key):
                out.append(f)
        return out

    def run(self):
        frames = self.frames_to_render()
        total = len(frames)
        if total == 0:
            self.finished_render.emit(0, False)
            return
        # 連続した区間に分けて各スレッドへ（デコーダは区間内で順方向に読める）
        n = max(1, SETTINGS.render_threads)
        chunk = max(15, (total + n - 1) // n)
        groups = [frames[i:i + chunk] for i in range(0, total, chunk)]
        done = [0]
        lock = threading.Lock()
        use_proxy = SETTINGS.use_proxy

        def work(group):
            pool = DecoderPool()
            try:
                for f in group:
                    if self.cancel.is_set():
                        return
                    key = render.frame_signature(self.seq, f, self.project, self.scale, use_proxy)
                    if CACHE.on_disk(key):
                        continue
                    arr = render.render_frame_np(self.seq, f, self.project, pool, self.scale)
                    CACHE.put_disk(key, np.ascontiguousarray(arr))
                    with lock:
                        done[0] += 1
                        d = done[0]
                    if d % 5 == 0:
                        self.progress.emit(d, total)
            finally:
                pool.close()

        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(work, groups))
        self.progress.emit(done[0], total)
        CACHE.prune()
        self.finished_render.emit(done[0], self.cancel.is_set())


class ProxyWorker(QThread):
    """プロキシをバックグラウンドで作成。"""

    progress = pyqtSignal(str, int, int)
    item_done = pyqtSignal(str, bool)

    def __init__(self, items):
        super().__init__()
        self.items = list(items)
        self.cancel = threading.Event()

    def run(self):
        from .media import build_proxy
        for it in self.items:
            if self.cancel.is_set():
                return
            ok = build_proxy(it, self.cancel,
                             lambda i, n, name=it.name: self.progress.emit(name, i, n)) is not None
            self.item_done.emit(it.id, ok)


class CopyWorker(QThread):
    """NAS 素材をローカルへバックグラウンドコピー（コピー中も元の場所の素材で編集可能）。"""

    progress = pyqtSignal(str, int, int)
    item_done = pyqtSignal(str, str, bool)     # media id, 新しいパス, 成功

    def __init__(self, jobs):
        super().__init__()
        self.jobs = list(jobs)       # [(media_id, src, dst)]
        self.cancel = threading.Event()

    def run(self):
        from .media import copy_file
        for mid, src, dst in self.jobs:
            if self.cancel.is_set():
                return
            name = os.path.basename(src)
            ok = copy_file(src, dst, self.cancel, lambda d, t, n=name: self.progress.emit(n, d, t))
            self.item_done.emit(mid, dst, ok)
