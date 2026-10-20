"""テロップと編集の追加機能（MainWindow のミックスイン）。

テロップ: 位置プリセット（9 か所）・スタイルの一括適用・表記統一辞書・テキストから一括配置・読み時間で長さ・
          まとめてずらす・音声に合わせる・話者ごとの色・マーカー ⇔ テロップ（登場 / 退場アニメは render.py）
編集:     マーカーの CSV / チャプター・マーカーで分割・隙間へ移動 / 黒で埋める・書き出し前チェック・フィット / フィル・PinP・
          ケン・バーンズ・スナップショット・未使用素材の整理・使用素材リスト・編集リスト・間隔をそろえる・
          再生ヘッドへ移動・同じ素材を選択・映像 / 音声だけ選択・素材の差し替え・シーケンス情報・IN-OUT を新しいシーケンスに
"""
from __future__ import annotations

import copy
import csv
import dataclasses
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget,
                             QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from core.timecode import frames_to_timecode
from . import model as M
from .settings import SETTINGS

POSITIONS = [("tl", "左上"), ("tc", "上"), ("tr", "右上"), ("ml", "左"), ("mc", "中央"), ("mr", "右"),
             ("bl", "左下"), ("bc", "下"), ("br", "右下")]
SPEAKER_COLORS = ["#FFE14D", "#6FD3FF", "#FF8FB1", "#8CF28C", "#FFB067", "#C7A0FF", "#FFFFFF"]


def reading_frames(text: str, fps: float, cps: float = 6.0, lo: float = 1.2, hi: float = 6.0) -> int:
    """文字数から、読める長さ（フレーム）。改行・空白は数えない。"""
    n = len("".join(str(text).split()))
    return max(1, int(round(max(lo, min(hi, n / max(0.5, cps))) * fps)))


def covered(seq: M.Sequence) -> List[Tuple[int, int]]:
    """背景になる映像（動画・画像・カラー。テロップや図形は数えない）がある区間をまとめたもの。"""
    iv = sorted((c.start, c.end) for t in seq.video_tracks if not t.muted
                for c in t.clips if c.enabled and c.kind in ("video", "image", "color"))
    out: List[List[int]] = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def gaps(seq: M.Sequence) -> List[Tuple[int, int]]:
    """先頭から最後の映像までで、背景の映像が何も無い（黒になる）区間。"""
    out, cur = [], 0
    for a, b in covered(seq):
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    return out


