"""フレーム合成・音声ミックス（リアルタイム再生向けに最適化）。

高速化の要点:
- 最下層が「全面・不透明・加工なし」の映像なら、デコード結果をそのまま土台にする（合成なし）
- 上のレイヤー（テロップ等）は、実際に絵がある範囲（バウンディングボックス）だけを numpy で合成
- テロップは描画結果をクロップしてキャッシュ（解像度ごと）
- frame_signature() でフレーム内容のハッシュを作り、レンダーキャッシュのキーにする
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image, ImageEnhance, ImageOps, ImageDraw

from .media import DecoderPool, SAMPLE_RATE, CHANNELS, has_proxy
from .model import Clip, Project, Sequence

_VISUAL = ("video", "image", "text", "color", "character", "shape", "adjust", "nest")
_NEST_DEPTH = [0]           # 入れ子シーケンスの深さ（自分を入れ子にしても止まるように）
_text_src: "OrderedDict[str, tuple]" = OrderedDict()     # key -> (rgba ndarray, x, y) フル解像度
_text_scaled: "OrderedDict[tuple, tuple]" = OrderedDict()  # (key, scale) -> (rgba, x, y)
_offline: dict = {}


# ─────────────────────────────────────────────
#  テロップ
# ─────────────────────────────────────────────

def design_for(project: Project, clip: Clip):
    """テキストクリップに使う DesignSettings（共通デザイン + クリップ個別スタイル）。"""
    from core.models import DesignSettings
    seq = project.sequence
    d = DesignSettings.from_dict(project.design or {})
    d.canvas_width, d.canvas_height, d.frame_rate = seq.width, seq.height, seq.fps
    return d.with_overrides(clip.prop("style") or {})


def _text_key(project: Project, clip: Clip) -> str:
    d = design_for(project, clip)
    return hashlib.md5((clip.prop("text", "") + json.dumps(d.to_dict(), sort_keys=True))
                       .encode("utf-8")).hexdigest()


def text_patch(project: Project, clip: Clip, scale: float):
    """テロップの絵がある部分だけを (RGBA配列, x, y) で返す（キャッシュ付き）。"""
    key = _text_key(project, clip)
    sk = (key, round(scale, 4))
    hit = _text_scaled.get(sk)
    if hit is not None:
        _text_scaled.move_to_end(sk)
        return hit
    src = _text_src.get(key)
    if src is None:
        from core.renderer import render_telop_image
        try:
            im = render_telop_image(clip.prop("text", ""), design_for(project, clip))
        except Exception:
            im = None
        bbox = im.getbbox() if im is not None else None
        if bbox is None:
            src = (None, 0, 0)
        else:
            src = (np.asarray(im.crop(bbox)), bbox[0], bbox[1])
        _text_src[key] = src
        while len(_text_src) > 128:
            _text_src.popitem(last=False)
    arr, x, y = src
    if arr is not None and scale != 1.0:
        h, w = arr.shape[:2]
        im = Image.fromarray(arr, "RGBA").resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                                                 Image.BILINEAR)
        arr, x, y = np.asarray(im), int(round(x * scale)), int(round(y * scale))
    out = (arr, x, y)
    _text_scaled[sk] = out
    while len(_text_scaled) > 256:
        _text_scaled.popitem(last=False)
    return out


# ─────────────────────────────────────────────
#  レイヤー情報
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  キャラクター（立ち絵・口パク・まばたき）
# ─────────────────────────────────────────────

_char_img: "OrderedDict[str, Image.Image]" = OrderedDict()


def _load_char_image(path: str) -> Optional[Image.Image]:
    im = _char_img.get(path)
    if im is None:
        try:
            im = Image.open(path).convert("RGBA")
        except Exception:
            return None
        _char_img[path] = im
        while len(_char_img) > 48:
            _char_img.popitem(last=False)
    return im


def mouth_level(project: Project, seq: Sequence, clip: Clip, cf: int, pool: DecoderPool) -> int:
    """キャラクターの口の開き（0=閉 / 1=半開き / 2=開き）。指定した音声トラックの音量で決める。"""
    ti = int(clip.prop("audio_track", 0) or 0)
    if ti < 0 or ti >= len(seq.audio_tracks):
        return 0
    t = seq.audio_tracks[ti]
    a = t.clip_at(cf)
    if a is None or a.kind != "audio" or t.muted:
        return 0
    m = project.media_by_id(a.media_id)
    if m is None:
        return 0
    spf = SAMPLE_RATE / seq.fps
    pos = int((a.src_in + (cf - a.start) * float(a.prop("speed", 1.0) or 1.0)) * spf)
    buf = pool.audio(m.path).read(pos, int(spf))
    if buf.size == 0:
        return 0
    rms = float(np.sqrt((buf.astype(np.float64) ** 2).mean()))
    db = 20 * np.log10(max(rms, 1e-6)) + float(a.prop("volume_db", 0.0))
    return 2 if db > -24 else (1 if db > -36 else 0)


def mouth_vowel(project: Project, seq: Sequence, clip: Clip, cf: int) -> Optional[str]:
    """音声合成のときに保存した母音タイムライン（<wav>.mouth.json）から口の形を返す。無ければ None。"""
    ti = int(clip.prop("audio_track", 0) or 0)
    if ti < 0 or ti >= len(seq.audio_tracks):
        return None
    t = seq.audio_tracks[ti]
    a = t.clip_at(cf)
    if a is None or a.kind != "audio" or t.muted:
        return None
    m = project.media_by_id(a.media_id)
    if m is None:
        return None
    from .voiceengine import mouth_shape_at
    sec = (a.src_in + (cf - a.start) * float(a.prop("speed", 1.0) or 1.0)) / seq.fps
    return mouth_shape_at(m.path, sec)


def _blink(clip: Clip, cf: int, fps: float) -> bool:
    """約 3〜5 秒ごとに 3 フレーム目を閉じる（間隔はクリップごとに少しずつ違う）。"""
    if not clip.prop("blink", True):
        return False
    try:
        jitter = (int(clip.id[:4], 16) % 200) / 100
    except ValueError:
        jitter = 1.0
    period = max(1, int(fps * (3.0 + jitter)))
    return (cf - clip.start) % period < 3


def character_state(project: Project, seq: Sequence, clip: Clip, cf: int, pool: DecoderPool):
    """(画像パス, 揺れの y オフセット px)"""
    ch = project.character(clip.prop("char_id", ""))
    if not ch:
        return None, 0
    imgs = ch.get("images", {})
    vowel = mouth_vowel(project, seq, clip, cf) if clip.prop("vowel_sync", True) else None
    if vowel is not None:
        # 母音に合わせた口（あいうえおん）。その口の画像が無ければ 開き / 半開き / 閉じ で代用
        lv = 0 if vowel in ("closed", "N") else (2 if vowel in ("a", "o") else 1)
        key = vowel if imgs.get(vowel) else {0: "closed", 1: "half", 2: "open"}[lv]
    else:
        lv = mouth_level(project, seq, clip, cf, pool)
        key = {0: "closed", 1: "half", 2: "open"}[lv]
    if key == "half" and not imgs.get("half"):
        key = "open"
    if lv == 0 and _blink(clip, cf, seq.fps) and imgs.get("blink"):
        key = "blink"
    dy = 0
    if clip.prop("bounce", True) and lv > 0:
        dy = -int(round(abs(np.sin((cf - clip.start) * 0.9)) * 6))
    return imgs.get(key) or imgs.get("closed"), dy


# ─────────────────────────────────────────────
#  図形（帯・プログレスバー・カウントダウン・エンドカード枠）
# ─────────────────────────────────────────────

def _shape_image(seq: Sequence, clip: Clip, cf: int, W: int, H: int) -> Image.Image:
    from PIL import ImageColor
    shape = clip.prop("shape", "band")
    col = clip.prop("color", "#000000")
    try:
        rgb = ImageColor.getrgb(col)[:3]
    except ValueError:
        rgb = (0, 0, 0)
    fo = int(255 * float(clip.prop("fill_opacity", 0.6)))
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    x, y, w, h = [float(v) for v in (clip.prop("rect") or [0, 78, 100, 22])]
    box = [W * x / 100, H * y / 100, W * (x + w) / 100, H * (y + h) / 100]
    rad = int(float(clip.prop("radius", 0)) * min(W, H) / 100)
    sw = int(round(float(clip.prop("stroke_width", 0) or 0) * H / 1080))
    try:
        srgb = ImageColor.getrgb(clip.prop("stroke_color") or "#FFFFFF")[:3] + (255,)
    except ValueError:
        srgb = (255, 255, 255, 255)
    fill = rgb + (fo,) if fo > 0 else None
    out_ = srgb if sw > 0 else None
    cx, cy, rr = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, min(box[2] - box[0], box[3] - box[1]) / 2
    from . import motion as MO
    if shape in ("band", "rect"):
        d.rounded_rectangle(box, radius=rad, fill=fill, outline=out_, width=max(1, sw))
    elif shape == "ellipse":
        d.ellipse(box, fill=fill, outline=out_, width=max(1, sw))
    elif shape == "star":
        n = int(clip.prop("sides", 5) or 5)
        d.polygon(MO.star_points(cx, cy, rr, rr * float(clip.prop("inner", 0.45) or 0.45), n), fill=fill, outline=out_,
                  width=max(1, sw))
    elif shape == "polygon":
        d.polygon(MO.polygon_points(cx, cy, rr, int(clip.prop("sides", 6) or 6)), fill=fill, outline=out_, width=max(1, sw))
    elif shape in ("line", "arrow"):
        lw = max(1, sw or int(H / 200))
        d.line([box[0], box[1], box[2], box[3]], fill=srgb if sw else rgb + (255,), width=lw)
        if shape == "arrow":
            ang = math.atan2(box[3] - box[1], box[2] - box[0])
            hl = lw * 4
            for da in (2.6, -2.6):
                d.line([box[2], box[3], box[2] + hl * math.cos(ang + da), box[3] + hl * math.sin(ang + da)],
                       fill=srgb if sw else rgb + (255,), width=lw)
    elif shape == "progress":
        prog = max(0.0, min(1.0, cf / max(1, seq.duration)))
        d.rectangle(box, fill=(40, 40, 40, int(fo * 0.6)))
        d.rectangle([box[0], box[1], box[0] + (box[2] - box[0]) * prog, box[3]], fill=rgb + (255,))
    elif shape == "countdown":
        remain = max(0, int(np.ceil((clip.end - cf) / seq.fps)))
        from PIL import ImageFont
        try:
            f = ImageFont.truetype("arialbd.ttf", int(H * h / 100))
        except Exception:
            f = None
        txt = str(remain)
        tw = d.textlength(txt, font=f) if f else len(txt) * 8
        d.text(((box[0] + box[2] - tw) / 2, box[1]), txt, font=f, fill=rgb + (255,),
               stroke_width=max(2, H // 200), stroke_fill=(0, 0, 0, 255))
    elif shape == "endcard":
        # YouTube の終了画面（動画 2 つ + チャンネル登録）の目安枠
        lw = max(2, W // 400)
        for bx in ((0.08, 0.22, 0.38, 0.62), (0.56, 0.22, 0.92, 0.62)):
            d.rounded_rectangle([W * bx[0], H * bx[1], W * bx[2], H * bx[3]], radius=W // 100,
                                outline=rgb + (255,), width=lw, fill=(0, 0, 0, int(fo * 0.5)))
        r = H * 0.11
        cx, cy = W * 0.5, H * 0.8
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=rgb + (255,), width=lw, fill=(0, 0, 0, int(fo * 0.5)))
    return im


def _noise(seed: int, frame: int) -> float:
    v = (frame * 1103515245 + seed * 12345) & 0x7FFFFFFF
    return (v % 2000) / 1000.0 - 1.0


def _shake_offset(clip: Clip, cf: int, scale: float):
    for fx in clip.prop("effects") or []:
        if fx.get("name") == "shake":
            amp = float(fx.get("value", 10)) * scale
            return _noise(1, cf) * amp, _noise(7, cf) * amp
    return 0.0, 0.0


class Layer:
    """合成する1枚: clip を、タイミング計算用フレーム cf・追加の不透明度 amul で描く。"""
    __slots__ = ("clip", "cf", "amul")

    def __init__(self, clip, cf, amul=1.0):
        self.clip, self.cf, self.amul = clip, cf, amul


def _track_layers(t, frame: int) -> List[Layer]:
    # クロスディゾルブ（右クリップの頭に設定。編集点を中心に前後へ）
    for x in t.clips:
        d = int(x.props.get("dissolve", 0) or 0)
        if d <= 0 or not x.enabled:
            continue
        e = x.start
        a0 = e - d // 2
        if a0 <= frame < a0 + d:
            prev = next((c for c in t.clips if c.end == e and c.enabled), None)
            a = (frame - a0 + 0.5) / d
            out = []
            if prev is not None and prev.kind in _VISUAL:
                out.append(Layer(prev, frame, 1.0))
            if x.kind in _VISUAL:
                out.append(Layer(x, frame, a))
            return out
    c = t.clip_at(frame)
    if c is not None and c.enabled and c.kind in _VISUAL:
        return [Layer(c, frame, 1.0)]
    return []


def active_layers(seq: Sequence, frame: int) -> List[Layer]:
    """frame で見えているレイヤー（下から順）。ディゾルブ中は前後のクリップの両方。"""
    out = []
    for t in seq.video_tracks:
        if not t.muted:
            out += _track_layers(t, frame)
    return out


def _cf_in(clip: Clip, cf: int) -> int:
    return max(clip.start, min(clip.end - 1, cf))


_RAMP: "OrderedDict[tuple, np.ndarray]" = OrderedDict()


def ramp_table(clip: Clip) -> Optional[np.ndarray]:
    """スピードランプ（props["speed_keys"] = [[相対フレーム, 速度, イージング], ...]）の
    「クリップ先頭から k フレーム目までに進む素材のフレーム数」の表（長さ duration+1）。無ければ None。"""
    keys = clip.props.get("speed_keys") if clip.props else None
    if not keys:
        return None
    k = (clip.id, clip.duration, json.dumps(keys))
    t = _RAMP.get(k)
    if t is None:
        sp = np.array([max(0.0, float(key_value(keys, i))) for i in range(clip.duration)], np.float64)
        t = np.concatenate([[0.0], np.cumsum(sp)])
        _RAMP[k] = t
        while len(_RAMP) > 256:
            _RAMP.popitem(last=False)
    return t


def src_frame(clip: Clip, cf: int) -> float:
    """タイムラインのフレーム cf に対応する素材のフレーム（速度・逆再生・フリーズ・スピードランプ対応）。"""
    rt = ramp_table(clip)
    if rt is not None:
        rel = max(0.0, min(float(clip.duration), float(cf - clip.start)))
        return float(clip.src_in + np.interp(rel, np.arange(len(rt)), rt))
    sp = float(clip.prop("speed", 1.0) or 0.0)
    rel = cf - clip.start
    if sp == 0:
        return float(clip.src_in)
    if sp < 0:
        return max(0.0, clip.src_in + (clip.duration - 1 - rel) * -sp)
    return max(0.0, clip.src_in + rel * sp)


EASES = {
    "linear": lambda t: t,
    "ease_in": lambda t: t * t * t,
    "ease_out": lambda t: 1 - (1 - t) ** 3,
    "ease_in_out": lambda t: 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    "back": lambda t: 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2,      # 少し行き過ぎて戻る
    "bounce": lambda t: 1 - abs(math.cos(t * math.pi * 2.5)) * (1 - t) ** 2,
}


def ease(name: str, t: float) -> float:
    return EASES.get(name or "linear", EASES["linear"])(max(0.0, min(1.0, t)))


def key_value(keys, rel: float):
    """キーフレーム [[クリップ先頭からのフレーム, 値, イージング], ...] の rel での値。"""
    ks = sorted(keys, key=lambda k: k[0])
    if not ks:
        return None
    if rel <= ks[0][0]:
        return ks[0][1]
    for (f0, v0, *e0), (f1, v1, *_e1) in zip(ks, ks[1:]):
        if f0 <= rel <= f1:
            t = (rel - f0) / max(1e-6, f1 - f0)
            ez = e0[0] if e0 else "linear"
            if ez == "hold":
                return v0
            if isinstance(v0, (int, float)) and isinstance(v1, (int, float)):
                return float(v0) + (float(v1) - float(v0)) * ease(ez, t)
            return v0 if t < 1 else v1
    return ks[-1][1]


def prop_at(clip: Clip, key: str, cf: int):
    """キーフレーム > アニメーション（開始→終了）> 固定値 の順で、そのフレームの値。"""
    keys = (clip.prop("keys") or {}).get(key)
    if keys:
        return key_value(keys, _cf_in(clip, cf) - clip.start)
    anim = clip.prop("anim") or {}
    v = anim.get(key)
    if isinstance(v, (list, tuple)) and len(v) == 2:
        dur = int(anim.get("_len") or 0) or clip.duration       # _len: アニメーションの長さ（フレーム、0=クリップ全体）
        t = ease(anim.get("_ease", "linear"), (_cf_in(clip, cf) - clip.start) / max(1, dur - 1))
        return float(v[0]) + (float(v[1]) - float(v[0])) * t
    return clip.prop(key)


def clip_alpha(clip: Clip, frame: int) -> float:
    """不透明度（アニメーション・フェードイン/アウト込み）。"""
    f = _cf_in(clip, frame)
    a = prop_at(clip, "opacity", f)
    a = float(a if a is not None else 1.0)
    if clip.props.get("expr"):
        from .motion import apply_expr
        a = float(apply_expr(clip, "opacity", a, f))
    off = f - clip.start
    fi, fo = int(clip.prop("fade_in", 0) or 0), int(clip.prop("fade_out", 0) or 0)
    if fi > 0 and off < fi:
        a *= (off + 1) / (fi + 1)
    if fo > 0 and clip.end - f <= fo:
        a *= (clip.end - f) / (fo + 1)
    return max(0.0, min(1.0, a))


def _plain(clip: Clip) -> bool:
    crop = clip.prop("crop") or [0, 0, 0, 0]
    return (float(clip.prop("scale", 1.0)) == 1.0 and not float(clip.prop("rotation", 0.0))
            and not float(clip.prop("pos_x", 0.0)) and not float(clip.prop("pos_y", 0.0))
            and not clip.prop("effects") and not any(crop) and not clip.prop("chroma")
            and not clip.prop("anim") and not clip.prop("keys") and (clip.prop("blend") or "normal") == "normal"
            and not (clip.prop("fx_mode") == "nodes" and clip.prop("fx_graph")) and clip.kind != "adjust")


_SIG_POOL = DecoderPool()


def frame_signature(seq: Sequence, frame: int, project: Project, scale: float, use_proxy: bool) -> str:
    """フレームの見た目を決める情報のハッシュ（レンダーキャッシュのキー）。"""
    from . import motion as MO
    MO.FPS[0] = seq.fps
    W_sig, H_sig = int(seq.width * scale), int(seq.height * scale)
    parts = [round(scale, 4), seq.width, seq.height]
    for L in active_layers(seq, frame):
        c = L.clip
        item = [c.kind, round(clip_alpha(c, L.cf) * L.amul, 4)]
        if c.kind == "video":
            m = project.media_by_id(c.media_id)
            item += [m.path if m else "", m.file_size if m else 0, getattr(m, "mtime", 0) if m else 0,
                     round(src_frame(c, L.cf), 2),
                     bool(use_proxy and m and has_proxy(m.path))]
        elif c.kind == "image":
            m = project.media_by_id(c.media_id)
            item += [m.path if m else "", m.file_size if m else 0, getattr(m, "mtime", 0) if m else 0]
        elif c.kind == "text":
            item.append(_text_key(project, c))
        elif c.kind == "color":
            item.append(c.prop("color", "#000000"))
        elif c.kind == "nest":
            nested = project.sequence_by_id(c.props.get("seq_id", ""))
            if nested is not None and nested is not seq and _NEST_DEPTH[0] < 4:
                _NEST_DEPTH[0] += 1
                try:
                    item.append(frame_signature(nested, int(round(src_frame(c, L.cf))), project, scale, use_proxy))
                finally:
                    _NEST_DEPTH[0] -= 1
        elif c.kind == "character":
            path, dy = character_state(project, seq, c, L.cf, _SIG_POOL)
            item += [path, dy, c.prop("flip", False)]
        elif c.kind == "shape":
            item += [c.prop("shape"), c.prop("color"), c.prop("rect"), c.prop("fill_opacity"), c.prop("radius")]
            if c.prop("shape") == "progress":
                item.append(round(L.cf / max(1, seq.duration), 4))
            elif c.prop("shape") == "countdown":
                item.append(int(np.ceil((c.end - L.cf) / seq.fps)))
        if c.kind == "text" and (c.prop("text_anim") or c.prop("karaoke") or c.prop("text_anim_out")):
            item += [c.prop("text_anim"), c.prop("karaoke"), c.prop("text_anim_out"), L.cf - c.start, c.end - L.cf]
        if any(fx.get("name") == "shake" for fx in (c.prop("effects") or [])):
            item.append(L.cf)
        if c.prop("effects"):
            from .fxlib import is_animated
            if is_animated(c.prop("effects")):
                item.append(("fx_t", L.cf - c.start))
        item.append([prop_at(c, k, L.cf) for k in ("scale", "rotation", "pos_x", "pos_y", "opacity")])
        if c.props.get("clip_in") or c.props.get("clip_out"):
            item.append(("ca", [round(v, 3) for v in MO.clip_anim(c, L.cf, seq.fps, W_sig, H_sig)]))
        if MO.is_dynamic(seq, c):
            item.append(("xf", [round(v, 3) for v in MO.transform(seq, c, L.cf, scale)]))
            if c.prop("motion_blur"):
                item.append(("mb", [round(v, 3) for t_ in (-0.25, 0.25)
                                    for v in MO.transform(seq, c, L.cf + t_, scale)], c.prop("shutter"), c.prop("mb_samples")))
        if c.prop("mask"):
            item.append(("mask", c.prop("mask")))
        item.append({k: c.props[k] for k in ("effects", "crop", "chroma", "blend", "fx_mode", "fx_graph", "keys")
                     if k in c.props})
        if c.prop("fx_mode") == "nodes" and c.prop("fx_graph"):
            from .nodes import graph_animated
            if graph_animated(c.prop("fx_graph")):
                item.append(("node_t", L.cf - c.start))
        parts.append(item)
    return hashlib.md5(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def complexity(seq: Sequence, frame: int, project: Project) -> int:
    """0=単純（リアルタイム可）/ 1=合成あり（たぶんリアルタイム可）/ 2=重い（レンダリング推奨）。"""
    layers = active_layers(seq, frame)
    if not layers:
        return 0
    heavy = len(layers) >= 3
    mid = len(layers) == 2
    for L in layers:
        c = L.clip
        if c.prop("effects") or float(c.prop("rotation", 0.0)) or c.prop("chroma") or c.prop("fx_graph") \
                or c.kind == "adjust":
            heavy = True
        if not _plain(c) or clip_alpha(c, L.cf) < 1.0 or L.amul < 1.0:
            mid = True
        if c.kind == "video":
            m = project.media_by_id(c.media_id)
            if m and (m.width or 0) > 2560 and not has_proxy(m.path):
                heavy = True
    return 2 if heavy else (1 if mid else 0)


# ─────────────────────────────────────────────
#  合成
# ─────────────────────────────────────────────

def _fit(w, h, W, H):
    if w <= 0 or h <= 0:
        return W, H
    r = min(W / w, H / h)
    return max(2, int(w * r)) & ~1, max(2, int(h * r)) & ~1


def offline_frame(W: int, H: int) -> np.ndarray:
    """素材が見つからない時の赤い画面（Avid の Media Offline 風）。"""
    arr = _offline.get((W, H))
    if arr is None:
        im = Image.new("RGB", (W, H), (150, 20, 20))
        d = ImageDraw.Draw(im)
        txt = "MEDIA OFFLINE"
        try:
            from PIL import ImageFont
            f = ImageFont.truetype("arial.ttf", max(12, H // 12))
        except Exception:
            f = None
        tw = d.textlength(txt, font=f) if f else len(txt) * 6
        d.text(((W - tw) / 2, H / 2 - H // 24), txt, fill=(255, 255, 255), font=f)
        arr = np.asarray(im)
        _offline[(W, H)] = arr
    return arr


BLEND_MODES = [("normal", "通常"), ("add", "加算"), ("screen", "スクリーン"), ("multiply", "乗算"),
               ("overlay", "オーバーレイ"), ("softlight", "ソフトライト"), ("lighten", "比較（明）"),
               ("darken", "比較（暗）"), ("difference", "差の絶対値")]


def blend_rgb(d: np.ndarray, s: np.ndarray, mode: str) -> np.ndarray:
    """d（下）と s（上）を 0〜1 の float で受け取り、ブレンド結果を返す。"""
    if mode == "add":
        return np.minimum(d + s, 1.0)
    if mode == "screen":
        return 1 - (1 - d) * (1 - s)
    if mode == "multiply":
        return d * s
    if mode == "overlay":
        return np.where(d < 0.5, 2 * d * s, 1 - 2 * (1 - d) * (1 - s))
    if mode == "softlight":
        return (1 - 2 * s) * d * d + 2 * s * d
    if mode == "lighten":
        return np.maximum(d, s)
    if mode == "darken":
        return np.minimum(d, s)
    if mode == "difference":
        return np.abs(d - s)
    return s


def _blend(out: np.ndarray, rgba: np.ndarray, x: int, y: int, mode: str = "normal"):
    """out（HxWx3）の (x,y) に RGBA を合成（はみ出しは切り取り）。mode: ブレンドモード。"""
    H, W = out.shape[:2]
    h, w = rgba.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[..., 3:4].astype(np.uint16)
    if not a.any():
        return
    dst = out[y0:y1, x0:x1]
    if mode and mode != "normal":
        d = dst.astype(np.float32) / 255
        r = blend_rgb(d, src[..., :3].astype(np.float32) / 255, mode)
        af = a.astype(np.float32) / 255
        out[y0:y1, x0:x1] = np.clip((d * (1 - af) + r * af) * 255 + 0.5, 0, 255).astype(np.uint8)
        return
    blended = (dst.astype(np.uint16) * (255 - a) + src[..., :3].astype(np.uint16) * a + 127) // 255
    out[y0:y1, x0:x1] = blended.astype(np.uint8)


def fx_ctx(seq: Sequence, clip: Clip, cf: int, scale: float):
    from .fxlib import Ctx
    try:
        seed = int(clip.id[:6], 16)
    except ValueError:
        seed = abs(hash(clip.id)) % 100000
    return Ctx(rel=cf - clip.start, fps=seq.fps, duration=clip.duration, scale=scale, seed=seed)


def apply_clip_fx(im: Image.Image, clip: Clip, seq: Sequence, cf: int, scale: float) -> Image.Image:
    """クリップのエフェクトを掛ける。管理方法が「ノード」ならノードグラフ、「レイヤー」なら上から順に。"""
    ctx = fx_ctx(seq, clip, cf, scale)
    if clip.prop("fx_mode") == "nodes" and clip.prop("fx_graph"):
        from .nodes import evaluate
        return evaluate(clip.prop("fx_graph"), im, ctx)
    return _apply_effects(im, clip.prop("effects") or [], scale, ctx)


def _apply_effects(im: Image.Image, effects: list, scale: float = 1.0, ctx=None) -> Image.Image:
    from PIL import ImageFilter
    from . import fxlib
    for fx in effects or []:
        name = fx.get("name")
        if fxlib.get(name) is not None:          # プラグイン（組み込み / 追加）のエフェクト
            if ctx is None:
                ctx = fxlib.Ctx(scale=scale)
            im = fxlib.apply(im, fx, ctx)
            continue
        if not fx.get("on", True):
            continue
        a = im.getchannel("A") if im.mode == "RGBA" else None
        rgb = im.convert("RGB")
        try:
            if name == "mono":
                rgb = ImageOps.grayscale(rgb).convert("RGB")
            elif name == "sepia":
                rgb = ImageOps.colorize(ImageOps.grayscale(rgb), "#2b1d0e", "#f3e0c0")
            elif name == "brightness":
                rgb = ImageEnhance.Brightness(rgb).enhance(float(fx.get("value", 1.0)))
            elif name == "contrast":
                rgb = ImageEnhance.Contrast(rgb).enhance(float(fx.get("value", 1.0)))
            elif name == "saturation":
                rgb = ImageEnhance.Color(rgb).enhance(float(fx.get("value", 1.0)))
            elif name == "invert":
                rgb = ImageOps.invert(rgb)
            elif name == "blur":
                r = max(0.1, float(fx.get("value", 1.0)) * 8 * scale)
                rgb = rgb.filter(ImageFilter.GaussianBlur(r))
            elif name == "mosaic":
                bs = max(2, int(float(fx.get("value", 16)) * scale))
                rect = fx.get("rect")
                w0, h0 = rgb.size
                if rect:
                    x, y, w, h = [float(v) / 100 for v in rect]
                    box = (int(w0 * x), int(h0 * y), max(int(w0 * x) + 1, int(w0 * (x + w))),
                           max(int(h0 * y) + 1, int(h0 * (y + h))))
                else:
                    box = (0, 0, w0, h0)
                part = rgb.crop(box)
                small = part.resize((max(1, part.width // bs), max(1, part.height // bs)), Image.BILINEAR)
                rgb.paste(small.resize(part.size, Image.NEAREST), box[:2])
            elif name == "shake":
                pass
            elif name == "sharpen":
                rgb = ImageEnhance.Sharpness(rgb).enhance(1.0 + float(fx.get("value", 1.0)) * 2)
            else:
                continue
        except Exception:
            continue
        im = rgb.convert("RGBA")
        if a is not None:
            im.putalpha(a)
    return im


def _crop(im: Image.Image, crop) -> Image.Image:
    if not crop or not any(crop):
        return im
    l, t, r, b = [max(0.0, min(49.0, float(v))) / 100 for v in crop]
    w, h = im.size
    box = (int(w * l), int(h * t), max(int(w * l) + 1, int(w * (1 - r))), max(int(h * t) + 1, int(h * (1 - b))))
    # 出力位置を保つため、切り取った部分は透明にする
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(im.crop(box), box[:2])
    return out


def _chroma(im: Image.Image, ck: dict) -> Image.Image:
    """クロマキー: 指定色に近い部分を透明にする。"""
    from PIL import ImageColor
    try:
        key = np.array(ImageColor.getrgb(ck.get("color", "#00ff00"))[:3], np.float32)
    except ValueError:
        return im
    tol = float(ck.get("tol", 0.25))
    soft = max(1e-3, float(ck.get("soft", 0.1)))
    arr = np.asarray(im.convert("RGBA")).astype(np.float32)
    dist = np.sqrt(((arr[..., :3] - key) ** 2).sum(axis=2)) / 441.7
    k = np.clip((dist - tol) / soft, 0, 1)
    arr[..., 3] *= k
    # 緑かぶりを少し抑える
    if key[1] > key[0] and key[1] > key[2]:
        g = arr[..., 1]
        lim = np.maximum(arr[..., 0], arr[..., 2])
        arr[..., 1] = np.where(g > lim, lim + (g - lim) * k, g)
    return Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGBA")


def _source_image(project: Project, seq: Sequence, clip: Clip, cf: int,
                  pool: DecoderPool, W: int, H: int):
    """クリップの素材を「出力サイズに収めた」大きさで返す。(PIL RGBA or None, offline)"""
    m = project.media_by_id(clip.media_id) if clip.media_id else None
    if clip.kind == "video":
        if m is None:
            return None, True
        dec = pool.video(m.path)
        if dec is None:
            return None, True
        fw, fh = _fit(m.width or W, m.height or H, W, H)
        if getattr(dec, "has_alpha", False):
            arr = dec.get_array(src_frame(clip, cf) / seq.fps, (fw, fh), alpha=True)
            return (Image.fromarray(arr, "RGBA"), False) if arr is not None else (None, False)
        arr = dec.get_array(src_frame(clip, cf) / seq.fps, (fw, fh))
        if arr is None:
            return None, False
        return Image.fromarray(arr, "RGB").convert("RGBA"), False
    if clip.kind == "character":
        path, dy = character_state(project, seq, clip, cf, pool)
        im = _load_char_image(path) if path else None
        if im is None:
            return None, False
        fw, fh = _fit(im.width, im.height, W, H)
        out = im.resize((max(1, fw), max(1, fh)), Image.BILINEAR)
        if clip.prop("flip", False):
            out = out.transpose(Image.FLIP_LEFT_RIGHT)
        if dy:
            pad = Image.new("RGBA", (out.width, out.height + abs(dy) * 2), (0, 0, 0, 0))
            pad.paste(out, (0, abs(dy) + dy))
            out = pad
        return out, False
    if clip.kind == "shape":
        return _shape_image(seq, clip, cf, W, H), False
    if clip.kind == "nest":
        nested = project.sequence_by_id(clip.props.get("seq_id", ""))
        if nested is None or nested is seq or _NEST_DEPTH[0] >= 4:
            return None, True
        sc = min(W / max(1, nested.width), H / max(1, nested.height))
        _NEST_DEPTH[0] += 1
        try:
            arr = render_frame_np(nested, int(round(src_frame(clip, cf))), project, pool, sc)
        finally:
            _NEST_DEPTH[0] -= 1
        return Image.fromarray(arr, "RGB").convert("RGBA"), False
    if clip.kind == "image":
        if m is None:
            return None, True
        im = pool.image(m.path)
        if im is None:
            return None, True
        fw, fh = _fit(im.width, im.height, W, H)
        return im.resize((fw, fh), Image.BILINEAR), False
    return None, False


# テロップの登場アニメ（pop / slide / type は従来の処理）。退場は text_anim_out に fade / zoom / drop / slide / wipe / blur
TEXT_IN_ANIMS = ("fade", "zoom", "drop", "wipe", "blur")


def _overlay(out: np.ndarray, project: Project, seq: Sequence, L: Layer,
             pool: DecoderPool, W: int, H: int, scale: float):
    clip, cf = L.clip, L.cf
    alpha = clip_alpha(clip, cf) * L.amul
    if alpha <= 0:
        return
    from . import motion as MO
    s, rot, px, py = MO.transform(seq, clip, cf, scale)
    if clip.kind != "text" and (clip.props.get("clip_in") or clip.props.get("clip_out")):
        ds, dr, dx, dy, da = MO.clip_anim(clip, cf, seq.fps, W, H)
        s, rot, px, py, alpha = s * ds, rot + dr, px + dx, py + dy, alpha * da
    if s <= 0 or alpha <= 0:
        return
    effects = clip.prop("effects") or ([1] if clip.prop("fx_mode") == "nodes" and clip.prop("fx_graph") else [])
    bm = clip.prop("blend") or "normal"

    sx, sy = _shake_offset(clip, cf, scale)
    px += sx
    py += sy
    if clip.kind == "text":
        anim = clip.prop("text_anim") or ""
        rel = cf - clip.start
        fps = seq.fps
        tclip = clip
        if anim == "type":
            full = clip.prop("text", "")
            n = min(len(full), int(rel / fps * float(clip.prop("type_speed", 12))) + 1)
            tclip = clip.copy(props={**clip.props, "text": full[:n]})
        arr, x, y = text_patch(project, tclip, scale)
        if arr is None:
            return
        kc = clip.prop("karaoke") or ""
        if kc:
            hi = clip.copy(props={**clip.props, "style": {**(clip.prop("style") or {}), "font_color": kc}})
            harr, hx, hy = text_patch(project, hi, scale)
            prog = max(0.0, min(1.0, (rel + 1) / max(1, clip.duration * 0.9)))
            cut = int(harr.shape[1] * prog) if harr is not None else 0
            if harr is not None and cut > 0:
                arr = arr.copy()
                arr[:, :cut] = harr[:, :cut] if harr.shape == arr.shape else arr[:, :cut]
        k = min(1.0, (rel + 1) / max(1, int(fps * 0.3)))
        if anim == "pop" and k < 1.0:
            ov = 1.70158
            t = k - 1
            s *= max(0.05, 1 + (ov + 1) * t ** 3 + ov * t ** 2) * 1.0
            # 文字の中心を基準に拡大
            h0, w0 = arr.shape[:2]
            cxp, cyp = x + w0 / 2, y + h0 / 2
            im = Image.fromarray(arr, "RGBA").resize((max(1, int(w0 * s)), max(1, int(h0 * s))), Image.BILINEAR)
            a = np.asarray(im).copy()
            if alpha < 1.0:
                a[..., 3] = (a[..., 3].astype(np.float32) * alpha).astype(np.uint8)
            _blend(out, a, int(round(cxp - im.width / 2 + px)), int(round(cyp - im.height / 2 + py)), mode=bm)
            return
        if anim == "slide" and k < 1.0:
            e = 1 - (1 - k) ** 3
            py += (1 - e) * 60 * scale
            alpha *= e
        # 登場（フェード・ズーム・落下・ワイプ・ぼかし）と退場（最後の 0.3 秒）
        zs, wipe, blur = 1.0, 1.0, 0.0
        phases = []
        if anim in TEXT_IN_ANIMS and k < 1.0:
            phases.append((anim, 1 - (1 - k) ** 3, 1))
        anim_out = clip.prop("text_anim_out") or ""
        if anim_out:
            ko = (clip.end - cf) / max(1, int(fps * 0.3))
            if ko < 1.0:
                phases.append((anim_out, 1 - (1 - max(0.0, ko)) ** 3, -1))
        for kind_, e, d in phases:
            if kind_ == "fade":
                alpha *= e
            elif kind_ == "zoom":
                zs *= (1.6 - 0.6 * e) if d > 0 else (1.0 + 0.4 * (1 - e))
                alpha *= e
            elif kind_ == "drop":
                py += -(1 - e) * 80 * scale if d > 0 else (1 - e) * 60 * scale
                alpha *= e
            elif kind_ == "slide":
                py -= (1 - e) * 60 * scale
                alpha *= e
            elif kind_ == "wipe":
                wipe = min(wipe, e)
            elif kind_ == "blur":
                blur = max(blur, (1 - e) * 14 * scale)
                alpha *= e
        if wipe < 1.0 or blur > 0.0:
            im0 = Image.fromarray(arr, "RGBA")
            if blur > 0.0:
                from PIL import ImageFilter
                pad = int(blur * 2) + 1
                big = Image.new("RGBA", (im0.width + pad * 2, im0.height + pad * 2), (0, 0, 0, 0))
                big.paste(im0, (pad, pad))
                im0 = big.filter(ImageFilter.GaussianBlur(blur))
                x, y = x - pad, y - pad
            a0 = np.asarray(im0).copy()
            if wipe < 1.0:
                a0[:, int(a0.shape[1] * wipe):, 3] = 0
            arr = a0
        if zs != 1.0:
            h0, w0 = arr.shape[:2]
            cxp, cyp = x + w0 / 2, y + h0 / 2
            im = Image.fromarray(arr, "RGBA")
            if effects:
                im = apply_clip_fx(im, clip, seq, cf, scale)
            f_ = s * zs
            im = im.resize((max(1, int(im.width * f_)), max(1, int(im.height * f_))), Image.BILINEAR)
            if rot:
                im = im.rotate(-rot, resample=Image.BILINEAR, expand=True)
            a = np.asarray(im).copy()
            if alpha < 1.0:
                a[..., 3] = (a[..., 3].astype(np.float32) * alpha).astype(np.uint8)
            cxp, cyp = W / 2 + (cxp - W / 2) * s, H / 2 + (cyp - H / 2) * s
            _blend(out, a, int(round(cxp - im.width / 2 + px)), int(round(cyp - im.height / 2 + py)), mode=bm)
            return
        if s == 1.0 and not rot and not effects:
            if alpha < 1.0:
                arr = arr.copy()
                arr[..., 3] = (arr[..., 3].astype(np.float32) * alpha).astype(np.uint8)
            _blend(out, arr, int(round(x + px)), int(round(y + py)), mode=bm)
            return
        im = Image.fromarray(arr, "RGBA")
        cx, cy = W / 2, H / 2
        nx, ny = cx + (x - cx) * s, cy + (y - cy) * s
        if effects:
            w_b, h_b = im.size
            im = apply_clip_fx(im, clip, seq, cf, scale)
            # 縁取り・影などで大きくなった分、中心がずれないように戻す
            nx -= (im.width - w_b) / 2 * s
            ny -= (im.height - h_b) / 2 * s
        if s != 1.0:
            im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
        if rot:
            w0, h0 = im.size
            im = im.rotate(-rot, resample=Image.BILINEAR, expand=True)
            nx -= (im.width - w0) / 2
            ny -= (im.height - h0) / 2
        a = np.asarray(im).copy()
        if alpha < 1.0:
            a[..., 3] = (a[..., 3].astype(np.float32) * alpha).astype(np.uint8)
        _blend(out, a, int(round(nx + px)), int(round(ny + py)), mode=bm)
        return

    if clip.kind == "color":
        im = Image.new("RGBA", (W, H), clip.prop("color", "#000000"))
    else:
        im, offline = _source_image(project, seq, clip, cf, pool, W, H)
        if im is None:
            if offline:
                _blend(out, np.dstack([offline_frame(W, H), np.full((H, W), int(255 * alpha), np.uint8)]), 0, 0, mode=bm)
            return
    ck = clip.prop("chroma")
    if ck:
        im = _chroma(im, ck)
    im = _crop(im, clip.prop("crop"))
    if clip.prop("mask"):
        im = MO.apply_mask(im, clip.prop("mask"), scale)
    if effects:
        im = apply_clip_fx(im, clip, seq, cf, scale)
    if clip.prop("motion_blur"):
        if _motion_blur(out, im, seq, clip, cf, scale, W, H, alpha, bm, (s, rot, px, py)):
            return
    a, x, y = _place(im, s, rot, px, py, W, H)
    if alpha < 1.0:
        a = a.copy()
        a[..., 3] = (a[..., 3].astype(np.float32) * alpha).astype(np.uint8)
    _blend(out, a, x, y, mode=bm)


def _place(im: Image.Image, s: float, rot: float, px: float, py: float, W: int, H: int):
    """素材画像を 拡大・回転して、中心 + (px, py) に置いたときの (RGBA 配列, x, y)。"""
    if s != 1.0:
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
    if rot:
        im = im.rotate(-rot, resample=Image.BILINEAR, expand=True)
    return np.asarray(im), int(round((W - im.width) / 2 + px)), int(round((H - im.height) / 2 + py))


def _motion_blur(out, im, seq, clip, cf, scale, W, H, alpha, bm, cur) -> bool:
    """動いている間だけ、シャッターが開いている間の何か所かに置いて平均する（AE のモーションブラー）。"""
    from . import motion as MO
    n = max(2, min(32, int(clip.prop("mb_samples", 8) or 8)))
    sh = max(0.0, min(720.0, float(clip.prop("shutter", 180) or 180))) / 360.0
    ts = [MO.transform(seq, clip, cf + (i / (n - 1) - 0.5) * sh, scale) for i in range(n)]
    if all(abs(t[0] - cur[0]) < 1e-4 and abs(t[1] - cur[1]) < 1e-3 and abs(t[2] - cur[2]) < 0.25
           and abs(t[3] - cur[3]) < 0.25 for t in ts):
        return False                                    # 止まっている
    acc = np.zeros((H, W, 4), np.float32)
    for s_, r_, x_, y_ in ts:
        if s_ <= 0:
            continue
        a, x, y = _place(im, s_, r_, x_, y_, W, H)
        h, w = a.shape[:2]
        x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        sub = a[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
        al = sub[..., 3:4] / 255.0
        acc[y0:y1, x0:x1, :3] += sub[..., :3] * al          # 乗算済みで足す
        acc[y0:y1, x0:x1, 3:4] += al
    acc /= len(ts)
    al = acc[..., 3:4]
    rgb = np.where(al > 1e-6, acc[..., :3] / np.maximum(al, 1e-6), 0)
    layer = np.dstack([np.clip(rgb, 0, 255), np.clip(al * 255 * alpha, 0, 255)]).astype(np.uint8)
    _blend(out, layer, 0, 0, mode=bm)
    return True


def render_frame_np(seq: Sequence, frame: int, project: Project, pool: DecoderPool,
                    scale: float = 1.0) -> np.ndarray:
    """合成フレームを HxWx3 uint8 で返す。"""
    from .motion import FPS
    FPS[0] = seq.fps
    W = max(2, int(seq.width * scale)) & ~1
    H = max(2, int(seq.height * scale)) & ~1
    layers = active_layers(seq, frame)
    out = None
    start = 0
    # 最下層が全面・不透明・加工なしなら、デコード結果をそのまま土台に（高速パス）
    if layers:
        L = layers[0]
        c = L.clip
        if c.props.get("mask") or c.props.get("motion_blur") or c.props.get("parent") or c.props.get("layer3d") \
                or c.props.get("expr") or c.props.get("clip_in") or c.props.get("clip_out") or c.props.get("speed_keys"):
            L = None
    if layers and L is not None:
        m0 = project.media_by_id(c.media_id) if c.kind == "video" and c.media_id else None
        dec0 = pool.video(m0.path) if m0 else None
        if _plain(c) and clip_alpha(c, L.cf) >= 1.0 and L.amul >= 1.0 and not getattr(dec0, "has_alpha", False):
            if c.kind == "color":
                from PIL import ImageColor
                out = np.empty((H, W, 3), np.uint8)
                out[:] = ImageColor.getrgb(c.prop("color", "#000000"))[:3]
                start = 1
            elif c.kind == "video":
                m = project.media_by_id(c.media_id)
                dec = pool.video(m.path) if m else None
                if dec is None:
                    out = offline_frame(W, H).copy()
                    start = 1
                else:
                    fw, fh = _fit(m.width or W, m.height or H, W, H)
                    arr = dec.get_array(src_frame(c, L.cf) / seq.fps, (fw, fh))
                    if arr is not None:
                        if (fw, fh) == (W, H):
                            out = arr if len(layers) == 1 else arr.copy()
                        else:
                            out = np.zeros((H, W, 3), np.uint8)
                            x, y = (W - fw) // 2, (H - fh) // 2
                            out[y:y + fh, x:x + fw] = arr
                        start = 1
    if out is None:
        out = np.zeros((H, W, 3), np.uint8)
    for L in layers[start:]:
        if L.clip.kind == "adjust":
            _adjust(out, seq, L, scale)
        else:
            _overlay(out, project, seq, L, pool, W, H, scale)
    return out


def _adjust(out: np.ndarray, seq: Sequence, L: Layer, scale: float):
    """調整レイヤー: それより下に合成された画面全体にエフェクトを掛ける（不透明度・ブレンドモード付き）。"""
    clip = L.clip
    a = clip_alpha(clip, L.cf) * L.amul
    if a <= 0:
        return
    im = Image.fromarray(out, "RGB").convert("RGBA")
    res = apply_clip_fx(im, clip, seq, L.cf, scale)
    if res.size != im.size:
        res = res.crop(((res.width - im.width) // 2, (res.height - im.height) // 2,
                        (res.width - im.width) // 2 + im.width, (res.height - im.height) // 2 + im.height))
    arr = np.asarray(res).copy()
    arr[..., 3] = (arr[..., 3].astype(np.float32) * a).astype(np.uint8)
    _blend(out, arr, 0, 0, clip.prop("blend") or "normal")


def render_frame(seq: Sequence, frame: int, project: Project, pool: DecoderPool,
                 scale: float = 1.0) -> Image.Image:
    return Image.fromarray(render_frame_np(seq, frame, project, pool, scale), "RGB")


# ─────────────────────────────────────────────
#  音声ミックス
# ─────────────────────────────────────────────

def _pan_gains(p: float):
    p = max(-1.0, min(1.0, p))
    return min(1.0, 1.0 - p), min(1.0, 1.0 + p)


def mix_audio(seq: Sequence, project: Project, pool: DecoderPool,
              start_sample: int, n: int) -> np.ndarray:
    """シーケンスの [start_sample, start_sample+n) をミックスして float32 (n,2) で返す。"""
    out = np.zeros((n, CHANNELS), dtype=np.float32)
    spf = SAMPLE_RATE / seq.fps
    solo = any(t.solo for t in seq.audio_tracks)
    # ダッキング用: ダッキングしないトラックで音が鳴っている区間
    duck_tracks = [t for t in seq.audio_tracks if getattr(t, "duck", False)]
    voice = None
    if duck_tracks:
        voice = np.zeros(n, np.float32)
        a_f = int(start_sample / spf) - int(seq.fps)
        b_f = int((start_sample + n) / spf) + int(seq.fps)
        for t in seq.audio_tracks:
            if getattr(t, "duck", False) or t.muted:
                continue
            for c in t.clips_in(a_f, b_f):
                if not c.enabled or c.kind != "audio":
                    continue
                a = max(0, int(round(c.start * spf)) - start_sample)
                b = min(n, int(round(c.end * spf)) - start_sample)
                if b > a:
                    voice[a:b] = 1.0
    for track in seq.audio_tracks:
        if track.muted or (solo and not track.solo):
            continue
        t_gain = 10 ** (float(getattr(track, "volume_db", 0.0)) / 20.0)
        a_frame = int(start_sample / spf)
        b_frame = int((start_sample + n) / spf) + 1
        for c in track.clips_in(a_frame, b_frame):
            if not c.enabled or c.kind not in ("audio", "nest"):
                continue
            sp = float(c.prop("speed", 1.0) or 0.0)
            if sp == 0:
                continue                   # フリーズフレームは無音
            cs = int(round(c.start * spf))
            ce = int(round(c.end * spf))
            a = max(start_sample, cs)
            b = min(start_sample + n, ce)
            if b <= a:
                continue
            src0 = int(round(c.src_in * spf))
            if c.kind == "nest":
                nested = project.sequence_by_id(c.props.get("seq_id", ""))
                if nested is None or nested is seq or _NEST_DEPTH[0] >= 4:
                    continue
                _NEST_DEPTH[0] += 1
                try:
                    buf = mix_audio(nested, project, pool, src0 + (a - cs), b - a).copy()
                finally:
                    _NEST_DEPTH[0] -= 1
                m = None
            else:
                m = project.media_by_id(c.media_id)
                if m is None:
                    continue
                src = pool.audio(m.path)
            rt = ramp_table(c) if m is not None else None
            if m is None:
                pass                       # 入れ子シーケンス: buf はもうある
            elif rt is not None:
                # スピードランプ: 素材上の位置を表から求めて補間（音程も速度に合わせて変わる）
                xs = (np.arange(a, b, dtype=np.float64) - cs) / spf
                pos = src0 + np.interp(xs, np.arange(len(rt)), rt) * spf
                lo = int(max(0, np.floor(pos.min())))
                hi = int(np.ceil(pos.max())) + 2
                raw = src.read(lo, hi - lo)
                idx = pos - lo
                buf = np.empty((b - a, CHANNELS), np.float32)
                base = np.arange(raw.shape[0])
                for ch in range(CHANNELS):
                    buf[:, ch] = np.interp(idx, base, raw[:, ch])
            elif sp == 1.0:
                buf = src.read(src0 + (a - cs), b - a)
            else:
                # 速度変更: 素材上の位置を計算して補間（逆再生も対応）
                xs = np.arange(a, b, dtype=np.float64) - cs
                if sp > 0:
                    pos = src0 + xs * sp
                else:
                    pos = src0 + (ce - cs - 1 - xs) * -sp
                lo = int(max(0, np.floor(pos.min())))
                hi = int(np.ceil(pos.max())) + 2
                raw = src.read(lo, hi - lo)
                idx = pos - lo
                buf = np.empty((b - a, CHANNELS), np.float32)
                base = np.arange(raw.shape[0])
                for ch in range(CHANNELS):
                    buf[:, ch] = np.interp(idx, base, raw[:, ch])
            gain = 10 ** (float(c.prop("volume_db", 0.0)) / 20.0) * t_gain
            fi = int(c.prop("fade_in", 0) or 0) * spf
            fo = int(c.prop("fade_out", 0) or 0) * spf
            if fi > 0 or fo > 0:
                idx2 = np.arange(a, b, dtype=np.float32)
                env = np.ones(b - a, dtype=np.float32)
                if fi > 0:
                    env *= np.clip((idx2 - cs) / fi, 0, 1)
                if fo > 0:
                    env *= np.clip((ce - idx2) / fo, 0, 1)
                buf *= env[:, None]
            gl, gr = _pan_gains(float(c.prop("pan", 0.0) or 0.0) + float(getattr(track, "pan", 0.0)))
            buf[:, 0] *= gain * gl
            buf[:, 1] *= gain * gr
            if getattr(track, "duck", False) and voice is not None:
                dg = 10 ** (float(getattr(track, "duck_db", -12.0)) / 20.0)
                seg = voice[a - start_sample: b - start_sample]
                # 0.25 秒で滑らかに上げ下げ
                k = max(1, int(SAMPLE_RATE * 0.25))
                env = np.convolve(seg, np.ones(k, np.float32) / k, mode="same") if seg.any() else seg
                buf *= (1.0 - (1.0 - dg) * env)[:, None]
            out[a - start_sample: b - start_sample] += buf
    mg = float(getattr(seq, "master_db", 0.0) or 0.0)
    if mg:
        out *= 10 ** (mg / 20.0)
    return out


def to_int16(buf: np.ndarray) -> bytes:
    return (np.clip(buf, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def levels_db(buf: np.ndarray):
    """(L, R) のピーク値（dBFS）。"""
    if buf.size == 0:
        return -90.0, -90.0
    pk = np.abs(buf).max(axis=0)
    return tuple(float(20 * np.log10(max(1e-5, v))) for v in pk[:2])
