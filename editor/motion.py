"""After Effects / Blender 風のモーショングラフィックス（描画側）。

- 親子付け: props["parent"] = 親クリップの id。位置・拡大・回転を親から受け継ぐ（AE のペアレント）。
  見えない親にはヌル（kind="null"）を使う
- エクスプレッション: props["expr"] = {"pos_x": "wiggle(2, 30)", "rotation": "time*90", ...}
  使える名前: value・time（秒）・frame・fps・wiggle(回数/秒, 幅)・sin/cos/abs/min/max/pi・
             loop(周期秒)・ease(t)・random(seed)・linear(t, t0, t1, v0, v1)
- 2.5D の 3D: props["layer3d"]=True のレイヤーは z（奥行き）とカメラ（kind="camera" のクリップ）で遠近が付く。
  カメラの props: pos_x / pos_y（パン）・pos_z（前後）・rotation（ロール）・zoom（焦点距離の倍率）
- マスク: props["mask"] = {"shape": rect/ellipse/poly, "rect": [x, y, w, h]（%）, "points": [[x, y], ...]（%）,
                          "feather": ぼかし px, "invert": 反転, "expand": 広げる px}
- モーションブラー: props["motion_blur"]=True（動いている間だけ、シャッター角 shutter・サンプル数 mb_samples）
"""
from __future__ import annotations

import ast
import math
from typing import Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter

EXPR_PROPS = ("scale", "rotation", "pos_x", "pos_y", "opacity", "z")
CAMERA_DIST = 1000.0          # 3D の基準距離（px・1080p 換算）。z=0 のとき等倍
FPS = [30.0]                  # 描画中のシーケンス fps（エクスプレッションの time 用）


# ─────────────────────────── エクスプレッション ───────────────────────────
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load, ast.Constant,
                  ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd,
                  ast.IfExp, ast.Compare, ast.Lt, ast.Gt, ast.LtE, ast.GtE, ast.Eq, ast.NotEq, ast.BoolOp,
                  ast.And, ast.Or, ast.Tuple)
_cache: Dict[str, object] = {}


def stable_seed(text: str) -> int:
    """文字列から毎回同じ数を作る（hash() は起動のたびに変わるので、プレビュー・書き出し・ファームの PC で揺れ方がずれる）。"""
    import zlib
    return zlib.crc32(str(text).encode("utf-8"))


def _noise1(x: float, seed: int) -> float:
    """なめらかなノイズ（-1〜1）。"""
    i = math.floor(x)
    f = x - i

    def h(n):
        n = (n * 374761393 + seed * 668265263) & 0xFFFFFFFF
        n = ((n ^ (n >> 13)) * 1274126177) & 0xFFFFFFFF
        return ((n ^ (n >> 16)) & 0xFFFF) / 32767.5 - 1.0
    u = f * f * (3 - 2 * f)
    return h(i) * (1 - u) + h(i + 1) * u


def compile_expr(src: str):
    """安全な式だけを許して compile（属性アクセス・import などは不可）。"""
    if src in _cache:
        return _cache[src]
    if len(src) > 500:
        raise ValueError("式が長すぎます")
    tree = ast.parse(src.strip(), mode="eval")
    for n in ast.walk(tree):
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow):
            raise ValueError("累乗は pow(a, b) を使ってください")
        if not isinstance(n, _ALLOWED_NODES):
            raise ValueError(f"使えない書き方です: {type(n).__name__}")
        if isinstance(n, ast.Constant) and not isinstance(n.value, (int, float, bool)):
            raise ValueError("数字だけが使えます")
        if isinstance(n, ast.Call) and not isinstance(n.func, ast.Name):
            raise ValueError("関数は名前で呼んでください")
    code = compile(tree, "<expr>", "eval")
    _cache[src] = code
    return code


def eval_expr(src: str, value: float, frame: float, fps: float, seed: int = 1) -> float:
    t = frame / max(1e-6, fps)
    env = {
        "value": value, "time": t, "frame": frame, "fps": fps, "pi": math.pi,
        "sin": math.sin, "cos": math.cos, "abs": abs, "min": min, "max": max, "sqrt": math.sqrt,
        "floor": math.floor, "round": round, "int": int,
        "pow": lambda a, b: math.pow(max(-1e6, min(1e6, a)), max(-50, min(50, b))),
        "wiggle": lambda freq, amp, s=0: value + amp * _noise1(t * freq, seed + int(s)),
        "loop": lambda period: (t % period) if period else t,
        "ease": lambda x: 1 - (1 - max(0.0, min(1.0, x))) ** 3,
        "random": lambda s=0: (math.sin((seed + s) * 12.9898) * 43758.5453) % 1.0,
        "linear": lambda x, x0, x1, v0, v1: v0 + (v1 - v0) * max(0.0, min(1.0, (x - x0) / ((x1 - x0) or 1e-6))),
    }
    return float(eval(compile_expr(src), {"__builtins__": {}}, env))


