"""MMD のモデル（PMX）を Blender（MMD Tools）で立ち絵にする。

- ポーズ: 組み込み（気をつけ・腕を下ろす・首をかしげる・少し斜め）＋ 角度の調整 ＋ VPD（MMD のポーズファイル）
  Blender の画面で自由にポーズを付けて保存した .blend からも作れる（「Blender で開いて調整」）
- 表情: 笑顔・怒り・困り・驚き・照れ・ジト目 など（モデルにあるモーフだけ使う）＋ モーフを直接指定
- 構図: 全身 / 膝上 / 腰から上 / バストアップ / 顔（正射影 = 立ち絵らしい・ゆがまない）・向き（斜め）・輪郭線
- 口（閉じ・半開き・開き・あいうえおん）と目（開き・半目・閉じ）を 1 枚ずつ背景透明でレンダリングし、
  立ち絵メーカーと同じキャラクター（目パチ・口パク）として保存する（テンプレートにも）
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 目・口のモーフ名の候補（モデルによって違う）
EYE_CLOSE = ["まばたき", "まばたき2", "ウィンク", "ｳｨﾝｸ", "ウインク"]
MOUTH = {"a": ["あ", "あ２", "ああ", "a"], "i": ["い", "い２", "いい", "i"], "u": ["う", "う２", "u"],
         "e": ["え", "え２", "e"], "o": ["お", "お２", "o"], "N": ["ん", "n"]}

# 表情（候補のうちモデルにあるものを使う）: 名前 → [(モーフ候補, 値)]
EXPRESSIONS = {
    "通常": [],
    "笑顔": [(["にこり"], 1.0), (["口角上げ"], 0.6), (["笑い目"], 0.3)],
    "にっこり（目を細める）": [(["笑い"], 1.0), (["にこり"], 1.0), (["口角上げ"], 0.6)],
    "怒り": [(["怒り"], 1.0), (["真面目"], 0.6), (["キリッ"], 0.5)],
    "困り": [(["困る"], 1.0), (["口角下げ"], 0.4)],
    "驚き": [(["びっくり", "驚き"], 1.0), (["上"], 0.5)],
    "照れ": [(["照れ１", "照れ", "照れ1", "赤面", "頬染め"], 1.0), (["困る"], 0.4), (["にこり"], 0.5)],
    "ジト目": [(["じと目", "ジト目"], 1.0)],
    "悲しみ": [(["困る"], 1.0), (["涙１", "涙", "ウルウル目"], 1.0), (["口角下げ"], 0.5)],
}

# ポーズ（アーマチュアの軸で回す: X = 左右・Y = 前後・Z = 上下。モデルは -Y を向いている。キャラの左 = +X）
#   値は (軸, 角度) のリスト。arm = 腕を下ろす角度・tilt = 首のかしげ に置き換える
POSES = {
    "そのまま（モデルの形）": {},
    "気をつけ（腕を下ろす）": {"左腕": [("Y", "arm")], "右腕": [("Y", "-arm")]},
    "腕を下ろしてひじを少し曲げる": {"左腕": [("Y", "arm")], "右腕": [("Y", "-arm")],
                                "左ひじ": [("X", -25)], "右ひじ": [("X", -25)]},
    "首をかしげる": {"左腕": [("Y", "arm")], "右腕": [("Y", "-arm")], "頭": [("Y", "tilt")]},
    "少し斜めに立つ": {"左腕": [("Y", "arm")], "右腕": [("Y", "-arm")], "上半身": [("Z", 12)], "頭": [("Z", -6)]},
}

FRAMINGS = ["全身", "膝上", "腰から上", "バストアップ", "顔"]

# 作る絵: キー → {"eye": 目を閉じる量, 口の母音: 開く量}
STATES = {
    "closed": {},
    "half": {"a": 0.45},
    "open": {"a": 1.0},
    "blink": {"eye": 1.0},
    "blink_half": {"eye": 0.5},
    "a": {"a": 1.0}, "i": {"i": 1.0}, "u": {"u": 1.0}, "e": {"e": 1.0}, "o": {"o": 1.0}, "N": {"N": 1.0},
}


def find_blender() -> str:
    from .settings import SETTINGS
    saved = getattr(SETTINGS, "blender_exe", "")
    if saved and os.path.exists(saved):
        return saved
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    hits = sorted(Path(pf, "Blender Foundation").glob("Blender */blender.exe"),
                  key=lambda p: [int(x) if x.isdigit() else 0 for x in p.parent.name.split()[-1].split(".")])
    # MMD Tools が入っている新しいもの
    from .mmd import has_mmd_tools
    for p in reversed(hits):
        if has_mmd_tools(str(p)):
            return str(p)
    return str(hits[-1]) if hits else ""


def pick(names: List[str], available: List[str]) -> str:
    av = set(available)
    return next((n for n in names if n in av), "")


def roles_for(morphs: List[str]) -> dict:
    """モデルのモーフから、目を閉じる・あいうえおん のモーフ名を決める。"""
    out = {"eye": pick(EYE_CLOSE, morphs)}
    for k, cands in MOUTH.items():
        out[k] = pick(cands, morphs)
    return out


def expression_morphs(name: str, morphs: List[str]) -> Dict[str, float]:
    out = {}
    for cands, v in EXPRESSIONS.get(name, []):
        m = pick(cands, morphs)
        if m:
            out[m] = v
    return out


def pose_rotations(name: str, arm: float = 38.0, tilt: float = 8.0) -> Dict[str, List[list]]:
    out = {}
    for bone, rots in POSES.get(name, {}).items():
        lst = []
        for axis, val in rots:
            if isinstance(val, str):
                sign = -1 if val.startswith("-") else 1
                val = sign * {"arm": arm, "tilt": tilt}[val.lstrip("-")]
            lst.append([axis, float(val)])
        out[bone] = lst
    return out


def state_morphs(roles: dict, base: Dict[str, float]) -> Dict[str, Dict[str, float]]:
    """作る絵ごとのモーフの値（表情のモーフ ＋ 目・口）。そのモデルに無いもの（ん など）は作らない。"""
    out = {}
    for key, st in STATES.items():
        if key == "N" and not roles.get("N"):
            continue
        if key in ("blink", "blink_half") and not roles.get("eye"):
            continue
        if key in ("i", "u", "e", "o") and not roles.get(key):
            continue
        vals = dict(base)
        ok = True
        for part, v in st.items():
            m = roles.get(part)
            if not m:
                ok = False
                break
            vals[m] = max(vals.get(m, 0.0), v)
        if ok:
            out[key] = vals
    return out


# ─────────────────────────── Blender のスクリプト ───────────────────────────
PROBE_SCRIPT = r'''
import bpy, json, sys
P = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
if not hasattr(bpy.ops, "mmd_tools"):
    print("TELOCUT_ERROR: MMD Tools が有効になっていません"); sys.exit(2)
if P.get("blend"):
    bpy.ops.wm.open_mainfile(filepath=P["blend"])
else:
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mmd_tools.import_model(filepath=P["pmx"], scale=0.08, types={"MESH", "ARMATURE", "MORPHS"})
root = next((o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"), None)
if root is None:
    print("TELOCUT_ERROR: モデルを読み込めませんでした"); sys.exit(2)
arm = next(o for o in root.children_recursive if o.type == "ARMATURE")
keys = []
for m in root.children_recursive:
    if m.type == "MESH" and m.data.shape_keys:
        keys += [k.name for k in m.data.shape_keys.key_blocks[1:] if not k.name.startswith("mmd_")]
print("TELOCUT_PROBE " + json.dumps({"name": root.mmd_root.name or root.name, "morphs": list(dict.fromkeys(keys)),
                                      "bones": [b.mmd_bone.name_j for b in arm.pose.bones if b.mmd_bone.name_j]},
                                     ensure_ascii=False))
'''

RENDER_SCRIPT = r'''
import bpy, json, sys, math, os
from mathutils import Vector, Quaternion
P = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))

def fail(m):
    print("TELOCUT_ERROR:", m); sys.exit(2)

if not hasattr(bpy.ops, "mmd_tools"):
    fail("MMD Tools が有効になっていません")
if P.get("blend"):
    bpy.ops.wm.open_mainfile(filepath=P["blend"])
else:
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    bpy.ops.mmd_tools.import_model(filepath=P["pmx"], scale=0.08, types={"MESH", "ARMATURE", "MORPHS", "DISPLAY"})
root = next((o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"), None)
if root is None:
    fail("モデルを読み込めませんでした")
arm = next(o for o in root.children_recursive if o.type == "ARMATURE")
meshes = [o for o in root.children_recursive if o.type == "MESH"]
scn = bpy.context.scene
scn.frame_set(0)

# ── ポーズ（VPD → 組み込み）
if P.get("vpd"):
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    try:
        bpy.ops.mmd_tools.import_vpd(filepath=P["vpd"], scale=0.08)
    except Exception as ex:
        print("VPD を読めませんでした:", ex)
by_j = {b.mmd_bone.name_j: b for b in arm.pose.bones if b.mmd_bone.name_j}
AX = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}
for name, rots in (P.get("pose") or {}).items():
    pb = by_j.get(name) or arm.pose.bones.get(name)
    if pb is None:
        continue
    pb.rotation_mode = "QUATERNION"
    rest = pb.bone.matrix_local.to_quaternion()
    for axis, deg in rots:
        q = Quaternion(AX[axis], math.radians(deg))
        pb.rotation_quaternion = (rest.inverted() @ q @ rest) @ pb.rotation_quaternion
root.rotation_mode = "XYZ"
root.rotation_euler[2] = math.radians(float(P.get("yaw", 0)))
bpy.context.view_layer.update()

# ── モーフ
ALL = set()
VARS = P.get("variants") or {"": P.get("states") or {}}
for sts in VARS.values():
    for st in sts.values():
        ALL |= set(st)
ALL |= set(P.get("base") or {})
ALL |= set(((P.get("anim") or {}).get("morph_keys") or {}).keys())

def set_morphs(vals):
    for m in meshes:
        sk = m.data.shape_keys
        if not sk:
            continue
        for k in sk.key_blocks:
            if k.name in ALL:
                k.value = float(vals.get(k.name, 0.0))
    bpy.context.view_layer.update()

# ── 構図（ポーズを付けた形で測る）
set_morphs(P.get("base") or {})
dg = bpy.context.evaluated_depsgraph_get()
pts = []
for m in meshes:
    ev = m.evaluated_get(dg)
    me = ev.to_mesh()
    mw = ev.matrix_world
    step = max(1, len(me.vertices) // 20000)
    vs = me.vertices
    pts += [mw @ vs[i].co for i in range(0, len(vs), step)]
    ev.to_mesh_clear()
if not pts:
    fail("メッシュがありません")
zmin, zmax = min(p.z for p in pts), max(p.z for p in pts)

def bone_z(names, default):
    for n in names:
        b = by_j.get(n)
        if b is not None:
            return (arm.matrix_world @ b.head).z
    return default
neck = bone_z(["首"], zmax - (zmax - zmin) * 0.15)
chest = bone_z(["上半身2", "上半身"], zmax - (zmax - zmin) * 0.3)
waist = bone_z(["下半身", "腰"], zmax - (zmax - zmin) * 0.5)
knee = bone_z(["左ひざ", "右ひざ"], zmin + (zmax - zmin) * 0.28)
bottom = {"全身": zmin, "膝上": knee, "腰から上": waist - (neck - waist) * 0.15,
          "バストアップ": chest - (neck - chest) * 0.6, "顔": neck - (zmax - neck) * 0.15}.get(P.get("framing", "全身"), zmin)
sel = [p for p in pts if p.z >= bottom]
xmin, xmax = min(p.x for p in sel), max(p.x for p in sel)
ymin = min(p.y for p in pts)
w, h = (xmax - xmin), (zmax - bottom)
pad = 1.0 + float(P.get("margin", 0.04)) * 2
if P.get("anim"):
    # 身振りで手が出ても切れないように左右を広げる
    cx = (xmin + xmax) / 2
    w = max(w, (zmax - bottom) * 0.62)
    xmin, xmax = cx - w / 2, cx + w / 2
H = int(P.get("height", 1600))
W = max(64, int(round(H * w / h)))
W += W % 2
H += H % 2
scn.render.resolution_x, scn.render.resolution_y = W, H
scn.render.resolution_percentage = 100
bpy.ops.object.camera_add(location=((xmin + xmax) / 2, ymin - 10, (zmax + bottom) / 2), rotation=(math.radians(90), 0, 0))
cam = bpy.context.active_object
cam.data.type = "ORTHO"
cam.data.ortho_scale = max(w, h) * pad
cam.data.clip_end = 1000
scn.camera = cam

# ── 見た目
scn.render.film_transparent = True
scn.view_settings.view_transform = "Standard"
for eng in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
    try:
        scn.render.engine = eng
        break
    except TypeError:
        pass
bpy.ops.object.light_add(type="SUN", rotation=(math.radians(50), 0, math.radians(30)))
bpy.context.active_object.data.energy = 3.0
scn.world = scn.world or bpy.data.worlds.new("World")
scn.world.use_nodes = True
bg = scn.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[1].default_value = 0.6
if P.get("outline"):
    scn.render.use_freestyle = True
    scn.render.line_thickness_mode = "ABSOLUTE"
    scn.render.line_thickness = float(P.get("outline_px", 2.0))
scn.render.image_settings.file_format = "PNG"
scn.render.image_settings.color_mode = "RGBA"

if P.get("save_blend"):
    bpy.ops.wm.save_as_mainfile(filepath=P["save_blend"], copy=True)

os.makedirs(P["out_dir"], exist_ok=True)
A = P.get("anim")
if A:
    # ── 身振り手振り・口パク・まばたき（キーフレーム）→ 背景透明の動画
    fps = float(A.get("fps", 30))
    if abs(fps - 29.97) < 0.01:
        scn.render.fps, scn.render.fps_base = 30000, 1001
    elif abs(fps - 59.94) < 0.01:
        scn.render.fps, scn.render.fps_base = 60000, 1001
    else:
        scn.render.fps, scn.render.fps_base = int(round(fps)), 1
    for name, keys in (A.get("bone_keys") or {}).items():
        pb = by_j.get(name) or arm.pose.bones.get(name)
        if pb is None:
            continue
        pb.rotation_mode = "QUATERNION"
        rest = pb.bone.matrix_local.to_quaternion()
        for f, rots in keys:
            q = Quaternion()
            for axis, deg in rots:
                q = (rest.inverted() @ Quaternion(AX[axis], math.radians(deg)) @ rest) @ q
            pb.rotation_quaternion = q
            pb.keyframe_insert("rotation_quaternion", frame=int(f))
    base = P.get("base") or {}
    for m in meshes:
        sk = m.data.shape_keys
        if not sk:
            continue
        for k in sk.key_blocks:
            if k.name in ALL:
                k.value = float(base.get(k.name, 0.0))
        for name, keys in (A.get("morph_keys") or {}).items():
            kb = sk.key_blocks.get(name)
            if kb is None:
                continue
            for f, v in keys:
                kb.value = float(v)
                kb.keyframe_insert("value", frame=int(f))
    scn.frame_start = 0
    scn.frame_end = max(1, int(A["frames"])) - 1
    scn.render.image_settings.file_format = "FFMPEG"
    scn.render.ffmpeg.format = "QUICKTIME"
    scn.render.ffmpeg.codec = "PNG"
    scn.render.image_settings.color_mode = "RGBA"
    scn.render.filepath = os.path.join(P["out_dir"], "anim_")
    bpy.ops.render.render(animation=True)
    print("TELOCUT_DONE")
else:
    for vname, states in VARS.items():
        d = os.path.join(P["out_dir"], vname) if vname else P["out_dir"]
        os.makedirs(d, exist_ok=True)
        for key, vals in states.items():
            set_morphs(vals)
            scn.render.filepath = os.path.join(d, key + ".png")
            bpy.ops.render.render(write_still=True)
            print("TELOCUT_STATE", (vname + "/" if vname else "") + key, flush=True)
    print("TELOCUT_DONE")
'''

EDIT_SCRIPT = r'''
# TeloCut: モデルを読み込んでポーズを付けた状態で開く（ここでポーズを直して保存 → TeloCut で「.blend から作る」）
import bpy, json, sys, math
P = json.load(open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8"))
''' + RENDER_SCRIPT.split("# ── モーフ")[0].split("import bpy, json, sys, math, os")[1] + r'''
bpy.ops.wm.save_as_mainfile(filepath=P["save_blend"])
print("TeloCut: ポーズを直したら Ctrl+S で保存して、TeloCut の「.blend から作る」を押してください")
'''


def run_blender(exe: str, script: str, params: dict, work_dir: str, background: bool = True,
                progress: Optional[Callable[[str], None]] = None, cancel=None, timeout: float = 1800,
                line_cb: Optional[Callable[[str], None]] = None) -> str:
    """Blender でスクリプトを実行（params は JSON で渡す）。標準出力をまとめて返す。エラーなら例外。"""
    os.makedirs(work_dir, exist_ok=True)
    sp = os.path.join(work_dir, "telocut_mmd.py")
    jp = os.path.join(work_dir, "telocut_mmd.json")
    Path(sp).write_text(script, encoding="utf-8")
    Path(jp).write_text(json.dumps(params, ensure_ascii=False), encoding="utf-8")
    args = [exe] + (["-b"] if background else []) + ["--python", sp, "--", jp]
    if not background:
        subprocess.Popen(args, creationflags=0)
        return ""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=_NO_WINDOW,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = []
    import time
    t0 = time.time()
    for raw in p.stdout:
        line = raw.decode("utf-8", "replace").rstrip()
        out.append(line)
        if progress and line.startswith("TELOCUT_STATE"):
            progress(line.split(" ", 1)[1])
        if line_cb is not None:
            line_cb(line)
        if cancel is not None and cancel():
            p.kill()
            raise InterruptedError("中止しました")
        if time.time() - t0 > timeout:
            p.kill()
            raise TimeoutError("Blender が時間内に終わりませんでした")
    p.wait()
    text = "\n".join(out)
    err = next((l.split("TELOCUT_ERROR:", 1)[1].strip() for l in out if "TELOCUT_ERROR:" in l), "")
    if err:
        raise RuntimeError(err)
    # Blender は スクリプトの例外では終了コードが 0 のまま → 終わりの印で確かめる
    if p.returncode != 0 or ("TELOCUT_DONE" not in text and "TELOCUT_PROBE" not in text and "TeloCut:" not in text):
        tb = [l for l in out if l.strip()][-15:]
        raise RuntimeError("Blender でエラーになりました:\n" + "\n".join(tb))
    return text


def probe(exe: str, pmx: str = "", blend: str = "", work_dir: str = "") -> dict:
    """モデルの名前・モーフ・ボーン。"""
    out = run_blender(exe, PROBE_SCRIPT, {"pmx": pmx, "blend": blend}, work_dir or _work_dir())
    line = next((l for l in out.splitlines() if l.startswith("TELOCUT_PROBE ")), "")
    if not line:
        raise RuntimeError("モデルの情報を読めませんでした")
    return json.loads(line[len("TELOCUT_PROBE "):])


def _work_dir() -> str:
    from .settings import BASE_DIR
    d = BASE_DIR / "mmd_tachie"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def render_params(pmx: str, morphs: List[str], expression: str = "通常", pose: str = "気をつけ（腕を下ろす）",
                  arm: float = 38.0, tilt: float = 8.0, framing: str = "上半身", yaw: float = 0.0, height: int = 1600,
                  outline: bool = False, vpd: str = "", blend: str = "", extra_morphs: Optional[Dict[str, float]] = None,
                  out_dir: str = "", save_blend: str = "") -> dict:
    roles = roles_for(morphs)
    base = expression_morphs(expression, morphs)
    base.update(extra_morphs or {})
    return {"pmx": pmx, "blend": blend, "vpd": vpd, "pose": pose_rotations(pose, arm, tilt) if not blend else {},
            "yaw": yaw, "framing": framing, "height": int(height), "outline": bool(outline), "base": base,
            "states": state_morphs(roles, base), "roles": roles, "out_dir": out_dir, "save_blend": save_blend}


def variants_params(params: dict, morphs: List[str], expressions: List[str],
                    extra_morphs: Optional[Dict[str, float]] = None) -> dict:
    """表情差分をまとめて作る（Blender は 1 回だけ起動）。params は render_params の結果。"""
    roles = roles_for(morphs)
    var = {}
    for i, ex in enumerate(expressions):
        base = expression_morphs(ex, morphs)
        base.update(extra_morphs or {})
        var[f"{i:02d}"] = state_morphs(roles, base)
    return {**params, "variants": var, "variant_names": {f"{i:02d}": ex for i, ex in enumerate(expressions)},
            "states": {}}


def render_variants(exe: str, params: dict, progress=None, cancel=None) -> Dict[str, Dict[str, str]]:
    """{表情の名前: {キー: PNG}}"""
    run_blender(exe, RENDER_SCRIPT, params, params["out_dir"], progress=progress, cancel=cancel)
    out = {}
    for vk, name in params["variant_names"].items():
        d = os.path.join(params["out_dir"], vk)
        paths = {k: os.path.join(d, k + ".png") for k in params["variants"][vk] if os.path.exists(os.path.join(d, k + ".png"))}
        if "closed" in paths:
            out[name] = paths
    if not out:
        raise RuntimeError("レンダリングした絵がありません")
    return out


def render_animation(exe: str, params: dict, progress=None, cancel=None) -> str:
    """身振り手振り・口パクの動画（背景透明の MOV）を作ってパスを返す。"""
    frames = int(params["anim"]["frames"])
    done = [0]

    def prog(line):
        if progress:
            progress(line)
    import re
    p = dict(params)

    def watch(line):
        m = re.match(r"Fra:(\d+)", line)
        if m and progress:
            f = int(m.group(1))
            if f > done[0]:
                done[0] = f
                progress(f"{f}/{frames}")
    run_blender(exe, RENDER_SCRIPT, p, p["out_dir"], progress=None, cancel=cancel, line_cb=watch, timeout=6 * 3600)
    hits = sorted(Path(p["out_dir"]).glob("anim_*.mov"), key=lambda x: x.stat().st_mtime)
    if not hits:
        raise RuntimeError("動画ができませんでした")
    return str(hits[-1])


def render(exe: str, params: dict, progress=None, cancel=None) -> Dict[str, str]:
    """レンダリングして {キー: PNG のパス}。"""
    run_blender(exe, RENDER_SCRIPT, params, params["out_dir"], progress=progress, cancel=cancel)
    out = {}
    for k in params["states"]:
        p = os.path.join(params["out_dir"], k + ".png")
        if os.path.exists(p):
            out[k] = p
    if "closed" not in out:
        raise RuntimeError("レンダリングした絵がありません")
    return out


def load_images(paths: Dict[str, str]):
    """PNG を読み込み、全部が入る範囲で切り抜く（立ち絵メーカーと同じ形）。"""
    from PIL import Image
    imgs = {k: Image.open(p).convert("RGBA") for k, p in paths.items()}
    box = None
    for im in imgs.values():
        b = im.getbbox()
        if b:
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    if box:
        imgs = {k: v.crop(box) for k, v in imgs.items()}
    return imgs


def open_in_blender(exe: str, params: dict) -> str:
    """モデルを読み込み、ポーズを付けた .blend を作って Blender の画面で開く（自由にポーズを直せる）。"""
    blend = params.get("save_blend") or os.path.join(_work_dir(), "pose.blend")
    params = {**params, "save_blend": blend}
    run_blender(exe, EDIT_SCRIPT, params, _work_dir())
    subprocess.Popen([exe, blend])
    return blend
