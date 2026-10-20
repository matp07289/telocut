"""モーショントラッキング（AE のトラッカー風）。画面上の範囲を、フレームを追って探す（OpenCV のテンプレートマッチング）。

合成した画面（今見えている映像）で追うので、素材の拡大・位置を変えていても、モニターで囲んだ所がそのまま追われる。
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

import numpy as np


def track_region(frames: Callable[[int], Optional[np.ndarray]], start: int, end: int,
                 rect: Tuple[float, float, float, float], progress=None, cancelled=None,
                 min_score: float = 0.35) -> List[Tuple[int, float, float, float]]:
    """frames(f) が返す RGB 画像（同じ大きさ）で、rect（0〜1 の x, y, w, h）の中身を start〜end まで追う。
    戻り値: [(フレーム, 中心 x, 中心 y（画像のピクセル）, 一致度)]。見失ったらそこで止める。"""
    import cv2
    img = frames(start)
    if img is None:
        return []
    H, W = img.shape[:2]
    x, y, w, h = rect
    tw, th = max(8, int(w * W)), max(8, int(h * H))
    tx, ty = int(x * W), int(y * H)
    tx, ty = max(0, min(W - tw, tx)), max(0, min(H - th, ty))
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    tmpl = gray[ty:ty + th, tx:tx + tw].astype(np.float32)
    cx, cy = tx + tw / 2, ty + th / 2
    out = [(start, cx, cy, 1.0)]
    total = max(1, end - start)
    for f in range(start + 1, end):
        if cancelled and cancelled():
            break
        img = frames(f)
        if img is None:
            break
        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
        # 前の位置のまわりだけ探す（速い・似た物に飛ばない）
        mx, my = int(tw * 1.5) + 4, int(th * 1.5) + 4
        x0, y0 = int(max(0, cx - tw / 2 - mx)), int(max(0, cy - th / 2 - my))
        x1, y1 = int(min(W, cx + tw / 2 + mx)), int(min(H, cy + th / 2 + my))
        win = g[y0:y1, x0:x1]
        if win.shape[0] < th or win.shape[1] < tw:
            break
        res = cv2.matchTemplate(win, tmpl, cv2.TM_CCOEFF_NORMED)
        _mn, score, _mnl, loc = cv2.minMaxLoc(res)
        if score < min_score:
            break
        nx, ny = x0 + loc[0], y0 + loc[1]
        # 少しずつ見た目の変化に合わせる（向き・明るさがゆっくり変わっても追える）
        if score > 0.75:
            tmpl = tmpl * 0.85 + g[ny:ny + th, nx:nx + tw] * 0.15
        cx, cy = nx + tw / 2, ny + th / 2
        out.append((f, cx, cy, float(score)))
        if progress and (f - start) % 5 == 0:
            progress(f - start, total)
    return out


def smooth(points, radius: int = 2):
    """ガタつきを軽くならす（移動平均）。"""
    if radius <= 0 or len(points) < 3:
        return points
    xs = np.array([p[1] for p in points])
    ys = np.array([p[2] for p in points])
    k = np.ones(radius * 2 + 1) / (radius * 2 + 1)
    pad = lambda a: np.pad(a, radius, mode="edge")
    xs2 = np.convolve(pad(xs), k, mode="valid")
    ys2 = np.convolve(pad(ys), k, mode="valid")
    return [(p[0], float(x), float(y), p[3]) for p, x, y in zip(points, xs2, ys2)]
