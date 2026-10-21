"""「すぐ使う」のエフェクト（エフェクトエディターでパラメーターを変えられる・キーフレームも打てる）。

ルック・背景を抜く・モザイク・揺れ・カクカク・ワイプの枠・ぼかし背景・スポットライト・虫めがね・巻き戻し風
（揺れとカクカクは画像ではなく、置く位置 / 使うコマを変えるので、ここでは画像はそのまま返す。render が読む）
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from .fxlib import P, effect

CAT = "すぐ使う"

LOOK_KINDS = ["明るく", "あたたかい", "すずしい", "シネマ", "レトロ", "モノクロ", "くっきり", "パステル", "夜"]
KEY_KINDS = {"緑": "#00ff00", "青": "#0000ff", "白": "#ffffff", "黒": "#000000"}


def _keep_alpha(src: Image.Image, rgb: Image.Image) -> Image.Image:
    out = rgb.convert("RGBA")
    out.putalpha(src.getchannel("A"))
    return out


def look_rgb(rgb: Image.Image, kind: str, ctx) -> Image.Image:
    """ルックの色づけ（RGB）。"""
    from .fxlib import get
    if kind == "明るく":
        return ImageEnhance.Contrast(ImageEnhance.Brightness(rgb).enhance(1.15)).enhance(1.05)
    if kind in ("あたたかい", "すずしい"):
        t = get("temperature")
        im = t.fn(rgb.convert("RGBA"), {"value": 35.0 if kind == "あたたかい" else -35.0}, ctx).convert("RGB")
        return ImageEnhance.Color(im).enhance(1.1) if kind == "あたたかい" else im
    if kind == "シネマ":
        im = ImageEnhance.Color(ImageEnhance.Contrast(rgb).enhance(1.12)).enhance(0.85)
        a = np.asarray(im).astype(np.float32)
        a[..., 2] = a[..., 2] * 0.92 + 12          # 影を少し青緑に・明るい所を暖かく（ティール＆オレンジ風）
        a[..., 0] = a[..., 0] * 1.04
        return Image.fromarray(a.clip(0, 255).astype(np.uint8))
    if kind == "レトロ":
        im = ImageOps.colorize(ImageOps.grayscale(rgb), "#2b1d0e", "#f3e0c0")
        return Image.blend(rgb, im, 0.7)
    if kind == "モノクロ":
        return ImageEnhance.Contrast(ImageOps.grayscale(rgb).convert("RGB")).enhance(1.1)
    if kind == "くっきり":
        return ImageEnhance.Color(ImageEnhance.Sharpness(rgb).enhance(2.2)).enhance(1.15)
    if kind == "パステル":
        return ImageEnhance.Brightness(ImageEnhance.Color(ImageEnhance.Contrast(rgb).enhance(0.85)).enhance(0.75)).enhance(1.1)
    if kind == "夜":
        a = np.asarray(ImageEnhance.Brightness(rgb).enhance(0.55)).astype(np.float32)
        a[..., 2] *= 1.25
        a[..., 0] *= 0.8
        return Image.fromarray(a.clip(0, 255).astype(np.uint8))
    return rgb


@effect("look", "ルック（色の雰囲気）", CAT, [P("kind", "種類", "シネマ", choices=LOOK_KINDS), P("amount", "強さ", 1.0, 0, 1)])
def fx_look(im, p, ctx):
    rgb = im.convert("RGB")
    out = look_rgb(rgb, str(p["kind"]), ctx)
    amt = float(p["amount"])
    if amt < 1.0:
        out = Image.blend(rgb, out.convert("RGB"), max(0.0, amt))
    return _keep_alpha(im, out)


@effect("keyout", "背景を抜く（グリーンバックなど）", CAT,
        [P("color", "抜く色", "#00ff00"), P("tol", "許容量", 0.25, 0, 1), P("soft", "なめらかさ", 0.08, 0.001, 0.5)])
def fx_keyout(im, p, ctx):
    from .render import _chroma
    return _chroma(im, {"color": p["color"], "tol": p["tol"], "soft": p["soft"]})


@effect("q_mosaic", "モザイク（範囲）", CAT,
        [P("size", "ブロックの大きさ", 16, 2, 128), P("x", "範囲 左（%）", 35.0, 0, 100, 1), P("y", "範囲 上（%）", 30.0, 0, 100, 1),
         P("w", "範囲 幅（%）", 30.0, 1, 100, 1), P("h", "範囲 高さ（%）", 40.0, 1, 100, 1)])
def fx_mosaic(im, p, ctx):
    bs = max(2, int(int(p["size"]) * ctx.scale))
    w0, h0 = im.size
    x, y = float(p["x"]) / 100, float(p["y"]) / 100
    w, h = float(p["w"]) / 100, float(p["h"]) / 100
    box = (int(w0 * x), int(h0 * y), max(int(w0 * x) + 1, min(w0, int(w0 * (x + w)))), max(int(h0 * y) + 1, min(h0, int(h0 * (y + h)))))
    part = im.crop(box)
    small = part.resize((max(1, part.width // bs), max(1, part.height // bs)), Image.BILINEAR)
    out = im.copy()
    out.paste(small.resize(part.size, Image.NEAREST), box[:2])
    return out


@effect("q_shake", "揺れ", CAT, [P("amount", "強さ（px）", 18.0, 0, 100, 1)], animated=True)
def fx_shake(im, p, ctx):
    return im                      # 置く位置を揺らす（render._shake_offset）


@effect("framestep", "カクカク（コマを間引く）", CAT, [P("step", "何コマごと", 3, 2, 12)])
def fx_framestep(im, p, ctx):
    return im                      # 使うコマを間引く（render.src_frame）


@effect("wipeframe", "ワイプの枠（角丸・枠・影）", CAT,
        [P("radius", "角の丸さ（%）", 6.0, 0, 50, 1), P("border", "枠の太さ（px）", 6.0, 0, 40, 1), P("color", "枠の色", "#ffffff"),
         P("shadow", "影", True)], expands=True)
def fx_wipeframe(im, p, ctx):
    w, h = im.size
    r = int(min(w, h) * float(p["radius"]) / 100)
    b = int(round(float(p["border"]) * ctx.scale))
    pad = int(max(b, 12 * ctx.scale if p["shadow"] else 0)) + 2
    W, H = w + pad * 2, h + pad * 2
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if p["shadow"]:
        sh = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([pad + 4, pad + 6, pad + w + 4, pad + h + 6], radius=r, fill=(0, 0, 0, 150))
        out = Image.alpha_composite(out, sh.filter(ImageFilter.GaussianBlur(max(1.0, 6 * ctx.scale))))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    a = ImageChops_multiply(im.getchannel("A"), mask)
    body = im.copy()
    body.putalpha(a)
    out.alpha_composite(body, (pad, pad))
    if b > 0:
        h_ = str(p["color"]).lstrip("#")
        col = tuple(int(h_[i:i + 2], 16) for i in (0, 2, 4)) if len(h_) >= 6 else (255, 255, 255)
        ImageDraw.Draw(out).rounded_rectangle([pad - b // 2, pad - b // 2, pad + w + b // 2 - 1, pad + h + b // 2 - 1],
                                              radius=r + b // 2, outline=col + (255,), width=b)
    return out


def ImageChops_multiply(a: Image.Image, b: Image.Image) -> Image.Image:
    from PIL import ImageChops
    return ImageChops.multiply(a, b)


@effect("blurfill", "ぼかし背景で埋める（縦長の写真・動画）", CAT,
        [P("blur", "ぼかし", 25.0, 0, 80, 1), P("dark", "背景の暗さ", 0.25, 0, 0.9)])
def fx_blurfill(im, p, ctx):
    W, H = int(getattr(ctx, "fw", 0) or 0), int(getattr(ctx, "fh", 0) or 0)
    if not W or not H:
        return im
    w, h = im.size
    if w >= W - 2 and h >= H - 2:
        return im                                         # もう画面いっぱい
    k = max(W / max(1, w), H / max(1, h))
    bg = im.convert("RGB").resize((max(1, int(w * k)), max(1, int(h * k))), Image.BILINEAR)
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2, (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    bg = bg.filter(ImageFilter.GaussianBlur(max(0.1, float(p["blur"]) * ctx.scale)))
    bg = ImageEnhance.Brightness(bg).enhance(1.0 - float(p["dark"])).convert("RGBA")
    bg.alpha_composite(im, ((W - w) // 2, (H - h) // 2))
    return bg


@effect("spotlight", "スポットライト（周りを暗く）", CAT,
        [P("x", "中心 横（%）", 50.0, 0, 100, 1), P("y", "中心 縦（%）", 50.0, 0, 100, 1), P("r", "大きさ（%）", 22.0, 2, 100, 1),
         P("dark", "暗さ", 0.65, 0, 1), P("soft", "ぼかし（%）", 6.0, 0, 50, 1)])
def fx_spotlight(im, p, ctx):
    w, h = im.size
    cx, cy = w * float(p["x"]) / 100, h * float(p["y"]) / 100
    r = min(w, h) * float(p["r"]) / 100
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    soft = max(1.0, min(w, h) * float(p["soft"]) / 100)
    k = np.clip((d - r) / soft, 0, 1) * float(p["dark"])
    a = np.asarray(im).astype(np.float32)
    a[..., :3] *= (1 - k)[..., None]
    return Image.fromarray(a.clip(0, 255).astype(np.uint8), "RGBA")


@effect("magnifier", "虫めがね（一部を拡大）", CAT,
        [P("x", "見る所 横（%）", 50.0, 0, 100, 1), P("y", "見る所 縦（%）", 50.0, 0, 100, 1), P("r", "レンズの大きさ（%）", 16.0, 2, 50, 1),
         P("zoom", "倍率", 2.0, 1, 6), P("ox", "レンズの位置 横（%）", 75.0, 0, 100, 1), P("oy", "レンズの位置 縦（%）", 30.0, 0, 100, 1),
         P("color", "ふちの色", "#ffffff")])
def fx_magnifier(im, p, ctx):
    w, h = im.size
    r = max(4, int(min(w, h) * float(p["r"]) / 100))
    z = max(1.0, float(p["zoom"]))
    cx, cy = w * float(p["x"]) / 100, h * float(p["y"]) / 100
    src = r / z
    part = im.crop((int(cx - src), int(cy - src), int(cx + src), int(cy + src))).resize((2 * r, 2 * r), Image.BILINEAR)
    mask = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    out = im.copy()
    ox, oy = int(w * float(p["ox"]) / 100) - r, int(h * float(p["oy"]) / 100) - r
    out.paste(part, (ox, oy), mask)
    h_ = str(p["color"]).lstrip("#")
    col = tuple(int(h_[i:i + 2], 16) for i in (0, 2, 4)) if len(h_) >= 6 else (255, 255, 255)
    d = ImageDraw.Draw(out)
    lw = max(2, int(4 * ctx.scale))
    d.ellipse([ox, oy, ox + 2 * r, oy + 2 * r], outline=col + (255,), width=lw)
    d.ellipse([cx - src, cy - src, cx + src, cy + src], outline=col + (200,), width=max(1, lw // 2))
    return out
