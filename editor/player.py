"""再生エンジン: 描画スレッド（先読み）＋ 音声出力 ＋ クロック。"""
from __future__ import annotations

import copy
import threading
import time
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, QElapsedTimer
from PyQt6.QtGui import QImage

import numpy as np

from .media import DecoderPool, SAMPLE_RATE, CHANNELS
from .model import Project, Sequence
from .settings import SETTINGS
from . import render
from .rendercache import CACHE


def array_to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


def pil_to_qimage(im) -> QImage:
    im = im.convert("RGB")
    data = im.tobytes("raw", "RGB")
    q = QImage(data, im.width, im.height, im.width * 3, QImage.Format.Format_RGB888)
    return q.copy()


class RenderThread(QThread):
    """合成フレームを作るワーカー。静止画要求（シーク）と再生用の先読みを担当。"""

    frame_ready = pyqtSignal(int, int, QImage)   # generation, frame, image

    def __init__(self):
        super().__init__()
        self._cv = threading.Condition()
        self._mode = "idle"
        self._still = None
        self._cursor = 0
        self._rate = 1
        self._gen = 0
        self._seq: Optional[Sequence] = None
        self._proj: Optional[Project] = None
        self._end = 0
        self.scale = 0.5
        self.clock = 0              # メインスレッドが更新する現在フレーム
        self._quit = False
        self.pool = DecoderPool()

    def set_snapshot(self, seq: Sequence, proj: Project, end: int):
        with self._cv:
            self._seq, self._proj, self._end = seq, proj, end
            self._gen += 1

    def request_still(self, frame: int):
        with self._cv:
            self._mode = "still"
            self._still = frame
            self._gen += 1
            self._cv.notify()

    def start_play(self, frame: int, rate: int):
        with self._cv:
            self._mode = "play"
            self._cursor = frame
            self._rate = rate
            self._gen += 1
            self._cv.notify()

    def stop_play(self):
        with self._cv:
            if self._mode == "play":
                self._mode = "idle"
            self._gen += 1

    def generation(self) -> int:
        return self._gen

    def quit_thread(self):
        with self._cv:
            self._quit = True
            self._cv.notify()
        self.wait(2000)
        self.pool.close()

    def run(self):
        while True:
            with self._cv:
                while not self._quit and (self._mode == "idle" or self._seq is None):
                    self._cv.wait(0.2)
                if self._quit:
                    return
                mode, gen = self._mode, self._gen
                seq, proj, scale = self._seq, self._proj, self.scale
                if mode == "still":
                    frame = self._still
                    self._mode = "idle"
                else:
                    # 再生: クロックより遅れていたら飛ばす / 先行しすぎなら待つ
                    clk = self.clock
                    if self._rate > 0:
                        if self._cursor < clk:
                            self._cursor = clk
                        if self._cursor > clk + 8 * self._rate:
                            self._cv.wait(0.004)
                            continue
                    else:
                        if self._cursor > clk:
                            self._cursor = clk
                        if self._cursor < clk - 8 * abs(self._rate):
                            self._cv.wait(0.004)
                            continue
                    frame = self._cursor
                    self._cursor += self._rate
                    if frame < 0 or frame > self._end:
                        self._cv.wait(0.01)
                        continue
            try:
                key = render.frame_signature(seq, frame, proj, scale, self.pool.use_proxy)
                arr = CACHE.get(key)
                if arr is None:
                    arr = render.render_frame_np(seq, frame, proj, self.pool, scale)
                    CACHE.put_ram(key, arr)
                q = array_to_qimage(arr)
            except Exception:
                continue
            self.frame_ready.emit(gen, frame, q)


