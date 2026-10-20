"""外部アプリ連携と自動同期（WatchTower 風）。

- 自動同期: 素材ファイル（PSD・画像・動画・音声）が他のアプリで保存されたら、自動で読み直してタイムラインに反映
- 書き出し待ち: After Effects / Blender の書き出し先を仮の素材として置き、ファイルができたら自動でつなぐ
- 監視フォルダ: 指定したフォルダに新しく置かれたファイルを自動でビンに取り込む
- Photoshop: 素材 / 現在のフレームを Photoshop で開く（保存すると自動で反映）
- After Effects: クリップを AE のコンポジションにして開く（JSX を生成。レンダーキューの出力先も設定）
- Blender: クリップを Blender のシーケンサーに読み込んだ .blend を作って開く / バックグラウンドでレンダリング
"""
from __future__ import annotations

import glob
import os
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from . import media as MD
from . import model as M
from .settings import SETTINGS

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ─────────────────────────────────────────────
#  アプリの場所
# ─────────────────────────────────────────────

def _latest(pattern: str) -> str:
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else ""


def find_app(app: str) -> str:
    saved = getattr(SETTINGS, f"{app}_exe", "")
    if saved and os.path.exists(saved):
        return saved
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    if app == "photoshop":
        return _latest(os.path.join(pf, "Adobe", "Adobe Photoshop*", "Photoshop.exe"))
    if app == "aftereffects":
        return _latest(os.path.join(pf, "Adobe", "Adobe After Effects*", "Support Files", "AfterFX.exe"))
    if app == "blender":
        return _latest(os.path.join(pf, "Blender Foundation", "Blender*", "blender.exe"))
    return ""


def work_dir(mw, sub: str) -> Path:
    base = Path(mw.project_path).parent if mw.project_path else Path(SETTINGS.cache_dir).parent / "telocut_work"
    d = base / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def _launch(args: List[str]):
    subprocess.Popen(args, creationflags=0)


# ─────────────────────────────────────────────
#  Photoshop
# ─────────────────────────────────────────────

def open_in_photoshop(path: str) -> bool:
    exe = find_app("photoshop")
    if exe:
        _launch([exe, path])
        return True
    if os.name == "nt":
        os.startfile(path, "edit")          # 既定の編集アプリ
        return True
    return False


# ─────────────────────────────────────────────
#  After Effects
# ─────────────────────────────────────────────

def _js(s: str) -> str:
    return s.replace("\\", "/").replace('"', '\\"')


def after_effects_script(media_path: str, name: str, width: int, height: int, fps: float,
                         src_in_sec: float, dur_sec: float, out_path: str, aep_path: str) -> str:
    return f"""// TeloCut から生成: クリップを After Effects のコンポジションにする
app.beginUndoGroup("TeloCut");
var proj = app.project || app.newProject();
var io = new ImportOptions(new File("{_js(media_path)}"));
var item = proj.importFile(io);
var comp = proj.items.addComp("{_js(name)}", {width}, {height}, 1.0, {dur_sec:.4f}, {fps:.4f});
var layer = comp.layers.add(item);
layer.startTime = -{src_in_sec:.4f};
comp.openInViewer();
var rq = proj.renderQueue.items.add(comp);
try {{ rq.outputModule(1).applyTemplate("High Quality with Alpha"); }} catch (e) {{}}
rq.outputModule(1).file = new File("{_js(out_path)}");
proj.save(new File("{_js(aep_path)}"));
app.endUndoGroup();
"""


def send_to_after_effects(mw, clip: M.Clip) -> Optional[M.MediaItem]:
    """クリップを AE に送り、書き出し先を「書き出し待ち」の素材としてクリップの上に置く。"""
    seq = mw.project.sequence
    m = mw.project.media_by_id(clip.media_id) if clip.media_id else None
    if m is None:
        raise ValueError("素材のあるクリップを選んでください")
    exe = find_app("aftereffects")
    d = work_dir(mw, "AfterEffects")
    stem = f"{Path(m.name).stem}_{clip.id[:6]}"
    out = d / f"{stem}_ae.mov"
    jsx = d / f"{stem}.jsx"
    aep = d / f"{stem}.aep"
    jsx.write_text(after_effects_script(m.path, stem, seq.width, seq.height, seq.fps, clip.src_in / seq.fps,
                                        clip.duration / seq.fps, str(out), str(aep)), encoding="utf-8")
    item = add_pending_media(mw, str(out), f"{stem}_ae.mov", clip, d)
    if exe:
        _launch([exe, "-r", str(jsx)])
    return item


# ─────────────────────────────────────────────
#  Blender
# ─────────────────────────────────────────────

