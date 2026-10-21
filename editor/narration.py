"""ナレーション録音（マイクの音を WAV に。録り終えたら再生ヘッドの位置に置く）。"""
from __future__ import annotations

import datetime
import os
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout

RATE = 48000


def write_wav(path: str, pcm: bytes, rate: int = RATE, channels: int = 1) -> str:
    with wave.open(path, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


class NarrationDialog(QDialog):
    """マイクを選んで 録音 → 止める → 置く。録音中は音の大きさのメーターと時間。"""

    def __init__(self, parent, folder: str):
        super().__init__(parent)
        self.setWindowTitle("ナレーション録音")
        self.folder = folder
        self.path: Optional[str] = None
        self.buf = bytearray()
        self.source = None
        self.io = None
        self.t0 = 0.0
        v = QVBoxLayout(self)
        self.dev = QComboBox()
        self.devices = []
        try:
            from PyQt6.QtMultimedia import QMediaDevices
            self.devices = list(QMediaDevices.audioInputs())
            default = QMediaDevices.defaultAudioInput()
            for d in self.devices:
                self.dev.addItem(d.description(), d)
            for i, d in enumerate(self.devices):
                if d.id() == default.id():
                    self.dev.setCurrentIndex(i)
        except Exception:
            pass
        v.addWidget(QLabel("マイク"))
        v.addWidget(self.dev)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        v.addWidget(self.meter)
        self.time = QLabel("0.0 秒")
        v.addWidget(self.time)
        h = QHBoxLayout()
        self.rec = QPushButton("● 録音")
        self.rec.clicked.connect(self.toggle)
        self.ok = QPushButton("再生ヘッドに置く")
        self.ok.setEnabled(False)
        self.ok.clicked.connect(self.accept)
        cancel = QPushButton("やめる")
        cancel.clicked.connect(self.reject)
        for b in (self.rec, self.ok, cancel):
            h.addWidget(b)
        v.addLayout(h)
        self.msg = QLabel("" if self.devices else "マイクが見つかりません（Windows の設定 → サウンド → 入力 を確認）")
        self.msg.setWordWrap(True)
        v.addWidget(self.msg)
        self.rec.setEnabled(bool(self.devices))
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self._tick)

    def toggle(self):
        if self.source is None:
            self.start()
        else:
            self.stop()

    def start(self):
        from PyQt6.QtMultimedia import QAudioFormat, QAudioSource
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self.buf = bytearray()
        self.source = QAudioSource(self.dev.currentData(), fmt, self)
        self.io = self.source.start()
        self.io.readyRead.connect(self._read)
        self.t0 = time.time()
        self.rec.setText("■ 止める")
        self.ok.setEnabled(False)
        self.timer.start()

    def _read(self):
        if self.io is not None:
            self.buf += bytes(self.io.readAll())

    def _tick(self):
        self.time.setText(f"{time.time() - self.t0:.1f} 秒")
        tail = np.frombuffer(bytes(self.buf[-RATE // 5 * 2:]), "<i2") if len(self.buf) > 4 else np.zeros(1)
        peak = float(np.abs(tail).max()) / 32768 if tail.size else 0.0
        self.meter.setValue(int(min(1.0, peak) * 100))

    def stop(self) -> Optional[str]:
        if self.source is None:
            return None
        self._read()
        self.source.stop()
        self.source = None
        self.io = None
        self.timer.stop()
        self.rec.setText("● 録音し直す")
        if not self.buf:
            self.msg.setText("音が録れませんでした")
            return None
        Path(self.folder).mkdir(parents=True, exist_ok=True)
        self.path = write_wav(os.path.join(self.folder, f"ナレーション_{datetime.datetime.now():%Y%m%d_%H%M%S}.wav"), bytes(self.buf))
        self.msg.setText(f"録りました（{len(self.buf) / 2 / RATE:.1f} 秒）: {self.path}")
        self.ok.setEnabled(True)
        return self.path

    def reject(self):
        if self.source is not None:
            self.source.stop()
        super().reject()