def check_expr(src: str) -> str:
    """問題なければ ""、だめならエラーの説明。"""
    try:
        eval_expr(src, 1.0, 10, 30)
        return ""
    except Exception as ex:
        return str(ex)


def apply_expr(clip, key: str, value, cf: float):
    ex = (clip.props.get("expr") or {}).get(key) if clip.props else None
    if not ex:
        return value
    try:
        return eval_expr(ex, float(value or 0.0), cf - clip.start, FPS[0], seed=stable_seed(clip.id) & 0xFFFF)
    except Exception:
        return value


# ─────────────────────────── 親子付け・カメラ ───────────────────────────
def find_clip(seq, cid: str):
    for t in seq.video_tracks:
        for c in t.clips:
            if c.id == cid:
                return c
    return None


def camera_at(seq, frame: float):
    for t in reversed(seq.video_tracks):
        if t.muted:
            continue
        for c in t.clips:
            if c.kind == "camera" and c.enabled and c.start <= frame < c.end:
                return c
    return None


def local_transform(clip, cf: float, scale: float) -> Tuple[float, float, float, float]:
    """クリップ自身の (拡大, 回転, x, y)。x / y は出力ピクセル。キーフレーム・アニメ・エクスプレッション込み。"""
    from .render import prop_at
    s = float(apply_expr(clip, "scale", prop_at(clip, "scale", cf) or 1.0, cf) or 1.0)
    r = float(apply_expr(clip, "rotation", prop_at(clip, "rotation", cf) or 0.0, cf) or 0.0)
    x = float(apply_expr(clip, "pos_x", prop_at(clip, "pos_x", cf) or 0.0, cf) or 0.0) * scale
    y = float(apply_expr(clip, "pos_y", prop_at(clip, "pos_y", cf) or 0.0, cf) or 0.0) * scale
    return s, r, x, y


def transform(seq, clip, cf: float, scale: float) -> Tuple[float, float, float, float]:
    """親子付け・3D カメラまで入れた最終的な (拡大, 回転, x, y)。"""
    s, r, x, y = local_transform(clip, cf, scale)
    seen = {clip.id}
    p = clip.props.get("parent") if clip.props else None
    while p and p not in seen and len(seen) < 16:
        seen.add(p)
        par = find_clip(seq, p)
        if par is None:
            break
        ps, pr, px, py = local_transform(par, cf, scale)
        a = math.radians(pr)
        cx, cy = x * ps, y * ps
        x = px + cx * math.cos(a) - cy * math.sin(a)
        y = py + cx * math.sin(a) + cy * math.cos(a)
        s *= ps
        r += pr
        p = par.props.get("parent")
    if clip.props and clip.props.get("layer3d"):
        from .render import prop_at
        z = float(apply_expr(clip, "z", prop_at(clip, "z", cf) or 0.0, cf) or 0.0)
        cam = camera_at(seq, cf)
        cz, czoom, cx_, cy_, croll = 0.0, 1.0, 0.0, 0.0, 0.0
        if cam is not None:
            cs, croll, cx_, cy_ = local_transform(cam, cf, scale)
            cz = float(apply_expr(cam, "z", prop_at(cam, "pos_z", cf) or 0.0, cf) or 0.0)
            czoom = float(prop_at(cam, "zoom", cf) or 1.0)
        d = CAMERA_DIST * czoom
        depth = CAMERA_DIST + (z - cz)
        if depth <= 10:
            return 0.0, r, x, y                      # カメラの後ろ → 見えない
        f = d / depth
        x, y = (x - cx_) * f, (y - cy_) * f
        if croll:
            a = math.radians(-croll)
            x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
            r -= croll
        s *= f
    return s, r, x, y


def is_dynamic(seq, clip) -> bool:
    """親・3D・エクスプレッション・モーションブラーなど、フレームごとに見た目が変わり得るか。"""
    p = clip.props or {}
    return bool(p.get("parent") or p.get("layer3d") or p.get("expr") or p.get("motion_blur"))


