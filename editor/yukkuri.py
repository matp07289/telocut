"""ゆっくり / 解説動画向け: キャラクター（立ち絵）・音声合成・台本からの一括生成。

- キャラクター: PNG（口閉じ・口開き・半開き・目閉じ）または PSD（口・目レイヤーを自動判定）から登録
- 音声: VOICEVOX / AivisSpeech / SHAREVOX / NEMO / COEIROINK（起動していなければ自動で起動）/ Windows 標準音声（SAPI）
- 口パク: VOICEVOX 系で合成した音声は母音（あいうえおん）に合わせて口を動かす（voiceengine.py）
- 台本: 「名前：セリフ」の行を読み、話者ごとに 音声クリップ・字幕・口パクする立ち絵 を一括配置
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton,
                             QLabel, QPlainTextEdit, QTableWidget, QTableWidgetItem, QComboBox,
                             QDoubleSpinBox, QFileDialog, QMessageBox, QHeaderView, QCheckBox,
                             QProgressBar, QSpinBox, QColorDialog, QAbstractItemView, QDialogButtonBox)

from .model import new_id
from .settings import SETTINGS

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def char_dir() -> Path:
    return Path(SETTINGS.cache_dir).parent / "telocut_characters"


def voice_dir(project_path: Optional[str]) -> Path:
    if project_path:
        return Path(project_path).parent / "Voice"
    return Path(SETTINGS.cache_dir).parent / "telocut_voice"


# ─────────────────────────────────────────────
#  キャラクター
# ─────────────────────────────────────────────

VOWEL_KEYS = [("a", "あ"), ("i", "い"), ("u", "う"), ("e", "え"), ("o", "お"), ("N", "ん")]


def create_character_from_pngs(name: str, closed: str, open_: str, half: str = "",
                               blink: str = "", vowels: Optional[Dict[str, str]] = None) -> dict:
    """vowels: {"a": あの口の画像, "i": ..., ...}（任意。あると母音に合わせて口パク）"""
    cid = new_id()
    d = char_dir() / cid
    d.mkdir(parents=True, exist_ok=True)
    imgs = {}
    srcs = [("closed", closed), ("open", open_), ("half", half), ("blink", blink)]
    srcs += [(k, v) for k, v in (vowels or {}).items() if v]
    for key, src in srcs:
        if src:
            out = d / f"{key}.png"
            Image.open(src).convert("RGBA").save(out)
            imgs[key] = str(out)
    return {"id": cid, "name": name, "images": imgs, "source": closed}


def create_character_from_psd(name: str, psd_path: str) -> dict:
    """PSD の「口」「目」レイヤーを判定して、口閉じ / 半開き / 開き / 目閉じ の4枚を合成して保存。"""
    from core.lipsync import extract_psd_layers, classify_layer_role, classify_eye_role
    layers = extract_psd_layers(psd_path)
    if not layers:
        raise ValueError("PSD からレイヤーを取り出せませんでした（psd-tools が必要です）")
    base, mouths, eyes_open, eyes_closed = [], {}, [], []
    for lname, img in layers.items():
        role = classify_layer_role(lname)
        eye = classify_eye_role(lname)
        if role is not None:
            mouths.setdefault(role, img)
        elif eye == "eye_closed":
            eyes_closed.append(img)
        elif eye == "eye_open":
            eyes_open.append(img)
        else:
            base.append(img)
    if not mouths:
        raise ValueError("口のレイヤー（名前に「口」や mouth を含むレイヤー）が見つかりません")
    w = max(i.width for i in layers.values())
    h = max(i.height for i in layers.values())

    def comp(mouth_key, eyes):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for l in base + eyes:
            im.alpha_composite(l.convert("RGBA"), (0, 0))
        m = mouths.get(mouth_key) or mouths.get("closed") or next(iter(mouths.values()))
        im.alpha_composite(m.convert("RGBA"), (0, 0))
        return im
    cid = new_id()
    d = char_dir() / cid
    d.mkdir(parents=True, exist_ok=True)
    open_key = "a" if "a" in mouths else ("o" if "o" in mouths else next(iter(mouths)))
    half_key = "i" if "i" in mouths else ("u" if "u" in mouths else "")
    variants = [("closed", "closed", eyes_open), ("open", open_key, eyes_open)]
    if half_key:
        variants.append(("half", half_key, eyes_open))
    if eyes_closed:
        variants.append(("blink", "closed", eyes_closed))
    for vk, _lab in VOWEL_KEYS:              # あいうえおん の口（PSD にあれば）
        if vk in mouths:
            variants.append((vk, vk, eyes_open))
    rendered = {key: comp(mk, eyes) for key, mk, eyes in variants}
    bbox = rendered["closed"].getbbox() or (0, 0, w, h)
    imgs = {}
    for key, im in rendered.items():
        p = d / f"{key}.png"
        im.crop(bbox).save(p)          # 全画像を同じ範囲で切り抜き（位置ずれ防止）
        imgs[key] = str(p)
    return {"id": cid, "name": name, "images": imgs, "source": psd_path}


# ─────────────────────────────────────────────
#  音声合成
# ─────────────────────────────────────────────

def voicevox_speakers() -> List[Tuple[str, int]]:
    try:
        from core.voicevox import VoiceVoxClient
        c = VoiceVoxClient()
        if not c.is_available():
            return []
        return c.flat_styles()
    except Exception:
        return []


def sapi_voices() -> List[str]:
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
             ".GetInstalledVoices() | % { $_.VoiceInfo.Name }"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW)
        return [l.strip() for l in out.stdout.splitlines() if l.strip()]
    except Exception:
        return []


def synthesize(text: str, engine: str, voice, speed: float, out_path: str, pitch: float = 0.0) -> str:
    """テキストを WAV にする。engine: voicevox / aivis / sharevox / nemo / coeiroink / sapi。
    VOICEVOX 系は口パク用の母音タイムライン（<wav>.mouth.json）も保存する。"""
    from . import voiceengine as VE
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    if engine in VE.ENGINE_BY_KEY:
        return VE.synthesize(text, engine, int(voice), out_path, speed=speed, pitch=pitch)
    # Windows 標準音声（SAPI）
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        tpath = tf.name
    rate = max(-10, min(10, int(round((speed - 1.0) * 10))))
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"try {{ $s.SelectVoice('{voice}') }} catch {{}}; "
        f"$s.Rate = {rate}; "
        f"$s.SetOutputToWaveFile('{out_path}'); "
        f"$s.Speak([IO.File]::ReadAllText('{tpath}', [Text.Encoding]::UTF8)); $s.Dispose()")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                           timeout=120, creationflags=_NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(out_path):
            raise RuntimeError(r.stderr.strip()[-300:] or "Windows 音声の合成に失敗しました")
    finally:
        try:
            os.remove(tpath)
        except OSError:
            pass
    return out_path


# ─────────────────────────────────────────────
#  台本
# ─────────────────────────────────────────────

_LINE_RE = re.compile(r"^\s*([^:：「」\s]{1,16})\s*[:：]\s*(.+)$")


def parse_script(text: str, default_speaker: str = "ナレーション") -> List[Tuple[str, str]]:
    """「名前：セリフ」形式。名前の無い行は直前の話者（最初は default_speaker）。空行は飛ばす。"""
    out = []
    cur = default_speaker
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if m:
            cur, body = m.group(1), m.group(2).strip()
        else:
            body = line
        body = body.strip("「」")
        if body:
            out.append((cur, body))
    return out


def subtitle_chunks(text: str, max_chars: int = 18) -> List[str]:
    """長いセリフを読みやすい長さの字幕に分ける（句読点・助詞の後で区切る）。"""
    from core.textfmt import balanced_break
    t = text.strip()
    if len(t) <= max_chars:
        return [t]
    n = (len(t) + max_chars - 1) // max_chars
    parts = balanced_break(t, n).split("\n")
    return [p for p in parts if p.strip()]


class ScriptJob(QThread):
    """台本の各行を音声合成する（UI を止めない）。"""

    progress = pyqtSignal(int, int, str)
    line_done = pyqtSignal(int, str)        # 行番号, wav パス
    failed = pyqtSignal(int, str)

    def __init__(self, lines, speakers: Dict[str, dict], out_dir: str):
        super().__init__()
        self.lines = lines
        self.speakers = speakers
        self.out_dir = out_dir
        self.cancel = threading.Event()

    def run(self):
        from . import voiceengine as VE
        for key in dict.fromkeys(c.get("engine") for c in self.speakers.values()):
            if key in VE.ENGINE_BY_KEY and not VE.is_running(key):
                try:
                    VE.launch(key, with_app=SETTINGS.voice_launch_app)
                    if not VE.wait_ready(key, tick=lambda m: self.progress.emit(0, len(self.lines), m),
                                         cancel=self.cancel.is_set):
                        raise RuntimeError(f"{VE.ENGINE_BY_KEY[key]['name']} を起動できませんでした")
                except Exception as ex:
                    self.failed.emit(0, str(ex))
                    return
        for i, (spk, text) in enumerate(self.lines):
            if self.cancel.is_set():
                return
            cfg = self.speakers.get(spk, {})
            self.progress.emit(i, len(self.lines), f"{spk}: {text[:20]}")
            key = hashlib.md5(f"{cfg.get('engine')}|{cfg.get('voice')}|{cfg.get('speed')}|{text}".encode()).hexdigest()[:12]
            out = os.path.join(self.out_dir, f"{i + 1:03d}_{re.sub(r'[^0-9A-Za-zぁ-んァ-ン一-龥]', '', spk)[:8]}_{key}.wav")
            try:
                if not os.path.exists(out):
                    synthesize(text, cfg.get("engine", "sapi"), cfg.get("voice", ""), float(cfg.get("speed", 1.0)), out)
                self.line_done.emit(i, out)
            except Exception as ex:
                self.failed.emit(i, str(ex))
                return


# ─────────────────────────────────────────────
#  ダイアログ
# ─────────────────────────────────────────────

SPEAKER_COLORS = ["#FF5A5A", "#5AA0FF", "#FFD24A", "#7BE07B", "#D08BFF", "#FF9A4A"]


class _EngineLauncher(QThread):
    """音声合成ソフトを起動して準備完了まで待つ。"""
    progress = pyqtSignal(str)
    done = pyqtSignal(list)

    def __init__(self, keys, with_app: bool):
        super().__init__()
        self.keys, self.with_app = keys, with_app

    def run(self):
        from . import voiceengine as VE
        failed = []
        for k in self.keys:
            name = VE.ENGINE_BY_KEY[k]["name"]
            if not VE.launch(k, with_app=self.with_app):
                failed.append(f"{name}（見つかりません）")
                continue
            if not VE.wait_ready(k, tick=self.progress.emit):
                failed.append(name)
        self.done.emit(failed)


class CharacterDialog(QDialog):
    """立ち絵を登録（PNG のセット または PSD）。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("キャラクター（立ち絵）を登録")
        self.result_char = None
        f = QFormLayout(self)
        self.name = QLineEdit("キャラクター")
        f.addRow("名前", self.name)
        self.mode = QComboBox()
        self.mode.addItems(["PNG を指定（口閉じ・口開き 必須）", "PSD から自動作成"])
        f.addRow("作り方", self.mode)
        self.paths = {}
        rows = [("closed", "口閉じ（基本）"), ("open", "口開き"), ("half", "口半開き（任意）"), ("blink", "目閉じ（任意）")]
        rows += [(k, f"口「{lab}」（任意）") for k, lab in VOWEL_KEYS]
        rows += [("psd", "PSD ファイル")]
        for key, lab in rows:
            row = QHBoxLayout()
            ed = QLineEdit()
            b = QPushButton("...")
            b.setFixedWidth(30)
            filt = "PSD (*.psd)" if key == "psd" else "画像 (*.png *.webp)"
            b.clicked.connect(lambda _=False, e=ed, ft=filt: self._pick(e, ft))
            row.addWidget(ed, 1)
            row.addWidget(b)
            self.paths[key] = ed
            f.addRow(lab, row)
        note = QLabel("PSD は、レイヤー名に「口」と母音（あ・い 等）や「閉」、目は「目」「閉」が含まれていると自動で判定します。\n"
                      "「あいうえおん」の口があると、VOICEVOX 系の音声に合わせて母音どおりに口が動きます（無ければ開き・閉じで代用）。")
        note.setObjectName("info")
        note.setWordWrap(True)
        f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def _pick(self, ed, filt):
        p, _ = QFileDialog.getOpenFileName(self, "ファイルを選択", "", filt)
        if p:
            ed.setText(p)
            if not self.name.text() or self.name.text() == "キャラクター":
                self.name.setText(Path(p).stem.split("_")[0])

    def _ok(self):
        try:
            if self.mode.currentIndex() == 1:
                if not self.paths["psd"].text():
                    raise ValueError("PSD を指定してください")
                self.result_char = create_character_from_psd(self.name.text(), self.paths["psd"].text())
            else:
                if not self.paths["closed"].text() or not self.paths["open"].text():
                    raise ValueError("口閉じ と 口開き の画像を指定してください")
                self.result_char = create_character_from_pngs(
                    self.name.text(), self.paths["closed"].text(), self.paths["open"].text(),
                    self.paths["half"].text(), self.paths["blink"].text(),
                    {k: self.paths[k].text() for k, _ in VOWEL_KEYS})
        except Exception as ex:
            QMessageBox.warning(self, "キャラクター", str(ex))
            return
        self.accept()


