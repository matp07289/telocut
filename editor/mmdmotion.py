"""MMD のモデルをセリフに合わせて動かす（身振り手振り・うなずき・体の揺れ・口パク・まばたき）。

声の大きさ（フレームごと）から、話している区間（フレーズ）と強く言った所を見つけ、
- フレーズごとに身振り（説明する手・両手を広げる・胸に手・指さし など）を、入り / 出をなめらかに
- 強く言った所でうなずく・ずっと体が少し揺れる・呼吸
- 口: 音声合成の母音（あいうえおん）があればそれ、無ければ声の大きさで「あ」を開ける（ときどき い・う・お を混ぜる）
- まばたき: 2.5〜5.5 秒ごと
を作り、Blender（mmdtachie.RENDER_SCRIPT の anim）でキーフレームにして背景透明の動画にする。
回転はアーマチュアの軸（X = 左右・Y = 前後・Z = 上下。キャラの左 = +X）で、ポーズ（腕を下ろす など）の後に足す。
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

import numpy as np

# 身振り: 名前 → {ボーン: [(軸, 角度)]}（ポーズの上に足す）
GESTURES: Dict[str, Dict[str, List[Tuple[str, float]]]] = {
    "右手で説明": {"右腕": [("X", -35), ("Y", 14)], "右ひじ": [("Z", 55)]},
    "左手で説明": {"左腕": [("X", -35), ("Y", -14)], "左ひじ": [("Z", -55)]},
    "両手を広げる": {"左腕": [("X", -25), ("Y", -16)], "右腕": [("X", -25), ("Y", 16)],
                  "左ひじ": [("Z", -25)], "右ひじ": [("Z", 25)]},
    "胸に手": {"右腕": [("X", -36), ("Y", 6)], "右ひじ": [("Z", 110)]},
    "指さし": {"右腕": [("X", -68), ("Y", 10)], "右ひじ": [("Z", 12)]},
    "両手を前で": {"左腕": [("X", -30), ("Y", 6)], "右腕": [("X", -30), ("Y", -6)],
                "左ひじ": [("Z", -70)], "右ひじ": [("Z", 70)]},
}
DEFAULT_SET = ["右手で説明", "両手を広げる", "左手で説明", "両手を前で", "胸に手", "指さし"]


def loudness_db(wav_path: str, fps: float, n_frames: int) -> np.ndarray:
    """フレームごとの声の大きさ（dB）。"""
    import wave
    with wave.open(wav_path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    dt = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
    x = np.frombuffer(raw, dt).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    x /= float(np.iinfo(dt).max)
    spf = sr / fps
    out = np.full(n_frames, -90.0, np.float32)
    for i in range(n_frames):
        seg = x[int(i * spf):int((i + 1) * spf)]
        if seg.size:
            out[i] = 20 * math.log10(max(1e-6, float(np.sqrt((seg * seg).mean()))))
    return out


def phrases(db: np.ndarray, fps: float, thr: float = -38.0, gap: float = 0.35, min_len: float = 0.25) -> List[Tuple[int, int]]:
    on = db > thr
    out, i, n = [], 0, len(db)
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            out.append([i, j])
            i = j
        else:
            i += 1
    merged: List[List[int]] = []
    for a, b in out:
        if merged and a - merged[-1][1] <= gap * fps:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len * fps]


def stresses(db: np.ndarray, fps: float, ranges, min_gap: float = 0.7) -> List[int]:
    """強く言った所（声の大きさの山の上のほう）。"""
    pts = []
    for a, b in ranges:
        seg = db[a:b]
        if seg.size < 3:
            continue
        top = np.percentile(seg, 80)
        for i in range(1, seg.size - 1):
            if seg[i] >= top and seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]:
                f = a + i
                if not pts or f - pts[-1] >= min_gap * fps:
                    pts.append(f)
    return pts


def _env(f: int, a: int, b: int, fin: int, fout: int) -> float:
    """区間 [a, b) の重み（入り fin・出 fout フレームでなめらかに）。"""
    if f < a - fin or f > b + fout:
        return 0.0
    if f < a:
        t = (f - (a - fin)) / max(1, fin)
    elif f > b:
        t = 1 - (f - b) / max(1, fout)
    else:
        t = 1.0
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def plan(db: np.ndarray, fps: float, base_pose: Dict[str, List[list]], roles: dict, seed: int = 0,
         strength: float = 1.0, gestures: Optional[List[str]] = None, vowels: Optional[List[Optional[str]]] = None,
         nod: bool = True, sway: bool = True, blink: bool = True, step: int = 2) -> dict:
    """{"frames", "bone_keys": {ボーン: [[f, [[軸, 角度], …]], …]}, "morph_keys": {モーフ: [[f, 値], …]}}"""
    rnd = random.Random(seed)
    n = len(db)
    ph = phrases(db, fps)
    gset = [g for g in (gestures or DEFAULT_SET) if g in GESTURES]
    # フレーズごとの身振り（短いフレーズは身振りなし・同じものが続かない）
    acts = []
    last = None
    for a, b in ph:
        if (b - a) < 0.8 * fps or not gset or rnd.random() > 0.85 * strength + 0.1:
            continue
        cand = [g for g in gset if g != last] or gset
        g = rnd.choice(cand)
        last = g
        acts.append((a, b, g))
    st = stresses(db, fps, ph) if nod else []
    bones = set(base_pose)
    for g in gset:
        bones |= set(GESTURES[g])
    bones |= {"頭", "上半身"}
    fin, fout = int(0.35 * fps), int(0.45 * fps)
    keys: Dict[str, list] = {b: [] for b in bones}
    frames = list(range(0, n, max(1, step))) + ([n - 1] if (n - 1) % max(1, step) else [])
    for f in frames:
        t = f / fps
        add: Dict[str, List[list]] = {b: [] for b in bones}
        for a, b, g in acts:
            w = _env(f, a, b, fin, fout) * strength
            if w <= 0:
                continue
            for bone, rots in GESTURES[g].items():
                add[bone] += [[ax, deg * w] for ax, deg in rots]
        if nod:
            nd = 0.0
            for s in st:
                d = (f - s) / fps
                if -0.15 <= d <= 0.45:
                    nd = max(nd, math.sin(math.pi * (d + 0.15) / 0.6))
            if nd:
                add["頭"].append(["X", 7.0 * nd * strength])
        if sway:
            add["上半身"].append(["Z", 2.2 * math.sin(2 * math.pi * t / 6.5 + seed % 7)])
            add["上半身"].append(["X", 0.9 * math.sin(2 * math.pi * t / 3.6)])          # 呼吸
            add["頭"].append(["Y", 1.6 * math.sin(2 * math.pi * t / 8.3 + 1.3)])
        for bone in bones:
            keys[bone].append([f, [list(r) for r in base_pose.get(bone, [])] + add[bone]])
    keys = {b: v for b, v in keys.items() if any(r for _f, r in v)}
    # ── 口
    mk: Dict[str, list] = {}

    def put(m, f, v):
        if not m:
            return
        lst = mk.setdefault(m, [])
        if lst and abs(lst[-1][1] - v) < 0.04 and f - lst[-1][0] < fps:
            return
        lst.append([f, round(float(v), 3)])
    vow_names = {k: roles.get(k) for k in ("a", "i", "u", "e", "o", "N")}
    mouths = [m for m in vow_names.values() if m]
    for m in set(mouths):
        put(m, 0, 0.0)
    cur = None
    for f in range(n):
        v = vowels[f] if vowels and f < len(vowels) else None
        target: Dict[str, float] = {}
        if v is not None:
            if v not in ("closed", "", None) and vow_names.get(v):
                target[vow_names[v]] = 0.9
        else:
            lv = (db[f] + 42) / 22.0                  # -42 dB で閉じ・-20 dB で全開
            if lv > 0.12:
                if cur is None or f % int(max(2, fps / 6)) == 0:
                    cur = rnd.choices(["a", "i", "u", "o", "e"], [5, 1.4, 1.2, 1.4, 1])[0]
                target[vow_names.get(cur) or vow_names.get("a") or ""] = min(1.0, lv)
        for m in set(mouths):
            put(m, f, target.get(m, 0.0))
    # ── まばたき
    eye = roles.get("eye")
    if blink and eye:
        put(eye, 0, 0.0)
        f = int(rnd.uniform(1.0, 3.0) * fps)
        while f < n - 4:
            for d, v in ((0, 0.0), (1, 1.0), (3, 1.0), (5, 0.0)):
                if f + d < n:
                    mk.setdefault(eye, []).append([f + d, v])
            f += int(rnd.uniform(2.5, 5.5) * fps)
    return {"frames": n, "fps": fps, "bone_keys": keys, "morph_keys": mk, "phrases": ph, "gestures": acts}


def vowels_for_range(project, seq, track_index: int, a: int, b: int) -> Optional[List[Optional[str]]]:
    """音声合成（VOICEVOX 系）の口の形（<wav>.mouth.json）があれば、範囲のフレームごとの母音。無ければ None。"""
    from .voiceengine import mouth_path, mouth_shape_at
    import os
    if track_index >= len(seq.audio_tracks):
        return None
    clips = seq.audio_tracks[track_index].clips
    out: List[Optional[str]] = [None] * (b - a)
    found = False
    for c in clips:
        m = project.media_by_id(c.media_id)
        if m is None or not os.path.exists(mouth_path(m.path)):
            continue
        found = True
        for f in range(max(a, c.start), min(b, c.end)):
            sec = (c.src_in + (f - c.start)) / seq.fps
            sh = mouth_shape_at(m.path, sec)
            out[f - a] = sh if sh else "closed"
    return out if found else None