class AudioOut:
    """QAudioSink（プッシュ方式）。使えない環境では None 扱い。"""

    def __init__(self):
        self.sink = None
        self.dev = None
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
            fmt = QAudioFormat()
            fmt.setSampleRate(SAMPLE_RATE)
            fmt.setChannelCount(CHANNELS)
            fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
            out = QMediaDevices.defaultAudioOutput()
            if out.isNull():
                return
            self._cls = (QAudioSink, out, fmt)
        except Exception:
            self._cls = None

    def available(self) -> bool:
        return getattr(self, "_cls", None) is not None

    def start(self):
        if not self.available():
            return False
        QAudioSink, out, fmt = self._cls
        self.sink = QAudioSink(out, fmt)
        self.sink.setBufferSize(int(SAMPLE_RATE * CHANNELS * 2 * 0.25))
        self.dev = self.sink.start()
        return self.dev is not None

    def stop(self):
        if self.sink is not None:
            try:
                self.sink.stop()
            except Exception:
                pass
        self.sink = None
        self.dev = None

    def processed_sec(self) -> float:
        return self.sink.processedUSecs() / 1e6 if self.sink is not None else 0.0

    def bytes_free(self) -> int:
        return self.sink.bytesFree() if self.sink is not None else 0

    def write(self, data: bytes):
        if self.dev is not None:
            self.dev.write(data)