def blender_script(media_path: str, kind: str, width: int, height: int, fps: float, src_in: int, dur: int,
                   out_prefix: str, blend_path: str) -> str:
    fps_i = int(round(fps))
    base = 1001 if abs(fps - fps_i) > 0.001 else 1
    fps_num = fps_i * 1000 if base == 1001 else fps_i
    return f"""# TeloCut から生成: クリップを Blender のビデオシーケンサーに読み込む
import bpy
scn = bpy.context.scene
scn.render.resolution_x = {width}
scn.render.resolution_y = {height}
scn.render.resolution_percentage = 100
scn.render.fps = {fps_num}
scn.render.fps_base = {base}
scn.frame_start = 1
scn.frame_end = {dur}
ed = scn.sequence_editor_create()
coll = getattr(ed, "strips", None) or ed.sequences
path = r"{media_path}"
if "{kind}" == "image":
    st = coll.new_image(name="telocut", filepath=path, channel=1, frame_start=1)
    st.frame_final_duration = {dur}
else:
    st = coll.new_movie(name="telocut", filepath=path, channel=1, frame_start=1 - {src_in})
    try:
        snd = coll.new_sound(name="telocut_audio", filepath=path, channel=2, frame_start=1 - {src_in})
    except Exception:
        pass
scn.render.image_settings.file_format = "FFMPEG"
scn.render.ffmpeg.format = "MPEG4"
scn.render.ffmpeg.codec = "H264"
scn.render.ffmpeg.constant_rate_factor = "HIGH"
scn.render.ffmpeg.audio_codec = "AAC"
scn.render.filepath = r"{out_prefix}"
bpy.ops.wm.save_as_mainfile(filepath=r"{blend_path}")
"""


def send_to_blender(mw, clip: M.Clip, render_now: bool = False):
    seq = mw.project.sequence
    m = mw.project.media_by_id(clip.media_id) if clip.media_id else None
    if m is None:
        raise ValueError("素材のあるクリップを選んでください")
    exe = find_app("blender")
    if not exe:
        raise FileNotFoundError("Blender が見つかりません（環境設定で場所を指定してください）")
    d = work_dir(mw, "Blender")
    stem = f"{Path(m.name).stem}_{clip.id[:6]}"
    blend = d / f"{stem}.blend"
    prefix = d / f"{stem}_render_"
    py = d / f"{stem}_setup.py"
    py.write_text(blender_script(m.path, "image" if m.kind == "image" else "video", seq.width, seq.height,
                                 seq.fps, clip.src_in, clip.duration, str(prefix), str(blend)), encoding="utf-8")
    subprocess.run([exe, "-b", "--factory-startup", "--python", str(py)], capture_output=True, timeout=300,
                   creationflags=_NO_WINDOW)
    if not blend.exists():
        raise RuntimeError("Blender のシーンを作れませんでした")
    item = add_pending_media(mw, "", f"{stem}_blender.mp4", clip, d, pattern=str(prefix) + "*.mp4")
    if render_now:
        threading.Thread(target=lambda: subprocess.run([exe, "-b", str(blend), "-a"], capture_output=True,
                                                       creationflags=_NO_WINDOW), daemon=True).start()
    else:
        _launch([exe, str(blend)])
    return item


# ─────────────────────────────────────────────
#  書き出し待ちの素材
# ─────────────────────────────────────────────

def add_pending_media(mw, path: str, name: str, clip: M.Clip, folder: Path, pattern: str = "") -> M.MediaItem:
    """外部アプリの書き出し先を、オフライン素材としてクリップの上のトラックに置く。"""
    seq = mw.project.sequence
    item = M.MediaItem(id=M.new_id(), path=path or str(folder / name), name=name, kind="video",
                       duration_s=clip.duration / seq.fps, fps=seq.fps, width=seq.width, height=seq.height,
                       has_video=True, has_audio=False, pending=pattern or path)
    mw.begin_edit()
    mw.project.media.append(item)
    (mw.project.bin_by_id(mw.bin.current_bin_id()) or mw.project.bins[0]).add("media", item.id)
    track, _ = seq.find(clip.id)
    vi = seq.video_tracks.index(track) + 1 if track in seq.video_tracks else len(seq.video_tracks)
    while vi >= len(seq.video_tracks):
        seq.video_tracks.append(M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False))
    nc = M.Clip(M.new_id(), "video", clip.start, clip.duration, 0, item.id, name)
    M.place_clips(seq, [(seq.video_tracks[vi], nc)])
    mw.end_edit("外部アプリの書き出し先を配置")
    return item


# ─────────────────────────────────────────────
#  自動同期（監視）
# ─────────────────────────────────────────────

