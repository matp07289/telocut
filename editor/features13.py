"""立ち絵メーカー（目パチ・口パク）とテンプレートから立ち絵を置く（MainWindow のミックスイン）。"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QInputDialog, QMessageBox

from . import model as M


class Features13Mixin:
    def _feature13_commands(self) -> dict:
        return {"tachie_maker": self.tachie_maker, "tachie_template": self.tachie_from_template_dialog,
                "dub_en_ja": self.dub_dialog}

    def _build_feature13_menus(self):
        m = self._menu_named("YouTube")
        if m is not None:
            m.addSeparator()
            for c in ("tachie_maker", "tachie_template", "dub_en_ja"):
                self._a(m, c)

    def tachie_maker(self, source: str = ""):
        """立ち絵の部品（PSD・PsdSplitter の JSON・PNG のフォルダ）から、目パチ・口パクの立ち絵を作る。"""
        from .tachie_ui import TachieMakerDialog
        dlg = TachieMakerDialog(self, source)
        self._tachie_dlg = dlg
        dlg.show()
        return dlg

    def add_character(self, ch: dict, place: bool = False, audio_track: Optional[int] = None) -> Optional[M.Clip]:
        """キャラクターをプロジェクトに入れる（place なら再生ヘッドからタイムラインに置く。口パクは選んでいる音声トラック）。"""
        self.begin_edit()
        self.project.characters = [c for c in self.project.characters if c.get("id") != ch["id"]] + [ch]
        clip = None
        if place:
            seq = self.project.sequence
            f = self.record_player.position
            ai = audio_track if audio_track is not None else next(
                (i for i, a in enumerate(seq.audio_tracks) if a.selected and a.clips),
                next((i for i, a in enumerate(seq.audio_tracks) if a.clips), 0))
            end = max((c.end for c in seq.audio_tracks[ai].clips), default=0) if seq.audio_tracks else 0
            dur = end - f if end > f + seq.fps else int(seq.fps * 10)
            p = ch.get("params") or {}
            t = M.Track("video", ch.get("name", "立ち絵")[:6], selected=False)
            seq.video_tracks.append(t)
            clip = M.Clip(M.new_id(), "character", f, max(1, dur), name=ch.get("name", "立ち絵"),
                          props={**M.default_props("character"), "char_id": ch["id"], "audio_track": ai,
                                 "scale": 0.55, "pos_x": round(seq.width * 0.3, 1), "pos_y": round(seq.height * 0.12, 1),
                                 "bounce": bool(p.get("bounce", True)), "vowel_sync": bool(p.get("vowel_sync", True)),
                                 "breath": True, "sway": True})
            t.clips.append(clip)
        self.end_edit(f"立ち絵「{ch.get('name', '')}」" + ("を置く" if place else "を追加"))
        if hasattr(self, "voice_panel"):
            try:
                self.voice_panel.refresh()
            except Exception:
                pass
        return clip

    def tachie_from_template_dialog(self):
        from . import tachie as T
        ts = T.templates()
        if not ts:
            QMessageBox.information(self, "立ち絵テンプレート", "テンプレートがありません。「立ち絵メーカー」で作って保存してください。")
            return self.tachie_maker()
        names = [t["name"] for t in ts]
        n, ok = QInputDialog.getItem(self, "立ち絵テンプレートから置く", "テンプレート:", names, 0, False)
        if ok:
            return self.tachie_from_template(ts[names.index(n)], place=True)
        return None

    def tachie_from_template(self, t: dict, place: bool = True) -> Optional[M.Clip]:
        """テンプレートの立ち絵をそのまま置く（目パチ・口パクの設定も）。"""
        from . import tachie as T
        ch = T.character_from_template(t)
        return self.add_character(ch, place)

    # ─────────────────────────────────────────
    #  英語の声 → 日本語の吹き替え（dub.py）
    # ─────────────────────────────────────────
    def dub_dialog(self):
        from PyQt6.QtWidgets import (QDialog, QFormLayout, QComboBox, QCheckBox, QSpinBox, QLabel, QDialogButtonBox,
                                     QPushButton, QHBoxLayout, QWidget)
        from . import dub as D, gpu as G, voiceengine as VE
        seq = self.project.sequence
        if seq.duration <= 0:
            QMessageBox.information(self, "吹き替え", "タイムラインに英語の声の入った素材を置いてください。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("英語の声 → 日本語の吹き替え")
        f = QFormLayout(dlg)
        rng = QComboBox()
        rng.addItems(["シーケンス全体", "IN〜OUT"])
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            rng.setCurrentIndex(1)
        f.addRow("範囲", rng)
        lang = QComboBox()
        lang.addItem("英語", "en")
        lang.addItem("自動で判定", "auto")
        f.addRow("元の言葉", lang)
        model = QComboBox()
        model.addItems(["small", "medium", "large-v3", "base"])
        f.addRow("文字起こしのモデル", model)
        gpu = QCheckBox("GPU を使う（CUDA）")
        gpu.setChecked(G.use_whisper_gpu())
        f.addRow(gpu)
        tr = QComboBox()
        tr.addItem("Claude Code で日本語に訳す", "claude")
        tr.addItem("訳さない（元の言葉のまま）", "none")
        f.addRow("翻訳", tr)
        eng = QComboBox()
        for e in VE.ENGINES:
            eng.addItem(e["name"], e["key"])
        voice = QComboBox()
        voice.addItem("ずんだもん（ノーマル）", 3)
        row = QHBoxLayout()
        row.addWidget(voice, 1)
        load = QPushButton("声の一覧")
        row.addWidget(load)
        w = QWidget()
        w.setLayout(row)

        def fill():
            key = eng.currentData()
            try:
                VE.ensure(key)
                sp = VE.speakers(key)
            except Exception as ex:
                QMessageBox.warning(dlg, "声の一覧", f"{eng.currentText()} を起動できません: {ex}")
                return
            voice.clear()
            for name, sid in sp:
                voice.addItem(name, sid)
        load.clicked.connect(fill)
        f.addRow("音声合成", eng)
        f.addRow("声", w)
        mk_voice = QCheckBox("日本語の声を作る")
        mk_voice.setChecked(True)
        mk_tel = QCheckBox("日本語のテロップを作る")
        mk_tel.setChecked(True)
        keep_en = QCheckBox("元の言葉の字幕も残す（上に小さく）")
        duck = QSpinBox()
        duck.setRange(-40, 0)
        duck.setValue(-14)
        duck.setSuffix(" dB")
        maxs = QSpinBox()
        maxs.setRange(100, 200)
        maxs.setValue(160)
        maxs.setSuffix(" %")
        for wdg in (mk_voice, mk_tel, keep_en):
            f.addRow(wdg)
        f.addRow("元の声の音量", duck)
        f.addRow("長いときの話速（最大）", maxs)
        f.addRow(QLabel("元の声を文字起こしして日本語に訳し、同じ時間に日本語の声とテロップを並べます。\n"
                        "日本語が長くて収まらないときは話速を上げます。新しいトラックに置くので元に戻せます。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        a, b = (seq.mark_in, seq.mark_out) if rng.currentIndex() == 1 else (0, seq.duration)
        opt = D.DubOptions(model=model.currentText(), use_gpu=gpu.isChecked(), language=lang.currentData(),
                           translate=tr.currentData(), engine=eng.currentData(), style_id=int(voice.currentData() or 3),
                           max_speed=maxs.value() / 100, make_voice=mk_voice.isChecked(), make_telops=mk_tel.isChecked(),
                           keep_english=keep_en.isChecked(), duck_db=float(duck.value()))
        return self.run_dub(a, b, opt)

    def run_dub(self, a: int, b: int, opt, translate_runner=None, synth=None, on_done=None, transcriber=None):
        """範囲 a〜b（フレーム）を吹き替える（別スレッド）。translate_runner / synth / transcriber はテスト用の差し替え。"""
        import copy
        import os
        from . import dub as D
        from .yukkuri import voice_dir
        proj = copy.deepcopy(self.project)
        import time
        # 毎回別のフォルダ（前の吹き替えの声を上書きしない）
        out_dir = str(voice_dir(getattr(self, "project_path", None) or None) / "dub" / time.strftime("%Y%m%d_%H%M%S"))

        def work(task, proj, a, b, opt):
            import tempfile
            from core import transcribe as T
            from .smartrender import write_audio
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                task.progress.emit(0, 0, "音声を書き出し中…")
                write_audio(proj, proj.sequence, a, b, wav)
                total = (b - a) / proj.sequence.fps
                run = transcriber or T.transcribe
                segs = run(wav, opt.model, opt.language, opt.use_gpu, True,
                           lambda d, t, s: task.progress.emit(int(d), int(t or total), "文字起こし: " + s[:24]))
            finally:
                try:
                    os.remove(wav)
                except OSError:
                    pass
            lines = D.segments_to_lines(segs)
            if not lines:
                return lines
            if opt.translate == "claude":
                task.progress.emit(0, len(lines), "Claude Code で翻訳中…")
                ja = D.translate_claude([ln.en for ln in lines], runner=translate_runner,
                                        progress=lambda i, n: task.progress.emit(i, n, "翻訳中…"))
            else:
                ja = [ln.en for ln in lines]
            for ln, j in zip(lines, ja):
                ln.ja = j
            if opt.make_voice:
                D.synth_lines(lines, out_dir, opt, synth=synth,
                              progress=lambda i, n, m: task.progress.emit(i, n, "声を合成: " + m),
                              cancel=lambda: task.cancelled)
            return lines

        def done(lines):
            if not lines:
                QMessageBox.information(self, "吹き替え", "声が見つかりませんでした。")
                return
            r = D.place(self, lines, a, opt)
            self.status.showMessage(f"吹き替え: テロップ {r['telops']} 枚・声 {r['voices']} 個を置きました", 8000)
            if on_done:
                on_done(lines, r)
        return self._run_task("吹き替え", work, proj, a, b, opt, on_done=done)
