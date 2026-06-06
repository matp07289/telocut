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


def generate_lipsync_comp(
    out_path: str,
    base_path: str,
    mouth_paths: dict,
    timeline: Optional[list] = None,
    fps: float = 30.0,
) -> None:
    """
    立ち絵 + 口パクの Fusion フルコンポジション (.comp) を書き出す。
    MediaOut1 を持つため Resolve の Fusion クリップとして読み込める
    （TimelineItem.ImportFusionComp で自動配置可能）。
    口パクのキーフレームを焼き込む（timeline 指定時）。
    """
    avail = [s for s in SHAPE_ORDER if s != "closed" and mouth_paths.get(s)]
    keys  = _timeline_to_keys(list(timeline), fps) if timeline else None

    L: list[str] = []
    A = L.append
    A("Composition {")
    A("\tCurrentTime = 0,")
    A("\tRenderRange = { 0, 100 },")
    A("\tGlobalRange = { 0, 100 },")
    A("\tTools = ordered() {")

    # 口形状コントロール
    A("\t\tMouthCtrl = Background {")
    A("\t\t\tNameSet = true,")
    A("\t\t\tUserControls = ordered() {")
    A("\t\t\t\tMouth = {")
    A('\t\t\t\t\tLINKID_DataType = "Number",')
    A('\t\t\t\t\tINPID_InputControl = "SliderControl",')
    A("\t\t\t\t\tINP_Integer = true,")
    A("\t\t\t\t\tINP_MinScale = 0, INP_MaxScale = 6, INP_Default = 0,")
    A('\t\t\t\t\tLINKS_Name = "Mouth",')
    A("\t\t\t\t},")
    A("\t\t\t},")
    A("\t\t\tInputs = {")
    A("\t\t\t\tWidth = Input { Value = 16, },")
    A("\t\t\t\tHeight = Input { Value = 16, },")
    if keys:
        A('\t\t\t\tMouth = Input { SourceOp = "MouthSpline", Source = "Value", },')
    A("\t\t\t},")
    A("\t\t\tViewInfo = OperatorInfo { Pos = { -110, 0 } },")
    A("\t\t},")

    if keys:
        A("\t\tMouthSpline = BezierSpline {")
        A("\t\t\tKeyFrames = {")
        for frame, idx in keys:
            A(f"\t\t\t\t[{frame}] = {{ {idx}, Flags = {{ StepIn = true, StepOut = true }} }},")
        A("\t\t\t},")
        A("\t\t},")

    # ベースローダー
    A("\t\tBaseLoader = Loader {")
    A("\t\t\tClips = { Clip {")
    A(f'\t\t\t\tFilename = "{_lua_path(base_path)}",')
    A('\t\t\t\tFormatID = "PNGFormat", Length = 1, TrimIn = 0,')
    A("\t\t\t} },")
    A("\t\t\tViewInfo = OperatorInfo { Pos = { 0, 0 } },")
    A("\t\t},")

    # 口ローダー
    for s in avail:
        A(f"\t\tMouth_{s} = Loader {{")
        A("\t\t\tClips = { Clip {")
        A(f'\t\t\t\tFilename = "{_lua_path(mouth_paths[s])}",')
        A('\t\t\t\tFormatID = "PNGFormat", Length = 1, TrimIn = 0,')
        A("\t\t\t} },")
        A(f"\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 0, {SHAPE_INDEX[s]*33}}} }},")
        A("\t\t},")

    # 口マージ
    prev = "BaseLoader"
    for s in avail:
        idx = SHAPE_INDEX[s]
        node = f"Merge_{s}"
        A(f"\t\t{node} = Merge {{")
        A("\t\t\tInputs = {")
        A(f'\t\t\t\tBackground = Input {{ SourceOp = "{prev}", Source = "Output", }},')
        A(f'\t\t\t\tForeground = Input {{ SourceOp = "Mouth_{s}", Source = "Output", }},')
        A(f'\t\t\t\tBlend = Input {{ Expression = "(MouthCtrl.Mouth == {idx}) and 1 or 0", }},')
        A("\t\t\t},")
        A(f"\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 165, {idx*33}}} }},")
        A("\t\t},")
        prev = node

    # MediaOut（Fusion クリップの出力）
    A("\t\tMediaOut1 = MediaOut {")
    A("\t\t\tInputs = {")
    A(f'\t\t\t\tInput = Input {{ SourceOp = "{prev}", Source = "Output", }},')
    A("\t\t\t},")
    A("\t\t\tViewInfo = OperatorInfo { Pos = { 360, 0 } },")
    A("\t\t},")

    A("\t},")
    A('\tActiveTool = "MediaOut1",')
    A("}")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _sanitize_node_name(name: str, used: set) -> str:
    """Fusion ノード名に使える識別子へ変換（重複回避）。"""
    import re
    base = re.sub(r'[^0-9A-Za-z_]', '_', name).strip('_') or "Layer"
    if base[0].isdigit():
        base = "L_" + base
    node = base
    i = 1
    while node in used:
        node = f"{base}_{i}"
        i += 1
    used.add(node)
    return node


