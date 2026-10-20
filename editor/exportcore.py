"""書き出しの中身（Qt を使わない）。アプリの書き出し・レンダーノードのジョブ・分割レンダリングの結合で共通。"""
from __future__ import annotations

import os
import threading
from typing import Callable, List, Optional

import numpy as np

from . import mediacodecs as MC
from .media import DecoderPool, SAMPLE_RATE


def render_to_file(project, a: int, b: int, path: str, preset_id: str, quality: int = 18, alpha: bool = False,
                   video: bool = True, audio: bool = True, progress: Optional[Callable[[int, int], None]] = None,
                   cancel: Optional[threading.Event] = None) -> str:
    """project.sequence の [a, b) を preset で path に書き出す。video / audio で片方だけも可（分割レンダリング用）。"""
    import av
    from . import render
    seq = project.sequence
    pool = DecoderPool()
    n = b - a
    W, H = seq.width & ~1, seq.height & ~1
    preset = MC.BY_ID[preset_id]
    use_alpha = alpha and preset.alpha
    err = MC.check(preset, W, H, seq.fps) if video and preset.vcodec else ""
    if err:
        raise ValueError(err)
    if preset.id in ("h264", "h264_mov", "h265"):
        preset = MC.Preset(**{**preset.__dict__, "options": {**preset.options, "crf": str(quality)}})
    out = MC.open_output(path, preset)
    ok = False
    try:
        vs = MC.add_video_stream(out, preset, W, H, seq.fps) if video and preset.vcodec else None
        ast = MC.add_audio_stream(out, preset, SAMPLE_RATE, 2) if audio else None
        res = av.AudioResampler(format=MC.audio_format(preset), layout="stereo", rate=SAMPLE_RATE) if audio else None
        s_total = int(round(b * SAMPLE_RATE / seq.fps))
        s_cur = int(round(a * SAMPLE_RATE / seq.fps))
        for i, f in enumerate(range(a, b)):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("中止しました")
            if vs is not None:
                if use_alpha:
                    arr = render.render_frame_rgba(seq, f, project, pool, 1.0)
                    if arr.shape[:2] != (H, W):
                        from PIL import Image
                        arr = np.asarray(Image.fromarray(arr, "RGBA").resize((W, H)))
                    vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(arr), format="rgba")
                else:
                    im = render.render_frame(seq, f, project, pool, 1.0)
                    if im.size != (W, H):
                        im = im.resize((W, H))
                    vf = av.VideoFrame.from_ndarray(np.asarray(im), format="rgb24")
                vf = vf.reformat(format=preset.pix_fmt)
                vf.pts = i
                for pkt in vs.encode(vf):
                    out.mux(pkt)
            if ast is not None:
                s_next = min(s_total, int(round((f + 1) * SAMPLE_RATE / seq.fps)))
                if s_next > s_cur:
                    _mux_audio(out, ast, res, render.mix_audio(seq, project, pool, s_cur, s_next - s_cur))
                    s_cur = s_next
            if progress is not None and i % 5 == 0:
                progress(i, n)
        for st in (vs, ast):
            if st is not None:
                for pkt in st.encode(None):
                    out.mux(pkt)
        ok = True
    finally:
        out.close()
        pool.close()
        if not ok:
            try:
                os.remove(path)
            except OSError:
                pass
    if progress is not None:
        progress(n, n)
    return path


def _mux_audio(out, ast, res, buf):
    import av
    af = av.AudioFrame.from_ndarray(np.ascontiguousarray(np.clip(buf, -1, 1).T.astype(np.float32)),
                                    format="fltp", layout="stereo")
    af.sample_rate = SAMPLE_RATE
    for rf in res.resample(af):
        rf.pts = None
        for pkt in ast.encode(rf):
            out.mux(pkt)


def merge_chunks(chunks: List[str], path: str, preset_id: str, project=None, a: int = 0, b: int = 0,
                 progress: Optional[Callable[[int, int], None]] = None,
                 cancel: Optional[threading.Event] = None) -> str:
    """分割して書いた映像（音なし）をつなぎ、音声はシーケンス全体を 1 本で付ける（区切りで音が途切れない）。
    映像は再エンコードしない（パケットをそのまま並べる）。"""
    import av
    from . import render
    preset = MC.BY_ID[preset_id]
    out = MC.open_output(path, preset)
    ok = False
    pool = DecoderPool()
    try:
        first = av.open(chunks[0])
        vin0 = first.streams.video[0]
        vs = out.add_stream(template=vin0)
        first.close()
        ast = res = None
        if project is not None and b > a:
            ast = MC.add_audio_stream(out, preset, SAMPLE_RATE, 2)
            res = av.AudioResampler(format=MC.audio_format(preset), layout="stereo", rate=SAMPLE_RATE)
        offset = 0
        total = len(chunks)
        for k, cp in enumerate(chunks):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("中止しました")
            with av.open(cp) as ic:
                vin = ic.streams.video[0]
                last = 0
                for pkt in ic.demux(vin):
                    if pkt.dts is None and pkt.pts is None:
                        continue
                    dur = pkt.duration or 0
                    pts = (pkt.pts or 0) + offset
                    dts = (pkt.dts if pkt.dts is not None else pkt.pts) + offset
                    last = max(last, (pkt.pts or 0) + dur, (pkt.dts or 0) + dur)
                    pkt.pts, pkt.dts = pts, dts
                    pkt.stream = vs
                    out.mux(pkt)
                offset += last
            if progress is not None:
                progress(k + 1, total + (1 if ast is not None else 0))
        if ast is not None:
            seq = project.sequence
            s0, s1 = int(round(a * SAMPLE_RATE / seq.fps)), int(round(b * SAMPLE_RATE / seq.fps))
            step = SAMPLE_RATE * 5
            for s in range(s0, s1, step):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("中止しました")
                _mux_audio(out, ast, res, render.mix_audio(seq, project, pool, s, min(step, s1 - s)))
            for pkt in ast.encode(None):
                out.mux(pkt)
        ok = True
    finally:
        out.close()
        pool.close()
        if not ok:
            try:
                os.remove(path)
            except OSError:
                pass
    return path
