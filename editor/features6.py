"""ほかの編集ソフトにある機能（MainWindow のミックスイン）。

スピードランプ（Premiere / CapCut の速度カーブ）・クリップの登場 / 退場アニメ（CapCut）・ネスト（Premiere / AE の
プリコンポーズ）・ファイルの収集（AE の Collect Files / Premiere のプロジェクトマネージャー）・シーケンスの比較
（Resolve の Timeline Compare）・ラウドネスをそろえる（Premiere のエッセンシャルサウンド）・テイク（FCP のオーディション）・
再生ヘッドのクリップを選ぶ（Premiere の D）・すぐ後ろに複製（Alt ドラッグ）
"""
from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                             QVBoxLayout)

from . import model as M

RAMPS = [("モンタージュ（速い → スロー → 速い）", [(0, 3.0), (0.35, 3.0), (0.45, 0.4), (0.6, 0.4), (0.7, 3.0), (1, 3.0)]),
         ("ヒーロー（途中でスロー）", [(0, 1.0), (0.4, 1.0), (0.5, 0.25), (0.75, 0.25), (0.85, 1.0), (1, 1.0)]),
         ("バレット（一瞬止まる）", [(0, 1.5), (0.45, 1.5), (0.5, 0.05), (0.6, 0.05), (0.65, 1.5), (1, 1.5)]),
         ("だんだん速く", [(0, 0.5), (1, 3.0)]), ("だんだん遅く", [(0, 3.0), (1, 0.5)]),
         ("ジャンプ（速い → 普通）", [(0, 4.0), (0.3, 4.0), (0.4, 1.0), (1, 1.0)])]


