"""MME（MikuMikuEffect）風のエフェクト: MMD のクリップ（背景透明の映像・立ち絵）だけに掛けてアニメ調にする。

- セル調（アニメ塗り）: 明るさを数段に分けてくっきりした影に・色を少し鮮やかに
- 輪郭線（アニメ）: 外側の輪郭（透明との境目）と、内側の線（明るさの差）を描く
- ディフュージョン: 明るい所がふわっとにじむ（MME の Diffusion 風）
- リムライト: 輪郭の内側を光らせる（逆光の縁）
- 漫画（白黒トーン）: 2〜3 階調のモノクロ＋線
透明の部分（背景）には何もしない。プリセット（MME_PRESETS）でまとめて付けられる。
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter

from .fxlib import P, effect

CAT = "MME 風（アニメ調）"


def _split(im: Image.Image):
    a = np.asarray(im.convert("RGBA")).astype(np.float32)
    return a[..., :3], a[..., 3:4]


def _join(rgb: np.ndarray, alpha: np.ndarray) -> Image.Image:
    return Image.fromarray(np.concatenate([rgb.clip(0, 255), alpha], axis=-1).astype(np.uint8), "RGBA")


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


@effect("mme_toon", "セル調（アニメ塗り）", CAT, [
    P("levels", "影の段数", 3, 2, 6), P("shadow", "影の濃さ", 0.55, 0.0, 1.0), P("soft", "段の境目のなめらかさ", 0.08, 0.0, 0.3),
    P("sat", "鮮やかさ", 1.25, 0.5, 2.0)])
def mme_toon(im, p, ctx):
    rgb, a = _split(im)
    L = _luma(rgb) / 255.0
    n = max(2, int(p["levels"]))
    x = L * (n - 1)
    lo = np.floor(x)
    fr = x - lo
    s = max(1e-3, float(p["soft"]))
    step = np.clip((fr - 0.5) / s + 0.5, 0, 1)            # 段の境目をなめらかに
    q = (lo + step) / (n - 1)
    sh = float(p["shadow"])
    q = 1.0 - (1.0 - q) * sh - (1.0 - L) * (1.0 - sh)      # 影の濃さ（0 = 元の明るさ / 1 = 段だけ）
    ratio = (q / np.maximum(L, 1e-3))[..., None]
    out = rgb * np.clip(ratio, 0, 3)
    g = _luma(out)[..., None]
    out = g + (out - g) * float(p["sat"])
    return _join(out, a)


@effect("mme_line", "輪郭線（アニメ）", CAT, [
    P("width", "太さ", 2.0, 0.5, 8.0, suffix="px"), P("color", "線の色", "#2A1E28"),
    P("inner", "内側の線", 0.6, 0.0, 1.0), P("thresh", "内側の線の感度", 0.18, 0.02, 0.6)], expands=True)
def mme_line(im, p, ctx):
    im = im.convert("RGBA")
    wpx = max(1, int(round(float(p["width"]) * ctx.scale)))
    from PIL import ImageColor
    col = np.array(ImageColor.getrgb(p["color"])[:3], np.float32)
    pad = wpx + 2
    base = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    base.paste(im, (pad, pad))
    rgb, a = _split(base)
    A = a[..., 0] / 255.0
    # 外側: 透明との境目を太らせた所
    grown = np.asarray(Image.fromarray((A * 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(wpx * 2 + 1))).astype(np.float32) / 255.0
    outer = np.clip(grown - A, 0, 1)
    # 内側: 明るさの差（ソーベル）
    L = _luma(rgb) / 255.0
    gx = np.zeros_like(L)
    gy = np.zeros_like(L)
    gx[:, 1:-1] = L[:, 2:] - L[:, :-2]
    gy[1:-1, :] = L[2:, :] - L[:-2, :]
    mag = np.sqrt(gx * gx + gy * gy)
    inner = np.clip((mag - float(p["thresh"])) * 6, 0, 1) * A * float(p["inner"])
    if wpx > 1 and inner.any():
        inner = np.asarray(Image.fromarray((inner * 255).astype(np.uint8)).filter(
            ImageFilter.MaxFilter(max(3, wpx // 2 * 2 + 1)))).astype(np.float32) / 255.0 * float(p["inner"])
    line = np.clip(np.maximum(outer, inner), 0, 1)[..., None]
    out = rgb * (1 - line) + col * line
    alpha = np.maximum(A, outer)[..., None] * 255.0
    return _join(out, alpha)


@effect("mme_diffusion", "ディフュージョン（柔らかい光）", CAT, [
    P("radius", "にじみの広さ", 10.0, 1.0, 40.0, suffix="px"), P("amount", "強さ", 0.45, 0.0, 1.0),
    P("bright", "明るい所だけ", 0.5, 0.0, 1.0)])
def mme_diffusion(im, p, ctx):
    im = im.convert("RGBA")
    rgb, a = _split(im)
    r = max(1.0, float(p["radius"]) * ctx.scale)
    L = _luma(rgb)[..., None] / 255.0
    th = float(p["bright"])
    src = rgb * np.clip((L - th * 0.6) / max(1e-3, 1 - th * 0.6), 0, 1)
    bl = np.asarray(Image.fromarray(src.clip(0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(r))).astype(np.float32)
    k = float(p["amount"])
    out = 255 - (255 - rgb) * (255 - bl * k) / 255.0           # スクリーン
    return _join(out, a)


@effect("mme_rim", "リムライト（輪郭の光）", CAT, [
    P("width", "光の幅", 6.0, 1.0, 30.0, suffix="px"), P("color", "光の色", "#BFE8FF"), P("amount", "強さ", 0.7, 0.0, 1.0)])
def mme_rim(im, p, ctx):
    im = im.convert("RGBA")
    rgb, a = _split(im)
    A = a[..., 0]
    w = max(1, int(round(float(p["width"]) * ctx.scale)))
    shrunk = np.asarray(Image.fromarray(A.astype(np.uint8)).filter(ImageFilter.MinFilter(w * 2 + 1))).astype(np.float32)
    rim = np.clip((A - shrunk) / 255.0, 0, 1)
    rim = np.asarray(Image.fromarray((rim * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(w / 2))).astype(np.float32) / 255.0
    rim = (rim * A / 255.0)[..., None] * float(p["amount"])
    from PIL import ImageColor
    col = np.array(ImageColor.getrgb(p["color"])[:3], np.float32)
    out = 255 - (255 - rgb) * (255 - col * rim) / 255.0
    return _join(out, a)


@effect("mme_manga", "漫画（白黒トーン）", CAT, [
    P("levels", "階調", 3, 2, 4), P("dots", "網点", True)])
def mme_manga(im, p, ctx):
    rgb, a = _split(im)
    L = _luma(rgb) / 255.0
    vis = a[..., 0] > 128
    if vis.any():
        # 明るさを広げてから（暗い絵でも白・灰・黒に分かれるように）
        lo, hi = np.percentile(L[vis], 5), np.percentile(L[vis], 95)
        L = np.clip((L - lo) / max(1e-3, hi - lo), 0, 1) ** 0.8
    n = int(p["levels"])
    q = np.round(L * (n - 1)) / (n - 1)
    if p["dots"]:
        h, w = q.shape
        yy, xx = np.mgrid[0:h, 0:w]
        cell = max(3, int(6 * ctx.scale))
        d = (np.sin(xx * np.pi * 2 / cell) * np.sin(yy * np.pi * 2 / cell) + 1) / 2
        mid = (q > 0.01) & (q < 0.99)
        q = np.where(mid, (d < q).astype(np.float32), q)
    g = (q * 255.0)[..., None].repeat(3, axis=-1)
    return _join(g, a)


# プリセット: 名前 → [(エフェクト, パラメーター)]
MME_PRESETS = {
    "アニメ調（セル＋輪郭）": [("mme_toon", {}), ("mme_line", {})],
    "アニメ調＋柔らかい光": [("mme_toon", {}), ("mme_line", {}), ("mme_diffusion", {"amount": 0.35})],
    "柔らかい光（ディフュージョン）": [("mme_diffusion", {})],
    "逆光（リムライト＋柔らかい光）": [("mme_rim", {}), ("mme_diffusion", {"amount": 0.3})],
    "漫画（白黒トーン＋線）": [("mme_manga", {}), ("mme_line", {"color": "#000000", "inner": 0.8})],
}
