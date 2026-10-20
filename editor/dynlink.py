"""Dynamic Link 風の連携: After Effects のプロジェクト（.aep）・Blender のファイル（.blend）をそのままクリップとして置く。

- 置いた時点で裏でレンダリングして、できた映像をクリップの中身にする（書き出しの操作はいらない）
- 向こうのアプリで保存すると、保存が終わるのを待って自動でレンダリングし直し、タイムラインに反映
- AE は aerender（AE に付属）、Blender はバックグラウンドの blender -b で書き出す（透過付きの MOV）
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import media as MD
from . import model as M

KINDS = {".aep": "aftereffects", ".aepx": "aftereffects", ".blend": "blender"}
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BLENDER_OUT = r"""
import bpy, sys
out = sys.argv[sys.argv.index("--") + 1]
for sc in [bpy.context.scene]:
    r = sc.render
    r.image_settings.file_format = "FFMPEG"
    r.ffmpeg.format = "QUICKTIME"
    r.ffmpeg.codec = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.film_transparent = True
    r.filepath = out
    r.use_file_extension = False
bpy.ops.render.render(animation=True)
"""


def aerender_exe() -> str:
    from .integrations import find_app
    ae = find_app("aftereffects")
    if not ae:
        return ""
    p = Path(ae).with_name("aerender.exe")
    return str(p) if p.exists() else ""


def render_command(src: str, comp: str, out: str) -> List[str]:
    """レンダリングのコマンド。アプリが見つからなければ例外。"""
    kind = KINDS.get(Path(src).suffix.lower())
    if kind == "aftereffects":
        exe = aerender_exe()
        if not exe:
            raise RuntimeError("After Effects（aerender.exe）が見つかりません。環境設定で AfterFX.exe の場所を指定してください")
        cmd = [exe, "-project", src, "-output", out, "-OMtemplate", "Lossless with Alpha", "-sound", "OFF"]
        return cmd + (["-comp", comp] if comp else ["-rqindex", "1"])
    if kind == "blender":
        from .integrations import find_app
        exe = find_app("blender")
        if not exe:
            raise RuntimeError("Blender が見つかりません。環境設定で blender.exe の場所を指定してください")
        script = Path(out).parent / "_telocut_blender_out.py"
        if not script.exists() or script.read_text(encoding="utf-8") != BLENDER_OUT:
            script.write_text(BLENDER_OUT, encoding="utf-8")
        cmd = [exe, "-b", src, "--python", str(script), "--", out]
        if comp:
            cmd[3:3] = ["-S", comp]                           # シーン名
        return cmd
    raise RuntimeError(f"この形式はつなげません: {Path(src).suffix}")


def out_path(work: Path, src: str, comp: str) -> Path:
    h = hashlib.md5(f"{os.path.abspath(src)}|{comp}".encode("utf-8")).hexdigest()[:8]
    return work / f"{Path(src).stem}_{comp or 'main'}_{h}.mov"


class DynLink(QObject):
    """つないだ .aep / .blend の見張りとレンダリング。"""

    changed = pyqtSignal(str, str)          # media id, メッセージ
    state_changed = pyqtSignal(str)          # media id

    def __init__(self, mw, interval_ms: int = 2000):
        super().__init__(mw)
        self.mw = mw
        self.state: Dict[str, dict] = {}     # id → {"status": rendering/ok/error/waiting, "msg", "time"}
        self._sig: Dict[str, tuple] = {}
        self._busy: Dict[str, threading.Thread] = {}
        self.command: Callable[[str, str, str], List[str]] = render_command     # テストで差し替えられる
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._done = []
        self._done_lock = threading.Lock()
        self.flush_timer = QTimer(self)
        self.flush_timer.setInterval(200)
        self.flush_timer.timeout.connect(self._flush)
        self.flush_timer.start()

    def linked(self) -> List[M.MediaItem]:
        return [m for m in self.mw.project.media if getattr(m, "link_src", "")]

    @staticmethod
    def _stat(p):
        try:
            st = os.stat(p)
            return st.st_size, st.st_mtime
        except OSError:
            return None

    # ── つなぐ ──
    def link(self, src: str, comp: str = "", frame: Optional[int] = None) -> M.MediaItem:
        from .integrations import work_dir
        mw = self.mw
        seq = mw.project.sequence
        out = out_path(work_dir(mw, "DynamicLink"), src, comp)
        name = f"{Path(src).name}" + (f" [{comp}]" if comp else "")
        item = M.MediaItem(id=M.new_id(), path=str(out), name=name, kind="video", duration_s=5.0, fps=seq.fps,
                           width=seq.width, height=seq.height, has_video=True, has_audio=False)
        item.link_src = os.path.abspath(src)
        item.link_comp = comp
        f = mw.record_player.position if frame is None else frame
        dur = int(round(5 * seq.fps))
        mw.begin_edit()
        mw.project.media.append(item)
        (mw.project.bin_by_id(mw.bin.current_bin_id()) or mw.project.bins[0]).add("media", item.id)
        track = next((t for t in reversed(seq.video_tracks) if not t.locked and t.is_empty(f, f + dur)), None)
        if track is None:
            track = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
            seq.video_tracks.append(track)
        c = M.Clip(M.new_id(), "video", f, dur, 0, item.id, name, props={"link_placeholder": True})
        track.clips.append(c)
        track.sort()
        mw.end_edit(f"Dynamic Link: {name}")
        self._sig[item.id] = self._stat(item.link_src)
        self.render(item, first=True)
        return item

    # ── 見張り ──
    def poll(self):
        self._polls = getattr(self, "_polls", 0) + 1
        for m in self.linked():
            if m.id in self._busy:
                continue
            if self._polls % 30 == 0 and os.path.exists(m.path):
                self._cleanup(m)
            sig = self._stat(m.link_src)
            old = self._sig.get(m.id)
            if old is None:
                self._sig[m.id] = sig
                if sig and not os.path.exists(m.path):
                    self.render(m)                     # 開いたプロジェクトで、まだ映像が無い
                continue
            if isinstance(old, tuple) and old and old[0] == "changing":
                if sig == old[1]:
                    self._sig[m.id] = sig
                    self.render(m)
                else:
                    self._sig[m.id] = ("changing", sig)
            elif sig is not None and sig != old:
                self._sig[m.id] = ("changing", sig)    # 保存の途中かもしれない → 次も同じなら
                self._set(m.id, "waiting", "保存を確認しています…")

    def _set(self, mid, status, msg=""):
        self.state[mid] = {"status": status, "msg": msg, "time": time.time()}
        self.state_changed.emit(mid)

    # ── レンダリング ──
    def render(self, m: M.MediaItem, first: bool = False):
        if m.id in self._busy:
            return
        # 再生中のファイルは Windows では上書きできないので、毎回新しい名前で書き出して切り替える
        base = Path(m.path)
        stem = base.stem.split("__r")[0]
        tmp = str(base.with_name(f"{stem}__r{int(time.time() * 1000)}{base.suffix}"))
        try:
            cmd = self.command(m.link_src, getattr(m, "link_comp", ""), tmp)
        except Exception as ex:
            self._set(m.id, "error", str(ex))
            self.changed.emit(m.id, f"Dynamic Link: {ex}")
            return
        self._set(m.id, "rendering", "レンダリング中…")
        t0 = time.time()

        def work():
            err = ""
            try:
                Path(tmp).parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", creationflags=_NO_WINDOW,
                                   timeout=6 * 3600)
                if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
                    err = "レンダリングに失敗しました: " + " / ".join(tail)[:300]
            except Exception as ex:
                err = str(ex)
            if err:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            with self._done_lock:
                self._done.append((m.id, err, first, time.time() - t0, tmp))
        th = threading.Thread(target=work, daemon=True)
        self._busy[m.id] = th
        th.start()

    def _flush(self):
        with self._done_lock:
            done, self._done = self._done, []
        for mid, err, first, sec, new_path in done:
            self._busy.pop(mid, None)
            m = self.mw.project.media_by_id(mid)
            if m is None:
                continue
            if err:
                self._set(mid, "error", err)
                self.changed.emit(mid, f"Dynamic Link: {err}")
                continue
            old_size = m.file_size
            m.path = new_path
            self._cleanup(m)
            try:
                info = MD.probe(m.path)
                m.duration_s, m.fps, m.width, m.height = info.duration_s, info.fps or m.fps, info.width, info.height
                m.has_audio, m.file_size, m.mtime = info.has_audio, info.file_size, info.mtime
            except Exception as ex:
                self._set(mid, "error", f"できた映像を読めません: {ex}")
                continue
            # 仮の長さで置いたクリップは、できた映像の長さに合わせる
            seq_fps = self.mw.project.sequence.fps
            n = max(1, int(round(m.duration_s * seq_fps)))
            for s in self.mw.project.sequences:
                for t in s.tracks():
                    for c in t.clips:
                        if c.media_id == mid and c.props.pop("link_placeholder", None):
                            nxt = min((o.start for o in t.clips if o.start > c.start), default=10 ** 9)
                            c.duration = max(1, min(n, nxt - c.start))
            self._set(mid, "ok", f"{sec:.0f} 秒でレンダリング")
            hist = getattr(self.mw, "history", None)
            if hist is not None:
                from .history import guess_app, size_delta
                hist.log("dynlink", mid, m.name, m.link_src, guess_app(m.path, m.link_src),
                         f"再レンダリング（{sec:.0f} 秒）" + (f" {size_delta(old_size, m.file_size)}" if old_size else ""))
            self.changed.emit(mid, f"Dynamic Link: {m.name} を{'取り込みました' if first else '更新しました'}")

    @staticmethod
    def _cleanup(m: M.MediaItem):
        """前のレンダリング結果を消す（再生中で消せない物は次の機会に）。"""
        cur = Path(m.path)
        stem = cur.stem.split("__r")[0]
        for p in cur.parent.glob(f"{stem}*{cur.suffix}"):
            if p != cur and (p.stem == stem or p.stem.startswith(stem + "__r")):
                try:
                    p.unlink()
                except OSError:
                    pass

    def open_source(self, m: M.MediaItem) -> bool:
        from .integrations import find_app, _launch
        kind = KINDS.get(Path(m.link_src).suffix.lower())
        exe = find_app(kind) if kind else ""
        if exe:
            _launch([exe, m.link_src])
            return True
        if os.name == "nt":
            os.startfile(m.link_src)        # noqa
            return True
        return False
