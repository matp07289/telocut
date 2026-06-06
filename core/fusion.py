"""
DaVinci Resolve / Fusion マクロ (.setting) 生成
  立ち絵 + 口パクを Fusion マクロ化し、Resolve 側でパラメータ調整できる。
    - 口形状スライダー (0=閉 1=あ 2=い 3=う 4=え 5=お 6=ん)
    - 位置 / サイズ / 不透明度 を公開コントロール化
    - VoiceVox 音素タイムラインがあれば口形状にキーフレームを焼き込み
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

# 口形状キー → マクロ内インデックス
SHAPE_INDEX = {"closed": 0, "a": 1, "i": 2, "u": 3, "e": 4, "o": 5, "N": 6}
INDEX_SHAPE = {v: k for k, v in SHAPE_INDEX.items()}
SHAPE_ORDER = ["closed", "a", "i", "u", "e", "o", "N"]


def _lua_path(p: str) -> str:
    """Windows パスを Fusion 用にエスケープ。"""
    return str(p).replace("\\", "\\\\")


def _timeline_to_keys(timeline: list[str], fps: float) -> list[tuple[int, int]]:
    """
    フレーム単位の口形状リスト → (frame, shape_index) のキーフレーム列。
    形状が変化したフレームのみキーにする（ステップ補間前提）。
    """
    keys: list[tuple[int, int]] = []
    prev = None
    for f, shape in enumerate(timeline):
        idx = SHAPE_INDEX.get(shape, 0)
        if idx != prev:
            keys.append((f, idx))
            prev = idx
    if not keys:
        keys = [(0, 0)]
    return keys


def generate_lipsync_macro(
    out_path: str,
    base_path: str,
    mouth_paths: dict,                 # {shape_key: png_path}
    timeline: Optional[list] = None,   # フレーム単位の口形状（焼き込み用）
    fps: float = 30.0,
    macro_name: str = "DavinTelop_LipSync",
) -> None:
    """
    立ち絵 + 口パクの Fusion マクロ (.setting) を書き出す。

    Resolve への導入:
      生成した .setting を
        %APPDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Fusion\\Macros\\
      に置く → Fusion ページで右クリック > Macro から追加できる。
      Inspector で「口形状 / 位置 / サイズ / 不透明度」を調整可能。
    """
    lines: list[str] = []
    A = lines.append

    # 口ローダーを持つ形状（指定されたものだけ）
    avail = [s for s in SHAPE_ORDER if s != "closed" and mouth_paths.get(s)]
    has_closed = bool(mouth_paths.get("closed"))

    # キーフレーム（口形状）
    if timeline:
        keys = _timeline_to_keys(list(timeline), fps)
    else:
        keys = None

    A("{")
    A("\tTools = ordered() {")
    A(f"\t\t{macro_name} = GroupOperator {{")
    A("\t\t\tCtrlWZoom = false,")
    A("\t\t\tInputs = ordered() {")
    # 公開: 口形状
    A('\t\t\t\tMouthShape = InstanceInput {')
    A('\t\t\t\t\tSourceOp = "MouthCtrl",')
    A('\t\t\t\t\tSource = "Mouth",')
    A('\t\t\t\t\tName = "口形状 (0閉 1あ 2い 3う 4え 5お 6ん)",')
    A("\t\t\t\t\tMinScale = 0, MaxScale = 6, Default = 0,")
    A("\t\t\t\t},")
    # 公開: 位置 / サイズ / 不透明度
    A('\t\t\t\tCharaCenter = InstanceInput { SourceOp = "CharaXF", Source = "Center", Name = "位置", },')
    A('\t\t\t\tCharaSize = InstanceInput { SourceOp = "CharaXF", Source = "Size", Name = "サイズ", Default = 1.0, },')
    A('\t\t\t\tCharaAngle = InstanceInput { SourceOp = "CharaXF", Source = "Angle", Name = "回転", Default = 0.0, },')
    A('\t\t\t\tCharaOpacity = InstanceInput { SourceOp = "CharaXF", Source = "Blend", Name = "不透明度", Default = 1.0, },')
    A("\t\t\t},")
    A("\t\t\tOutputs = ordered() {")
    A('\t\t\t\tOutput = InstanceOutput { SourceOp = "CharaXF", Source = "Output", },')
    A("\t\t\t},")
    A("\t\t\tViewInfo = GroupInfo { Pos = { 0, 0 } },")
    A("\t\t\tTools = ordered() {")

    # ── 口形状コントロール (ユーザーコントロール Mouth を持つ Background) ──
    A("\t\t\t\tMouthCtrl = Background {")
    A("\t\t\t\t\tNameSet = true,")
    A("\t\t\t\t\tUserControls = ordered() {")
    A("\t\t\t\t\t\tMouth = {")
    A('\t\t\t\t\t\t\tLINKID_DataType = "Number",')
    A('\t\t\t\t\t\t\tINPID_InputControl = "SliderControl",')
    A("\t\t\t\t\t\t\tINP_Integer = true,")
    A("\t\t\t\t\t\t\tINP_MinScale = 0, INP_MaxScale = 6, INP_Default = 0,")
    A('\t\t\t\t\t\t\tLINKS_Name = "Mouth",')
    A("\t\t\t\t\t\t},")
    A("\t\t\t\t\t},")
    A("\t\t\t\t\tInputs = {")
    A("\t\t\t\t\t\tWidth = Input { Value = 16, },")
    A("\t\t\t\t\t\tHeight = Input { Value = 16, },")
    if keys:
        # 口形状にキーフレームを焼き込み
        A('\t\t\t\t\t\tMouth = Input { SourceOp = "MouthSpline", Source = "Value", },')
    A("\t\t\t\t\t},")
    A("\t\t\t\t\tViewInfo = OperatorInfo { Pos = { -110, 0 } },")
    A("\t\t\t\t},")

    if keys:
        A("\t\t\t\tMouthSpline = BezierSpline {")
        A('\t\t\t\t\tSplineColor = { Red = 255, Green = 128, Blue = 0 },')
        A("\t\t\t\t\tKeyFrames = {")
        for frame, idx in keys:
            # ステップ補間（口は瞬時に切替）
            A(f"\t\t\t\t\t\t[{frame}] = {{ {idx}, Flags = {{ StepIn = true, StepOut = true }} }},")
        A("\t\t\t\t\t},")
        A("\t\t\t\t},")

    # ── ベース立ち絵 ローダー ──
    A("\t\t\t\tBaseLoader = Loader {")
    A("\t\t\t\t\tClips = {")
    A("\t\t\t\t\t\tClip {")
    A(f'\t\t\t\t\t\t\tFilename = "{_lua_path(base_path)}",')
    A('\t\t\t\t\t\t\tFormatID = "PNGFormat",')
    A("\t\t\t\t\t\t\tLength = 1,")
    A("\t\t\t\t\t\t\tTrimIn = 0,")
    A("\t\t\t\t\t\t},")
    A("\t\t\t\t\t},")
    A("\t\t\t\t\tViewInfo = OperatorInfo { Pos = { 0, 0 } },")
    A("\t\t\t\t},")

    # ── 口ローダー群 ──
    prev_node = "BaseLoader"
    y = 0
    for s in avail:
        node = f"Mouth_{s}"
        A(f"\t\t\t\t{node} = Loader {{")
        A("\t\t\t\t\tClips = {")
        A("\t\t\t\t\t\tClip {")
        A(f'\t\t\t\t\t\t\tFilename = "{_lua_path(mouth_paths[s])}",')
        A('\t\t\t\t\t\t\tFormatID = "PNGFormat",')
        A("\t\t\t\t\t\t\tLength = 1, TrimIn = 0,")
        A("\t\t\t\t\t\t},")
        A("\t\t\t\t\t},")
        A(f"\t\t\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 0, {(SHAPE_INDEX[s]) * 33}}} }},")
        A("\t\t\t\t},")

    # ── 口を Merge でベースに重ね、Blend を口形状で切替 ──
    for s in avail:
        node  = f"Merge_{s}"
        idx   = SHAPE_INDEX[s]
        A(f"\t\t\t\t{node} = Merge {{")
        A("\t\t\t\t\tInputs = {")
        A(f'\t\t\t\t\t\tBackground = Input {{ SourceOp = "{prev_node}", Source = "Output", }},')
        A(f'\t\t\t\t\t\tForeground = Input {{ SourceOp = "Mouth_{s}", Source = "Output", }},')
        # 口形状が一致するときだけ表示
        A(f'\t\t\t\t\t\tBlend = Input {{ Expression = "(MouthCtrl.Mouth == {idx}) and 1 or 0", }},')
        A("\t\t\t\t\t},")
        A(f"\t\t\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 165, {idx * 33}}} }},")
        A("\t\t\t\t},")
        prev_node = node

    # ── 最終トランスフォーム（位置/サイズ/回転/不透明度を公開） ──
    A("\t\t\t\tCharaXF = Transform {")
    A("\t\t\t\t\tInputs = {")
    A(f'\t\t\t\t\t\tInput = Input {{ SourceOp = "{prev_node}", Source = "Output", }},')
    A("\t\t\t\t\t},")
    A("\t\t\t\t\tViewInfo = OperatorInfo { Pos = { 360, 0 } },")
    A("\t\t\t\t},")

    A("\t\t\t},")   # Tools
    A("\t\t},")     # GroupOperator
    A("\t},")       # Tools
    A(f'\tActiveTool = "{macro_name}",')
    A("}")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def resolve_macros_dir() -> Optional[str]:
    """Resolve の Fusion Macros フォルダ（存在すれば）を返す。"""
    import os
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Macros"
    return str(p)
