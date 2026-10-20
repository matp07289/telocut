"""MMD / Blender 3D / ノード管理 / 有名ソフト由来の機能 のコマンド（MainWindow のミックスイン）。"""
from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QInputDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QCheckBox,
                             QPlainTextEdit, QListWidget)

from . import model as M
from .settings import SETTINGS


def _row(*ws):
    h = QHBoxLayout()
    for w in ws:
        h.addWidget(w, 1 if isinstance(w, QLineEdit) else 0)
    return h


def _file_row(dlg, edit: QLineEdit, title: str, filt: str):
    b = QPushButton("...")
    b.setFixedWidth(30)
    b.clicked.connect(lambda: (lambda p: edit.setText(p) if p else None)(
        QFileDialog.getOpenFileName(dlg, title, os.path.dirname(edit.text()) if edit.text() else "", filt)[0]))
    return _row(edit, b)


class Features3DMixin:
    def _feature3d_commands(self) -> dict:
        return {
            "node_editor": self.show_node_editor, "add_adjust": self.add_adjustment_layer, "text3d_edit": self.text3d_edit,
            "beat_markers": self.beat_markers, "text3d": self.text3d_dialog, "logo3d": self.logo3d_dialog,
            "mmd_launch": self.launch_mmd, "mmd_lipsync": self.mmd_lipsync_dialog,
            "mmd_render": self.mmd_render_dialog, "mmd_tools_install": self.install_mmd_tools,
        }

    def _init_features3d(self):
        from .nodes import NodeEditor
        self.node_editor = NodeEditor(self)
        self.dock_nodes = self._dock("ノードエディター", self.node_editor, Qt.DockWidgetArea.BottomDockWidgetArea, "nodes")
        self.tabifyDockWidget(self.dock_tl, self.dock_nodes)
        self.dock_nodes.hide()
        self.dock_tl.raise_()
        self.docks.append(self.dock_nodes)
        self.canvas.selection_changed.connect(self._sync_node_editor)
        # キーフレームがあるクリップは、再生ヘッドを動かしたらインスペクターの値も更新
        self._key_timer = QTimer(self)
        self._key_timer.setSingleShot(True)
        self._key_timer.setInterval(120)
        self._key_timer.timeout.connect(self._refresh_keyed_inspector)
        self.record_player.position_changed.connect(lambda _f: None if self.record_player.playing else self._key_timer.start())
        self._jobs3d = []

    def _build_feature3d_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "3D / MMD")
        for c in ("node_editor", "add_adjust", "beat_markers", None, "text3d", "text3d_edit", "logo3d", None,
                  "mmd_launch", "mmd_lipsync", "mmd_render", "mmd_tools_install"):
            m.addSeparator() if c is None else self._a(m, c)

    # ─────────────────────────── ノード ───────────────────────────
    def show_node_editor(self):
        self.dock_nodes.show()
        self.dock_nodes.raise_()
        self._sync_node_editor()
        QTimer.singleShot(50, self.node_editor.fit)

    def _sync_node_editor(self):
        if not hasattr(self, "node_editor"):
            return
        sel = [c for _, c in self.canvas.selected_clips() if c.kind != "audio"]
        cur = self.node_editor.clip_id
        new = sel[0].id if sel else None
        if new != cur:
            self.node_editor.load_clip(sel[0] if sel else None)

    def _refresh_keyed_inspector(self):
        clips = [c for _, c in self.canvas.selected_clips()]
        if clips and any(c.prop("keys") or any(fx.get("keys") for fx in (c.prop("effects") or [])) for c in clips):
            self.inspector.load(clips)

    # ─────────────────────────── 調整レイヤー・ビート ───────────────────────────
    def add_adjustment_layer(self):
        """調整レイヤー（AE / Premiere の調整レイヤー、AviUtl のフィルタオブジェクト）を一番上に置く。"""
        seq = self.project.sequence
        f = self.record_player.position
        seq_in, seq_out = seq.mark_in, seq.mark_out
        a, b = (seq_in, seq_out) if 0 <= seq_in < seq_out else (f, f + int(seq.fps * 5))
        self.begin_edit()
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        c = M.Clip(M.new_id(), "adjust", a, max(1, b - a), name="調整レイヤー", props={"effects": []})
        if SETTINGS.fx_mode_default == "nodes":
            from .nodes import new_graph
            c.props.update({"fx_mode": "nodes", "fx_graph": new_graph()})
        t.clips.append(c)
        self.end_edit("調整レイヤーを追加")
        self.canvas.selected = {c.id}
        self.canvas.selection_changed.emit()
        self.status.showMessage("調整レイヤー: エフェクトを掛けると、下のトラック全体に効きます", 5000)

    def beat_markers(self):
        from .analysis import beats
        from PyQt6.QtWidgets import QInputDialog
        sel = [c for _, c in self.canvas.selected_clips() if c.kind == "audio"]
        if not sel:
            QMessageBox.information(self, "ビート検出", "BGM の音声クリップを選んでください。")
            return
        items = ["1 拍ごと", "2 拍ごと", "小節の頭（4 拍ごと）"]
        ch, ok = QInputDialog.getItem(self, "ビート検出", "マーカーを打つ間隔:", items, 2, False)
        if not ok:
            return
        every = {0: 1, 1: 2, 2: 4}[items.index(ch)]
        seq = self.project.sequence
        c = sel[0]
        m = self.project.media_by_id(c.media_id)
        bpm, frames = beats(m, c, seq.fps, every)
        if not frames:
            QMessageBox.information(self, "ビート検出", "ビートを見つけられませんでした。")
            return
        self.begin_edit()
        for f in frames:
            seq.markers.append(M.Marker(f, f"⚠ ビート {bpm:.0f} BPM", "#3fa9f5"))
        seq.markers.sort(key=lambda mk: mk.frame)
        self.end_edit(f"ビート検出（{bpm:.1f} BPM・{len(frames)} 個）")
        self.status.showMessage(f"{bpm:.1f} BPM。マーカーにスナップしてカットや SE を合わせられます", 6000)

    # ─────────────────────────── Blender 3D ───────────────────────────
    def _blender(self) -> str:
        from .integrations import find_app
        exe = find_app("blender")
        if not exe:
            QMessageBox.warning(self, "Blender", "Blender が見つかりません。環境設定で場所を指定してください。")
        return exe

    def _run_blender(self, script: str, name: str, stem: str, frames: int, factory: bool = True, label: str = "3D"):
        from .blender3d import BlenderJob, place_pending
        from .integrations import work_dir
        exe = self._blender()
        if not exe:
            return None
        d = work_dir(self, "Blender3D")
        prefix = d / f"{stem}_"
        for old in d.glob(f"{stem}_*.mov"):
            try:
                old.unlink()
            except OSError:
                pass
        job = BlenderJob(exe, script.replace("__OUT__", str(prefix)), d / f"{stem}.py", str(prefix) + "*.mov",
                         d / f"{stem}.log", factory=factory)
        seq = self.project.sequence
        dur = max(1, int(round(frames / (30.0 if not factory else seq.fps) * seq.fps)))
        item = place_pending(self, name, str(prefix) + "*.mov", self.record_player.position, dur, d)
        job.done.connect(lambda p: self.status.showMessage(f"{label} のレンダリングが終わりました: {Path(p).name}", 6000))
        job.failed.connect(lambda e: QMessageBox.warning(self, label, e))
        self._jobs3d.append(job)
        job.start()
        self.status.showMessage(f"{label} を Blender でレンダリング中…（終わると自動でつながります）", 8000)
        return item

    def _common_3d_form(self, dlg, f):
        mat = QComboBox()
        from .blender3d import MATERIALS, ANIMS
        mat.addItems(list(MATERIALS))
        anim = QComboBox()
        anim.addItems(ANIMS)
        dur = QDoubleSpinBox()
        dur.setRange(0.5, 60)
        dur.setValue(4.0)
        dur.setSuffix(" 秒")
        q = QComboBox()
        q.addItem("プレビュー（半分の大きさ・速い）", 0.5)
        q.addItem("本番（シーケンスと同じ大きさ）", 1.0)
        f.addRow("材質", mat)
        f.addRow("動き", anim)
        f.addRow("長さ", dur)
        f.addRow("画質", q)
        return mat, anim, dur, q

    def text3d_dialog(self, initial: dict = None, clip: M.Clip = None, wait: bool = False, auto_ok: bool = False):
        """3D テキスト（Blender）。テンプレートから選ぶ・テンプレートとして保存・置いたクリップを直す（clip）。"""
        from .blender3d import MATERIALS, ANIMS
        from . import templates as TP
        p = dict(initial or {})
        dlg = QDialog(self)
        dlg.setWindowTitle("3D テロップを直す（Blender）" if clip is not None else "3D テロップ（Blender）")
        f = QFormLayout(dlg)
        tmpl = QComboBox()
        tmpl.addItem("（テンプレートを選ぶ）", None)
        for t in TP.text3d_templates():
            tmpl.addItem(("★ " if t.get("_user") else "") + t["name"], t)
        f.addRow("テンプレート", tmpl)
        text = QPlainTextEdit(p.get("text", "テロカット"))
        text.setFixedHeight(60)
        font = QLineEdit(p.get("font") or SETTINGS_font_default(self))
        depth = QDoubleSpinBox()
        depth.setRange(0, 2)
        depth.setSingleStep(0.05)
        depth.setValue(float(p.get("depth", 0.2)))
        bevel = QDoubleSpinBox()
        bevel.setRange(0, 0.2)
        bevel.setSingleStep(0.01)
        bevel.setValue(float(p.get("bevel", 0.03)))
        f.addRow("文字", text)
        f.addRow("フォント", _file_row(dlg, font, "フォント", "フォント (*.ttf *.otf *.ttc)"))
        f.addRow("厚み", depth)
        f.addRow("面取り", bevel)
        mat, anim, dur, q = self._common_3d_form(dlg, f)
        if p.get("material") in MATERIALS:
            mat.setCurrentText(p["material"])
        if p.get("anim") in ANIMS:
            anim.setCurrentText(p["anim"])
        if clip is not None:
            dur.setValue(clip.duration / self.project.sequence.fps)
        elif p.get("duration"):
            dur.setValue(float(p["duration"]))
        if p.get("quality") is not None:
            q.setCurrentIndex(max(0, q.findData(float(p["quality"]))))

        def load_tmpl(_i):
            t = tmpl.currentData()
            if not t:
                return
            cur = text.toPlainText()
            tx = t.get("text", "")
            if "{" in tx and cur:
                import re
                tx = re.sub(r"\{[^{}]*\}", cur.replace("\\", "/"), tx)
            text.setPlainText(tx)
            depth.setValue(float(t.get("depth", depth.value())))
            bevel.setValue(float(t.get("bevel", bevel.value())))
            if t.get("material") in MATERIALS:
                mat.setCurrentText(t["material"])
            if t.get("anim") in ANIMS:
                anim.setCurrentText(t["anim"])
            if t.get("duration") and clip is None:
                dur.setValue(float(t["duration"]))
            if t.get("font") and os.path.exists(t["font"]):
                font.setText(t["font"])
        tmpl.currentIndexChanged.connect(load_tmpl)

        def params():
            return {"text": text.toPlainText(), "font": font.text(), "depth": depth.value(), "bevel": bevel.value(),
                    "material": mat.currentText(), "anim": anim.currentText(), "duration": dur.value(),
                    "quality": q.currentData()}

        def save_tmpl():
            from . import license as LIC
            if not LIC.require(self, "template_save"):
                return
            name, ok = QInputDialog.getText(dlg, "テンプレートとして保存", "名前:", text=text.toPlainText()[:16])
            if ok and name.strip():
                TP.save_template("blender3d", {"name": name.strip(), **params()})
                tmpl.addItem("★ " + name.strip(), {"name": name.strip(), **params(), "_user": True})
                self.status.showMessage(f"3D テロップのテンプレート「{name.strip()}」を保存しました", 4000)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        sv = bb.addButton("テンプレートとして保存...", QDialogButtonBox.ButtonRole.ActionRole)
        sv.clicked.connect(save_tmpl)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        self._text3d_dlg = (dlg, tmpl, text, mat, anim, dur, q, params)
        if not auto_ok and (not dlg.exec() or not text.toPlainText().strip()):
            return None
        return self.render_text3d(params(), clip)

    def render_text3d(self, prm: dict, clip: M.Clip = None):
        """3D テロップを Blender でレンダリングして置く（clip を渡すと、そのクリップの中身を差し替える）。"""
        from .blender3d import text3d_script
        seq = self.project.sequence
        k = float(prm.get("quality", 0.5) or 0.5)
        frames = max(1, int(round(float(prm.get("duration", 4)) * seq.fps)))
        sc = text3d_script(prm["text"], prm.get("font", ""), float(prm.get("depth", 0.2)), float(prm.get("bevel", 0.03)),
                           prm.get("material", "金"), prm.get("anim", "なし"), int(seq.width * k) & ~1,
                           int(seq.height * k) & ~1, int(round(seq.fps)), frames, "__OUT__")
        stem = f"text3d_{M.new_id()[:6]}"
        before = {c.id for _, c in seq.all_clips()}
        item = self._run_blender(sc, f"3D_{prm['text'][:10]}.mov", stem, frames, label="3D テロップ")
        if item is None:
            return None
        seq = self.project.sequence
        new = next((c for _, c in seq.all_clips() if c.id not in before and c.media_id == item.id), None)
        self.begin_edit()
        if clip is not None:
            # 直す: 置いてあったクリップの中身だけ差し替え（位置・長さ・エフェクトはそのまま）、仮に置いたクリップは消す
            tgt = seq.find(clip.id)[1]
            if tgt is not None:
                tgt.media_id = item.id
                tgt.name = item.name
                tgt.src_in = 0
                tgt.props["blender_text"] = dict(prm)
            if new is not None:
                tr = seq.find(new.id)[0]
                tr.clips.remove(new)
                if not tr.clips and tr in seq.video_tracks and seq.video_tracks[-1] is tr:
                    seq.video_tracks.remove(tr)
            self.end_edit("3D テロップを直す")
            return tgt
        if new is not None:
            new.props["blender_text"] = dict(prm)
        self.end_edit(None)
        return new

    def text3d_edit(self):
        """選んだ 3D テロップ（Blender）を、作ったときの設定で開いて直す。"""
        clips = [c for _, c in self.canvas.selected_clips() if c.props.get("blender_text")]
        if not clips:
            QMessageBox.information(self, "3D テロップを直す", "Blender で作った 3D テロップのクリップを選んでください。")
            return None
        return self.text3d_dialog(clips[0].props["blender_text"], clips[0])

    def logo3d_dialog(self):
        from .blender3d import logo3d_script
        dlg = QDialog(self)
        dlg.setWindowTitle("3D ロゴ板（Blender）")
        f = QFormLayout(dlg)
        img = QLineEdit()
        sel = [self.project.media_by_id(i) for i in self.bin.selected_ids()]
        sel = [m for m in sel if m and m.kind == "image"]
        if sel:
            img.setText(sel[0].path)
        depth = QDoubleSpinBox()
        depth.setRange(0.01, 1)
        depth.setSingleStep(0.02)
        depth.setValue(0.08)
        f.addRow("画像（PNG のロゴなど）", _file_row(dlg, img, "画像", "画像 (*.png *.jpg *.webp)"))
        f.addRow("厚み", depth)
        mat, anim, dur, q = self._common_3d_form(dlg, f)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec() or not os.path.exists(img.text()):
            return
        seq = self.project.sequence
        k = q.currentData()
        frames = max(1, int(round(dur.value() * seq.fps)))
        sc = logo3d_script(img.text(), depth.value(), mat.currentText(), anim.currentText(), int(seq.width * k) & ~1,
                           int(seq.height * k) & ~1, int(round(seq.fps)), frames, "__OUT__")
        self._run_blender(sc, f"3Dロゴ_{Path(img.text()).stem[:10]}.mov", f"logo3d_{M.new_id()[:6]}", frames,
                          label="3D ロゴ")

    # ─────────────────────────── MMD ───────────────────────────
    def launch_mmd(self):
        from .mmd import launch_mmd, find_mmd
        exe = find_mmd()
        if not exe:
            p, _ = QFileDialog.getOpenFileName(self, "MikuMikuDance.exe の場所", "", "MikuMikuDance (MikuMikuDance.exe)")
            if not p:
                return
            SETTINGS.mmd_exe = p
            SETTINGS.save()
            exe = p
        launch_mmd(exe)
        self.status.showMessage(f"MMD を起動しました（{exe}）。AVI 出力を監視フォルダに保存すると自動で取り込めます", 7000)

    def _voice_tracks(self):
        from .voiceengine import mouth_path
        seq = self.project.sequence
        out = []
        for i, t in enumerate(seq.audio_tracks):
            n = 0
            for c in t.clips:
                m = self.project.media_by_id(c.media_id) if c.media_id else None
                if m is not None and os.path.exists(mouth_path(m.path)):
                    n += 1
            if n:
                spk = next((c.props.get("speaker") for c in t.clips if c.props.get("speaker")), "")
                out.append((i, f"{t.name}（{spk or 'セリフ'}・{n} 個）"))
        return out

    def mmd_lipsync_dialog(self):
        from .mmd import lipsync_from_sequence, write_vmd
        tracks = self._voice_tracks()
        if not tracks:
            QMessageBox.information(self, "MMD 口パク",
                                    "VOICEVOX 系で合成したセリフ（母音タイムライン付き）がありません。\n"
                                    "セリフパネルか台本から生成で音声を作ってください。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("セリフから MMD の口パク（VMD）を作る")
        f = QFormLayout(dlg)
        tr = QComboBox()
        for i, lab in tracks:
            tr.addItem(lab, i)
        model = QLineEdit("初音ミク")
        rng = QComboBox()
        rng.addItems(["シーケンスの最初から", "IN 点から（IN〜OUT）"])
        strength = QDoubleSpinBox()
        strength.setRange(0.1, 1.0)
        strength.setValue(1.0)
        f.addRow("セリフのトラック", tr)
        f.addRow("モデル名（VMD に書く名前）", model)
        f.addRow("0 フレーム目", rng)
        f.addRow("口の開き", strength)
        f.addRow(QLabel("表情「あ・い・う・え・お」を動かします（MMD は 30 fps）。\n"
                        "MMD でモデルを選んでから「ファイル → モーションデータ読込」で読み込んでください。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        seq = self.project.sequence
        a, b = (seq.mark_in, seq.mark_out) if rng.currentIndex() == 1 and 0 <= seq.mark_in < seq.mark_out else (0, 0)
        keys, n = lipsync_from_sequence(self.project, seq, tr.currentData(), a, b, strength.value())
        if not keys:
            QMessageBox.information(self, "MMD 口パク", "口パクにするセリフが範囲内にありません。")
            return
        p, _ = QFileDialog.getSaveFileName(self, "VMD を保存", f"{seq.name}_口パク.vmd", "VMD (*.vmd)")
        if not p:
            return
        write_vmd(p, model.text().strip() or "model", keys)
        self.status.showMessage(f"口パク VMD を保存しました（セリフ {n} 個・キー {len(keys)} 個）: {p}", 7000)

    def install_mmd_tools(self):
        from .mmd import has_mmd_tools, install_mmd_tools
        exe = self._blender()
        if not exe:
            return
        if has_mmd_tools(exe):
            QMessageBox.information(self, "MMD Tools", "MMD Tools はすでに入っています。")
            return True
        if QMessageBox.question(self, "MMD Tools",
                                "Blender に MMD Tools（PMX / VMD を読み込む拡張機能）を入れます。\n"
                                "Blender 公式の拡張機能サイト（extensions.blender.org）から取得します。よろしいですか？") \
                != QMessageBox.StandardButton.Yes:
            return False
        from PyQt6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, log = install_mmd_tools(exe)
        finally:
            QApplication.restoreOverrideCursor()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "MMD Tools", "入れました。" if ok else "入れられませんでした:\n" + log)
        return ok

    def mmd_render_dialog(self):
        from .mmd import has_mmd_tools, guess_frames, render_script, lipsync_from_sequence, write_vmd, find_mmd, \
            mmd_user_dirs
        exe = self._blender()
        if not exe:
            return
        if not has_mmd_tools(exe) and not self.install_mmd_tools():
            return
        dirs = mmd_user_dirs(find_mmd()) if find_mmd() else {}
        dlg = QDialog(self)
        dlg.setWindowTitle("MMD のモデルとモーションを Blender でレンダリング")
        f = QFormLayout(dlg)
        pmx = QLineEdit(self.settings.value("mmd/pmx", ""))
        motion = QLineEdit(self.settings.value("mmd/motion", ""))
        cam = QLineEdit(self.settings.value("mmd/camera", ""))
        f.addRow("モデル（PMX / PMD）", _file_row(dlg, pmx, "モデル", "MMD モデル (*.pmx *.pmd)"))
        f.addRow("モーション（VMD）", _file_row(dlg, motion, "モーション", "VMD (*.vmd)"))
        f.addRow("カメラ（VMD・任意）", _file_row(dlg, cam, "カメラ", "VMD (*.vmd)"))
        lip = QComboBox()
        lip.addItem("口パクしない", -1)
        for i, lab in self._voice_tracks():
            lip.addItem(f"口パク: {lab}", i)
        f.addRow("セリフに合わせて口パク", lip)
        frames = QSpinBox()
        frames.setRange(1, 100000)
        frames.setSuffix(" フレーム（30 fps）")
        frames.setSpecialValueText("モーションの長さ")
        q = QComboBox()
        q.addItem("プレビュー（半分の大きさ・速い）", 0.5)
        q.addItem("本番（シーケンスと同じ大きさ）", 1.0)
        f.addRow("長さ", frames)
        f.addRow("画質", q)
        if dirs:
            f.addRow(QLabel(f"MMD の素材フォルダ: {Path(dirs['Model']).parent}"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec() or not os.path.exists(pmx.text()):
            return
        for k, w in (("pmx", pmx), ("motion", motion), ("camera", cam)):
            self.settings.setValue(f"mmd/{k}", w.text())
        seq = self.project.sequence
        motions = [motion.text()] if os.path.exists(motion.text()) else []
        n = frames.value() or guess_frames(motions, cam.text() if os.path.exists(cam.text()) else "") or 150
        lip_vmd = ""
        if lip.currentData() is not None and lip.currentData() >= 0:
            from .integrations import work_dir
            f0 = self.record_player.position
            keys, cnt = lipsync_from_sequence(self.project, seq, lip.currentData(), f0,
                                              f0 + int(n / 30 * seq.fps))
            if keys:
                lip_vmd = str(work_dir(self, "Blender3D") / f"lipsync_{M.new_id()[:6]}.vmd")
                write_vmd(lip_vmd, "model", keys)
        k = q.currentData()
        sc = render_script(pmx.text(), motions, cam.text() if os.path.exists(cam.text()) else "", lip_vmd, "__OUT__",
                           int(seq.width * k) & ~1, int(seq.height * k) & ~1, n)
        self._run_blender(sc, f"MMD_{Path(pmx.text()).stem[:12]}.mov", f"mmd_{M.new_id()[:6]}", n, factory=False,
                          label="MMD")


def SETTINGS_font_default(mw) -> str:
    """3D テキストの既定フォント: テロップの共通デザインのフォント → 日本語の太字フォント。"""
    p = (mw.project.design or {}).get("font_path", "")
    if p and os.path.exists(p):
        return p
    for c in ("C:/Windows/Fonts/meiryob.ttc", "C:/Windows/Fonts/YuGothB.ttc", "C:/Windows/Fonts/msgothic.ttc"):
        if os.path.exists(c):
            return c
    return ""
