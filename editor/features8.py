"""編集の便利機能（MainWindow のミックスイン）。

ミックスダウン（映像 / 音声）・コンタクトシート・ショットリスト（HTML）・ASS / WebVTT 字幕・テロップのフォントを一括・
はみ出しを直す・三分割ガイド・フレームをクリップボードへ・同じ色のマーカーへ・クリップ名を連番に・空のトラックを削除・
種類で選択・素材を新しいビンに・モノラル / 左右入れ替え・2 つのクリップを入れ替え・シーケンスの形式を変える・
確認用に書き出してサーバーへ・クリップのロック・撮影順に並べる・マーカーでシーケンスを分ける・シーケンスをつなげる
"""
from __future__ import annotations

import copy
import dataclasses
import html
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, \
    QInputDialog, QListWidget, QListWidgetItem, QMessageBox, QSpinBox, QAbstractItemView

from . import model as M
from .settings import SETTINGS


class _T:
    cancelled = False

    class progress:
        @staticmethod
        def emit(*_a):
            pass


def _srt_like(sec: float, sep: str = ".") -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}{sep}{ms % 1000:03d}"


class Features8Mixin:
    def _feature8_commands(self) -> dict:
        return {
            "mixdown_video": self.mixdown_video, "mixdown_audio": self.mixdown_audio,
            "contact_sheet": self.contact_sheet_dialog, "shot_list": self.shot_list_dialog,
            "export_ass": lambda: self._sub_export("ass"), "export_vtt": lambda: self._sub_export("vtt"),
            "telop_font_all": self.telop_font_all_dialog, "telop_fit_width": self.telop_fit_width,
            "guides_grid": self.toggle_grid, "copy_frame": self.copy_frame,
            "next_marker_color": lambda: self.goto_marker_color(1), "prev_marker_color": lambda: self.goto_marker_color(-1),
            "rename_clips": self.rename_clips_dialog, "remove_empty_tracks": self.remove_empty_tracks,
            "select_kind": self.select_kind_dialog, "media_to_bin": self.media_to_new_bin,
            "audio_mono": lambda: self.audio_channels("mono"), "audio_swap": lambda: self.audio_channels("swap"),
            "swap_clips": self.swap_clips, "conform_sequence": self.conform_sequence_dialog,
            "review_upload": self.review_upload, "lock_clips": self.lock_clips,
            "place_by_date": self.place_by_date, "split_by_markers": self.split_sequence_by_markers,
            "join_sequences": self.join_sequences_dialog,
        }

    def _build_feature8_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "便利")
        for c in ("mixdown_video", "mixdown_audio", None, "contact_sheet", "shot_list", "export_ass", "export_vtt",
                  "review_upload", None, "telop_font_all", "telop_fit_width", None, "guides_grid", "copy_frame",
                  "next_marker_color", "prev_marker_color", None, "rename_clips", "lock_clips", "swap_clips",
                  "select_kind", "media_to_bin", "audio_mono", "audio_swap", "place_by_date", None,
                  "remove_empty_tracks", "conform_sequence", "split_by_markers", "join_sequences"):
            m.addSeparator() if c is None else self._a(m, c)

    def _sel8(self, kinds=None) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if kinds is None or c.kind in kinds]

    def _rng8(self) -> Tuple[int, int]:
        seq = self.project.sequence
        return (seq.mark_in, seq.mark_out) if 0 <= seq.mark_in < seq.mark_out else (0, seq.duration)

    def _out_dir8(self, sub: str) -> Path:
        d = (Path(self.project_path).parent / sub) if self.project_path else Path(SETTINGS.cache_dir) / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ─────────────── ミックスダウン（Avid のビデオ / オーディオミックスダウン）───────────────
    def mixdown_video(self, wait: bool = True) -> Optional[M.Clip]:
        """IN〜OUT（無ければ全体）の見た目を 1 本の映像（ProRes 422）にして、一番上のトラックに置く。重い所を軽くする。"""
        from . import exportcore as EC
        from . import media as MD
        seq = self.project.sequence
        a, b = self._rng8()
        if b <= a:
            return None
        out = str(self._out_dir8("Mixdown") / f"{seq.name}_mixdown_{M.new_id()[:6]}.mov")
        proj = M.Project.from_json(self.project.to_json())
        EC.render_to_file(proj, a, b, out, "prores_422", audio=False)
        info = MD.probe(out)
        info.id = M.new_id()
        info.name = f"ミックスダウン {Path(out).stem[-6:]}"
        self.begin_edit()
        self.project.media.append(info)
        self.project.bin_by_id(self.named_bin("ミックスダウン")).add("media", info.id)
        t = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(t)
        c = M.Clip(M.new_id(), "video", a, b - a, 0, info.id, info.name)
        t.clips.append(c)
        self.end_edit("ビデオミックスダウン")
        self.bin.refresh()
        return c

    def mixdown_audio(self) -> Optional[M.Clip]:
        """IN〜OUT の音をまとめて 1 本の WAV にして、新しい音声トラックに置く（元のトラックはミュート）。"""
        from . import media as MD
        from .smartrender import write_audio
        seq = self.project.sequence
        a, b = self._rng8()
        if b <= a:
            return None
        out = str(self._out_dir8("Mixdown") / f"{seq.name}_audio_{M.new_id()[:6]}.wav")
        write_audio(self.project, seq, a, b, out)
        info = MD.probe(out)
        info.id = M.new_id()
        info.name = f"オーディオミックスダウン {Path(out).stem[-6:]}"
        MD.build_audio_cache(out)
        self.begin_edit()
        self.project.media.append(info)
        self.project.bin_by_id(self.named_bin("ミックスダウン")).add("media", info.id)
        for t in seq.audio_tracks:
            t.muted = True
        t = M.Track("audio", f"A{len(seq.audio_tracks) + 1}")
        seq.audio_tracks.append(t)
        c = M.Clip(M.new_id(), "audio", a, b - a, 0, info.id, info.name)
        t.clips.append(c)
        self.end_edit("オーディオミックスダウン（元の音声トラックはミュート）")
        self.bin.refresh()
        return c

    # ─────────────── 一覧の書き出し ───────────────
    def _cuts(self) -> List[M.Clip]:
        seq = self.project.sequence
        v1 = sorted((c for c in seq.video_tracks[0].clips if c.kind in ("video", "image", "nest")), key=lambda c: c.start) \
            if seq.video_tracks else []
        return v1 or sorted((c for _, c in seq.all_clips() if c.kind in ("video", "image")), key=lambda c: c.start)

    def _thumb(self, f: int, width: int = 320):
        from PIL import Image
        from .media import DecoderPool
        from .render import render_frame_np
        seq = self.project.sequence
        return Image.fromarray(render_frame_np(seq, f, self.project, DecoderPool(), width / max(1, seq.width)))

    def contact_sheet_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "コンタクトシート", f"{self.project.sequence.name}_contact.png", "PNG (*.png)")
        if p:
            self.contact_sheet(p)

    def contact_sheet(self, path: str, cols: int = 5, width: int = 320) -> str:
        """カットごとの絵を 1 枚に並べる（TC と名前付き）。"""
        from PIL import Image, ImageDraw
        from .render import _offline_font
        cuts = self._cuts()
        if not cuts:
            raise ValueError("カットがありません")
        thumbs = [self._thumb(c.start + c.duration // 2, width) for c in cuts]
        th = thumbs[0].height
        rows = (len(cuts) + cols - 1) // cols
        cell_h = th + 34
        sheet = Image.new("RGB", (cols * (width + 10) + 10, rows * (cell_h + 10) + 50), (24, 24, 28))
        d = ImageDraw.Draw(sheet)
        f, fs = _offline_font(22), _offline_font(14, bold=False)
        d.text((12, 12), f"{self.project.sequence.name}  （{len(cuts)} カット）", font=f, fill=(255, 255, 255))
        for i, (c, im) in enumerate(zip(cuts, thumbs)):
            x, y = 10 + (i % cols) * (width + 10), 50 + (i // cols) * (cell_h + 10)
            sheet.paste(im, (x, y))
            d.text((x + 2, y + th + 4), f"{i + 1}  {self.tc_str(c.start)}  {(c.name or '')[:22]}", font=fs, fill=(220, 220, 220))
        sheet.save(path)
        return path

    def shot_list_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "ショットリスト", f"{self.project.sequence.name}_shots.html", "HTML (*.html)")
        if p:
            self.shot_list(p)

    def shot_list(self, path: str) -> str:
        """ショットリスト（HTML・サムネイル付き）。サムネイルは同じ名前のフォルダに置く。"""
        seq = self.project.sequence
        cuts = self._cuts()
        img_dir = Path(path).with_suffix("")
        img_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, c in enumerate(cuts, 1):
            fn = f"shot_{i:03d}.jpg"
            self._thumb(c.start + c.duration // 2, 240).save(img_dir / fn, quality=85)
            m = self.project.media_by_id(c.media_id) if c.media_id else None
            texts = [str(t.prop("text", "")) for _tr, t in seq.all_clips() if t.kind == "text" and t.start < c.end and t.end > c.start]
            rows.append(f"<tr><td>{i}</td><td><img src='{img_dir.name}/{fn}'></td><td>{self.tc_str(c.start)}<br>{self.tc_str(c.end)}</td>"
                        f"<td>{c.duration / seq.fps:.2f} 秒</td><td>{html.escape(m.name if m else c.name)}"
                        f"<div class=d>{html.escape(m.path if m else '')}</div></td>"
                        f"<td>{'<br>'.join(html.escape(x) for x in texts[:4])}</td></tr>")
        doc = f"""<!doctype html><meta charset="utf-8"><title>{html.escape(seq.name)} ショットリスト</title>
<style>body{{font-family:"Yu Gothic UI",sans-serif;margin:20px}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccc;padding:6px;vertical-align:top;font-size:13px}}img{{width:240px}}.d{{color:#888;font-size:11px}}</style>
<h2>{html.escape(seq.name)}（{len(cuts)} カット・{self.tc_str(seq.duration)}）</h2>
<table><tr><th>#</th><th>絵</th><th>TC</th><th>長さ</th><th>素材</th><th>テロップ</th></tr>{''.join(rows)}</table>"""
        Path(path).write_text(doc, encoding="utf-8")
        return path

    def _sub_export(self, kind: str, path: str = ""):
        seq = self.project.sequence
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, "字幕を書き出し", f"{seq.name}.{kind}",
                                                  "ASS (*.ass)" if kind == "ass" else "WebVTT (*.vtt)")
        if not path:
            return None
        return self.export_ass(path) if kind == "ass" else self.export_vtt(path)

    def _subs(self) -> List[M.Clip]:
        seq = self.project.sequence
        return sorted((c for _, c in seq.all_clips() if c.kind == "text" and c.enabled and str(c.prop("text", "")).strip()),
                      key=lambda c: c.start)

    def export_vtt(self, path: str) -> str:
        fps = self.project.sequence.fps
        out = ["WEBVTT", ""]
        for i, c in enumerate(self._subs(), 1):
            out += [str(i), f"{_srt_like(c.start / fps)} --> {_srt_like(c.end / fps)}", str(c.prop("text", "")), ""]
        Path(path).write_text("\n".join(out), encoding="utf-8")
        return path

    def export_ass(self, path: str) -> str:
        """ASS（スタイル付き字幕。色・縁取り・位置を残す）。"""
        seq = self.project.sequence
        design = self.project.design or {}

        def col(hexs, alpha=0):
            h = (hexs or "#FFFFFF").lstrip("#")
            r, g, b = h[0:2], h[2:4], h[4:6]
            return f"&H{alpha:02X}{b}{g}{r}".upper()

        def ts(f):
            s = f / seq.fps
            return f"{int(s // 3600)}:{int(s // 60 % 60):02d}:{int(s % 60):02d}.{int(round(s % 1 * 100)) % 100:02d}"
        styles, lines = {}, []
        for c in self._subs():
            st = {**design, **(c.prop("style") or {})}
            key = (st.get("font_color", "#FFFFFF"), st.get("stroke_color", "#000000"), int(st.get("font_size", 60)),
                   st.get("position_v", "bottom"), st.get("position_h", "center"), bool(st.get("bg_enabled")))
            if key not in styles:
                styles[key] = f"S{len(styles) + 1}"
            text = str(c.prop("text", "")).replace("\n", "\\N")
            lines.append(f"Dialogue: 0,{ts(c.start)},{ts(c.end)},{styles[key]},,0,0,0,,{text}")
        an = {("bottom", "left"): 1, ("bottom", "center"): 2, ("bottom", "right"): 3, ("middle", "left"): 4,
              ("middle", "center"): 5, ("middle", "right"): 6, ("top", "left"): 7, ("top", "center"): 8, ("top", "right"): 9}
        sty = []
        for (fc, sc, fs, pv, ph, box), name in styles.items():
            sty.append(f"Style: {name},Yu Gothic UI,{fs},{col(fc)},{col(fc)},{col(sc)},&H80000000,-1,0,0,0,100,100,0,0,"
                       f"{3 if box else 1},{4 if not box else 2},1,{an.get((pv, ph), 2)},40,40,60,1")
        doc = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {seq.width}", f"PlayResY: {seq.height}", "",
               "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
               "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
               "MarginL, MarginR, MarginV, Encoding", *sty, "", "[Events]",
               "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *lines]
        Path(path).write_text("\n".join(doc) + "\n", encoding="utf-8-sig")
        return path

    # ─────────────── テロップ ───────────────
    def telop_font_all_dialog(self):
        p, _ = QFileDialog.getOpenFileName(self, "フォント", "C:/Windows/Fonts", "フォント (*.ttf *.otf *.ttc)")
        if p:
            self.telop_font_all(p)

    def telop_font_all(self, font_path: str) -> int:
        clips = self._text_targets()
        self.begin_edit()
        for c in clips:
            c.props["style"] = {**(c.prop("style") or {}), "font_path": font_path}
        self.end_edit(f"テロップのフォントを一括変更（{len(clips)} 個）")
        return len(clips)

    def telop_fit_width(self, margin: float = 0.05, max_lines: int = 2) -> int:
        """画面からはみ出す・折り返して 3 行以上になるテロップを、収まる大きさに縮める（文字の大きさを下げる）。"""
        from .render import text_patch
        seq = self.project.sequence
        n = 0
        self.begin_edit()
        for c in self._text_targets():
            lines = max(max_lines, str(c.prop("text", "")).count("\n") + 1)
            changed = False
            for _ in range(15):
                arr, _x, _y = text_patch(self.project, c, 1.0)
                if arr is None:
                    break
                st = dict(c.prop("style") or {})
                cur = int(st.get("font_size", (self.project.design or {}).get("font_size", 60)))
                too_wide = arr.shape[1] > seq.width * (1 - 2 * margin)
                too_tall = arr.shape[0] > lines * cur * 1.45 or arr.shape[0] > seq.height * (1 - 2 * margin)
                if not (too_wide or too_tall) or cur <= 12:
                    break
                st["font_size"] = max(12, int(cur * 0.9))
                c.props["style"] = st
                changed = True
            n += changed
        self.end_edit(f"はみ出すテロップを縮めました（{n} 個）")
        return n

    # ─────────────── モニター・マーカー ───────────────
    def toggle_grid(self) -> bool:
        on = not self.record_mon.view.grid
        for mon in (self.record_mon, self.source_mon):
            mon.view.grid = on
            mon.view.update()
        return on

    def copy_frame(self):
        """今のフレームを画像としてクリップボードへ（チャットや資料にそのまま貼れる）。"""
        from PyQt6.QtGui import QImage
        im = self._thumb(self.record_player.position, self.project.sequence.width)
        arr = np.ascontiguousarray(np.asarray(im.convert("RGB")))
        img = QImage(arr.data, arr.shape[1], arr.shape[0], arr.shape[1] * 3, QImage.Format.Format_RGB888).copy()
        QApplication.clipboard().setImage(img)
        self.status.showMessage("今のフレームをクリップボードにコピーしました", 3000)
        return img

    def goto_marker_color(self, direction: int) -> Optional[int]:
        """今いるマーカー（無ければ直前のマーカー）と同じ色の、次 / 前のマーカーへ。"""
        seq = self.project.sequence
        f = self.record_player.position
        ms = sorted(seq.markers, key=lambda m: m.frame)
        if not ms:
            return None
        here = next((m for m in ms if m.frame == f), None) or next((m for m in reversed(ms) if m.frame <= f), ms[0])
        same = [m.frame for m in ms if m.color == here.color]
        cand = [x for x in same if (x > f if direction > 0 else x < f)]
        if not cand:
            return None
        to = cand[0] if direction > 0 else cand[-1]
        self.seek_record(to)
        return to

    # ─────────────── クリップ ───────────────
    def rename_clips_dialog(self):
        base, ok = QInputDialog.getText(self, "クリップ名を連番に", "名前（# が番号になります）:", text="カット ##")
        if ok and base.strip():
            self.rename_clips(base.strip())

    def rename_clips(self, pattern: str = "カット ##") -> int:
        clips = sorted(self._sel8() or [c for _, c in self.project.sequence.all_clips() if c.kind in ("video", "image")],
                       key=lambda c: c.start)
        k = max(1, pattern.count("#"))
        self.begin_edit()
        for i, c in enumerate(clips, 1):
            c.name = pattern.replace("#" * k, f"{i:0{k}d}") if "#" in pattern else f"{pattern} {i}"
        self.end_edit(f"クリップ名を連番に（{len(clips)} 個）")
        return len(clips)

    def lock_clips(self) -> int:
        """選んだクリップの位置をロック / 解除（ロック中はドラッグで動かない）。"""
        clips = self._sel8()
        on = not all(c.props.get("pos_locked") for c in clips)
        self.begin_edit()
        for c in clips:
            if on:
                c.props["pos_locked"] = True
            else:
                c.props.pop("pos_locked", None)
        self.end_edit(("ロック" if on else "ロックを解除") + f"（{len(clips)} 個）")
        return len(clips)

    def remove_empty_tracks(self) -> int:
        seq = self.project.sequence
        self.begin_edit()
        before = len(seq.video_tracks) + len(seq.audio_tracks)
        seq.video_tracks = [t for t in seq.video_tracks if t.clips] or seq.video_tracks[:1]
        seq.audio_tracks = [t for t in seq.audio_tracks if t.clips] or seq.audio_tracks[:1]
        for i, t in enumerate(seq.video_tracks):
            if t.name.startswith("V") and t.name[1:].isdigit():
                t.name = f"V{i + 1}"
        for i, t in enumerate(seq.audio_tracks):
            if t.name.startswith("A") and t.name[1:].isdigit():
                t.name = f"A{i + 1}"
        n = before - len(seq.video_tracks) - len(seq.audio_tracks)
        self.end_edit(f"空のトラックを削除（{n} 本）")
        return n

    KINDS = [("text", "テロップ"), ("image", "画像"), ("video", "映像"), ("audio", "音声"), ("shape", "図形"),
             ("character", "立ち絵"), ("nest", "ネスト")]

    def select_kind_dialog(self):
        names = [lab for _k, lab in self.KINDS]
        n, ok = QInputDialog.getItem(self, "種類で選択", "選ぶもの:", names, 0, False)
        if ok:
            self.select_kind(self.KINDS[names.index(n)][0])

    def select_kind(self, kind: str) -> int:
        ids = {c.id for _, c in self.project.sequence.all_clips() if c.kind == kind}
        self.canvas.selected = ids
        self.canvas.selection_changed.emit()
        self.canvas.update()
        return len(ids)

    def media_to_new_bin(self, name: str = "") -> Optional[M.Bin]:
        mids = []
        for c in self._sel8():
            if c.media_id and c.media_id not in mids:
                mids.append(c.media_id)
        if not mids:
            return None
        name = name or f"{self.project.sequence.name} の素材"
        self.begin_edit()
        b = M.Bin(M.new_id(), name)
        for i in mids:
            b.add("media", i)
        self.project.bins.append(b)
        self.end_edit(f"素材を新しいビンに（{len(mids)} 個）")
        self.bin.refresh()
        return b

    def audio_channels(self, how: str) -> int:
        """選んだ音声をモノラル（左右を混ぜる）に / 左右を入れ替える。"""
        clips = self._sel8(("audio", "nest"))
        key = "mono" if how == "mono" else "swap_lr"
        on = not all(c.props.get(key) for c in clips)
        self.begin_edit()
        for c in clips:
            if on:
                c.props[key] = True
            else:
                c.props.pop(key, None)
        self.end_edit(("モノラルに" if how == "mono" else "左右を入れ替え") + (" " if on else "（戻す）") + f"（{len(clips)} 個）")
        return len(clips)

    def swap_clips(self) -> bool:
        """同じトラックの 2 つのクリップの位置を入れ替える（間のクリップはずらす）。"""
        sel = list(self.canvas.selected_clips())
        if len(sel) != 2 or sel[0][0] is not sel[1][0]:
            QMessageBox.information(self, "入れ替え", "同じトラックのクリップを 2 つ選んでください。")
            return False
        t = sel[0][0]
        a, b = sorted((sel[0][1], sel[1][1]), key=lambda c: c.start)
        self.begin_edit()
        da, db = a.duration, b.duration
        a_start = a.start
        delta = db - da
        for c in t.clips:
            if a.end <= c.start < b.start:
                c.start += delta
        b.start, a.start = a_start, b.start + delta
        t.sort()
        self.end_edit("クリップを入れ替え")
        return True

    def place_by_date(self) -> int:
        """ビンで選んだ素材を、ファイルの日時（撮影順）に並べてタイムラインの最後に置く。"""
        ids = self.bin.selected_ids()
        ms = [m for m in self.project.media if m.id in ids and m.kind in ("video", "image", "audio")]
        if not ms:
            QMessageBox.information(self, "撮影順に並べる", "ビンで素材を選んでください。")
            return 0
        ms.sort(key=lambda m: (m.mtime or (os.path.getmtime(m.path) if os.path.exists(m.path) else 0), m.name))
        seq = self.project.sequence
        f = seq.duration
        self.begin_edit()
        for m in ms:
            d = self.media_frames(m)
            tv, ta = self._target_tracks()
            pl = self._clips_for_media(m, f, 0, d, tv, ta)
            M.place_clips(seq, pl)
            f += d
        self.end_edit(f"撮影順に並べる（{len(ms)} 個）")
        return len(ms)

    # ─────────────── シーケンス ───────────────
    def conform_sequence_dialog(self):
        seq = self.project.sequence
        dlg = QDialog(self)
        dlg.setWindowTitle("シーケンスの形式を変える")
        f = QFormLayout(dlg)
        w, h = QSpinBox(), QSpinBox()
        for s, v in ((w, seq.width), (h, seq.height)):
            s.setRange(16, 16384)
            s.setValue(v)
        fps = QComboBox()
        for v in (23.976, 24, 25, 29.97, 30, 50, 59.94, 60):
            fps.addItem(f"{v:g} fps", v)
        fps.setCurrentIndex(max(0, fps.findData(seq.fps)))
        f.addRow("幅", w)
        f.addRow("高さ", h)
        f.addRow("フレームレート", fps)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.conform_sequence(w.value(), h.value(), fps.currentData())

    def conform_sequence(self, width: int, height: int, fps: float) -> M.Sequence:
        """今のシーケンスの解像度・fps を変える（クリップの位置・長さ・キーフレームを秒で換算、位置の px は大きさの比で）。"""
        seq = self.project.sequence
        k = fps / seq.fps
        sx = width / seq.width
        conv = lambda f: int(round(f * k))
        self.begin_edit()
        for _t, c in seq.all_clips():
            end = conv(c.end)
            c.start = conv(c.start)
            c.duration = max(1, end - c.start)
            c.src_in = conv(c.src_in)
            for key in ("fade_in", "fade_out", "dissolve"):
                if c.props.get(key):
                    c.props[key] = conv(c.props[key])
            for key in ("pos_x", "pos_y"):
                if c.props.get(key):
                    c.props[key] = round(float(c.props[key]) * sx, 2)
            ks = c.props.get("keys") or {}
            for prop, lst in ks.items():
                for kk in lst:
                    kk[0] = conv(kk[0])
                    if prop in ("pos_x", "pos_y") and isinstance(kk[1], (int, float)):
                        kk[1] = round(kk[1] * sx, 2)
        for mk in seq.markers:
            mk.frame = conv(mk.frame)
        seq.mark_in = conv(seq.mark_in) if seq.mark_in >= 0 else -1
        seq.mark_out = conv(seq.mark_out) if seq.mark_out >= 0 else -1
        seq.width, seq.height, seq.fps = width, height, fps
        self.end_edit(f"シーケンスの形式を {width}×{height} {fps:g}fps に")
        return seq

    def split_sequence_by_markers(self) -> List[M.Sequence]:
        """マーカーごとに区切って、別々のシーケンスにする（章ごとに書き出す・ショート用など）。"""
        seq = self.project.sequence
        cuts = sorted({m.frame for m in seq.markers if 0 < m.frame < seq.duration})
        if not cuts:
            QMessageBox.information(self, "マーカーで分ける", "区切りにするマーカーがありません。")
            return []
        names = {m.frame: m.name for m in seq.markers}
        bounds = [0] + cuts + [seq.duration]
        made = []
        cur_in, cur_out = seq.mark_in, seq.mark_out
        for i, (a, b) in enumerate(zip(bounds, bounds[1:]), 1):
            seq.mark_in, seq.mark_out = a, b
            ns = self.in_out_to_sequence()
            if ns is not None:
                ns.name = f"{seq.name} - {i:02d} {names.get(a, '')}".rstrip()
                made.append(ns)
        seq.mark_in, seq.mark_out = cur_in, cur_out
        self.bin.refresh()
        self.status.showMessage(f"{len(made)} 本のシーケンスに分けました（ビンにあります）", 5000)
        return made

    def join_sequences_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("シーケンスをつなげる")
        v = QFormLayout(dlg)
        lw = QListWidget()
        lw.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        lw.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        for s in self.project.sequences:
            it = QListWidgetItem(s.name)
            it.setData(Qt.ItemDataRole.UserRole, s.id)
            lw.addItem(it)
        v.addRow("つなげる順に選ぶ（ドラッグで並べ替え）", lw)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addRow(bb)
        if dlg.exec():
            ids = [lw.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lw.count()) if lw.item(i).isSelected()]
            if len(ids) >= 2:
                self.join_sequences(ids)

    def join_sequences(self, ids: List[str], name: str = "") -> Optional[M.Sequence]:
        """シーケンスを順につなげて 1 本に（トラックは番号でそろえる・マーカーも）。"""
        seqs = [self.project.sequence_by_id(i) for i in ids]
        seqs = [s for s in seqs if s is not None]
        if len(seqs) < 2:
            return None
        base = seqs[0]
        self.begin_edit()
        ns = M.Sequence.create(name=name or " + ".join(s.name for s in seqs)[:60], fps=base.fps, width=base.width,
                               height=base.height, start_tc=base.start_tc)
        nv = max(len(s.video_tracks) for s in seqs)
        na = max(len(s.audio_tracks) for s in seqs)
        ns.video_tracks = [M.Track("video", f"V{i + 1}", selected=(i == 0)) for i in range(nv)]
        ns.audio_tracks = [M.Track("audio", f"A{i + 1}", selected=(i < 2)) for i in range(na)]
        off = 0
        for s in seqs:
            links: Dict[str, str] = {}
            for src, dst in ((s.video_tracks, ns.video_tracks), (s.audio_tracks, ns.audio_tracks)):
                for i, t in enumerate(src):
                    for c in t.clips:
                        nl = links.setdefault(c.link, M.new_id()) if c.link else ""
                        dst[i].clips.append(c.copy(id=M.new_id(), start=c.start + off, link=nl))
            ns.markers += [dataclasses.replace(m, frame=m.frame + off) for m in s.markers]
            off += s.duration
        for t in ns.tracks():
            t.sort()
        self.project.sequences.append(ns)
        (self.project.bin_of(base.id) or self.project.bins[0]).add("sequence", ns.id)
        self.end_edit(f"シーケンスをつなげる（{len(seqs)} 本）")
        self.bin.refresh()
        return ns

    # ─────────────── 確認用に書き出してサーバーへ ───────────────
    def review_upload(self, wait: bool = False):
        """確認用（720p・H.264・タイムコード入り）に書き出して、アセットサーバーに送り、ブラウザで開く URL をコピー。"""
        url = (SETTINGS.asset_server or "").rstrip("/")
        if not url:
            QMessageBox.information(self, "確認用に送る", "アセットサーバーの URL を、アセットパネルの「サーバー」タブで設定してください。")
            return None
        seq = self.project.sequence
        a, b = self._rng8()
        proj = M.Project.from_json(self.project.to_json())
        ps = proj.sequence
        tc_track = M.Track("video", "TC", selected=False)
        tc_track.clips.append(M.Clip(M.new_id(), "shape", 0, max(1, ps.duration), name="TC",
                                     props={"shape": "timecode", "color": "#FFFFFF", "rect": [0, 91, 100, 6],
                                            "fill_opacity": 0.6, "tc_label": SETTINGS.user_name}))
        ps.video_tracks.append(tc_track)
        sc = min(1.0, 1280 / max(1, ps.width))
        if sc < 1.0:
            ps.width, ps.height = int(ps.width * sc) & ~1, int(ps.height * sc) & ~1
            for _t, c in ps.all_clips():
                for key in ("pos_x", "pos_y"):
                    if c.props.get(key):
                        c.props[key] = float(c.props[key]) * sc
        out = str(self._out_dir8("Review") / f"{seq.name}_review_{M.new_id()[:4]}.mp4")

        def work(task, *_a):
            from . import exportcore as EC
            from .farm import _put_file
            import urllib.parse
            EC.render_to_file(proj, a, b, out, "h264", quality=26,
                              progress=lambda i, n: task.progress.emit(i, n, "書き出し中"))
            task.progress.emit(0, 0, "サーバーに送っています")
            import http.client
            u = urllib.parse.urlparse(url)
            q = f"/api/upload?name={urllib.parse.quote(Path(out).name)}&folder={urllib.parse.quote('確認用')}"
            size = os.path.getsize(out)
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
            conn.putrequest("POST", q)
            conn.putheader("Content-Length", str(size))
            conn.putheader("X-TeloCut", "1")
            conn.endheaders()
            with open(out, "rb") as fp:
                while True:
                    buf = fp.read(1 << 20)
                    if not buf:
                        break
                    conn.send(buf)
            r = conn.getresponse()
            import json as _j
            d = _j.loads(r.read() or b"{}")
            conn.close()
            _ = _put_file
            return d

        def done(d):
            if not d or not d.get("id"):
                QMessageBox.warning(self, "確認用に送る", "送れませんでした。")
                return None
            link = f"{url}/#v=search&id={d['id']}"
            QApplication.clipboard().setText(link)
            self.status.showMessage(f"確認用を送りました。URL をコピーしました: {link}", 10000)
            return link
        if wait:
            return done(work(_T()))
        self._run_task("確認用に書き出してサーバーへ", work, on_done=done)
        return None