class MediaWatcher(QObject):
    """素材の更新・書き出し待ちファイルの出現・監視フォルダの新規ファイルを検出する。"""

    changed = pyqtSignal(str, str)       # media id, メッセージ

    def __init__(self, mw, interval_ms: int = 1500):
        super().__init__(mw)
        self.mw = mw
        self._sig: Dict[str, tuple] = {}
        self._pending_size: Dict[str, int] = {}
        self._seen_watch: Dict[str, set] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    def _stat(self, p):
        try:
            st = os.stat(p)
            return st.st_size, st.st_mtime
        except OSError:
            return None

    def poll(self):
        proj = self.mw.project
        for m in list(proj.media):
            if m.pending:
                self._check_pending(m)
                continue
            sig = self._stat(m.path)
            old = self._sig.get(m.id)
            if isinstance(old, tuple) and old and old[0] == "changing":
                if sig == old[1]:
                    self._sig[m.id] = sig
                    self._reload(m)
                else:
                    self._sig[m.id] = ("changing", sig)
            elif old is not None and sig is not None and sig != old:
                # 書き込み途中の可能性 → 次回も同じなら反映
                self._sig[m.id] = ("changing", sig)
            else:
                if old is None and sig is not None:
                    self._first_seen(m)
                self._sig[m.id] = sig
        self._check_watch_folders()

    def _first_seen(self, m: M.MediaItem):
        """見張りを始めた素材の今の版を残しておく（あとで「前の版に戻す」ため）。"""
        h = getattr(self.mw, "history", None)
        if h is not None and not m.link_src:
            threading.Thread(target=h.snapshot, args=(m.id, m.path), daemon=True).start()

    def _reload(self, m: M.MediaItem):
        old_size = m.file_size
        MD.invalidate(m.path)
        st = self._stat(m.path)
        if st:
            m.file_size, m.mtime = st
        try:
            info = MD.probe(m.path)
            m.duration_s, m.width, m.height = info.duration_s or m.duration_s, info.width, info.height
        except Exception:
            pass
        h = getattr(self.mw, "history", None)
        if h is not None and not m.link_src:
            from .history import guess_app, size_delta
            h.log("update", m.id, m.name, m.path, guess_app(m.path), size_delta(old_size, m.file_size))
            threading.Thread(target=h.snapshot, args=(m.id, m.path), daemon=True).start()
        self.changed.emit(m.id, f"更新を反映: {m.name}")

    def _check_pending(self, m: M.MediaItem):
        pat = m.pending
        hits = sorted(glob.glob(pat)) if any(ch in pat for ch in "*?[") else ([pat] if os.path.exists(pat) else [])
        if not hits:
            return
        p = hits[-1]
        st = self._stat(p)
        if st is None or st[0] == 0:
            return
        if self._pending_size.get(m.id) != st[0]:      # 書き込みが終わるまで待つ
            self._pending_size[m.id] = st[0]
            return
        try:
            info = MD.probe(p)
        except Exception:
            return
        m.path, m.pending = p, ""
        m.kind, m.duration_s, m.fps = info.kind, info.duration_s, info.fps
        m.width, m.height, m.has_audio, m.has_video = info.width, info.height, info.has_audio, info.has_video
        m.file_size, m.mtime = info.file_size, info.mtime
        self._sig[m.id] = st
        if m.has_audio:
            threading.Thread(target=lambda: (MD.build_audio_cache(p), MD.build_peaks(p)), daemon=True).start()
        self.changed.emit(m.id, f"書き出しを取り込みました: {Path(p).name}")

    def _check_watch_folders(self):
        folders = getattr(self.mw.project, "watch_folders", None) or []
        exts = MD.VIDEO_EXT | MD.AUDIO_EXT | MD.IMAGE_EXT
        for d in folders:
            if not os.path.isdir(d):
                continue
            try:
                files = {os.path.join(d, f): self._stat(os.path.join(d, f)) for f in os.listdir(d)
                         if Path(f).suffix.lower() in exts}
            except OSError:
                continue
            handled = self._seen_watch.get(d)
            if handled is None:
                self._seen_watch[d] = set(files)       # 監視開始時にあったファイルは取り込まない
                continue
            known = {os.path.normcase(m.path) for m in self.mw.project.media}
            new = []
            for f, st in files.items():
                if f in handled or os.path.normcase(f) in known or not st or st[0] <= 0:
                    continue
                if self._pending_size.get(f) == st[0]:   # 前回とサイズが同じ = 書き込み完了
                    new.append(f)
                    handled.add(f)
                else:
                    self._pending_size[f] = st[0]
            if new:
                self.mw.import_files(sorted(new), bin_id=self.mw.watch_bin_id())
                self.changed.emit("", f"監視フォルダから {len(new)} 個取り込みました")
