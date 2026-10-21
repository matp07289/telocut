"""YouTube 向け機能: サムネイル / 書き出しプリセット / ショート変換 / 概要欄 / スライドショー /
BGM ループ / ラウドネス（-14 LUFS 目安）/ 文字起こし字幕 / SE パッド / テロップの定番スタイル。"""
from __future__ import annotations

import copy
import json
import math
import os
import zlib
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from PIL import Image
from PyQt6.QtCore import Qt, QSize, QMimeData, QByteArray
from PyQt6.QtGui import QColor, QImage, QPixmap, QDrag
from PyQt6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QPushButton, QLabel,
                             QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QFileDialog,
                             QMessageBox, QColorDialog, QWidget, QGridLayout, QToolButton, QScrollArea,
                             QDialogButtonBox, QSlider)

from core.timecode import frames_to_timecode
from . import model as M
from . import media as MD
from . import render as R
from .settings import SETTINGS

# 書き出しプリセット（名前, 幅, 高さ, fps, エンコーダ, CRF）
EXPORT_PRESETS = [
    ("YouTube 1080p（H.264 高画質）", 1920, 1080, None, "libx264", 18),
    ("YouTube 4K（H.264）", 3840, 2160, None, "libx264", 18),
    ("YouTube ショート（縦 1080×1920）", 1080, 1920, None, "libx264", 18),
    ("YouTube 720p（軽量）", 1280, 720, None, "libx264", 21),
    ("アーカイブ用 ProRes 422 HQ", 0, 0, None, "prores_ks", 0),
]

# テロップの定番スタイル（DesignSettings の上書き）
TELOP_STYLES = [
    ("YouTube 黄色・極太縁", {"font_color": "#FFE500", "stroke_color": "#000000", "stroke_width": 10, "font_size": 84, "bold": True}),
    ("白文字・黒縁（基本字幕）", {"font_color": "#FFFFFF", "stroke_color": "#000000", "stroke_width": 6, "font_size": 64}),
    ("赤・白縁（強調）", {"font_color": "#FF2D2D", "stroke_color": "#FFFFFF", "stroke_width": 10, "font_size": 90, "bold": True}),
    ("ゆっくり霊夢（赤縁）", {"font_color": "#FFFFFF", "stroke_color": "#D22B2B", "stroke_width": 8, "font_size": 64}),
    ("ゆっくり魔理沙（黄縁）", {"font_color": "#FFFFFF", "stroke_color": "#C9A200", "stroke_width": 8, "font_size": 64}),
    ("ニュース帯（白文字・紺帯）", {"font_color": "#FFFFFF", "stroke_width": 0, "bg_enabled": True, "bg_color": "#0B2A6B",
                              "bg_opacity": 230, "bg_padding": 18, "font_size": 56}),
    ("上部見出し（白・黒帯）", {"font_color": "#FFFFFF", "stroke_width": 0, "bg_enabled": True, "bg_color": "#000000",
                           "bg_opacity": 180, "position_v": "top", "offset_y": 40, "font_size": 52}),
    ("ツッコミ（青・白縁・右上）", {"font_color": "#2D7CFF", "stroke_color": "#FFFFFF", "stroke_width": 8,
                            "position_v": "top", "position_h": "right", "offset_y": 120, "offset_x": -80, "font_size": 70}),
]

TEXT_ANIMS = [("なし", ""), ("ポップ（ぽよん）", "pop"), ("スライドアップ", "slide"), ("タイプライター", "type"),
              ("フェードイン", "fade"), ("ズーム（大きく→等倍）", "zoom"), ("上から落ちる", "drop"),
              ("ワイプ（左から現れる）", "wipe"), ("ぼかし→くっきり", "blur")]
TEXT_OUT_ANIMS = [("なし", ""), ("フェードアウト", "fade"), ("ズームして消える", "zoom"), ("下へ落ちる", "drop"),
                  ("上へスライド", "slide"), ("ワイプ（右へ消える）", "wipe"), ("ぼかして消える", "blur")]


# ─────────────────────────────────────────────
#  サムネイル
# ─────────────────────────────────────────────

