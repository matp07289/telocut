"""レコードモニターでクリップを直接動かす（Premiere のダイレクト操作）と、「すぐ使う」をエフェクトとして使う（MainWindow のミックスイン）。

ダイレクト操作:
  レコードモニターをダブルクリック → そこに映っている一番上のクリップを選んで枠を出す
  枠の中をドラッグ = 位置 / 角の □ = 大きさ（Shift で中心を固定しない）/ 角の外側 = 回転（Shift で 15° ずつ）
  動かすと画面の中心・端・三分割にぴったり付く（Alt を押している間は付かない）
  矢印キー = 1px（Shift で 10px）/ Esc か何もない所をダブルクリックで終わり
  キーフレームのある値は、再生ヘッドの位置にキーを打つ（エフェクトエディターと同じ）

すぐ使う → エフェクト: ルック・背景を抜く・モザイク・揺れ・カクカク・ワイプの枠・ぼかし背景・スポットライト・虫めがね
  （エフェクトエディターで種類・強さ・範囲などをあとから変えられる・キーフレームも打てる）
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from . import model as M

Box = Tuple[float, float, float, float, float]          # 中心 x, 中心 y, 幅, 高さ, 回転（度）・シーケンスの px
VISUAL = ("video", "image", "text", "shape", "color", "nest", "character")
SNAP_PX = 8                                              # 画面上でこのくらい近づいたら付く


class Features11Mixin:
    def _feature11_commands(self) -> dict:
        return {"dm_toggle": self.dm_toggle, "spotlight": lambda: self.add_quick_fx("spotlight"),
                "magnifier": lambda: self.add_quick_fx("magnifier")}

    def _init_feature11(self):
        v = self.record_mon.view
        v.double_clicked.connect(self.dm_double_click)
        v.tf_mouse.connect(self.dm_mouse)
        v.tf_key.connect(self.dm_key)
        self._dm_clip: Optional[str] = None
        self._dm_drag = None

    def _build_feature11_menus(self):
        m = self._menu_named("クリップ")
        if m is not None:
            m.addSeparator()
            for c in ("dm_toggle", "spotlight", "magnifier"):
                self._a(m, c)

    # ─────────────────────────── 枠の計算 ───────────────────────────
    def clip_box(self, clip: M.Clip, cf: Optional[int] = None) -> Optional[Box]:
        """クリップが画面のどこに映っているか（中心・幅・高さ・回転。シーケンスの px）。"""
        from . import motion as MO
        seq = self.project.sequence
        W, H = seq.width, seq.height
        cf = self.record_player.position if cf is None else cf
        try:
            s, rot, px, py = MO.transform(seq, clip, cf, 1.0)
        except Exception:
            return None
        ox = oy = 0.0
        effects = [e.get("name") for e in clip.prop("effects") or [] if e.get("on", True)]
        if clip.kind in ("video", "image"):
            m = self.project.media_by_id(clip.media_id) if clip.media_id else None
            w0, h0 = (m.width, m.height) if m and m.width else (W, H)
            r = min(W / w0, H / h0)
            bw, bh = w0 * r, h0 * r
            if "blurfill" in effects:
                bw, bh = W, H
            else:
                l, t, rr, b = [max(0.0, min(49.0, float(v))) / 100 for v in (clip.prop("crop") or [0, 0, 0, 0])]
                ox, oy = (l - rr) / 2 * bw, (t - b) / 2 * bh
                bw, bh = bw * (1 - l - rr), bh * (1 - t - b)
        elif clip.kind == "text":
            from .render import text_patch
            arr, x, y = text_patch(self.project, clip, 1.0)
            if arr is None:
                return None
            bh, bw = arr.shape[0], arr.shape[1]
            ox, oy = x + bw / 2 - W / 2, y + bh / 2 - H / 2
        elif clip.kind == "shape":
            x, y, w, h = [float(v) for v in (clip.prop("rect") or [0, 78, 100, 22])]
            bw, bh = W * w / 100, H * h / 100
            ox, oy = W * (x + w / 2) / 100 - W / 2, H * (y + h / 2) / 100 - H / 2
        else:
            bw, bh = W, H
        return (W / 2 + ox * s + px, H / 2 + oy * s + py, bw * s, bh * s, rot)

    @staticmethod
    def _local(box: Box, x: float, y: float) -> Tuple[float, float]:
        cx, cy, _w, _h, rot = box
        a = math.radians(-rot)
        dx, dy = x - cx, y - cy
        return dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)

    @staticmethod
    def box_corners(box: Box) -> List[Tuple[float, float]]:
        cx, cy, w, h, rot = box
        a = math.radians(rot)
        out = []
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            lx, ly = sx * w / 2, sy * h / 2
            out.append((cx + lx * math.cos(a) - ly * math.sin(a), cy + lx * math.sin(a) + ly * math.cos(a)))
        return out

    def clip_at_point(self, x: float, y: float) -> Optional[M.Clip]:
        """その点に映っている一番上のクリップ（見えないトラック・ロックしたトラック・無効のクリップは飛ばす）。"""
        seq = self.project.sequence
        f = self.record_player.position
        for t in reversed(seq.video_tracks):
            if t.muted or t.locked:
                continue
            c = t.clip_at(f)
            if c is None or not c.enabled or c.kind not in VISUAL:
                continue
            box = self.clip_box(c, f)
            if box is None:
                continue
            lx, ly = self._local(box, x, y)
            if abs(lx) <= box[2] / 2 and abs(ly) <= box[3] / 2:
                return c
        return None

    # ─────────────────────────── モード ───────────────────────────
    def _to_seq(self, p: QPointF) -> Tuple[float, float]:
        seq = self.project.sequence
        return p.x() * seq.width, p.y() * seq.height

    def _px_per_view(self) -> float:
        r = self.record_mon.view.video_rect()
        return self.project.sequence.width / max(1, r.width())

    def dm_double_click(self, p: QPointF):
        x, y = self._to_seq(p)
        c = self.clip_at_point(x, y)
        if c is None:
            self.dm_exit()
            return None
        return self.dm_enter(c)

    def dm_enter(self, c: M.Clip) -> M.Clip:
        self._dm_clip = c.id
        self.canvas.selected = {c.id}
        self.canvas.selection_changed.emit()
        self.canvas.update()
        self.record_mon.view.tf_mode = True
        self.record_mon.view.setFocus()
        self.dm_update_overlay()
        self.status.showMessage("モニターで直接動かせます（中 = 位置・角 = 大きさ・角の外 = 回転・矢印キーで 1px・Esc で終わり）", 6000)
        return c

    def dm_exit(self):
        self._dm_clip = None
        self._dm_drag = None
        v = self.record_mon.view
        v.tf_mode = False
        v.tf_box = None
        v.snap_lines = []
        v.update()

    def dm_toggle(self):
        """選んだクリップをモニターで直接動かす / やめる。"""
        if self._dm_clip:
            self.dm_exit()
            return None
        sel = [c for _, c in self.canvas.selected_clips() if c.kind in VISUAL]
        if not sel:
            self.status.showMessage("動かすクリップを選ぶか、レコードモニターをダブルクリックしてください", 4000)
            return None
        return self.dm_enter(sel[0])

    def _dm_target(self) -> Optional[M.Clip]:
        if not self._dm_clip:
            return None
        _t, c = self.project.sequence.find(self._dm_clip)
        if c is None:
            self.dm_exit()
        return c

    def dm_update_overlay(self):
        c = self._dm_target()
        v = self.record_mon.view
        if c is None:
            return
        box = self.clip_box(c)
        seq = self.project.sequence
        if box is None or not (c.start <= self.record_player.position < c.end):
            v.tf_box = None
        else:
            v.tf_box = [(x / seq.width, y / seq.height) for x, y in self.box_corners(box)] + [(box[0] / seq.width, box[1] / seq.height)]
        v.update()

    # ─────────────────────────── 値の読み書き（キーフレーム対応）───────────────────────────
    def _val(self, c: M.Clip, key: str) -> float:
        from .render import prop_at
        return float(prop_at(c, key, self.record_player.position) or (1.0 if key == "scale" else 0.0))

    def _set(self, c: M.Clip, key: str, value: float):
        from .inspector import set_key
        keys = (c.prop("keys") or {}).get(key)
        if keys:
            rel = max(0, min(c.duration - 1, self.record_player.position - c.start))
            c.props["keys"] = {**c.prop("keys"), key: set_key(keys, rel, value)}
        else:
            c.props[key] = value
            if c.props.get("anim") and key in c.props["anim"]:
                c.props["anim"] = {k: v for k, v in c.props["anim"].items() if k != key}   # 開始→終了の動きより、置いた位置を優先

    # ─────────────────────────── マウス ───────────────────────────
    def dm_hit(self, x: float, y: float) -> str:
        """どこをつかんだか: corner0..3 / rotate / move / ""。"""
        c = self._dm_target()
        box = self.clip_box(c) if c is not None else None
        if box is None:
            return ""
        tol = 9 * self._px_per_view()
        for i, (hx, hy) in enumerate(self.box_corners(box)):
            d = math.hypot(x - hx, y - hy)
            if d <= tol:
                return f"corner{i}"
            if d <= tol * 3.2:
                lx, ly = self._local(box, x, y)
                if abs(lx) > box[2] / 2 or abs(ly) > box[3] / 2:
                    return "rotate"
        lx, ly = self._local(box, x, y)
        return "move" if abs(lx) <= box[2] / 2 and abs(ly) <= box[3] / 2 else ""

    def dm_mouse(self, kind: str, p: QPointF, mods: int):
        c = self._dm_target()
        if c is None:
            return
        x, y = self._to_seq(p)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier.value)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier.value)
        if kind == "press":
            hit = self.dm_hit(x, y)
            if not hit:
                other = self.clip_at_point(x, y)               # 別のクリップを押したら、そちらに切り替え
                if other is not None and other.id != c.id:
                    self.dm_enter(other)
                    c, hit = other, "move"
                else:
                    return
            box = self.clip_box(c)
            self.begin_edit()
            self._dm_drag = {"hit": hit, "x": x, "y": y, "px": self._val(c, "pos_x"), "py": self._val(c, "pos_y"),
                             "s": self._val(c, "scale"), "r": self._val(c, "rotation"), "box": box}
        elif kind == "move" and self._dm_drag:
            self._dm_apply(c, x, y, shift, alt)
        elif kind == "release" and self._dm_drag:
            self._dm_apply(c, x, y, shift, alt)
            hit = self._dm_drag["hit"]
            self._dm_drag = None
            self.record_mon.view.snap_lines = []
            self.end_edit({"move": "モニターで位置を変更", "rotate": "モニターで回転"}.get(hit, "モニターで大きさを変更"))
            self.dm_update_overlay()
        elif kind == "escape":
            self.dm_exit()

    def _dm_apply(self, c: M.Clip, x: float, y: float, shift: bool, alt: bool):
        d = self._dm_drag
        seq = self.project.sequence
        hit = d["hit"]
        box = d["box"]
        lines = []
        if hit == "move":
            dx, dy = x - d["x"], y - d["y"]
            if shift:                                       # Shift: 縦か横だけ
                if abs(dx) > abs(dy):
                    dy = 0
                else:
                    dx = 0
            nx, ny = d["px"] + dx, d["py"] + dy
            if not alt:
                nx, ny, lines = self._snap(box, nx - d["px"], ny - d["py"], d["px"], d["py"])
            self._set(c, "pos_x", round(nx, 1))
            self._set(c, "pos_y", round(ny, 1))
        elif hit.startswith("corner"):
            cx, cy = box[0], box[1]
            d0 = math.hypot(d["x"] - cx, d["y"] - cy)
            d1 = math.hypot(x - cx, y - cy)
            k = d1 / max(1e-3, d0)
            ns = max(0.01, d["s"] * k)
            if not alt and abs(ns - 1.0) < 0.03:            # 100% に付く
                ns = 1.0
            self._set(c, "scale", round(ns, 4))
        elif hit == "rotate":
            cx, cy = box[0], box[1]
            a0 = math.degrees(math.atan2(d["y"] - cy, d["x"] - cx))
            a1 = math.degrees(math.atan2(y - cy, x - cx))
            nr = d["r"] + (a1 - a0)
            nr = (nr + 180) % 360 - 180
            if shift:
                nr = round(nr / 15) * 15
            self._set(c, "rotation", round(nr, 1))
        self.record_mon.view.snap_lines = [(k_, v_ / (seq.width if k_ == "v" else seq.height)) for k_, v_ in lines]
        self.preview_changed()
        self.dm_update_overlay()

    def _snap(self, box: Box, dx: float, dy: float, px0: float, py0: float):
        """中心・端・三分割の線にぴったり付ける。戻り値: (pos_x, pos_y, ガイドの線)。"""
        seq = self.project.sequence
        W, H = seq.width, seq.height
        tol = SNAP_PX * self._px_per_view()
        cx, cy, w, h, _r = box
        ncx, ncy = cx + dx, cy + dy
        lines = []
        best_x = None
        for target in (W / 2, W / 3, W * 2 / 3):
            if abs(ncx - target) <= tol:
                best_x = target - cx
                lines.append(("v", target))
                break
        if best_x is None:
            for edge, target in ((ncx - w / 2, 0), (ncx + w / 2, W)):
                if abs(edge - target) <= tol:
                    best_x = dx + (target - edge)
                    lines.append(("v", target))
                    break
        best_y = None
        for target in (H / 2, H / 3, H * 2 / 3):
            if abs(ncy - target) <= tol:
                best_y = target - cy
                lines.append(("h", target))
                break
        if best_y is None:
            for edge, target in ((ncy - h / 2, 0), (ncy + h / 2, H)):
                if abs(edge - target) <= tol:
                    best_y = dy + (target - edge)
                    lines.append(("h", target))
                    break
        return px0 + (best_x if best_x is not None else dx), py0 + (best_y if best_y is not None else dy), lines

    def dm_key(self, dx: int, dy: int):
        """矢印キーで 1px（Shift で 10px）動かす。"""
        c = self._dm_target()
        if c is None:
            return
        self.begin_edit()
        self._set(c, "pos_x", round(self._val(c, "pos_x") + dx, 1))
        self._set(c, "pos_y", round(self._val(c, "pos_y") + dy, 1))
        self.end_edit("モニターで位置を変更（矢印キー）")
        self.preview_changed()
        self.dm_update_overlay()

    # ─────────────────────────── すぐ使う → エフェクト ───────────────────────────
    def _put_fx(self, c: M.Clip, name: str, params: Optional[dict] = None, replace: bool = True):
        fx = [e for e in (c.prop("effects") or []) if not (replace and e.get("name") == name)]
        fx.append({"name": name, "params": dict(params or {}), "on": True})
        c.props["effects"] = fx

    def _drop_fx(self, c: M.Clip, name: str) -> bool:
        before = list(c.prop("effects") or [])
        c.props["effects"] = [e for e in before if e.get("name") != name]
        return len(before) != len(c.props["effects"])

    def _fx_targets(self) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image", "nest", "color", "shape", "text")]

    def add_quick_fx(self, name: str, params: Optional[dict] = None) -> int:
        """選んだクリップに「すぐ使う」のエフェクトを付ける（あとでエフェクトエディターで直せる）。"""
        clips = self._fx_targets()
        if not clips:
            self.status.showMessage("クリップを選んでください", 3000)
            return 0
        from . import fxlib
        self.begin_edit()
        for c in clips:
            self._put_fx(c, name, params)
        self.end_edit(f"{fxlib.get(name).name if fxlib.get(name) else name}（{len(clips)} 個）")
        return len(clips)

    def apply_look(self, name: str) -> int:
        clips = self._fx_targets()
        if not clips:
            QMessageBox.information(self, "ルック", "映像か画像のクリップを選んでください。")
            return 0
        from .fx_quick import LOOK_KINDS
        self.begin_edit()
        for c in clips:
            c.props["effects"] = [e for e in (c.prop("effects") or []) if not e.get("look")]     # 前の版のルックも外す
            if name in LOOK_KINDS:
                self._put_fx(c, "look", {"kind": name, "amount": 1.0})
            else:
                self._drop_fx(c, "look")
        self.end_edit(f"ルック「{name}」（{len(clips)} 個）")
        return len(clips)

    def look_dialog(self):
        from .fx_quick import LOOK_KINDS
        names = LOOK_KINDS + ["（ルックを外す）"]
        n, ok = QInputDialog.getItem(self, "ルック", "色の雰囲気（あとでエフェクトエディターで強さを変えられます）:", names, 3, False)
        if ok:
            self.apply_look(n)

    def key_out(self, name: str) -> int:
        from .fx_quick import KEY_KINDS
        from .features10 import KEY_COLORS
        col, tol = KEY_COLORS.get(name, (KEY_KINDS.get(name, name), 0.25))
        clips = [c for c in self._fx_targets() if c.kind in ("video", "image")]
        self.begin_edit()
        for c in clips:
            c.props.pop("chroma", None)
            self._put_fx(c, "keyout", {"color": col, "tol": tol, "soft": 0.08})
        self.end_edit(f"背景を抜く（{len(clips)} 個）")
        return len(clips)

    def add_mosaic(self, rect=(35.0, 30.0, 30.0, 40.0), block: int = 16) -> int:
        x, y, w, h = rect
        return self.add_quick_fx("q_mosaic", {"size": block, "x": x, "y": y, "w": w, "h": h})

    def shake_range(self, amount: float = 18.0) -> int:
        seq = self.project.sequence
        if 0 <= seq.mark_in < seq.mark_out:
            a, b = seq.mark_in, seq.mark_out
        else:
            a = self.record_player.position
            b = a + int(round(seq.fps * 0.5))
        self.begin_edit()
        clips = self._split_range_visual(a, b)
        for c in clips:
            self._drop_fx(c, "shake")
            self._put_fx(c, "q_shake", {"amount": amount})
            c.props["scale"] = max(float(c.prop("scale", 1.0) or 1.0), 1.06)
        self.end_edit(f"揺れ（{len(clips)} 個）")
        return len(clips)

    def toggle_frame_step(self, step: int = 3) -> int:
        clips = [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "nest")]
        on = not all(any(e.get("name") == "framestep" for e in c.prop("effects") or []) or c.props.get("frame_step") for c in clips)
        self.begin_edit()
        for c in clips:
            c.props.pop("frame_step", None)
            if on:
                self._put_fx(c, "framestep", {"step": step})
            else:
                self._drop_fx(c, "framestep")
        self.end_edit(("カクカク" if on else "カクカクを外す") + f"（{len(clips)} 個）")
        return len(clips)

    def wipe(self, corner: str = "br", size: float = 0.3) -> int:
        n = self.pip(corner, size)
        if not n:
            return 0
        self.begin_edit()
        for c in self._fx_targets():
            c.props["effects"] = [e for e in (c.prop("effects") or []) if e.get("name") not in ("roundrect", "border", "shadow")]
            self._put_fx(c, "wipeframe", {"radius": 6.0, "border": 6.0, "color": "#ffffff", "shadow": True})
        self.end_edit("ワイプにする")
        return n

    def blur_fill(self, blur: float = 25.0) -> int:
        """縦長の写真・動画の左右（上下）を、同じ絵をぼかして埋める（エフェクト「ぼかし背景」）。"""
        clips = [c for c in self._fx_targets() if c.kind in ("video", "image")]
        if not clips:
            QMessageBox.information(self, "ぼかし背景で埋める", "映像か画像のクリップを選んでください。")
            return 0
        self.begin_edit()
        for c in clips:
            self._put_fx(c, "blurfill", {"blur": blur, "dark": 0.25})
        self.end_edit(f"ぼかし背景で埋める（{len(clips)} 個）")
        return len(clips)