class Features4Mixin:
    def _feature4_commands(self) -> dict:
        return {
            # テロップ
            "telop_pos": self.telop_position_dialog, "telop_style_all": self.apply_style_to_all,
            "telop_dict": self.telop_dict_dialog, "telop_from_text": self.telops_from_text_dialog,
            "telop_auto_len": self.telop_auto_length, "telop_shift": self.telop_shift_dialog,
            "telop_fit_audio": self.telop_fit_audio, "telop_speaker_colors": self.telop_speaker_colors,
            "markers_to_telops": self.markers_to_telops, "telops_to_markers": self.telops_to_markers,
            # 編集
            "marker_panel": lambda: self.show_dock(self.dock_markers), "split_at_markers": self.split_at_markers,
            "marker_csv": self.export_markers_csv, "marker_chapters": self.copy_marker_chapters,
            "next_gap": lambda: self.goto_gap(1), "prev_gap": lambda: self.goto_gap(-1),
            "fill_gaps": self.fill_gaps_black, "preflight": self.preflight_dialog,
            "fit_frame": lambda: self.fit_clips("fit"), "fill_frame": lambda: self.fit_clips("fill"),
            "pip": self.pip_dialog, "ken_burns": self.ken_burns, "snapshot": self.snapshot_sequence,
            "unused_media": self.unused_media_dialog, "media_list": self.export_media_list,
            "edit_list": self.export_edit_list, "space_clips": self.space_clips_dialog,
            "move_to_playhead": self.move_to_playhead, "select_same_media": self.select_same_media,
            "select_video_only": lambda: self.select_kind_only("video"),
            "select_audio_only": lambda: self.select_kind_only("audio"),
            "replace_media": self.replace_media_dialog, "seq_stats": self.sequence_stats,
            "in_out_to_seq": self.in_out_to_sequence,
        }

    def _init_features4(self):
        pass

    def _build_feature4_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        t = QMenu_before(mb, help_menu, "テロップ")
        for c in ("telop_editor", None, "telop_pos", "telop_style_all", "telop_speaker_colors", None,
                  "telop_from_text", "telop_dict", None, "telop_auto_len", "telop_shift", "telop_fit_audio", None,
                  "markers_to_telops", "telops_to_markers"):
            t.addSeparator() if c is None else self._a(t, c)
        e = QMenu_before(mb, help_menu, "整理・チェック")
        for c in ("marker_panel", "marker_csv", "marker_chapters", "split_at_markers", None, "next_gap", "prev_gap", "fill_gaps", "preflight", None,
                  "fit_frame", "fill_frame", "pip", "ken_burns", None,
                  "space_clips", "move_to_playhead", "select_same_media", "select_video_only", "select_audio_only",
                  "replace_media", None, "snapshot", "in_out_to_seq", "seq_stats", None,
                  "unused_media", "media_list", "edit_list"):
            e.addSeparator() if c is None else self._a(e, c)

    # ─────────────────────────── 共通 ───────────────────────────
    def tc_str(self, f: int) -> str:
        seq = self.project.sequence
        return frames_to_timecode(f + self.tc_offset(seq), seq.fps)

    def _text_targets(self) -> List[M.Clip]:
        """選んだテロップ（無ければシーケンスのテロップすべて）を時間順に。"""
        seq = self.project.sequence
        sel = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        if not sel:
            sel = [c for _, c in seq.all_clips() if c.kind == "text"]
        return sorted(sel, key=lambda c: c.start)

    def _track_of(self, clip: M.Clip) -> Optional[M.Track]:
        return self.project.sequence.find(clip.id)[0]

    def _msg(self, title: str, text: str):
        QMessageBox.information(self, title, text)

    # ─────────────────────────── テロップ ───────────────────────────
    def telop_position_dialog(self):
        names = [n for _, n in POSITIONS]
        n, ok = QInputDialog.getItem(self, "テロップの位置", "置く場所（文字の大きさに合わせて余白を取ります）:", names, 7, False)
        if ok:
            self.telop_position(POSITIONS[names.index(n)][0])

    def telop_position(self, where: str, margin_x: float = 0.05, margin_y: float = 0.07) -> int:
        """選んだテロップを画面の 9 か所のどこかへ（タイトルセーフ相当の余白付き）。"""
        from .render import text_patch
        seq = self.project.sequence
        W, H = seq.width, seq.height
        clips = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        if not clips:
            self._msg("テロップの位置", "テロップ（テキストクリップ）を選んでください。")
            return 0
        self.begin_edit()
        for c in clips:
            arr, x, y = text_patch(self.project, c, 1.0)
            if arr is None:
                continue
            h, w = arr.shape[:2]
            s = float(c.prop("scale", 1.0) or 1.0)
            cx0 = W / 2 + (x + w / 2 - W / 2) * s          # 今の（位置 0 のときの）中心
            cy0 = H / 2 + (y + h / 2 - H / 2) * s
            v, hz = where[0], where[1]
            tx = {"l": W * margin_x + w * s / 2, "c": W / 2, "r": W * (1 - margin_x) - w * s / 2}[hz]
            ty = {"t": H * margin_y + h * s / 2, "m": H / 2, "b": H * (1 - margin_y) - h * s / 2}[v]
            c.props["pos_x"] = round(tx - cx0, 1)
            c.props["pos_y"] = round(ty - cy0, 1)
        self.end_edit(f"テロップを{dict(POSITIONS)[where]}へ（{len(clips)} 個）")
        return len(clips)

    def apply_style_to_all(self, whole_sequence: Optional[bool] = None) -> int:
        """選んだテロップの見た目（スタイル・文字アニメ・位置）を、同じトラック（またはシーケンス全体）のテロップへ。"""
        sel = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        if not sel:
            self._msg("スタイルを一括適用", "お手本にするテロップを 1 つ選んでください。")
            return 0
        src = sel[0]
        if whole_sequence is None:
            items = ["同じトラックのテロップ", "シーケンスのテロップすべて"]
            ch, ok = QInputDialog.getItem(self, "スタイルを一括適用", f"「{str(src.prop('text', ''))[:16]}」の見た目を:",
                                          items, 0, False)
            if not ok:
                return 0
            whole_sequence = ch == items[1]
        seq = self.project.sequence
        tr = self._track_of(src)
        targets = [c for t, c in seq.all_clips() if c.kind == "text" and c is not src and (whole_sequence or t is tr)]
        self.begin_edit()
        for c in targets:
            c.props["style"] = copy.deepcopy(src.prop("style") or {})
            for k in ("text_anim", "text_anim_out", "pos_x", "pos_y", "scale", "effects", "karaoke"):
                if k in src.props:
                    c.props[k] = copy.deepcopy(src.props[k])
                else:
                    c.props.pop(k, None)
        self.end_edit(f"スタイルを一括適用（{len(targets)} 個）")
        return len(targets)

    # 表記統一（例: 「出来る」→「できる」）
    def telop_dict_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("表記統一辞書")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("1 行に「元=直す形」。保存した辞書は次回も使えます。\n例:  出来る=できる　　youtube=YouTube"))
        ed = QPlainTextEdit("\n".join(f"{a}={b}" for a, b in SETTINGS.telop_dict))
        v.addWidget(ed, 1)
        only = QCheckBox("選択したテロップだけ")
        v.addWidget(only)
        bb = QDialogButtonBox()
        bb.addButton("保存して適用", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton("保存だけ", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(lambda: dlg.done(2))
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(420, 360)
        r = dlg.exec()
        if not r:
            return
        pairs = []
        for line in ed.toPlainText().splitlines():
            if "=" in line:
                a, b = line.split("=", 1)
                if a.strip():
                    pairs.append([a.strip(), b.strip()])
        SETTINGS.telop_dict = pairs
        SETTINGS.save()
        if r == 1:
            self.apply_telop_dict(only.isChecked())

    def apply_telop_dict(self, only_selected: bool = False) -> int:
        seq = self.project.sequence
        clips = [c for _, c in self.canvas.selected_clips() if c.kind == "text"] if only_selected else \
            [c for _, c in seq.all_clips() if c.kind == "text"]
        n = 0
        self.begin_edit()
        for c in clips:
            t = str(c.prop("text", ""))
            new = t
            for a, b in SETTINGS.telop_dict:
                new = new.replace(a, b)
            if new != t:
                c.props["text"] = new
                n += 1
        self.end_edit(f"表記統一（{n} 個を直しました）")
        self.status.showMessage(f"表記統一: {n} 個のテロップを直しました", 5000)
        return n

    # テキストから一括配置
    def telops_from_text_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("テキストからテロップを一括配置")
        f = QFormLayout(dlg)
        ed = QPlainTextEdit()
        ed.setPlaceholderText("1 行 = 1 テロップ（空行は無視）。\n「\\n」と書くとテロップの中で改行します。")
        f.addRow(ed)
        mode = QComboBox()
        mode.addItems(["文字数に合わせた長さ（読みやすさ優先）", "すべて同じ長さ", "IN〜OUT に均等に並べる"])
        sec = QDoubleSpinBox()
        sec.setRange(0.3, 30)
        sec.setValue(3.0)
        sec.setSuffix(" 秒")
        cps = QDoubleSpinBox()
        cps.setRange(2, 20)
        cps.setValue(6)
        cps.setSuffix(" 文字/秒")
        gap = QSpinBox()
        gap.setRange(0, 120)
        gap.setSuffix(" フレーム")
        f.addRow("長さの決め方", mode)
        f.addRow("同じ長さのとき", sec)
        f.addRow("読む速さ", cps)
        f.addRow("テロップの間", gap)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        dlg.resize(460, 420)
        if dlg.exec():
            self.telops_from_text(ed.toPlainText(), ("auto", "fixed", "even")[mode.currentIndex()],
                                  sec.value(), cps.value(), gap.value())

    def telops_from_text(self, text: str, mode: str = "auto", sec: float = 3.0, cps: float = 6.0, gap: int = 0) -> int:
        from .ops import _telop_target
        lines = [ln.strip().replace("\\n", "\n") for ln in text.splitlines() if ln.strip()]
        if not lines:
            return 0
        seq = self.project.sequence
        fps = seq.fps
        start = seq.mark_in if seq.mark_in >= 0 else self.record_player.position
        if mode == "even":
            if not (0 <= seq.mark_in < seq.mark_out):
                self._msg("テロップを一括配置", "IN と OUT を設定してください（その間に均等に並べます）。")
                return 0
            span = seq.mark_out - seq.mark_in
            each = max(1, (span - gap * (len(lines) - 1)) // len(lines))
            lens = [each] * len(lines)
        elif mode == "fixed":
            lens = [max(1, int(round(sec * fps)))] * len(lines)
        else:
            lens = [reading_frames(t, fps, cps) for t in lines]
        self.begin_edit()
        tr = _telop_target(seq)
        f = start
        made = []
        for t, n in zip(lines, lens):
            c = M.Clip(M.new_id(), "text", f, n, name="テロップ", props={"text": t, "style": {}})
            M.place_clips(seq, [(tr, c)])
            made.append(c.id)
            f += n + gap
        self.end_edit(f"テキストからテロップ（{len(lines)} 個）")
        self.canvas.selected = set(made)
        self.canvas.selection_changed.emit()
        return len(lines)

    def telop_auto_length(self, cps: float = 6.0, lo: float = 1.2, hi: float = 6.0) -> int:
        """文字数から長さを決め直す（次のテロップには重ねない）。"""
        seq = self.project.sequence
        clips = self._text_targets()
        n = 0
        self.begin_edit()
        for c in clips:
            tr = self._track_of(c)
            nxt = min((o.start for o in tr.clips if o.start > c.start and o is not c), default=10 ** 9)
            want = reading_frames(c.prop("text", ""), seq.fps, cps, lo, hi)
            d = max(1, min(want, nxt - c.start))
            if d != c.duration:
                c.duration = d
                n += 1
        self.end_edit(f"読み時間で長さを調整（{n} 個）")
        return n

    def telop_shift_dialog(self):
        fps = self.project.sequence.fps
        v, ok = QInputDialog.getDouble(self, "テロップをまとめてずらす",
                                       "ずらす秒数（− で前へ。選択が無ければすべてのテロップ）:", 0.5, -600, 600, 2)
        if ok and v:
            self.telop_shift(int(round(v * fps)))

    def telop_shift(self, frames: int) -> int:
        clips = self._text_targets()
        if not clips or not frames:
            return 0
        if min(c.start for c in clips) + frames < 0:
            frames = -min(c.start for c in clips)
        self.begin_edit()
        for c in clips:
            c.start += frames
        for t in self.project.sequence.tracks():
            t.sort()
        self.end_edit(f"テロップを {frames:+d} フレームずらす（{len(clips)} 個）")
        return len(clips)

    def telop_fit_audio(self) -> int:
        """選んだテロップの開始と長さを、いちばん重なっている音声クリップ（セリフ）に合わせる。"""
        seq = self.project.sequence
        clips = [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        if not clips:
            self._msg("音声に合わせる", "テロップを選んでください（重なっているセリフの音声に長さを合わせます）。")
            return 0
        n = 0
        self.begin_edit()
        for c in clips:
            best, ov = None, 0
            for t in seq.audio_tracks:
                if t.muted:
                    continue
                for a in t.clips:
                    o = min(a.end, c.end) - max(a.start, c.start)
                    if o > ov:
                        best, ov = a, o
            if best is not None:
                c.start, c.duration = best.start, best.duration
                n += 1
        for t in seq.tracks():
            t.sort()
        self.end_edit(f"テロップを音声に合わせる（{n} 個）")
        return n

    def telop_speaker_colors(self) -> Dict[str, str]:
        """話者（speaker）ごとに文字色をそろえる。話者設定に色があればそれを使う。"""
        seq = self.project.sequence
        clips = [c for _, c in seq.all_clips() if c.kind == "text" and c.prop("speaker")]
        if not clips:
            self._msg("話者ごとの色", "話者の付いたテロップがありません（セリフパネル・台本から作ったテロップに付きます）。")
            return {}
        conf = {str(s.get("name")): s.get("color") or s.get("subtitle_color") for s in self.project.speakers or []}
        names = []
        for c in clips:
            if c.prop("speaker") not in names:
                names.append(c.prop("speaker"))
        colors = {n: conf.get(n) or SPEAKER_COLORS[i % len(SPEAKER_COLORS)] for i, n in enumerate(names)}
        self.begin_edit()
        for c in clips:
            c.props["style"] = {**(c.prop("style") or {}), "font_color": colors[c.prop("speaker")]}
        self.end_edit(f"話者ごとに色分け（{len(names)} 人）")
        return colors

    def markers_to_telops(self) -> int:
        """マーカーの名前をテロップに（次のマーカーまで。最後は 3 秒）。"""
        from .ops import _telop_target
        seq = self.project.sequence
        ms = sorted((m for m in seq.markers if m.name.strip()), key=lambda m: m.frame)
        if not ms:
            self._msg("マーカー → テロップ", "名前の付いたマーカーがありません。")
            return 0
        self.begin_edit()
        tr = _telop_target(seq)
        for i, m in enumerate(ms):
            end = ms[i + 1].frame if i + 1 < len(ms) else m.frame + int(seq.fps * 3)
            c = M.Clip(M.new_id(), "text", m.frame, max(1, end - m.frame), name="テロップ",
                       props={"text": m.name, "style": {}})
            M.place_clips(seq, [(tr, c)])
        self.end_edit(f"マーカーからテロップ（{len(ms)} 個）")
        return len(ms)

    def telops_to_markers(self) -> int:
        seq = self.project.sequence
        clips = self._text_targets()
        have = {(m.frame, m.name) for m in seq.markers}
        self.begin_edit()
        n = 0
        for c in clips:
            name = str(c.prop("text", "")).replace("\n", " ")[:40]
            if (c.start, name) not in have:
                seq.markers.append(M.Marker(c.start, name, "#4F8FE0"))
                n += 1
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit(f"テロップからマーカー（{n} 個）")
        return n

    # ─────────────────────────── マーカー（一覧パネルのボタンからも）───────────────────────────
    def export_markers_csv(self, path: str = "") -> str:
        seq = self.project.sequence
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "マーカーを保存", f"{seq.name}_markers.csv", "CSV (*.csv)")
        if not path:
            return ""
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["TC", "フレーム", "名前", "コメント", "色"])
            for m in sorted(seq.markers, key=lambda m: m.frame):
                w.writerow([self.tc_str(m.frame), m.frame, m.name, m.comment, m.color])
        self.status.showMessage(f"マーカーを保存しました: {path}", 5000)
        return path

    def copy_marker_chapters(self) -> str:
        """マーカーを YouTube のチャプター（0:00 タイトル）にしてコピー。"""
        from PyQt6.QtWidgets import QApplication
        seq = self.project.sequence
        ms = sorted(seq.markers, key=lambda m: m.frame)
        lines = [] if ms and ms[0].frame == 0 else ["0:00 オープニング"]
        for m in ms:
            s_ = int(m.frame / seq.fps)
            t = f"{s_ // 3600}:{s_ // 60 % 60:02d}:{s_ % 60:02d}" if s_ >= 3600 else f"{s_ // 60}:{s_ % 60:02d}"
            lines.append(f"{t} {m.name or 'チャプター'}")
        text = "\n".join(lines)
        QApplication.clipboard().setText(text)
        self.status.showMessage("YouTube のチャプターをコピーしました（概要欄に貼れます）", 5000)
        return text

    # ─────────────────────────── 編集 ───────────────────────────
    def split_at_markers(self) -> int:
        seq = self.project.sequence
        if not seq.markers:
            self._msg("マーカーで分割", "マーカーがありません。")
            return 0
        tracks = self._selected_tracks()
        self.begin_edit()
        n = sum(M.razor(seq, m.frame, tracks) for m in sorted(seq.markers, key=lambda m: m.frame))
        self.end_edit(f"マーカーの位置で分割（{n} か所）")
        return n

    def goto_gap(self, direction: int) -> Optional[int]:
        seq = self.project.sequence
        f = self.record_player.position
        g = gaps(seq)
        cand = [a for a, _ in g if a > f] if direction > 0 else [a for a, _ in g if a < f]
        if not cand:
            self.status.showMessage("この先に隙間（黒くなる所）はありません" if direction > 0 else "前に隙間はありません", 3000)
            return None
        to = cand[0] if direction > 0 else cand[-1]
        self.seek_record(to)
        b = next(b for a, b in g if a == to)
        self.status.showMessage(f"隙間: {self.tc_str(to)} から {(b - to) / seq.fps:.2f} 秒", 4000)
        return to

    def fill_gaps_black(self, color: str = "#000000") -> int:
        """映像の隙間に黒（カラー）クリップを入れる（V1 の空いている所）。"""
        seq = self.project.sequence
        g = gaps(seq)
        if not g:
            self.status.showMessage("隙間はありません", 3000)
            return 0
        t = seq.video_tracks[0]
        self.begin_edit()
        n = 0
        for a, b in g:
            if t.is_empty(a, b):
                t.clips.append(M.Clip(M.new_id(), "color", a, b - a, name="黒", props={"color": color}))
                n += 1
        t.sort()
        self.end_edit(f"隙間を黒で埋める（{n} か所）")
        return n

    def preflight(self) -> List[Tuple[int, str]]:
        """書き出し前に気をつける所の一覧 [(フレーム, 内容)]。"""
        seq = self.project.sequence
        fps = seq.fps
        out: List[Tuple[int, str]] = []
        for t, c in seq.all_clips():
            if c.media_id:
                m = self.project.media_by_id(c.media_id)
                if m is None or not os.path.exists(m.path):
                    out.append((c.start, f"オフライン素材: {c.name or (m.name if m else '?')}（{t.name}）"))
            if c.kind == "text" and not str(c.prop("text", "")).strip():
                out.append((c.start, f"空のテロップ（{t.name}）"))
            if c.kind == "text":
                n = len("".join(str(c.prop("text", "")).split()))
                if n and n / max(1e-6, c.duration / fps) > 8:
                    out.append((c.start, f"速すぎるテロップ（{n / (c.duration / fps):.1f} 文字/秒）: {str(c.prop('text'))[:16]}"))
            if not c.enabled:
                out.append((c.start, f"無効にしたクリップ: {c.name}（{t.name}）"))
        for t in seq.tracks():
            cl = sorted(t.clips, key=lambda c: c.start)
            for a, b in zip(cl, cl[1:]):
                if a.end > b.start:
                    out.append((b.start, f"クリップが重なっています（{t.name}）: {a.name} / {b.name}"))
            if t.muted and t.clips:
                out.append((t.clips[0].start, f"{t.name} が{'非表示' if t.kind == 'video' else 'ミュート'}です"))
        for a, b in gaps(seq):
            out.append((a, f"映像の隙間（黒） {(b - a) / fps:.2f} 秒"))
        if 0 <= seq.mark_in < seq.mark_out:
            out.append((seq.mark_in, "IN / OUT が付いています（書き出し範囲に注意）"))
        return sorted(out, key=lambda x: x[0])

    def preflight_dialog(self):
        items = self.preflight()
        dlg = QDialog(self)
        dlg.setWindowTitle("書き出し前チェック")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"{len(items)} 件　ダブルクリックでその場所へ" if items else "問題は見つかりませんでした ✓"))
        lw = QListWidget()
        for f, text in items:
            it = QListWidgetItem(f"{self.tc_str(f)}   {text}")
            it.setData(Qt.ItemDataRole.UserRole, f)
            lw.addItem(it)
        lw.itemDoubleClicked.connect(lambda it: self.seek_record(it.data(Qt.ItemDataRole.UserRole)))
        v.addWidget(lw, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(560, 420)
        dlg.setModal(False)
        dlg.show()
        self._preflight_dlg = dlg
        return items

    def _visual_clips(self) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image")]

    def fit_clips(self, how: str) -> int:
        """フィット（全体が入る）/ フィル（画面を埋める・はみ出しは切れる）。"""
        seq = self.project.sequence
        clips = self._visual_clips()
        if not clips:
            self._msg("画面に合わせる", "映像か画像のクリップを選んでください。")
            return 0
        self.begin_edit()
        for c in clips:
            m = self.project.media_by_id(c.media_id)
            w, h = (m.width, m.height) if m and m.width else (seq.width, seq.height)
            fit = min(seq.width / w, seq.height / h)
            fill = max(seq.width / w, seq.height / h)
            c.props["scale"] = 1.0 if how == "fit" else round(fill / fit, 4)
            c.props["pos_x"] = c.props["pos_y"] = 0.0
            c.props.pop("anim", None)
        self.end_edit(("画面にフィット" if how == "fit" else "画面を埋める") + f"（{len(clips)} 個）")
        return len(clips)

    def pip_dialog(self):
        names = ["右上", "左上", "右下", "左下"]
        n, ok = QInputDialog.getItem(self, "ピクチャー・イン・ピクチャー", "置く場所:", names, 0, False)
        if ok:
            self.pip({"右上": "tr", "左上": "tl", "右下": "br", "左下": "bl"}[n])

    def pip(self, corner: str = "tr", size: float = 0.32, margin: float = 0.04) -> int:
        seq = self.project.sequence
        clips = self._visual_clips()
        if not clips:
            self._msg("ピクチャー・イン・ピクチャー", "上のトラックの映像か画像を選んでください。")
            return 0
        W, H = seq.width, seq.height
        self.begin_edit()
        for c in clips:
            m = self.project.media_by_id(c.media_id)
            w, h = (m.width, m.height) if m and m.width else (W, H)
            r = min(W / w, H / h)
            fw, fh = w * r * size, h * r * size
            c.props["scale"] = size
            sx = 1 if corner[1] == "r" else -1
            sy = 1 if corner[0] == "b" else -1
            c.props["pos_x"] = round(sx * (W / 2 - W * margin - fw / 2), 1)
            c.props["pos_y"] = round(sy * (H / 2 - H * margin - fh / 2), 1)
            c.props.pop("anim", None)
        self.end_edit(f"ピクチャー・イン・ピクチャー（{len(clips)} 個）")
        return len(clips)

    def ken_burns(self, zoom: float = 1.15) -> int:
        """ゆっくりズーム（交互にズームイン / アウト・少し横に動く）。"""
        clips = self._visual_clips()
        if not clips:
            self._msg("ケン・バーンズ", "画像か映像のクリップを選んでください。")
            return 0
        seq = self.project.sequence
        self.begin_edit()
        for i, c in enumerate(sorted(clips, key=lambda c: c.start)):
            base = float(c.prop("scale", 1.0) or 1.0)
            a, b = (base, base * zoom) if i % 2 == 0 else (base * zoom, base)
            dx = seq.width * 0.02 * (1 if i % 2 == 0 else -1)
            c.props["anim"] = {"scale": [a, b], "pos_x": [-dx, dx], "_ease": "ease_in_out"}
        self.end_edit(f"ケン・バーンズ（{len(clips)} 個）")
        return len(clips)

    def snapshot_sequence(self, note: str = "") -> M.Sequence:
        """今のシーケンスの版を「スナップショット」ビンに保存（戻したいときはビンから開く）。"""
        src = self.project.sequence
        self.begin_edit()
        sq = M.Sequence.from_dict(copy.deepcopy(dataclasses.asdict(src)))
        sq.id = M.new_id()
        sq.name = f"{src.name} @ {time.strftime('%m/%d %H:%M')}" + (f" {note}" if note else "")
        for _, c in sq.all_clips():
            c.id = M.new_id()
        self.project.sequences.append(sq)
        b = next((b for b in self.project.bins if b.name == "スナップショット"), None)
        if b is None:
            b = M.Bin(M.new_id(), "スナップショット")
            self.project.bins.append(b)
        b.add("sequence", sq.id)
        self.end_edit("スナップショットを保存")
        self.bin.refresh()
        self.status.showMessage(f"「{sq.name}」を保存しました（ビン「スナップショット」）", 5000)
        return sq

    def unused_media(self) -> List[M.MediaItem]:
        used = {c.media_id for s in self.project.sequences for _, c in s.all_clips() if c.media_id}
        used |= {ch.get("image") for ch in self.project.characters or []}
        return [m for m in self.project.media if m.id not in used]

    def unused_media_dialog(self):
        ms = self.unused_media()
        if not ms:
            self._msg("未使用の素材", "どのシーケンスでも使っていない素材はありません。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("未使用の素材")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"どのシーケンスでも使っていない素材 {len(ms)} 件（ファイルは消えません。プロジェクトから外すだけ）"))
        lw = QListWidget()
        for m in ms:
            it = QListWidgetItem(f"{m.name}    {m.path}")
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            it.setData(Qt.ItemDataRole.UserRole, m.id)
            lw.addItem(it)
        v.addWidget(lw, 1)
        bb = QDialogButtonBox()
        bb.addButton("チェックした素材をプロジェクトから外す", QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(620, 420)
        if dlg.exec():
            ids = [lw.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lw.count())
                   if lw.item(i).checkState() == Qt.CheckState.Checked]
            if ids:
                self.remove_media(ids)

    def export_media_list(self, path: str = "") -> str:
        """使用素材リスト（CSV）: 名前・パス・使った回数・使った長さ・クレジット。"""
        seq = self.project.sequence
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "使用素材リスト", f"{seq.name}_素材.csv", "CSV (*.csv)")
        if not path:
            return ""
        use: Dict[str, List[int]] = {}
        for _, c in seq.all_clips():
            if c.media_id:
                u = use.setdefault(c.media_id, [0, 0])
                u[0] += 1
                u[1] += c.duration
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["名前", "種類", "パス", "使った回数", "使った長さ（秒）", "クレジット", "元の場所"])
            for m in self.project.media:
                if m.id in use:
                    w.writerow([m.name, m.kind, m.path, use[m.id][0], f"{use[m.id][1] / seq.fps:.2f}", m.credit,
                                m.original_path])
        self.status.showMessage(f"使用素材リストを保存しました: {path}", 5000)
        return path

    def export_edit_list(self, path: str = "") -> str:
        """編集リスト（テキスト）: 時間順に「TC  トラック  内容」。テロップは本文、素材はファイル名と素材側の TC。"""
        seq = self.project.sequence
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "編集リスト", f"{seq.name}_編集リスト.txt", "テキスト (*.txt)")
        if not path:
            return ""
        rows = []
        for t, c in seq.all_clips():
            if c.kind == "text":
                what = "テロップ: " + str(c.prop("text", "")).replace("\n", " / ")
            elif c.media_id:
                m = self.project.media_by_id(c.media_id)
                what = f"{m.name if m else c.name}  [{frames_to_timecode(c.src_in, seq.fps)}〜]"
            else:
                what = c.name or c.kind
            rows.append((c.start, t.name, f"{self.tc_str(c.start)} - {self.tc_str(c.end)}  {t.name:<4} {what}"))
        for m in seq.markers:
            rows.append((m.frame, "", f"{self.tc_str(m.frame)}              ◆ {m.name} {m.comment}".rstrip()))
        rows.sort(key=lambda r: (r[0], r[1]))
        head = [f"{seq.name}  {seq.width}×{seq.height} {seq.fps:g}fps  長さ {self.tc_str(seq.duration)}", ""]
        Path(path).write_text("\n".join(head + [r[2] for r in rows]) + "\n", encoding="utf-8")
        self.status.showMessage(f"編集リストを保存しました: {path}", 5000)
        return path

    def _move_selected(self, new_starts: Dict[str, int], label: str) -> bool:
        """選んだクリップを新しい位置へ（他のクリップと重なるときはやめる）。"""
        seq = self.project.sequence
        ids = set(new_starts)
        for t in seq.tracks():
            for c in t.clips:
                if c.id in ids:
                    a = new_starts[c.id]
                    if a < 0 or any(o.id not in ids and o.start < a + c.duration and a < o.end for o in t.clips):
                        self._msg(label, "ほかのクリップと重なるので動かせません。")
                        return False
        self.begin_edit()
        for t in seq.tracks():
            for c in t.clips:
                if c.id in ids:
                    c.start = new_starts[c.id]
            t.sort()
        self.end_edit(label)
        return True

    def space_clips_dialog(self):
        fps = self.project.sequence.fps
        v, ok = QInputDialog.getDouble(self, "間隔をそろえる", "クリップの間（秒。0 で詰める）:", 0.0, 0, 60, 2)
        if ok:
            self.space_clips(int(round(v * fps)))

    def space_clips(self, gap: int = 0) -> bool:
        """選んだクリップを、トラックごとに一定の間隔で並べ直す（最初のクリップの位置から）。"""
        seq = self.project.sequence
        new: Dict[str, int] = {}
        by_track: Dict[int, List[M.Clip]] = {}
        for t, c in self.canvas.selected_clips():
            by_track.setdefault(id(t), []).append(c)
        if not by_track:
            self._msg("間隔をそろえる", "クリップを選んでください。")
            return False
        for cl in by_track.values():
            cl.sort(key=lambda c: c.start)
            f = cl[0].start
            for c in cl:
                new[c.id] = f
                f += c.duration + gap
        # リンクした音声も一緒に（映像だけ選んでいても）
        for t, c in list(self.canvas.selected_clips()):
            if c.link:
                for t2, o in seq.linked(c):
                    if o.id not in new:
                        new[o.id] = new[c.id] + (o.start - c.start)
        return self._move_selected(new, "間隔をそろえる")

    def move_to_playhead(self) -> bool:
        sel = [c for _, c in self.canvas.selected_clips()]
        if not sel:
            self._msg("再生ヘッドへ移動", "クリップを選んでください。")
            return False
        d = self.record_player.position - min(c.start for c in sel)
        new = {c.id: c.start + d for c in sel}
        for c in sel:
            if c.link:
                for _t, o in self.project.sequence.linked(c):
                    new.setdefault(o.id, o.start + d)
        return self._move_selected(new, "再生ヘッドへ移動")

    def select_same_media(self) -> int:
        seq = self.project.sequence
        mids = {c.media_id for _, c in self.canvas.selected_clips() if c.media_id}
        if not mids:
            self._msg("同じ素材を選択", "素材のクリップを選んでください。")
            return 0
        self.canvas.selected = {c.id for _, c in seq.all_clips() if c.media_id in mids}
        self.canvas.selection_changed.emit()
        self.canvas.update()
        self.status.showMessage(f"同じ素材のクリップ {len(self.canvas.selected)} 個を選択", 3000)
        return len(self.canvas.selected)

    def select_kind_only(self, kind: str) -> int:
        sel = self.canvas.selected_clips()
        keep = {c.id for t, c in sel if (t.kind == "audio") == (kind == "audio")}
        self.canvas.selected = keep
        self.canvas.selection_changed.emit()
        self.canvas.update()
        return len(keep)

    def replace_media_dialog(self):
        sel = [c for _, c in self.canvas.selected_clips() if c.media_id]
        if not sel:
            self._msg("素材の差し替え", "差し替えたい素材のクリップを選んでください。")
            return
        old = self.project.media_by_id(sel[0].media_id)
        cands = [m for m in self.project.media if m.id != old.id and m.kind == old.kind]
        if not cands:
            self._msg("素材の差し替え", "同じ種類の素材がビンにありません。先に読み込んでください。")
            return
        names = [f"{m.name}   ({m.path})" for m in cands]
        n, ok = QInputDialog.getItem(self, "素材の差し替え", f"「{old.name}」を使っているクリップをすべて:", names, 0, False)
        if ok:
            self.replace_media(old.id, cands[names.index(n)].id)

    def replace_media(self, old_id: str, new_id: str) -> int:
        """old の素材を使っているクリップ（全シーケンス）を new に差し替える（位置・長さ・エフェクトはそのまま）。"""
        new = self.project.media_by_id(new_id)
        if new is None:
            return 0
        n = 0
        self.begin_edit()
        for s in self.project.sequences:
            for _, c in s.all_clips():
                if c.media_id == old_id:
                    c.media_id = new_id
                    c.name = new.name
                    n += 1
        self.end_edit(f"素材を差し替え（{n} 個）")
        return n

    def sequence_stats(self) -> dict:
        seq = self.project.sequence
        fps = seq.fps
        clips = [c for _, c in seq.all_clips()]
        kinds: Dict[str, int] = {}
        for c in clips:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        texts = [c for c in clips if c.kind == "text"]
        st = {"長さ": self.tc_str(seq.duration) + f"（{seq.duration / fps:.1f} 秒）",
              "クリップ": f"{len(clips)} 個（" + "・".join(f"{k} {v}" for k, v in sorted(kinds.items())) + "）",
              "テロップ": f"{len(texts)} 個・{sum(len(''.join(str(c.prop('text', '')).split())) for c in texts)} 文字",
              "使った素材": f"{len({c.media_id for c in clips if c.media_id})} 個",
              "カット数（V1）": f"{len(seq.video_tracks[0].clips) if seq.video_tracks else 0}",
              "平均カット長": f"{(sum(c.duration for c in seq.video_tracks[0].clips) / max(1, len(seq.video_tracks[0].clips)) / fps):.2f} 秒" if seq.video_tracks else "",
              "マーカー": f"{len(seq.markers)} 個",
              "隙間（黒）": f"{len(gaps(seq))} か所",
              "トラック": f"映像 {len(seq.video_tracks)}・音声 {len(seq.audio_tracks)}",
              "形式": f"{seq.width}×{seq.height} {seq.fps:g}fps"}
        if not getattr(self, "_quiet_stats", False):
            self._msg("シーケンス情報", f"<b>{seq.name}</b><br>" + "<br>".join(f"{k}: {v}" for k, v in st.items()))
        return st

    def in_out_to_sequence(self) -> Optional[M.Sequence]:
        """IN〜OUT の部分だけを新しいシーケンスにする（ハイライト・ショート・確認用）。"""
        src = self.project.sequence
        a, b = src.mark_in, src.mark_out
        if not (0 <= a < b):
            self._msg("IN〜OUT を新しいシーケンスに", "IN と OUT を設定してください。")
            return None
        self.begin_edit()
        sq = M.Sequence.from_dict(copy.deepcopy(dataclasses.asdict(src)))
        sq.id = M.new_id()
        sq.name = f"{src.name}（{self.tc_str(a)}〜）"
        sq.mark_in = sq.mark_out = -1
        for t in sq.tracks():
            keep = []
            for c in t.clips:
                if c.end <= a or c.start >= b:
                    continue
                c.id = M.new_id()
                if c.start < a:
                    cut = a - c.start
                    if c.kind in ("video", "audio"):
                        sp = float(c.prop("speed", 1.0) or 1.0)
                        c.src_in += int(round(cut * abs(sp))) if sp > 0 else 0
                    c.start, c.duration = a, c.duration - cut
                if c.end > b:
                    c.duration = b - c.start
                c.start -= a
                keep.append(c)
            t.clips = keep
        sq.markers = [dataclasses.replace(m, frame=m.frame - a) for m in sq.markers if a <= m.frame < b]
        sq.playhead = 0
        self.project.sequences.append(sq)
        (self.project.bin_of(src.id) or self.project.bins[0]).add("sequence", sq.id)
        self.end_edit("IN〜OUT を新しいシーケンスに")
        self.bin.refresh()
        self.status.showMessage(f"「{sq.name}」を作りました（ビンからダブルクリックで開けます）", 5000)
        return sq
