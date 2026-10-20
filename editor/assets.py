"""アプリアイコン（PIL で描画して .ico を生成）。"""
from __future__ import annotations

import sys
from pathlib import Path

ICON_NAME = "telocut.ico"


def draw_icon(size: int = 256):
    from PIL import Image, ImageDraw
    s = size
    k = s / 256
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    # 背景: 紫のグラデーションの角丸
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        t = y / max(1, s - 1)
        c = (int(110 - 60 * t), int(92 - 55 * t), int(240 - 120 * t), 255)
        gd.line([(0, y), (s, y)], fill=c)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([int(8 * k), int(8 * k), s - int(8 * k), s - int(8 * k)],
                                           radius=int(52 * k), fill=255)
    im.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(im)
    # フィルムの帯（下部）
    y0, y1 = int(168 * k), int(212 * k)
    d.rectangle([int(8 * k), y0, s - int(8 * k), y1], fill=(16, 14, 30, 235))
    hole_w, hole_h = int(14 * k), int(9 * k)
    for x in range(int(22 * k), s - int(22 * k), int(28 * k)):
        d.rounded_rectangle([x, y0 + int(5 * k), x + hole_w, y0 + int(5 * k) + hole_h],
                            radius=max(1, int(2 * k)), fill=(230, 230, 240, 220))
        d.rounded_rectangle([x, y1 - int(5 * k) - hole_h, x + hole_w, y1 - int(5 * k)],
                            radius=max(1, int(2 * k)), fill=(230, 230, 240, 220))
    # 黄（スプライス）と赤（上書き）のクリップ
    d.rectangle([int(30 * k), y0 + int(17 * k), int(122 * k), y1 - int(17 * k)], fill=(229, 192, 42, 255))
    d.rectangle([int(130 * k), y0 + int(17 * k), int(226 * k), y1 - int(17 * k)], fill=(216, 70, 60, 255))
    # 大きな T
    white = (255, 255, 255, 255)
    d.rounded_rectangle([int(54 * k), int(40 * k), int(202 * k), int(76 * k)], radius=int(8 * k), fill=white)
    d.rounded_rectangle([int(110 * k), int(40 * k), int(146 * k), int(160 * k)], radius=int(8 * k), fill=white)
    # 再生ヘッド
    px = int(178 * k)
    d.polygon([(px - int(11 * k), int(96 * k)), (px + int(11 * k), int(96 * k)), (px, int(112 * k))],
              fill=(79, 179, 255, 255))
    d.rectangle([px - max(1, int(2 * k)), int(110 * k), px + max(1, int(2 * k)), int(222 * k)],
                fill=(79, 179, 255, 255))
    return im


def make_ico(path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    big = draw_icon(256)
    big.save(str(path), format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                              (64, 64), (128, 128), (256, 256)])
    return path


def icon_path() -> Path:
    if getattr(sys, "frozen", False):
        p = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / ICON_NAME
        if p.exists():
            return p
    p = Path(__file__).with_name(ICON_NAME)
    if not p.exists():
        try:
            make_ico(p)
        except Exception:
            pass
    return p


def app_icon():
    from PyQt6.QtGui import QIcon
    p = icon_path()
    return QIcon(str(p)) if p.exists() else QIcon()


if __name__ == "__main__":
    out = make_ico(Path(__file__).with_name(ICON_NAME))
    draw_icon(256).save(str(out.with_suffix(".png")))
    print(out)
