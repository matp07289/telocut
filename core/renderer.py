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
]


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
                            fill: tuple, letter_spacing: int) -> None:
    """カーニング(字間)を考慮して1文字ずつテキストを描画。"""
    x, y = pos
    for char in text:
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

    # --- シャドウ描画 ---
    if settings.shadow_enabled:
        shadow_layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(shadow_layer)
        sc = _hex_to_rgba(settings.shadow_color, settings.shadow_opacity)
        sx, sy = settings.shadow_offset_x, settings.shadow_offset_y
        for i, (line, (lw, _)) in enumerate(zip(lines, line_sizes)):
            lx = _line_x(base_x, max_line_w, lw, settings.text_align)
            ly = base_y + i * line_height
            draw_text_with_spacing(sdraw, (lx + sx, ly + sy), line, font, sc, ls)
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
        bdraw.rectangle(rect, fill=bg_color)
        img = Image.alpha_composite(img, bg_layer)
        draw = ImageDraw.Draw(img)

    # --- テキスト描画 (縁取り + 本文) ---
    stroke_color = _hex_to_rgba(settings.stroke_color, 255)
    sw = settings.stroke_width if settings.stroke_enabled else 0

    for i, (line, (lw, _)) in enumerate(zip(lines, line_sizes)):
        lx = _line_x(base_x, max_line_w, lw, settings.text_align)
        ly = base_y + i * line_height

        # 縁取り: 各文字を各方向にオフセットして描画
        if sw > 0:
            for dx in range(-sw, sw + 1):
                for dy in range(-sw, sw + 1):
                    if dx == 0 and dy == 0:
                        continue
                    if dx * dx + dy * dy <= sw * sw + sw:
                        draw_text_with_spacing(draw, (lx + dx, ly + dy), line, font, stroke_color, ls)

        # 本文
        draw_text_with_spacing(draw, (lx, ly), line, font, font_color, ls)

    return img


def _line_x(base_x: int, max_w: int, line_w: int, align: str) -> int:
    """行のX座標をアライメントに基づいて計算。"""
    if align == "left":
        return base_x
    elif align == "right":
        return base_x + max_w - line_w
    else:
        return base_x + (max_w - line_w) // 2
