"""組み込みエフェクト（AviUtl のカタログでよく使われるもの中心）。

カラー / スタイライズ / ゆがみ / ブラー / 演出（集中線・雨・雪・キラキラ・フラッシュ）/ マスク・枠
px 単位のパラメーターは ctx.scale（プレビュー縮小率）を掛けて、どの画質でも同じ見た目にする。
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

from .fxlib import P, effect


def _rgb(hexs: str):
    h = (hexs or "#000000").lstrip("#")
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (0, 0, 0)


def _arr(im):
    return np.asarray(im.convert("RGBA")).astype(np.float32)


def _img(a):
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGBA")


def _rand(ctx, salt=0):
    return np.random.default_rng((ctx.seed * 7919 + ctx.rel * 104729 + salt) & 0xFFFFFFFF)


def _px(v, ctx):
    return max(0.0, float(v) * ctx.scale)


# ─────────────────────────── カラー ───────────────────────────

@effect("hue", "色相シフト", "カラー", [P("deg", "角度", 60.0, -180, 180, 1, suffix="°")])
def hue(im, p, ctx):
    a = im.getchannel("A")
    h, s, v = im.convert("RGB").convert("HSV").split()
    sh = int(round(float(p["deg"]) / 360 * 255)) % 256
    h = h.point(lambda x: (x + sh) % 256)
    out = Image.merge("HSV", (h, s, v)).convert("RGBA")
    out.putalpha(a)
    return out


@effect("temperature", "色温度", "カラー", [P("value", "暖かさ（−で寒色）", 30.0, -100, 100, 1)])
def temperature(im, p, ctx):
    a = _arr(im)
    k = float(p["value"]) / 100
    a[..., 0] *= 1 + 0.25 * k
    a[..., 2] *= 1 - 0.25 * k
    return _img(a)


@effect("levels", "レベル補正", "カラー", [P("black", "黒", 0, 0, 254), P("white", "白", 255, 1, 255),
                                         P("gamma", "ガンマ", 1.0, 0.2, 3.0)])
def levels(im, p, ctx):
    a = _arr(im)
    b, w = float(p["black"]), max(float(p["white"]), float(p["black"]) + 1)
    x = np.clip((a[..., :3] - b) / (w - b), 0, 1) ** (1 / max(0.05, float(p["gamma"])))
    a[..., :3] = x * 255
    return _img(a)


@effect("posterize", "ポスタリゼーション", "カラー", [P("levels", "階調", 4, 2, 16)])
def posterize(im, p, ctx):
    a = _arr(im)
    n = max(2, int(p["levels"])) - 1
    a[..., :3] = np.round(a[..., :3] / 255 * n) / n * 255
    return _img(a)


@effect("threshold", "2 値化", "カラー", [P("level", "しきい値", 128, 0, 255), P("dark", "暗い色", "#000000"),
                                       P("light", "明るい色", "#FFFFFF")])
def threshold(im, p, ctx):
    a = _arr(im)
    lum = a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
    m = (lum >= float(p["level"]))[..., None]
    a[..., :3] = np.where(m, np.array(_rgb(p["light"]), np.float32), np.array(_rgb(p["dark"]), np.float32))
    return _img(a)


@effect("colorize", "単色化", "カラー", [P("color", "色", "#3A7BFF"), P("amount", "強さ", 1.0, 0, 1)])
def colorize(im, p, ctx):
    a = _arr(im)
    lum = (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114)[..., None] / 255
    tint = lum * np.array(_rgb(p["color"]), np.float32)
    k = float(p["amount"])
    a[..., :3] = a[..., :3] * (1 - k) + tint * k
    return _img(a)


@effect("vignette", "周辺減光（ビネット）", "カラー", [P("amount", "強さ", 0.6, 0, 1), P("size", "範囲", 0.7, 0.1, 1.5)])
def vignette(im, p, ctx):
    a = _arr(im)
    h, w = a.shape[:2]
    y, x = np.ogrid[:h, :w]
    d = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2) / max(0.1, float(p["size"]))
    k = 1 - np.clip(d - 0.5, 0, 1) * float(p["amount"]) * 1.4
    a[..., :3] *= np.clip(k, 0, 1)[..., None]
    return _img(a)


@effect("grain", "フィルムグレイン", "カラー", [P("amount", "強さ", 0.25, 0, 1)], animated=True)
def grain(im, p, ctx):
    a = _arr(im)
    n = _rand(ctx).normal(0, 40 * float(p["amount"]), a.shape[:2]).astype(np.float32)
    a[..., :3] += n[..., None]
    return _img(a)


# ─────────────────────────── スタイライズ ───────────────────────────

@effect("glow", "グロー（発光）", "スタイライズ", [P("radius", "広がり", 12.0, 1, 80, 1, suffix=" px"),
                                            P("strength", "強さ", 0.8, 0, 3), P("threshold", "明るい所だけ", 0.5, 0, 1)])
def glow(im, p, ctx):
    a = _arr(im)
    lum = (a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114) / 255
    mask = np.clip((lum - float(p["threshold"])) / max(0.01, 1 - float(p["threshold"])), 0, 1) * (a[..., 3] / 255)
    src = a.copy()
    src[..., :3] *= mask[..., None]
    src[..., 3] = mask * 255
    bl = _arr(_img(src).filter(ImageFilter.GaussianBlur(max(0.5, _px(p["radius"], ctx)))))
    k = float(p["strength"])
    a[..., :3] = a[..., :3] + bl[..., :3] * k
    a[..., 3] = np.maximum(a[..., 3], np.clip(bl[..., 3] * k, 0, 255))
    return _img(a)


@effect("outline", "縁取り", "スタイライズ", [P("color", "色", "#FFFFFF"), P("width", "太さ", 6.0, 1, 60, 1, suffix=" px"),
                                          P("soft", "ぼかし", 0.0, 0, 10, 0.5)], expands=True,
        description="立ち絵・切り抜き素材・テロップのふちどり")
def outline(im, p, ctx):
    wpx = max(1, int(round(_px(p["width"], ctx))))
    pad = wpx + 2
    base = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    base.paste(im, (pad, pad))
    al = base.getchannel("A").filter(ImageFilter.MaxFilter(wpx * 2 + 1 if wpx < 12 else 25))
    if wpx >= 12:     # 太い縁は数回に分けて膨張
        for _ in range((wpx - 12) // 12 + 1):
            al = al.filter(ImageFilter.MaxFilter(25))
    if float(p["soft"]) > 0:
        al = al.filter(ImageFilter.GaussianBlur(_px(p["soft"], ctx)))
    edge = Image.new("RGBA", base.size, _rgb(p["color"]) + (255,))
    edge.putalpha(al)
    edge.alpha_composite(base)
    return edge


@effect("shadow", "ドロップシャドウ", "スタイライズ", [P("color", "色", "#000000"), P("dx", "横", 8.0, -100, 100, 1, suffix=" px"),
                                                P("dy", "縦", 8.0, -100, 100, 1, suffix=" px"),
                                                P("blur", "ぼかし", 6.0, 0, 50, 1, suffix=" px"), P("opacity", "濃さ", 0.6, 0, 1)],
        expands=True)
def shadow(im, p, ctx):
    dx, dy, bl = float(p["dx"]) * ctx.scale, float(p["dy"]) * ctx.scale, _px(p["blur"], ctx)
    pad = int(abs(dx) + abs(dy) + bl * 3 + 2)
    out = Image.new("RGBA", (im.width + pad * 2, im.height + pad * 2), (0, 0, 0, 0))
    sh = Image.new("RGBA", im.size, _rgb(p["color"]) + (0,))
    sh.putalpha(im.getchannel("A").point(lambda v: int(v * float(p["opacity"]))))
    out.alpha_composite(sh, (int(pad + dx), int(pad + dy)))
    if bl > 0:
        out = out.filter(ImageFilter.GaussianBlur(bl))
    out.alpha_composite(im, (pad, pad))
    return out


@effect("emboss", "エンボス", "スタイライズ", [])
def emboss(im, p, ctx):
    a = im.getchannel("A")
    out = im.convert("RGB").filter(ImageFilter.EMBOSS).convert("RGBA")
    out.putalpha(a)
    return out


@effect("edges", "エッジ抽出（線画風）", "スタイライズ", [P("invert", "白地に黒線", True)])
def edges(im, p, ctx):
    a = im.getchannel("A")
    e = ImageOps.grayscale(im.convert("RGB")).filter(ImageFilter.FIND_EDGES)
    e = ImageOps.autocontrast(e)
    if p["invert"]:
        e = ImageOps.invert(e)
    out = e.convert("RGBA")
    out.putalpha(a)
    return out


@effect("pixelart", "ドット絵風", "スタイライズ", [P("size", "ドットの大きさ", 8.0, 2, 64, 1, suffix=" px"), P("levels", "色数", 6, 2, 16)])
def pixelart(im, p, ctx):
    s = max(1, int(_px(p["size"], ctx)))
    small = im.resize((max(1, im.width // s), max(1, im.height // s)), Image.BILINEAR)
    small = posterize(small, {"levels": p["levels"]}, ctx)
    return small.resize(im.size, Image.NEAREST)


@effect("scanlines", "走査線", "スタイライズ", [P("gap", "間隔", 4.0, 2, 20, 1, suffix=" px"), P("dark", "暗さ", 0.35, 0, 1)])
def scanlines(im, p, ctx):
    a = _arr(im)
    g = max(2, int(_px(p["gap"], ctx)))
    rows = (np.arange(a.shape[0]) % g) < max(1, g // 2)
    a[rows, :, :3] *= 1 - float(p["dark"])
    return _img(a)


@effect("rgbshift", "色ずれ（RGB ずらし）", "スタイライズ", [P("amount", "ずれ", 6.0, 0, 60, 1, suffix=" px"),
                                                         P("angle", "向き", 0.0, -180, 180, 1, suffix="°")])
def rgbshift(im, p, ctx):
    a = np.asarray(im.convert("RGBA")).copy()
    d = _px(p["amount"], ctx)
    ang = math.radians(float(p["angle"]))
    dx, dy = int(round(math.cos(ang) * d)), int(round(math.sin(ang) * d))
    out = a.copy()
    out[..., 0] = np.roll(np.roll(a[..., 0], dx, axis=1), dy, axis=0)
    out[..., 2] = np.roll(np.roll(a[..., 2], -dx, axis=1), -dy, axis=0)
    out[..., 3] = np.maximum.reduce([a[..., 3], np.roll(np.roll(a[..., 3], dx, 1), dy, 0),
                                     np.roll(np.roll(a[..., 3], -dx, 1), -dy, 0)])
    return Image.fromarray(out, "RGBA")


@effect("vhs", "VHS 風", "スタイライズ", [P("strength", "強さ", 0.6, 0, 1)], animated=True)
def vhs(im, p, ctx):
    k = float(p["strength"])
    im = rgbshift(im, {"amount": 4 * k + 1, "angle": 0}, ctx)
    a = np.asarray(im).copy()
    rng = _rand(ctx, 3)
    h = a.shape[0]
    # 横方向のゆれ（数本の帯）
    for _ in range(int(3 * k) + 1):
        y0 = int(rng.integers(0, max(1, h - 4)))
        hh = int(rng.integers(2, max(3, h // 30)))
        a[y0:y0 + hh] = np.roll(a[y0:y0 + hh], int(rng.integers(-12, 13) * k * ctx.scale + 1), axis=1)
    f = a.astype(np.float32)
    f[..., :3] += rng.normal(0, 18 * k, a.shape[:2])[..., None]
    f[..., 1] *= 0.96
    out = _img(f)
    return scanlines(out, {"gap": 3, "dark": 0.18 * k}, ctx)


@effect("glitch", "グリッチ", "スタイライズ", [P("strength", "強さ", 0.5, 0, 1), P("rate", "頻度", 0.5, 0, 1)], animated=True)
def glitch(im, p, ctx):
    rng = _rand(ctx, 11)
    if rng.random() > float(p["rate"]):
        return im
    k = float(p["strength"])
    a = np.asarray(im.convert("RGBA")).copy()
    h, w = a.shape[:2]
    for _ in range(int(2 + 8 * k)):
        y0 = int(rng.integers(0, max(1, h - 2)))
        hh = int(rng.integers(1, max(2, int(h * 0.08 * k) + 2)))
        a[y0:y0 + hh] = np.roll(a[y0:y0 + hh], int(rng.integers(-int(w * 0.1 * k) - 1, int(w * 0.1 * k) + 2)), axis=1)
    if rng.random() < 0.5:
        ch = int(rng.integers(0, 3))
        a[..., ch] = np.roll(a[..., ch], int(rng.integers(-10, 11) * ctx.scale), axis=1)
    return Image.fromarray(a, "RGBA")


@effect("mirror", "ミラー（反転コピー）", "スタイライズ", [P("mode", "向き", "左を右へ", choices=["左を右へ", "右を左へ", "上を下へ", "下を上へ"])])
def mirror(im, p, ctx):
    w, h = im.size
    m = p["mode"]
    out = im.copy()
    if m in ("左を右へ", "右を左へ"):
        half = im.crop((0, 0, w // 2, h)) if m == "左を右へ" else im.crop((w - w // 2, 0, w, h))
        flip = ImageOps.mirror(half)
        out.paste(half if m == "左を右へ" else flip, (0, 0))
        out.paste(flip if m == "左を右へ" else half, (w - w // 2, 0))
    else:
        half = im.crop((0, 0, w, h // 2)) if m == "上を下へ" else im.crop((0, h - h // 2, w, h))
        flip = ImageOps.flip(half)
        out.paste(half if m == "上を下へ" else flip, (0, 0))
        out.paste(flip if m == "上を下へ" else half, (0, h - h // 2))
    return out


# ─────────────────────────── ゆがみ・ブラー ───────────────────────────

def _remap(a, xs, ys):
    h, w = a.shape[:2]
    xi = np.clip(np.round(xs).astype(np.int32), 0, w - 1)
    yi = np.clip(np.round(ys).astype(np.int32), 0, h - 1)
    out = a[yi, xi]
    outside = (xs < 0) | (xs > w - 1) | (ys < 0) | (ys > h - 1)
    out[outside] = 0
    return out


@effect("wave", "波（ゆらゆら）", "ゆがみ", [P("amp", "振れ幅", 8.0, 0, 80, 1, suffix=" px"), P("length", "波長", 120.0, 10, 800, 1, suffix=" px"),
                                       P("speed", "速さ", 1.0, 0, 5), P("dir", "向き", "横ゆれ", choices=["横ゆれ", "縦ゆれ"])],
        animated=True)
def wave(im, p, ctx):
    a = np.asarray(im.convert("RGBA"))
    h, w = a.shape[:2]
    amp, L = _px(p["amp"], ctx), max(2.0, _px(p["length"], ctx))
    ph = ctx.t * float(p["speed"]) * 2 * math.pi
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    if p["dir"] == "横ゆれ":
        xs, ys = xx + amp * np.sin(yy / L * 2 * math.pi + ph), yy
    else:
        xs, ys = xx, yy + amp * np.sin(xx / L * 2 * math.pi + ph)
    return Image.fromarray(_remap(a, xs, ys), "RGBA")


@effect("fisheye", "魚眼（レンズ）", "ゆがみ", [P("strength", "強さ", 0.5, -1, 1)])
def fisheye(im, p, ctx):
    a = np.asarray(im.convert("RGBA"))
    h, w = a.shape[:2]
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    nx, ny = (xx - w / 2) / (w / 2), (yy - h / 2) / (h / 2)
    r = np.clip(np.sqrt(nx ** 2 + ny ** 2), 0, 1)
    k = float(p["strength"])
    f = 1 - k * 0.5 * (1 - r ** 2)          # 正: 中心がふくらむ / 負: すぼまる
    return Image.fromarray(_remap(a, nx * f * (w / 2) + w / 2, ny * f * (h / 2) + h / 2), "RGBA")


@effect("zoomblur", "放射ブラー（ズーム）", "ゆがみ", [P("amount", "強さ", 0.15, 0, 0.6), P("steps", "滑らかさ", 8, 3, 24)])
def zoomblur(im, p, ctx):
    acc = _arr(im)
    n = int(p["steps"])
    w, h = im.size
    for i in range(1, n):
        k = 1 + float(p["amount"]) * i / n
        big = im.resize((int(w * k), int(h * k)), Image.BILINEAR)
        x0, y0 = (big.width - w) // 2, (big.height - h) // 2
        acc += _arr(big.crop((x0, y0, x0 + w, y0 + h)))
    return _img(acc / n)


@effect("motionblur", "方向ブラー", "ゆがみ", [P("length", "長さ", 20.0, 0, 200, 1, suffix=" px"), P("angle", "向き", 0.0, -180, 180, 1, suffix="°")])
def motionblur(im, p, ctx):
    L = _px(p["length"], ctx)
    if L < 1:
        return im
    a = _arr(im)
    acc = np.zeros_like(a)
    n = max(3, min(24, int(L / 2)))
    ang = math.radians(float(p["angle"]))
    for i in range(n):
        d = (i / (n - 1) - 0.5) * L
        acc += np.roll(np.roll(a, int(round(math.cos(ang) * d)), axis=1), int(round(math.sin(ang) * d)), axis=0)
    return _img(acc / n)


# ─────────────────────────── 演出 ───────────────────────────

@effect("speedlines", "集中線", "演出", [P("color", "色", "#FFFFFF"), P("count", "本数", 90, 10, 400),
                                    P("inner", "中心の空き", 0.45, 0, 0.9), P("width", "太さ", 1.0, 0.2, 4),
                                    P("flicker", "パラパラ動く", True)], animated=True,
        description="漫画風の集中線（強調・ツッコミに）")
def speedlines(im, p, ctx):
    w, h = im.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = _rand(ctx if p["flicker"] else type(ctx)(seed=ctx.seed), 21)
    cx, cy = w / 2, h / 2
    R = math.hypot(w, h) / 2
    col = _rgb(p["color"])
    for _ in range(int(p["count"])):
        a = rng.random() * 2 * math.pi
        r0 = R * (float(p["inner"]) + rng.random() * 0.25)
        half = (0.004 + rng.random() * 0.01) * float(p["width"])
        pts = [(cx + math.cos(a - half) * R * 1.1, cy + math.sin(a - half) * R * 1.1),
               (cx + math.cos(a + half) * R * 1.1, cy + math.sin(a + half) * R * 1.1),
               (cx + math.cos(a) * r0, cy + math.sin(a) * r0)]
        d.polygon(pts, fill=col + (int(170 + rng.random() * 85),))
    out = im.copy()
    out.alpha_composite(layer)
    return out


@effect("flash", "フラッシュ", "演出", [P("color", "色", "#FFFFFF"), P("length", "長さ", 8, 1, 60, suffix=" f"),
                                   P("at_end", "終わりで光る", False)], animated=True)
def flash(im, p, ctx):
    n = max(1, int(p["length"]))
    rel = (ctx.duration - 1 - ctx.rel) if p["at_end"] else ctx.rel
    if rel >= n:
        return im
    k = 1 - rel / n
    a = _arr(im)
    a[..., :3] = a[..., :3] * (1 - k) + np.array(_rgb(p["color"]), np.float32) * k
    return _img(a)


@effect("blinkfx", "点滅", "演出", [P("interval", "間隔", 10, 2, 120, suffix=" f"), P("duty", "見えている割合", 0.5, 0.1, 0.9)],
        animated=True)
def blinkfx(im, p, ctx):
    n = max(2, int(p["interval"]))
    if (ctx.rel % n) < n * float(p["duty"]):
        return im
    out = im.copy()
    out.putalpha(0)
    return out


def _particles(im, ctx, count, draw_fn, salt, fall_speed, drift=0.0):
    w, h = im.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = np.random.default_rng(ctx.seed * 31 + salt)
    n = int(count)
    xs, ys = rng.random(n), rng.random(n)
    sp = 0.5 + rng.random(n)
    sz = rng.random(n)
    t = ctx.t
    for i in range(n):
        y = (ys[i] + t * fall_speed * sp[i]) % 1.0
        x = (xs[i] + t * drift * sp[i] + 0.02 * math.sin(t * 2 + i)) % 1.0
        draw_fn(d, x * w, y * h, sz[i], i)
    out = im.copy()
    out.alpha_composite(layer)
    return out


@effect("rain", "雨", "演出", [P("amount", "量", 150, 10, 800), P("angle", "傾き", 12.0, -45, 45, 1, suffix="°"),
                             P("opacity", "濃さ", 0.5, 0, 1)], animated=True)
def rain(im, p, ctx):
    L = 40 * ctx.scale
    ang = math.radians(float(p["angle"]))
    al = int(255 * float(p["opacity"]))

    def draw(d, x, y, s, i):
        d.line([(x, y), (x - math.sin(ang) * L * (0.5 + s), y + math.cos(ang) * L * (0.5 + s))],
               fill=(200, 210, 230, al), width=max(1, int(1.5 * ctx.scale)))
    return _particles(im, ctx, p["amount"], draw, 5, 1.6, -math.tan(ang) * 0.3)


@effect("snow", "雪", "演出", [P("amount", "量", 120, 10, 600), P("size", "大きさ", 4.0, 1, 20, 0.5, suffix=" px")], animated=True)
def snow(im, p, ctx):
    r0 = _px(p["size"], ctx)

    def draw(d, x, y, s, i):
        r = max(1.0, r0 * (0.4 + s))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, int(150 + 100 * s)))
    return _particles(im, ctx, p["amount"], draw, 9, 0.12, 0.03)


@effect("sparkle", "キラキラ", "演出", [P("amount", "数", 30, 1, 200), P("color", "色", "#FFF6B0"),
                                    P("size", "大きさ", 14.0, 2, 60, 1, suffix=" px")], animated=True)
def sparkle(im, p, ctx):
    w, h = im.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rng = np.random.default_rng(ctx.seed * 17 + 77)
    n = int(p["amount"])
    xs, ys, ph = rng.random(n), rng.random(n), rng.random(n) * 6.28
    col = _rgb(p["color"])
    R = _px(p["size"], ctx)
    for i in range(n):
        k = max(0.0, math.sin(ctx.t * 4 + ph[i]))
        if k < 0.05:
            continue
        r = R * k
        x, y = xs[i] * w, ys[i] * h
        a = int(255 * k)
        d.polygon([(x, y - r), (x + r * 0.18, y - r * 0.18), (x + r, y), (x + r * 0.18, y + r * 0.18),
                   (x, y + r), (x - r * 0.18, y + r * 0.18), (x - r, y), (x - r * 0.18, y - r * 0.18)], fill=col + (a,))
    if R > 3:
        layer = Image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(R * 0.15)), layer)
    out = im.copy()
    out.alpha_composite(layer)
    return out


@effect("lightleak", "光漏れ", "演出", [P("color", "色", "#FF9A3C"), P("amount", "強さ", 0.5, 0, 1)], animated=True)
def lightleak(im, p, ctx):
    a = _arr(im)
    h, w = a.shape[:2]
    cx = w * (0.2 + 0.6 * (0.5 + 0.5 * math.sin(ctx.t * 0.7 + ctx.seed)))
    y, x = np.ogrid[:h, :w]
    g = np.exp(-(((x - cx) / (w * 0.35)) ** 2 + ((y - h * 0.2) / (h * 0.6)) ** 2))
    k = (g * float(p["amount"]))[..., None]
    c = np.array(_rgb(p["color"]), np.float32)
    a[..., :3] = 255 - (255 - a[..., :3]) * (1 - k * c / 255)      # スクリーン合成
    return _img(a)


# ─────────────────────────── マスク・枠 ───────────────────────────

@effect("roundrect", "角丸", "マスク・枠", [P("radius", "丸み", 0.1, 0, 0.5)])
def roundrect(im, p, ctx):
    w, h = im.size
    m = Image.new("L", (w, h), 0)
    r = int(min(w, h) * float(p["radius"]))
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel("A"), m))
    return out


@effect("circlemask", "円形に切り抜き", "マスク・枠", [P("size", "大きさ", 1.0, 0.1, 1.5), P("feather", "ぼかし", 0.0, 0, 50, 1, suffix=" px")])
def circlemask(im, p, ctx):
    w, h = im.size
    m = Image.new("L", (w, h), 0)
    r = min(w, h) / 2 * float(p["size"])
    ImageDraw.Draw(m).ellipse([w / 2 - r, h / 2 - r, w / 2 + r, h / 2 + r], fill=255)
    if float(p["feather"]) > 0:
        m = m.filter(ImageFilter.GaussianBlur(_px(p["feather"], ctx)))
    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel("A"), m))
    return out


@effect("border", "枠線", "マスク・枠", [P("color", "色", "#FFFFFF"), P("width", "太さ", 8.0, 1, 80, 1, suffix=" px")])
def border(im, p, ctx):
    out = im.copy()
    wpx = max(1, int(_px(p["width"], ctx)))
    ImageDraw.Draw(out).rectangle([wpx // 2, wpx // 2, im.width - 1 - wpx // 2, im.height - 1 - wpx // 2],
                                  outline=_rgb(p["color"]) + (255,), width=wpx)
    return out


@effect("featheredge", "ふちをぼかす", "マスク・枠", [P("width", "幅", 40.0, 1, 300, 1, suffix=" px")])
def featheredge(im, p, ctx):
    w, h = im.size
    f = max(1, int(_px(p["width"], ctx)))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rectangle([f, f, w - 1 - f, h - 1 - f], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(f / 2))
    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel("A"), m))
    return out


@effect("chromakey_spill", "緑かぶり除去", "マスク・枠", [P("amount", "強さ", 1.0, 0, 1)],
        description="グリーンバックの縁に残る緑を抑える（クロマキーと一緒に）")
def despill(im, p, ctx):
    a = _arr(im)
    lim = (a[..., 0] + a[..., 2]) / 2
    g = a[..., 1]
    a[..., 1] = g - np.clip(g - lim, 0, None) * float(p["amount"])
    return _img(a)



# ─────────────────────────── 3D ───────────────────────────

def _persp_coeffs(src, dst):
    """dst の 4 点 → src の 4 点 へ写す射影変換の係数（PIL の PERSPECTIVE 用）。"""
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        B += [u, v]
    return np.linalg.solve(np.array(A, np.float64), np.array(B, np.float64)).tolist()


@effect("transform3d", "3D 回転（パース）", "3D", [
    P("rx", "X 回転（縦に倒す）", 0.0, -180, 180, 1, suffix="°"),
    P("ry", "Y 回転（横に回す）", 30.0, -180, 180, 1, suffix="°"),
    P("rz", "Z 回転", 0.0, -180, 180, 1, suffix="°"),
    P("distance", "カメラ距離（小さいほど遠近が強い）", 2.0, 0.6, 10, 0.1),
    P("spin", "回り続ける速さ（Y・度/秒）", 0.0, -720, 720, 1),
    P("backface", "裏面も表示", True),
], expands=True, description="AE の 3D レイヤー風。キーフレームで回転を動かせる")
def transform3d(im, p, ctx):
    w, h = im.size
    ry = float(p["ry"]) + float(p["spin"]) * ctx.t
    ax, ay, az = math.radians(float(p["rx"])), math.radians(ry), math.radians(float(p["rz"]))
    if abs(ax) < 1e-6 and abs(ay) < 1e-6 and abs(az) < 1e-6:
        return im
    pts = [(-w / 2, -h / 2, 0), (w / 2, -h / 2, 0), (w / 2, h / 2, 0), (-w / 2, h / 2, 0)]
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    f = max(w, h) * float(p["distance"])
    out_pts = []
    for x, y, z in pts:
        x, y = x * cz - y * sz, x * sz + y * cz
        y, z = y * cx - z * sx, y * sx + z * cx
        x, z = x * cy + z * sy, -x * sy + z * cy
        k = f / max(1e-3, f + z)
        out_pts.append((x * k, y * k))
    area = sum(out_pts[i][0] * out_pts[(i + 1) % 4][1] - out_pts[(i + 1) % 4][0] * out_pts[i][1] for i in range(4))
    if area < 0 and not p["backface"]:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if abs(area) < 1.0:
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))      # 真横を向いて見えない
    xs = [q[0] for q in out_pts]
    ys = [q[1] for q in out_pts]
    W = max(w, int(math.ceil(2 * max(abs(min(xs)), abs(max(xs))))) + 2)
    H = max(h, int(math.ceil(2 * max(abs(min(ys)), abs(max(ys))))) + 2)
    dst = [(x + W / 2, y + H / 2) for x, y in out_pts]
    src = [(0, 0), (w, 0), (w, h), (0, h)]
    try:
        coeffs = _persp_coeffs(src, dst)
    except np.linalg.LinAlgError:
        return Image.new("RGBA", (W, H), (0, 0, 0, 0))
    return im.transform((W, H), Image.PERSPECTIVE, coeffs, Image.BILINEAR)


# ─────────────────────────── グレーディング ───────────────────────────

_lut_cache = {}


def _load_cube(path):
    hit = _lut_cache.get(path)
    if hit is not None:
        return hit
    size, rows = 0, []
    dmin, dmax = np.zeros(3), np.ones(3)
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            t = line.strip()
            if not t or t.startswith("#"):
                continue
            k = t.split()
            if k[0] == "LUT_3D_SIZE":
                size = int(k[1])
            elif k[0] == "DOMAIN_MIN":
                dmin = np.array([float(v) for v in k[1:4]])
            elif k[0] == "DOMAIN_MAX":
                dmax = np.array([float(v) for v in k[1:4]])
            elif k[0][0] in "0123456789-.":
                rows.append([float(v) for v in k[:3]])
    if size < 2 or len(rows) < size ** 3:
        raise ValueError("3D LUT（.cube）を読めません")
    lut_ = np.array(rows[:size ** 3], np.float32).reshape(size, size, size, 3)   # [b][g][r]（.cube は R が最も速く変わる）
    _lut_cache[path] = (lut_, dmin, dmax)
    return _lut_cache[path]


@effect("lut", "LUT（.cube）", "カラー", [P("path", "LUT ファイル", "", kind="file"), P("amount", "強さ", 1.0, 0, 1)],
        description="DaVinci Resolve / Premiere のカラー LUT（3D .cube）を適用")
def lut(im, p, ctx):
    if not p["path"]:
        return im
    table, dmin, dmax = _load_cube(p["path"])
    n = table.shape[0]
    a = _arr(im)
    rgb = (a[..., :3] / 255 - dmin) / np.maximum(dmax - dmin, 1e-6)
    x = np.clip(rgb, 0, 1) * (n - 1)
    i0 = np.floor(x).astype(np.int32)
    i1 = np.minimum(i0 + 1, n - 1)
    f = x - i0
    r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
    r1, g1, b1 = i1[..., 0], i1[..., 1], i1[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    c = lambda b, g, r: table[b, g, r]
    out = (((c(b0, g0, r0) * (1 - fr) + c(b0, g0, r1) * fr) * (1 - fg) +
            (c(b0, g1, r0) * (1 - fr) + c(b0, g1, r1) * fr) * fg) * (1 - fb) +
           ((c(b1, g0, r0) * (1 - fr) + c(b1, g0, r1) * fr) * (1 - fg) +
            (c(b1, g1, r0) * (1 - fr) + c(b1, g1, r1) * fr) * fg) * fb)
    k = float(p["amount"])
    a[..., :3] = a[..., :3] * (1 - k) + out * 255 * k
    return _img(a)


@effect("colorwheels", "カラーホイール（リフト / ガンマ / ゲイン）", "カラー", [
    P("lift", "リフト（暗部）の色", "#808080"), P("lift_l", "リフトの明るさ", 0.0, -0.5, 0.5),
    P("gamma", "ガンマ（中間）の色", "#808080"), P("gamma_l", "ガンマの明るさ", 0.0, -0.5, 0.5),
    P("gain", "ゲイン（明部）の色", "#808080"), P("gain_l", "ゲインの明るさ", 0.0, -0.5, 0.5),
], description="DaVinci Resolve のプライマリー補正。灰色（#808080）が中立で、ずらした色の方向へ寄せる")
def colorwheels(im, p, ctx):
    a = _arr(im)
    x = a[..., :3] / 255
    sh = lambda col: (np.array(_rgb(col), np.float32) - 128) / 255
    lift = sh(p["lift"]) + float(p["lift_l"])
    gain = 1 + sh(p["gain"]) * 2 + float(p["gain_l"])
    gam = np.clip(1 - (sh(p["gamma"]) + float(p["gamma_l"])), 0.2, 5)
    y = np.clip(x * (gain - lift) + lift, 0, 1) ** gam
    a[..., :3] = y * 255
    return _img(a)


@effect("shapemask", "図形マスク", "マスク・枠", [
    P("shape", "形", "楕円", choices=["楕円", "四角", "角丸四角"]),
    P("x", "中心 X（%）", 50.0, -50, 150, 1), P("y", "中心 Y（%）", 50.0, -50, 150, 1),
    P("w", "幅（%）", 60.0, 1, 200, 1), P("h", "高さ（%）", 60.0, 1, 200, 1),
    P("feather", "ぼかし", 10.0, 0, 200, 1, suffix=" px"), P("invert", "反転（外側を残す）", False),
], description="After Effects のマスク。キーフレームで動かせる")
def shapemask(im, p, ctx):
    w, h = im.size
    cx, cy = float(p["x"]) / 100 * w, float(p["y"]) / 100 * h
    mw, mh = float(p["w"]) / 100 * w / 2, float(p["h"]) / 100 * h / 2
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    box = [cx - mw, cy - mh, cx + mw, cy + mh]
    if p["shape"] == "楕円":
        d.ellipse(box, fill=255)
    elif p["shape"] == "角丸四角":
        d.rounded_rectangle(box, radius=int(min(mw, mh) * 0.3), fill=255)
    else:
        d.rectangle(box, fill=255)
    if float(p["feather"]) > 0:
        m = m.filter(ImageFilter.GaussianBlur(_px(p["feather"], ctx)))
    if p["invert"]:
        m = ImageOps.invert(m)
    out = im.copy()
    out.putalpha(ImageChops.multiply(im.getchannel("A"), m))
    return out
