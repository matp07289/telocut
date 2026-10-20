"""MainWindow に追加する機能（YouTube 向け・ゆっくり・AI・外部アプリ連携・アセット / ファーム）。

mainwindow.py が大きくなりすぎないよう、ミックスインとして分けている。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QInputDialog, QLabel, QListWidget, QMessageBox, QPlainTextEdit, QProgressDialog,
                             QPushButton, QSpinBox, QCheckBox, QVBoxLayout, QHBoxLayout, QComboBox)

from core.timecode import frames_to_timecode
from . import media as MD
from . import model as M
from .settings import SETTINGS

class _Task(QThread):
    """重い処理を別スレッドで実行する小さな入れ物。"""
    done = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.cancelled = False

    def run(self):
        try:
            self.done.emit(self.fn(self, *self.args))
        except Exception as ex:
            import traceback
            self.failed.emit(f"{ex}\n{traceback.format_exc()[-800:]}")


class FeaturesMixin:
    # ─────────────────────────────────────────
    #  初期化
    # ─────────────────────────────────────────
    def _feature_commands(self) -> dict:
        return {
            "command_palette": self.command_palette, "drag_zoom": lambda: self.set_drag_zoom(None),
            "script_gen": self.script_dialog, "character_new": self.character_dialog,
            "regen_line": self.regen_lines, "transcribe_subs": self.transcribe_subtitles,
            "thumbnail": self.thumbnail_dialog, "make_shorts": self.make_shorts,
            "description": self.description_dialog, "loudness": self.loudness_check,
            "slideshow": self.slideshow, "bgm_loop": self.bgm_loop, "cps_check": self.cps_check,
            "add_band": lambda: self.add_shape("band"), "add_progress": lambda: self.add_shape("progress"),
            "ai_assist": lambda: self.show_dock(self.dock_assist), "ai_image": lambda: self.show_dock(self.dock_comfy),
            "asset_panel": lambda: self.show_dock(self.dock_assets), "se_pad": lambda: self.show_dock(self.dock_se),
            "preset_panel": lambda: self.show_dock(self.dock_presets),
            "edit_in_photoshop": self.edit_in_photoshop, "frame_to_photoshop": self.frame_to_photoshop,
            "send_ae": self.send_after_effects, "send_blender": lambda: self.send_blender(False),
            "render_blender": lambda: self.send_blender(True), "watch_folders": self.watch_folders_dialog,
            "toggle_duck": self.toggle_duck, "favorite_media": self.favorite_selected_media,
            "telop_editor": lambda: self.show_dock(self.dock_telop_ed),
            "voice_panel": lambda: self.show_dock(self.dock_voice),
            "launch_voice": self.launch_voice_engine, "plugin_folder": self.open_plugin_folder,
            "reload_plugins": self.reload_plugins,
        }

    def _init_features(self):
        """ドック・監視・ブリッジを用意する（__init__ の最後で呼ぶ）。"""
        from .comfy import ComfyPanel
        from .assetpanel import AssetPanel
        from .assistant import AssistantPanel
        from .klapanels import PresetPanel
        from .youtube import SEPadPanel
        from .integrations import MediaWatcher
        R = Qt.DockWidgetArea.RightDockWidgetArea
        self.assist_panel = AssistantPanel(self)
        self.comfy_panel = ComfyPanel(self)
        self.asset_panel = AssetPanel(self)
        self.preset_panel = PresetPanel(self)
        self.se_panel = SEPadPanel(self)
        self.dock_assist = self._dock("AI アシスト（Claude）", self.assist_panel, R, "assist")
        self.dock_comfy = self._dock("AI 画像生成（ComfyUI）", self.comfy_panel, R, "comfy")
        self.dock_assets = self._dock("アセット / お気に入り", self.asset_panel, R, "assets")
        self.dock_presets = self._dock("プリセット", self.preset_panel, R, "presets")
        self.dock_se = self._dock("SE パッド", self.se_panel, R, "sepad")
        from .telopeditor import TelopEditor
        from .voicepanel import VoicePanel
        self.telop_editor = TelopEditor(self)
        self.voice_panel = VoicePanel(self)
        self.dock_telop_ed = self._dock("テロップエディター（DavinTelop）", self.telop_editor, R, "telop_editor")
        self.dock_voice = self._dock("セリフ（音声合成・立ち絵）", self.voice_panel, R, "voice")
        new = [self.dock_telop_ed, self.dock_voice, self.dock_presets, self.dock_assets, self.dock_se,
               self.dock_assist, self.dock_comfy]
        # タブで重ねたドックは一番広い最小幅に合わせられてしまうので、最小幅を控えめにする
        # （狭いときは中身が縮む / スクロールする。タイムラインやモニターを押しつぶさない）
        for d in new:
            d.widget().setMinimumWidth(240)
        for d in new:
            self.tabifyDockWidget(self.dock_insp, d)
            d.hide()
        self.docks += new
        self.record_mon.view.zoom_rect.connect(self.drag_zoom)
        self.watcher = MediaWatcher(self)
        self.watcher.changed.connect(self._on_media_changed)
        self.bridge = None
        if SETTINGS.bridge_enabled and os.environ.get("TELOCUT_NO_BRIDGE") != "1":
            try:
                from .bridge import Bridge
                self.bridge = Bridge(self)
                self.bridge.start(47310)
            except Exception as ex:
                self.status.showMessage(f"連携用の接続を開けませんでした: {ex}", 5000)

    def _build_feature_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        yt = QMenu_before(mb, help_menu, "YouTube")
        for c in ("voice_panel", "launch_voice", "script_gen", "character_new", "regen_line", None,
                  "telop_editor", "transcribe_subs", "cps_check", None,
                  "thumbnail", "make_shorts", "description", "loudness", None, "slideshow", "bgm_loop",
                  "toggle_duck", None, "add_band", "add_progress", None, "se_pad", "preset_panel"):
            yt.addSeparator() if c is None else self._a(yt, c)
        ai = QMenu_before(mb, help_menu, "AI / 連携")
        for c in ("command_palette", "drag_zoom", None, "ai_assist", "ai_image", "asset_panel", "favorite_media", None,
                  "edit_in_photoshop", "frame_to_photoshop", "send_ae", "send_blender", "render_blender",
                  "watch_folders", None, "plugin_folder", "reload_plugins"):
            ai.addSeparator() if c is None else self._a(ai, c)
        ai.addSeparator()
        ai.addAction("Claude Code に TeloCut を登録（MCP）...", self.register_mcp)
        ai.addAction("編集ルール（AI アシスト用）を編集...", self.edit_rules)
        ai.addSeparator()
        ai.addAction("このPCでアセットサーバーを起動...", self.start_asset_server)
        ai.addAction("このPCをレンダーノードにする...", self.start_render_node)
        ai.addAction("DavinTelop（テロップ編集）を開く", self.open_davintelop)

    # ─────────────────────────────────────────
    #  共通
    # ─────────────────────────────────────────
    def show_dock(self, d):
        d.show()
        d.raise_()

    def watch_bin_id(self) -> str:
        return self.named_bin("監視フォルダ")

    def _sel(self, kinds=None):
        sel = self.canvas.selected_clips()
        if kinds:
            sel = [(t, c) for t, c in sel if c.kind in kinds]
        return sel

    def _need_sel(self, kinds, what="クリップ"):
        sel = self._sel(kinds)
        if not sel:
            QMessageBox.information(self, "選択", f"タイムラインで{what}を選んでください。")
        return sel

    def _on_media_changed(self, mid: str, msg: str):
        self.status.showMessage(msg, 5000)
        if mid:
            m = self.project.media_by_id(mid)
            if m is not None:
                self._thumbs.pop(mid, None)
                self._peaks.pop(mid, None)
        self._set_dirty()
        self.bin.refresh()
        self.refresh_all(False)

    def _run_task(self, title: str, fn, *args, on_done=None):
        dlg = QProgressDialog(title, "中止", 0, 0, self)
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(300)
        t = _Task(fn, *args)
        t.progress.connect(lambda i, n, m: (dlg.setMaximum(max(1, n)), dlg.setValue(i), dlg.setLabelText(f"{title}\n{m}")))
        dlg.canceled.connect(lambda: setattr(t, "cancelled", True))

        def fin(res):
            dlg.close()
            if on_done:
                on_done(res)
        t.done.connect(fin)
        t.failed.connect(lambda e: (dlg.close(), QMessageBox.critical(self, title, e)))
        self._workers.append(t)
        t.finished.connect(lambda: self._workers.remove(t) if t in self._workers else None)
        t.start()
        return t

    # ─────────────────────────────────────────
    #  コマンドパレット / Drag & Zoom
    # ─────────────────────────────────────────
    def command_palette(self):
        from .klapanels import CommandPalette
        dlg = CommandPalette(self)
        g = self.geometry()
        dlg.move(g.x() + (g.width() - dlg.width()) // 2, g.y() + 80)
        dlg.exec()

    def set_drag_zoom(self, on=None):
        v = self.record_mon.view
        v.zoom_drag = (not v.zoom_drag) if on is None else bool(on)
        v.setCursor(Qt.CursorShape.CrossCursor if v.zoom_drag else Qt.CursorShape.ArrowCursor)
        if hasattr(self, "preset_panel") and self.preset_panel.drag_btn.isChecked() != v.zoom_drag:
            self.preset_panel.drag_btn.blockSignals(True)
            self.preset_panel.drag_btn.setChecked(v.zoom_drag)
            self.preset_panel.drag_btn.blockSignals(False)
        self.status.showMessage("Drag & Zoom: レコードモニター上でズームしたい範囲を囲んでください" if v.zoom_drag
                                else "Drag & Zoom を終了しました", 4000)

    def drag_zoom(self, r):
        """レコードモニターで囲んだ範囲（0〜1）がフレームいっぱいになるよう、選択クリップを拡大・移動する。"""
        if getattr(self, "_track_mode", False):          # トラッキングの範囲指定
            self._track_mode = False
            self.set_drag_zoom(False)
            self.track_from_rect((r.x(), r.y(), r.width(), r.height()))
            return
        seq = self.project.sequence
        sel = [(t, c) for t, c in self._sel(("video", "image", "character", "shape", "color"))]
        if not sel:
            f = self.record_player.position
            for t in reversed(seq.video_tracks):
                c = next((c for c in t.clips if c.start <= f < c.end and c.kind in ("video", "image")), None)
                if c is not None:
                    sel = [(t, c)]
                    break
        if not sel:
            self.status.showMessage("ズームするクリップがありません", 3000)
            return
        k = 1.0 / max(r.width(), r.height())
        dx = (r.center().x() - 0.5) * seq.width
        dy = (r.center().y() - 0.5) * seq.height
        animate = hasattr(self, "preset_panel") and self.preset_panel.zoom_anim.isChecked()
        n = self.preset_panel.zoom_len.value() if hasattr(self, "preset_panel") else 0
        self.begin_edit()
        for _, c in sel:
            s0 = float(c.prop("scale", 1.0) or 1.0)
            x0, y0 = float(c.prop("pos_x", 0) or 0), float(c.prop("pos_y", 0) or 0)
            s1, x1, y1 = s0 * k, (x0 - dx) * k, (y0 - dy) * k
            if animate:
                an = {"scale": [s0, s1], "pos_x": [x0, x1], "pos_y": [y0, y1], "_ease": "ease_in_out"}
                if n:
                    an["_len"] = n
                c.props["anim"] = an
            else:
                c.props.update({"scale": s1, "pos_x": x1, "pos_y": y1})
        self.end_edit(f"Drag & Zoom（{k:.2f} 倍）")
        self.set_drag_zoom(False)

    # ─────────────────────────────────────────
    #  ゆっくり / 台本
    # ─────────────────────────────────────────
    def script_dialog(self):
        from .yukkuri import ScriptDialog
        ScriptDialog(self).exec()

    def character_dialog(self):
        from .yukkuri import CharacterDialog
        dlg = CharacterDialog(self)
        if dlg.exec() and getattr(dlg, "result_char", None):
            ch = dlg.result_char
            self.begin_edit()
            self.project.characters = [c for c in self.project.characters if c.get("id") != ch["id"]] + [ch]
            self.end_edit(f"キャラクター登録: {ch['name']}")
            if QMessageBox.question(self, "キャラクター", f"「{ch['name']}」を再生ヘッド位置に配置しますか？") \
                    == QMessageBox.StandardButton.Yes:
                self.add_character_clip(ch["id"], ch["name"])

    def add_character_clip(self, char_id: str, name: str = "キャラクター"):
        seq = self.project.sequence
        f = self.record_player.position
        self.begin_edit()
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        ai = next((i for i, a in enumerate(seq.audio_tracks) if a.selected), 0)
        t.clips.append(M.Clip(M.new_id(), "character", f, int(seq.fps * 10), name=name,
                              props={**M.default_props("character"), "char_id": char_id, "audio_track": ai,
                                     "scale": 0.55, "pos_x": 640, "pos_y": 160}))
        self.end_edit("キャラクターを配置")

    def regen_lines(self):
        from .yukkuri import regenerate_line
        sel = [c for _, c in self._sel(("audio",)) if c.props.get("line_text")]
        if not sel:
            QMessageBox.information(self, "セリフを作り直し",
                                    "台本から生成した音声クリップを選んでください。\n"
                                    "（インスペクターで line_text を書き換えてから実行すると、その文で作り直します）")
            return
        n = 0
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for c in sel:
                n += bool(regenerate_line(self, c))
        except Exception as ex:
            QMessageBox.critical(self, "セリフを作り直し", str(ex))
        finally:
            QApplication.restoreOverrideCursor()
        self.status.showMessage(f"{n} 個のセリフを作り直しました", 4000)

    # ─────────────────────────────────────────
    #  文字起こし → 字幕
    # ─────────────────────────────────────────
    def transcribe_subtitles(self):
        seq = self.project.sequence
        if seq.duration <= 0:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("文字起こしで字幕を作成")
        f = QFormLayout(dlg)
        model = QComboBox()
        model.addItems(["small", "medium", "large-v3", "base"])
        maxc = QSpinBox()
        maxc.setRange(6, 40)
        maxc.setValue(18)
        rng = QComboBox()
        rng.addItems(["シーケンス全体", "IN〜OUT"])
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            rng.setCurrentIndex(1)
        gpu = QCheckBox("GPU を使う（CUDA）")
        f.addRow("モデル", model)
        f.addRow("1 枚の最大文字数", maxc)
        f.addRow("範囲", rng)
        f.addRow(gpu)
        f.addRow(QLabel("音声（全トラックのミックス）を faster-whisper で文字起こしし、\n"
                        "意味の切れ目で分けたテロップを新しいトラックに並べます。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        a, b = (seq.mark_in, seq.mark_out) if rng.currentIndex() == 1 else (0, seq.duration)
        import copy
        proj = copy.deepcopy(self.project)

        def work(task, proj, a, b, model_size, use_gpu):
            import tempfile
            from core import transcribe as T
            from .smartrender import write_audio
            fd, wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                task.progress.emit(0, 0, "音声を書き出し中…")
                write_audio(proj, proj.sequence, a, b, wav)
                total = (b - a) / proj.sequence.fps

                def cb(done, tot, text):
                    task.progress.emit(int(done), int(tot or total), text[:30])
                return T.transcribe(wav, model_size, "ja", use_gpu, True, cb)
            finally:
                try:
                    os.remove(wav)
                except OSError:
                    pass

        def done(segs):
            self.place_transcript(segs, a, maxc.value())
        self._run_task("文字起こし", work, proj, a, b, model.currentText(), gpu.isChecked(), on_done=done)

    def place_transcript(self, segs, start_frame: int, max_chars: int = 18) -> int:
        from .yukkuri import subtitle_chunks
        seq = self.project.sequence
        fps = seq.fps
        self.begin_edit()
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        n = 0
        for s in segs:
            a = start_frame + int(round(s.start * fps))
            b = start_frame + int(round(s.end * fps))
            chunks = subtitle_chunks(s.text, max_chars)
            total = sum(len(c) for c in chunks) or 1
            pos = a
            for i, ch in enumerate(chunks):
                d = (b - pos) if i == len(chunks) - 1 else max(1, int((b - a) * len(ch) / total))
                if d <= 0:
                    continue
                t.clips.append(M.Clip(M.new_id(), "text", pos, d, name="字幕", props={"text": ch, "style": {}}))
                pos += d
                n += 1
        t.sort()
        # 重なりを除く（前の字幕の終わりを次の頭で切る）
        for c0, c1 in zip(t.clips, t.clips[1:]):
            if c0.end > c1.start:
                c0.duration = max(1, c1.start - c0.start)
        self.end_edit(f"文字起こしの字幕（{n} 枚）")
        return n

    def cps_check(self):
        """テロップの読みやすさ（1 秒あたりの文字数）を確認し、速すぎるものにマーカーを付ける。"""
        seq = self.project.sequence
        bad = []
        for t in seq.video_tracks:
            for c in t.clips:
                if c.kind != "text":
                    continue
                txt = str(c.prop("text", "")).replace("\n", "")
                cps = len(txt) / max(1e-6, c.duration / seq.fps)
                if cps > 8 or c.duration < seq.fps * 0.7:
                    bad.append((c, cps))
        if not bad:
            QMessageBox.information(self, "読みやすさチェック", "速すぎるテロップはありません（目安: 1 秒 8 文字以下）。")
            return
        if QMessageBox.question(self, "読みやすさチェック",
                                f"速すぎる / 短すぎるテロップが {len(bad)} 枚あります（目安: 1 秒 8 文字以下・0.7 秒以上）。\n"
                                "該当箇所にマーカーを付けますか？") != QMessageBox.StandardButton.Yes:
            return
        self.begin_edit()
        for c, cps in bad:
            seq.markers.append(M.Marker(c.start, f"⚠ 読みにくい {cps:.1f} 文字/秒", "#ff9900",
                                        comment=str(c.prop("text", ""))[:40]))
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit("読みやすさチェックのマーカー")

    # ─────────────────────────────────────────
    #  YouTube
    # ─────────────────────────────────────────
    def thumbnail_dialog(self):
        from .youtube import ThumbnailDialog
        ThumbnailDialog(self).exec()

    def make_shorts(self):
        from .youtube import make_shorts_sequence
        r = QMessageBox.question(self, "ショート動画を作成",
                                 "今のシーケンスから縦型（1080×1920）のコピーを作ります。\n"
                                 "背景にぼかした映像を敷きますか？（いいえ = 黒背景）",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No |
                                 QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return
        self.begin_edit()
        src = self.project.sequence
        d = make_shorts_sequence(self.project, src, r == QMessageBox.StandardButton.Yes)
        (self.project.bin_of(src.id) or self.project.bins[0]).add("sequence", d.id)
        self.end_edit("ショート動画を作成")
        self.load_sequence(d.id)
        if src.mark_in >= 0 and src.mark_out > src.mark_in:
            self.status.showMessage("IN〜OUT を 60 秒以内にして書き出すとショートになります", 6000)

    def description_dialog(self):
        from .youtube import description_text
        seq = self.project.sequence
        dlg = QDialog(self)
        dlg.setWindowTitle("概要欄テキスト（チャプター・クレジット）")
        dlg.resize(560, 520)
        v = QVBoxLayout(dlg)
        ed = QPlainTextEdit(description_text(self.project, seq))
        v.addWidget(QLabel("マーカーがチャプター、素材のクレジットと VOICEVOX の表記が入ります。"))
        v.addWidget(ed, 1)
        row = QHBoxLayout()
        cp = QPushButton("コピー")
        cp.clicked.connect(lambda: (QApplication.clipboard().setText(ed.toPlainText()),
                                    self.status.showMessage("概要欄をコピーしました", 3000)))
        sv = QPushButton("保存...")

        def save():
            p, _ = QFileDialog.getSaveFileName(dlg, "概要欄を保存", f"{seq.name}_概要欄.txt", "テキスト (*.txt)")
            if p:
                Path(p).write_text(ed.toPlainText(), encoding="utf-8")
        sv.clicked.connect(save)
        close = QPushButton("閉じる")
        close.clicked.connect(dlg.accept)
        row.addWidget(cp)
        row.addWidget(sv)
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)
        dlg.exec()

    def loudness_check(self):
        from .youtube import measure_loudness
        import copy
        seq = self.project.sequence
        if seq.duration <= 0:
            return
        proj = copy.deepcopy(self.project)
        proj.sequence.master_db = 0.0

        def work(task, proj):
            return measure_loudness(proj.sequence, proj, lambda i, n: task.progress.emit(int(i / 4800), int(n / 4800), "測定中"))

        def done(lufs):
            if lufs is None:
                QMessageBox.information(self, "ラウドネス", "音がありません。")
                return
            target = -14.0
            gain = round(target - lufs, 1)
            cur = float(getattr(seq, "master_db", 0.0) or 0.0)
            if QMessageBox.question(self, "ラウドネス（YouTube 基準 -14 LUFS）",
                                    f"マスター調整前のラウドネス: {lufs:.1f} LUFS（近似）\n"
                                    f"現在のマスターゲイン: {cur:+.1f} dB\n\n"
                                    f"マスターゲインを {gain:+.1f} dB にして -14 LUFS に合わせますか？\n"
                                    "（YouTube は大きすぎる音を下げるので、-14 付近が目安）") == QMessageBox.StandardButton.Yes:
                self.begin_edit()
                seq.master_db = max(-30.0, min(20.0, gain))
                self.end_edit(f"マスターゲイン {seq.master_db:+.1f} dB")
        self._run_task("ラウドネス測定", work, proj, on_done=done)

    def slideshow(self):
        from .youtube import slideshow_placements
        ids = self.bin.selected_ids()
        items = [m for m in (self.project.media_by_id(i) for i in ids) if m and m.kind in ("image", "video")]
        if not items:
            QMessageBox.information(self, "スライドショー", "ビンで画像（複数）を選んでください。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"スライドショー（{len(items)} 枚）")
        f = QFormLayout(dlg)
        sec = QDoubleSpinBox()
        sec.setRange(0.5, 60)
        sec.setValue(4.0)
        sec.setSuffix(" 秒")
        dis = QDoubleSpinBox()
        dis.setRange(0, 5)
        dis.setValue(0.5)
        dis.setSuffix(" 秒")
        kb = QCheckBox("Ken Burns（ゆっくりズーム）")
        kb.setChecked(True)
        f.addRow("1 枚の長さ", sec)
        f.addRow("ディゾルブ", dis)
        f.addRow(kb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if not dlg.exec():
            return
        seq = self.project.sequence
        tv, _ = self._target_tracks()
        tv = tv or seq.video_tracks[0]
        clips = slideshow_placements(seq, items, self.record_player.position, sec.value(), dis.value(), kb.isChecked())
        self.begin_edit()
        M.place_clips(seq, [(tv, c) for c in clips])
        self.end_edit(f"スライドショー（{len(clips)} 枚）")

    def bgm_loop(self):
        from .youtube import bgm_loop_clips
        sel = self._need_sel(("audio",), "BGM の音声クリップ")
        if not sel:
            return
        t, c = sel[0]
        m = self.project.media_by_id(c.media_id)
        seq = self.project.sequence
        end = max((x.end for tr in seq.video_tracks for x in tr.clips), default=seq.duration)
        if seq.mark_out > 0:
            end = seq.mark_out
        if end <= c.end:
            self.status.showMessage("BGM はすでに最後まで届いています", 3000)
            return
        self.begin_edit()
        new = bgm_loop_clips(c, m, end, seq.fps)
        M.place_clips(seq, [(t, x) for x in new])
        t.duck = True
        self.end_edit(f"BGM をループ（{len(new)} 回）")
        self.status.showMessage("BGM トラックのダッキング（声がある所で自動で下げる）を有効にしました", 5000)

    def toggle_duck(self):
        seq = self.project.sequence
        tracks = [t for t in seq.audio_tracks if t.selected]
        if not tracks:
            QMessageBox.information(self, "ダッキング", "BGM の音声トラックを選択（トラック名をクリック）してください。")
            return
        self.begin_edit()
        on = not all(t.duck for t in tracks)
        for t in tracks:
            t.duck = on
        self.end_edit("ダッキング " + ("オン" if on else "オフ") + "（" + ", ".join(t.name for t in tracks) + "）")

    def add_shape(self, shape: str):
        seq = self.project.sequence
        f = self.record_player.position
        self.begin_edit()
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        props = M.default_props("shape")
        props["shape"] = shape
        if shape == "progress":
            props.update({"color": "#FF3B3B", "rect": [0, 98, 100, 2], "fill_opacity": 1.0})
            c = M.Clip(M.new_id(), "shape", 0, max(1, seq.duration), name="進行バー", props=props)
        else:
            c = M.Clip(M.new_id(), "shape", f, int(seq.fps * 5), name="帯", props=props)
        t.clips.append(c)
        self.end_edit("図形を追加")

    def place_se(self, path: str, track_index: int = -1, frame: Optional[int] = None, record_undo: bool = True):
        """効果音を再生ヘッド（frame）に置く。素材が未登録なら「効果音」ビンに登録する。"""
        seq = self.project.sequence
        m = next((x for x in self.project.media if os.path.normcase(x.path) == os.path.normcase(path)), None)
        if m is None:
            m = MD.probe(path)
            MD.build_audio_cache(m.path)
            try:
                MD.build_peaks(m.path)
            except Exception:
                pass
            self.project.media.append(m)
            b = self.project.bin_by_id(self.named_bin("効果音"))
            b.add("media", m.id)
        f = self.record_player.position if frame is None else int(frame)
        dur = max(1, int(round(m.duration_s * seq.fps)))
        if record_undo:
            self.begin_edit()
        if not seq.audio_tracks:
            seq.audio_tracks.append(M.Track("audio", "A1"))
        ti = track_index if 0 <= track_index < len(seq.audio_tracks) else len(seq.audio_tracks) - 1
        t = seq.audio_tracks[ti]
        if any(c.start < f + dur and f < c.end for c in t.clips):
            # 空いている音声トラックを探し、無ければ足す
            t = next((x for x in seq.audio_tracks if not any(c.start < f + dur and f < c.end for c in x.clips)), None)
            if t is None:
                t = M.Track("audio", f"A{len(seq.audio_tracks) + 1}")
                seq.audio_tracks.append(t)
        t.clips.append(M.Clip(M.new_id(), "audio", f, dur, 0, m.id, m.name))
        t.sort()
        if record_undo:
            self.end_edit(f"効果音: {m.name}")
        return m

    def launch_voice_engine(self):
        """音声合成ソフト（VOICEVOX など）を起動して、セリフパネルを開く。"""
        from . import voiceengine as VE
        eng = VE.installed()
        if not eng:
            QMessageBox.information(self, "音声合成ソフト",
                                    "VOICEVOX / AivisSpeech / SHAREVOX / COEIROINK が見つかりません。\n"
                                    "インストールするか、環境設定（voice_engine_paths）で場所を指定してください。\n"
                                    "Windows 標準音声はそのまま使えます。")
            return
        names = [e["name"] for e in eng]
        name, ok = QInputDialog.getItem(self, "音声合成ソフトを起動", "起動するソフト:", names, 0, False)
        if not ok:
            return
        key = eng[names.index(name)]["key"]
        with_app = QMessageBox.question(self, "音声合成ソフトを起動",
                                        f"{name} のアプリ画面も開きますか？\n（いいえ = エンジンだけ起動。軽くて画面が増えません）") \
            == QMessageBox.StandardButton.Yes
        self.show_dock(self.dock_voice)
        from .yukkuri import _EngineLauncher
        self._voice_launcher = _EngineLauncher([key], with_app)
        self._voice_launcher.progress.connect(lambda m: self.status.showMessage(m, 3000))
        self._voice_launcher.done.connect(lambda failed: (
            self.status.showMessage(f"{name} を起動できませんでした" if failed else f"{name} の準備ができました", 6000),
            self.voice_panel.engine.setCurrentIndex(max(0, self.voice_panel.engine.findData(key))),
            self.voice_panel._fill_voices()))
        self._voice_launcher.start()

    def open_plugin_folder(self):
        from .fxlib import write_sample_plugin
        d = write_sample_plugin()
        if os.name == "nt":
            os.startfile(str(d))
        QMessageBox.information(self, "エフェクトプラグイン",
                                f"このフォルダに .py を置くとエフェクトパレットに追加されます:\n{d}\n\n"
                                "書き方はサンプル（sample_plugin.py）を見てください。置いたら「プラグインを再読み込み」。")

    def reload_plugins(self):
        from . import fxlib
        fxlib.load(force=True)
        self.effects.reload()
        msg = f"エフェクト {len(fxlib.REGISTRY)} 個を読み込みました"
        if fxlib.LOAD_ERRORS:
            QMessageBox.warning(self, "プラグイン", msg + "\n\n読み込めなかったもの:\n" + "\n".join(fxlib.LOAD_ERRORS)[:2000])
        self.status.showMessage(msg, 5000)
        self.refresh_all()

    def favorite_selected_media(self):
        from .assetpanel import set_favorite
        ids = self.bin.selected_ids()
        n = 0
        for mid in ids:
            m = self.project.media_by_id(mid)
            if m:
                set_favorite(m.path, True, m.name, m.kind, m.credit)
                n += 1
        if hasattr(self, "asset_panel"):
            self.asset_panel.refresh_favorites()
        self.status.showMessage(f"{n} 個をお気に入りに追加しました" if n else "ビンで素材を選んでください", 3000)

    # ─────────────────────────────────────────
    #  外部アプリ連携
    # ─────────────────────────────────────────
    def _sel_media_clip(self):
        sel = self._need_sel(("video", "image", "audio"), "素材のクリップ")
        return sel[0][1] if sel else None

    def edit_in_photoshop(self):
        from .integrations import open_in_photoshop
        c = None
        sel = self._sel(("image",))
        if sel:
            c = sel[0][1]
        ids = self.bin.selected_ids()
        m = self.project.media_by_id(c.media_id) if c else next(
            (x for x in (self.project.media_by_id(i) for i in ids) if x and x.kind == "image"), None)
        if m is None:
            QMessageBox.information(self, "Photoshop", "画像（PSD / PNG など）のクリップか素材を選んでください。")
            return
        if not open_in_photoshop(m.path):
            QMessageBox.warning(self, "Photoshop", "Photoshop が見つかりません。環境設定で場所を指定してください。")
            return
        self.status.showMessage(f"Photoshop で開きました。保存すると自動で反映されます: {m.name}", 6000)

    def frame_to_photoshop(self):
        """現在のフレームを PSD 用の PNG にして一番上のトラックに置き、Photoshop で開く（描き込み用）。"""
        from .integrations import open_in_photoshop, work_dir
        from . import render as R
        seq = self.project.sequence
        f = self.record_player.position
        d = work_dir(self, "Photoshop")
        p = d / f"frame_{frames_to_timecode(f + self.tc_offset(seq), seq.fps).replace(':', '')}.png"
        pool = MD.DecoderPool()
        try:
            R.render_frame(seq, f, self.project, pool, 1.0).save(p)
        finally:
            pool.close()
        m = MD.probe(str(p))
        self.begin_edit()
        self.project.media.append(m)
        self.project.bin_by_id(self.named_bin("Photoshop")).add("media", m.id)
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        t.clips.append(M.Clip(M.new_id(), "image", f, int(seq.fps * 3), 0, m.id, m.name))
        self.end_edit("フレームを Photoshop 用に配置")
        if not open_in_photoshop(str(p)):
            QMessageBox.information(self, "Photoshop", f"Photoshop が見つかりません。\n{p}\nを編集・保存すると自動で反映されます。")

    def send_after_effects(self):
        from .integrations import send_to_after_effects, find_app
        c = self._sel_media_clip()
        if c is None:
            return
        try:
            send_to_after_effects(self, c)
        except Exception as ex:
            QMessageBox.critical(self, "After Effects", str(ex))
            return
        msg = ("After Effects を起動しました。レンダーキューで書き出すと、上のトラックに自動でつながります。"
               if find_app("aftereffects") else "After Effects が見つかりません（JSX は AfterEffects フォルダに保存しました）。")
        self.status.showMessage(msg, 8000)

    def send_blender(self, render_now: bool):
        from .integrations import send_to_blender
        c = self._sel_media_clip()
        if c is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            send_to_blender(self, c, render_now)
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Blender", str(ex))
            return
        QApplication.restoreOverrideCursor()
        self.status.showMessage("Blender でレンダリング中。終わると上のトラックに自動でつながります。" if render_now else
                                "Blender を開きました。レンダリング（Ctrl+F12）すると自動でつながります。", 8000)

    def watch_folders_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("監視フォルダ（新しいファイルを自動で取り込む）")
        dlg.resize(560, 320)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("ここに置かれた新しい素材（AI 生成・Photoshop の書き出し・録画など）を\n"
                           "「監視フォルダ」ビンに自動で取り込みます。プロジェクトごとに保存されます。"))
        lw = QListWidget()
        lw.addItems(self.project.watch_folders)
        v.addWidget(lw, 1)
        row = QHBoxLayout()
        add = QPushButton("追加...")
        rem = QPushButton("削除")
        add.clicked.connect(lambda: (lambda d: lw.addItem(d) if d else None)(
            QFileDialog.getExistingDirectory(dlg, "監視するフォルダ")))
        rem.clicked.connect(lambda: [lw.takeItem(lw.row(i)) for i in lw.selectedItems()])
        ok = QPushButton("OK")
        ok.clicked.connect(dlg.accept)
        row.addWidget(add)
        row.addWidget(rem)
        row.addStretch(1)
        row.addWidget(ok)
        v.addLayout(row)
        if dlg.exec():
            new = [lw.item(i).text() for i in range(lw.count())]
            if new != self.project.watch_folders:
                self.begin_edit()
                self.project.watch_folders = new
                self.end_edit("監視フォルダを変更")

    # ─────────────────────────────────────────
    #  AI / サーバー
    # ─────────────────────────────────────────
    def register_mcp(self):
        from .assistant import register_mcp
        register_mcp(self)

    def edit_rules(self):
        from .assistant import load_rules, rules_path
        dlg = QDialog(self)
        dlg.setWindowTitle("編集ルール（AI アシストに毎回渡す）")
        dlg.resize(620, 420)
        v = QVBoxLayout(dlg)
        ed = QPlainTextEdit(load_rules())
        v.addWidget(ed, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec():
            p = rules_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(ed.toPlainText(), encoding="utf-8")

    def start_asset_server(self):
        if getattr(self, "_asset_httpd", None) is not None:
            QMessageBox.information(self, "アセットサーバー", f"起動中です: http://{_lan_ip()}:{self._asset_port}")
            return
        d = QFileDialog.getExistingDirectory(self, "アセットサーバーで公開する素材フォルダ（NAS など）")
        if not d:
            return
        port, ok = QInputDialog.getInt(self, "アセットサーバー", "ポート番号:", 8770, 1024, 65535)
        if not ok:
            return
        from .assetserver import serve
        from .settings import BASE_DIR
        try:
            self._asset_httpd, _ = serve([d], str(BASE_DIR / "asset_server"), port, block=False)
        except OSError as ex:
            QMessageBox.critical(self, "アセットサーバー", f"起動できません: {ex}")
            return
        self._asset_port = port
        url = f"http://{_lan_ip()}:{port}"
        if not SETTINGS.asset_server:
            SETTINGS.asset_server = url
            SETTINGS.save()
        QMessageBox.information(self, "アセットサーバー",
                                f"起動しました: {url}\nブラウザでも開けます。TeloCut を閉じると止まります。\n\n"
                                "常時動かす場合は TeloCut.exe --asset-server --root <フォルダ> を\n"
                                "サーバー PC（VM 可）で実行してください。")

    def start_render_node(self):
        if getattr(self, "_node_stop", None) is not None:
            self._node_stop.set()
            self._node_stop = None
            self.status.showMessage("レンダーノードを停止しました", 4000)
            return
        url, ok = QInputDialog.getText(self, "レンダーノード", "アセットサーバーの URL:", text=SETTINGS.asset_server)
        if not ok or not url.strip():
            return
        from .rendercluster import run_node, server_ok
        url = url.strip() if url.strip().startswith("http") else "http://" + url.strip()
        if not server_ok(url):
            QMessageBox.warning(self, "レンダーノード", f"{url} に接続できません。")
            return
        self._node_stop = threading.Event()
        threading.Thread(target=run_node, args=(url,), kwargs={"stop": self._node_stop}, daemon=True).start()
        self.status.showMessage("この PC はレンダーノードとして待機中です（もう一度選ぶと停止）", 6000)

    def open_davintelop(self):
        import subprocess
        import sys
        exe_dir = Path(sys.executable).parent
        if getattr(sys, "frozen", False):
            cands = [exe_dir / "DavinTelop.exe"]
            cmd = next(([str(c)] for c in cands if c.exists()), [sys.executable, "--telop"])
        else:
            cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py")]
        subprocess.Popen(cmd + ["--telocut-sync"])      # 起動したらすぐリアルタイム連携
        self.status.showMessage("DavinTelop を起動しました（テロップ・デザイン・選択がリアルタイムで連動します）", 5000)


def QMenu_before(mb, before_action, title):
    from PyQt6.QtWidgets import QMenu
    m = QMenu(title, mb)
    mb.insertMenu(before_action, m)
    return m


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.0.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
