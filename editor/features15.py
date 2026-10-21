"""キャラクター（表情の切り替え・呼吸 / 揺れ）・MMD の身振り手振り・MME 風エフェクト・Blackmagic から収録・
YouTube 公開まで自動・メニューの並べ直し（MainWindow のミックスイン）。"""
from __future__ import annotations

import copy
import os
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
from PyQt6.QtCore import QDateTime, Qt, QTimer
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                             QPushButton, QSpinBox, QVBoxLayout)

from . import model as M
from .settings import SETTINGS


def _checklist(items: List[str], checked=None) -> QListWidget:
    lw = QListWidget()
    for t in items:
        it = QListWidgetItem(t)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Checked if (checked is None or t in checked) else Qt.CheckState.Unchecked)
        lw.addItem(it)
    lw.setMaximumHeight(130)
    return lw


def _checked(lw: QListWidget) -> List[str]:
    return [lw.item(i).text() for i in range(lw.count()) if lw.item(i).checkState() == Qt.CheckState.Checked]


class Features15Mixin:
    def _feature15_commands(self) -> dict:
        return {"char_expression": self.char_expression_dialog, "char_idle": self.char_idle_dialog,
                "mmd_gesture": self.mmd_gesture_dialog, "mme_fx": self.mme_fx_dialog,
                "bmd_capture": self.bmd_capture_dialog, "yt_publish": self.yt_publish_dialog}

    def _build_feature15_menus(self):
        # 項目は menulayout.py が並べる（ここでは新しいメニューを作らない）
        holder = self._menu_named("YouTube")
        if holder is not None:
            for c in ("char_expression", "char_idle", "mmd_gesture", "mme_fx", "bmd_capture", "yt_publish"):
                self._a(holder, c)
        from . import menulayout as ML
        try:
            self._menu_layout_result = ML.apply(self)
        except Exception as ex:                 # 並べ直しに失敗しても元のメニューで使える
            self._menu_layout_result = {"error": str(ex)}

    def _sel_chars(self) -> List[M.Clip]:
        return [c for _t, c in self.canvas.selected_clips() if c.kind == "character"]

    # ═════════════════════════════════════════
    #  立ち絵: 表情の切り替え・呼吸 / 揺れ
    # ═════════════════════════════════════════
    def char_family(self, ch: dict) -> str:
        return ch.get("family") or ch.get("name", "")

    def set_char_expression(self, clip: M.Clip, char_id: str, from_playhead: bool = False) -> M.Clip:
        """立ち絵のクリップの表情（キャラクター）を変える。from_playhead なら再生ヘッドで分けて後ろだけ。"""
        seq = self.project.sequence
        f = self.record_player.position
        self.begin_edit()
        target = clip
        if from_playhead and clip.start < f < clip.end:
            t = next(t for t in seq.video_tracks if clip in t.clips)
            right = clip.copy(id=M.new_id(), start=f, duration=clip.end - f)
            clip.duration = f - clip.start
            t.clips.append(right)
            t.sort()
            target = right
        target.props["char_id"] = char_id
        ch = self.project.character(char_id) or {}
        self.end_edit(f"立ち絵の表情: {ch.get('expression') or ch.get('name', '')}")
        return target

    def char_expression_dialog(self):
        clips = self._sel_chars()
        if not clips:
            QMessageBox.information(self, "立ち絵の表情", "タイムラインで立ち絵のクリップを選んでください。")
            return
        cur = self.project.character(clips[0].prop("char_id", "")) or {}
        fam = self.char_family(cur)
        chars = sorted(self.project.characters, key=lambda c: (self.char_family(c) != fam, c.get("name", "")))
        dlg = QDialog(self)
        dlg.setWindowTitle("立ち絵の表情を切り替える")
        f = QFormLayout(dlg)
        cb = QComboBox()
        for c in chars:
            mark = "★ " if self.char_family(c) == fam else ""
            cb.addItem(mark + (c.get("name", "") + (f"（{c['expression']}）" if c.get("expression") and c["expression"] not in c.get("name", "") else "")),
                       c["id"])
        cb.setCurrentIndex(max(0, cb.findData(cur.get("id"))))
        f.addRow("表情（★ = 同じキャラクター）", cb)
        ph = QCheckBox("再生ヘッドから後ろだけ変える（そこでクリップを分ける）")
        f.addRow(ph)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        for c in clips:
            self.set_char_expression(c, cb.currentData(), ph.isChecked())

    def char_idle_dialog(self):
        clips = self._sel_chars() or [c for t in self.project.sequence.video_tracks for c in t.clips if c.kind == "character"]
        if not clips:
            QMessageBox.information(self, "立ち絵の動き", "立ち絵のクリップがありません。")
            return
        c0 = clips[0]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"立ち絵の動き（{len(clips)} 個）")
        f = QFormLayout(dlg)
        br = QCheckBox("呼吸（ゆっくり縦に伸び縮み）")
        br.setChecked(bool(c0.prop("breath", False)))
        ba = QDoubleSpinBox()
        ba.setRange(0.2, 4.0)
        ba.setSingleStep(0.1)
        ba.setValue(float(c0.prop("breath_amp", 1.0)))
        ba.setSuffix(" %")
        sw = QCheckBox("体の揺れ（足元を中心にゆっくり）")
        sw.setChecked(bool(c0.prop("sway", False)))
        sd = QDoubleSpinBox()
        sd.setRange(0.2, 6.0)
        sd.setSingleStep(0.1)
        sd.setValue(float(c0.prop("sway_deg", 1.2)))
        sd.setSuffix(" °")
        bo = QCheckBox("話すときに弾む")
        bo.setChecked(bool(c0.prop("bounce", True)))
        for w, lab in ((br, None), (ba, "呼吸の大きさ"), (sw, None), (sd, "揺れの大きさ"), (bo, None)):
            f.addRow(w) if lab is None else f.addRow(lab, w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        self.set_char_idle(clips, br.isChecked(), sw.isChecked(), ba.value(), sd.value(), bo.isChecked())

    def set_char_idle(self, clips, breath: bool, sway: bool, breath_amp: float = 1.0, sway_deg: float = 1.2,
                      bounce: Optional[bool] = None):
        self.begin_edit()
        for c in clips:
            c.props.update({"breath": breath, "sway": sway, "breath_amp": breath_amp, "sway_deg": sway_deg})
            if bounce is not None:
                c.props["bounce"] = bounce
        self.end_edit(f"立ち絵の動き（{len(clips)} 個）")

    # ═════════════════════════════════════════
    #  MME 風エフェクト
    # ═════════════════════════════════════════
    def apply_mme_preset(self, name: str, clips: Optional[List[M.Clip]] = None) -> int:
        from .fx_mme import MME_PRESETS
        if clips is None:
            clips = [c for _t, c in self.canvas.selected_clips() if c.kind in ("video", "image", "character", "nest")]
        if not clips:
            return 0
        self.begin_edit()
        for c in clips:
            fx = [e for e in (c.prop("effects") or []) if not str(e.get("name", "")).startswith("mme_")]
            for fid, prm in MME_PRESETS.get(name, []):
                fx.append({"name": fid, "params": dict(prm), "on": True})
            c.props["effects"] = fx
        self.end_edit(f"MME 風: {name}（{len(clips)} 個）")
        return len(clips)

    def mme_fx_dialog(self):
        from .fx_mme import MME_PRESETS
        clips = [c for _t, c in self.canvas.selected_clips() if c.kind in ("video", "image", "character", "nest")]
        if not clips:
            QMessageBox.information(self, "MME 風エフェクト", "MMD の映像・立ち絵のクリップを選んでください（そのクリップだけに掛かります）。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("MME 風エフェクト（選んだクリップだけ）")
        f = QFormLayout(dlg)
        cb = QComboBox()
        cb.addItems(list(MME_PRESETS) + ["（MME 風を外す）"])
        f.addRow("プリセット", cb)
        f.addRow(QLabel("セル調・輪郭線・ディフュージョン・リムライト・漫画 を組み合わせます。\n"
                        "あとでエフェクトエディターで細かく直せます（MME 風（アニメ調）の分類）。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.apply_mme_preset(cb.currentText(), clips)

    # ═════════════════════════════════════════
    #  MMD をセリフに合わせて動かす（身振り手振り・口パク）
    # ═════════════════════════════════════════
    def mmd_gesture_dialog(self, pmx: str = ""):
        from . import mmdmotion as MM
        from . import mmdtachie as MT
        from .fx_mme import MME_PRESETS
        exe = MT.find_blender()
        if not exe:
            QMessageBox.warning(self, "MMD を動かす", "Blender（MMD Tools 入り）が見つかりません。")
            return None
        seq = self.project.sequence
        dlg = QDialog(self)
        dlg.setWindowTitle("MMD をセリフに合わせて動かす（身振り手振り・口パク）")
        f = QFormLayout(dlg)
        src = QLineEdit(pmx or getattr(SETTINGS, "mmd_last_model", ""))
        b = QPushButton("…")
        r = QHBoxLayout()
        r.addWidget(src, 1)
        r.addWidget(b)
        f.addRow("モデル（PMX / .blend）", r)
        b.clicked.connect(lambda: src.setText(QFileDialog.getOpenFileName(dlg, "MMD のモデル", src.text(),
                                                                          "MMD (*.pmx *.pmd);;Blender (*.blend)")[0] or src.text()))
        track = QComboBox()
        for i, t in enumerate(seq.audio_tracks):
            track.addItem(f"{t.name}（{len(t.clips)} クリップ）", i)
        sel_a = next((i for i, t in enumerate(seq.audio_tracks) if t.selected and t.clips), 0)
        track.setCurrentIndex(sel_a)
        f.addRow("声の音声トラック", track)
        rng = QComboBox()
        rng.addItems(["その音声トラックの声の範囲", "IN〜OUT", "シーケンス全体"])
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            rng.setCurrentIndex(1)
        f.addRow("範囲", rng)
        expr = QComboBox()
        expr.addItems(list(MT.EXPRESSIONS))
        f.addRow("表情", expr)
        pose = QComboBox()
        pose.addItems(list(MT.POSES))
        pose.setCurrentIndex(1)
        f.addRow("基本のポーズ", pose)
        frame = QComboBox()
        frame.addItems(MT.FRAMINGS)
        frame.setCurrentText("膝上")
        f.addRow("構図", frame)
        height = QSpinBox()
        height.setRange(360, 2160)
        height.setSingleStep(120)
        height.setValue(min(1080, seq.height))
        height.setSuffix(" px")
        f.addRow("高さ", height)
        gl = _checklist(list(MM.GESTURES))
        f.addRow("使う身振り", gl)
        stren = QDoubleSpinBox()
        stren.setRange(0.2, 1.5)
        stren.setSingleStep(0.1)
        stren.setValue(1.0)
        f.addRow("身振りの大きさ", stren)
        nod = QCheckBox("強く言った所でうなずく")
        nod.setChecked(True)
        sway = QCheckBox("体の揺れ・呼吸")
        sway.setChecked(True)
        blink = QCheckBox("まばたき")
        blink.setChecked(True)
        outline = QCheckBox("輪郭線（Blender）")
        rr = QHBoxLayout()
        for w in (nod, sway, blink, outline):
            rr.addWidget(w)
        f.addRow(rr)
        mme = QComboBox()
        mme.addItems(["（なし）"] + list(MME_PRESETS))
        f.addRow("MME 風エフェクト", mme)
        est = QLabel("")
        est.setStyleSheet("color: gray;")
        f.addRow(est)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("作る（Blender でレンダリング）")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)

        def span():
            ti = track.currentData()
            if rng.currentIndex() == 1 and seq.mark_out > seq.mark_in >= 0:
                return seq.mark_in, seq.mark_out
            if rng.currentIndex() == 0 and ti < len(seq.audio_tracks) and seq.audio_tracks[ti].clips:
                cl = seq.audio_tracks[ti].clips
                return min(c.start for c in cl), max(c.end for c in cl)
            return 0, max(1, seq.duration)

        def upd(*_a):
            a, bnd = span()
            sec = (bnd - a) / seq.fps
            est.setText(f"{sec:.1f} 秒・{bnd - a} コマ（目安: レンダリング {max(1, int(sec * seq.fps * 0.25 / 60) + 1)} 分ほど）")
        track.currentIndexChanged.connect(upd)
        rng.currentIndexChanged.connect(upd)
        upd()
        if not dlg.exec():
            return None
        a, bnd = span()
        SETTINGS.mmd_last_model = src.text().strip()
        opts = {"pmx": src.text().strip(), "track": track.currentData(), "a": a, "b": bnd, "expression": expr.currentText(),
                "pose": pose.currentText(), "framing": frame.currentText(), "height": height.value(),
                "gestures": _checked(gl), "strength": stren.value(), "nod": nod.isChecked(), "sway": sway.isChecked(),
                "blink": blink.isChecked(), "outline": outline.isChecked(),
                "mme": "" if mme.currentIndex() == 0 else mme.currentText()}
        return self.run_mmd_gesture(opts)

    def run_mmd_gesture(self, o: dict, on_done=None):
        """o: pmx / track / a / b / expression / pose / framing / height / gestures / strength / nod / sway / blink / outline / mme"""
        from . import mmdmotion as MM
        from . import mmdtachie as MT
        exe = MT.find_blender()
        seq = self.project.sequence
        proj = copy.deepcopy(self.project)
        s2 = proj.sequence
        for i, t in enumerate(s2.audio_tracks):          # 声のトラックだけ残す
            if i != o["track"]:
                t.clips = []
        for t in s2.video_tracks:
            t.clips = []
        vowels = MM.vowels_for_range(self.project, seq, o["track"], o["a"], o["b"])
        out_dir = os.path.join(MT._work_dir(), "anim_" + time.strftime("%Y%m%d_%H%M%S"))
        cache = getattr(self, "_mmd_probe_cache", {})
        self._mmd_probe_cache = cache
        pmx = o["pmx"]
        blend = pmx if pmx.lower().endswith(".blend") else ""

        def work(task):
            import tempfile
            from .smartrender import write_audio
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                task.progress.emit(0, 0, "声を読み込み中…")
                write_audio(proj, s2, o["a"], o["b"], wav)
                n = o["b"] - o["a"]
                db = MM.loudness_db(wav, seq.fps, n)
            finally:
                try:
                    os.remove(wav)
                except OSError:
                    pass
            if pmx not in cache:
                task.progress.emit(0, 0, "モデルを調べています…")
                cache[pmx] = MT.probe(exe, "" if blend else pmx, blend)
            info = cache[pmx]
            P = MT.render_params("" if blend else pmx, info["morphs"], o["expression"], o["pose"], framing=o["framing"],
                                 height=o["height"], outline=o["outline"], blend=blend, out_dir=out_dir)
            plan = MM.plan(db, seq.fps, P["pose"], P["roles"], seed=int(time.time()) % 1000, strength=o["strength"],
                           gestures=o["gestures"], vowels=vowels, nod=o["nod"], sway=o["sway"], blink=o["blink"])
            P["anim"] = {k: plan[k] for k in ("frames", "fps", "bone_keys", "morph_keys")}
            P["states"] = {}
            task.progress.emit(0, n, f"Blender でレンダリング（身振り {len(plan['gestures'])} 回・{len(plan['phrases'])} フレーズ）")
            mov = MT.render_animation(exe, P, progress=lambda s: task.progress.emit(int(s.split("/")[0]), n, f"{s} コマ"),
                                      cancel=lambda: task.cancelled)
            return mov, plan

        def done(res):
            mov, plan = res
            clip = self._place_mmd_movie(mov, o["a"], o.get("mme", ""))
            self.status.showMessage(f"MMD を動かしました: 身振り {len(plan['gestures'])} 回・うなずき・口パク（{Path(mov).name}）", 8000)
            if on_done:
                on_done(clip, plan, mov)
        return self._run_task("MMD をセリフに合わせて動かす", work, on_done=done)

    def _place_mmd_movie(self, path: str, at: int, mme: str = "") -> M.Clip:
        from . import media as MD
        seq = self.project.sequence
        item = MD.probe(path)
        self.begin_edit()
        self.project.media.append(item)
        b = self.project.bin_by_id(self.named_bin("3D / MMD")) if hasattr(self, "named_bin") else None
        (b or self.project.bins[0]).add("media", item.id)
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        dur = max(1, int(round(item.duration_s * seq.fps)))
        clip = M.Clip(M.new_id(), "video", at, dur, 0, item.id, "MMD",
                      props={**M.default_props("video"), "pos_x": round(seq.width * 0.28, 1)})
        if mme:
            from .fx_mme import MME_PRESETS
            clip.props["effects"] = [{"name": fid, "params": dict(prm), "on": True} for fid, prm in MME_PRESETS.get(mme, [])]
        t.clips.append(clip)
        self.end_edit("MMD（身振り手振り・口パク）を置く")
        return clip

    # ═════════════════════════════════════════
    #  Blackmagic から収録
    # ═════════════════════════════════════════
    def bmd_capture_dialog(self):
        from . import bmdcapture as BC
        vids, auds = BC.devices()
        if not vids:
            QMessageBox.information(self, "Blackmagic から収録", "Blackmagic の入力が見つかりません（Desktop Video を入れて、機器をつないでください）。")
            return None
        seq = self.project.sequence
        dlg = QDialog(self)
        dlg.setWindowTitle("Blackmagic から収録")
        f = QFormLayout(dlg)
        dev = QComboBox()
        dev.addItems(vids)
        f.addRow("入力", dev)
        fmt = QComboBox()
        f.addRow("入力の形式（信号と同じに）", fmt)

        def fill_fmt():
            fmt.clear()
            fs = BC.formats(dev.currentText())
            for x in fs:
                fmt.addItem(x["label"], x)
            want = next((i for i, x in enumerate(fs) if x["size"] == f"{seq.width}x{seq.height}" and abs(x["fps"] - seq.fps) < 0.02), -1)
            if want < 0:
                want = next((i for i, x in enumerate(fs) if x["size"] == "1920x1080" and abs(x["fps"] - seq.fps) < 0.02), 0)
            fmt.setCurrentIndex(max(0, want))
        dev.currentIndexChanged.connect(fill_fmt)
        fill_fmt()
        aud = QComboBox()
        aud.addItem("（音なし）", "")
        for a in auds:
            aud.addItem(a, a)
        if auds:
            aud.setCurrentIndex(1)
        f.addRow("音声", aud)
        codec = QComboBox()
        from . import gpu as G
        for k, (lab, _e, _v, _a) in BC.CODECS.items():
            if k == "h264_nvenc" and not G.use_nvenc("h264"):
                continue
            codec.addItem(lab, k)
        f.addRow("保存の形式", codec)
        folder = QLineEdit(os.path.join(str(Path(self.project_path).parent) if getattr(self, "project_path", None)
                                        else SETTINGS.local_media_dir, "収録"))
        f.addRow("保存先", folder)
        cut = QCheckBox("止めたら取り込んで、無音の所をカットしたシーケンスを作る")
        cut.setChecked(True)
        f.addRow(cut)
        stat = QLabel("止まっています")
        stat.setStyleSheet("font-size: 15px;")
        f.addRow(stat)
        row = QHBoxLayout()
        b_rec = QPushButton("● 収録開始")
        b_stop = QPushButton("■ 止める")
        b_stop.setEnabled(False)
        row.addWidget(b_rec)
        row.addWidget(b_stop)
        f.addRow(row)
        st = {"rec": None}
        tm = QTimer(dlg)

        def tick():
            r = st["rec"]
            if r is None:
                return
            if not r.running:
                tm.stop()
                stat.setText("止まりました: " + (" / ".join(r.log[-2:]) or ""))
                b_rec.setEnabled(True)
                b_stop.setEnabled(False)
                return
            el = int(r.elapsed())
            stat.setText(f"● 収録中 {el // 60:02d}:{el % 60:02d}　{r.frames} コマ　{r.size() / 1e6:.1f} MB")

        def start():
            x = fmt.currentData()
            name = time.strftime("BMD_%Y%m%d_%H%M%S")
            try:
                st["rec"] = BC.Recorder(dev.currentText(), aud.currentData(), x["size"], x["fps"], codec.currentData(),
                                        os.path.join(folder.text(), name))
            except Exception as ex:
                QMessageBox.warning(dlg, "Blackmagic から収録", str(ex))
                return
            b_rec.setEnabled(False)
            b_stop.setEnabled(True)
            tm.start(500)

        def stop():
            r = st["rec"]
            if r is None:
                return
            tm.stop()
            try:
                path = r.stop()
            except Exception as ex:
                QMessageBox.warning(dlg, "Blackmagic から収録", str(ex))
                b_rec.setEnabled(True)
                b_stop.setEnabled(False)
                return
            st["path"] = path
            stat.setText(f"保存しました: {Path(path).name}（{r.frames} コマ）")
            b_rec.setEnabled(True)
            b_stop.setEnabled(False)
            if cut.isChecked():
                self.obs_process(path, {"convert": "none", "cut": True})
            else:
                self.import_files([path])
        tm.timeout.connect(tick)
        b_rec.clicked.connect(start)
        b_stop.clicked.connect(stop)
        dlg.finished.connect(lambda _r: stop() if st["rec"] is not None and st["rec"].running else None)
        dlg._st = st
        dlg._start, dlg._stop = start, stop
        dlg.show()
        self._bmd_cap_dlg = dlg
        return dlg

    # ═════════════════════════════════════════
    #  YouTube 公開まで自動
    # ═════════════════════════════════════════
    def auto_thumbnail(self, path: str, title: str = "", a: int = 0, b: int = 0, samples: int = 24) -> str:
        """くっきり・色のある場面を選び、タイトルを大きく入れたサムネイル（1280×720 JPEG）。"""
        from PIL import Image
        from core.models import DesignSettings
        from core.renderer import render_telop_image
        from . import media as MD
        from . import render as R
        seq = self.project.sequence
        b = b or seq.duration
        pool = MD.DecoderPool()
        best, best_s = a, -1.0
        for i in range(samples):
            f = int(a + (b - a) * (i + 0.5) / samples)
            im = np.asarray(R.render_frame(seq, f, self.project, pool, 0.2).convert("RGB")).astype(np.float32)
            L = im.mean(axis=2)
            sat = (im.max(axis=2) - im.min(axis=2)).mean()
            gx = np.abs(np.diff(L, axis=1)).mean()
            score = L.std() + sat * 0.6 + gx * 2 - abs(L.mean() - 128) * 0.3
            if score > best_s:
                best, best_s = f, score
        frame = R.render_frame(seq, best, self.project, pool, 1.0).convert("RGB").resize((1280, 720), Image.LANCZOS)
        if title:
            d = DesignSettings(canvas_width=1280, canvas_height=720, font_size=96, font_color="#FFE600", stroke_enabled=True,
                               stroke_color="#000000", stroke_width=10, stroke2_enabled=True, stroke2_color="#FFFFFF",
                               stroke2_width=6, position_v="bottom", offset_y=50, bold=True)
            t = render_telop_image(title[:28], d)
            frame = frame.convert("RGBA")
            frame.alpha_composite(t)
            frame = frame.convert("RGB")
        frame.save(path, "JPEG", quality=92)
        return path

    def yt_publish(self, o: dict, task=None) -> dict:
        """o: out_dir / title / tags / privacy / publish_at / playlist / a / b / steps(set) / quality。戻り値: できたもの。"""
        from . import youtube as YTX
        from . import ytupload as YT
        from .export import ExportWorker
        seq = self.project.sequence
        steps = o["steps"]
        os.makedirs(o["out_dir"], exist_ok=True)
        base = os.path.join(o["out_dir"], _safe_name(o["title"] or seq.name))
        res = {"issues": [], "video": "", "thumbnail": "", "description": "", "srt": "", "url": ""}

        def prog(i, n, m):
            if task is not None:
                task.progress.emit(i, n, m)
            if task is not None and task.cancelled:
                raise InterruptedError("中止しました")
        if "check" in steps:
            prog(0, 0, "書き出し前チェック…")
            res["issues"] = [list(x) for x in self.run_checks(heavy=False)]
        if "export" in steps:
            out = base + ".mp4"
            w = ExportWorker(self.project, o["a"], o["b"], out, "h264", int(o.get("quality", 18)))
            err = []
            w.progress.connect(lambda i, n: prog(i, n, "書き出し中…"))
            w.failed.connect(err.append)
            w.run()
            if err or not os.path.exists(out):
                raise RuntimeError("書き出しに失敗しました: " + (err[0] if err else ""))
            res["video"] = out
        if "thumbnail" in steps:
            prog(0, 0, "サムネイルを作っています…")
            res["thumbnail"] = self.auto_thumbnail(base + "_thumb.jpg", o["title"], o["a"], o["b"])
        if "description" in steps:
            res["description"] = YTX.description_text(self.project, seq, o["title"])
            Path(base + "_概要欄.txt").write_text(res["description"], encoding="utf-8")
        if "srt" in steps:
            try:
                res["srt"] = self.yt_subs(base + ".srt")
            except Exception:
                res["srt"] = ""
        if "upload" in steps:
            if not res["video"]:
                raise RuntimeError("アップロードする動画がありません（書き出しを選んでください）")
            meta = {"title": o["title"], "description": res["description"], "tags": o.get("tags") or [],
                    "privacy": o.get("privacy", "private"), "publish_at": o.get("publish_at", ""),
                    "thumbnail": res["thumbnail"], "playlist": o.get("playlist", ""), "category": "22"}
            class _Cancel:                     # ytupload は threading.Event と同じ is_set() で中止を見る
                @staticmethod
                def is_set():
                    return bool(task is not None and task.cancelled)
            entry = YT.upload(res["video"], meta, progress=lambda a, b: prog(a, b, "YouTube にアップロード中…"),
                              cancel=_Cancel())
            res["url"] = entry.get("url", "")
            res["notes"] = entry.get("notes", [])
        return res

    def yt_publish_dialog(self):
        from . import ytupload as YT
        seq = self.project.sequence
        dlg = QDialog(self)
        dlg.setWindowTitle("YouTube 公開まで自動（書き出し → サムネ → 概要欄 → アップロード）")
        v = QVBoxLayout(dlg)
        f = QFormLayout()
        v.addLayout(f)
        title = QLineEdit(seq.name)
        f.addRow("タイトル", title)
        tags = QLineEdit()
        tags.setPlaceholderText("カンマで区切る（例: ゆっくり解説, ゲーム）")
        f.addRow("タグ", tags)
        priv = QComboBox()
        for k, lab in (("private", "非公開"), ("unlisted", "限定公開"), ("public", "公開"), ("schedule", "予約公開")):
            priv.addItem(lab, k)
        when = QDateTimeEdit(QDateTime.currentDateTime().addSecs(3600 * 24))
        when.setDisplayFormat("yyyy-MM-dd HH:mm")
        when.setCalendarPopup(True)
        when.setEnabled(False)
        priv.currentIndexChanged.connect(lambda _i: when.setEnabled(priv.currentData() == "schedule"))
        r = QHBoxLayout()
        r.addWidget(priv)
        r.addWidget(when)
        f.addRow("公開", r)
        pl = QComboBox()
        pl.addItem("（再生リストに入れない）", "")
        if YT.signed_in():
            try:
                for p in YT.playlists():
                    pl.addItem(p.get("snippet", {}).get("title", p.get("id")), p.get("id"))
            except Exception:
                pass
        f.addRow("再生リスト", pl)
        rng = QComboBox()
        rng.addItems(["シーケンス全体", "IN〜OUT"])
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            rng.setCurrentIndex(1)
        f.addRow("範囲", rng)
        q = QSpinBox()
        q.setRange(14, 30)
        q.setValue(18)
        q.setToolTip("小さいほど高画質（NVIDIA があれば NVENC で書き出します）")
        f.addRow("画質（CRF）", q)
        out = QLineEdit(os.path.join(os.path.expanduser("~"), "Videos", "TeloCut 公開"))
        f.addRow("保存先", out)
        steps = _checklist(["書き出し前チェック", "書き出し", "サムネイル（自動で選んでタイトルを入れる）", "概要欄（チャプター・クレジット）",
                            "字幕（SRT）", "YouTube にアップロード"])
        if not YT.signed_in():
            steps.item(5).setCheckState(Qt.CheckState.Unchecked)
            steps.item(5).setText("YouTube にアップロード（先に「YouTube にアップロード」でログインしてください）")
        f.addRow("やること", steps)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("始める")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if not dlg.exec():
            return None
        keys = ["check", "export", "thumbnail", "description", "srt", "upload"]
        chosen = {keys[i] for i in range(steps.count()) if steps.item(i).checkState() == Qt.CheckState.Checked}
        a, b = (seq.mark_in, seq.mark_out) if rng.currentIndex() == 1 else (0, seq.duration)
        pub = ""
        privacy = priv.currentData()
        if privacy == "schedule":
            pub = when.dateTime().toUTC().toString("yyyy-MM-ddTHH:mm:ss.000Z")
            privacy = "private"
        o = {"out_dir": out.text(), "title": title.text().strip() or seq.name,
             "tags": [t.strip() for t in tags.text().replace("、", ",").split(",") if t.strip()], "privacy": privacy,
             "publish_at": pub, "playlist": pl.currentData(), "a": a, "b": b, "steps": chosen, "quality": q.value()}
        return self.run_yt_publish(o)

    def run_yt_publish(self, o: dict, on_done=None):
        def work(task):
            return self.yt_publish(o, task)

        def done(res):
            lines = []
            if "check" in o["steps"]:
                n = len(res["issues"])
                lines.append(f"チェック: {n} 件" + ("（チェック・管理 → 放送前チェック で確認）" if n else "（問題なし）"))
            for k, lab in (("video", "動画"), ("thumbnail", "サムネイル"), ("srt", "字幕")):
                if res.get(k):
                    lines.append(f"{lab}: {res[k]}")
            if res.get("url"):
                lines.append(f"YouTube: {res['url']}")
                from PyQt6.QtWidgets import QApplication
                QApplication.clipboard().setText(res["url"])
            for nt in res.get("notes") or []:
                lines.append("※ " + nt)
            self._yt_publish_result = res
            self.status.showMessage("公開まで完了" + (f": {res['url']}（URL をコピーしました）" if res.get("url") else ""), 10000)
            if on_done:
                on_done(res)
            else:
                QMessageBox.information(self, "YouTube 公開まで自動", "\n".join(lines) or "完了しました")
        return self._run_task("YouTube 公開まで自動", work, on_done=done)


def _safe_name(s: str) -> str:
    import re
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip()[:80] or "video"

