"""MMD（MikuMikuDance）連携。

- MMD の起動（場所は自動で探す / 環境設定 mmd_exe）
- 口パク VMD: TeloCut で合成したセリフの母音タイムライン（<wav>.mouth.json）から「あ い う え お」の表情モーションを作る。
  MMD でモデルに読み込むと、セリフに合わせて口が動く（30 fps・タイムラインの時刻どおり）
- MMD のシーンを Blender（MMD Tools）でレンダリング: PMX モデル + VMD モーション（+ カメラ VMD + 口パク）を
  背景透明の動画にして、タイムラインに自動でつなぐ（外部アプリ連携と同じ「書き出し待ち」の仕組み）
"""
from __future__ import annotations

import glob
import json
import os
import string
import struct
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .settings import SETTINGS

MMD_FPS = 30
VOWEL_MORPHS = {"a": "あ", "i": "い", "u": "う", "e": "え", "o": "お"}


# ─────────────────────────── MMD 本体 ───────────────────────────

def find_mmd() -> str:
    saved = getattr(SETTINGS, "mmd_exe", "")
    if saved and os.path.exists(saved):
        return saved
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), str(Path.home() / "Desktop"),
             str(Path.home() / "Documents"), str(Path.home() / "Downloads")]
    roots += [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
    for r in roots:
        for pat in ("MikuMikuDance*/MikuMikuDance.exe", "*/MikuMikuDance*/MikuMikuDance.exe", "MMD*/MikuMikuDance.exe"):
            hits = sorted(glob.glob(os.path.join(r, pat)))
            if hits:
                return hits[-1]
    return ""


def launch_mmd(exe: str = "", pmm: str = "") -> bool:
    exe = exe or find_mmd()
    if not exe:
        return False
    args = [exe] + ([pmm] if pmm else [])
    subprocess.Popen(args, cwd=os.path.dirname(exe))
    return True


def mmd_user_dirs(exe: str) -> Dict[str, str]:
    """MMD 付属の UserFile（Model / Motion など）の場所。"""
    base = Path(exe).parent / "UserFile"
    return {k: str(base / k) for k in ("Model", "Motion", "Wave", "Accessory")} if base.is_dir() else {}


# ─────────────────────────── VMD ───────────────────────────

def _fixed(text: str, n: int) -> bytes:
    b = text.encode("cp932", "replace")[:n]
    return b + b"\0" * (n - len(b))


def write_vmd(path: str, model_name: str, morphs: List[Tuple[str, int, float]]):
    """表情（モーフ）だけの VMD を書く。morphs: [(表情名, フレーム, 0〜1), ...]"""
    with open(path, "wb") as f:
        f.write(_fixed("Vocaloid Motion Data 0002", 30))
        f.write(_fixed(model_name, 20))
        f.write(struct.pack("<I", 0))                              # ボーン
        f.write(struct.pack("<I", len(morphs)))
        for name, frame, w in morphs:
            f.write(_fixed(name, 15) + struct.pack("<If", int(frame), float(w)))
        f.write(struct.pack("<III", 0, 0, 0))                     # カメラ / 照明 / セルフ影
        f.write(struct.pack("<I", 0))                              # 表示・IK


def read_vmd_morphs(path: str) -> Tuple[str, List[Tuple[str, int, float]]]:
    b = open(path, "rb").read()
    name = b[30:50].split(b"\0")[0].decode("cp932", "ignore")
    nb = struct.unpack_from("<I", b, 50)[0]
    off = 54 + nb * 111
    nm = struct.unpack_from("<I", b, off)[0]
    off += 4
    out = []
    for i in range(nm):
        n = b[off:off + 15].split(b"\0")[0].decode("cp932", "ignore")
        fr, w = struct.unpack_from("<If", b, off + 15)
        out.append((n, fr, w))
        off += 23
    return name, out


def vmd_length(path: str) -> int:
    """VMD の最後のフレーム（ボーン・表情・カメラのうち最大）。"""
    try:
        b = open(path, "rb").read()
        off = 50
        last = 0
        nb = struct.unpack_from("<I", b, off)[0]
        off += 4
        for i in range(nb):
            last = max(last, struct.unpack_from("<I", b, off + 15)[0])
            off += 111
        if off + 4 <= len(b):
            nm = struct.unpack_from("<I", b, off)[0]
            off += 4
            for i in range(nm):
                last = max(last, struct.unpack_from("<I", b, off + 15)[0])
                off += 23
        if off + 4 <= len(b):
            nc = struct.unpack_from("<I", b, off)[0]
            off += 4
            for i in range(nc):
                last = max(last, struct.unpack_from("<I", b, off)[0])
                off += 61
        return last
    except Exception:
        return 0


def lipsync_morphs(segments: List[Tuple[float, str]], strength: float = 1.0, morph_names: Optional[dict] = None,
                   duration: float = 0.0) -> List[Tuple[str, int, float]]:
    """segments: [(開始秒, 母音タイムラインの wav), ...] → 「あいうえお」の表情キー（30 fps）。
    母音が変わる所だけキーを打つ（前のフレームに前の値を打って、1 フレームで切り替わるように）。"""
    from .voiceengine import mouth_shape_at
    names = {**VOWEL_MORPHS, **(morph_names or {})}
    end = duration
    lens = []
    for t0, wav in segments:
        try:
            d = json.loads(Path(wav + ".mouth.json").read_text(encoding="utf-8"))
            ln = len(d["shapes"]) / d["rate"]
        except (OSError, ValueError, KeyError):
            ln = 0
        lens.append(ln)
        end = max(end, t0 + ln)
    n = int(end * MMD_FPS) + 2
    weights = [{} for _ in range(n)]
    for (t0, wav), ln in zip(segments, lens):
        for f in range(int(t0 * MMD_FPS), min(n, int((t0 + ln) * MMD_FPS) + 1)):
            sh = mouth_shape_at(wav, f / MMD_FPS - t0)
            if sh in names:
                weights[f] = {sh: strength}
    out: List[Tuple[str, int, float]] = []
    for key, mname in names.items():
        prev = 0.0
        for f in range(n):
            w = weights[f].get(key, 0.0)
            if w != prev:
                if f > 0:
                    out.append((mname, f - 1, prev))
                out.append((mname, f, w))
            prev = w
        if prev:
            out.append((mname, n, 0.0))
    if not out:
        return []
    # 0 フレーム目に全部 0 を置いておく（前のモーションの口が残らないように）
    out += [(m, 0, 0.0) for m in names.values() if not any(o[0] == m and o[1] == 0 for o in out)]
    return sorted(out, key=lambda o: (o[1], o[0]))


def lipsync_from_sequence(project, seq, track_index: int, start_frame: int = 0, end_frame: int = 0,
                          strength: float = 1.0) -> Tuple[List[Tuple[str, int, float]], int]:
    """音声トラックのセリフ（母音タイムライン付き）から口パクの表情キーを作る。
    start_frame の位置が VMD の 0 フレーム目。戻り値: (表情キー, 使ったクリップ数)"""
    from .voiceengine import mouth_path
    t = seq.audio_tracks[track_index]
    segs = []
    for c in t.clips:
        if c.kind != "audio" or c.end <= start_frame or (end_frame and c.start >= end_frame):
            continue
        m = project.media_by_id(c.media_id)
        if m is None or not os.path.exists(mouth_path(m.path)):
            continue
        segs.append(((c.start - start_frame) / seq.fps - c.src_in / seq.fps, m.path))
    dur = ((end_frame or seq.duration) - start_frame) / seq.fps
    return lipsync_morphs(segs, strength, duration=dur), len(segs)


# ─────────────────────────── Blender（MMD Tools）でレンダリング ───────────────────────────

def blender_user_dir(blender_exe: str) -> Optional[Path]:
    import re
    m = re.search(r"Blender (\d+\.\d+)", blender_exe)
    if not m:
        return None
    return Path(os.environ.get("APPDATA", "")) / "Blender Foundation" / "Blender" / m.group(1)


def has_mmd_tools(blender_exe: str) -> bool:
    d = blender_user_dir(blender_exe)
    if d is None:
        return False
    return any(p.is_dir() for p in list(d.glob("extensions/*/mmd_tools")) + [d / "scripts" / "addons" / "mmd_tools"])


def install_mmd_tools(blender_exe: str) -> Tuple[bool, str]:
    """Blender の拡張機能サイト（extensions.blender.org）から MMD Tools を入れて有効にする。"""
    r = subprocess.run([blender_exe, "-b", "-c", "extension", "install", "-s", "-e", "mmd_tools"],
                       capture_output=True, text=True, timeout=600, creationflags=0x08000000 if os.name == "nt" else 0)
    out = (r.stdout + r.stderr)[-1500:]
    return has_mmd_tools(blender_exe), out


def render_script(pmx: str, motions: List[str], camera_vmd: str, lipsync_vmd: str, out_prefix: str,
                  width: int, height: int, frames: int, scale: float = 0.08, eevee: bool = True) -> str:
    """MMD Tools でモデルとモーションを読み込み、背景透明でレンダリングする Blender 用スクリプト。"""
    return f'''# TeloCut から生成: MMD のモデルとモーションを背景透明でレンダリング
import bpy, math, sys
from mathutils import Vector

def fail(msg):
    print("TELOCUT_ERROR:", msg)
    sys.exit(2)

if not hasattr(bpy.ops, "mmd_tools") or not hasattr(bpy.ops.mmd_tools, "import_model"):
    fail("MMD Tools が有効になっていません")

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
scn = bpy.context.scene
scn.render.fps = {MMD_FPS}
scn.render.resolution_x, scn.render.resolution_y = {width}, {height}
scn.render.resolution_percentage = 100
scn.render.film_transparent = True
scn.view_settings.view_transform = "Standard"     # Blender 4 の既定（AgX）は色が淡くなるので
try:
    scn.render.engine = "BLENDER_EEVEE_NEXT" if {bool(eevee)} else "CYCLES"
except TypeError:
    scn.render.engine = "BLENDER_EEVEE"

bpy.ops.mmd_tools.import_model(filepath=r"{pmx}", scale={scale}, types={{"MESH", "ARMATURE", "MORPHS", "DISPLAY"}})
root = next((o for o in bpy.data.objects if getattr(o, "mmd_type", "") == "ROOT"), None)
if root is None:
    fail("モデルを読み込めませんでした")

def select_model():
    bpy.ops.object.select_all(action="DESELECT")
    for o in [root] + list(root.children_recursive):
        o.select_set(True)
    bpy.context.view_layer.objects.active = root

for vmd in {motions!r} + ([r"{lipsync_vmd}"] if r"{lipsync_vmd}" else []):
    select_model()
    bpy.ops.mmd_tools.import_vmd(filepath=vmd, scale={scale}, margin=0, update_scene_settings=False)

# カメラ
cam_vmd = r"{camera_vmd}"
if cam_vmd:
    bpy.ops.object.camera_add()
    bpy.ops.mmd_tools.convert_to_mmd_camera(scale={scale})
    cam_root = bpy.context.active_object
    bpy.ops.object.select_all(action="DESELECT")
    cam_root.select_set(True)
    bpy.context.view_layer.objects.active = cam_root
    bpy.ops.mmd_tools.import_vmd(filepath=cam_vmd, scale={scale}, margin=0, update_scene_settings=False)
    scn.camera = next(o for o in bpy.data.objects if o.type == "CAMERA")
else:
    # 全身が入るように正面から
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center = (lo + hi) / 2
    h = max(hi.z - lo.z, (hi.x - lo.x) * {height} / {width})
    bpy.ops.object.camera_add(location=(center.x, center.y - h * 2.2, center.z), rotation=(math.radians(90), 0, 0))
    cam = bpy.context.active_object
    cam.data.lens = 50
    scn.camera = cam

bpy.ops.object.light_add(type="SUN", rotation=(math.radians(50), 0, math.radians(30)))
bpy.context.active_object.data.energy = 3.0
scn.world = scn.world or bpy.data.worlds.new("World")
scn.world.use_nodes = True
bg = scn.world.node_tree.nodes.get("Background")
if bg:
    bg.inputs[1].default_value = 0.6

scn.frame_start = 0
scn.frame_end = {max(1, frames) - 1}
scn.render.image_settings.file_format = "FFMPEG"
scn.render.ffmpeg.format = "QUICKTIME"
scn.render.ffmpeg.codec = "PNG"
scn.render.image_settings.color_mode = "RGBA"
scn.render.filepath = r"{out_prefix}"
bpy.ops.render.render(animation=True)
print("TELOCUT_DONE")
'''


def guess_frames(motions: List[str], camera_vmd: str = "") -> int:
    return max([vmd_length(m) for m in motions + ([camera_vmd] if camera_vmd else [])] + [0]) + 1
