"""セリフパネル（ゆっくりムービーメーカー風）: 1 行打って Enter で 音声合成 → 配置 → 字幕 → 口パクする立ち絵。

- 話者ごとに 音声合成ソフト・声・速さ・字幕の色・立ち絵 を設定（プロジェクトの speakers に保存、台本ダイアログと共通）
- Enter で再生ヘッドの位置に置き、再生ヘッドをセリフの後ろ（+ 間）へ進める → 続けて次のセリフを打てる
- 音声は話者ごとの音声トラック、字幕は「セリフ」トラック、立ち絵は話者ごとのトラックに置き、立ち絵は自動で伸びる
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit, QPushButton,
                             QLabel, QDoubleSpinBox, QSpinBox, QCheckBox, QPlainTextEdit)

from . import model as M
from .settings import SETTINGS


class _SynthJob(QThread):
    done = pyqtSignal(str)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, text, cfg, out):
        super().__init__()
        self.text, self.cfg, self.out = text, cfg, out

    def run(self):
        from . import voiceengine as VE
        from .yukkuri import synthesize
        try:
            key = self.cfg.get("engine")
            if key in VE.ENGINE_BY_KEY and not VE.is_running(key):
                VE.launch(key, with_app=SETTINGS.voice_launch_app)
                if not VE.wait_ready(key, tick=self.progress.emit):
                    raise RuntimeError(f"{VE.ENGINE_BY_KEY[key]['name']} を起動できませんでした")
            if not os.path.exists(self.out):
                synthesize(self.text, key or "sapi", self.cfg.get("voice", ""), float(self.cfg.get("speed", 1.0)), self.out)
            self.done.emit(self.out)
        except Exception as ex:
            self.failed.emit(str(ex))


class VoicePanel(QWidget):
    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.job: Optional[_SynthJob] = None
        from . import voiceengine as VE
        self._engines = VE.installed()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        f = QFormLayout()
        self.speaker = QComboBox()
        self.speaker.setEditable(True)
        self.speaker.setToolTip("話者の名前（新しい名前を入れると話者が増えます）")
        self.speaker.currentTextChanged.connect(self._load_speaker)
        self.engine = QComboBox()
        for e in self._engines:
            self.engine.addItem(e["name"], e["key"])
        self.engine.addItem("Windows 標準音声", "sapi")
        self.engine.currentIndexChanged.connect(lambda _i: self._fill_voices())
        self.voice = QComboBox()
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.5, 2.0)
        self.speed.setSingleStep(0.1)
        self.speed.setValue(1.0)
        self.char = QComboBox()
        launch = QPushButton("起動")
        launch.setToolTip("音声合成ソフトを起動して声の一覧を読み込む")
        launch.clicked.connect(self._launch)
        er = QHBoxLayout()
        er.addWidget(self.engine, 1)
        er.addWidget(launch)
        f.addRow("話者", self.speaker)
        f.addRow("音声合成", er)
        f.addRow("声", self.voice)
        sr = QHBoxLayout()
        sr.addWidget(self.speed)
        sr.addWidget(QLabel("立ち絵"))
        sr.addWidget(self.char, 1)
        f.addRow("速さ", sr)
        lay.addLayout(f)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("セリフを入力して Enter（Shift+Enter で改行）。「霊夢：こんにちは」のように話者も指定できます")
        self.text.setFixedHeight(70)
        self.text.installEventFilter(self)
        lay.addWidget(self.text)
        row = QHBoxLayout()
        self.pause = QDoubleSpinBox()
        self.pause.setRange(0, 5)
        self.pause.setValue(0.3)
        self.pause.setSuffix(" 秒")
        self.maxc = QSpinBox()
        self.maxc.setRange(6, 40)
        self.maxc.setValue(18)
        self.subs = QCheckBox("字幕")
        self.subs.setChecked(True)
        row.addWidget(QLabel("間"))
        row.addWidget(self.pause)
        row.addWidget(QLabel("字幕の文字数"))
        row.addWidget(self.maxc)
        row.addWidget(self.subs)
        row.addStretch(1)
        self.go = QPushButton("合成して配置")
        self.go.clicked.connect(self.submit)
        row.addWidget(self.go)
        lay.addLayout(row)
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.msg.setObjectName("info")
        lay.addWidget(self.msg)
        lay.addStretch(1)
        self.refresh()

    # ── 話者 ──
    def refresh(self):
        cur = self.speaker.currentText()
        self.speaker.blockSignals(True)
        self.speaker.clear()
        names = [s["name"] for s in self.mw.project.speakers] or ["霊夢", "魔理沙"]
        self.speaker.addItems(names)
        self.speaker.blockSignals(False)
        self.char.clear()
        self.char.addItem("（なし）", "")
        for c in self.mw.project.characters:
            self.char.addItem(c["name"], c["id"])
        self.speaker.setCurrentText(cur or names[0])
        self._load_speaker(self.speaker.currentText())

    def _cfg(self, name: str) -> dict:
        return next((s for s in self.mw.project.speakers if s["name"] == name), {})

    def _load_speaker(self, name: str):
        cfg = self._cfg(name)
        i = self.engine.findData(cfg.get("engine", self.engine.itemData(0)))
        self.engine.setCurrentIndex(max(0, i))
        self._fill_voices(cfg.get("voice"))
        self.speed.setValue(float(cfg.get("speed", 1.0)))
        i = self.char.findData(cfg.get("char_id", ""))
        if i < 0:
            i = self.char.findText(name)
        self.char.setCurrentIndex(max(0, i))

    def _fill_voices(self, want=None):
        from . import voiceengine as VE
        from .yukkuri import sapi_voices
        want = self.voice.currentData() if want is None else want
        self.voice.clear()
        key = self.engine.currentData()
        if key == "sapi":
            for n in sapi_voices() or ["Microsoft Haruka Desktop"]:
                self.voice.addItem(n, n)
        else:
            vs = VE.speakers(key) if VE.is_running(key) else []
            for n, sid in vs:
                self.voice.addItem(n, sid)
            if not vs:
                self.voice.addItem("（「起動」で声を読み込み。既定の声で合成）", 3 if key == "voicevox" else 0)
        if want is not None and self.voice.findData(want) >= 0:
            self.voice.setCurrentIndex(self.voice.findData(want))

    def _launch(self):
        from .yukkuri import _EngineLauncher
        key = self.engine.currentData()
        if key == "sapi":
            return
        self.msg.setText("起動中…")
        self._launcher = _EngineLauncher([key], SETTINGS.voice_launch_app)
        self._launcher.progress.connect(self.msg.setText)
        self._launcher.done.connect(lambda failed: (self._fill_voices(), self.msg.setText(
            "起動できませんでした" if failed else "準備ができました。セリフを入力して Enter")))
        self._launcher.start()

    def _save_speaker(self, name: str) -> dict:
        sp = self.mw.project.speakers
        old = self._cfg(name)
        cfg = {**old, "name": name, "engine": self.engine.currentData(), "voice": self.voice.currentData(),
               "voice_name": self.voice.currentText(), "speed": self.speed.value(),
               "char_id": self.char.currentData() or "",
               "color": old.get("color") or ["#FF5A5A", "#5AA0FF", "#FFD24A", "#7BE07B"][len(sp) % 4]}
        self.mw.project.speakers = [s for s in sp if s["name"] != name] + [cfg]
        return cfg

    # ── 入力 ──
    def eventFilter(self, obj, e):
        if obj is self.text and e.type() == e.Type.KeyPress and e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                and not e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submit()
            return True
        return super().eventFilter(obj, e)

    def submit(self):
        if self.job and self.job.isRunning():
            return
        raw = self.text.toPlainText().strip()
        if not raw:
            return
        from .yukkuri import parse_script, voice_dir
        lines = parse_script(raw, self.speaker.currentText())
        name, body = lines[0][0], " ".join(t for _, t in lines)
        if name != self.speaker.currentText():
            if self.speaker.findText(name) < 0:
                self.speaker.addItem(name)
            self.speaker.setCurrentText(name)
        cfg = self._save_speaker(name)
        key = hashlib.md5(f"{cfg['engine']}|{cfg['voice']}|{cfg['speed']}|{body}".encode()).hexdigest()[:12]
        out = str(voice_dir(self.mw.project_path) /
                  f"{re.sub(r'[^0-9A-Za-zぁ-んァ-ン一-龥]', '', name)[:8]}_{key}.wav")
        self._pending = (name, body, cfg)
        self.go.setEnabled(False)
        self.msg.setText("合成中…")
        self.job = _SynthJob(body, cfg, out)
        self.job.progress.connect(self.msg.setText)
        self.job.done.connect(self._place)
        self.job.failed.connect(lambda e: (self.msg.setText("失敗: " + e), self.go.setEnabled(True)))
        self.job.start()

    def _place(self, wav: str):
        name, body, cfg = self._pending
        self.go.setEnabled(True)
        n = place_line(self.mw, name, body, wav, cfg, self.mw.record_player.position, self.pause.value(),
                       self.maxc.value() if self.subs.isChecked() else 0)
        self.text.clear()
        self.msg.setText(f"{name}: {body[:20]}（{n / self.mw.project.sequence.fps:.1f} 秒）")


def _speaker_track(seq: M.Sequence, project: M.Project, name: str) -> int:
    """話者ごとの音声トラック（話者の並び順。足りなければ追加）。"""
    names = [s["name"] for s in project.speakers]
    i = names.index(name) if name in names else 0
    while len(seq.audio_tracks) <= i:
        seq.audio_tracks.append(M.Track("audio", f"A{len(seq.audio_tracks) + 1}"))
    return i


def place_line(mw, name: str, text: str, wav: str, cfg: dict, frame: int, pause: float, max_chars: int) -> int:
    """合成した 1 行を置く。戻り値: セリフの長さ（フレーム）。"""
    from . import media as MD
    from .yukkuri import subtitle_chunks
    seq = mw.project.sequence
    fps = seq.fps
    item = MD.probe(wav)
    MD.build_audio_cache(item.path)
    MD.build_peaks(item.path)
    mw.begin_edit()
    mw.project.media.append(item)
    mw.project.bin_by_id(mw.named_bin("セリフ")).add("media", item.id)
    dur = max(1, int(round(item.duration_s * fps)))
    ai = _speaker_track(seq, mw.project, name)
    at = seq.audio_tracks[ai]
    M.clear_range(at, frame, frame + dur)
    at.clips.append(M.Clip(M.new_id(), "audio", frame, dur, 0, item.id, f"{name}: {text[:12]}",
                           props={"line_text": text, "speaker": name}))
    at.sort()
    # 字幕（「セリフ」トラック。無ければ一番上に作る）
    if max_chars > 0:
        st = next((t for t in seq.video_tracks if t.name.endswith("セリフ")), None)
        if st is None:
            st = M.Track("video", f"V{len(seq.video_tracks) + 1} セリフ", selected=False)
            seq.video_tracks.append(st)
        M.clear_range(st, frame, frame + dur)
        chunks = subtitle_chunks(text, max_chars)
        total = sum(len(c) for c in chunks) or 1
        pos = frame
        for k, ch in enumerate(chunks):
            d = frame + dur - pos if k == len(chunks) - 1 else max(1, int(dur * len(ch) / total))
            st.clips.append(M.Clip(M.new_id(), "text", pos, d, name="セリフ",
                                   props={"text": ch, "style": {"font_color": "#FFFFFF",
                                                                "stroke_color": cfg.get("color", "#000000"),
                                                                "stroke_width": 6, "font_size": 64}}))
            pos += d
        st.sort()
    # 立ち絵（話者ごとのトラック。既にあればセリフの終わりまで伸ばす）
    cid = cfg.get("char_id")
    if cid:
        hit = next(((t, c) for t in seq.video_tracks for c in t.clips
                    if c.kind == "character" and c.prop("char_id") == cid), None)
        if hit is None:
            t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
            st_idx = next((i for i, x in enumerate(seq.video_tracks) if x.name.endswith("セリフ")), len(seq.video_tracks))
            seq.video_tracks.insert(st_idx, t)       # 字幕の下
            left = ai % 2 == 1
            t.clips.append(M.Clip(M.new_id(), "character", frame, dur, name=name,
                                  props={**M.default_props("character"), "char_id": cid, "audio_track": ai,
                                         "scale": 0.55, "pos_x": -640 if left else 640, "pos_y": 160, "flip": left}))
        else:
            t, c = hit
            end = max(c.end, frame + dur)
            start = min(c.start, frame)
            c.start, c.duration = start, end - start
            c.props["audio_track"] = ai
    for i, t in enumerate(seq.video_tracks):
        t.name = f"V{i + 1}" + (" セリフ" if t.name.endswith("セリフ") else "")
    mw.end_edit(f"セリフ: {name}")
    mw.seek_record(frame + dur + int(round(pause * fps)))
    return dur
