"""トランスコードとほかの新機能（MainWindow のミックスイン）。

素材のトランスコード（DNxHR / DNxHD / ProRes / QuickTime / H.264 …）・音声だけ書き出し・GIF・連番画像・
プロジェクトを zip に・サムネイル候補・J / L カット・反転・カラーマッチ・オートカラー・音声で同期・手ぶれ補正・
分割画面・静止画の長さ・N 秒ごとに分割・並べ替え・マーカーに合わせる・使用箇所・タイムコード焼き込み・
ウォーターマーク・シネスコの黒帯・カラーバーとトーン・長いテロップの分割・テロップを文字数で並べる
"""
from __future__ import annotations


import math
import os
import random
import threading
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                             QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QVBoxLayout)

from . import media as MD
from . import mediacodecs as MC
from . import model as M
from .settings import SETTINGS


class _T:
    """待つとき用の「タスク」（_run_task と同じ形）。"""
    cancelled = False

    class progress:
        @staticmethod
        def emit(*_a):
            pass


# ─────────────────────────── トランスコード（アプリ）───────────────────────────
class TranscodeWorker(QThread):
    progress = pyqtSignal(int, int, float, str)        # 何本目, 全部, その 1 本の進み, 名前
    done = pyqtSignal(list, list)                      # [(media id, 出力)], [(名前, エラー)]

    def __init__(self, jobs, preset, size):
        super().__init__()
        self.jobs, self.preset, self.size = jobs, preset, size
        self.cancel = threading.Event()

    def run(self):
        ok, ng = [], []
        for i, (mid, src, dst) in enumerate(self.jobs):
            if self.cancel.is_set():
                break
            name = Path(src).name
            try:
                MC.transcode(src, dst, self.preset, self.size,
                             lambda x, i=i, name=name: self.progress.emit(i, len(self.jobs), x, name), self.cancel)
                ok.append((mid, dst))
            except InterruptedError:
                break
            except Exception as ex:
                ng.append((name, str(ex)))
        self.done.emit(ok, ng)