class Features6Mixin:
    def _feature6_commands(self) -> dict:
        return {
            "speed_ramp": self.speed_ramp_dialog, "speed_ramp_clear": self.clear_speed_ramp,
            "clip_anim_in": lambda: self.clip_anim_dialog("in"), "clip_anim_out": lambda: self.clip_anim_dialog("out"),
            "nest": self.nest_selected, "nest_open": self.open_nest,
            "collect_files": self.collect_files, "compare_seq": self.compare_sequences_dialog,
            "match_loudness": self.match_loudness_dialog, "take_add": self.take_add_dialog, "take_next": self.take_next,
            "select_at_playhead": self.select_at_playhead, "duplicate_after": self.duplicate_after,
        }

    def _build_feature6_menus(self):
        mb = self.menuBar()
        help_menu = mb.actions()[-1]
        from .features import QMenu_before
        m = QMenu_before(mb, help_menu, "クリップ")
        for c in ("select_at_playhead", "duplicate_after", None, "speed_ramp", "speed_ramp_clear", None,
                  "clip_anim_in", "clip_anim_out", None, "nest", "nest_open", None, "take_add", "take_next", None,
                  "match_loudness", None, "compare_seq", "collect_files"):
            m.addSeparator() if c is None else self._a(m, c)

    def _sel_clips(self, kinds=None) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if kinds is None or c.kind in kinds]

    # ─────────────────────────── スピードランプ ───────────────────────────
    def speed_ramp_dialog(self):
        names = [n for n, _ in RAMPS]
        n, ok = QInputDialog.getItem(self, "スピードランプ", "速度の変化（クリップの長さはそのまま）:", names, 0, False)
        if ok:
            self.speed_ramp(names.index(n))

    def speed_ramp(self, idx: int) -> int:
        from .render import ramp_table
        clips = self._sel_clips(("video", "audio", "nest"))
        if not clips:
            QMessageBox.information(self, "スピードランプ", "映像か音声のクリップを選んでください。")
            return 0
        name, pts = RAMPS[idx]
        short = []
        self.begin_edit()
        for c in clips:
            d = max(2, c.duration)
            c.props["speed_keys"] = [[int(round(t * (d - 1))), v, "ease_in_out"] for t, v in pts]
            c.props.pop("speed", None)
            m = self.project.media_by_id(c.media_id) if c.media_id else None
            if m is not None and m.duration_s:
                need = c.src_in + float(ramp_table(c)[-1])
                if need > m.duration_s * self.project.sequence.fps + 1:
                    short.append(m.name)
        self.end_edit(f"スピードランプ: {name}（{len(clips)} 個）")
        if short:
            self.status.showMessage(f"素材の長さが足りないクリップがあります（最後は止まります）: {', '.join(short[:3])}", 8000)
        return len(clips)

    def clear_speed_ramp(self) -> int:
        clips = [c for c in self._sel_clips() if c.props.get("speed_keys")]
        self.begin_edit()
        for c in clips:
            c.props.pop("speed_keys", None)
        self.end_edit(f"スピードランプを外す（{len(clips)} 個）")
        return len(clips)

    # ─────────────────────────── 登場 / 退場アニメ ───────────────────────────
    def clip_anim_dialog(self, which: str):
        from .motion import CLIP_ANIMS
        names = [lab for _k, lab in CLIP_ANIMS]
        n, ok = QInputDialog.getItem(self, "登場アニメ" if which == "in" else "退場アニメ",
                                     "動き（テロップは KLA パネルの文字アニメで）:", names, 1, False)
        if ok:
            self.set_clip_anim(which, CLIP_ANIMS[names.index(n)][0])

    def set_clip_anim(self, which: str, kind: str, frames: int = 0) -> int:
        clips = self._sel_clips(("video", "image", "color", "character", "shape", "nest"))
        if not clips:
            QMessageBox.information(self, "クリップのアニメ", "映像・画像・図形などのクリップを選んでください。")
            return 0
        key = "clip_in" if which == "in" else "clip_out"
        self.begin_edit()
        for c in clips:
            if kind:
                c.props[key] = kind
            else:
                c.props.pop(key, None)
            if frames:
                c.props["clip_anim_len"] = frames
        self.end_edit(("登場" if which == "in" else "退場") + f"アニメ（{len(clips)} 個）")
        return len(clips)

    # ─────────────────────────── ネスト ───────────────────────────
    def nest_selected(self, name: str = "") -> Optional[M.Sequence]:
        """選んだクリップを新しいシーケンスにまとめ、その場に 1 本のクリップとして置く（映像と音声）。"""
        seq = self.project.sequence
        sel = list(self.canvas.selected_clips())
        if not sel:
            QMessageBox.information(self, "ネスト", "まとめたいクリップを選んでください。")
            return None
        a, b = min(c.start for _, c in sel), max(c.end for _, c in sel)
        if not name:
            n = sum(1 for s in self.project.sequences if s.name.startswith("ネスト")) + 1
            name = f"ネスト {n:02d}"
        self.begin_edit()
        ns = M.Sequence.create(name=name, fps=seq.fps, width=seq.width, height=seq.height)
        ns.video_tracks = [M.Track("video", t.name, selected=(i == 0)) for i, t in enumerate(seq.video_tracks)]
        ns.audio_tracks = [M.Track("audio", t.name, selected=(i < 2)) for i, t in enumerate(seq.audio_tracks)]
        vt = at = None
        ids = {c.id for _, c in sel}
        for t, c in sel:
            lst = seq.video_tracks if t.kind == "video" else seq.audio_tracks
            i = lst.index(t)
            dst = (ns.video_tracks if t.kind == "video" else ns.audio_tracks)[i]
            dst.clips.append(c.copy(id=M.new_id(), start=c.start - a))
            if t.kind == "video":
                vt = t if vt is None or i < seq.video_tracks.index(vt) else vt
            else:
                at = t if at is None or i < seq.audio_tracks.index(at) else at
        for t in seq.tracks():
            t.clips = [c for c in t.clips if c.id not in ids]
        for t in ns.tracks():
            t.sort()
        ns.markers = [dataclasses.replace(m, frame=m.frame - a) for m in seq.markers if a <= m.frame < b]
        self.project.sequences.append(ns)
        (self.project.bin_of(seq.id) or self.project.bins[0]).add("sequence", ns.id)
        link = M.new_id() if vt and at else ""
        new = []
        if vt is not None:
            new.append((vt, M.Clip(M.new_id(), "nest", a, b - a, name=name, link=link, props={"seq_id": ns.id})))
        if at is not None:
            new.append((at, M.Clip(M.new_id(), "nest", a, b - a, name=name, link=link, props={"seq_id": ns.id})))
        for t, c in new:
            t.clips.append(c)
            t.sort()
        self.end_edit(f"ネスト「{name}」（{len(sel)} 個）")
        self.canvas.selected = {c.id for _, c in new}
        self.canvas.selection_changed.emit()
        self.bin.refresh()
        self.status.showMessage(f"「{name}」にまとめました（中身は「ネストを開く」かビンから）", 5000)
        return ns

    def open_nest(self) -> bool:
        c = next((c for c in self._sel_clips(("nest",))), None)
        if c is None:
            QMessageBox.information(self, "ネストを開く", "ネストのクリップを選んでください。")
            return False
        self.load_sequence(c.props.get("seq_id", ""))
        return True

    # ─────────────────────────── ファイルの収集 ───────────────────────────
    def collect_files(self, dest: str = "", wait: bool = False):
        """使っている素材をプロジェクトフォルダの Media にコピーして、つなぎ直す（持ち運び・納品・バックアップ用）。"""
        if not self.project_path and not dest:
            QMessageBox.information(self, "ファイルの収集", "先にプロジェクトを保存してください。")
            return None
        base = Path(dest) if dest else Path(self.project_path).parent / "Media"
        used = {c.media_id for s in self.project.sequences for _, c in s.all_clips() if c.media_id}
        todo = [m for m in self.project.media if m.id in used and os.path.exists(m.path)
                and not os.path.normcase(os.path.abspath(m.path)).startswith(os.path.normcase(str(base.parent)))]
        if not todo:
            QMessageBox.information(self, "ファイルの収集", "集める素材はありません（もうプロジェクトのフォルダにあります）。")
            return {}
        total = sum(os.path.getsize(m.path) for m in todo)
        if not wait and QMessageBox.question(
                self, "ファイルの収集", f"{len(todo)} 個（{total / 1024 ** 3:.2f} GB）を\n{base}\nにコピーして、つなぎ直しますか？") \
                != QMessageBox.StandardButton.Yes:
            return None
        jobs = [(m.id, m.path) for m in todo]

        def work(task, *_a):
            base.mkdir(parents=True, exist_ok=True)
            done: Dict[str, str] = {}
            used_names = set()
            for i, (mid, src) in enumerate(jobs):
                if task.cancelled:
                    break
                task.progress.emit(i, len(jobs), Path(src).name)
                dst = base / Path(src).name
                k = 1
                while (dst.exists() and os.path.getsize(dst) != os.path.getsize(src)) or dst.name.lower() in used_names:
                    dst = base / f"{Path(src).stem}_{k}{Path(src).suffix}"
                    k += 1
                if not dst.exists():
                    tmp = dst.with_name(dst.name + ".part")
                    shutil.copy2(src, tmp)
                    os.replace(tmp, dst)
                used_names.add(dst.name.lower())
                done[mid] = str(dst)
            return done

        def apply(done):
            self.begin_edit()
            for mid, p in done.items():
                m = self.project.media_by_id(mid)
                if m is not None:
                    m.original_path = m.original_path or m.path
                    m.path = p
            self.end_edit(f"ファイルの収集（{len(done)} 個）")
            self.bin.refresh()
            self.status.showMessage(f"{len(done)} 個を {base} に集めました", 6000)
            return done

        if wait:
            class _T:
                cancelled = False

                class progress:
                    @staticmethod
                    def emit(*_a):
                        pass
            return apply(work(_T()))
        self._run_task("ファイルの収集", work, on_done=apply)
        return None

    # ─────────────────────────── シーケンスの比較 ───────────────────────────
    @staticmethod
    def _clip_key(t: M.Track, c: M.Clip) -> tuple:
        what = c.media_id or str(c.prop("text", "")) or c.prop("color", "") or c.kind
        return t.name, c.kind, what, c.src_in

    def compare_sequences(self, other: M.Sequence, base: Optional[M.Sequence] = None) -> List[Tuple[int, str]]:
        """base（既定は今のシーケンス）と other の違い [(フレーム, 内容)]。"""
        base = base or self.project.sequence
        name = lambda c: (str(c.prop("text", "")).replace("\n", " ")[:20] if c.kind == "text" else c.name or c.kind)
        A: Dict[tuple, List[M.Clip]] = {}
        B: Dict[tuple, List[M.Clip]] = {}
        for t, c in base.all_clips():
            A.setdefault(self._clip_key(t, c), []).append(c)
        for t, c in other.all_clips():
            B.setdefault(self._clip_key(t, c), []).append(c)
        out = []
        for k in set(A) | set(B):
            xa = sorted(A.get(k, []), key=lambda c: c.start)
            xb = sorted(B.get(k, []), key=lambda c: c.start)
            for i in range(max(len(xa), len(xb))):
                ca = xa[i] if i < len(xa) else None
                cb = xb[i] if i < len(xb) else None
                if ca and not cb:
                    out.append((ca.start, f"追加: {k[0]} {name(ca)}"))
                elif cb and not ca:
                    out.append((cb.start, f"削除: {k[0]} {name(cb)}（{other.name} にだけある）"))
                else:
                    if ca.start != cb.start:
                        out.append((ca.start, f"移動: {k[0]} {name(ca)}  {self.tc_str(cb.start)} → {self.tc_str(ca.start)}"))
                    if ca.duration != cb.duration:
                        out.append((ca.start, f"長さ: {k[0]} {name(ca)}  {cb.duration} → {ca.duration} フレーム"))
                    if ca.props != cb.props and ca.start == cb.start and ca.duration == cb.duration:
                        out.append((ca.start, f"設定: {k[0]} {name(ca)}（エフェクト・位置など）"))
        ma = {(m.frame, m.name) for m in base.markers}
        mb_ = {(m.frame, m.name) for m in other.markers}
        out += [(f, f"マーカー追加: {n}") for f, n in ma - mb_] + [(f, f"マーカー削除: {n}") for f, n in mb_ - ma]
        return sorted(out, key=lambda x: x[0])

    def compare_sequences_dialog(self):
        cur = self.project.sequence
        others = [s for s in self.project.sequences if s is not cur]
        if not others:
            QMessageBox.information(self, "シーケンスの比較", "比べるシーケンスがありません（スナップショットや複製を作ってください）。")
            return
        names = [s.name for s in others]
        n, ok = QInputDialog.getItem(self, "シーケンスの比較", f"「{cur.name}」と比べるシーケンス:", names, len(names) - 1, False)
        if not ok:
            return
        diff = self.compare_sequences(others[names.index(n)])
        dlg = QDialog(self)
        dlg.setWindowTitle(f"比較: {cur.name} ⇔ {n}")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"違い {len(diff)} か所　ダブルクリックでその場所へ" if diff else "違いはありません"))
        lw = QListWidget()
        for f, t in diff:
            it = QListWidgetItem(f"{self.tc_str(f)}   {t}")
            it.setData(Qt.ItemDataRole.UserRole, f)
            lw.addItem(it)
        lw.itemDoubleClicked.connect(lambda it: self.seek_record(it.data(Qt.ItemDataRole.UserRole)))
        v.addWidget(lw, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.resize(620, 440)
        dlg.setModal(False)
        dlg.show()
        self._compare_dlg = dlg

    # ─────────────────────────── ラウドネスをそろえる ───────────────────────────
    def match_loudness_dialog(self):
        v, ok = QInputDialog.getDouble(self, "ラウドネスをそろえる",
                                       "目標（LUFS。セリフ −16 / BGM −28 / YouTube 全体 −14 が目安）:", -16.0, -40, -5, 1)
        if ok:
            self.match_loudness(v)

    def match_loudness(self, target: float = -16.0) -> Dict[str, float]:
        """選んだ音声クリップの音量を、それぞれ目標のラウドネスになるように（バラバラのセリフの大きさをそろえる）。"""
        from .analysis import clip_loudness
        fps = self.project.sequence.fps
        clips = self._sel_clips(("audio",))
        if not clips:
            QMessageBox.information(self, "ラウドネスをそろえる", "音声クリップを選んでください。")
            return {}
        res = {}
        self.begin_edit()
        for c in clips:
            m = self.project.media_by_id(c.media_id)
            lu = clip_loudness(m, c, fps) if m else None
            if lu is None:
                continue
            c.props["volume_db"] = round(max(-40.0, min(24.0, target - lu)), 1)
            res[c.id] = c.props["volume_db"]
        self.end_edit(f"ラウドネスを {target:.0f} LUFS にそろえる（{len(res)} 個）")
        if len(res) < len(clips):
            self.status.showMessage(f"{len(clips) - len(res)} 個は無音か、音声の準備中のためそろえられませんでした", 6000)
        return res

    # ─────────────────────────── テイク（オーディション）───────────────────────────
    def take_add_dialog(self):
        c = next(iter(self._sel_clips(("video", "audio", "image"))), None)
        if c is None:
            QMessageBox.information(self, "テイク", "テイクを足すクリップを選んでください。")
            return
        cur = self.project.media_by_id(c.media_id)
        cands = [m for m in self.project.media if m.id != c.media_id and m.kind == (cur.kind if cur else c.kind)]
        if not cands:
            QMessageBox.information(self, "テイク", "同じ種類の素材がビンにありません。")
            return
        names = [m.name for m in cands]
        n, ok = QInputDialog.getItem(self, "テイクを追加", "このクリップの別テイクにする素材:", names, 0, False)
        if ok:
            self.take_add(c, cands[names.index(n)].id)

    def take_add(self, clip: M.Clip, media_id: str) -> List[str]:
        """クリップに別テイク（候補の素材）を足す。「次のテイク」で入れ替えて見比べられる（FCP のオーディション）。"""
        self.begin_edit()
        c = self.project.sequence.find(clip.id)[1]
        takes = list(c.props.get("takes") or [c.media_id])
        if media_id not in takes:
            takes.append(media_id)
        c.props["takes"] = takes
        self.end_edit(f"テイクを追加（{len(takes)} 個）")
        return takes

    def take_next(self) -> int:
        clips = [c for c in self._sel_clips() if len(c.props.get("takes") or []) > 1]
        if not clips:
            QMessageBox.information(self, "次のテイク", "テイクを足したクリップを選んでください（クリップ → テイクを追加）。")
            return 0
        seq = self.project.sequence
        self.begin_edit()
        for c in clips:
            takes = c.props["takes"]
            i = takes.index(c.media_id) if c.media_id in takes else -1
            new = takes[(i + 1) % len(takes)]
            m = self.project.media_by_id(new)
            group = [c] + [o for _t, o in seq.linked(c) if o.media_id == c.media_id and o is not c]
            for o in group:
                o.media_id = new
                if m is not None:
                    o.name = m.name
                    if m.mark_in >= 0:
                        o.src_in = m.mark_in
        self.end_edit("次のテイク")
        m = self.project.media_by_id(clips[0].media_id)
        self.status.showMessage(f"テイク: {m.name if m else ''}（{clips[0].props['takes'].index(clips[0].media_id) + 1} / "
                                f"{len(clips[0].props['takes'])}）", 4000)
        return len(clips)

    # ─────────────────────────── 選択・複製 ───────────────────────────
    def select_at_playhead(self) -> int:
        """再生ヘッドの位置にあるクリップを選ぶ（選択トラックだけ。Premiere の D）。"""
        f = self.record_player.position
        tracks = self._selected_tracks()
        ids = {c.id for t in tracks for c in t.clips if c.start <= f < c.end}
        self.canvas.selected = ids
        self.canvas.selection_changed.emit()
        self.canvas.update()
        return len(ids)

    def duplicate_after(self) -> int:
        """選んだクリップを、すぐ後ろに複製（後ろのクリップは押し出さず、空いている所だけ）。"""
        sel = list(self.canvas.selected_clips())
        if not sel:
            return 0
        a, b = min(c.start for _, c in sel), max(c.end for _, c in sel)
        span = b - a
        if any(not t.is_empty(c.start + span, c.end + span, ignore={x.id for _, x in sel}) for t, c in sel):
            QMessageBox.information(self, "すぐ後ろに複製", "後ろにほかのクリップがあるので置けません。")
            return 0
        self.begin_edit()
        links: Dict[str, str] = {}
        new = []
        for t, c in sel:
            nl = links.setdefault(c.link, M.new_id()) if c.link else ""
            n = c.copy(id=M.new_id(), start=c.start + span, link=nl)
            t.clips.append(n)
            t.sort()
            new.append(n)
        self.end_edit(f"すぐ後ろに複製（{len(new)} 個）")
        self.canvas.selected = {c.id for c in new}
        self.canvas.selection_changed.emit()
        return len(new)
