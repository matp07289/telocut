"""テレビ・映画の編集のやり方を YouTube 向けにした機能（MainWindow のミックスイン）。

放送前チェック（YouTube の決まり）: 点滅・黒い画面・音割れ・長い無音・テロップの重なり・出しっぱなしのテロップ・チャプターの決まり・
    冒頭のつかみ・ショートの決まり・YouTube の推奨設定・NG ワード・クレジットの抜け・言いよどみ（えー・あの）・フレームレートの混在・まとめてチェック
番組の構成（テレビ）: 広告の区切りの候補・アイキャッチ・アバン（冒頭に見どころ）・エンドロール・PR 表記・L 字画面・章の区切りに暗転・
    ランキング見出し・カウントアップ数字・コメントを流す（ニコニコ風）・ビフォーアフター・終了画面・章の扉・オープニングタイトル・
    出演者のテロップ（一括）・テロップをセーフエリアに収める
映画の手法: 映画風にする・カットのテンポ分析・フレームの配色を取り出す・巻き戻し演出・タイムラプス（N 秒に）・全体を目標の長さに合わせる
音: ナレーション録音・カットの継ぎ目に効果音・ルームトーン（無音の隙間を環境音で埋める）・BGM の頭とおしりを自動でフェード
公開・仕上げ: サムネを小さく表示して確認・YouTube 用の字幕・盛り上がりからショートを作る・修正指示リスト（サムネ付き）・新しい版にする・
    テロップの見本帳・翻訳用の字幕（CSV の書き出し / 読み込み）
"""
from __future__ import annotations

import copy
import csv
import dataclasses
import datetime
import html
import math
import os
import re
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
                             QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMessageBox, QPlainTextEdit,
                             QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QDoubleSpinBox, QLineEdit)

from . import model as M
from .settings import SETTINGS

Issue = Tuple[int, str, str]          # フレーム, 種類, 内容
FILLERS = re.compile(r"(えー+|えっと|えーと|あのー*|あの、|うーん|んー|まあ、|なんか、|そのー)")
DEFAULT_NG = ["死ね", "殺す", "ぶっ殺", "クソ", "自殺", "ガイジ"]
WARN = "#E05050"


