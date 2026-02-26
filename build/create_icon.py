#!/usr/bin/env python3
"""
DavinTelop アイコン生成スクリプト
Usage:
    python3 build/create_icon.py        # 両方生成
    python3 build/create_icon.py mac    # .icns のみ
    python3 build/create_icon.py win    # .ico のみ
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow が必要です: pip install Pillow")
    sys.exit(1)

# プロジェクトルート (build/ の親)
PROJECT_ROOT = Path(__file__).parent.parent
BUILD_DIR = PROJECT_ROOT / "build"


def draw_master_icon(size: int) -> Image.Image:
    """
    DavinTelop マスターアイコンを描画する (RGBA)。
    - ネイビー背景 + 角丸
    - "D" レターフォーム (ポリゴン)
    - テロップバー (下部の白い横線)
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # --- 背景 (角丸四角形) ---
    bg_color = (18, 32, 74, 255)  # ネイビー
    radius = size // 8
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=bg_color)

    # --- "D" レターフォーム ---
    # 文字のバウンディングボックス
    pad = size * 0.18
    left   = pad
    top    = pad
    right  = size - pad
    bottom = size - pad * 2.2  # テロップバーのスペースを確保

    cx = (left + right) / 2
    cy = (top + bottom) / 2
    w  = right - left
    h  = bottom - top

    # ポリゴンで "D" を描く
    stem_w = w * 0.22   # 縦棒の幅
    arc_r  = h * 0.5    # アーチ半径

    # アーチ部分 (楕円) → 右半分のみマスクで見せる
    arc_box = [left + stem_w, top, left + stem_w + arc_r * 2, top + h]
    draw.ellipse(arc_box, fill=(255, 255, 255, 255))
    # 左側をネイビーで塗り直して "D" の形にする
    draw.rectangle([left, top, left + stem_w + arc_r, top + h], fill=bg_color)

    # 縦棒
    draw.rectangle(
        [left, top, left + stem_w, top + h],
        fill=(255, 255, 255, 255),
    )

    # --- テロップバー (下部) ---
    bar_h      = size * 0.07
    bar_margin = size * 0.10
    bar_top    = size - bar_margin - bar_h
    bar_radius = bar_h / 2
    draw.rounded_rectangle(
        [bar_margin, bar_top, size - bar_margin, bar_top + bar_h],
        radius=bar_radius,
        fill=(100, 180, 255, 220),  # 淡い青
    )

    return img


def build_icns() -> Path:
    """
    macOS 用 .icns を生成する。
    iconutil コマンドが必要 (macOS のみ)。
    """
    iconset_dir = BUILD_DIR / "DavinTelop.iconset"
    iconset_dir.mkdir(exist_ok=True)

    # iconutil が要求するサイズ一覧
    sizes = [
        (16,   "icon_16x16"),
        (32,   "icon_16x16@2x"),
        (32,   "icon_32x32"),
        (64,   "icon_32x32@2x"),
        (128,  "icon_128x128"),
        (256,  "icon_128x128@2x"),
        (256,  "icon_256x256"),
        (512,  "icon_256x256@2x"),
        (512,  "icon_512x512"),
        (1024, "icon_512x512@2x"),
    ]

    master = draw_master_icon(1024)

    for px, name in sizes:
        resized = master.resize((px, px), Image.LANCZOS)
        resized.save(iconset_dir / f"{name}.png")
        print(f"  [icns] wrote {name}.png ({px}x{px})")

    icns_path = PROJECT_ROOT / "DavinTelop.icns"
    result = subprocess.run(
        ["iconutil", "--convert", "icns", str(iconset_dir), "--output", str(icns_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: iconutil failed:\n{result.stderr}")
        sys.exit(1)

    shutil.rmtree(iconset_dir)
    print(f"  [icns] {icns_path}")
    return icns_path


def build_ico() -> Path:
    """
    Windows 用 .ico を生成する (Pillow のみで完結)。
    """
    ico_sizes = [16, 32, 48, 64, 128, 256]
    master = draw_master_icon(256)

    frames = []
    for s in ico_sizes:
        frames.append(master.resize((s, s), Image.LANCZOS).convert("RGBA"))

    ico_path = PROJECT_ROOT / "DavinTelop.ico"
    frames[0].save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
        append_images=frames[1:],
    )
    print(f"  [ico]  {ico_path}")
    return ico_path


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "both"

    print("=== DavinTelop アイコン生成 ===")

    if mode in ("mac", "both"):
        if shutil.which("iconutil"):
            build_icns()
        else:
            print("  [icns] SKIP: iconutil が見つかりません (macOS のみ対応)")

    if mode in ("win", "both"):
        build_ico()

    print("完了")


if __name__ == "__main__":
    main()
