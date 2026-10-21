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

    # 装飾（テロップ専用機のような見た目。既定はすべてオフ = 今までと同じ絵）
    grad_enabled: bool = False     # 文字のグラデーション（文字色 → grad_color）
    grad_color: str = "#FFD84A"
    grad_angle: int = 0            # 0 = 上→下 / 90 = 左→右
    stroke2_enabled: bool = False  # 二重縁（縁取りの外側にもう 1 本）
    stroke2_color: str = "#FFFFFF"
    stroke2_width: int = 6
    glow_enabled: bool = False     # 光彩（文字のまわりをぼかして光らせる）
    glow_color: str = "#66FFCC"
    glow_size: int = 12
    bg_shape: str = "rect"         # 背景の形: rect 四角 / round 角丸 / band 横いっぱいの帯
    scale_x: int = 100             # 横の拡大率（%）
    scale_y: int = 100             # 縦の拡大率（%）
    rotation: float = 0.0          # 回転（度・時計回り）
    skew: float = 0.0              # せん断（度・右に傾く）
    vertical: bool = False         # 縦書き

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DesignSettings":
        valid_keys = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in valid_keys}
        return cls(**filtered)

    def copy(self) -> "DesignSettings":
        return copy.copy(self)

    def with_overrides(self, overrides: Optional[dict]) -> "DesignSettings":
        """テロップ個別スタイル (部分的な設定辞書) を上書きしたコピーを返す。"""
        if not overrides:
            return self
        d = self.copy()
        for k, v in overrides.items():
            if k not in self.__dataclass_fields__ or v is None or v == "":
                continue
            cur = getattr(d, k)
            try:
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, int):
                    v = int(v)
                elif isinstance(cur, float):
                    v = float(v)
            except (TypeError, ValueError):
                continue
            setattr(d, k, v)
        return d


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
    style: dict = field(default_factory=dict)   # 個別スタイル (DesignSettings の部分上書き)
    uid: str = ""            # TeloCut のテキストクリップ ID（リアルタイム連携で同じテロップを対応づける）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TelopEntry":
        """未知キーを無視して生成（後方互換）。"""
        import inspect
        keys = set(inspect.signature(cls).parameters)
        return cls(**{k: v for k, v in d.items() if k in keys})