class CheckDialog(QDialog):
    """チェックの結果（ダブルクリックでその場所へ・マーカーにする）。"""

    def __init__(self, mw, title: str, items: List[Issue]):
        super().__init__(mw)
        self.mw, self.items = mw, items
        self.setWindowTitle(f"{title}（{len(items)} 件）")
        self.resize(760, 460)
        v = QVBoxLayout(self)
        self.table = QTableWidget(len(items), 3)
        self.table.setHorizontalHeaderLabels(["場所", "チェック", "内容"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for i, (f, kind, text) in enumerate(items):
            for c, val in enumerate((mw.tc_str(f) if f >= 0 else "全体", kind, text)):
                self.table.setItem(i, c, QTableWidgetItem(val))
        self.table.cellDoubleClicked.connect(lambda r, _c: items[r][0] >= 0 and mw.seek_record(items[r][0]))
        v.addWidget(QLabel("ダブルクリックでその場所へ。" if items else "問題は見つかりませんでした 👍"))
        v.addWidget(self.table, 1)
        h = QHBoxLayout()
        mk = QPushButton("マーカーにする")
        mk.clicked.connect(self.to_markers)
        h.addWidget(mk)
        h.addStretch(1)
        close = QPushButton("閉じる")
        close.clicked.connect(self.reject)
        h.addWidget(close)
        v.addLayout(h)

    def to_markers(self) -> int:
        return self.mw.issues_to_markers(self.items)


class Features12Mixin:
    def _feature12_commands(self) -> dict:
        c = self._checks_table()
        cmds = {f"chk_{k}": (lambda fn=fn, lab=lab: self.show_check(lab, fn())) for k, (lab, fn, _heavy) in c.items()}
        cmds.update({
            "chk_all": self.check_all_dialog, "ng_words": self.ng_words_dialog,
            "ad_breaks": self.ad_breaks, "eyecatch": self.eyecatch_dialog, "aban": self.aban_dialog,
            "end_roll": self.end_roll_dialog, "pr_label": self.pr_label, "l_frame": self.l_frame,
            "blackout_chapters": self.blackout_chapters, "ranking": self.ranking_dialog, "countup": self.countup_dialog,
            "nico_comments": self.nico_dialog, "before_after": self.before_after, "end_screen": self.end_screen,
            "chapter_cards": self.chapter_cards, "opening_title": self.opening_dialog, "cast_supers": self.cast_dialog,
            "telops_safe": self.telops_to_safe, "cinema_look": self.cinema_look, "tempo": self.tempo_dialog,
            "palette": self.palette_dialog, "rewind": self.rewind, "timelapse": self.timelapse_dialog,
            "fit_target": self.fit_target_dialog, "narration": self.narration_dialog, "se_on_cuts": self.se_on_cuts_dialog,
            "room_tone": self.room_tone, "bgm_fades": self.bgm_fades, "thumb_preview": self.thumb_preview_dialog,
            "yt_subs": self.yt_subs_dialog, "shorts_hype": self.shorts_from_hype, "correction_list": self.correction_list_dialog,
            "new_version": self.new_version, "style_guide": self.style_guide_dialog,
            "trans_export": self.translation_export_dialog, "trans_import": self.translation_import_dialog,
        })
        return cmds

    def _build_feature12_menus(self):
        def sub(title, name, items):
            m = self._menu_named(title)
            if m is None:
                return
            m.addSeparator()
            s = m.addMenu(name)
            for c in items:
                s.addSeparator() if c is None else self._a(s, c)
        sub("整理・チェック", "放送前チェック（YouTube の決まり）",
            ["chk_all", None] + [f"chk_{k}" for k in self._checks_table()] + [None, "ng_words"])
        sub("YouTube", "番組の構成（テレビの手法）",
            ("ad_breaks", "eyecatch", "aban", "chapter_cards", "blackout_chapters", None, "opening_title", "end_roll",
             "end_screen", "pr_label", None, "l_frame", "ranking", "countup", "nico_comments", "before_after", None,
             "cast_supers", "telops_safe"))
        sub("クリップ", "映画の手法（ルック・テンポ・時間）",
            ("cinema_look", "palette", "tempo", None, "rewind", "timelapse", "fit_target"))
        sub("オーディオ", "ナレーション・効果音・環境音", ("narration", "se_on_cuts", "room_tone", "bgm_fades"))
        sub("YouTube", "公開・仕上げ", ("thumb_preview", "yt_subs", "shorts_hype", None, "correction_list", "new_version",
                                    "style_guide", None, "trans_export", "trans_import"))

    # ─────────────────────────── 共通 ───────────────────────────
    def show_check(self, title: str, items: List[Issue]) -> CheckDialog:
        dlg = CheckDialog(self, title, items)
        self._last_check = dlg
        dlg.show()
        return dlg

    def issues_to_markers(self, items: List[Issue]) -> int:
        seq = self.project.sequence
        self.begin_edit()
        n = 0
        for f, kind, text in items:
            if f < 0 or any(m.frame == f and m.name.startswith("⚠") for m in seq.markers):
                continue
            seq.markers.append(M.Marker(f, f"⚠ {kind}", WARN, text))
            n += 1
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit(f"チェックの結果をマーカーに（{n} 個）")
        return n

    def _texts(self) -> List[M.Clip]:
        return sorted((c for _, c in self.project.sequence.all_clips() if c.kind == "text" and c.enabled), key=lambda c: c.start)

    def _v1_cuts(self) -> List[int]:
        seq = self.project.sequence
        if not seq.video_tracks:
            return []
        return sorted({c.start for c in seq.video_tracks[0].clips if c.start > 0})

    def _fps(self) -> float:
        return self.project.sequence.fps

    def _work_dir12(self, sub: str) -> Path:
        d = (Path(self.project_path).parent / sub) if self.project_path else Path(SETTINGS.cache_dir) / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _top12(self, name: str) -> M.Track:
        t = M.Track("video", name, selected=False)
        self.project.sequence.video_tracks.append(t)
        return t

    def _ripple_insert(self, at: int, length: int):
        """すべてのトラックに隙間を作る（後ろとマーカーをずらす）。"""
        seq = self.project.sequence
        for t in seq.tracks():
            if not t.locked:
                M.insert_gap(t, at, length)
        for m in seq.markers:
            if m.frame >= at:
                m.frame += length

    # ─────────────────────────── 放送前チェック ───────────────────────────
    def _checks_table(self) -> Dict[str, Tuple[str, object, bool]]:
        return {"flash": ("点滅（光の点滅）", self.check_flash, True), "black": ("黒い画面", self.check_black, True),
                "clip": ("音割れ", self.check_clipping, True), "silence": ("長い無音", self.check_silence, True),
                "overlap": ("テロップの重なり", self.check_telop_overlap, False),
                "long": ("出しっぱなしのテロップ", self.check_long_telops, False),
                "chapters": ("チャプターの決まり", self.check_chapters, False), "hook": ("冒頭のつかみ", self.check_hook, False),
                "shorts": ("ショートの決まり", self.check_shorts, False), "settings": ("YouTube の推奨設定", self.check_yt_settings, False),
                "ng": ("NG ワード", self.check_ng_words, False), "credits": ("クレジットの抜け", self.check_credits, False),
                "fillers": ("言いよどみ（えー・あの）", self.find_fillers, False), "fps": ("フレームレートの混在", self.check_fps_mix, False)}

    def luma_series(self, step: int = 0) -> Tuple[List[int], np.ndarray]:
        """画面の明るさ（0〜255）を step フレームごとに（小さく描いて速く）。"""
        from .media import DecoderPool
        from .render import render_frame_np
        seq = self.project.sequence
        n = max(1, seq.duration)
        step = step or max(1, n // 6000)
        pool = DecoderPool()
        frames = list(range(0, n, step))
        out = []
        for f in frames:
            a = render_frame_np(seq, f, self.project, pool, 64 / max(1, seq.width)).astype(np.float32)
            out.append(float((a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114).mean()))
        return frames, np.array(out)

    def check_flash(self, series=None) -> List[Issue]:
        """1 秒に 3 回より多い強い点滅（テレビの「光の点滅」の目安: 明るさが大きく上下する）。"""
        frames, lum = series or self.luma_series(1)
        fps = self._fps()
        if len(lum) < 3:
            return []
        step = frames[1] - frames[0] if len(frames) > 1 else 1
        d = np.diff(lum)
        big = [i for i in range(len(d)) if abs(d[i]) >= 40]
        flips = [i for j, i in enumerate(big) if j > 0 and np.sign(d[i]) != np.sign(d[big[j - 1]])]
        out, last = [], -10 ** 9
        win = max(1, int(round(fps / step)))
        for j, i in enumerate(flips):
            k = sum(1 for x in flips[j:] if x - i < win)
            if k >= 6 and frames[i] - last > fps * 2:                 # 1 秒に 3 回の明暗（6 回の切り替わり）より多い
                out.append((frames[i], "点滅", f"1 秒に {k // 2} 回以上の強い点滅（見ている人の体調に注意・テレビでは避ける）"))
                last = frames[i]
        return out

    def check_black(self, series=None, min_s: float = 0.5) -> List[Issue]:
        frames, lum = series or self.luma_series()
        fps = self._fps()
        out, start = [], None
        for f, v in zip(frames + [10 ** 9], list(lum) + [255.0]):
            if v < 16 and start is None:
                start = f
            elif v >= 16 and start is not None:
                if f - start >= min_s * fps and f < 10 ** 9:
                    out.append((start, "黒い画面", f"{(f - start) / fps:.1f} 秒 真っ黒（素材が抜けていないか）"))
                elif f >= 10 ** 9 and frames and frames[-1] - start >= min_s * fps:
                    out.append((start, "黒い画面", f"最後まで真っ黒（{(frames[-1] - start) / fps:.1f} 秒）"))
                start = None
        return out

    def _audio_mono(self) -> Tuple[np.ndarray, int]:
        from .smartrender import write_audio
        seq = self.project.sequence
        tmp = str(self._work_dir12("Analysis") / f"chk_{M.new_id()[:6]}.wav")
        write_audio(self.project, seq, 0, max(1, seq.duration), tmp)
        try:
            with wave.open(tmp) as w_:
                rate, ch, sw = w_.getframerate(), w_.getnchannels(), w_.getsampwidth()
                raw = w_.readframes(w_.getnframes())
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        dt = {2: "<i2", 4: "<i4"}.get(sw, "<i2")
        a = np.frombuffer(raw, dt).astype(np.float32) / (2 ** (8 * sw - 1))
        if ch > 1:
            a = a.reshape(-1, ch)
            a = a[:, 0] if ch == 1 else np.abs(a).max(axis=1) * np.sign(a[:, 0] + 1e-9)
        return a, rate

    def check_clipping(self, audio=None) -> List[Issue]:
        """音割れ（−1 dBFS を超える所）。"""
        a, rate = audio or self._audio_mono()
        fps = self._fps()
        idx = np.nonzero(np.abs(a) >= 0.891)[0]
        out, last = [], -10 ** 9
        for i in idx:
            f = int(i / rate * fps)
            if f - last > fps:
                out.append((f, "音割れ", "音が大きすぎて割れそう（−1 dBFS 超え）。音量を下げるかリミッターを"))
                last = f
        return out

    def check_silence(self, audio=None, min_s: float = 3.0) -> List[Issue]:
        a, rate = audio or self._audio_mono()
        fps = self._fps()
        hop = max(1, rate // 10)
        n = len(a) // hop
        if n == 0:
            return []
        rms = np.sqrt((a[:n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)
        db = 20 * np.log10(rms)
        out, start = [], None
        for i, v in enumerate(list(db) + [0.0]):
            if v < -50 and start is None:
                start = i
            elif v >= -50 and start is not None:
                if (i - start) * hop / rate >= min_s:
                    out.append((int(start * hop / rate * fps), "長い無音", f"{(i - start) * hop / rate:.1f} 秒 音がない（BGM か環境音を）"))
                start = None
        return out

    def check_telop_overlap(self) -> List[Issue]:
        out = []
        tx = self._texts()
        for i, a in enumerate(tx):
            for b in tx[i + 1:]:
                if b.start >= a.end:
                    break
                f = max(a.start, b.start)
                ba, bb = self.clip_box(a, f), self.clip_box(b, f)
                if not ba or not bb:
                    continue
                if abs(ba[0] - bb[0]) * 2 < ba[2] + bb[2] and abs(ba[1] - bb[1]) * 2 < ba[3] + bb[3]:
                    out.append((f, "テロップの重なり", f"「{str(a.prop('text', ''))[:12]}」と「{str(b.prop('text', ''))[:12]}」が重なっている"))
        return out

    def check_long_telops(self, max_s: float = 8.0) -> List[Issue]:
        fps = self._fps()
        return [(c.start, "出しっぱなし", f"{c.duration / fps:.1f} 秒 出ている「{str(c.prop('text', ''))[:16]}」（長すぎて邪魔に）")
                for c in self._texts() if c.duration > max_s * fps and len(str(c.prop("text", ""))) < 40]

    def check_chapters(self) -> List[Issue]:
        """YouTube のチャプターの決まり: 最初が 0:00・3 つ以上・それぞれ 10 秒以上。"""
        seq = self.project.sequence
        fps = self._fps()
        ms = sorted((m for m in seq.markers if not m.name.startswith("⚠") and m.name.strip()), key=lambda m: m.frame)
        if not ms:
            return [(-1, "チャプター", "名前の付いたマーカーがありません（チャプターを付けると見やすくなります）")]
        out = []
        if ms[0].frame > 0:
            out.append((0, "チャプター", "最初のチャプターは 0:00 から（概要欄の書き出しでは自動で「オープニング」が入ります）"))
        if len(ms) + (1 if ms[0].frame > 0 else 0) < 3:
            out.append((-1, "チャプター", "チャプターは 3 つ以上必要です"))
        bounds = [m.frame for m in ms] + [seq.duration]
        for m, nxt in zip(ms, bounds[1:]):
            if nxt - m.frame < 10 * fps:
                out.append((m.frame, "チャプター", f"「{m.name}」が {(nxt - m.frame) / fps:.1f} 秒（10 秒以上必要）"))
        return out

    def check_hook(self) -> List[Issue]:
        """冒頭のつかみ（最初の 5 秒・15 秒で絵が動くか）。"""
        fps = self._fps()
        cuts = [f for f in self._v1_cuts() if f < 15 * fps]
        tel = [c for c in self._texts() if c.start < 5 * fps]
        out = []
        if not [f for f in cuts if f < 5 * fps] and not tel:
            out.append((0, "冒頭のつかみ", "最初の 5 秒にカットもテロップもない（離脱されやすい。見どころを先に＝アバン）"))
        if len(cuts) < 2:
            out.append((0, "冒頭のつかみ", f"最初の 15 秒のカットが {len(cuts)} 回（テンポよく 3 回以上がおすすめ）"))
        return out

    def check_shorts(self) -> List[Issue]:
        seq = self.project.sequence
        fps = self._fps()
        out = []
        if seq.width > seq.height:
            out.append((-1, "ショート", f"横長（{seq.width}×{seq.height}）です。ショートは縦 9:16（1080×1920）"))
        if seq.duration > 180 * fps:
            out.append((-1, "ショート", f"{seq.duration / fps:.0f} 秒（ショートは 3 分まで）"))
        for c in self._texts():
            b = self.clip_box(c, c.start)
            if not b:
                continue
            if b[1] + b[3] / 2 > seq.height * 0.80 or b[0] + b[2] / 2 > seq.width * 0.84:
                out.append((c.start, "ショート", f"「{str(c.prop('text', ''))[:12]}」がアプリのボタン・説明文に隠れる位置"))
        return out

    def check_yt_settings(self) -> List[Issue]:
        seq = self.project.sequence
        out = []
        if seq.width < 1280 and seq.height < 1280:
            out.append((-1, "推奨設定", f"{seq.width}×{seq.height} は小さめ（1920×1080 以上がおすすめ）"))
        if seq.fps > 61:
            out.append((-1, "推奨設定", f"{seq.fps:g} fps（YouTube は 60 fps まで）"))
        if abs(seq.width / max(1, seq.height) - 16 / 9) > 0.02 and abs(seq.width / max(1, seq.height) - 9 / 16) > 0.02:
            out.append((-1, "推奨設定", "16:9 / 9:16 以外の画面比率（黒い帯が付きます）"))
        if not any(t.clips for t in seq.audio_tracks):
            out.append((-1, "推奨設定", "音声がありません"))
        return out

    def ng_words(self) -> List[str]:
        return list(getattr(SETTINGS, "ng_words", None) or DEFAULT_NG)

    def ng_words_dialog(self):
        txt, ok = QInputDialog.getMultiLineText(self, "NG ワード", "テロップに入っていたら知らせる言葉（1 行に 1 つ）:", "\n".join(self.ng_words()))
        if ok:
            SETTINGS.ng_words = [x.strip() for x in txt.splitlines() if x.strip()]
            SETTINGS.save()

    def check_ng_words(self) -> List[Issue]:
        words = self.ng_words()
        out = []
        for c in self._texts():
            t = str(c.prop("text", ""))
            hit = [w_ for w_ in words if w_ and w_ in t]
            if hit:
                out.append((c.start, "NG ワード", f"「{'・'.join(hit)}」（広告が付きにくくなる・規約に注意）"))
        return out

    def check_credits(self) -> List[Issue]:
        """使っている画像・音声で、クレジット（作者・ライセンス）が空のもの。"""
        seq = self.project.sequence
        first: Dict[str, int] = {}
        for _t, c in seq.all_clips():
            if c.media_id:
                first[c.media_id] = min(first.get(c.media_id, 10 ** 9), c.start)
        out = []
        for m in self.project.media:
            if m.id in first and m.kind in ("image", "audio") and not (m.credit or "").strip():
                if "Mixdown" in m.path or "SE" in Path(m.path).parent.name and m.name.startswith("beep"):
                    continue
                out.append((first[m.id], "クレジット", f"{m.name} のクレジット（作者・ライセンス）が未入力"))
        return out

    def find_fillers(self) -> List[Issue]:
        out = []
        for c in self._texts():
            t = str(c.prop("text", ""))
            m = FILLERS.search(t)
            if m:
                out.append((c.start, "言いよどみ", f"「{m.group(0)}」— {t[:20]}（カットすると聞きやすい）"))
        return out

    def check_fps_mix(self) -> List[Issue]:
        seq = self.project.sequence
        out, seen = [], set()
        for _t, c in seq.all_clips():
            if c.kind != "video" or c.media_id in seen:
                continue
            seen.add(c.media_id)
            m = self.project.media_by_id(c.media_id)
            if m and m.fps and abs(m.fps - seq.fps) > 0.02 and abs(m.fps - seq.fps * 2) > 0.02:
                out.append((c.start, "フレームレート", f"{m.name} は {m.fps:g} fps（シーケンス {seq.fps:g} fps。動きがカクつくことがある）"))
        return out

    def run_checks(self, heavy: bool = False) -> List[Issue]:
        out: List[Issue] = []
        series = audio = None
        for k, (lab, fn, is_heavy) in self._checks_table().items():
            if is_heavy and not heavy:
                continue
            if k in ("flash", "black"):
                series = series or self.luma_series(1 if k == "flash" else 0)
                out += fn(series)
            elif k in ("clip", "silence"):
                audio = audio or self._audio_mono()
                out += fn(audio)
            else:
                out += fn()
        return sorted(out, key=lambda x: x[0])

    def check_all_dialog(self):
        r = QMessageBox.question(self, "まとめてチェック", "絵と音も調べますか？（点滅・黒い画面・音割れ・長い無音。長い動画は時間がかかります）",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return None
        return self.show_check("まとめてチェック", self.run_checks(r == QMessageBox.StandardButton.Yes))

    # ─────────────────────────── 番組の構成（テレビ）───────────────────────────
    def ad_breaks(self, every_min: float = 8.0) -> List[M.Marker]:
        """広告（ミッドロール）を入れる場所の候補: だいたい N 分ごとの、近くのカットの切れ目。8 分以上の動画で使える。"""
        seq = self.project.sequence
        fps = self._fps()
        if seq.duration < 8 * 60 * fps:
            self.status.showMessage("ミッドロール広告は 8 分以上の動画で使えます", 4000)
        cuts = self._v1_cuts() or list(range(0, seq.duration, int(fps)))
        made = []
        self.begin_edit()
        t = every_min * 60 * fps
        while t < seq.duration - 60 * fps:
            near = [c for c in cuts if abs(c - t) <= 60 * fps]          # 1 分以内のカットの切れ目（無ければその時間）
            f = min(near, key=lambda c: abs(c - t)) if near else int(t)
            if not any(abs(m.frame - f) < fps for m in seq.markers):
                mk = M.Marker(int(f), "⚠ 広告の区切り（候補）", "#9a6ae0", "話の切れ目で広告を入れると離脱されにくい")
                seq.markers.append(mk)
                made.append(mk)
            t += every_min * 60 * fps
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit(f"広告の区切りの候補（{len(made)} か所）")
        return made

    def eyecatch_dialog(self):
        t, ok = QInputDialog.getText(self, "アイキャッチ", "表示する文字（チャンネル名・番組名など）:", text=self.project.name or "このあと")
        if ok:
            self.eyecatch(t)

    def eyecatch(self, text: str = "", seconds: float = 2.0) -> List[M.Clip]:
        """再生ヘッドに 2 秒のアイキャッチ（色の背景 + 文字）を差し込む（後ろは押し出す）。"""
        f = self.record_player.position
        n = int(round(seconds * self._fps()))
        self.begin_edit()
        self._ripple_insert(f, n)
        bg = M.Clip(M.new_id(), "color", f, n, name="アイキャッチ", props={"color": "#1d2b53", "fade_in": 4, "fade_out": 4})
        tx = M.Clip(M.new_id(), "text", f, n, name="アイキャッチ", props={
            "text": text or self.project.name or "TeloCut", "text_anim": "pop", "text_anim_out": "fade",
            "style": {"font_size": 110, "bold": True, "font_color": "#ffe14d", "stroke_enabled": True, "stroke_color": "#000000",
                      "stroke_width": 10, "position_v": "middle", "offset_y": 0}})
        t1 = self._top12("アイキャッチ")
        t1.clips.append(bg)
        t2 = self._top12("アイキャッチ文字")
        t2.clips.append(tx)
        self.end_edit("アイキャッチ")
        return [bg, tx]

    def aban_dialog(self):
        sec, ok = QInputDialog.getDouble(self, "アバン（冒頭に見どころ）", "マーカーの周り 1 つあたりの秒数:", 3.0, 1.0, 15.0, 1)
        if ok:
            self.aban(sec)

    def aban(self, each_s: float = 3.0) -> int:
        """マーカーの所の見どころを、動画の頭に「このあと…」として並べる（テレビのアバン）。"""
        seq = self.project.sequence
        fps = self._fps()
        ms = sorted((m for m in seq.markers if not m.name.startswith("⚠")), key=lambda m: m.frame)[:6]
        if not ms:
            QMessageBox.information(self, "アバン", "見どころにマーカーを付けてください。")
            return 0
        half = int(round(each_s * fps / 2))
        rngs = [(max(0, m.frame - half), min(seq.duration, m.frame + half)) for m in ms]
        total = sum(b - a for a, b in rngs)
        self.begin_edit()
        self._ripple_insert(0, total)
        off = 0
        for a, b in rngs:
            a2, b2 = a + total, b + total
            for t in seq.tracks():
                tmp = M.Track(t.kind, t.name, clips=[c.copy() for c in t.clips if c.end > a2 and c.start < b2])
                M.split_clip(tmp, a2)
                M.split_clip(tmp, b2)
                links: Dict[str, str] = {}
                for c in tmp.clips:
                    if c.start >= a2 and c.end <= b2:
                        nl = links.setdefault(c.link, M.new_id()) if c.link else ""
                        t.clips.append(c.copy(id=M.new_id(), start=c.start - a2 + off, link=nl))
                t.sort()
            off += b - a
        lab = self._top12("アバン")
        lab.clips.append(M.Clip(M.new_id(), "text", 0, total, name="このあと", props={
            "text": "▶ このあと…", "text_anim": "fade", "style": {"font_size": 54, "bold": True, "position_h": "left",
                                                              "position_v": "top", "offset_x": 50, "offset_y": 40,
                                                              "bg_enabled": True, "bg_color": "#d01c1c", "bg_opacity": 220,
                                                              "bg_padding": 12, "stroke_enabled": False}}))
        self.end_edit(f"アバン（見どころ {len(rngs)} か所・{total / fps:.1f} 秒）")
        return total

    def end_roll_dialog(self):
        txt, ok = QInputDialog.getMultiLineText(self, "エンドロール", "流す文字（1 行ずつ）:",
                                                 "出演\n霊夢\n魔理沙\n\n編集\nTeloCut\n\nご視聴ありがとうございました")
        if ok and txt.strip():
            self.end_roll(txt)

    def end_roll(self, text: str, seconds: float = 12.0) -> M.Clip:
        """下から上へ流れるエンドロール（スタッフロール）を再生ヘッドに。"""
        from .render import text_patch
        seq = self.project.sequence
        n = int(round(seconds * self._fps()))
        c = M.Clip(M.new_id(), "text", self.record_player.position, n, name="エンドロール", props={
            "text": text, "style": {"font_size": 46, "position_v": "middle", "offset_y": 0, "line_spacing": 1.4,
                                    "stroke_enabled": True, "stroke_color": "#000000", "stroke_width": 4}})
        arr, _x, _y = text_patch(self.project, c, 1.0)
        th = arr.shape[0] if arr is not None else seq.height
        c.props["anim"] = {"pos_y": [seq.height / 2 + th / 2, -(seq.height / 2 + th / 2)], "_ease": "linear"}
        self.begin_edit()
        self._top12("エンドロール").clips.append(c)
        self.end_edit("エンドロール")
        return c

    def pr_label(self, seconds: float = 6.0) -> M.Clip:
        """「プロモーションを含みます」の表示（企業案件・提供のある動画。YouTube の「有料プロモーション」と一緒に）。"""
        c = M.Clip(M.new_id(), "text", 0, int(round(seconds * self._fps())), name="PR 表記", props={
            "text": "プロモーションを含みます", "text_anim": "fade", "text_anim_out": "fade",
            "style": {"font_size": 34, "position_h": "left", "position_v": "top", "offset_x": 36, "offset_y": 30,
                      "bg_enabled": True, "bg_color": "#000000", "bg_opacity": 150, "bg_padding": 10, "stroke_enabled": False}})
        self.begin_edit()
        self._top12("PR 表記").clips.append(c)
        self.end_edit("PR 表記")
        return c

    def l_frame(self, color: str = "#0d2a5c") -> List[M.Clip]:
        """L 字画面（映像を左上に小さく・右と下に情報の帯。ニュース・中継の形）。"""
        seq = self.project.sequence
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind in ("video", "image")]
        if not sel:
            QMessageBox.information(self, "L 字画面", "映像のクリップを選んでください。")
            return []
        a = min(c.start for _, c in sel)
        b = max(c.end for _, c in sel)
        W, H = seq.width, seq.height
        k = 0.76
        self.begin_edit()
        for _t, c in sel:
            c.props["scale"] = k
            c.props["pos_x"] = round(-(W * (1 - k)) / 2, 1)
            c.props["pos_y"] = round(-(H * (1 - k)) / 2, 1)
        tr = self._top12("L 字")
        right = M.Clip(M.new_id(), "shape", a, b - a, name="L 字（右）", props={"shape": "rect", "color": color, "fill_opacity": 1.0,
                                                                                "rect": [k * 100, 0, (1 - k) * 100, 100]})
        bottom = M.Clip(M.new_id(), "shape", a, b - a, name="L 字（下）", props={"shape": "rect", "color": color, "fill_opacity": 1.0,
                                                                                 "rect": [0, k * 100, 100, (1 - k) * 100]})
        tr.clips += [right, bottom]
        info = M.Clip(M.new_id(), "text", a, b - a, name="L 字の文字", props={
            "text": "ここに情報（天気・速報・告知）", "style": {"font_size": 44, "position_h": "left", "position_v": "bottom",
                                                     "offset_x": 40, "offset_y": int(H * (1 - k) / 2 - 22), "stroke_enabled": False}})
        self._top12("L 字の文字").clips.append(info)
        self.end_edit("L 字画面")
        return [right, bottom, info]

    def blackout_chapters(self) -> int:
        """章（名前の付いたマーカー）の区切りに、暗転（黒にフェード）を一括で。"""
        seq = self.project.sequence
        fs = [m.frame for m in seq.markers if m.frame > 0 and m.name.strip() and not m.name.startswith("⚠")]
        if not fs:
            return 0
        half = int(round(0.4 * self._fps()))
        self.begin_edit()
        tr = self._top12("暗転")
        for f in sorted(fs):
            a = max(0, f - half)
            tr.clips.append(M.Clip(M.new_id(), "color", a, f + half - a, name="暗転", props={"color": "#000000", "fade_in": f - a, "fade_out": half}))
        self.end_edit(f"章の区切りに暗転（{len(fs)} か所）")
        return len(fs)

    def ranking_dialog(self):
        n, ok = QInputDialog.getInt(self, "ランキング見出し", "何位から？（マーカーの順に 第 N 位 → 第 1 位）:", 5, 2, 50)
        if ok:
            self.ranking(n)

    def ranking(self, n: int = 5, seconds: float = 3.0) -> List[M.Clip]:
        """ランキングの見出し「第 N 位」をマーカーの所に（マーカーの名前が順位のあとに出る）。マーカーが無ければ再生ヘッドから 10 秒ごと。"""
        seq = self.project.sequence
        fps = self._fps()
        ms = sorted((m for m in seq.markers if not m.name.startswith("⚠")), key=lambda m: m.frame)[:n]
        spots = [(m.frame, m.name.strip()) for m in ms] or [(self.record_player.position + int(i * 10 * fps), "") for i in range(n)]
        self.begin_edit()
        tr = self._top12("ランキング")
        made = []
        for i, (f, name) in enumerate(spots):
            rank = n - i
            c = M.Clip(M.new_id(), "text", f, int(seconds * fps), name=f"第{rank}位", props={
                "text": f"第{rank}位" + (f"\n{name}" if name else ""), "text_anim": "zoom", "text_anim_out": "fade",
                "style": {"font_size": 120 if rank == 1 else 96, "bold": True, "font_color": "#ffd700" if rank == 1 else "#ffffff",
                          "stroke_enabled": True, "stroke_color": "#000000", "stroke_width": 10, "position_v": "middle", "offset_y": 0}})
            if tr.is_empty(c.start, c.end):
                tr.clips.append(c)
                made.append(c)
        tr.sort()
        self.end_edit(f"ランキング見出し（{len(made)} 個）")
        return made

    def countup_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("カウントアップ数字")
        f = QFormLayout(dlg)
        a, b, sec = QDoubleSpinBox(), QDoubleSpinBox(), QDoubleSpinBox()
        for s, v, mx in ((a, 0, 1e12), (b, 10000, 1e12), (sec, 2.0, 60)):
            s.setRange(-1e12, mx)
            s.setDecimals(0 if s is not sec else 1)
            s.setValue(v)
        pre, suf = QLineEdit(""), QLineEdit("円")
        for lab, w_ in (("はじめの数", a), ("おわりの数", b), ("かける秒数", sec), ("前に付ける", pre), ("後に付ける", suf)):
            f.addRow(lab, w_)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.countup(a.value(), b.value(), sec.value(), pre.text(), suf.text())

    def countup(self, start: float = 0, end: float = 10000, seconds: float = 2.0, prefix: str = "", suffix: str = "", hold: float = 1.5) -> M.Clip:
        """数字が増えていく演出（金額・登録者数・ランキングの点数など）。"""
        fps = self._fps()
        c = M.Clip(M.new_id(), "shape", self.record_player.position, int(round((seconds + hold) * fps)), name="カウントアップ", props={
            "shape": "countup", "rect": [20, 38, 60, 22], "color": "#ffe14d", "fill_opacity": 0.0, "count_from": start,
            "count_to": end, "count_frames": int(round(seconds * fps)), "prefix": prefix, "suffix": suffix})
        self.begin_edit()
        self._top12("カウントアップ").clips.append(c)
        self.end_edit("カウントアップ")
        return c

    def nico_dialog(self):
        txt, ok = QInputDialog.getMultiLineText(self, "コメントを流す", "流すコメント（1 行に 1 つ。「0:12 文字」なら時刻つき）:",
                                                "草\nwww\nかわいい\n888888\nすごい")
        if ok and txt.strip():
            self.nico_comments(txt.splitlines())

    def nico_comments(self, lines: List[str], each_s: float = 4.0, gap_s: float = 0.6) -> List[M.Clip]:
        """コメントを右から左へ流す（ニコニコ風・段をずらして重ならないように）。「m:ss 文字」なら その時刻から。"""
        from .render import text_patch
        seq = self.project.sequence
        fps = self._fps()
        W, H = seq.width, seq.height
        base = self.record_player.position
        rows = 8
        busy = [0] * rows
        made = []
        self.begin_edit()
        tr = self._top12("コメント")
        lanes: Dict[int, M.Track] = {0: tr}
        for i, line in enumerate(x for x in lines if x.strip()):
            m = re.match(r"^\s*(\d+):(\d{1,2})\s+(.+)$", line)
            if m:
                f = int((int(m.group(1)) * 60 + int(m.group(2))) * fps)
                text = m.group(3)
            else:
                f = base + int(i * gap_s * fps)
                text = line.strip()
            row = min(range(rows), key=lambda r_: busy[r_] if busy[r_] > f else -1)
            busy[row] = f + int(each_s * fps * 0.45)
            c = M.Clip(M.new_id(), "text", f, int(each_s * fps), name="コメント", props={
                "text": text, "style": {"font_size": 54, "bold": True, "font_color": "#ffffff", "stroke_enabled": True,
                                        "stroke_color": "#000000", "stroke_width": 5, "position_h": "center", "position_v": "top",
                                        "offset_y": int(H * 0.04 + row * H * 0.1)}})
            arr, _x, _y = text_patch(self.project, c, 1.0)
            tw = arr.shape[1] if arr is not None else 200
            c.props["anim"] = {"pos_x": [W / 2 + tw / 2, -(W / 2 + tw / 2)], "_ease": "linear"}
            lane = next((t for t in lanes.values() if t.is_empty(c.start, c.end)), None)
            if lane is None:
                lane = self._top12("コメント")
                lanes[len(lanes)] = lane
            lane.clips.append(c)
            lane.sort()
            made.append(c)
        self.end_edit(f"コメントを流す（{len(made)} 個）")
        return made

    def before_after(self) -> List[M.Clip]:
        """ビフォーアフター: 選んだ 2 つの映像（上下のトラック）を左右半分ずつに・ラベルと境目の線。"""
        sel = sorted([(t, c) for t, c in self.canvas.selected_clips() if c.kind in ("video", "image")],
                     key=lambda x: self.project.sequence.video_tracks.index(x[0]))
        if len(sel) != 2:
            QMessageBox.information(self, "ビフォーアフター", "上下のトラックの映像を 2 つ選んでください（下 = Before・上 = After）。")
            return []
        (_t1, before), (_t2, after) = sel
        a, b = max(before.start, after.start), min(before.end, after.end)
        self.begin_edit()
        after.props["crop"] = [50, 0, 0, 0]                 # 上の映像は右半分だけ見せる
        tr = self._top12("ビフォーアフター")
        line = M.Clip(M.new_id(), "shape", a, max(1, b - a), name="境目", props={"shape": "rect", "color": "#ffffff",
                                                                                "fill_opacity": 1.0, "rect": [49.7, 0, 0.6, 100]})
        st = {"font_size": 56, "bold": True, "position_v": "top", "offset_y": 40, "stroke_enabled": True,
              "stroke_color": "#000000", "stroke_width": 6}
        lb = M.Clip(M.new_id(), "text", a, max(1, b - a), name="Before", props={"text": "Before", "style": {**st, "position_h": "left", "offset_x": 50}})
        la = M.Clip(M.new_id(), "text", a, max(1, b - a), name="After", props={"text": "After", "style": {**st, "position_h": "right", "offset_x": 50}})
        tr.clips.append(line)
        lt = self._top12("ビフォーアフター文字")
        lt.clips += [lb, la]
        self.end_edit("ビフォーアフター")
        return [line, lb, la]

    def end_screen(self, seconds: float = 20.0) -> List[M.Clip]:
        """終了画面（最後の 20 秒に、動画 2 つ + チャンネル登録の目安の枠と文字）。"""
        seq = self.project.sequence
        fps = self._fps()
        n = int(round(seconds * fps))
        if seq.duration < 25 * fps:
            QMessageBox.information(self, "終了画面", "終了画面は 25 秒以上の動画で使えます（最後の 5〜20 秒）。")
            return []
        a = seq.duration - n
        self.begin_edit()
        tr = self._top12("終了画面")
        frame = M.Clip(M.new_id(), "shape", a, n, name="終了画面の枠", props={"shape": "endcard", "color": "#ffffff", "fill_opacity": 0.5})
        tx = M.Clip(M.new_id(), "text", a, n, name="終了画面の文字", props={
            "text": "ご視聴ありがとうございました！", "text_anim": "fade",
            "style": {"font_size": 54, "bold": True, "position_v": "top", "offset_y": 60, "stroke_enabled": True,
                      "stroke_color": "#000000", "stroke_width": 6}})
        tr.clips.append(frame)
        self._top12("終了画面の文字").clips.append(tx)
        self.end_edit("終了画面（最後の 20 秒）")
        return [frame, tx]

    def chapter_cards(self, seconds: float = 2.0) -> int:
        """章の扉: 名前の付いたマーカーの所に、全画面の章タイトル（2 秒）を差し込む（後ろは押し出す）。"""
        seq = self.project.sequence
        fps = self._fps()
        ms = sorted((m for m in seq.markers if m.name.strip() and not m.name.startswith("⚠")), key=lambda m: -m.frame)
        if not ms:
            QMessageBox.information(self, "章の扉", "名前の付いたマーカー（章）がありません。")
            return 0
        n = int(round(seconds * fps))
        self.begin_edit()
        bg_t, tx_t = self._top12("章の扉"), self._top12("章の扉の文字")
        total = len(ms)
        for i, m in enumerate(ms):                        # 後ろから入れると、前の場所がずれない
            f = m.frame
            name = m.name.strip()
            self._ripple_insert(f, n)
            m.frame = f                                    # 章の始まりは扉の頭
            bg_t.clips.append(M.Clip(M.new_id(), "color", f, n, name=f"扉 {name}", props={"color": "#101018", "fade_in": 3}))
            tx_t.clips.append(M.Clip(M.new_id(), "text", f, n, name=f"扉 {name}", props={
                "text": f"第{total - i}章\n{name}", "text_anim": "fade", "text_anim_out": "fade",
                "style": {"font_size": 84, "bold": True, "position_v": "middle", "offset_y": 0, "stroke_enabled": False}}))
        bg_t.sort()
        tx_t.sort()
        self.end_edit(f"章の扉（{total} 個）")
        return total

    def opening_dialog(self):
        t, ok = QInputDialog.getText(self, "オープニングタイトル", "タイトル:", text=self.project.sequence.name)
        if ok:
            s, ok2 = QInputDialog.getText(self, "オープニングタイトル", "サブタイトル（空でも可）:")
            if ok2:
                self.opening_title(t, s)

    def opening_title(self, title: str, subtitle: str = "", seconds: float = 3.5) -> List[M.Clip]:
        """オープニング（色の背景・大きなタイトルが寄ってくる・サブタイトル）を再生ヘッドに差し込む。"""
        f = self.record_player.position
        n = int(round(seconds * self._fps()))
        self.begin_edit()
        self._ripple_insert(f, n)
        bg = M.Clip(M.new_id(), "color", f, n, name="オープニング", props={"color": "#14143a", "fade_out": 6})
        t1 = M.Clip(M.new_id(), "text", f, n, name="タイトル", props={
            "text": title, "text_anim": "zoom", "text_anim_out": "fade",
            "style": {"font_size": 120, "bold": True, "position_v": "middle", "offset_y": -40, "stroke_enabled": True,
                      "stroke_color": "#000000", "stroke_width": 8}})
        made = [bg, t1]
        self._top12("オープニング").clips.append(bg)
        tt = self._top12("オープニング文字")
        tt.clips.append(t1)
        if subtitle:
            t2 = M.Clip(M.new_id(), "text", f + int(0.5 * self._fps()), n - int(0.5 * self._fps()), name="サブタイトル", props={
                "text": subtitle, "text_anim": "fade", "style": {"font_size": 50, "position_v": "middle", "offset_y": 90,
                                                                 "font_color": "#9fd0ff", "stroke_enabled": False}})
            self._top12("オープニング文字").clips.append(t2)
            made.append(t2)
        self.end_edit("オープニングタイトル")
        return made

    def cast_dialog(self):
        txt, ok = QInputDialog.getMultiLineText(self, "出演者のテロップ（一括）", "「名前, 肩書き」を 1 行ずつ:", "山田 太郎, 編集長\n佐藤 花子, ゲスト")
        if ok and txt.strip():
            self.cast_supers([l for l in txt.splitlines() if l.strip()])

    def cast_supers(self, lines: List[str], every_s: float = 6.0) -> List[M.Clip]:
        """出演者の名前と肩書き（ロワーサード）をまとめて置く: 選んだクリップの頭ごと（無ければ再生ヘッドから 6 秒ごと）。"""
        from . import templates as TP
        fps = self._fps()
        tp = next((t for t in TP.telop_templates() if t["name"].startswith("ロワーサード")), None)
        if tp is None:
            return []
        starts = sorted({c.start for _, c in self.canvas.selected_clips()})
        made = []
        self.begin_edit()
        for i, line in enumerate(lines):
            name, _, role = [x.strip() for x in line.replace("，", ",").partition(",")]
            f = starts[i] if i < len(starts) else self.record_player.position + int(i * every_s * fps)
            made += TP.apply_telop(self.project.sequence, tp, f, {"名前": name, "肩書き": role})
        self.end_edit(f"出演者のテロップ（{len(lines)} 人）")
        return made

    def telops_to_safe(self, safe: float = 0.9) -> int:
        """テレビのタイトルセーフ（画面の 90%）からはみ出したテロップを内側へずらす。"""
        seq = self.project.sequence
        W, H = seq.width, seq.height
        x0, x1 = W * (1 - safe) / 2, W * (1 + safe) / 2
        y0, y1 = H * (1 - safe) / 2, H * (1 + safe) / 2
        n = 0
        self.begin_edit()
        for c in self._texts():
            b = self.clip_box(c, c.start)
            if not b:
                continue
            dx = max(0.0, x0 - (b[0] - b[2] / 2)) - max(0.0, (b[0] + b[2] / 2) - x1)
            dy = max(0.0, y0 - (b[1] - b[3] / 2)) - max(0.0, (b[1] + b[3] / 2) - y1)
            if abs(dx) > 0.5 or abs(dy) > 0.5:
                c.props["pos_x"] = round(float(c.prop("pos_x", 0.0) or 0.0) + dx, 1)
                c.props["pos_y"] = round(float(c.prop("pos_y", 0.0) or 0.0) + dy, 1)
                n += 1
        self.end_edit(f"テロップをセーフエリアに（{n} 個）")
        return n

    # ─────────────────────────── 映画の手法 ───────────────────────────
    def cinema_look(self) -> int:
        """映画風: シネスコ（2.39:1）の黒帯 + 映像にシネマのルック（ティール＆オレンジ）+ 少しのフィルムグレイン。"""
        seq = self.project.sequence
        clips = [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image")] or \
                [c for t in seq.video_tracks for c in t.clips if c.kind in ("video", "image")]
        self.begin_edit()
        for c in clips:
            self._put_fx(c, "look", {"kind": "シネマ", "amount": 1.0})
            self._put_fx(c, "grain", {"amount": 0.12})
        self.end_edit("映画風（ルック）")
        if not any(c.prop("shape") == "letterbox" for _t, c in seq.all_clips()):
            self.letterbox(2.39)
        return len(clips)

    def tempo(self) -> dict:
        """カットのテンポ（平均・中央値・1 分あたりのカット数・長すぎるカット）。"""
        seq = self.project.sequence
        fps = self._fps()
        cuts = sorted((c for c in (seq.video_tracks[0].clips if seq.video_tracks else []) if c.kind in ("video", "image", "nest")),
                      key=lambda c: c.start)
        if not cuts:
            return {}
        lens = np.array([c.duration / fps for c in cuts])
        per_min = len(cuts) / max(1e-6, seq.duration / fps / 60)
        long_ = [(c.start, round(c.duration / fps, 1)) for c in cuts if c.duration / fps > max(8.0, float(np.median(lens)) * 3)]
        hint = ("テンポが速い（ショート・バラエティ向き）" if np.mean(lens) < 2.5 else
                "ふつう（解説・実況向き）" if np.mean(lens) < 6 else "ゆっくり（長いカットが多い。間延びしていないか確認）")
        return {"cuts": len(cuts), "avg": round(float(lens.mean()), 2), "median": round(float(np.median(lens)), 2),
                "per_min": round(per_min, 1), "long": long_, "hint": hint,
                "hist": [int(((lens >= a) & (lens < b)).sum()) for a, b in ((0, 1), (1, 2), (2, 4), (4, 8), (8, 1e9))]}

    def tempo_dialog(self):
        t = self.tempo()
        if not t:
            QMessageBox.information(self, "カットのテンポ", "V1 にカットがありません。")
            return None
        labs = ["1 秒未満", "1〜2 秒", "2〜4 秒", "4〜8 秒", "8 秒以上"]
        mx = max(t["hist"]) or 1
        bars = "\n".join(f"{l:>6}  {'█' * int(20 * n / mx)} {n}" for l, n in zip(labs, t["hist"]))
        longs = "、".join(f"{self.tc_str(f)}（{s} 秒）" for f, s in t["long"][:6])
        QMessageBox.information(self, "カットのテンポ",
                                f"カット {t['cuts']} 個・平均 {t['avg']} 秒・中央値 {t['median']} 秒・1 分に {t['per_min']} カット\n"
                                f"→ {t['hint']}\n\n{bars}" + (f"\n\n長いカット: {longs}" if longs else ""))
        return t

    def palette(self, n: int = 6) -> List[str]:
        """今のフレームから目立つ色を取り出す（テロップ・サムネの色合わせに）。"""
        from PIL import Image
        im = self._thumb(self.record_player.position, 320).convert("RGB")
        q = im.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()[: n * 3]
        counts = sorted(q.getcolors(), reverse=True)
        out = []
        for _cnt, idx in counts:
            r, g, b = pal[idx * 3: idx * 3 + 3]
            out.append(f"#{r:02X}{g:02X}{b:02X}")
        return out[:n]

    def palette_dialog(self):
        cols = self.palette()
        dlg = QDialog(self)
        dlg.setWindowTitle("フレームの配色")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("色をクリック: コピー（テロップを選んでいれば文字の色にする）"))
        h = QHBoxLayout()
        for c in cols:
            b = QPushButton(c)
            b.setMinimumSize(90, 60)
            light = sum(int(c[i:i + 2], 16) for i in (1, 3, 5)) > 380
            b.setStyleSheet(f"background: {c}; color: {'#000' if light else '#fff'}; font-weight: bold;")
            b.clicked.connect(lambda _=False, c=c: self.use_color(c))
            h.addWidget(b)
        v.addLayout(h)
        dlg.show()
        self._palette_dlg = dlg
        return cols

    def use_color(self, col: str) -> int:
        QApplication.clipboard().setText(col)
        tx = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        if tx:
            self.begin_edit()
            for c in tx:
                c.props["style"] = {**(c.prop("style") or {}), "font_color": col}
            self.end_edit(f"テロップの色を {col} に")
        self.status.showMessage(f"{col} をコピーしました", 3000)
        return len(tx)

    def rewind(self, seconds: float = 2.0) -> Optional[M.Clip]:
        """巻き戻し演出: 再生ヘッドの前 N 秒を逆再生で差し込む（VHS 風・◀◀ の表示）。"""
        from . import render as R
        seq = self.project.sequence
        fps = self._fps()
        f = self.record_player.position
        a = max(0, f - int(round(seconds * fps)))
        t, c = None, None
        for tr in reversed(seq.video_tracks):
            cc = tr.clip_at(max(a, f - 1))
            if cc is not None and cc.kind == "video" and not tr.locked:
                t, c = tr, cc
                break
        if c is None or f <= a:
            QMessageBox.information(self, "巻き戻し", "映像のある所の少し後ろで使ってください。")
            return None
        a = max(a, c.start)
        n = int(round((f - a) / 2))                       # 2 倍速で巻き戻す
        rw = c.copy(id=M.new_id(), start=f, duration=max(1, n), link="", src_in=int(R.src_frame(c, a)))
        rw.props["speed"] = -2.0 * abs(float(c.prop("speed", 1.0) or 1.0))
        rw.props["dissolve"] = 0
        rw.props["effects"] = list(c.prop("effects") or []) + [{"name": "vhs", "params": {}, "on": True}]
        rw.name = (c.name or "") + "（巻き戻し）"
        self.begin_edit()
        M.place_clips(seq, [(t, rw)], "insert")
        self._top12("巻き戻し").clips.append(M.Clip(M.new_id(), "text", f, max(1, n), name="◀◀", props={
            "text": "◀◀", "style": {"font_size": 80, "bold": True, "position_h": "left", "position_v": "top",
                                   "offset_x": 50, "offset_y": 40, "stroke_enabled": True, "stroke_color": "#000000", "stroke_width": 6}}))
        self.end_edit("巻き戻し演出")
        return rw

    def timelapse_dialog(self):
        s, ok = QInputDialog.getDouble(self, "タイムラプス", "選んだクリップを何秒にしますか（速さを自動で決める）:", 5.0, 0.5, 600, 1)
        if ok:
            self.timelapse(s)

    def timelapse(self, seconds: float) -> float:
        """選んだクリップがちょうど N 秒になる速さにする（後ろは全トラックでずらす）。戻り値: 速さ。"""
        sel = [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "nest")]
        if not sel:
            QMessageBox.information(self, "タイムラプス", "映像のクリップを選んでください。")
            return 0.0
        a, b = min(c.start for c in sel), max(c.end for c in sel)
        k = (b - a) / max(1.0, seconds * self._fps())
        seq = self.project.sequence
        keep = (seq.mark_in, seq.mark_out)
        seq.mark_in, seq.mark_out = a, b
        try:
            self.speed_range(k)
        finally:
            seq.mark_in, seq.mark_out = keep
        return round(k, 4)

    def fit_target_dialog(self):
        cur = float(self.project.settings.get("target_seconds", 0) or 0)
        s, ok = QInputDialog.getDouble(self, "全体を目標の長さに", "目標の長さ（秒）:", cur or 600.0, 5, 36000, 1)
        if ok:
            self.fit_to_target(s)

    def fit_to_target(self, seconds: float) -> float:
        """全体の速さを少しだけ変えて、目標の長さにぴったり合わせる（テレビの尺合わせ。±15% まで）。"""
        seq = self.project.sequence
        k = seq.duration / max(1.0, seconds * self._fps())
        if abs(k - 1) > 0.15:
            QMessageBox.information(self, "尺合わせ", f"速さが {k:.2f} 倍になります（±15% を超えるので、カットで調整してください）。")
            return 0.0
        keep = (seq.mark_in, seq.mark_out)
        seq.mark_in, seq.mark_out = 0, seq.duration
        try:
            self.speed_range(k)
        finally:
            seq.mark_in, seq.mark_out = -1, -1
        for t in seq.tracks():                            # 早送りの表示は付けない
            if t.name == "早送り":
                t.clips = []
        seq.video_tracks = [t for t in seq.video_tracks if not (t.name == "早送り" and not t.clips)]
        return round(k, 4)

    # ─────────────────────────── 音 ───────────────────────────
    def place_narration(self, path: str, frame: Optional[int] = None) -> M.Clip:
        """録音したナレーションを「ナレーション」トラックに置く。"""
        seq = self.project.sequence
        f = self.record_player.position if frame is None else frame
        self.begin_edit()
        info = self._add_media(path, "ナレーション")
        n = max(1, int(round(info.duration_s * self._fps())))
        tr = self._audio_track("ナレーション", f, f + n)
        c = M.Clip(M.new_id(), "audio", f, n, 0, info.id, info.name)
        tr.clips.append(c)
        tr.sort()
        self.end_edit("ナレーション")
        self.bin.refresh()
        return c

    def narration_dialog(self):
        from .narration import NarrationDialog
        dlg = NarrationDialog(self, str(self._work_dir12("Narration")))
        if dlg.exec() and dlg.path:
            self.place_narration(dlg.path)
        return dlg

    def se_on_cuts_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "カットの継ぎ目に入れる効果音（シュッ など）", SETTINGS.se_dir or "", "音声 (*.wav *.mp3 *.ogg *.m4a)")
        if p:
            self.se_on_cuts(p)

    def se_on_cuts(self, path: str, lead_s: float = 0.15) -> int:
        """V1 のカットの継ぎ目ごとに効果音（少し前から鳴らす）。選んだクリップがあれば、その頭だけ。"""
        fps = self._fps()
        starts = sorted({c.start for _, c in self.canvas.selected_clips() if c.start > 0}) or self._v1_cuts()
        if not starts:
            return 0
        self.begin_edit()
        info = self._add_media(path, "効果音")
        full = max(1, int(round(info.duration_s * fps))) if info.duration_s else int(fps)
        tr = self._audio_track("SE（継ぎ目）", 0, 1)
        n = 0
        for i, s in enumerate(starts):
            a = max(0, s - int(round(lead_s * fps)))
            nxt = starts[i + 1] - int(round(lead_s * fps)) if i + 1 < len(starts) else a + full
            d = min(full, nxt - a)
            if d > 0 and tr.is_empty(a, a + d):
                tr.clips.append(M.Clip(M.new_id(), "audio", a, d, 0, info.id, info.name))
                n += 1
        tr.sort()
        self.end_edit(f"カットの継ぎ目に効果音（{n} 個）")
        self.bin.refresh()
        return n

    def room_tone(self) -> int:
        """ルームトーン: 選んだ音声（部屋の静かな音）で、同じトラックの隙間を埋める（映画の整音のやり方。音が急に消えない）。"""
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind == "audio"]
        if not sel:
            QMessageBox.information(self, "ルームトーン", "部屋の静かな音（話していない所）の音声クリップを選んでください。")
            return 0
        t, src = sel[0]
        seq = self.project.sequence
        end = max((c.end for c in t.clips), default=0)
        gaps, cur = [], 0
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.start > cur:
                gaps.append((cur, c.start))
            cur = max(cur, c.end)
        fade = max(1, int(round(0.05 * self._fps())))
        self.begin_edit()
        n = 0
        for a, b in gaps:
            p = a
            while p < b:
                d = min(src.duration, b - p)
                t.clips.append(src.copy(id=M.new_id(), start=p, duration=d, link="", name="ルームトーン",
                                        props={**src.props, "fade_in": fade, "fade_out": fade, "volume_db": float(src.prop("volume_db", 0.0)) - 3}))
                p += d
                n += 1
        t.sort()
        self.end_edit(f"ルームトーンで隙間を埋める（{len(gaps)} か所）")
        _ = end, seq
        return n

    def bgm_fades(self, seconds: float = 1.5) -> int:
        """BGM（選んだ音声・無ければ名前に BGM が入ったトラック）の頭とおしりにフェード。"""
        seq = self.project.sequence
        clips = [c for _, c in self.canvas.selected_clips() if c.kind == "audio"] or \
                [c for t in seq.audio_tracks if "BGM" in t.name.upper() for c in t.clips]
        n = int(round(seconds * self._fps()))
        self.begin_edit()
        for c in clips:
            c.props["fade_in"] = min(n, c.duration // 2)
            c.props["fade_out"] = min(n, c.duration // 2)
        self.end_edit(f"BGM にフェード（{len(clips)} 個）")
        return len(clips)

    # ─────────────────────────── 公開・仕上げ ───────────────────────────
    def thumb_preview_dialog(self, image: str = ""):
        """サムネイルを、スマホの一覧・関連動画の大きさで見る（文字が読めるか）。"""
        from PIL import Image
        if image:
            im = Image.open(image).convert("RGB")
        else:
            im = self._thumb(self.record_player.position, 1280).convert("RGB")
        im = im.resize((1280, 720))
        dlg = QDialog(self)
        dlg.setWindowTitle("サムネを小さく表示して確認")
        v = QVBoxLayout(dlg)
        h = QHBoxLayout()
        sizes = [(480, 270, "パソコンのトップ"), (320, 180, "スマホの一覧"), (168, 94, "関連動画（小）")]
        for w_, h_, lab in sizes:
            small = im.resize((w_, h_), Image.LANCZOS)
            arr = np.ascontiguousarray(np.asarray(small))
            qi = QImage(arr.data, w_, h_, w_ * 3, QImage.Format.Format_RGB888).copy()
            box = QVBoxLayout()
            pic = QLabel()
            pic.setPixmap(QPixmap.fromImage(qi))
            box.addWidget(pic)
            box.addWidget(QLabel(f"{lab}（{w_}×{h_}）"))
            h.addLayout(box)
        v.addLayout(h)
        v.addWidget(QLabel("いちばん小さい大きさで文字が読めるか、顔が分かるかを確認してください。"))
        dlg.show()
        self._thumb_dlg = dlg
        return [s[:2] for s in sizes]

    def yt_subs(self, path: str, max_chars: int = 42) -> str:
        """YouTube 用の字幕（SRT）: 1 行 42 文字まで・2 行まで・重なりを切る・0.7 秒以上。"""
        from .features10 import wrap_text
        fps = self._fps()
        subs = self._texts()
        out = []
        for i, c in enumerate(subs):
            a = c.start
            b = c.end
            if i + 1 < len(subs) and subs[i + 1].start < b:
                b = subs[i + 1].start
            b = max(b, a + int(round(0.7 * fps)))
            text = wrap_text(str(c.prop("text_h", c.prop("text", ""))).replace("\n", " "), max_chars)
            lines = text.split("\n")
            # 2 行に収まらなければ、2 行ずつの字幕に分けて、文字数で時間を割る
            chunks = [lines[k:k + 2] for k in range(0, len(lines), 2)]
            total = sum(len("".join(x)) for x in chunks) or 1

            def ts(f):
                ms = int(round(f / fps * 1000))
                return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
            cur = a
            for j, ch in enumerate(chunks):
                end = b if j == len(chunks) - 1 else cur + int(round((b - a) * len("".join(ch)) / total))
                out.append(f"{len(out) + 1}\n{ts(cur)} --> {ts(end)}\n" + "\n".join(ch) + "\n")
                cur = end
        Path(path).write_text("\n".join(out), encoding="utf-8")
        return path

    def yt_subs_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "YouTube 用の字幕", f"{self.project.sequence.name}.srt", "SRT (*.srt)")
        if p:
            self.yt_subs(p)

    def shorts_from_hype(self, length_s: float = 55.0) -> Optional[M.Sequence]:
        """音から一番盛り上がった 55 秒を探して、縦型ショートのシーケンスを作る。"""
        from . import autoclip as AC
        from .smartrender import write_audio
        from .youtube import make_shorts_sequence
        seq = self.project.sequence
        fps = self._fps()
        tmp = str(self._work_dir12("Analysis") / f"shorts_{M.new_id()[:6]}.wav")
        write_audio(self.project, seq, 0, seq.duration, tmp)
        try:
            sc = AC.score_series(AC.measure(tmp))["score"]
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        n = int(length_s)
        if len(sc) <= n:
            start = 0
        else:
            sums = np.convolve(sc, np.ones(n), mode="valid")
            start = int(np.argmax(sums))
        a = int(start * fps)
        b = min(seq.duration, a + int(length_s * fps))
        keep = (seq.mark_in, seq.mark_out)
        seq.mark_in, seq.mark_out = a, b
        try:
            part = self.in_out_to_sequence()
        finally:
            seq.mark_in, seq.mark_out = keep
        if part is None:
            return None
        self.begin_edit()
        d = make_shorts_sequence(self.project, part, True)
        d.name = f"{seq.name}（ショート・見どころ）"
        (self.project.bin_of(seq.id) or self.project.bins[0]).add("sequence", d.id)
        self.end_edit("盛り上がりからショート")
        self.bin.refresh()
        return d

    def correction_list(self, path: str) -> str:
        """修正指示リスト（マーカーのコメント・サムネ付きの HTML。テレビの試写の直しリストのように）。"""
        seq = self.project.sequence
        ms = sorted(seq.markers, key=lambda m: m.frame)
        img_dir = Path(path).with_suffix("")
        img_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, m in enumerate(ms, 1):
            fn = f"fix_{i:03d}.jpg"
            self._thumb(m.frame, 240).convert("RGB").save(img_dir / fn, quality=85)
            rows.append(f"<tr><td>{i}</td><td><img src='{img_dir.name}/{fn}'></td><td>{self.tc_str(m.frame)}</td>"
                        f"<td><b>{html.escape(m.name)}</b><br>{html.escape(m.comment or '')}</td><td>☐</td></tr>")
        doc = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(seq.name)} 修正指示</title>
<style>body{{font-family:"Yu Gothic UI",sans-serif;margin:20px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;vertical-align:top;font-size:13px}}img{{width:240px}}</style>
<h2>{html.escape(seq.name)} 修正指示（{len(ms)} か所・{datetime.datetime.now():%Y-%m-%d}）</h2>
<table><tr><th>#</th><th>絵</th><th>TC</th><th>指示</th><th>済</th></tr>{''.join(rows)}</table>"""
        Path(path).write_text(doc, encoding="utf-8")
        return path

    def correction_list_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "修正指示リスト", f"{self.project.sequence.name}_修正指示.html", "HTML (*.html)")
        if p:
            self.correction_list(p)

    def new_version(self) -> M.Sequence:
        """新しい版にする（今のシーケンスを「v2」「v3」… として複製して開く。前の版はそのまま残る）。"""
        src = self.project.sequence
        base = re.sub(r"\s*v\d+$", "", src.name)
        nums = [int(m.group(1)) for s in self.project.sequences for m in [re.search(r"\sv(\d+)$", s.name)] if m and s.name.startswith(base)]
        nxt = max(nums + [1]) + 1
        self.begin_edit()
        sq = M.Sequence.from_dict(copy.deepcopy(dataclasses.asdict(src)))
        sq.id = M.new_id()
        sq.name = f"{base} v{nxt}"
        for _t, c in sq.all_clips():
            c.id = M.new_id()
        self.project.sequences.append(sq)
        (self.project.bin_of(src.id) or self.project.bins[0]).add("sequence", sq.id)
        self.end_edit(f"新しい版（v{nxt}）")
        self.load_sequence(sq.id)
        self.bin.refresh()
        return sq

    def style_guide(self, path: str) -> str:
        """テロップの見本帳（使っているテロップの見た目の種類と、その設定。テレビのテロップ表のように）。"""
        from PIL import Image
        from .render import text_patch
        styles: Dict[str, Tuple[M.Clip, int]] = {}
        for c in self._texts():
            key = repr(sorted((c.prop("style") or {}).items()))
            if key in styles:
                styles[key] = (styles[key][0], styles[key][1] + 1)
            else:
                styles[key] = (c, 1)
        img_dir = Path(path).with_suffix("")
        img_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, (c, n) in enumerate(styles.values(), 1):
            arr, _x, _y = text_patch(self.project, c, 0.5)
            fn = f"style_{i:02d}.png"
            if arr is not None:
                bg = Image.new("RGBA", (arr.shape[1] + 20, arr.shape[0] + 20), (90, 90, 100, 255))
                bg.alpha_composite(Image.fromarray(arr), (10, 10))
                bg.save(img_dir / fn)
            st = ", ".join(f"{k}={v}" for k, v in sorted((c.prop("style") or {}).items()))
            rows.append(f"<tr><td>{i}</td><td><img src='{img_dir.name}/{fn}'></td><td>{n} 回</td><td>{html.escape(st)}</td></tr>")
        doc = f"""<!doctype html><meta charset="utf-8"><title>テロップの見本帳</title>
<style>body{{font-family:"Yu Gothic UI",sans-serif;margin:20px}}table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:6px;font-size:12px;vertical-align:top}}img{{max-width:520px}}</style>
<h2>{html.escape(self.project.sequence.name)} テロップの見本帳（{len(styles)} 種類）</h2>
<table><tr><th>#</th><th>見た目</th><th>使った数</th><th>設定</th></tr>{''.join(rows)}</table>"""
        Path(path).write_text(doc, encoding="utf-8")
        return path

    def style_guide_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "テロップの見本帳", f"{self.project.sequence.name}_テロップ見本帳.html", "HTML (*.html)")
        if p:
            self.style_guide(p)

    def translation_export(self, path: str) -> str:
        """翻訳用の CSV（id・時間・原文・訳）。訳の列を埋めて「翻訳を読み込み」で別トラックの字幕に。"""
        fps = self._fps()
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w_ = csv.writer(f)
            w_.writerow(["id", "開始(秒)", "終了(秒)", "原文", "訳"])
            for c in self._texts():
                w_.writerow([c.id, round(c.start / fps, 3), round(c.end / fps, 3), str(c.prop("text_h", c.prop("text", ""))), ""])
        return path

    def translation_import(self, path: str, lang: str = "訳") -> int:
        seq = self.project.sequence
        rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
        self.begin_edit()
        tr = M.Track("video", f"字幕: {lang}", selected=False)
        seq.video_tracks.append(tr)
        n = 0
        for r in rows:
            txt = (r.get("訳") or "").strip()
            if not txt:
                continue
            _t, src = seq.find(r.get("id", ""))
            if src is None:
                continue
            c = src.copy(id=M.new_id(), name=f"{lang}: {txt[:10]}")
            c.props["text"] = txt
            c.props.pop("text_h", None)
            st = dict(c.prop("style") or {})
            st["offset_y"] = int(st.get("offset_y", 60) or 60) + 90          # 原文の少し上に
            c.props["style"] = st
            if tr.is_empty(c.start, c.end):
                tr.clips.append(c)
                n += 1
        tr.sort()
        self.end_edit(f"翻訳の字幕（{n} 個）")
        return n

    def translation_export_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "翻訳用の CSV", f"{self.project.sequence.name}_翻訳.csv", "CSV (*.csv)")
        if p:
            self.translation_export(p)

    def translation_import_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "翻訳を読み込み（訳の列を埋めた CSV）", "", "CSV (*.csv)")
        if p:
            lang, ok = QInputDialog.getText(self, "翻訳を読み込み", "言語の名前（トラック名になる）:", text="English")
            if ok:
                self.translation_import(p, lang.strip() or "訳")
