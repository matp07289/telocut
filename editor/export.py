"""シーケンスの書き出し（合成フレームを ffmpeg にパイプ）。"""
from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import threading
import wave

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (QDialog, QFormLayout, QLineEdit, QPushButton, QHBoxLayout,
                             QComboBox, QSpinBox, QProgressBar, QLabel, QFileDialog,
                             QVBoxLayout, QDialogButtonBox, QMessageBox)

from core.timecode import frames_to_timecode
from .media import DecoderPool, SAMPLE_RATE, CHANNELS, ffmpeg_exe
from .model import Project
from . import render

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

ENCODERS = [
    ("H.264 (libx264) / MP4", "libx264", ".mp4"),
    ("H.264 (NVIDIA NVENC) / MP4", "h264_nvenc", ".mp4"),
    ("H.265 (libx265) / MP4", "libx265", ".mp4"),
    ("ProRes 422 HQ / MOV", "prores_ks", ".mov"),
]


class ExportWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, project: Project, a: int, b: int, path: str, encoder: str, quality: int):
        super().__init__()
        self.project = copy.deepcopy(project)
        self.seq = self.project.sequence
        self.a, self.b = a, b
        self.path = path
        self.encoder = encoder
        self.quality = quality
        self.cancel = threading.Event()

    def run(self):
        if self.encoder == "h264_nvenc":
            self._run_ffmpeg_cli()
        else:
            self._run_pyav()

    def _fps_fraction(self):
        from fractions import Fraction
        fps = self.seq.fps
        for nominal, frac in ((29.97, Fraction(30000, 1001)), (59.94, Fraction(60000, 1001)),
                              (23.976, Fraction(24000, 1001))):
            if abs(fps - nominal) < 0.01:
                return frac
        return Fraction(fps).limit_denominator(1001)

    def _run_pyav(self):
        """PyAV（同梱の FFmpeg ライブラリ）で書き出す。外部 ffmpeg 不要。"""
        import av
        import numpy as np
        seq = self.seq
        pool = DecoderPool()
        n = self.b - self.a
        W, H = seq.width & ~1, seq.height & ~1
        out = None
        try:
            out = av.open(self.path, "w")
            rate = self._fps_fraction()
            vs = out.add_stream(self.encoder, rate=rate)
            vs.width, vs.height = W, H
            if self.encoder == "prores_ks":
                vs.pix_fmt = "yuv422p10le"
                vs.options = {"profile": "3"}
            else:
                vs.pix_fmt = "yuv420p"
                vs.options = {"crf": str(self.quality), "preset": "medium"}
            mov = self.path.lower().endswith(".mov")
            ast = out.add_stream("pcm_s16le" if mov else "aac", rate=SAMPLE_RATE)
            ast.layout = "stereo"
            if not mov:
                ast.bit_rate = 192000
            s_total = int(round(self.b * SAMPLE_RATE / seq.fps))
            s_cur = int(round(self.a * SAMPLE_RATE / seq.fps))
            a_pts = 0
            for i, f in enumerate(range(self.a, self.b)):
                if self.cancel.is_set():
                    return
                im = render.render_frame(seq, f, self.project, pool, 1.0)
                if im.size != (W, H):
                    im = im.resize((W, H))
                vf = av.VideoFrame.from_ndarray(np.asarray(im), format="rgb24")
                vf.pts = i
                for pkt in vs.encode(vf):
                    out.mux(pkt)
                # このフレームまでの音声
                s_next = min(s_total, int(round((f + 1) * SAMPLE_RATE / seq.fps)))
                if s_next > s_cur:
                    buf = render.mix_audio(seq, self.project, pool, s_cur, s_next - s_cur)
                    pcm = (np.clip(buf, -1, 1) * 32767).astype(np.int16).reshape(1, -1)
                    af = av.AudioFrame.from_ndarray(pcm, format="s16", layout="stereo")
                    af.sample_rate = SAMPLE_RATE
                    af.pts = a_pts
                    a_pts += s_next - s_cur
                    for pkt in ast.encode(af):
                        out.mux(pkt)
                    s_cur = s_next
                if i % 5 == 0:
                    self.progress.emit(i, n)
            for pkt in vs.encode(None):
                out.mux(pkt)
            for pkt in ast.encode(None):
                out.mux(pkt)
            out.close()
            out = None
            self.progress.emit(n, n)
            self.finished_ok.emit(self.path)
        except Exception as ex:
            self.failed.emit(f"書き出しに失敗しました: {ex}")
        finally:
            pool.close()
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
                if self.cancel.is_set():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass

    def _run_ffmpeg_cli(self):
        """外部 ffmpeg 経由（NVENC など PyAV に無いエンコーダ用）。"""
        if not ffmpeg_exe():
            self.failed.emit("NVENC での書き出しには ffmpeg が必要です（PATH に ffmpeg を置いてください）。")
            return
        seq = self.seq
        pool = DecoderPool()
        n = self.b - self.a
        tmpwav = None
        try:
            # 1) 音声ミックスを一時 WAV に
            fd, tmpwav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            s0 = int(round(self.a * SAMPLE_RATE / seq.fps))
            s1 = int(round(self.b * SAMPLE_RATE / seq.fps))
            with wave.open(tmpwav, "wb") as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                step = SAMPLE_RATE * 5
                for s in range(s0, s1, step):
                    if self.cancel.is_set():
                        return
                    buf = render.mix_audio(seq, self.project, pool, s, min(step, s1 - s))
                    w.writeframes(render.to_int16(buf))

            # 2) 映像をパイプで ffmpeg へ
            W, H = seq.width & ~1, seq.height & ~1
            vcodec = ["-c:v", self.encoder]
            if self.encoder in ("libx264", "libx265"):
                vcodec += ["-crf", str(self.quality), "-preset", "medium", "-pix_fmt", "yuv420p"]
            elif self.encoder == "h264_nvenc":
                vcodec += ["-cq", str(self.quality), "-preset", "p5", "-pix_fmt", "yuv420p"]
            elif self.encoder == "prores_ks":
                vcodec += ["-profile:v", "3", "-pix_fmt", "yuv422p10le"]
            acodec = ["-c:a", "pcm_s16le"] if self.path.lower().endswith(".mov") else \
                ["-c:a", "aac", "-b:a", "192k"]
            fps = f"{seq.fps:.5f}".rstrip("0").rstrip(".")
            if abs(seq.fps - 29.97) < 0.01:
                fps = "30000/1001"
            elif abs(seq.fps - 59.94) < 0.01:
                fps = "60000/1001"
            elif abs(seq.fps - 23.976) < 0.01:
                fps = "24000/1001"
            cmd = [ffmpeg_exe(), "-v", "error", "-y",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", fps, "-i", "-",
                   "-i", tmpwav] + vcodec + acodec + ["-shortest", "-movflags", "+faststart", self.path]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=_NO_WINDOW)
            try:
                for i, f in enumerate(range(self.a, self.b)):
                    if self.cancel.is_set():
                        proc.kill()
                        return
                    im = render.render_frame(seq, f, self.project, pool, 1.0)
                    if im.size != (W, H):
                        im = im.resize((W, H))
                    proc.stdin.write(im.tobytes())
                    if i % 5 == 0:
                        self.progress.emit(i, n)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            err = proc.stderr.read().decode("utf-8", "replace")
            proc.wait()
            if proc.returncode != 0:
                self.failed.emit(err[-1500:] or f"ffmpeg エラー ({proc.returncode})")
                return
            self.progress.emit(n, n)
            self.finished_ok.emit(self.path)
        except Exception as ex:
            self.failed.emit(str(ex))
        finally:
            pool.close()
            if tmpwav and os.path.exists(tmpwav):
                try:
                    os.remove(tmpwav)
                except OSError:
                    pass