def generate_layered_comp(
    out_path: str,
    layers: list,            # [(layer_name, png_path), ...]  下→上 の順
) -> None:
    """
    PSD の全レイヤーを個別の Loader ノードとして展開した
    Fusion コンポジション(.comp)を書き出す。
    各レイヤーは Merge チェーンで下から重ねられ、MediaOut1 へ出力。
    口の割り当て・キーフレーム等の細かい編集は Fusion 側で行う。
    """
    if not layers:
        raise ValueError("レイヤーがありません。")

    L: list[str] = []
    A = L.append
    A("Composition {")
    A("\tCurrentTime = 0,")
    A("\tRenderRange = { 0, 100 },")
    A("\tGlobalRange = { 0, 100 },")
    A("\tTools = ordered() {")

    used: set = set()
    nodes: list[str] = []   # (node_name, layer_name)
    # ローダー生成
    for i, (lname, lpath) in enumerate(layers):
        node = _sanitize_node_name(lname, used)
        nodes.append(node)
        A(f"\t\t{node} = Loader {{")
        A("\t\t\tClips = { Clip {")
        A(f'\t\t\t\tFilename = "{_lua_path(lpath)}",')
        A('\t\t\t\tFormatID = "PNGFormat", Length = 1, TrimIn = 0,')
        A("\t\t\t} },")
        # 元レイヤー名をコメントとして保持（Fusionで識別しやすく）
        A(f'\t\t\tComments = Input {{ Value = "PSD: {lname}", }},')
        A(f"\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 0, {i*33}}} }},")
        A("\t\t},")

    # Merge チェーン（下から上へ）
    prev = nodes[0]
    for i, node in enumerate(nodes[1:], start=1):
        mname = f"Merge{i}"
        A(f"\t\t{mname} = Merge {{")
        A("\t\t\tInputs = {")
        A(f'\t\t\t\tBackground = Input {{ SourceOp = "{prev}", Source = "Output", }},')
        A(f'\t\t\t\tForeground = Input {{ SourceOp = "{node}", Source = "Output", }},')
        A("\t\t\t},")
        A(f"\t\t\tViewInfo = OperatorInfo {{ Pos = {{ 220, {i*33}}} }},")
        A("\t\t},")
        prev = mname

    # MediaOut
    A("\t\tMediaOut1 = MediaOut {")
    A("\t\t\tInputs = {")
    A(f'\t\t\t\tInput = Input {{ SourceOp = "{prev}", Source = "Output", }},')
    A("\t\t\t},")
    A("\t\t\tViewInfo = OperatorInfo { Pos = { 440, 0 } },")
    A("\t\t},")

    A("\t},")
    A('\tActiveTool = "MediaOut1",')
    A("}")

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def split_psd_all_layers(psd_path: str, out_dir: str) -> list:
    """
    PSD の全レイヤーを個別 PNG として書き出す。
    返値: [(layer_name, png_path), ...]（抽出順 = 概ね下→上）
    """
    from .lipsync import extract_psd_layers, HAS_PSD_TOOLS
    if not HAS_PSD_TOOLS:
        raise RuntimeError("psd-tools が必要です: pip install psd-tools")
    layers = extract_psd_layers(psd_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = []
    used: set = set()
    for name, img in layers.items():
        fn = _sanitize_node_name(name, used)
        p = str(out / f"{fn}.png")
        img.save(p, "PNG")
        result.append((name, p))
    return result


def split_psd_to_pngs(psd_path: str, out_dir: str) -> tuple:
    """
    PSD を分解して基本立ち絵 + 口形状 PNG を out_dir に書き出す。
    返値: (base_png_path | None, {shape_key: png_path})
    """
    from .lipsync import extract_psd_layers, auto_map_psd_layers, HAS_PSD_TOOLS
    if not HAS_PSD_TOOLS:
        raise RuntimeError("psd-tools が必要です: pip install psd-tools")

    layers = extract_psd_layers(psd_path)
    base, mouth = auto_map_psd_layers(layers)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    base_path = None
    if base is not None:
        base_path = str(out / "base.png")
        base.save(base_path, "PNG")

    mouth_paths: dict = {}
    for key, img in mouth.items():
        p = str(out / f"mouth_{key}.png")
        img.save(p, "PNG")
        mouth_paths[key] = p

    return base_path, mouth_paths


def resolve_macros_dir() -> Optional[str]:
    """Resolve の Fusion Macros フォルダ（存在すれば）を返す。"""
    import os
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Macros"
    return str(p)
