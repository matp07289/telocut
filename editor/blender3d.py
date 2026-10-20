"""Blender で作る 3D 素材（3D テキスト / ロゴ板）と、Blender・MMD のバックグラウンドレンダリング。

- 3D テキスト: 文字に厚み・面取り・材質（金・銀・クローム・プラスチック・ネオン）を付け、回転 / ポップイン / ゆれ で動かす
- 3D ロゴ板: 画像（PNG のロゴなど）を厚みのある板にして回す
- どちらも背景透明の動画（QuickTime PNG）でレンダリングし、再生ヘッドの位置に「書き出し待ち」として置く
  → レンダリングが終わると自動でつながる（MediaWatcher）
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from . import model as M

MATERIALS = {
    "金": {"color": (1.0, 0.52, 0.08), "metallic": 1.0, "rough": 0.3},
    "銀": {"color": (0.92, 0.92, 0.95), "metallic": 1.0, "rough": 0.18},
    "クローム": {"color": (0.98, 0.98, 1.0), "metallic": 1.0, "rough": 0.03},
    "赤いプラスチック": {"color": (0.85, 0.08, 0.08), "metallic": 0.0, "rough": 0.35},
    "白いプラスチック": {"color": (0.95, 0.95, 0.95), "metallic": 0.0, "rough": 0.4},
    "ネオン（光る）": {"color": (0.05, 0.75, 1.0), "metallic": 0.0, "rough": 0.5, "emit": 1.6},
}
ANIMS = ["回転（Y 軸）", "ポップイン", "ゆれ", "なし"]

_COMMON_TAIL = '''
# ── カメラ・ライト・出力 ──
from mathutils import Vector
bpy.context.view_layer.update()
pts = [o.matrix_world @ Vector(c) for o in objs for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
w_, h_ = hi.x - lo.x, hi.z - lo.z
span = max(w_ * 1.25, h_ * 1.25 * RES_X / RES_Y, 0.1) / FILL
bpy.ops.object.camera_add(location=(0, -span * 2.0, 0), rotation=(math.radians(90), 0, 0))
cam = bpy.context.active_object
cam.data.lens = 50
cam.data.sensor_width = 36
cam.location.y = -(span * 50 / 36) * 1.0
scn.camera = cam
bpy.ops.object.light_add(type="AREA", location=(span * 0.6, -span * 1.2, span * 0.9))
key = bpy.context.active_object
key.data.energy = 120 * span * span
key.data.size = span
key.rotation_euler = (math.radians(55), 0, math.radians(25))
bpy.ops.object.light_add(type="AREA", location=(-span, -span * 0.6, -span * 0.2))
fill = bpy.context.active_object
fill.data.energy = 45 * span * span
fill.data.size = span
fill.rotation_euler = (math.radians(80), 0, math.radians(-50))
scn.world = scn.world or bpy.data.worlds.new("World")
scn.world.use_nodes = True
bgn = scn.world.node_tree.nodes.get("Background")
if bgn:
    bgn.inputs[0].default_value = (0.32, 0.33, 0.36, 1)     # 金属の映り込み用の環境光（明るすぎると白っぽくなる）
    bgn.inputs[1].default_value = 0.45
scn.render.film_transparent = True
scn.view_settings.view_transform = "Standard"     # Blender 4 の既定（AgX）は色が淡くなるので
scn.render.resolution_x, scn.render.resolution_y, scn.render.resolution_percentage = RES_X, RES_Y, 100
scn.render.fps = FPS
scn.frame_start, scn.frame_end = 1, FRAMES
try:
    scn.render.engine = "BLENDER_EEVEE_NEXT"
except TypeError:
    scn.render.engine = "BLENDER_EEVEE"
scn.render.image_settings.file_format = "FFMPEG"
scn.render.ffmpeg.format = "QUICKTIME"
scn.render.ffmpeg.codec = "PNG"
scn.render.image_settings.color_mode = "RGBA"
scn.render.filepath = OUT
bpy.ops.render.render(animation=True)
print("TELOCUT_DONE")
'''


def _material_code(name: str, mat: str, image: str = "") -> str:
    m = MATERIALS.get(mat, MATERIALS["金"])
    emit = m.get("emit", 0.0)
    tex = ""
    if image:
        tex = f'''
img = bpy.data.images.load(r"{image}")
tn = nt.nodes.new("ShaderNodeTexImage")
tn.image = img
nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
nt.links.new(tn.outputs["Alpha"], bsdf.inputs["Alpha"])
mat.blend_method = "BLEND" if hasattr(mat, "blend_method") else None
'''
    return f'''
mat = bpy.data.materials.new("{name}")
mat.use_nodes = True
nt = mat.node_tree
bsdf = nt.nodes.get("Principled BSDF")
bsdf.inputs["Base Color"].default_value = ({m["color"][0]}, {m["color"][1]}, {m["color"][2]}, 1)
bsdf.inputs["Metallic"].default_value = {m["metallic"]}
bsdf.inputs["Roughness"].default_value = {m["rough"]}
if {emit} > 0:
    k = "Emission Color" if "Emission Color" in bsdf.inputs else "Emission"
    bsdf.inputs[k].default_value = ({m["color"][0]}, {m["color"][1]}, {m["color"][2]}, 1)
    bsdf.inputs["Emission Strength"].default_value = {emit}
{tex}'''


def _anim_code(anim: str, frames: int) -> str:
    if anim.startswith("回転"):
        return f'''
for o in objs:
    o.rotation_euler = (o.rotation_euler.x, 0, 0)
    o.keyframe_insert("rotation_euler", index=2, frame=1)
    o.rotation_euler.z = math.radians(360)
    o.keyframe_insert("rotation_euler", index=2, frame={frames + 1})
    for fc in o.animation_data.action.fcurves:
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
'''
    if anim == "ポップイン":
        return '''
for o in objs:
    s = o.scale.copy()
    for f, k in ((1, 0.01), (9, 1.18), (14, 0.95), (18, 1.0)):
        o.scale = s * k
        o.keyframe_insert("scale", frame=f)
'''
    if anim == "ゆれ":
        return f'''
for o in objs:
    for f in range(1, {frames + 2}, 6):
        o.rotation_euler.z = math.radians(18 * math.sin(f / 10))
        o.keyframe_insert("rotation_euler", index=2, frame=f)
'''
    return ""


def text3d_script(text: str, font: str, depth: float, bevel: float, material: str, anim: str,
                  width: int, height: int, fps: int, frames: int, out_prefix: str, fill: float = 0.7) -> str:
    font_line = f'cu.font = bpy.data.fonts.load(r"{font}")' if font and os.path.exists(font) else ""
    return f'''# TeloCut から生成: 3D テキスト
import bpy, math
RES_X, RES_Y, FPS, FRAMES, OUT, FILL = {width}, {height}, {fps}, {frames}, r"{out_prefix}", {fill}
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
scn = bpy.context.scene
cu = bpy.data.curves.new("telop3d", "FONT")
cu.body = {text!r}
{font_line}
cu.align_x, cu.align_y = "CENTER", "CENTER"
cu.size = 1.0
cu.extrude = {depth}
cu.bevel_depth = {bevel}
cu.bevel_resolution = 3
cu.space_line = 1.1
ob = bpy.data.objects.new("telop3d", cu)
scn.collection.objects.link(ob)
ob.rotation_euler = (math.radians(90), 0, 0)
{_material_code("telop3d_mat", material)}
ob.data.materials.append(mat)
objs = [ob]
{_anim_code(anim, frames)}
''' + _COMMON_TAIL


def logo3d_script(image: str, depth: float, material: str, anim: str, width: int, height: int, fps: int,
                  frames: int, out_prefix: str, fill: float = 0.7) -> str:
    """画像を厚みのある板にする（表面は画像、側面は材質の色）。"""
    return f'''# TeloCut から生成: 3D ロゴ板
import bpy, math
RES_X, RES_Y, FPS, FRAMES, OUT, FILL = {width}, {height}, {fps}, {frames}, r"{out_prefix}", {fill}
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob, do_unlink=True)
scn = bpy.context.scene
img = bpy.data.images.load(r"{image}")
aspect = img.size[0] / max(1, img.size[1])
bpy.ops.mesh.primitive_cube_add(size=1)
ob = bpy.context.active_object
ob.scale = (aspect, {max(0.01, depth)}, 1.0)
bpy.ops.object.transform_apply(scale=True)
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.uv.cube_project(cube_size=1.0)
bpy.ops.object.mode_set(mode="OBJECT")
{_material_code("logo_side", material)}
side = mat
{_material_code("logo_face", material, image)}
face = mat
ob.data.materials.append(side)
ob.data.materials.append(face)
# 正面（-Y 向き）と背面の面に画像を貼る
for poly in ob.data.polygons:
    if abs(poly.normal.y) > 0.9:
        poly.material_index = 1
# 正面の UV を画像全体に合わせる
uv = ob.data.uv_layers.active.data
for poly in ob.data.polygons:
    if abs(poly.normal.y) > 0.9:
        vs = [ob.data.vertices[ob.data.loops[li].vertex_index].co for li in poly.loop_indices]
        xs, zs = [v.x for v in vs], [v.z for v in vs]
        for li in poly.loop_indices:
            v = ob.data.vertices[ob.data.loops[li].vertex_index].co
            u = (v.x - min(xs)) / max(1e-6, max(xs) - min(xs))
            if poly.normal.y > 0:
                u = 1 - u
            uv[li].uv = (u, (v.z - min(zs)) / max(1e-6, max(zs) - min(zs)))
objs = [ob]
{_anim_code(anim, frames)}
''' + _COMMON_TAIL


class BlenderJob(QObject):
    """Blender をバックグラウンドで動かす（終わったら done / 失敗したら failed）。"""
    done = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, blender: str, script: str, script_path: Path, out_file_glob: str, log_path: Path,
                 extra_args=None, factory: bool = True):
        super().__init__()
        self.args = [blender, "-b"] + (["--factory-startup"] if factory else []) + \
            (extra_args or []) + ["--python", str(script_path)]
        script_path.write_text(script, encoding="utf-8")
        self.glob = out_file_glob
        self.log = log_path
        self.proc: Optional[subprocess.Popen] = None

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import glob as _g
        try:
            with open(self.log, "w", encoding="utf-8", errors="replace") as lf:
                self.proc = subprocess.Popen(self.args, stdout=lf, stderr=subprocess.STDOUT,
                                             creationflags=0x08000000 if os.name == "nt" else 0)
                code = self.proc.wait()
        except OSError as ex:
            self.failed.emit(f"Blender を起動できません: {ex}")
            return
        hits = sorted(_g.glob(self.glob))
        txt = self.log.read_text(encoding="utf-8", errors="replace") if self.log.exists() else ""
        if code == 0 and hits and "TELOCUT_ERROR" not in txt:
            self.done.emit(hits[-1])
        else:
            err = next((l.split("TELOCUT_ERROR:", 1)[1].strip() for l in txt.splitlines() if "TELOCUT_ERROR:" in l), "")
            self.failed.emit(err or f"Blender でのレンダリングに失敗しました（ログ: {self.log}）\n" + txt[-600:])

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


def place_pending(mw, name: str, pattern: str, frame: int, dur: int, folder: Path) -> M.MediaItem:
    """レンダリング中の素材を「書き出し待ち」として再生ヘッドの位置（一番上の新しいトラック）に置く。"""
    seq = mw.project.sequence
    item = M.MediaItem(id=M.new_id(), path=str(folder / name), name=name, kind="video",
                       duration_s=dur / seq.fps, fps=seq.fps, width=seq.width, height=seq.height,
                       has_video=True, has_audio=False, pending=pattern)
    mw.begin_edit()
    mw.project.media.append(item)
    mw.project.bin_by_id(mw.named_bin("3D / MMD")).add("media", item.id)
    t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
    seq.video_tracks.append(t)
    t.clips.append(M.Clip(M.new_id(), "video", frame, max(1, dur), 0, item.id, name))
    mw.end_edit(f"3D 素材を配置（レンダリング中）: {name}")
    return item