class SmartExportWorker(QThread):
    """差分書き出し（前回と同じ区間を再利用）。並列 / レンダーファーム対応。"""
    progress_msg = pyqtSignal(str, int, int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, project: Project, a: int, b: int, path: str, codec: str, crf: int,
                 processes: int = 1, server: str = ""):
        super().__init__()
        self.project = copy.deepcopy(project)
        self.a, self.b, self.path, self.codec, self.crf = a, b, path, codec, crf
        self.processes, self.server = processes, server
        self.cancel = threading.Event()
        self.stats = {}

    def run(self):
        from . import smartrender as SR
        try:
            farm = None
            if self.server:
                from .rendercluster import FarmClient
                farm = FarmClient(self.server)
            self.stats = SR.smart_export(self.project, self.a, self.b, self.path, self.codec, self.crf,
                                         progress=lambda m, i, n: self.progress_msg.emit(m, i, n),
                                         cancel=self.cancel, processes=self.processes, farm=farm)
            self.finished_ok.emit(self.path)
        except InterruptedError:
            self.failed.emit("中止しました")
        except Exception as ex:
            import traceback
            self.failed.emit(f"{ex}\n\n{traceback.format_exc()[-1500:]}")


class ExportDialog(QDialog):
    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("メディアを書き出し")
        self.setMinimumWidth(520)
        seq = mw.project.sequence
        lay = QVBoxLayout(self)
        f = QFormLayout()
        row = QHBoxLayout()
        self.path = QLineEdit(os.path.join(os.path.expanduser("~"), "Desktop", f"{seq.name}.mp4"))
        row.addWidget(self.path, 1)
        b = QPushButton("参照...")
        b.clicked.connect(self._browse)
        row.addWidget(b)
        f.addRow("出力ファイル", row)
        self.enc = QComboBox()
        for label, codec, ext in ENCODERS:
            if codec == "h264_nvenc" and not ffmpeg_exe():
                continue      # NVENC は外部 ffmpeg がある場合のみ
            self.enc.addItem(label, (codec, ext))
        self.enc.currentIndexChanged.connect(self._on_enc)
        f.addRow("形式", self.enc)
        self.q = QSpinBox()
        self.q.setRange(0, 51)
        self.q.setValue(18)
        self.q.setToolTip("小さいほど高画質（CRF / CQ）")
        f.addRow("画質 (CRF)", self.q)
        self.range = QComboBox()
        self.range.addItems(["シーケンス全体", "IN 点から OUT 点"])
        if seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            self.range.setCurrentIndex(1)
        f.addRow("範囲", self.range)
        f.addRow("解像度 / fps", QLabel(f"{seq.width}×{seq.height}  {seq.fps}fps（シーケンス設定）"))
        from .settings import SETTINGS
        self.mode = QComboBox()
        self.mode.addItem("通常（1 本ずつ書き出し）", "normal")
        self.mode.addItem("スマート（変わった区間だけレンダリング・部分修正向け）", "smart")
        self.mode.addItem("スマート + 並列（この PC の CPU を全部使う）", "parallel")
        self.mode.addItem("スマート + レンダーファーム（アセットサーバー経由で複数 PC）", "farm")
        self.mode.setToolTip("スマート書き出しは 2 秒ごとの区間に分けてキャッシュし、テロップ修正などで\n"
                             "変わった区間だけをレンダリングし直して結合します（2 回目以降が速い）。\n"
                             "H.264 / ProRes のみ対応。")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mw.settings.value("export_mode", "smart"))))
        f.addRow("方式", self.mode)
        self.server = QLineEdit(SETTINGS.asset_server)
        self.server.setPlaceholderText("http://192.168.0.10:8770")
        f.addRow("サーバー", self.server)
        self.mode.currentIndexChanged.connect(self._on_mode)
        lay.addLayout(f)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        lay.addWidget(self.bar)
        self.msg = QLabel("")
        self.msg.setObjectName("info")
        lay.addWidget(self.msg)
        self.btns = QDialogButtonBox()
        self.go = self.btns.addButton("書き出し", QDialogButtonBox.ButtonRole.AcceptRole)
        self.cancel_btn = self.btns.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        self.go.clicked.connect(self._start)
        self.cancel_btn.clicked.connect(self._cancel)
        lay.addWidget(self.btns)
        self.worker = None
        self._on_mode()

    def _on_mode(self):
        m = self.mode.currentData()
        self.server.setEnabled(m == "farm")
        codec = self.enc.currentData()[0]
        if m != "normal" and codec not in ("libx264", "prores_ks"):
            self.msg.setText("スマート書き出しは H.264 (libx264) と ProRes のみ対応です（通常で書き出します）")
        else:
            self.msg.setText("")

    def _on_enc(self, i):
        codec, ext = self.enc.itemData(i)
        base, _ = os.path.splitext(self.path.text())
        self.path.setText(base + ext)
        self.q.setEnabled(codec != "prores_ks")
        if hasattr(self, "mode"):
            self._on_mode()

    def _browse(self):
        ext = self.enc.currentData()[1]
        p, _ = QFileDialog.getSaveFileName(self, "書き出し先", self.path.text(), f"*{ext}")
        if p:
            self.path.setText(p)

    def _start(self):
        seq = self.mw.project.sequence
        if self.range.currentIndex() == 1 and seq.mark_in >= 0 and seq.mark_out > seq.mark_in:
            a, b = seq.mark_in, seq.mark_out
        else:
            a, b = 0, seq.duration
        if b <= a:
            QMessageBox.warning(self, "書き出し", "書き出す範囲が空です。")
            return
        mode = self.mode.currentData()
        codec = self.enc.currentData()[0]
        self.mw.settings.setValue("export_mode", mode)
        if mode != "normal" and codec in ("libx264", "prores_ks"):
            server = ""
            if mode == "farm":
                from .rendercluster import server_ok, online_nodes
                from .settings import SETTINGS
                server = self.server.text().strip().rstrip("/")
                if server and not server.startswith("http"):
                    server = "http://" + server
                if not server or not server_ok(server):
                    QMessageBox.warning(self, "レンダーファーム", f"アセットサーバー {server or '（未設定）'} に接続できません。")
                    return
                if server != SETTINGS.asset_server:
                    SETTINGS.asset_server = server
                    SETTINGS.save()
                self.msg.setText(f"レンダーノード {len(online_nodes(server))} 台 + この PC で書き出します")
            procs = max(1, (os.cpu_count() or 2) - 1) if mode == "parallel" else 1
            self.worker = SmartExportWorker(self.mw.project, a, b, self.path.text(), codec, self.q.value(),
                                            procs, server)
            self.worker.progress_msg.connect(self._on_smart_progress)
        else:
            self.worker = ExportWorker(self.mw.project, a, b, self.path.text(), codec, self.q.value())
            self.worker.progress.connect(self._on_progress)
        self.worker.finished_ok.connect(self._done)
        self.worker.failed.connect(self._fail)
        self.bar.setVisible(True)
        self.bar.setRange(0, b - a)
        self.go.setEnabled(False)
        self.cancel_btn.setText("中止")
        self.msg.setText("書き出し中...")
        self.worker.start()

    def _on_progress(self, i, n):
        self.bar.setValue(i)
        self.msg.setText(f"書き出し中... {i}/{n} フレーム")

    def _on_smart_progress(self, m, i, n):
        self.bar.setRange(0, max(1, n))
        self.bar.setValue(i)
        self.msg.setText(f"{m}... {i}/{n}")

    def _done(self, path):
        st = getattr(self.worker, "stats", None)
        if st:
            self.msg.setText(f"完了: {path}\n区間 {st['segments']} 個（再利用 {st['reused']}・新規 {st['rendered']}）")
        else:
            self.msg.setText(f"完了: {path}")
        self.go.setEnabled(True)
        self.cancel_btn.setText("閉じる")
        self.worker = None

    def _fail(self, err):
        self.msg.setText("失敗しました")
        self.go.setEnabled(True)
        self.cancel_btn.setText("閉じる")
        self.worker = None
        QMessageBox.critical(self, "書き出しエラー", err)

    def _cancel(self):
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(3000)
            self.worker = None
            self.msg.setText("中止しました")
            self.go.setEnabled(True)
            self.cancel_btn.setText("閉じる")
            return
        self.reject()

    def closeEvent(self, e):
        if self.worker is not None:
            self.worker.cancel.set()
            self.worker.wait(3000)
        super().closeEvent(e)
