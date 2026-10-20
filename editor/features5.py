"""After Effects / Blender 風の機能（MainWindow のミックスイン）。

モーションパネル・グラフエディター・ヌル・カメラ・図形・3D レイヤー・カメラワークのプリセット・
モーショントラッキング・親子付け。Dynamic Link 風の連携・更新履歴・制作管理は dynlink.py / production.py。
"""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from . import model as M


class Features5Mixin:
    def _feature5_commands(self) -> dict:
        return {
            "motion_panel": lambda: self.show_dock(self.dock_motion),
            "graph_editor": self.show_graph_editor,
            "add_null": self.add_null, "add_camera": self.add_camera, "add_shape_layer": self.add_shape_dialog,
            "make_3d": self.toggle_3d, "camera_move": self.camera_move_dialog,
            "track_motion": self.start_tracking, "parent_to_null": self.parent_to_null,
            "unparent": self.unparent,
        }

    def _init_features5(self):
        from .motionpanel import GraphEditor, MotionPanel
        R = Qt.DockWidgetArea.RightDockWidgetArea
        self.motion_panel = MotionPanel(self)
        self.dock_motion = self._dock("モーション（AE 風）", self.motion_panel, R, "motion")
        self.motion_panel.setMinimumWidth(240)
        self.tabifyDockWidget(self.dock_insp, self.dock_motion)
        self.dock_motion.hide()
        self.graph_editor = GraphEditor(self)
        self.dock_graph = self._dock("グラフエディター", self.graph_editor, Qt.DockWidgetArea.BottomDockWidgetArea, "graph")
        self.tabifyDockWidget(self.dock_tl, self.dock_graph)
        self.dock_graph.hide()
        self.dock_tl.raise_()
        self.docks += [self.dock_motion, self.dock_graph]
        self._track_mode = False

    def _build_feature5_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "モーション")
        for c in ("motion_panel", "graph_editor", None, "add_shape_layer", "add_null", "add_camera", "make_3d",
                  "camera_move", None, "track_motion", "parent_to_null", "unparent"):
            m.addSeparator() if c is None else self._a(m, c)

    # ─────────────────────────── 共通 ───────────────────────────
    def show_graph_editor(self):
        self.dock_graph.show()
        self.dock_graph.raise_()
        self.graph_editor.canvas.update()

    def _range_here(self, sec: float = 5.0):
        seq = self.project.sequence
        if 0 <= seq.mark_in < seq.mark_out:
            return seq.mark_in, seq.mark_out
        f = self.record_player.position
        return f, f + int(seq.fps * sec)

    def _new_top_track(self) -> M.Track:
        seq = self.project.sequence
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        return t

    def _free_track(self, a: int, b: int) -> M.Track:
        for t in reversed(self.project.sequence.video_tracks):
            if not t.locked and t.is_empty(a, b):
                return t
        return self._new_top_track()

    def _select(self, clips):
        self.canvas.selected = {c.id for c in clips}
        self.canvas.selection_changed.emit()
        self.canvas.update()

    # ─────────────────────────── ヌル・カメラ・図形 ───────────────────────────
    def add_null(self, name: str = "ヌル") -> M.Clip:
        a, b = self._range_here()
        self.begin_edit()
        c = M.Clip(M.new_id(), "null", a, b - a, name=name, props={})
        self._new_top_track().clips.append(c)
        self.end_edit("ヌルを追加")
        self._select([c])
        self.status.showMessage("ヌル: ほかのクリップの「親」にすると、まとめて動かせます（モーションパネル）", 5000)
        return c

    def add_camera(self) -> M.Clip:
        a, b = self._range_here(10)
        self.begin_edit()
        c = M.Clip(M.new_id(), "camera", a, b - a, name="カメラ", props={"zoom": 1.0, "pos_z": 0.0})
        self._new_top_track().clips.append(c)
        self.end_edit("カメラを追加")
        self._select([c])
        self.status.showMessage("カメラ: 「3D レイヤー」にしたクリップだけが、カメラの前後・パン・ズームで遠近が付きます", 6000)
        return c

    def add_shape_dialog(self):
        from .motionpanel import SHAPES
        names = [lab for _k, lab in SHAPES[:7]]
        n, ok = QInputDialog.getItem(self, "図形を追加", "種類:", names, 2, False)
        if ok:
            self.add_shape_layer(SHAPES[names.index(n)][0])

    def add_shape_layer(self, shape: str = "ellipse", color: str = "#FFD257") -> M.Clip:
        a, b = self._range_here()
        rect = {"line": [20, 50, 60, 0], "arrow": [25, 60, 40, -20], "band": [0, 78, 100, 22]}.get(shape, [35, 30, 30, 40])
        self.begin_edit()
        c = M.Clip(M.new_id(), "shape", a, b - a, name=f"図形（{shape}）",
                   props={"shape": shape, "color": color, "rect": rect, "fill_opacity": 1.0,
                          "stroke_width": 8 if shape in ("line", "arrow") else 0, "stroke_color": "#FFFFFF",
                          "sides": 5 if shape == "star" else 6})
        M.place_clips(self.project.sequence, [(self._free_track(a, b), c)])
        self.end_edit("図形を追加")
        self._select([c])
        self.show_dock(self.dock_motion)
        return c

    def toggle_3d(self) -> int:
        clips = [c for _, c in self.canvas.selected_clips() if c.kind not in ("audio", "camera")]
        if not clips:
            QMessageBox.information(self, "3D レイヤー", "クリップを選んでください。")
            return 0
        on = not all(c.props.get("layer3d") for c in clips)
        self.begin_edit()
        for c in clips:
            if on:
                c.props["layer3d"] = True
            else:
                c.props.pop("layer3d", None)
        self.end_edit("3D レイヤー " + ("オン" if on else "オフ"))
        return len(clips)

    CAMERA_MOVES = [("ドリーイン（ゆっくり寄る）", {"pos_z": [0, 500]}), ("ドリーアウト（引く）", {"pos_z": [500, 0]}),
                    ("左から右へパン", {"pos_x": [-250, 250]}), ("下から上へ（クレーン）", {"pos_y": [200, -200]}),
                    ("回り込み（ロール）", {"rotation": [-8, 8]}), ("ズームイン", {"zoom": [1.0, 1.6]})]

    def camera_move_dialog(self):
        names = [n for n, _ in self.CAMERA_MOVES]
        n, ok = QInputDialog.getItem(self, "カメラワーク", "動き（選んだクリップを 3D にして、カメラを足します）:", names, 0, False)
        if ok:
            self.camera_move(names.index(n))

    def camera_move(self, idx: int) -> M.Clip:
        """カメラワークのプリセット: カメラを足してキーフレームを打ち、選んだクリップを 3D レイヤーにする。"""
        name, mv = self.CAMERA_MOVES[idx]
        sel = [c for _, c in self.canvas.selected_clips() if c.kind not in ("audio", "camera")]
        a, b = (min(c.start for c in sel), max(c.end for c in sel)) if sel else self._range_here(5)
        self.begin_edit()
        for c in sel:
            c.props["layer3d"] = True
        keys = {k: [[0, v0, "ease_in_out"], [b - a - 1, v1, "ease_in_out"]] for k, (v0, v1) in mv.items()}
        cam = M.Clip(M.new_id(), "camera", a, b - a, name=f"カメラ（{name}）", props={"zoom": 1.0, "pos_z": 0.0, "keys": keys})
        self._new_top_track().clips.append(cam)
        self.end_edit(f"カメラワーク: {name}")
        return cam

    # ─────────────────────────── 親子付け ───────────────────────────
    def parent_to_null(self) -> Optional[M.Clip]:
        """選んだクリップをヌルの子にする（まとめて動かす・トラッキングで追従させる用）。"""
        sel = [c for _, c in self.canvas.selected_clips() if c.kind not in ("audio", "camera", "null")]
        if not sel:
            QMessageBox.information(self, "ヌルにまとめる", "まとめて動かしたいクリップを選んでください。")
            return None
        a, b = min(c.start for c in sel), max(c.end for c in sel)
        self.begin_edit()
        n = M.Clip(M.new_id(), "null", a, b - a, name="ヌル（まとめ）", props={})
        self._new_top_track().clips.append(n)
        for c in sel:
            c.props["parent"] = n.id
        self.end_edit(f"ヌルにまとめる（{len(sel)} 個）")
        self._select([n])
        return n

    def unparent(self) -> int:
        sel = [c for _, c in self.canvas.selected_clips() if c.props.get("parent")]
        self.begin_edit()
        for c in sel:
            c.props.pop("parent", None)
        self.end_edit(f"親子付けを外す（{len(sel)} 個）")
        return len(sel)

    # ─────────────────────────── トラッキング ───────────────────────────
    def start_tracking(self):
        """モニターで範囲を囲む → その範囲を追ったヌルを作る（選んだクリップはヌルの子にして追従させる）。"""
        self._track_mode = True
        self.set_drag_zoom(True)
        self.status.showMessage("トラッキング: レコードモニターで、追いかけたい所（はっきりした模様・角）を小さめに囲んでください", 8000)

    def track_from_rect(self, rect01, end: Optional[int] = None, targets: Optional[List[M.Clip]] = None,
                        wait: bool = False):
        """rect01 = (x, y, w, h)（0〜1）。再生ヘッドから end（OUT・最大 30 秒）まで追う。"""
        from .media import DecoderPool
        from .render import render_frame_np
        from .tracker import smooth, track_region
        seq = self.project.sequence
        start = self.record_player.position
        if end is None:
            end = seq.mark_out if seq.mark_out > start else min(seq.duration, start + int(seq.fps * 30))
        if end <= start + 1:
            QMessageBox.information(self, "トラッキング", "追う区間がありません（OUT を再生ヘッドより後ろに）。")
            return None
        if targets is None:
            targets = [c for _, c in self.canvas.selected_clips() if c.kind not in ("audio", "camera")]
        sc = min(1.0, 640.0 / max(1, seq.width))
        snap = M.Project.from_json(self.project.to_json()).sequence      # 追っている間に編集しても影響しない
        proj = self.project

        def work(task, *_a):
            pool = DecoderPool()
            fr = lambda f: render_frame_np(snap, f, proj, pool, sc)
            pts = track_region(fr, start, end, rect01, progress=lambda i, n: task.progress.emit(i, n, f"{i} / {n} フレーム"),
                               cancelled=lambda: task.cancelled)
            return smooth(pts, 1)

        def done(pts):
            self._apply_track(pts, start, sc, targets)

        if wait:
            class _T:
                cancelled = False

                class progress:
                    @staticmethod
                    def emit(*_a):
                        pass
            pts = work(_T())
            return self._apply_track(pts, start, sc, targets)
        self._run_task("トラッキング", work, on_done=done)
        return None

    def _apply_track(self, pts, start, sc, targets):
        if len(pts) < 2:
            QMessageBox.information(self, "トラッキング", "追えませんでした。はっきりした模様や角を、小さめに囲んでください。")
            return None
        x0, y0 = pts[0][1], pts[0][2]
        keys = {"pos_x": [[f - start, round((x - x0) / sc, 2), "linear"] for f, x, y, _s in pts],
                "pos_y": [[f - start, round((y - y0) / sc, 2), "linear"] for f, x, y, _s in pts]}
        last = pts[-1][0]
        self.begin_edit()
        n = M.Clip(M.new_id(), "null", start, max(1, last - start + 1), name="トラック（追従）", props={"keys": keys})
        self._new_top_track().clips.append(n)
        for c in targets or []:
            if c.kind not in ("null", "camera"):
                c.props["parent"] = n.id
        self.end_edit(f"トラッキング（{len(pts)} フレーム）")
        lost = last + 1 < (self.project.sequence.mark_out if self.project.sequence.mark_out > start else last + 1)
        self.status.showMessage(f"トラッキング: {len(pts)} フレーム追いました" + ("（途中で見失いました）" if lost else "")
                                + ("。選んだクリップが付いていきます" if targets else "。クリップの「親」にこのヌルを選ぶと付いていきます"), 8000)
        return n
