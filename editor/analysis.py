"""素材の解析: 無音区間の検出（無音カット）、シーンチェンジ検出、ピーク（ノーマライズ）。"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

import numpy as np

from .media import AudioSource, DecoderPool, SAMPLE_RATE, CHANNELS
from .model import Clip, MediaItem


def clip_peak_db(media: MediaItem, clip: Clip, fps: float) -> Optional[float]:
    """クリップが使っている範囲のピーク値（dBFS）。音声キャッシュが無ければ None。"""
    src = AudioSource(media.path)
    if src.data is None:
        return None
    spf = SAMPLE_RATE / fps
    sp = abs(float(clip.props.get("speed", 1.0) or 1.0))
    a = int(clip.src_in * spf)
    n = int(clip.duration * spf * sp)
    buf = src.read(a, max(1, n))
    pk = float(np.abs(buf).max()) if buf.size else 0.0
    return 20 * np.log10(max(pk, 1e-6))


def silent_ranges(media: MediaItem, clip: Clip, fps: float, threshold_db: float = -40.0,
                  min_silence: float = 0.5, padding: float = 0.1) -> List[Tuple[int, int]]:
    """クリップ内の無音区間を、タイムラインのフレーム [a, b) のリストで返す（前後に padding 秒残す）。"""
    src = AudioSource(media.path)
    if src.data is None:
        return []
    spf = SAMPLE_RATE / fps
    s0 = int(clip.src_in * spf)
    n = int(clip.duration * spf)
    buf = src.read(s0, n)
    if buf.size == 0:
        return []
    win = max(1, int(spf))                    # 1 フレームごとの RMS
    frames = n // win
    x = buf[:frames * win].reshape(frames, win, CHANNELS)
    rms = np.sqrt((x.astype(np.float64) ** 2).mean(axis=(1, 2)))
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    quiet = db < threshold_db
    out = []
    min_f = int(round(min_silence * fps))
    pad = int(round(padding * fps))
    i = 0
    while i < frames:
        if quiet[i]:
            j = i
            while j < frames and quiet[j]:
                j += 1
            if j - i >= min_f:
                a = i + (pad if i > 0 else 0)
                b = j - (pad if j < frames else 0)
                if b > a:
                    out.append((clip.start + a, clip.start + b))
            i = j
        else:
            i += 1
    return out


def scene_cuts(media: MediaItem, clip: Clip, fps: float, sensitivity: float = 0.5,
               progress: Optional[Callable[[int, int], None]] = None,
               cancel=None) -> List[int]:
    """映像のシーンチェンジ位置（タイムラインのフレーム）を返す。sensitivity 0〜1（大きいほど敏感）。"""
    pool = DecoderPool()
    dec = pool.video(media.path)
    if dec is None:
        return []
    prev = None
    diffs = []
    try:
        for i in range(clip.duration):
            if cancel is not None and cancel.is_set():
                break
            t = (clip.src_in + i * float(clip.props.get("speed", 1.0) or 1.0)) / fps
            arr = dec.get_array(t, (64, 36))
            if arr is None:
                break
            g = arr.astype(np.float32)          # 色の変化も拾うよう RGB で比較
            if prev is not None:
                diffs.append((i, float(np.abs(g - prev).mean())))
            prev = g
            if progress is not None and i % 30 == 0:
                progress(i, clip.duration)
    finally:
        pool.close()
    if not diffs:
        return []
    vals = np.array([d for _, d in diffs])
    med = float(np.median(vals)) + 1e-3
    # 中央値の何倍か + 絶対値の下限で判定
    factor = 3.0 + (1.0 - sensitivity) * 9.0
    floor = 6.0 + (1.0 - sensitivity) * 20.0
    cuts = []
    min_gap = int(fps * 0.4)
    for i, d in diffs:
        if d > med * factor and d > floor:
            if not cuts or i - (cuts[-1] - clip.start) >= min_gap:
                cuts.append(clip.start + i)
    return cuts


def beats(media: MediaItem, clip: Clip, fps: float, every: int = 1) -> Tuple[float, List[int]]:
    """BGM のビート位置（CapCut / Premiere のビート検出）。戻り値: (BPM, タイムラインのフレームのリスト)。
    every: 何拍ごとに返すか（4 = 小節の頭だけ）。"""
    src = AudioSource(media.path)
    if src.data is None:
        return 0.0, []
    s0 = int(clip.src_in * SAMPLE_RATE / fps)
    n = int(clip.duration * SAMPLE_RATE / fps)
    buf = src.read(s0, n)
    if buf.size == 0:
        return 0.0, []
    mono = buf.astype(np.float32).mean(axis=1) if buf.ndim == 2 else buf.astype(np.float32)
    hop = 512
    rate = SAMPLE_RATE / hop                       # 約 94 回 / 秒
    m = len(mono) // hop
    if m < int(rate * 2):
        return 0.0, []
    # 低域を強めた音量の変化（立ち上がり）をオンセットの強さに
    frames = mono[:m * hop].reshape(m, hop)
    low = frames - np.concatenate([frames[:, :1], frames[:, :-1]], axis=1) * 0.0
    energy = np.log1p(np.sqrt((low ** 2).mean(axis=1)) * 1000)
    onset = np.maximum(0, np.diff(energy, prepend=energy[0]))
    onset -= np.convolve(onset, np.ones(16) / 16, mode="same")
    onset = np.maximum(onset, 0)
    if not onset.any():
        return 0.0, []
    # テンポ: 60〜180 BPM の範囲で自己相関が一番強い間隔
    ac = np.correlate(onset, onset, mode="full")[len(onset) - 1:]
    lo, hi = int(rate * 60 / 180), int(rate * 60 / 60)
    lag = lo + int(np.argmax(ac[lo:hi]))
    # 細かく（端数）合わせる
    if 0 < lag < len(ac) - 1:
        a_, b_, c_ = ac[lag - 1], ac[lag], ac[lag + 1]
        den = a_ - 2 * b_ + c_
        lag_f = lag + (0.5 * (a_ - c_) / den if den else 0.0)
    else:
        lag_f = float(lag)
    bpm = 60 * rate / lag_f
    # 位相: その間隔の格子でオンセットの合計が一番大きいずれ
    best, phase = -1.0, 0
    for ph in range(lag):
        idx = (ph + np.arange(0, m - ph, lag_f)).astype(int)
        sc = onset[idx[idx < m]].sum()
        if sc > best:
            best, phase = sc, ph
    out = []
    k = 0
    t = float(phase)
    while t < m:
        if k % max(1, every) == 0:
            out.append(clip.start + int(round(t / rate * fps)))
        t += lag_f
        k += 1
    return float(bpm), out


def clip_loudness(media: MediaItem, clip: Clip, fps: float) -> Optional[float]:
    """クリップが使っている範囲のラウドネス（LUFS の近似: 400ms ブロック・ゲート付き。音量・フェードは含めない）。"""
    src = AudioSource(media.path)
    if src.data is None:
        return None
    spf = SAMPLE_RATE / fps
    sp = abs(float(clip.props.get("speed", 1.0) or 1.0)) or 1.0
    buf = src.read(int(clip.src_in * spf), max(1, int(clip.duration * spf * sp))).astype(np.float64)
    blk, hop = int(SAMPLE_RATE * 0.4), int(SAMPLE_RATE * 0.1)
    if buf.shape[0] < blk:
        blk = hop = max(1, buf.shape[0])
    cs = np.concatenate([[0.0], np.cumsum((buf ** 2).sum(axis=1))])
    ms = np.array([(cs[i + blk] - cs[i]) / blk / CHANNELS for i in range(0, buf.shape[0] - blk + 1, hop)])
    if ms.size == 0:
        return None
    lk = -0.691 + 10 * np.log10(np.maximum(ms * 2, 1e-12))
    ms = ms[lk > -70]
    if ms.size == 0:
        return None
    rel = -0.691 + 10 * np.log10(ms.mean() * 2) - 10
    lk = -0.691 + 10 * np.log10(np.maximum(ms * 2, 1e-12))
    ms = ms[lk > rel]
    return float(-0.691 + 10 * np.log10(ms.mean() * 2)) if ms.size else None
