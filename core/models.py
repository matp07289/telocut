"""テロップのデータモデル定義"""
from __future__ import annotations
import copy
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class DesignSettings:
    """テロップのデザイン設定"""
    # フォント設定
    font_path: str = ""
    font_size: int = 60
    font_color: str = "#FFFFFF"
    bold: bool = False
    italic: bool = False
    # カーニング（字間）
    letter_spacing: int = 0        # ピクセル単位の追加字間 (正=広げる, 負=詰める)
    line_spacing: float = 1.3      # 行間倍率

    # 縁取り（ストローク）
    stroke_enabled: bool = True
    stroke_color: str = "#000000"
    stroke_width: int = 3

    # ドロップシャドウ
    shadow_enabled: bool = False
    shadow_color: str = "#000000"
    shadow_opacity: int = 180      # 0-255
    shadow_offset_x: int = 4
    shadow_offset_y: int = 4

    # 背景
    bg_enabled: bool = False
    bg_color: str = "#000000"
    bg_opacity: int = 160          # 0-255
    bg_padding: int = 15           # ピクセル単位の内側余白

    # 位置・アライメント
    position_h: str = "center"    # left / center / right
    position_v: str = "bottom"    # top / middle / bottom
    offset_x: int = 0
    offset_y: int = 80             # エッジからのオフセット(px)
    text_align: str = "center"    # left / center / right

    # キャンバス設定
    canvas_width: int = 1920
    canvas_height: int = 1080
    frame_rate: float = 29.97

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DesignSettings":
        valid_keys = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    def copy(self) -> "DesignSettings":
        return copy.copy(self)


@dataclass
class TelopEntry:
    """テロップエントリ (1行分)"""
    id: int
    start_time: str    # "HH:MM:SS:FF" または "HH:MM:SS.mmm"
    end_time: str
    text: str
    enabled: bool = True
    note: str = ""
    speaker: str = ""        # 話者名（話者別色分け用）
    track: int = 0           # 配置ビデオトラック（0=既定を使用）
    color: str = ""          # Resolve クリップカラー（""=既定）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TelopEntry":
        """未知キーを無視して生成（後方互換）。"""
        import inspect
        keys = set(inspect.signature(cls).parameters)
        return cls(**{k: v for k, v in d.items() if k in keys})
