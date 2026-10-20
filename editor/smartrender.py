"""スマートレンダリング（部分修正・差分書き出し）と分散レンダリングの土台。

書き出しを SEG フレームごとの「区間」に分け、区間ごとに独立した映像ファイル（先頭がキーフレーム）を作る。
区間の中身（各フレームの描画内容のハッシュ = render.frame_signature）が前回と同じなら、その区間は作り直さずに再利用する。
最後に区間を再エンコードなしでつなぎ（リマックス）、音声を付けて 1 本にする。

- 修正した部分だけを作り直すので、2 回目以降の書き出しが速い（書き出し済み動画の「部分修正」）
- 区間は独立しているので、複数プロセス・複数 PC（レンダーノード）に分けて並列にレンダリングできる
- Qt に依存しない（レンダーノード / 別プロセスからも使える）
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import wave
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .media import DecoderPool, SAMPLE_RATE, CHANNELS
from .model import Project, Sequence
from .settings import SETTINGS
from . import render as R

SEG = 60            # 区間の長さ（フレーム）
CODECS = {"libx264": ".mp4", "prores_ks": ".mov"}


def seg_dir() -> Path:
    p = Path(SETTINGS.render_dir) / "segments"
    p.mkdir(parents=True, exist_ok=True)
    return p


def fps_fraction(fps: float) -> Fraction:
    for nominal, frac in ((29.97, Fraction(30000, 1001)), (59.94, Fraction(60000, 1001)),
                          (23.976, Fraction(24000, 1001))):
        if abs(fps - nominal) < 0.01:
            return frac
    return Fraction(fps).limit_denominator(1001)


def plan_segments(a: int, b: int) -> List[Tuple[int, int]]:
    """絶対フレームの格子（SEG 刻み）で区切る。編集の影響が隣の区間に広がらない。"""
    out = []
    k = (a // SEG) * SEG
    while k < b:
        out.append((max(a, k), min(b, k + SEG)))
        k += SEG
    return out


def segment_key(project: Project, seq: Sequence, a: int, b: int, codec: str, crf: int) -> str:
    h = hashlib.md5()
    h.update(json.dumps([seq.width, seq.height, round(seq.fps, 3), codec, crf, a, b, 2]).encode())
    for f in range(a, b):
        h.update(R.frame_signature(seq, f, project, 1.0, False).encode())
    return h.hexdigest()


def segment_path(key: str, codec: str) -> Path:
    return seg_dir() / f"{key}{CODECS.get(codec, '.mp4')}"


def render_segment(project: Project, seq: Sequence, a: int, b: int, out_path: str, codec: str = "libx264",
                   crf: int = 18, pool: Optional[DecoderPool] = None, cancel=None) -> bool:
    """[a, b) を 1 つの映像ファイルにする（先頭キーフレーム・B フレームなし → そのままつなげる）。"""
    import av
    own = pool is None
    pool = pool or DecoderPool()
    pool.use_proxy = False
    W, H = seq.width & ~1, seq.height & ~1
    tmp = out_path + ".part" + Path(out_path).suffix
    try:
        out = av.open(tmp, "w")
        rate = fps_fraction(seq.fps)
        vs = out.add_stream(codec, rate=rate)
        vs.width, vs.height = W, H
        if codec == "prores_ks":
            vs.pix_fmt = "yuv422p10le"
            vs.options = {"profile": "3"}
        else:
            vs.pix_fmt = "yuv420p"
            n = b - a
            vs.options = {"crf": str(crf), "preset": "medium",
                          "x264-params": f"keyint={n}:min-keyint={n}:scenecut=0:bframes=0:open-gop=0"}
        for i, f in enumerate(range(a, b)):
            if cancel is not None and cancel.is_set():
                out.close()
                os.remove(tmp)
                return False
            arr = R.render_frame_np(seq, f, project, pool, 1.0)
            if arr.shape[1] != W or arr.shape[0] != H:
                from PIL import Image
                arr = np.asarray(Image.fromarray(arr).resize((W, H)))
            fr = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format="rgb24")
            fr.pts = i
            for pkt in vs.encode(fr):
                out.mux(pkt)
        for pkt in vs.encode(None):
            out.mux(pkt)
        out.close()
        os.replace(tmp, out_path)
        return True
    finally:
        if own:
            pool.close()
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_audio(project: Project, seq: Sequence, a: int, b: int, path: str):
    pool = DecoderPool()
    s0, s1 = int(round(a * SAMPLE_RATE / seq.fps)), int(round(b * SAMPLE_RATE / seq.fps))
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in range(s0, s1, SAMPLE_RATE * 10):
            w.writeframes(R.to_int16(R.mix_audio(seq, project, pool, s, min(SAMPLE_RATE * 10, s1 - s))))
    pool.close()


def concat(segments: List[str], audio_wav: str, out_path: str, fps: float, codec: str):
    """区間ファイルを再エンコードせずにつなぎ、音声を付ける。"""
    import av
    rate = fps_fraction(fps)
    out = av.open(out_path, "w")
    first = av.open(segments[0])
    ivs = first.streams.video[0]
    ovs = out.add_stream(template=ivs)
    first.close()
    mov = out_path.lower().endswith(".mov")
    oas = out.add_stream("pcm_s16le" if mov else "aac", rate=SAMPLE_RATE)
    oas.layout = "stereo"
    if not mov:
        oas.bit_rate = 192000
    offset = Fraction(0)                    # 秒
    for p in segments:
        src = av.open(p)
        st = src.streams.video[0]
        tb = st.time_base
        nframes = 0
        off = int(round(offset / tb))          # この区間の開始位置（入力の時間単位）
        for pkt in src.demux(st):
            if pkt.dts is None:
                continue
            if pkt.pts is not None:
                pkt.pts += off
            pkt.dts += off
            pkt.stream = ovs                    # 出力の時間単位への変換は mux が行う
            out.mux(pkt)
            nframes += 1
        src.close()
        offset += Fraction(nframes) / rate
    # 音声
    with wave.open(audio_wav, "rb") as w:
        apts = 0
        while True:
            data = w.readframes(SAMPLE_RATE)
            if not data:
                break
            pcm = np.frombuffer(data, np.int16).reshape(1, -1)
            af = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
            af.sample_rate = SAMPLE_RATE
            af.pts = apts
            apts += pcm.shape[1] // CHANNELS
            for pkt in oas.encode(af):
                out.mux(pkt)
    for pkt in oas.encode(None):
        out.mux(pkt)
    out.close()


# ─────────────────────────────────────────────
#  並列レンダリング（別プロセス）
# ─────────────────────────────────────────────

def _worker(args):
    """別プロセス: プロジェクト JSON から区間をレンダリング。"""
    proj_json, a, b, out_path, codec, crf = args
    from .model import Project as _P
    p = _P.from_json(proj_json)
    ok = render_segment(p, p.sequence, a, b, out_path, codec, crf)
    return out_path if ok else ""


def smart_export(project: Project, a: int, b: int, out_path: str, codec: str = "libx264", crf: int = 18,
                 progress: Optional[Callable[[str, int, int], None]] = None, cancel=None,
                 processes: int = 1, farm=None) -> Dict[str, int]:
    """差分書き出し。戻り値: {"segments": 全区間, "reused": 再利用, "rendered": 新規}。

    farm: rendercluster.FarmClient（レンダーノードに分散する場合）
    """
    seq = project.sequence
    segs = plan_segments(a, b)
    keys = []
    for i, (sa, sb) in enumerate(segs):
        keys.append(segment_key(project, seq, sa, sb, codec, crf))
        if progress and i % 10 == 0:
            progress("差分を確認中", i, len(segs))
    todo = [(sa, sb, k) for (sa, sb), k in zip(segs, keys) if not segment_path(k, codec).exists()]
    reused = len(segs) - len(todo)
    done = [0]

    def tick(msg):
        done[0] += 1
        if progress:
            progress(msg, done[0], len(todo))

    if todo:
        if farm is not None:
            farm.render(project, todo, codec, crf, lambda k: segment_path(k, codec), tick, cancel)
        elif processes > 1 and len(todo) > 1:
            import multiprocessing as mp
            pj = project.to_json()
            ctx = mp.get_context("spawn")
            with ctx.Pool(processes) as pool:
                jobs = [(pj, sa, sb, str(segment_path(k, codec)), codec, crf) for sa, sb, k in todo]
                for r in pool.imap_unordered(_worker, jobs):
                    tick("レンダリング中（並列）")
                    if cancel is not None and cancel.is_set():
                        pool.terminate()
                        raise InterruptedError
        else:
            dp = DecoderPool()
            try:
                for sa, sb, k in todo:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError
                    render_segment(project, seq, sa, sb, str(segment_path(k, codec)), codec, crf, dp, cancel)
                    tick("レンダリング中")
            finally:
                dp.close()
    missing = [k for k in keys if not segment_path(k, codec).exists()]
    if missing:
        raise RuntimeError(f"{len(missing)} 個の区間がレンダリングできませんでした")
    if progress:
        progress("音声を書き出し中", 0, 1)
    fd, wav = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        write_audio(project, seq, a, b, wav)
        if progress:
            progress("結合中", 0, 1)
        concat([str(segment_path(k, codec)) for k in keys], wav, out_path, seq.fps, codec)
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    return {"segments": len(segs), "reused": reused, "rendered": len(todo)}


def prune_segments(max_gb: Optional[float] = None):
    """区間キャッシュが大きくなりすぎたら古い順に削除。"""
    limit = int((max_gb if max_gb is not None else SETTINGS.render_cache_gb) * 1024 ** 3)
    files = []
    for p in seg_dir().glob("*.*"):
        try:
            st = p.stat()
            files.append((st.st_atime, st.st_size, p))
        except OSError:
            pass
    total = sum(s for _, s, _ in files)
    for _, s, p in sorted(files):
        if total <= limit:
            break
        try:
            p.unlink()
            total -= s
        except OSError:
            pass