class TranscodeDialog(QDialog):
    def __init__(self, mw, media: List[M.MediaItem]):
        super().__init__(mw)
        self.mw, self.media = mw, media
        self.setWindowTitle(f"トランスコード（{len(media)} 個）")
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        f = QFormLayout()
        self.preset = QComboBox()
        last = mw.settings.value("transcode_preset", "dnxhr_hq")
        for p in MC.PRESETS:
            self.preset.addItem(f"{p.group}: {p.label}" + (f"（{p.note}）" if p.note else ""), p.id)
        self.preset.setCurrentIndex(max(0, self.preset.findData(last)))
        f.addRow("形式", self.preset)
        self.size = QComboBox()
        for k, lab in MC.SIZES:
            self.size.addItem(lab, k)
        f.addRow("大きさ", self.size)
        self.dest = QComboBox()
        self.dest.addItem("プロジェクトの Transcoded フォルダ", "project")
        self.dest.addItem("元のファイルと同じフォルダ", "same")
        self.dest.addItem("フォルダを選ぶ...", "pick")
        self.dest.addItem("ファーム（レンダーノードに振り分け・この PC は空く）", "farm")
        if not mw.project_path:
            self.dest.setCurrentIndex(1)
        f.addRow("保存先", self.dest)
        self.after = QComboBox()
        self.after.addItem("変換したファイルに差し替える（元のパスは残す）", "replace")
        self.after.addItem("ビンに追加する（元はそのまま）", "add")
        self.after.addItem("ファイルを作るだけ", "none")
        f.addRow("変換後", self.after)
        v.addLayout(f)
        self.info = QLabel("DNxHR は Avid・Resolve、ProRes は Final Cut・Premiere 向け。透明が要るなら ProRes 4444 / Animation / PNG")
        self.info.setObjectName("info")
        self.info.setWordWrap(True)
        v.addWidget(self.info)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.bb = QDialogButtonBox()
        self.go = self.bb.addButton("変換する", QDialogButtonBox.ButtonRole.AcceptRole)
        self.close_btn = self.bb.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        self.go.clicked.connect(self.start)
        self.close_btn.clicked.connect(self._close)
        v.addWidget(self.bb)
        self.worker = None
        self.result = None

    def out_dir(self, src: str) -> Optional[Path]:
        k = self.dest.currentData()
        if k == "project" and self.mw.project_path:
            return Path(self.mw.project_path).parent / "Transcoded"
        if k == "pick":
            if not getattr(self, "_picked", None):
                d = QFileDialog.getExistingDirectory(self, "保存先")
                if not d:
                    return None
                self._picked = d
            return Path(self._picked)
        return Path(src).parent

    def start(self):
        preset, size = self.preset.currentData(), self.size.currentData()
        self.mw.settings.setValue("transcode_preset", preset)
        if self.dest.currentData() == "farm":
            dst = str(Path(self.mw.project_path).parent / "Transcoded") if self.mw.project_path else ""
            if dst:
                os.makedirs(dst, exist_ok=True)
            try:
                jobs = self.mw.farm_submit_transcode([m for m in self.media if m.kind in ("video", "audio")], preset, size,
                                                     dst, self.after.currentData())
            except Exception as ex:
                QMessageBox.warning(self, "ファーム", f"送れませんでした: {ex}")
                return
            self.result = ([], [])
            self.info.setText(f"{len(jobs)} 件をファームに送りました。終わると自動で取り込みます（「ジョブ（ファーム）」で確認）")
            self.go.setEnabled(False)
            return
        jobs = []
        for m in self.media:
            if m.kind not in ("video", "audio") or MD.is_offline(m.path):
                continue
            d = self.out_dir(m.path)
            if d is None:
                return
            dst = d / MC.default_name(m.path, preset, size)
            k = 1
            while dst.exists():
                dst = d / f"{Path(MC.default_name(m.path, preset, size)).stem}_{k}{MC.BY_ID[preset].ext}"
                k += 1
            jobs.append((m.id, m.path, str(dst)))
        if not jobs:
            QMessageBox.information(self, "トランスコード", "変換できる素材（オンラインの動画・音声）がありません。")
            return
        self.worker = TranscodeWorker(jobs, preset, size)
        self.worker.progress.connect(self._prog)
        self.worker.done.connect(self._done)
        self.bar.setVisible(True)
        self.bar.setRange(0, 1000)
        self.go.setEnabled(False)
        self.close_btn.setText("中止")
        self.worker.start()

    def _prog(self, i, n, x, name):
        self.bar.setValue(int((i + x) / max(1, n) * 1000))
        self.info.setText(f"{i + 1} / {n}  {name}  {x * 100:.0f}%")

    def _done(self, ok, ng):
        self.worker = None
        self.result = (ok, ng)
        self.go.setEnabled(True)
        self.close_btn.setText("閉じる")
        self.mw.apply_transcoded(ok, self.after.currentData())
        msg = f"{len(ok)} 個を変換しました"
        if ng:
            msg += "\n失敗: " + " / ".join(f"{n}: {e}" for n, e in ng[:3])
        self.info.setText(msg)

    def _close(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(5000)
            self.worker = None
            self.info.setText("中止しました")
            self.go.setEnabled(True)
            self.close_btn.setText("閉じる")
            return
        self.accept()


class Features7Mixin:
    def _feature7_commands(self) -> dict:
        return {
            "transcode": self.transcode_dialog, "export_audio_only": self.export_audio_only_dialog,
            "export_gif": self.export_gif_dialog, "export_images": self.export_image_sequence_dialog,
            "archive_project": self.archive_project_dialog, "thumb_candidates": self.thumbnail_candidates,
            "j_cut": lambda: self.jl_cut("j"), "l_cut": lambda: self.jl_cut("l"),
            "flip_h": lambda: self.flip_clips("h"), "flip_v": lambda: self.flip_clips("v"),
            "color_match": self.color_match_dialog, "auto_color": self.auto_color,
            "sync_audio": self.sync_by_audio, "stabilize": self.stabilize_selected,
            "split_screen": self.split_screen, "still_length": self.still_length_dialog,
            "split_every": self.split_every_dialog, "reverse_order": lambda: self.reorder_clips("reverse"),
            "shuffle_order": lambda: self.reorder_clips("shuffle"), "fit_to_markers": self.fit_to_markers,
            "media_usage": self.media_usage_dialog, "burn_timecode": self.burn_timecode,
            "watermark": self.watermark_dialog, "letterbox": self.letterbox,
            "bars_tone": self.bars_and_tone, "split_long_telops": self.split_long_telops,
            "telops_by_length": self.telops_by_length,
        }

    def _build_feature7_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "変換・仕上げ")
        for c in ("farm_panel", "farm_export", None, "transcode", "export_audio_only", "export_gif", "export_images", "archive_project", "thumb_candidates",
                  None, "color_match", "auto_color", "stabilize", "flip_h", "flip_v", "split_screen", None,
                  "j_cut", "l_cut", "sync_audio", "still_length", "split_every", "reverse_order", "shuffle_order",
                  "fit_to_markers", "media_usage", None, "burn_timecode", "watermark", "letterbox", "bars_tone", None,
                  "split_long_telops", "telops_by_length"):
            m.addSeparator() if c is None else self._a(m, c)

    def _sel7(self, kinds=None) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if kinds is None or c.kind in kinds]

    def _range7(self, max_sec: float = 0) -> Tuple[int, int]:
        seq = self.project.sequence
        a, b = (seq.mark_in, seq.mark_out) if 0 <= seq.mark_in < seq.mark_out else (0, seq.duration)
        if max_sec:
            b = min(b, a + int(max_sec * seq.fps))
        return a, b

    def _new_track7(self) -> M.Track:
        seq = self.project.sequence
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        return t

    def _info(self, title, text):
        QMessageBox.information(self, title, text)

    # ─────────────────────────── トランスコード ───────────────────────────
    def transcode_dialog(self, media: Optional[List[M.MediaItem]] = None):
        if media is None:
            ids = self.bin.selected_ids() or list({c.media_id for c in self._sel7() if c.media_id})
            media = [m for m in self.project.media if m.id in ids]
        if not media:
            self._info("トランスコード", "ビンかタイムラインで、変換したい素材を選んでください。")
            return None
        dlg = TranscodeDialog(self, media)
        dlg.show()
        self._tc_dlg = dlg
        return dlg

    def apply_transcoded(self, done: List[Tuple[str, str]], how: str):
        if not done or how == "none":
            return
        if how == "replace":
            self.begin_edit()
            for mid, path in done:
                m = self.project.media_by_id(mid)
                if m is None:
                    continue
                m.original_path = m.original_path or m.path
                m.path = path
                try:
                    info = MD.probe(path)
                    m.width, m.height, m.fps, m.file_size, m.mtime = info.width, info.height, info.fps, info.file_size, info.mtime
                except Exception:
                    pass
            self.end_edit(f"変換した素材に差し替え（{len(done)} 個）")
            self.bin.refresh()
        else:
            self.import_files([p for _m, p in done])

    def transcode_now(self, media_ids: List[str], preset: str, size: str = "", dest: str = "", how: str = "add") -> list:
        """テスト・スクリプト用: 待って変換する。"""
        out = []
        for mid in media_ids:
            m = self.project.media_by_id(mid)
            d = Path(dest) if dest else Path(m.path).parent
            p = str(d / MC.default_name(m.path, preset, size))
            MC.transcode(m.path, p, preset, size)
            out.append((mid, p))
        if how != "add":
            self.apply_transcoded(out, how)
        return out

    # ─────────────────────────── 書き出し（音声だけ・GIF・連番・zip・サムネイル）───────────────────────────
    def export_audio_only_dialog(self):
        items = [(p.id, p.label) for p in MC.PRESETS if not p.vcodec]
        n, ok = QInputDialog.getItem(self, "音声だけ書き出し", "形式（IN〜OUT、無ければ全体）:", [x[1] for x in items], 0, False)
        if not ok:
            return
        pid = items[[x[1] for x in items].index(n)][0]
        seq = self.project.sequence
        path, _ = QFileDialog.getSaveFileName(self, "音声を保存", f"{seq.name}{MC.BY_ID[pid].ext}", f"*{MC.BY_ID[pid].ext}")
        if path:
            self._run_task("音声だけ書き出し", lambda task: self.export_audio_only(path, pid, task),
                           on_done=lambda p: self.status.showMessage(f"書き出しました: {p}", 6000))

    def export_audio_only(self, path: str, preset_id: str, task=None) -> str:
        import av
        from . import render as R
        seq = self.project.sequence
        a, b = self._range7()
        pr = MC.BY_ID[preset_id]
        pool = MD.DecoderPool()
        s0, s1 = int(round(a * MD.SAMPLE_RATE / seq.fps)), int(round(b * MD.SAMPLE_RATE / seq.fps))
        with MC.open_output(path, pr) as oc:
            st = MC.add_audio_stream(oc, pr, MD.SAMPLE_RATE, 2)
            res = av.AudioResampler(format=MC.audio_format(pr), layout="stereo", rate=MD.SAMPLE_RATE)
            step = MD.SAMPLE_RATE * 2
            for s in range(s0, s1, step):
                n = min(step, s1 - s)
                buf = R.mix_audio(seq, self.project, pool, s, n)
                af = av.AudioFrame.from_ndarray(np.ascontiguousarray(np.clip(buf, -1, 1).T.astype(np.float32)),
                                                format="fltp", layout="stereo")
                af.sample_rate = MD.SAMPLE_RATE
                for rf in res.resample(af):
                    rf.pts = None
                    for p in st.encode(rf):
                        oc.mux(p)
                if task is not None:
                    task.progress.emit(s - s0, s1 - s0, "書き出し中")
            for p in st.encode(None):
                oc.mux(p)
        return path

    def export_gif_dialog(self):
        seq = self.project.sequence
        path, _ = QFileDialog.getSaveFileName(self, "GIF を保存", f"{seq.name}.gif", "GIF (*.gif)")
        if not path:
            return
        w, ok = QInputDialog.getInt(self, "GIF", "幅（px）:", 480, 120, 1280, 40)
        if ok:
            self._run_task("GIF を書き出し", lambda task: self.export_gif(path, w, 12, task),
                           on_done=lambda p: self.status.showMessage(f"GIF を書き出しました: {p}", 6000))

    def export_gif(self, path: str, width: int = 480, fps: int = 12, task=None) -> str:
        """IN〜OUT（最大 30 秒）を GIF に。"""
        from PIL import Image
        from . import render as R
        seq = self.project.sequence
        a, b = self._range7(30)
        sc = min(1.0, width / seq.width)
        pool = MD.DecoderPool()
        frames = []
        step = seq.fps / fps
        n = max(1, int((b - a) / step))
        for i in range(n):
            if task is not None and task.cancelled:
                break
            im = Image.fromarray(R.render_frame_np(seq, int(a + i * step), self.project, pool, sc))
            frames.append(im.convert("P", palette=Image.ADAPTIVE, colors=256))
            if task is not None:
                task.progress.emit(i, n, "")
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=int(1000 / fps), loop=0, optimize=True)
        return path

    def export_image_sequence_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "連番画像の保存先")
        if not d:
            return
        fmt, ok = QInputDialog.getItem(self, "連番画像", "形式:", ["PNG（透明あり）", "JPEG"], 0, False)
        if ok:
            self._run_task("連番画像を書き出し", lambda task: self.export_image_sequence(d, "png" if fmt.startswith("PNG") else "jpg", task),
                           on_done=lambda n: self.status.showMessage(f"{n} 枚を書き出しました: {d}", 6000))

    def export_image_sequence(self, folder: str, fmt: str = "png", task=None) -> int:
        from PIL import Image
        from . import render as R
        seq = self.project.sequence
        a, b = self._range7()
        pool = MD.DecoderPool()
        os.makedirs(folder, exist_ok=True)
        stem = "".join(ch for ch in seq.name if ch not in '\\/:*?"<>|') or "frame"
        for i, f in enumerate(range(a, b)):
            if task is not None and task.cancelled:
                return i
            if fmt == "png":
                Image.fromarray(R.render_frame_rgba(seq, f, self.project, pool, 1.0), "RGBA").save(
                    os.path.join(folder, f"{stem}_{i:06d}.png"))
            else:
                Image.fromarray(R.render_frame_np(seq, f, self.project, pool, 1.0)).save(
                    os.path.join(folder, f"{stem}_{i:06d}.jpg"), quality=95)
            if task is not None:
                task.progress.emit(i, b - a, "")
        return b - a

    def archive_project_dialog(self):
        if not self.project_path:
            self._info("プロジェクトを zip に", "先にプロジェクトを保存してください。")
            return
        path, _ = QFileDialog.getSaveFileName(self, "zip を保存", str(Path(self.project_path).with_suffix(".zip")), "zip (*.zip)")
        if path:
            self._run_task("プロジェクトを zip に", lambda task: self.archive_project(path, task),
                           on_done=lambda r: self.status.showMessage(f"{r['files']} 個のファイルをまとめました: {path}", 6000))

    def archive_project(self, path: str, task=None) -> dict:
        """プロジェクトと使っている素材を 1 つの zip に（素材は Media/ に入れ、プロジェクトからの相対パスでつなぐ）。"""
        proj = M.Project.from_json(self.project.to_json())
        used = {c.media_id for s in proj.sequences for _, c in s.all_clips() if c.media_id}
        files, names = [], set()
        for m in proj.media:
            if m.id not in used or not os.path.isfile(m.path):
                continue
            arc = Path(m.path).name
            k = 1
            while arc.lower() in names:
                arc = f"{Path(m.path).stem}_{k}{Path(m.path).suffix}"
                k += 1
            names.add(arc.lower())
            files.append((m.path, "Media/" + arc))
            m.original_path = m.original_path or m.path
            m.path = m.rel_path = "Media/" + arc
        pname = Path(self.project_path).name if self.project_path else "project.tcut"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            zf.writestr(pname, proj.to_json())
            for i, (src, arc) in enumerate(files):
                if task is not None and task.cancelled:
                    break
                zf.write(src, arc)
                if task is not None:
                    task.progress.emit(i, len(files), Path(src).name)
        return {"files": len(files) + 1, "path": path}

    def thumbnail_candidates(self, count: int = 6, folder: str = "") -> List[str]:
        """くっきりして色のある場面を自動で選んで PNG に（サムネイルの候補）。マーカーも打つ。"""
        import cv2
        from PIL import Image
        from . import render as R
        seq = self.project.sequence
        if seq.duration <= 0:
            return []
        pool = MD.DecoderPool()
        n = min(80, max(count, seq.duration // max(1, int(seq.fps))))
        cands = []
        for i in range(n):
            f = int((i + 0.5) * seq.duration / n)
            a = R.render_frame_np(seq, f, self.project, pool, min(1.0, 320 / seq.width))
            g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
            sharp = float(cv2.Laplacian(g, cv2.CV_64F).var())
            rg, yb = a[..., 0].astype(float) - a[..., 1], (a[..., 0].astype(float) + a[..., 1]) / 2 - a[..., 2]
            colorful = float(np.sqrt(rg.std() ** 2 + yb.std() ** 2))
            bright = float(g.mean())
            score = math.log1p(sharp) * (1 + colorful / 50) * (0.3 if bright < 30 or bright > 235 else 1.0)
            cands.append((score, f))
        cands.sort(reverse=True)
        pick: List[int] = []
        gap = int(seq.fps * 2)
        for _s, f in cands:
            if all(abs(f - p) >= gap for p in pick):
                pick.append(f)
            if len(pick) >= count:
                break
        pick.sort()
        d = Path(folder) if folder else ((Path(self.project_path).parent / "Thumbnails") if self.project_path
                                          else Path(SETTINGS.cache_dir) / "thumbs_out")
        d.mkdir(parents=True, exist_ok=True)
        out = []
        self.begin_edit()
        for k, f in enumerate(pick):
            p = d / f"{seq.name}_thumb{k + 1}_{f}.png"
            Image.fromarray(R.render_frame_np(seq, f, self.project, pool, min(1.0, 1280 / seq.width))).save(p)
            out.append(str(p))
            seq.markers.append(M.Marker(f, f"サムネ候補 {k + 1}", "#FF66CC"))
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit(f"サムネイル候補（{len(out)} 枚）")
        self.status.showMessage(f"サムネイル候補 {len(out)} 枚: {d}", 8000)
        return out

    # ─────────────────────────── 編集 ───────────────────────────
    def jl_cut(self, kind: str, frames: Optional[int] = None) -> int:
        """J カット（音が先に始まる）/ L カット（音が後まで残る）。選んだ映像にリンクした音声を伸ばす。"""
        seq = self.project.sequence
        if frames is None:
            frames, ok = QInputDialog.getInt(self, "J カット" if kind == "j" else "L カット", "ずらすフレーム数:",
                                             int(seq.fps // 2), 1, int(seq.fps * 10))
            if not ok:
                return 0
        audio = {}
        for t, c in self.canvas.selected_clips():
            if t.kind == "audio":
                audio[c.id] = (t, c)
            elif c.link:
                for t2, o in seq.linked(c):
                    if t2.kind == "audio":
                        audio[o.id] = (t2, o)
        if not audio:
            self._info("J / L カット", "音声付きの映像（リンクした音声）か音声クリップを選んでください。")
            return 0
        n = 0
        self.begin_edit()
        for t, c in audio.values():
            m = self.project.media_by_id(c.media_id)
            if kind == "j":
                d = min(frames, c.src_in, c.start)
                if d <= 0:
                    continue
                M.clear_range(t, c.start - d, c.start, ignore={c.id})
                c.start, c.src_in, c.duration = c.start - d, c.src_in - d, c.duration + d
            else:
                room = (int(m.duration_s * seq.fps) - (c.src_in + c.duration)) if m and m.duration_s else frames
                d = min(frames, room)
                if d <= 0:
                    continue
                M.clear_range(t, c.end, c.end + d, ignore={c.id})
                c.duration += d
            t.sort()
            n += 1
        self.end_edit(("J" if kind == "j" else "L") + f" カット（{n} 個）")
        return n

    def flip_clips(self, axis: str) -> int:
        clips = self._sel7(("video", "image", "character", "shape", "nest", "color"))
        key = "flip_h" if axis == "h" else "flip_v"
        on = not all(c.props.get(key) for c in clips)
        self.begin_edit()
        for c in clips:
            if on:
                c.props[key] = True
            else:
                c.props.pop(key, None)
        self.end_edit(("左右" if axis == "h" else "上下") + f"反転（{len(clips)} 個）")
        return len(clips)

    def _clip_pixels(self, c: M.Clip, size=(320, 180)) -> Optional[np.ndarray]:
        from . import render as R
        m = self.project.media_by_id(c.media_id) if c.media_id else None
        if m is None or MD.is_offline(m.path):
            return None
        pool = MD.DecoderPool()
        try:
            if c.kind == "video":
                d = pool.video(m.path)
                return d.get_array(R.src_frame(c, c.start + c.duration // 2) / self.project.sequence.fps, size) if d else None
            if c.kind == "image":
                im = pool.image(m.path)
                return np.asarray(im.convert("RGB").resize(size)) if im is not None else None
        finally:
            pool.close()
        return None

    def _set_rgb_gain(self, c: M.Clip, gains, offs):
        fx = [x for x in (c.prop("effects") or []) if x.get("name") != "rgb_gain"]
        fx.append({"name": "rgb_gain", "on": True, "params": {"gr": round(gains[0], 3), "gg": round(gains[1], 3),
                                                              "gb": round(gains[2], 3), "or": round(offs[0], 1),
                                                              "og": round(offs[1], 1), "ob": round(offs[2], 1)}})
        c.props["effects"] = fx

    def color_match_dialog(self):
        clips = self._sel7(("video", "image"))
        if len(clips) < 2:
            self._info("カラーマッチ", "お手本のクリップと、色を合わせたいクリップを 2 つ以上選んでください。")
            return
        names = [f"{c.name}（{self.tc_str(c.start)}）" for c in clips]
        n, ok = QInputDialog.getItem(self, "カラーマッチ", "お手本にするクリップ:", names, 0, False)
        if ok:
            self.color_match(clips[names.index(n)], [c for c in clips if c is not clips[names.index(n)]])

    def color_match(self, ref: M.Clip, targets: List[M.Clip]) -> int:
        """お手本の色（RGB それぞれの平均と幅）に合わせる（Premiere / Resolve のカラーマッチの簡易版）。"""
        ra = self._clip_pixels(ref)
        if ra is None:
            return 0
        rm, rs = ra.reshape(-1, 3).mean(0), ra.reshape(-1, 3).std(0) + 1e-3
        n = 0
        self.begin_edit()
        for c in targets:
            ta = self._clip_pixels(c)
            if ta is None:
                continue
            tm, ts = ta.reshape(-1, 3).mean(0), ta.reshape(-1, 3).std(0) + 1e-3
            g = np.clip(rs / ts, 0.3, 3.0)
            self._set_rgb_gain(c, g, rm - g * tm)
            n += 1
        self.end_edit(f"カラーマッチ（{n} 個）")
        return n

    def auto_color(self) -> int:
        """自動レベル補正: いちばん暗い所を黒・明るい所を白に（RGB それぞれ。色かぶりも取れる）。"""
        n = 0
        self.begin_edit()
        for c in self._sel7(("video", "image")):
            a = self._clip_pixels(c)
            if a is None:
                continue
            px = a.reshape(-1, 3).astype(np.float32)
            lo, hi = np.percentile(px, 0.5, axis=0), np.percentile(px, 99.5, axis=0)
            g = np.clip(255.0 / np.maximum(1.0, hi - lo), 0.5, 3.0)
            self._set_rgb_gain(c, g, -lo * g)
            n += 1
        self.end_edit(f"オートカラー（{n} 個）")
        return n

    @staticmethod
    def _envelope(m: M.MediaItem, start_s: float, dur_s: float, rate: int = 1000) -> Optional[np.ndarray]:
        src = MD.AudioSource(m.path)
        if src.data is None:
            return None
        buf = src.read(int(start_s * MD.SAMPLE_RATE), int(dur_s * MD.SAMPLE_RATE))
        if buf.size == 0:
            return None
        x = np.abs(buf.astype(np.float32)).mean(axis=1)
        k = MD.SAMPLE_RATE // rate
        x = x[:len(x) // k * k].reshape(-1, k).mean(axis=1)
        return x - x.mean()

    def sync_by_audio(self, max_sec: float = 60.0) -> Optional[int]:
        """2 つのクリップの音（カメラの音と外部録音など）の形を比べて、ぴったり合う位置へ動かす（PluralEyes 風）。"""
        seq = self.project.sequence
        clips = [c for c in self._sel7(("video", "audio")) if c.media_id and self.project.media_by_id(c.media_id)
                 and (self.project.media_by_id(c.media_id).has_audio or c.kind == "audio")]
        uniq: Dict[str, M.Clip] = {}
        for c in sorted(clips, key=lambda c: (c.kind != "video", c.start)):
            key = c.link or c.id
            uniq.setdefault(key, c)
        cl = list(uniq.values())
        if len(cl) != 2:
            self._info("音声で同期", "合わせたい 2 つのクリップ（どちらも音があるもの）を選んでください。")
            return None
        ref, mov = sorted(cl, key=lambda c: c.start)
        rate = 1000
        ma, mb = self.project.media_by_id(ref.media_id), self.project.media_by_id(mov.media_id)
        xa = self._envelope(ma, ref.src_in / seq.fps, min(max_sec, ref.duration / seq.fps), rate)
        xb = self._envelope(mb, mov.src_in / seq.fps, min(max_sec, mov.duration / seq.fps), rate)
        if xa is None or xb is None or xa.std() < 1e-6 or xb.std() < 1e-6:
            self._info("音声で同期", "音声の準備ができていないか、音がありません。少し待ってからもう一度。")
            return None
        n = 1 << int(math.ceil(math.log2(len(xa) + len(xb))))
        c = np.fft.irfft(np.fft.rfft(xa, n) * np.conj(np.fft.rfft(xb, n)), n)
        k = int(np.argmax(c))
        lag = k if k < n // 2 else k - n                      # 秒 × rate
        new_start = ref.start + int(round(lag / rate * seq.fps))
        d = new_start - mov.start
        if new_start < 0:                                    # 前に出せない → お手本の方を後ろへ
            ref, mov, d = mov, ref, -d
        ids = {mov.id} | {o.id for _t, o in seq.linked(mov)}
        starts = {cid: seq.find(cid)[1].start + d for cid in ids}
        if self._move_selected(starts, f"音声で同期（{d:+d} フレーム）"):
            return d
        return None

    def stabilize_selected(self, wait: bool = False):
        """手ぶれ補正（Warp Stabilizer の簡易版）: 画面全体の揺れを測って、逆に動かす（少し拡大して端を隠す）。"""
        clips = self._sel7(("video",))
        if not clips:
            self._info("手ぶれ補正", "映像クリップを選んでください。")
            return None
        seq = self.project.sequence
        c = clips[0]
        m = self.project.media_by_id(c.media_id)
        if m is None or MD.is_offline(m.path):
            self._info("手ぶれ補正", "素材がオフラインです。")
            return None
        from . import render as R

        def work(task, *_a):
            import cv2
            dec = MD.VideoDecoder(m.path)
            sw = 320
            sh = max(2, int(round(sw * (m.height or 9) / max(1, m.width or 16))))
            prev = None
            traj = [(0.0, 0.0)]
            win = cv2.createHanningWindow((sw, sh), cv2.CV_32F)
            for i, f in enumerate(range(c.start, c.end)):
                if task.cancelled:
                    return None
                a = dec.get_array(R.src_frame(c, f) / seq.fps, (sw, sh))
                if a is None:
                    traj.append(traj[-1])
                    continue
                g = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.float32)
                if prev is not None:
                    (dx, dy), _r = cv2.phaseCorrelate(prev, g, win)
                    traj.append((traj[-1][0] + dx, traj[-1][1] + dy))
                prev = g
                if i % 10 == 0:
                    task.progress.emit(i, c.duration, "揺れを測っています")
            dec.close()
            return sw, traj[:c.duration]

        def apply(res):
            if not res:
                return None
            sw, traj = res
            t = np.array(traj)
            r = int(seq.fps // 2)
            pad = np.pad(t, ((r, r), (0, 0)), mode="edge")
            k = np.ones(2 * r + 1) / (2 * r + 1)
            smooth = np.stack([np.convolve(pad[:, i], k, mode="valid") for i in range(2)], 1)
            corr = smooth - t
            fit = min(seq.width / max(1, m.width), seq.height / max(1, m.height)) * (m.width or seq.width) / sw
            zoom = 1.08
            base_s = float(c.prop("scale", 1.0) or 1.0)
            bx, by = float(c.prop("pos_x", 0) or 0), float(c.prop("pos_y", 0) or 0)
            self.begin_edit()
            cc = self.project.sequence.find(c.id)[1]
            keys = dict(cc.props.get("keys") or {})
            keys["pos_x"] = [[i, round(bx + corr[i, 0] * fit * base_s * zoom, 2), "linear"] for i in range(len(corr))]
            keys["pos_y"] = [[i, round(by + corr[i, 1] * fit * base_s * zoom, 2), "linear"] for i in range(len(corr))]
            cc.props["keys"] = keys
            cc.props["scale"] = round(base_s * zoom, 4)
            cc.props["stabilized"] = True
            self.end_edit("手ぶれ補正")
            self.status.showMessage(f"手ぶれ補正: 最大 {np.abs(corr).max() * fit:.0f} px の揺れを打ち消しました（8% 拡大）", 6000)
            return cc
        if wait:
            return apply(work(_T()))
        self._run_task("手ぶれ補正", work, on_done=apply)
        return None

    def split_screen(self) -> int:
        """選んだクリップを分割画面に（2 つ = 左右、3〜4 = 2×2、5〜9 = 3×3）。"""
        seq = self.project.sequence
        clips = sorted(self._sel7(("video", "image", "nest")), key=lambda c: (c.start, c.id))
        if len(clips) < 2:
            self._info("分割画面", "並べたいクリップを 2 つ以上選んでください（別々のトラックに重ねて置いておく）。")
            return 0
        k = len(clips)
        cols = 2 if k <= 4 else 3
        rows = 1 if k == 2 else math.ceil(k / cols)
        W, H = seq.width, seq.height
        cw, ch = W / cols, H / rows
        self.begin_edit()
        for i, c in enumerate(clips[:cols * rows]):
            m = self.project.media_by_id(c.media_id) if c.media_id else None
            mw_, mh_ = (m.width, m.height) if m and m.width else (W, H)
            f = min(W / mw_, H / mh_)
            fw, fh = mw_ * f, mh_ * f
            s = min(cw / fw, ch / fh)
            col, row = i % cols, i // cols
            c.props.update({"scale": round(s, 4), "pos_x": round((col + 0.5) * cw - W / 2, 1),
                            "pos_y": round((row + 0.5) * ch - H / 2, 1)})
            c.props.pop("anim", None)
        self.end_edit(f"分割画面（{cols}×{rows}）")
        return k

    def still_length_dialog(self):
        v, ok = QInputDialog.getDouble(self, "静止画の長さ", "選んだ画像の長さ（秒）:", 3.0, 0.1, 600, 1)
        if ok:
            self.still_length(v)

    def still_length(self, sec: float) -> int:
        """選んだ静止画の長さをそろえる（後ろのクリップはその分ずらす）。"""
        seq = self.project.sequence
        n = int(round(sec * seq.fps))
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind == "image"]
        self.begin_edit()
        for t, c in sorted(sel, key=lambda tc: -tc[1].start):
            d = n - c.duration
            if d:
                M.shift_after(t, c.end, d, ignore={c.id})
                c.duration = n
        self.end_edit(f"静止画の長さを {sec:g} 秒に（{len(sel)} 個）")
        return len(sel)

    def split_every_dialog(self):
        v, ok = QInputDialog.getDouble(self, "N 秒ごとに分割", "間隔（秒）:", 2.0, 0.1, 600, 1)
        if ok:
            self.split_every(v)

    def split_every(self, sec: float) -> int:
        seq = self.project.sequence
        step = max(1, int(round(sec * seq.fps)))
        n = 0
        self.begin_edit()
        for t, c in list(self.canvas.selected_clips()):
            for f in range(c.start + step, c.end, step):
                if M.split_clip(t, f):
                    n += 1
        self.end_edit(f"{sec:g} 秒ごとに分割（{n} か所）")
        return n

    def _layout(self, changes: Dict[str, Tuple[int, int]], label: str) -> bool:
        """{id: (開始, 長さ)} を、ほかのクリップと重ならなければ反映。"""
        seq = self.project.sequence
        for t in seq.tracks():
            mine = [c for c in t.clips if c.id in changes]
            others = [c for c in t.clips if c.id not in changes]
            for c in mine:
                a, d = changes[c.id]
                if a < 0 or any(o.start < a + d and a < o.end for o in others):
                    self._info(label, "ほかのクリップと重なるのでできません。")
                    return False
        self.begin_edit()
        for t in seq.tracks():
            for c in t.clips:
                if c.id in changes:
                    c.start, c.duration = changes[c.id]
            t.sort()
        self.end_edit(label)
        return True

    def reorder_clips(self, how: str, seed: Optional[int] = None) -> bool:
        """選んだクリップの順番を逆に / ばらばらに（トラックごと。リンクした音声も一緒）。"""
        seq = self.project.sequence
        by_track: Dict[int, List[M.Clip]] = {}
        for t, c in self.canvas.selected_clips():
            if t.kind == "video" or not c.link:
                by_track.setdefault(id(t), []).append(c)
        ch: Dict[str, Tuple[int, int]] = {}
        for cl in by_track.values():
            cl.sort(key=lambda c: c.start)
            order = list(reversed(cl)) if how == "reverse" else random.Random(seed).sample(cl, len(cl))
            f = cl[0].start
            for c in order:
                ch[c.id] = (f, c.duration)
                for _t, o in seq.linked(c):
                    ch[o.id] = (f + (o.start - c.start), o.duration)
                f += c.duration
        return self._layout(ch, "逆順に並べる" if how == "reverse" else "シャッフル") if ch else False

    def fit_to_markers(self) -> bool:
        """選んだクリップを、マーカー（ビートなど）の位置に順に並べ、次のマーカーまでの長さにする。"""
        seq = self.project.sequence
        clips = sorted(self._sel7(("video", "image", "color", "nest")), key=lambda c: c.start)
        if not clips:
            return False
        ms = sorted({m.frame for m in seq.markers if m.frame >= clips[0].start})
        if len(ms) < 2:
            self._info("マーカーに合わせる", "選んだクリップより後ろに、マーカーが 2 つ以上必要です（ビート検出で打てます）。")
            return False
        ch: Dict[str, Tuple[int, int]] = {}
        for c, a, b in zip(clips, ms, ms[1:]):
            d = b - a
            m = self.project.media_by_id(c.media_id) if c.media_id else None
            if c.kind == "video" and m and m.duration_s:
                d = min(d, int(m.duration_s * seq.fps) - c.src_in)
            ch[c.id] = (a, max(1, d))
            for _t, o in seq.linked(c):
                ch[o.id] = (a, max(1, d))
        return self._layout(ch, f"マーカーに合わせて並べる（{len(ch)} 個）")

    def media_usage(self, media_id: str) -> List[Tuple[str, str, int, str]]:
        out = []
        for s in self.project.sequences:
            for t, c in s.all_clips():
                if c.media_id == media_id:
                    out.append((s.id, s.name, c.start, t.name))
        return out

    def media_usage_dialog(self):
        ids = self.bin.selected_ids() or [c.media_id for c in self._sel7() if c.media_id]
        if not ids:
            self._info("使用箇所", "ビンかタイムラインで素材を選んでください。")
            return
        m = self.project.media_by_id(ids[0])
        uses = self.media_usage(ids[0])
        dlg = QDialog(self)
        dlg.setWindowTitle(f"使用箇所: {m.name}")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"{len(uses)} か所で使っています　ダブルクリックでそのシーケンスの位置へ" if uses else "どこでも使っていません"))
        lw = QListWidget()
        for sid, sname, f, tname in uses:
            it = QListWidgetItem(f"{sname}  {tname}  {self.tc_str(f)}")
            it.setData(Qt.ItemDataRole.UserRole, (sid, f))
            lw.addItem(it)

        def go(it):
            sid, f = it.data(Qt.ItemDataRole.UserRole)
            self.load_sequence(sid)
            self.seek_record(f)
        lw.itemDoubleClicked.connect(go)
        v.addWidget(lw, 1)
        dlg.resize(480, 320)
        dlg.show()
        self._usage_dlg = dlg

    # ─────────────────────────── 重ねる物 ───────────────────────────
    def burn_timecode(self, label: str = "") -> M.Clip:
        """タイムコードの焼き込み（確認用の書き出しに。シーケンス全体・一番上）。"""
        seq = self.project.sequence
        self.begin_edit()
        c = M.Clip(M.new_id(), "shape", 0, max(1, seq.duration), name="タイムコード",
                   props={"shape": "timecode", "color": "#FFFFFF", "rect": [0, 90, 100, 7], "fill_opacity": 0.6,
                          "tc_label": label})
        self._new_track7().clips.append(c)
        self.end_edit("タイムコードを焼き込み")
        return c

    def watermark_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "ロゴ画像", "", "画像 (*.png *.jpg *.jpeg *.webp *.psd)")
        if p:
            self.watermark(p)

    def watermark(self, path: str, corner: str = "br", size: float = 0.14, opacity: float = 0.7):
        """ロゴを全編の隅に（透かし）。"""
        seq = self.project.sequence

        def place(items):
            m = next((x for x in items if os.path.normcase(x.path) == os.path.normcase(path)), None) or \
                next((x for x in self.project.media if os.path.normcase(x.path) == os.path.normcase(path)), None)
            if m is None:
                return
            W, H = seq.width, seq.height
            f = min(W / max(1, m.width), H / max(1, m.height))
            fw, fh = m.width * f * size, m.height * f * size
            sx = 1 if corner[1] == "r" else -1
            sy = 1 if corner[0] == "b" else -1
            self.begin_edit()
            c = M.Clip(M.new_id(), "image", 0, max(1, seq.duration), media_id=m.id, name="ウォーターマーク",
                       props={"scale": size, "opacity": opacity, "pos_x": round(sx * (W / 2 - W * 0.03 - fw / 2), 1),
                              "pos_y": round(sy * (H / 2 - H * 0.04 - fh / 2), 1)})
            self._new_track7().clips.append(c)
            self.end_edit("ウォーターマーク")
        existing = next((x for x in self.project.media if os.path.normcase(x.path) == os.path.normcase(path)), None)
        if existing is not None:
            place([existing])
        else:
            self.import_files([path], after=place)

    def letterbox(self, ratio: float = 2.39) -> M.Clip:
        """シネスコ（2.39:1 など）の黒帯を全編に。"""
        seq = self.project.sequence
        bar = max(0.0, (seq.height - seq.width / ratio) / 2 / seq.height * 100)
        self.begin_edit()
        c = M.Clip(M.new_id(), "shape", 0, max(1, seq.duration), name=f"黒帯 {ratio:g}:1",
                   props={"shape": "letterbox", "color": "#000000", "rect": [0, 0, 100, round(bar, 3)], "fill_opacity": 1.0})
        self._new_track7().clips.append(c)
        self.end_edit(f"シネスコの黒帯（{ratio:g}:1）")
        return c

    def bars_and_tone(self, sec: float = 10.0):
        """カラーバーと 1 kHz のトーン（−20 dBFS）を再生ヘッドに。"""
        import wave
        seq = self.project.sequence
        f = self.record_player.position
        n = int(round(sec * seq.fps))
        d = (Path(self.project_path).parent / "Generated") if self.project_path else Path(SETTINGS.cache_dir) / "generated"
        d.mkdir(parents=True, exist_ok=True)
        wav = d / "tone_1kHz_-20dBFS.wav"
        if not wav.exists():
            t = np.arange(int(48000 * 60)) / 48000
            x = (np.sin(2 * np.pi * 1000 * t) * 0.1 * 32767).astype(np.int16)
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(np.repeat(x[:, None], 2, axis=1).tobytes())
        self.begin_edit()
        c = M.Clip(M.new_id(), "shape", f, n, name="カラーバー", props={"shape": "bars", "fill_opacity": 1.0})
        t = next((t for t in seq.video_tracks if not t.locked and t.is_empty(f, f + n)), None) or self._new_track7()
        t.clips.append(c)
        t.sort()
        self.end_edit("カラーバー")

        def place(items):
            m = next((x for x in items if x.kind == "audio"), None) or \
                next((x for x in self.project.media if os.path.normcase(x.path) == os.path.normcase(str(wav))), None)
            if m is None:
                return
            at = next((t for t in seq.audio_tracks if not t.locked and t.is_empty(f, f + n)), None)
            if at is None:
                at = M.Track("audio", f"A{len(seq.audio_tracks) + 1}")
                seq.audio_tracks.append(at)
            self.begin_edit()
            at.clips.append(M.Clip(M.new_id(), "audio", f, n, 0, m.id, "1kHz トーン"))
            at.sort()
            self.end_edit("1kHz トーン")
        existing = next((x for x in self.project.media if os.path.normcase(x.path) == os.path.normcase(str(wav))), None)
        if existing is not None:
            place([existing])
        else:
            self.import_files([str(wav)], after=place)
        return c

    # ─────────────────────────── テロップ ───────────────────────────
    @staticmethod
    def _split_text(t: str, limit: int) -> List[str]:
        """文の切れ目（。！？、）で limit 文字以内に分ける。"""
        s = t.replace("\n", "")
        out, cur = [], ""
        for ch in s:
            cur += ch
            if len(cur) >= limit or (ch in "。！？!?" and len(cur) >= limit * 0.4):
                cut = max(cur.rfind("、"), cur.rfind("，")) if len(cur) >= limit and ch not in "。！？!?" else -1
                if cut > limit * 0.4:
                    out.append(cur[:cut + 1])
                    cur = cur[cut + 1:]
                else:
                    out.append(cur)
                    cur = ""
        if cur.strip():
            out.append(cur)
        return [x.strip() for x in out if x.strip()]

    def split_long_telops(self, limit: Optional[int] = None) -> int:
        """長いテロップ（limit 文字より長い）を、文の切れ目で何枚かに分ける（長さは文字数の割合で）。"""
        if limit is None:
            limit, ok = QInputDialog.getInt(self, "長いテロップを分割", "1 枚の最大文字数:", 26, 6, 100)
            if not ok:
                return 0
        seq = self.project.sequence
        clips = self._text_targets()
        n = 0
        self.begin_edit()
        for c in clips:
            t = str(c.prop("text", ""))
            parts = self._split_text(t, limit)
            if len(parts) <= 1:
                continue
            tr = seq.find(c.id)[0]
            total = sum(len(p) for p in parts)
            f, end0, dur0 = c.start, c.end, c.duration
            for i, p in enumerate(parts):
                d = end0 - f if i == len(parts) - 1 else max(1, int(round(dur0 * len(p) / total)))
                if i == 0:
                    c.props["text"] = p
                    c.duration = d
                else:
                    tr.clips.append(c.copy(id=M.new_id(), start=f, duration=d, props={**c.props, "text": p}))
                f += d
            tr.sort()
            n += 1
        self.end_edit(f"長いテロップを分割（{n} 個）")
        return n

    def telops_by_length(self) -> int:
        """選んだテロップ（無ければ IN〜OUT の中のテロップ）を、文字数の割合で IN〜OUT にすき間なく並べ直す。"""
        seq = self.project.sequence
        if not (0 <= seq.mark_in < seq.mark_out):
            self._info("文字数で並べる", "IN と OUT を設定してください。")
            return 0
        picked = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        clips = sorted(picked, key=lambda c: c.start) if picked else             [c for c in self._text_targets() if c.end > seq.mark_in and c.start < seq.mark_out]
        if not clips:
            return 0
        span = seq.mark_out - seq.mark_in
        w = [max(1, len("".join(str(c.prop("text", "")).split()))) for c in clips]
        tot = sum(w)
        f = seq.mark_in
        self.begin_edit()
        for i, (c, x) in enumerate(zip(clips, w)):
            d = seq.mark_out - f if i == len(clips) - 1 else max(1, int(round(span * x / tot)))
            c.start, c.duration = f, d
            f += d
        for t in seq.tracks():
            t.sort()
        self.end_edit(f"テロップを文字数で並べる（{len(clips)} 個）")
        return len(clips)