class ScriptDialog(QDialog):
    """台本 → 音声・字幕・立ち絵 を一括生成（ゆっくり / 解説動画）。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("台本から一括生成（ゆっくり・解説動画）")
        self.resize(900, 640)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("台本（1行1セリフ。「霊夢：こんにちは」のように 話者：セリフ。# で始まる行はコメント）"))
        self.script = QPlainTextEdit()
        self.script.setPlainText("霊夢：ゆっくり霊夢です。\n魔理沙：ゆっくり魔理沙だぜ。\n霊夢：今日は TeloCut の使い方を紹介するわ。")
        self.script.textChanged.connect(self._sync_speakers)
        v.addWidget(self.script, 2)
        v.addWidget(QLabel("話者の設定"))
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["話者", "音声エンジン", "声", "速さ", "字幕の色", "キャラクター（立ち絵）"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        v.addWidget(self.table, 2)
        row = QHBoxLayout()
        b = QPushButton("＋ キャラクターを登録...")
        b.clicked.connect(self._add_char)
        row.addWidget(b)
        row.addStretch(1)
        row.addWidget(QLabel("セリフ間の間"))
        self.pause = QDoubleSpinBox()
        self.pause.setRange(0, 5)
        self.pause.setValue(0.3)
        self.pause.setSuffix(" 秒")
        row.addWidget(self.pause)
        row.addWidget(QLabel("字幕の最大文字数"))
        self.maxc = QSpinBox()
        self.maxc.setRange(6, 40)
        self.maxc.setValue(18)
        row.addWidget(self.maxc)
        self.place_chars = QCheckBox("立ち絵を配置")
        self.place_chars.setChecked(True)
        row.addWidget(self.place_chars)
        v.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        v.addWidget(self.bar)
        self.msg = QLabel("")
        self.msg.setObjectName("info")
        v.addWidget(self.msg)
        bb = QHBoxLayout()
        bb.addStretch(1)
        self.go = QPushButton("生成して配置（再生ヘッドから）")
        self.go.clicked.connect(self._start)
        bb.addWidget(self.go)
        c = QPushButton("閉じる")
        c.clicked.connect(self.reject)
        bb.addWidget(c)
        v.addLayout(bb)
        from . import voiceengine as VE
        self._engines = VE.installed()
        self._voices: Dict[str, List[Tuple[str, int]]] = {}
        self._sapi = sapi_voices()
        top = QHBoxLayout()
        self.engine_state = QLabel("")
        self.launch_btn = QPushButton("🎤 音声合成ソフトを起動して声を読み込む")
        self.launch_btn.clicked.connect(self._launch_engines)
        self.with_app = QCheckBox("アプリの画面も開く")
        self.with_app.setChecked(SETTINGS.voice_launch_app)
        top.addWidget(self.engine_state, 1)
        top.addWidget(self.with_app)
        top.addWidget(self.launch_btn)
        v.insertLayout(3, top)
        self._refresh_engine_state()
        self._sync_speakers()

    # 話者テーブル
    def _speakers_in_script(self):
        seen = []
        for spk, _ in parse_script(self.script.toPlainText()):
            if spk not in seen:
                seen.append(spk)
        return seen

    def _sync_speakers(self):
        names = self._speakers_in_script()
        existing = {self.table.item(r, 0).text(): r for r in range(self.table.rowCount()) if self.table.item(r, 0)}
        saved = {s["name"]: s for s in self.mw.project.speakers}
        for i, n in enumerate(names):
            if n in existing:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = QTableWidgetItem(n)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(r, 0, it)
            sv = saved.get(n, {})
            eng = QComboBox()
            for e in self._engines:
                eng.addItem(e["name"], e["key"])
            eng.addItem("Windows 標準音声", "sapi")
            if sv.get("engine"):
                idx = eng.findData(sv["engine"])
                if idx >= 0:
                    eng.setCurrentIndex(idx)
            voice = QComboBox()
            eng.currentIndexChanged.connect(lambda _i, e=eng, vc=voice: self._fill_voices(e, vc))
            self._fill_voices(eng, voice)
            if sv.get("voice") is not None:
                idx = voice.findData(sv["voice"])
                if idx >= 0:
                    voice.setCurrentIndex(idx)
            elif voice.count() > 1:
                voice.setCurrentIndex(r % voice.count())
            self.table.setCellWidget(r, 1, eng)
            self.table.setCellWidget(r, 2, voice)
            sp = QDoubleSpinBox()
            sp.setRange(0.5, 2.0)
            sp.setSingleStep(0.1)
            sp.setValue(float(sv.get("speed", 1.0)))
            self.table.setCellWidget(r, 3, sp)
            col = QPushButton()
            c = sv.get("color", SPEAKER_COLORS[r % len(SPEAKER_COLORS)])
            col.setStyleSheet(f"background: {c};")
            col.setProperty("color", c)
            col.clicked.connect(lambda _=False, btn=col: self._pick_color(btn))
            self.table.setCellWidget(r, 4, col)
            ch = QComboBox()
            ch.addItem("（なし）", "")
            for chd in self.mw.project.characters:
                ch.addItem(chd["name"], chd["id"])
            idx = ch.findData(sv.get("char_id", ""))
            if idx < 0:
                idx = ch.findText(n)
            ch.setCurrentIndex(max(0, idx))
            self.table.setCellWidget(r, 5, ch)

    def _voices_for(self, key: str):
        from . import voiceengine as VE
        if key not in self._voices and VE.is_running(key):
            self._voices[key] = VE.speakers(key)
        return self._voices.get(key, [])

    def _fill_voices(self, eng, voice):
        cur = voice.currentData()
        voice.clear()
        key = eng.currentData()
        if key != "sapi":
            vs = self._voices_for(key)
            for name, sid in vs:
                voice.addItem(name, sid)
            if not vs:
                voice.addItem("（起動すると声を選べます。既定の声で合成）", 3 if key == "voicevox" else 0)
            if cur is not None and voice.findData(cur) >= 0:
                voice.setCurrentIndex(voice.findData(cur))
        else:
            for n in self._sapi or ["Microsoft Haruka Desktop"]:
                voice.addItem(n, n)

    def _refresh_engine_state(self):
        from . import voiceengine as VE
        if not self._engines:
            self.engine_state.setText("音声合成ソフトが見つかりません（VOICEVOX などをインストールすると選べます。Windows 標準音声は使えます）")
            self.launch_btn.setEnabled(False)
            return
        st = [f"{e['name']}: {'起動中' if VE.is_running(e['key']) else '停止'}" for e in self._engines]
        self.engine_state.setText("　".join(st))

    def _launch_engines(self):
        """表で選ばれている音声合成ソフト（無ければ最初の 1 つ）を起動し、声の一覧を読み込む。"""
        from . import voiceengine as VE
        keys = [self.table.cellWidget(r, 1).currentData() for r in range(self.table.rowCount())]
        keys = [k for k in dict.fromkeys(keys) if k in VE.ENGINE_BY_KEY] or [self._engines[0]["key"]]
        SETTINGS.voice_launch_app = self.with_app.isChecked()
        SETTINGS.save()
        self.launch_btn.setEnabled(False)
        self.bar.setRange(0, 0)
        self.bar.setVisible(True)
        self._launcher = _EngineLauncher(keys, self.with_app.isChecked())
        self._launcher.progress.connect(self.msg.setText)
        self._launcher.done.connect(self._engines_ready)
        self._launcher.start()

    def _engines_ready(self, failed: list):
        self.bar.setVisible(False)
        self.launch_btn.setEnabled(True)
        self._voices.clear()
        for r in range(self.table.rowCount()):
            self._fill_voices(self.table.cellWidget(r, 1), self.table.cellWidget(r, 2))
        self._refresh_engine_state()
        self.msg.setText("起動できませんでした: " + "、".join(failed) if failed else "音声合成ソフトの準備ができました")

    def _pick_color(self, btn):
        c = QColorDialog.getColor(QColor(btn.property("color")), self)
        if c.isValid():
            btn.setProperty("color", c.name())
            btn.setStyleSheet(f"background: {c.name()};")

    def _add_char(self):
        dlg = CharacterDialog(self)
        if dlg.exec() and dlg.result_char:
            self.mw.begin_edit()
            self.mw.project.characters.append(dlg.result_char)
            self.mw.end_edit("キャラクターを登録")
            for r in range(self.table.rowCount()):
                cb = self.table.cellWidget(r, 5)
                cb.addItem(dlg.result_char["name"], dlg.result_char["id"])
                if self.table.item(r, 0).text() == dlg.result_char["name"]:
                    cb.setCurrentIndex(cb.count() - 1)

    def speaker_settings(self) -> Dict[str, dict]:
        out = {}
        for r in range(self.table.rowCount()):
            n = self.table.item(r, 0).text()
            out[n] = {"name": n, "engine": self.table.cellWidget(r, 1).currentData(),
                      "voice": self.table.cellWidget(r, 2).currentData(),
                      "voice_name": self.table.cellWidget(r, 2).currentText(),
                      "speed": self.table.cellWidget(r, 3).value(),
                      "color": self.table.cellWidget(r, 4).property("color"),
                      "char_id": self.table.cellWidget(r, 5).currentData(), "order": r}
        return out

    # 生成
    def _start(self):
        lines = parse_script(self.script.toPlainText())
        if not lines:
            return
        self.speakers = self.speaker_settings()
        self.lines = lines
        self.wavs = {}
        out_dir = str(voice_dir(self.mw.project_path))
        self.job = ScriptJob(lines, self.speakers, out_dir)
        self.job.progress.connect(lambda i, n, t: (self.bar.setValue(i), self.msg.setText(f"音声合成中 {i + 1}/{n}  {t}")))
        self.job.line_done.connect(lambda i, p: self.wavs.__setitem__(i, p))
        self.job.failed.connect(lambda i, e: QMessageBox.critical(self, "音声合成", f"{i + 1} 行目で失敗しました:\n{e}"))
        self.job.finished.connect(self._done)
        self.bar.setRange(0, len(lines))
        self.bar.setVisible(True)
        self.go.setEnabled(False)
        self.job.start()

    def _done(self):
        self.go.setEnabled(True)
        if len(self.wavs) != len(self.lines):
            self.msg.setText("中断しました")
            return
        self.msg.setText("配置中...")
        n = build_script_timeline(self.mw, self.lines, self.wavs, self.speakers, self.pause.value(),
                                  self.maxc.value(), self.place_chars.isChecked())
        self.msg.setText(f"{n} 行のセリフを配置しました")
        self.accept()


def build_script_timeline(mw, lines, wavs: Dict[int, str], speakers: Dict[str, dict], pause: float,
                          max_chars: int, place_chars: bool, start_frame: Optional[int] = None) -> int:
    """合成した WAV を読み込み、音声・字幕・立ち絵をタイムラインに並べる。"""
    from . import model as M
    from . import media as MD
    seq = mw.project.sequence
    fps = seq.fps
    f = mw.record_player.position if start_frame is None else start_frame
    t0 = f
    order = sorted(speakers.values(), key=lambda s: s["order"])
    a_index = {s["name"]: i for i, s in enumerate(order)}
    mw.begin_edit()
    # 話者設定をプロジェクトに保存（次回も同じ声・色）
    saved = {s["name"]: s for s in mw.project.speakers}
    for s in order:
        saved[s["name"]] = {k: v for k, v in s.items() if k != "order"}
    mw.project.speakers = list(saved.values())
    while len(seq.audio_tracks) < len(order):
        seq.audio_tracks.append(M.Track("audio", f"A{len(seq.audio_tracks) + 1}"))
    # 字幕トラック・立ち絵トラック（一番上に追加）
    sub_track = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
    seq.video_tracks.append(sub_track)
    b = mw.project.bin_by_id(mw.bin.current_bin_id()) or mw.project.bins[0]
    spans: Dict[str, List[int]] = {}
    for i, (spk, text) in enumerate(lines):
        item = MD.probe(wavs[i])
        MD.build_audio_cache(item.path)
        MD.build_peaks(item.path)
        mw.project.media.append(item)
        b.add("media", item.id)
        dur = max(1, int(round(item.duration_s * fps)))
        ai = a_index.get(spk, 0)
        clip = M.Clip(M.new_id(), "audio", f, dur, 0, item.id, f"{spk}: {text[:12]}",
                      props={"line_text": text, "speaker": spk})
        M.place_clips(seq, [(seq.audio_tracks[ai], clip)])
        # 字幕（長いセリフは分割し、文字数で時間を按分）
        chunks = subtitle_chunks(text, max_chars)
        total = sum(len(c) for c in chunks)
        pos = f
        col = speakers.get(spk, {}).get("color", "#FFFFFF")
        for k, ch in enumerate(chunks):
            d = dur - (pos - f) if k == len(chunks) - 1 else max(1, int(dur * len(ch) / total))
            tc = M.Clip(M.new_id(), "text", pos, d, name="セリフ",
                        props={"text": ch, "style": {"font_color": "#FFFFFF", "stroke_color": col,
                                                     "stroke_width": 6, "font_size": 64}})
            sub_track.clips.append(tc)
            pos += d
        spans.setdefault(spk, [f, f + dur])
        spans[spk][1] = f + dur
        f += dur + int(round(pause * fps))
    sub_track.sort()
    if place_chars:
        for s in order:
            cid = s.get("char_id")
            if not cid or s["name"] not in spans:
                continue
            t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
            seq.video_tracks.insert(len(seq.video_tracks) - 1, t)   # 字幕の下
            left = s["order"] % 2 == 1
            t.clips.append(M.Clip(M.new_id(), "character", t0, max(1, f - t0), name=s["name"],
                                  props={"char_id": cid, "audio_track": a_index[s["name"]],
                                         "scale": 0.55, "pos_x": -640 if left else 640, "pos_y": 160,
                                         "flip": left}))
        for i, t in enumerate(seq.video_tracks):
            t.name = f"V{i + 1}"
    mw.end_edit(f"台本から生成（{len(lines)} 行）")
    mw.canvas.zoom_fit()
    return len(lines)


def regenerate_line(mw, clip) -> bool:
    """選択した音声クリップのセリフ（props.line_text）を作り直す。長さが変わったら後ろを詰め/押し出す。"""
    from . import media as MD
    from . import model as M
    spk = clip.props.get("speaker", "")
    text = clip.props.get("line_text", "")
    cfg = next((s for s in mw.project.speakers if s["name"] == spk), None)
    if not cfg or not text:
        return False
    out = str(voice_dir(mw.project_path) / f"regen_{new_id()}.wav")
    synthesize(text, cfg.get("engine", "sapi"), cfg.get("voice", ""), float(cfg.get("speed", 1.0)), out)
    item = MD.probe(out)
    MD.build_audio_cache(item.path)
    MD.build_peaks(item.path)
    seq = mw.project.sequence
    mw.begin_edit()
    mw.project.media.append(item)
    (mw.project.bin_of(clip.media_id) or mw.project.bins[0]).add("media", item.id)
    track, cur = seq.find(clip.id)
    new_dur = max(1, int(round(item.duration_s * seq.fps)))
    delta = new_dur - cur.duration
    cur.media_id, cur.src_in = item.id, 0
    if delta:
        for t in seq.tracks():
            if t.sync_lock and not t.locked:
                M.shift_after(t, cur.end, delta, ignore={cur.id})
        cur.duration = new_dur
    mw.end_edit("セリフを作り直し")
    return True
