"""Pillowを使ったテロップ画像レンダリング"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

from .models import DesignSettings


# フォントフォールバック候補 (macOS / Windows 共通)
_FONT_FALLBACK_CANDIDATES = [
    # Windows 日本語フォント
    "C:/Windows/Fonts/meiryo.ttc",
    "C:/Windows/Fonts/meiryob.ttc",
    "C:/Windows/Fonts/YuGothM.ttc",
    "C:/Windows/Fonts/YuGothB.ttc",
    "C:/Windows/Fonts/YuGothL.ttc",
    "C:/Windows/Fonts/msgothic.ttc",
    "C:/Windows/Fonts/msmincho.ttc",
    "C:/Windows/Fonts/yugothic.ttf",
    "C:/Windows/Fonts/yumin.ttf",
    # macOS 日本語フォント
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc",
    "/System/Library/Fonts/ヒラギノ明朝 ProN W3.ttc",
    "/System/Library/Fonts/ヒラギノ明朝 ProN W6.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux（Rocky / Ubuntu の Noto CJK）
    "/usr/share/fonts/google-noto-sans-cjk-ttc/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]

_FONT_INDEX: Optional[dict] = None


def _find_font_by_name(font_path: str) -> Optional[str]:
    """別の OS で作ったプロジェクトのフォント（例 C:/Windows/Fonts/xxx.ttf）を、
    この PC のフォントフォルダから同じファイル名で探す（Linux のレンダーノード用）。"""
    global _FONT_INDEX
    if _FONT_INDEX is None:
        _FONT_INDEX = {}
        dirs = [os.environ.get("TELOCUT_FONT_DIR", ""), "/usr/share/fonts", "/usr/local/share/fonts",
                str(Path.home() / ".fonts"), str(Path.home() / ".local/share/fonts"),
                "C:/Windows/Fonts", str(Path.home() / "AppData/Local/Microsoft/Windows/Fonts")]
        for d in dirs:
            if d and os.path.isdir(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        _FONT_INDEX.setdefault(f.lower(), os.path.join(root, f))
    name = font_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return _FONT_INDEX.get(name)


def get_system_fonts() -> list[tuple[str, str]]:
    """
    システムフォントを検索して (表示名, パス) のリストを返す。
    Windows / macOS 両対応。日本語フォントを優先表示する。
    """
    import platform
    font_dirs: list[Path] = []

    if platform.system() == "Windows":
        font_dirs = [
            Path("C:/Windows/Fonts"),
            Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
        ]
    else:
        font_dirs = [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library/Fonts",
        ]

    fonts: list[tuple[str, str]] = []
    seen: set[str] = set()

    for font_dir in font_dirs:
        if not font_dir.exists():
            continue
        for ext in ("*.ttf", "*.otf", "*.ttc"):
            for p in sorted(font_dir.glob(ext)):
                if p.stem not in seen:
                    seen.add(p.stem)
                    fonts.append((p.stem, str(p)))

    # 日本語フォントを先頭に
    jp_keywords = [
        "ヒラギノ", "游", "メイリオ", "明朝", "ゴシック",
        "YuGo", "YuMin", "Hiragino", "Meiryo", "Noto", "GenEi", "M PLUS",
        "msgothic", "msmincho", "yugoth", "yumin",
    ]
    jp_fonts = [f for f in fonts if any(k.lower() in f[0].lower() for k in jp_keywords)]
    other_fonts = [f for f in fonts if not any(k.lower() in f[0].lower() for k in jp_keywords)]

    return jp_fonts + other_fonts


def load_font(font_path: str, size: int) -> "ImageFont.FreeTypeFont":
    """フォントをロード。失敗時は日本語フォントへフォールバック。"""
    if not PILLOW_AVAILABLE:
        raise RuntimeError("Pillowがインストールされていません。pip install Pillow を実行してください。")

    # 指定パスを試みる
    if font_path and Path(font_path).exists():
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass

    # 同じファイル名のフォントを探す（別の PC・OS で作ったプロジェクト）
    if font_path:
        alt = _find_font_by_name(font_path)
        if alt:
            try:
                return ImageFont.truetype(alt, size)
            except Exception:
                pass

    # フォールバック: システムの日本語フォント
    for path in _FONT_FALLBACK_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # 最終フォールバック
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple:
    """16進数カラー文字列をRGBAタプルに変換。"""
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join(c * 2 for c in hex_color)
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return (r, g, b, alpha)


def measure_text_with_spacing(draw: "ImageDraw.ImageDraw", text: str,
                               font: "ImageFont.FreeTypeFont",
                               letter_spacing: int) -> tuple[int, int]:
    """
    カーニング(字間)を考慮してテキストのサイズを計算。
    Returns (width, height)
    """
    if not text:
        return (0, font.size)

    total_width = 0
    max_height = 0
    for i, char in enumerate(text):
        bbox = draw.textbbox((0, 0), char, font=font)
        char_w = bbox[2] - bbox[0]
        char_h = bbox[3] - bbox[1]
        total_width += char_w
        if i < len(text) - 1:
            total_width += letter_spacing
        max_height = max(max_height, char_h)

    return (total_width, max_height)


def draw_text_with_spacing(draw: "ImageDraw.ImageDraw", pos: tuple[int, int],
                            text: str, font: "ImageFont.FreeTypeFont",
                            fill: tuple, letter_spacing: int, stroke: int = 0) -> None:
    """カーニング(字間)を考慮して1文字ずつテキストを描画。stroke > 0 なら同じ色で太らせる（二重縁・光彩用）。"""
    x, y = pos
    for char in text:
        if stroke > 0:
            draw.text((x, y), char, font=font, fill=fill, stroke_width=stroke, stroke_fill=fill)
        else:
            draw.text((x, y), char, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), char, font=font)
        char_w = bbox[2] - bbox[0]
        x += char_w + letter_spacing


def wrap_text(text: str, font: "ImageFont.FreeTypeFont",
              max_width: int, letter_spacing: int,
              dummy_draw: "ImageDraw.ImageDraw") -> list[str]:
    """
    テキストをmax_widthに収まるよう折り返す。
    日本語の任意の位置での折り返しに対応。
    """
    result_lines: list[str] = []

    for raw_line in text.split('\n'):
        if not raw_line:
            result_lines.append('')
            continue

        line_w, _ = measure_text_with_spacing(dummy_draw, raw_line, font, letter_spacing)
        if line_w <= max_width:
            result_lines.append(raw_line)
            continue

        # 1文字ずつ追加しながら折り返し
        current = ''
        for char in raw_line:
            test = current + char
            w, _ = measure_text_with_spacing(dummy_draw, test, font, letter_spacing)
            if w > max_width and current:
                result_lines.append(current)
                current = char
            else:
                current = test
        if current:
            result_lines.append(current)

    return result_lines


def render_telop_image(text: str, settings: DesignSettings) -> "Image.Image":
    """
    テロップを透過PNGとしてレンダリングしてPIL Imageを返す。
    カーニング、縁取り、背景、シャドウに対応。
    """
    if not PILLOW_AVAILABLE:
        raise RuntimeError("Pillowがインストールされていません。")

    W = settings.canvas_width
    H = settings.canvas_height

    # メインレイヤー (透過RGBA)
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font = load_font(settings.font_path, settings.font_size)
    font_color = _hex_to_rgba(settings.font_color, 255)
    ls = settings.letter_spacing
    vertical = bool(getattr(settings, "vertical", False))

    if vertical:
        cols, max_line_w, block_h = _vertical_layout(text, font, settings, draw, H)
    else:
        # テキスト折り返し
        text_area_w = int(W * 0.9)
        lines = wrap_text(text, font, text_area_w, ls, draw)

        # 各行のサイズを計算
        line_height = int(settings.font_size * settings.line_spacing)
        line_sizes: list[tuple[int, int]] = []
        for line in lines:
            w, h = measure_text_with_spacing(draw, line, font, ls)
            line_sizes.append((w, h))

        max_line_w = max((s[0] for s in line_sizes), default=0)
        block_h = line_height * len(lines)

    # ベース位置を計算
    pad = settings.bg_padding
    if settings.position_h == "left":
        base_x = pad + settings.offset_x
    elif settings.position_h == "right":
        base_x = W - max_line_w - pad + settings.offset_x
    else:
        base_x = (W - max_line_w) // 2 + settings.offset_x

    if settings.position_v == "top":
        base_y = pad + settings.offset_y
    elif settings.position_v == "middle":
        base_y = (H - block_h) // 2 + settings.offset_y
    else:
        base_y = H - block_h - settings.offset_y

    # 描く文字の並び: (x, y, 文字列, 字間)
    if vertical:
        runs = _vertical_runs(cols, font, settings, draw, base_x, base_y, max_line_w, block_h)
    else:
        runs = []
        for i, (line, (lw, _)) in enumerate(zip(lines, line_sizes)):
            runs.append((_line_x(base_x, max_line_w, lw, settings.text_align), base_y + i * line_height, line, ls))

    def paint(d, fill, dx=0, dy=0, stroke=0):
        for x, y, t, l in runs:
            draw_text_with_spacing(d, (x + dx, y + dy), t, font, fill, l, stroke)

    stroke_color = _hex_to_rgba(settings.stroke_color, 255)
    sw = settings.stroke_width if settings.stroke_enabled else 0
    sw2 = int(getattr(settings, "stroke2_width", 0)) if getattr(settings, "stroke2_enabled", False) else 0

    # --- 光彩（いちばん下）---
    if getattr(settings, "glow_enabled", False) and int(getattr(settings, "glow_size", 0)) > 0:
        gs = int(settings.glow_size)
        glow = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        paint(ImageDraw.Draw(glow), _hex_to_rgba(settings.glow_color, 255), stroke=sw + sw2 + max(1, gs // 4))
        glow = glow.filter(ImageFilter.GaussianBlur(gs))
        img = Image.alpha_composite(img, glow)
        img = Image.alpha_composite(img, glow)    # 2 回重ねて強めに光らせる
        draw = ImageDraw.Draw(img)

    # --- シャドウ描画 ---
    if settings.shadow_enabled:
        shadow_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow_layer)
        sc = _hex_to_rgba(settings.shadow_color, settings.shadow_opacity)
        sx, sy = settings.shadow_offset_x, settings.shadow_offset_y
        paint(sdraw, sc, sx, sy, stroke=sw2)
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(2))
        img = Image.alpha_composite(img, shadow_layer)
        draw = ImageDraw.Draw(img)

    # --- 背景描画 ---
    if settings.bg_enabled and settings.bg_opacity > 0:
        bg_color = _hex_to_rgba(settings.bg_color, settings.bg_opacity)
        bg_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(bg_layer)
        rect = (
            base_x - pad,
            base_y - pad,
            base_x + max_line_w + pad,
            base_y + block_h + pad,
        )
        shape = getattr(settings, "bg_shape", "rect")
        if shape == "band":
            bdraw.rectangle((0, rect[1], W, rect[3]), fill=bg_color)
        elif shape == "round":
            bdraw.rounded_rectangle(rect, radius=max(4, min(rect[3] - rect[1], rect[2] - rect[0]) // 3), fill=bg_color)
        else:
            bdraw.rectangle(rect, fill=bg_color)
        img = Image.alpha_composite(img, bg_layer)
        draw = ImageDraw.Draw(img)

    # --- 二重縁（外側の縁）---
    if sw2 > 0:
        paint(draw, _hex_to_rgba(settings.stroke2_color, 255), stroke=sw + sw2)

    # --- テキスト描画 (縁取り + 本文) ---
    # 縁取り: 各文字を各方向にオフセットして描画
    if sw > 0:
        for dx in range(-sw, sw + 1):
            for dy in range(-sw, sw + 1):
                if dx == 0 and dy == 0:
                    continue
                if dx * dx + dy * dy <= sw * sw + sw:
                    paint(draw, stroke_color, dx, dy)

    # 本文（グラデーションなら文字の形で切り抜いて塗る）
    if getattr(settings, "grad_enabled", False):
        mask = Image.new('L', (W, H), 0)
        paint(ImageDraw.Draw(mask), 255)
        box = mask.getbbox()
        if box:
            grad = _gradient(box[2] - box[0], box[3] - box[1], settings.font_color, settings.grad_color,
                             int(getattr(settings, "grad_angle", 0)))
            layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            layer.paste(grad, box[:2])
            layer.putalpha(mask)
            img = Image.alpha_composite(img, layer)
    else:
        paint(draw, font_color)

    # --- 拡大縮小・回転・せん断（テロップのまん中を中心に）---
    sxp, syp = int(getattr(settings, "scale_x", 100)), int(getattr(settings, "scale_y", 100))
    rot, skw = float(getattr(settings, "rotation", 0.0)), float(getattr(settings, "skew", 0.0))
    if (sxp, syp) != (100, 100) or rot or skw:
        img = _transform(img, sxp / 100, syp / 100, rot, skw,
                         base_x + max_line_w / 2, base_y + block_h / 2)

    return img


# 縦書きで向きを変える文字（縦書き用の字形）
_VERT = {"ー": "丨", "－": "丨", "—": "丨", "〜": "≀", "～": "≀", "…": "︙", "‥": "︰", "、": "︑", "。": "︒",
         "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄", "（": "︵", "）": "︶", "(": "︵", ")": "︶",
         "【": "︻", "】": "︼", "〔": "︹", "〕": "︺", "［": "﹇", "］": "﹈", "｛": "︷", "｝": "︸",
         "＝": "‖", "→": "↓", "←": "↑"}


def _vertical_layout(text, font, s, draw, H):
    """縦書きの列（右から）と、ブロックの幅・高さ。"""
    fs = s.font_size
    step = max(1, fs + s.letter_spacing)
    per = max(1, int(H * 0.9) // step)
    cols: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            cols.append("")
            continue
        for i in range(0, len(raw), per):
            cols.append(raw[i:i + per])
    col_w = int(fs * s.line_spacing)
    bw = col_w * (len(cols) - 1) + fs if cols else 0
    bh = max((len(c) for c in cols), default=0) * step - (step - fs)
    return cols, bw, max(bh, fs)


def _vertical_runs(cols, font, s, draw, base_x, base_y, bw, bh):
    fs = s.font_size
    step = max(1, fs + s.letter_spacing)
    col_w = int(fs * s.line_spacing)
    rb = draw.textbbox((0, 0), "国", font=font)
    ref_c = (rb[1] + rb[3]) / 2
    runs = []
    for i, col in enumerate(cols):
        cx = base_x + bw - fs - i * col_w
        colh = len(col) * step - (step - fs) if col else 0
        oy = {"left": 0, "right": bh - colh}.get(s.text_align, (bh - colh) // 2)
        for j, ch in enumerate(col):
            ch2 = _VERT.get(ch, ch)
            bb = draw.textbbox((0, 0), ch2, font=font)
            cw = bb[2] - bb[0]
            y = base_y + oy + j * step
            if ch2 != ch and ch not in "、。":
                # 縦書き用の字形（括弧・のばす棒など）はフォントによって上下にずれるので、ふつうの字（国）と同じ高さのまん中に置く
                y += int(ref_c - (bb[1] + bb[3]) / 2)
            runs.append((cx + (fs - cw) // 2 - bb[0], y, ch2, 0))
    return runs


def _gradient(w, h, c1, c2, angle):
    """c1 → c2 のグラデーション（angle 0 = 上→下 / 90 = 左→右）。"""
    import math
    import numpy as np
    a = math.radians(angle)
    dx, dy = math.sin(a), math.cos(a)
    ys, xs = np.mgrid[0:max(1, h), 0:max(1, w)].astype("float32")
    t = (xs - w / 2) * dx + (ys - h / 2) * dy
    span = abs(w * dx) / 2 + abs(h * dy) / 2 or 1
    t = ((t / span) + 1) / 2
    t = t.clip(0, 1)[..., None]
    p = np.array(_hex_to_rgba(c1, 255)[:3], "float32")
    q = np.array(_hex_to_rgba(c2, 255)[:3], "float32")
    rgb = (p * (1 - t) + q * t).astype("uint8")
    return Image.fromarray(rgb, "RGB").convert("RGBA")


def _transform(img, sx, sy, rot_deg, skew_deg, cx, cy):
    """(cx, cy) を中心に 拡大縮小 → せん断 → 回転（時計回り）。"""
    import math
    r, k = math.radians(rot_deg), math.tan(math.radians(max(-80.0, min(80.0, skew_deg))))
    cs, sn = math.cos(r), math.sin(r)
    # 前へ: p' = C + R · K · S · (p - C)
    a, b = sx, -k * sy
    c, d = 0.0, sy
    m00, m01 = cs * a - sn * c, cs * b - sn * d
    m10, m11 = sn * a + cs * c, sn * b + cs * d
    det = m00 * m11 - m01 * m10
    if abs(det) < 1e-6:
        return Image.new('RGBA', img.size, (0, 0, 0, 0))
    i00, i01, i10, i11 = m11 / det, -m01 / det, -m10 / det, m00 / det
    coeffs = (i00, i01, cx - i00 * cx - i01 * cy, i10, i11, cy - i10 * cx - i11 * cy)
    # 縁がにじまないよう、色に透明度を掛けてから変形する
    pre = img.convert("RGBa")
    out = pre.transform(img.size, Image.AFFINE, coeffs, resample=Image.BICUBIC)
    return out.convert("RGBA")


def _line_x(base_x: int, max_w: int, line_w: int, align: str) -> int:
    """行のX座標をアライメントに基づいて計算。"""
    if align == "left":
        return base_x
    elif align == "right":
        return base_x + max_w - line_w
    else:
        return base_x + (max_w - line_w) // 2