class ThumbnailDialog(QDialog):
    """現在のフレーム + 大きな文字 + 立ち絵 でサムネイル（1280×720）を作る。"""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("サムネイル作成")
        self.resize(980, 620)
        h = QHBoxLayout(self)
        self.preview = QLabel()
        self.preview.setMinimumSize(640, 360)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(self.preview, 3)
        f = QFormLayout()
        self.text = QPlainTextEdit("ここに\nタイトル")
        self.text.setFixedHeight(80)
        self.text.textChanged.connect(self.update_preview)
        f.addRow("文字", self.text)
        self.size = QSpinBox()
        self.size.setRange(40, 400)
        self.size.setValue(150)
        self.size.valueChanged.connect(self.update_preview)
        f.addRow("文字サイズ", self.size)
        self.color = QPushButton()
        self.color.setProperty("c", "#FFE500")
        self.color.setStyleSheet("background: #FFE500;")
        self.color.clicked.connect(lambda: self._pick(self.color))
        self.stroke = QPushButton()
        self.stroke.setProperty("c", "#000000")
        self.stroke.setStyleSheet("background: #000000;")
        self.stroke.clicked.connect(lambda: self._pick(self.stroke))
        row = QHBoxLayout()
        row.addWidget(QLabel("文字"))
        row.addWidget(self.color)
        row.addWidget(QLabel("縁"))
        row.addWidget(self.stroke)
        f.addRow("色", row)
        self.pos = QComboBox()
        self.pos.addItems(["左下", "中央", "左上", "右下"])
        self.pos.currentIndexChanged.connect(self.update_preview)
        f.addRow("位置", self.pos)
        self.dark = QSlider(Qt.Orientation.Horizontal)
        self.dark.setRange(0, 80)
        self.dark.setValue(25)
        self.dark.valueChanged.connect(self.update_preview)
        f.addRow("背景を暗く", self.dark)
        self.char = QComboBox()
        self.char.addItem("（なし）", "")
        for c in mw.project.characters:
            self.char.addItem(c["name"], c["id"])
        self.char.currentIndexChanged.connect(self.update_preview)
        f.addRow("立ち絵", self.char)
        b = QPushButton("PNG で保存...")
        b.clicked.connect(self._save)
        f.addRow(b)
        w = QWidget()
        w.setLayout(f)
        h.addWidget(w, 2)
        pool = MD.DecoderPool()
        seq = mw.project.sequence
        self.bg = R.render_frame(seq, mw.record_player.position, mw.project, pool, 1.0).resize((1280, 720))
        pool.close()
        self.update_preview()

    def _pick(self, btn):
        c = QColorDialog.getColor(QColor(btn.property("c")), self)
        if c.isValid():
            btn.setProperty("c", c.name())
            btn.setStyleSheet(f"background: {c.name()};")
            self.update_preview()

    def compose(self) -> Image.Image:
        from core.models import DesignSettings
        from core.renderer import render_telop_image
        im = self.bg.convert("RGBA")
        k = self.dark.value() / 100
        if k:
            im = Image.blend(im, Image.new("RGBA", im.size, (0, 0, 0, 255)), k)
        cid = self.char.currentData()
        ch = self.mw.project.character(cid) if cid else None
        if ch and ch["images"].get("closed"):
            c = Image.open(ch["images"]["closed"]).convert("RGBA")
            c.thumbnail((520, 700))
            im.alpha_composite(c, (1280 - c.width - 20, 720 - c.height))
        d = DesignSettings()
        d.canvas_width, d.canvas_height = 1280, 720
        d.font_size = self.size.value()
        d.font_color = self.color.property("c")
        d.stroke_color = self.stroke.property("c")
        d.stroke_width = max(4, self.size.value() // 12)
        d.bold = True
        d.line_spacing = 1.1
        p = self.pos.currentIndex()
        d.position_v = {0: "bottom", 1: "middle", 2: "top", 3: "bottom"}[p]
        d.position_h = {0: "left", 1: "center", 2: "left", 3: "right"}[p]
        d.text_align = d.position_h
        d.offset_x = 40 if d.position_h == "left" else (-40 if d.position_h == "right" else 0)
        d.offset_y = 40
        try:
            t = render_telop_image(self.text.toPlainText(), d)
            im.alpha_composite(t)
        except Exception:
            pass
        return im.convert("RGB")

    def update_preview(self):
        im = self.compose().resize((640, 360))
        data = im.tobytes("raw", "RGB")
        self.preview.setPixmap(QPixmap.fromImage(QImage(data, 640, 360, 640 * 3, QImage.Format.Format_RGB888).copy()))

    def _save(self):
        seq = self.mw.project.sequence
        p, _ = QFileDialog.getSaveFileName(self, "サムネイルを保存", f"{seq.name}_thumbnail.png", "PNG (*.png);;JPEG (*.jpg)")
        if p:
            self.compose().save(p, quality=95)
            self.mw.status.showMessage(f"サムネイルを保存しました: {p}", 4000)


# ─────────────────────────────────────────────
#  ショート（縦型）変換
# ─────────────────────────────────────────────

def make_shorts_sequence(project: M.Project, src: M.Sequence, blur_bg: bool = True) -> M.Sequence:
    """横長シーケンスを縦型 1080×1920 に変換したコピーを作る。
    blur_bg: 背景に同じ映像を拡大＋ぼかしで敷き、手前に横幅いっぱいの映像を置く。"""
    d = copy.deepcopy(src)
    d.id = M.new_id()
    d.name = src.name + "（ショート）"
    d.width, d.height = 1080, 1920
    new_v = []
    for t in d.video_tracks:
        if blur_bg and any(c.kind == "video" for c in t.clips) and not new_v:
            bg = copy.deepcopy(t)
            bg.name = "BG"
            for c in bg.clips:
                c.id = M.new_id()
                c.link = ""
                if c.kind in ("video", "image"):
                    c.props["scale"] = 3.2
                    c.props["effects"] = list(c.props.get("effects", [])) + [{"name": "blur", "value": 2.0},
                                                                             {"name": "brightness", "value": 0.7}]
            new_v.append(bg)
        for c in t.clips:
            if c.kind == "text":
                st = dict(c.props.get("style", {}))
                st["font_size"] = int(st.get("font_size", 60) * 1.15)
                c.props["style"] = st
        new_v.append(t)
    d.video_tracks = new_v
    for i, t in enumerate(d.video_tracks):
        t.name = f"V{i + 1}"
    project.sequences.append(d)
    return d


# ─────────────────────────────────────────────
#  概要欄テキスト
# ─────────────────────────────────────────────

def description_text(project: M.Project, seq: M.Sequence, title: str = "") -> str:
    def fmt(f):
        s = int(f / seq.fps)
        h, m, sec = s // 3600, (s // 60) % 60, s % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
    lines = [title or seq.name, ""]
    # ⚠ で始まるマーカー（チェック・AI のカット候補など）はチャプターにしない
    marks = sorted((m for m in seq.markers if not m.name.startswith("⚠")), key=lambda m: m.frame)
    if marks:
        lines.append("■ チャプター")
        if marks[0].frame > 0:
            lines.append("00:00 オープニング")
        for i, mk in enumerate(marks):
            lines.append(f"{fmt(mk.frame)} {mk.name or f'チャプター {i + 1}'}")
        lines.append("")
    credits = []
    for s in project.speakers:
        from .voiceengine import credit_for     # 例「VOICEVOX:ずんだもん」（利用規約のクレジット表記）
        c = credit_for(s.get("engine", ""), str(s.get("voice_name") or s.get("name")))
        if c:
            credits.append(c)
    used = {c.media_id for _, c in seq.all_clips() if c.media_id}
    for m in project.media:
        if m.id in used and getattr(m, "credit", ""):
            credits.append(m.credit)
    bgm = [project.media_by_id(c.media_id).name for t in seq.audio_tracks if getattr(t, "duck", False)
           for c in t.clips if project.media_by_id(c.media_id)]
    if credits or bgm:
        lines.append("■ クレジット")
        lines += list(dict.fromkeys(credits))
        for b in dict.fromkeys(bgm):
            lines.append(f"BGM: {Path(b).stem}")
        lines.append("")
    lines.append("#TeloCut")
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  ラウドネス（-14 LUFS 目安）
# ─────────────────────────────────────────────

def measure_loudness(seq: M.Sequence, project: M.Project, progress=None) -> Optional[float]:
    """シーケンス全体の統合ラウドネス（LUFS の近似: 400ms ブロック・ゲート付き RMS）。"""
    pool = MD.DecoderPool()
    sr = MD.SAMPLE_RATE
    total = int(seq.duration / seq.fps * sr)
    if total <= 0:
        return None
    blk = int(sr * 0.4)
    hop = int(sr * 0.1)
    ms_list = []
    pos = 0
    chunk = sr * 10
    while pos < total:
        n = min(chunk + blk, total - pos)
        buf = R.mix_audio(seq, project, pool, pos, n)
        sq = (buf.astype(np.float64) ** 2).sum(axis=1)
        cs = np.concatenate([[0], np.cumsum(sq)])
        for s in range(0, max(1, min(chunk, n - blk)), hop):
            if s + blk <= n:
                ms_list.append((cs[s + blk] - cs[s]) / blk / 2)
        pos += chunk
        if progress:
            progress(pos, total)
    ms = np.array(ms_list)
    if ms.size == 0:
        return None
    lk = -0.691 + 10 * np.log10(np.maximum(ms * 2, 1e-12))
    ms = ms[lk > -70]
    if ms.size == 0:
        return None
    rel = -0.691 + 10 * np.log10(ms.mean() * 2) - 10
    lk = -0.691 + 10 * np.log10(np.maximum(ms * 2, 1e-12))
    ms = ms[lk > rel]
    return float(-0.691 + 10 * np.log10(ms.mean() * 2)) if ms.size else None


# ─────────────────────────────────────────────
#  SE パッド（効果音ボタン）
# ─────────────────────────────────────────────

class SEPadPanel(QWidget):
    """効果音フォルダのファイルをボタンで並べ、クリックで再生ヘッドに配置（KLA Audio Auto 風）。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        self.folder = QLabel(self._folder() or "（効果音フォルダ未設定）")
        self.folder.setObjectName("info")
        row.addWidget(self.folder, 1)
        b = QToolButton()
        b.setText("フォルダ...")
        b.clicked.connect(self._pick)
        row.addWidget(b)
        self.track = QComboBox()
        self.track.setToolTip("配置する音声トラック")
        row.addWidget(self.track)
        v.addLayout(row)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.inner = QWidget()
        self.grid = QGridLayout(self.inner)
        self.grid.setSpacing(3)
        self.area.setWidget(self.inner)
        v.addWidget(self.area, 1)
        hint = QLabel("クリック: 再生ヘッドに配置 / 右クリック: 試聴はソースモニターで")
        hint.setObjectName("info")
        v.addWidget(hint)
        self.reload()

    @staticmethod
    def _folder() -> str:
        return getattr(SETTINGS, "se_dir", "") or ""

    def _pick(self):
        d = QFileDialog.getExistingDirectory(self, "効果音フォルダ", self._folder())
        if d:
            SETTINGS.se_dir = d
            SETTINGS.save()
            self.folder.setText(d)
            self.reload()

    def refresh_tracks(self):
        cur = self.track.currentIndex()
        self.track.clear()
        for t in self.mw.project.sequence.audio_tracks:
            self.track.addItem(t.name)
        self.track.setCurrentIndex(cur if 0 <= cur < self.track.count() else max(0, self.track.count() - 1))

    def reload(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        d = self._folder()
        files = []
        if d and os.path.isdir(d):
            for root, _, fs in os.walk(d):
                for f in sorted(fs):
                    if Path(f).suffix.lower() in MD.AUDIO_EXT:
                        files.append(os.path.join(root, f))
        cols = ["#3f6fb5", "#2f8f8f", "#8a5fb0", "#b5873f", "#a54a6a", "#5f8f3f"]
        for i, p in enumerate(files[:200]):
            b = QPushButton(Path(p).stem[:14])
            b.setToolTip(p)
            b.setMinimumHeight(40)
            col = cols[zlib.crc32(Path(p).parent.name.encode("utf-8")) % len(cols)]     # フォルダごとに毎回同じ色
            b.setStyleSheet(f"background: {col}; color: white;")
            b.clicked.connect(lambda _=False, p=p: self.mw.place_se(p, self.track.currentIndex()))
            self.grid.addWidget(b, i // 4, i % 4)
        self.refresh_tracks()


# ─────────────────────────────────────────────
#  スライドショー / BGM ループ
# ─────────────────────────────────────────────

def slideshow_placements(seq: M.Sequence, items: List[M.MediaItem], start: int, sec: float,
                         dissolve: float, kenburns: bool) -> List[M.Clip]:
    out = []
    f = start
    n = int(round(sec * seq.fps))
    for i, m in enumerate(items):
        props = {}
        if kenburns:
            props["anim"] = {"scale": [1.0, 1.12] if i % 2 == 0 else [1.12, 1.0]}
        if dissolve > 0 and i > 0:
            props["dissolve"] = int(round(dissolve * seq.fps))
        out.append(M.Clip(M.new_id(), "image" if m.kind == "image" else "video", f, n, 0, m.id, m.name, props=props))
        f += n
    return out


def bgm_loop_clips(clip: M.Clip, media: M.MediaItem, until: int, fps: float, xfade: int = 15) -> List[M.Clip]:
    """BGM クリップを until フレームまで繰り返す（つなぎ目はフェード）。"""
    out = []
    length = clip.duration
    f = clip.end
    while f < until:
        d = min(length, until - f)
        c = clip.copy(id=M.new_id(), start=f, duration=d)
        c.props["fade_in"] = min(xfade, d // 2)
        c.props["fade_out"] = min(xfade, d // 2) if f + d < until else int(fps)
        out.append(c)
        f += d
    return out