# ─────────────────────────── マスク ───────────────────────────
def mask_image(size, mask: dict, scale: float = 1.0) -> Optional[Image.Image]:
    """マスク（L 画像・255 = 見える）。size は素材画像の大きさ。"""
    if not mask or not mask.get("shape"):
        return None
    w, h = size
    m = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(m)
    shape = mask.get("shape")
    ex = float(mask.get("expand", 0) or 0) * scale
    if shape in ("rect", "ellipse"):
        x, y, mw, mh = [float(v) for v in (mask.get("rect") or [25, 25, 50, 50])]
        box = [w * x / 100 - ex, h * y / 100 - ex, w * (x + mw) / 100 + ex, h * (y + mh) / 100 + ex]
        if box[2] > box[0] and box[3] > box[1]:
            if shape == "rect":
                rad = float(mask.get("radius", 0) or 0) * min(w, h) / 100
                d.rounded_rectangle(box, radius=rad, fill=255)
            else:
                d.ellipse(box, fill=255)
    elif shape == "poly":
        pts = [(w * float(px) / 100, h * float(py) / 100) for px, py in (mask.get("points") or [])]
        if len(pts) >= 3:
            d.polygon(pts, fill=255)
            if ex > 0:
                m = m.filter(ImageFilter.MaxFilter(int(ex) * 2 + 1))
    fe = float(mask.get("feather", 0) or 0) * scale
    if fe > 0:
        m = m.filter(ImageFilter.GaussianBlur(fe))
    if mask.get("invert"):
        from PIL import ImageOps
        m = ImageOps.invert(m)
    return m


def apply_mask(im: Image.Image, mask: dict, scale: float = 1.0) -> Image.Image:
    m = mask_image(im.size, mask, scale)
    if m is None:
        return im
    im = im.convert("RGBA")
    from PIL import ImageChops
    a = ImageChops.multiply(im.getchannel("A"), m)
    im = im.copy()
    im.putalpha(a)
    return im


# ─────────────────────────── 図形（シェイプレイヤー）───────────────────────────
def star_points(cx, cy, r_out, r_in, n, rot=-90.0):
    pts = []
    for i in range(n * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.radians(rot + i * 180.0 / n)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def polygon_points(cx, cy, r, n, rot=-90.0):
    return [(cx + r * math.cos(math.radians(rot + i * 360.0 / n)), cy + r * math.sin(math.radians(rot + i * 360.0 / n)))
            for i in range(max(3, n))]


# ─────────────────────────── クリップの登場 / 退場アニメ（CapCut 風）───────────────────────────
CLIP_ANIMS = [("", "なし"), ("fade", "フェード"), ("zoom_in", "ズーム（小→等倍）"), ("zoom_out", "ズーム（大→等倍）"),
              ("slide_left", "左から"), ("slide_right", "右から"), ("slide_up", "下から"), ("slide_down", "上から"),
              ("spin", "回転"), ("pop", "ポップ（弾む）")]


def clip_anim(clip, cf: float, fps: float, W: int, H: int):
    """(拡大の倍率, 回転の足し算, x, y, 不透明度の倍率)。登場は先頭、退場は最後の clip_anim_len フレーム（既定 0.4 秒）。"""
    n = int(clip.props.get("clip_anim_len") or 0) or max(1, int(round(fps * 0.4)))
    n = max(1, min(n, clip.duration // 2 or 1))
    ds, dr, dx, dy, da = 1.0, 0.0, 0.0, 0.0, 1.0
    for kind, k, d in ((clip.props.get("clip_in"), (cf - clip.start + 1) / n, 1),
                       (clip.props.get("clip_out"), (clip.end - cf) / n, -1)):
        if not kind or k >= 1.0:
            continue
        k = max(0.0, k)
        e = 1 - (1 - k) ** 3
        if kind == "fade":
            da *= e
        elif kind == "zoom_in":
            ds *= 0.3 + 0.7 * e
            da *= e
        elif kind == "zoom_out":
            ds *= 1.0 + 0.8 * (1 - e)
            da *= e
        elif kind in ("slide_left", "slide_right"):
            dx += (-1 if kind == "slide_left" else 1) * (1 - e) * W
        elif kind in ("slide_up", "slide_down"):
            dy += (1 if kind == "slide_up" else -1) * (1 - e) * H
        elif kind == "spin":
            dr += (1 - e) * 180 * d
            ds *= 0.5 + 0.5 * e
            da *= e
        elif kind == "pop":
            t = k - 1
            ds *= max(0.05, 1 + 2.70158 * t ** 3 + 1.70158 * t ** 2)
    return ds, dr, dx, dy, da