class Player(QObject):
    """モニター1面分のプレイヤー。sequence_provider() が再生対象を返す。"""

    position_changed = pyqtSignal(int)
    image_changed = pyqtSignal(QImage)
    state_changed = pyqtSignal(bool)
    fps_changed = pyqtSignal(float, float)      # 実測 fps, 目標 fps
    levels_changed = pyqtSignal(float, float)   # 音声レベル L / R（dBFS）

    def __init__(self, project_provider: Callable[[], Project],
                 sequence_provider: Callable[[], Optional[Sequence]], parent=None):
        super().__init__(parent)
        self._proj_provider = project_provider
        self._seq_provider = sequence_provider
        self.position = 0
        self.rate = 0                 # 0=停止, 1=再生, 2,4.. 早送り, 負=逆再生
        self.loop = False
        self.allow_beyond = False     # 末尾より先にもシーク可（レコード側）
        self._worker = RenderThread()
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.start()
        self._timer = QTimer(self)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._audio = AudioOut()
        self._audio_pool = DecoderPool()
        self._clock = QElapsedTimer()
        self._start_frame = 0
        self._sample_cursor = 0
        self._snapshot: Optional[Sequence] = None
        self._proj_snap: Optional[Project] = None
        self._pending: dict = {}
        self._shown = -1
        self.end_frame = 0
        self._fps_count = 0
        self._fps_t = QElapsedTimer()

    # ── 外部API ─────────────────────────────
    @property
    def playing(self) -> bool:
        return self.rate != 0

    def set_scale(self, s: float):
        self._worker.scale = s

    def set_sequence_provider(self, fn):
        """表示するシーケンスを差し替え（トリムモードでソース側にシーケンスを出す等）。"""
        self._seq_provider = fn
        self.refresh()

    def set_use_proxy(self, on: bool):
        self._worker.pool.use_proxy = on
        self._audio_pool.use_proxy = on
        self.refresh()

    def refresh(self):
        """モデルが変わったら呼ぶ（スナップショットを取り直して再描画）。"""
        self._take_snapshot()
        if self.playing:
            self._restart_play()
        else:
            self._worker.request_still(self.position)

    def seek(self, frame: int):
        hi = 10 ** 9 if self.allow_beyond else max(0, self.end_frame)
        frame = max(0, min(int(frame), hi))
        if self.playing:
            self.stop()
        self.position = frame
        if self._snapshot is None:
            self._take_snapshot()
        self._worker.request_still(frame)
        self.position_changed.emit(frame)

    def step(self, n: int):
        self.seek(self.position + n)

    def play(self, rate: int = 1):
        self._take_snapshot()
        if self.end_frame <= 0:
            return
        if rate > 0 and self.position >= self.end_frame:
            self.position = 0
        self.rate = rate
        self._restart_play()
        self.state_changed.emit(True)

    def stop(self):
        if not self.playing:
            return
        self.rate = 0
        self._timer.stop()
        self._audio.stop()
        self._worker.stop_play()
        self._pending.clear()
        self.state_changed.emit(False)
        self.fps_changed.emit(-1.0, 0.0)
        self.levels_changed.emit(-90.0, -90.0)
        self._worker.request_still(self.position)

    def toggle(self):
        if self.playing:
            self.stop()
        else:
            self.play(1)

    def shuttle(self, key: str):
        """J/K/L シャトル。"""
        if key == "K":
            self.stop()
            return
        if key == "L":
            r = 1 if self.rate <= 0 else min(8, self.rate * 2)
        else:
            r = -1 if self.rate >= 0 else max(-8, self.rate * 2)
        if self.playing:
            self.stop()
        self.play(r)

    def shutdown(self):
        self._timer.stop()
        self._audio.stop()
        self._worker.quit_thread()

    # ── 内部 ────────────────────────────────
    def _take_snapshot(self):
        seq = self._seq_provider()
        proj = self._proj_provider()
        if seq is None:
            self._snapshot = None
            self.end_frame = 0
            return
        self._snapshot = copy.deepcopy(seq)
        self._proj_snap = copy.copy(proj)
        self._proj_snap.media = [copy.copy(m) for m in proj.media]
        self._proj_snap.design = dict(proj.design or {})
        self._proj_snap.sequences = [self._snapshot]
        self._proj_snap.current = 0
        self.end_frame = max(1, self._snapshot.duration)
        self._worker.set_snapshot(self._snapshot, self._proj_snap, self.end_frame)

    def _restart_play(self):
        self._pending.clear()
        self._start_frame = self.position
        self._clock.start()
        self._audio.stop()
        self._use_audio = False
        if self.rate == 1 and self._audio.start():
            self._use_audio = True
            self._sample_cursor = int(round(self.position * SAMPLE_RATE / self._snapshot.fps))
            self._audio_t0 = None
            self._feed_audio()
        self._worker.clock = self.position
        self._worker.start_play(self.position, self.rate)
        self._fps_count = 0
        self._fps_t.start()
        self._timer.start()

    def _feed_audio(self):
        free = self._audio.bytes_free()
        chunk = 1024 * CHANNELS * 2
        while free >= chunk:
            n = free // (CHANNELS * 2)
            n = min(n, SAMPLE_RATE // 10)
            buf = render.mix_audio(self._snapshot, self._proj_snap, self._audio_pool,
                                   self._sample_cursor, n)
            self._audio.write(render.to_int16(buf))
            l, r = render.levels_db(buf)
            self.levels_changed.emit(l, r)
            self._sample_cursor += n
            free = self._audio.bytes_free()

    def _clock_frame(self) -> int:
        fps = self._snapshot.fps if self._snapshot else 30.0
        if self._use_audio:
            sec = self._audio.processed_sec()
        else:
            sec = self._clock.elapsed() / 1000.0
        return self._start_frame + int(sec * fps * self.rate)

    def _tick(self):
        if not self.playing or self._snapshot is None:
            return
        if self._use_audio:
            self._feed_audio()
        f = self._clock_frame()
        if (self.rate > 0 and f >= self.end_frame) or (self.rate < 0 and f <= 0):
            f = max(0, min(f, self.end_frame))
            self.position = f
            self.position_changed.emit(f)
            if self.loop and self.rate > 0:
                self.position = 0
                self._restart_play()
                return
            self.stop()
            return
        self._worker.clock = f
        if self._fps_t.isValid() and self._fps_t.elapsed() >= 1000:
            el = self._fps_t.elapsed() / 1000.0
            self.fps_changed.emit(self._fps_count / el, abs(self.rate) * (self._snapshot.fps if self._snapshot else 30))
            self._fps_count = 0
            self._fps_t.restart()
        if f != self.position:
            self.position = f
            self.position_changed.emit(f)
        # 表示: f 以下で最新のフレーム
        if self._pending:
            if self.rate > 0:
                ready = [k for k in self._pending if k <= f]
                best = max(ready) if ready else None
            else:
                ready = [k for k in self._pending if k >= f]
                best = min(ready) if ready else None
            if best is not None and best != self._shown:
                self._shown = best
                self._fps_count += 1
                self.image_changed.emit(self._pending[best])
                for k in list(self._pending):
                    if (self.rate > 0 and k <= best) or (self.rate < 0 and k >= best):
                        del self._pending[k]

    def _on_frame(self, gen: int, frame: int, img: QImage):
        if gen != self._worker.generation():
            return
        if self.playing:
            self._pending[frame] = img
            if len(self._pending) > 24:
                for k in sorted(self._pending)[:-24]:
                    del self._pending[k]
        else:
            self._shown = frame
            self.image_changed.emit(img)
