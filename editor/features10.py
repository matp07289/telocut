"""各機能を使いやすい場所に置く仕組みと、30 の新機能（MainWindow のミックスイン）。

置き場所: ページごとの道具の列（ツールバーの 2 段目）・エフェクトエディターの「すぐ使う」ボタン（選んだクリップの種類に合わせる）・
         タイムラインとビンの右クリック・やりたいこと検索がメニューだけの項目も探す・長いメニューのサブメニュー化
テロップ: N 文字で改行・結合・縦書き・PNG で書き出し・CSV で書き出し・文字の置換・動きを一括
編集: 1 つ上 / 下のトラックへ・頭 / 終わりを再生ヘッドに・前の編集位置へ戻る・IN〜OUT をスロー / 早送り・ラベルの色で選択
音声: 音量 +3dB / −3dB
映像: ルック（ワンクリックの色調）・モザイク・揺れ・背景を抜く（緑 / 青 / 白 / 黒）・位置と大きさをリセット・カクカク（コマを間引く）
制作: 目標の長さ・やることリスト・保存のたびにバックアップ・書き出しファイル名のルール・ショートカット早見表・
      クリップの情報をコピー・タイムラインを画像に
"""
from __future__ import annotations

import datetime
import html
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import model as M
from .settings import SETTINGS

# ─────────────────────────── 置き場所 ───────────────────────────
# ページごとの道具の列（ツールバーの 2 段目）。(コマンド, ボタンの文字)
PAGE_TOOLS: Dict[str, List[Optional[Tuple[str, str]]]] = {
    "edit": [("splice_in", "挿入"), ("overwrite", "上書き"), ("add_edit", "✂ 分割"), ("ripple_delete", "詰めて削除"),
             ("close_gaps", "隙間を詰める"), None, ("trim_head_here", "⇤ 頭をここに"), ("trim_tail_here", "終わりをここに ⇥"),
             ("clip_up", "↑ 上のトラックへ"), ("clip_down", "↓ 下のトラックへ"), None, ("add_marker", "◆ マーカー"),
             ("back_to_last_edit", "↩ 前の編集位置"), ("insert_space", "空白を挿入")],
    "telop": [("telop_template", "🅣 テンプレート"), ("add_telop", "T 追加"), ("telop_editor", "テロップエディター"),
              ("transcribe_subs", "🎤 文字起こし"), None, ("telop_check", "✓ 表記チェック"), ("telop_tidy", "句読点"),
              ("telop_wrap", "改行"), ("telop_merge", "結合"), ("telop_vertical", "縦書き"), ("telop_replace", "置換"),
              None, ("telop_anim_all", "動きを一括"), ("telop_font_all", "フォント一括"), ("telop_se", "♪ 効果音")],
    "audio": [("vol_up", "🔊 +3dB"), ("vol_down", "🔉 −3dB"), ("normalize", "ノーマライズ"), ("loudness", "−14 LUFS"),
              None, ("toggle_duck", "ダッキング"), ("bgm_loop", "BGM ループ"), ("bgm_end", "BGM の終わり"), None,
              ("bleep", "ピー音"), ("silence_cut", "無音カット"), ("se_pad", "SE パッド"), ("mixdown_audio", "ミックスダウン")],
    "effects": [("look", "🎨 ルック"), ("key_out", "背景を抜く"), ("mosaic", "モザイク"), ("shake_range", "揺れ"),
                None, ("wipe", "ワイプ"), ("blur_fill", "ぼかし背景"), ("auto_crop", "黒帯カット"),
                ("reset_transform", "位置をリセット"), None, ("slow_range", "スロー"), ("fast_range", "早送り"),
                ("frame_step", "カクカク"), ("motion_panel", "モーション")],
    "finish": [("preflight", "✓ 書き出し前チェック"), ("telop_check", "表記チェック"), ("loudness", "−14 LUFS"),
               ("target_length", "⏱ 目標の長さ"), ("todo_panel", "☑ やること"), None, ("thumbnail", "サムネイル"),
               ("description", "概要欄"), ("export", "📤 書き出し"), ("work_report", "作業レポート")],
}

# エフェクトエディターの「すぐ使う」ボタン（選んだクリップの種類ごと）
QUICK: Dict[str, List[Tuple[str, str]]] = {
    "text": [("telop_template", "テンプレート"), ("telop_wrap", "改行"), ("telop_vertical", "縦書き"),
             ("telop_merge", "結合"), ("telop_anim_all", "動き"), ("telop_png", "PNG に")],
    "video": [("look", "ルック"), ("wipe", "ワイプ"), ("blur_fill", "ぼかし背景"), ("auto_crop", "黒帯カット"),
              ("key_out", "背景を抜く"), ("mosaic", "モザイク"), ("frame_step", "カクカク"), ("reset_transform", "リセット")],
    "image": [("look", "ルック"), ("wipe", "ワイプ"), ("blur_fill", "ぼかし背景"), ("key_out", "背景を抜く"),
              ("auto_crop", "黒帯カット"), ("reset_transform", "リセット")],
    "audio": [("vol_up", "+3dB"), ("vol_down", "−3dB"), ("normalize", "ノーマライズ"), ("audio_mono", "モノラル"),
              ("add_fades", "フェード")],
    "any": [("clip_up", "↑ 上へ"), ("clip_down", "↓ 下へ"), ("copy_clip_info", "情報をコピー")],
}

LOOKS: Dict[str, List[dict]] = {
    "明るく": [{"name": "brightness", "value": 1.15}, {"name": "contrast", "value": 1.05}],
    "あたたかい": [{"name": "temperature", "value": 35.0}, {"name": "saturation", "value": 1.1}],
    "すずしい": [{"name": "temperature", "value": -35.0}],
    "シネマ": [{"name": "contrast", "value": 1.12}, {"name": "saturation", "value": 0.85},
             {"name": "vignette", "amount": 0.5, "size": 0.75}],
    "レトロ": [{"name": "sepia"}, {"name": "grain", "amount": 0.3}, {"name": "vignette", "amount": 0.6, "size": 0.7}],
    "モノクロ": [{"name": "mono"}, {"name": "contrast", "value": 1.1}],
    "くっきり": [{"name": "sharpen", "value": 0.6}, {"name": "saturation", "value": 1.15}],
    "（ルックを外す）": [],
}
KEY_COLORS = {"緑（グリーンバック）": ("#00ff00", 0.25), "青（ブルーバック）": ("#0000ff", 0.25),
              "白（白い背景のイラスト）": ("#ffffff", 0.12), "黒（黒い背景の素材）": ("#000000", 0.12)}
TEXT_ANIMS = [("", "なし"), ("fade", "フェード"), ("pop", "ポップ"), ("slide", "スライド"), ("zoom", "ズーム"),
              ("drop", "落ちてくる"), ("wipe", "ワイプ"), ("blur", "ぼかしから"), ("type", "タイプライター")]
# 文の切れ目（この文字の後ろで改行しやすい）
BREAK_AFTER = "、。，．！？!?」』）) "
PARTICLES = "はがをにでともへや"


def wrap_text(text: str, n: int) -> str:
    """1 行が n 文字を超えたら、句読点・助詞のあと（無ければ n 文字目）で改行する。"""
    out = []
    for line in text.split("\n"):
        while len(line) > n:
            cut = 0
            for i in range(n, max(1, n // 2) - 1, -1):
                if line[i - 1] in BREAK_AFTER:
                    cut = i
                    break
            if not cut:
                for i in range(n, max(1, n // 2) - 1, -1):
                    if line[i - 1] in PARTICLES and i < len(line):
                        cut = i
                        break
            cut = cut or n
            out.append(line[:cut].rstrip())
            line = line[cut:].lstrip()
        out.append(line)
    return "\n".join(out)


VERTICAL_MAP = {"ー": "｜", "−": "｜", "-": "｜", "〜": "≀", "～": "≀", "…": "︙", "（": "︵", "）": "︶", "「": "﹁", "」": "﹂",
                "(": "︵", ")": "︶", "【": "︻", "】": "︼"}


def to_vertical(text: str) -> str:
    """縦書き風（1 文字ずつ改行・長音やカッコを縦向きの文字に）。2 行以上は右から左の列に並べる。"""
    cols = [[VERTICAL_MAP.get(ch, ch) for ch in line if ch != " "] for line in text.split("\n")]
    cols = [c for c in cols if c] or [[]]
    h = max(len(c) for c in cols)
    rows = []
    for i in range(h):
        rows.append("　".join(c[i] if i < len(c) else "　" for c in reversed(cols)))
    return "\n".join(rows)


def _fmt_s(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600):02d}:{int(sec // 60 % 60):02d}:{sec % 60:06.3f}"


class TodoPanel(QWidget):
    """やることリスト（プロジェクトに保存。チェックで完了）。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self._proj = None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        h = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("やることを書いて Enter（例: サムネを作る・BGM のクレジット）")
        self.edit.returnPressed.connect(self.add)
        h.addWidget(self.edit, 1)
        rm = QPushButton("完了を消す")
        rm.clicked.connect(self.remove_done)
        h.addWidget(rm)
        v.addLayout(h)
        self.list = QListWidget()
        self.list.itemChanged.connect(self._save)
        v.addWidget(self.list, 1)
        self.info = QLabel("")
        self.info.setObjectName("info")
        v.addWidget(self.info)

    def items(self) -> List[dict]:
        return self.mw.project.settings.setdefault("todo", [])

    def refresh(self, force: bool = False):
        if self.mw.project is self._proj and not force:
            return
        self._proj = self.mw.project
        self.list.blockSignals(True)
        self.list.clear()
        for it in self.items():
            li = QListWidgetItem(it.get("text", ""))
            li.setFlags(li.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEditable)
            li.setCheckState(Qt.CheckState.Checked if it.get("done") else Qt.CheckState.Unchecked)
            self.list.addItem(li)
        self.list.blockSignals(False)
        self._count()

    def _count(self):
        its = self.items()
        self.info.setText(f"{sum(1 for x in its if x.get('done'))} / {len(its)} 完了")

    def _save(self, *_a):
        self.mw.project.settings["todo"] = [{"text": self.list.item(i).text(),
                                             "done": self.list.item(i).checkState() == Qt.CheckState.Checked}
                                            for i in range(self.list.count())]
        self.mw.dirty = True
        self._count()

    def add(self, text: str = ""):
        text = (text or self.edit.text()).strip()
        if not text:
            return
        self.items().append({"text": text, "done": False})
        self.edit.clear()
        self.mw.dirty = True
        self.refresh(force=True)

    def remove_done(self):
        self.mw.project.settings["todo"] = [x for x in self.items() if not x.get("done")]
        self.mw.dirty = True
        self.refresh(force=True)


class Features10Mixin:
    def _feature10_commands(self) -> dict:
        return {
            "telop_wrap": self.telop_wrap_dialog, "telop_merge": self.telop_merge, "telop_vertical": self.telop_vertical,
            "telop_png": self.telop_png_dialog, "telop_csv": self.telop_csv_dialog, "telop_replace": self.telop_replace_dialog,
            "telop_anim_all": self.telop_anim_dialog, "clip_up": lambda: self.move_clips_track(1),
            "clip_down": lambda: self.move_clips_track(-1), "trim_head_here": lambda: self.trim_to_playhead("in"),
            "trim_tail_here": lambda: self.trim_to_playhead("out"), "back_to_last_edit": self.back_to_last_edit,
            "slow_range": lambda: self.speed_range(0.5), "fast_range": lambda: self.speed_range(2.0),
            "select_label": self.select_label_dialog, "vol_up": lambda: self.nudge_volume(3.0),
            "vol_down": lambda: self.nudge_volume(-3.0), "look": self.look_dialog, "mosaic": self.add_mosaic,
            "shake_range": self.shake_range, "key_out": self.key_out_dialog, "reset_transform": self.reset_transform,
            "frame_step": self.toggle_frame_step, "target_length": self.target_length_dialog,
            "todo_panel": lambda: self.show_dock(self.dock_todo), "backup_folder": self.backup_folder_dialog,
            "export_name_rule": self.export_name_dialog, "key_cheatsheet": self.key_cheatsheet_dialog,
            "copy_clip_info": self.copy_clip_info, "timeline_image": self.timeline_image,
        }

    def _init_features10(self):
        """やることリストのパネル・目標の長さの表示・編集位置の記録（メニューより前に呼ぶ）。"""
        self.todo_panel = TodoPanel(self)
        self.dock_todo = self._dock("やることリスト", self.todo_panel, Qt.DockWidgetArea.RightDockWidgetArea, "todo")
        self.tabifyDockWidget(self.dock_insp, self.dock_todo)
        self.dock_todo.hide()
        self.docks.append(self.dock_todo)
        self.target_label = QLabel("")
        self.target_label.setObjectName("info")
        self.status.addPermanentWidget(self.target_label)
        self._edit_positions: List[int] = []
        self._f10_timer = QTimer(self)
        self._f10_timer.setInterval(1500)
        self._f10_timer.timeout.connect(self._f10_tick)
        self._f10_timer.start()

    def _f10_tick(self):
        self.update_target_label()
        self.todo_panel.refresh()

    def _build_feature10_menus(self):
        def into(title: str, items, sub: str = ""):
            m = self._menu_named(title)
            if m is None:
                return
            m.addSeparator()
            if sub:
                m = m.addMenu(sub)
            for c in items:
                m.addSeparator() if c is None else self._a(m, c)
        into("テロップ", ("telop_wrap", "telop_merge", "telop_vertical", "telop_replace", "telop_anim_all", None,
                         "telop_png", "telop_csv"), "整える・書き出す（改行・結合・縦書き・PNG など）")
        into("編集", ("clip_up", "clip_down", "trim_head_here", "trim_tail_here", "back_to_last_edit", "select_label"))
        into("クリップ", ("look", "key_out", "mosaic", "shake_range", "reset_transform", "frame_step", None,
                         "slow_range", "fast_range", None, "copy_clip_info"), "見た目と動き（ルック・背景を抜く・スロー など）")
        into("オーディオ", ("vol_up", "vol_down"))
        into("整理・チェック", ("target_length", "todo_panel", "backup_folder", "export_name_rule"))
        into("ウィンドウ", ("todo_panel", "timeline_image"))
        into("ヘルプ", ("key_cheatsheet",))

    # ─────────────────────────── 置き場所 ───────────────────────────
    def _init_feature10_ui(self):
        """ページの道具の列・すぐ使うボタン（ツールバーとページを作ったあとに呼ぶ）。"""
        self.addToolBarBreak(Qt.ToolBarArea.TopToolBarArea)
        tb = QToolBar("ページの道具", self)
        tb.setObjectName("page_toolbar")
        tb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        self.page_toolbar = tb
        self.fill_page_toolbar(SETTINGS.page or "edit")
        QTimer.singleShot(0, self.ensure_page_toolbar_row)        # 保存した配置を戻したあとでも 2 段目に
        box = getattr(self.inspector, "quick_box", None)
        if box is not None:
            self.fill_quick(set())

    def restoreState(self, state, version: int = 0) -> bool:
        """配置を戻したあと（ワークスペース・複数画面の切り替えも）、ページの道具の列を 2 段目に戻す。"""
        ok = super().restoreState(state, version)
        if hasattr(self, "page_toolbar"):
            self.ensure_page_toolbar_row()
        return ok

    def ensure_page_toolbar_row(self):
        """ページの道具の列を、メインのツールバーの下の段に置く（同じ段に並ぶと右が切れて見えない）。"""
        tb = self.page_toolbar
        if self.toolBarArea(tb) != Qt.ToolBarArea.TopToolBarArea:
            self.addToolBar(Qt.ToolBarArea.TopToolBarArea, tb)
        if not self.toolBarBreak(tb):
            self.insertToolBarBreak(tb)
        tb.show()

    def fill_page_toolbar(self, key: str) -> List[str]:
        """ページに合う道具を 2 段目に並べる。並べたコマンドを返す。"""
        tb = self.page_toolbar
        tb.clear()
        lab = QLabel({"edit": " 編集 ", "telop": " テロップ ", "audio": " 音声 ", "effects": " エフェクト ",
                      "finish": " 仕上げ "}.get(key, ""))
        lab.setObjectName("info")
        tb.addWidget(lab)
        put = []
        for item in PAGE_TOOLS.get(key, []):
            if item is None:
                tb.addSeparator()
                continue
            cid, text = item
            a = self.actions.get(cid)
            if a is None:
                continue
            b = QToolButton()
            b.setText(text)
            b.setToolTip(a.toolTip())
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setObjectName("pt_" + cid)
            b.clicked.connect(lambda _=False, c=cid: self.run_command(c))
            tb.addWidget(b)
            put.append(cid)
        return put

    def fill_quick(self, kinds: set) -> List[str]:
        """エフェクトエディターの「すぐ使う」ボタンを、選んだクリップの種類に合わせて並べる。"""
        insp = getattr(self, "inspector", None)
        if insp is None or not hasattr(self, "actions"):         # 起動の途中（まだボタンを作れない）
            return []
        box = insp.quick_box
        grid: QGridLayout = box.layout()
        while grid.count():
            w = grid.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        cids: List[Tuple[str, str]] = []
        kinds = set(kinds)
        if kinds & {"color", "nest"}:
            kinds.add("image")                            # 色・ネストも映像と同じ道具が使える
        for k in ("text", "video", "image", "audio"):
            if k in kinds:
                cids += [x for x in QUICK[k] if x not in cids]
        if kinds:
            cids += [x for x in QUICK["any"] if x not in cids]
        for i, (cid, text) in enumerate(cids):
            a = self.actions.get(cid)
            if a is None:
                continue
            b = QPushButton(text)
            b.setObjectName("q_" + cid)
            b.setToolTip(a.toolTip())
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, c=cid: self.run_command(c))
            grid.addWidget(b, i // 3, i % 3)
        box.setVisible(bool(cids))
        return [c for c, _t in cids]

    def clip_context_actions(self, menu, clip: M.Clip):
        """タイムラインの右クリック（クリップの上）に、そのクリップの種類で使う機能を足す。"""
        menu.addSeparator()
        sub = menu.addMenu("このクリップで")
        cids = [c for c, _t in QUICK.get(clip.kind, [])] + [c for c, _t in QUICK["any"]]
        if clip.kind == "text":
            cids += ["telop_check", "telop_tidy", "telop_se"]
        if clip.kind in ("video", "image"):
            cids += ["shake_range", "slow_range", "fast_range", "tsukkomi", "replay"]
        if clip.kind == "audio":
            cids += ["bleep", "bgm_end", "vol_up", "vol_down"]
        seen = []
        for c in cids:
            if c in self.actions and c not in seen:
                sub.addAction(self.actions[c])
                seen.append(c)
        return seen

    def empty_context_actions(self, menu, frame: int):
        menu.addSeparator()
        for c in ("insert_space", "add_marker", "telop_template", "subscribe_button", "transition_flash"):
            if c in self.actions:
                menu.addAction(self.actions[c])

    def bin_context_actions(self, menu):
        menu.addSeparator()
        sub = menu.addMenu("ビンの整理")
        for c in ("bins_by_kind", "merge_duplicates", "place_by_date", "unused_media"):
            if c in self.actions:
                sub.addAction(self.actions[c])

    # ── 編集位置の記録（end_edit から呼ばれる）──
    def note_edit_position(self):
        f = self.record_player.position
        if not self._edit_positions or self._edit_positions[-1] != f:
            self._edit_positions.append(f)
            del self._edit_positions[:-50]

    def back_to_last_edit(self) -> Optional[int]:
        """前に編集した場所へ戻る（押すたびにさらに前へ）。"""
        here = self.record_player.position
        while self._edit_positions:
            f = self._edit_positions.pop()
            if f != here:
                self.seek_record(f)
                return f
        self.status.showMessage("戻る編集位置がありません", 2000)
        return None

    # ─────────────────────────── テロップ ───────────────────────────
    def telop_wrap_dialog(self):
        n, ok = QInputDialog.getInt(self, "テロップを改行", "1 行の文字数:", 16, 4, 60)
        if ok:
            self.telop_wrap(n)

    def telop_wrap(self, n: int = 16) -> int:
        cnt = 0
        self.begin_edit()
        for c in self._text_targets():
            old = str(c.prop("text", ""))
            new = wrap_text(old.replace("\n", ""), n)          # 今の改行を外して、n 文字で組み直す
            if new != old:
                c.props["text"] = new
                cnt += 1
        self.end_edit(f"テロップを {n} 文字で改行（{cnt} 個）")
        return cnt

    def telop_merge(self) -> Optional[M.Clip]:
        """選んだテロップを 1 つにまとめる（文は改行でつなぐ・長さは最初から最後まで）。"""
        seq = self.project.sequence
        sel = sorted(((t, c) for t, c in self.canvas.selected_clips() if c.kind == "text"), key=lambda x: x[1].start)
        if len(sel) < 2:
            QMessageBox.information(self, "テロップを結合", "テロップを 2 つ以上選んでください。")
            return None
        first_t, first = sel[0]
        end = max(c.end for _t, c in sel)
        self.begin_edit()
        first.props["text"] = "\n".join(str(c.prop("text", "")) for _t, c in sel)
        for t, c in sel[1:]:
            t.clips.remove(c)
        if first_t.is_empty(first.end, end, ignore=(first.id,)):
            first.duration = end - first.start
        self.end_edit(f"テロップを結合（{len(sel)} 個）")
        self.canvas.selected = {first.id}
        self.canvas.selection_changed.emit()
        return first

    def telop_vertical(self) -> int:
        """縦書き / 横書きを切り替える（元の文は覚えておく）。"""
        clips = self._text_targets()
        self.begin_edit()
        for c in clips:
            if c.props.get("text_h") is not None:
                c.props["text"] = c.props.pop("text_h")
            else:
                c.props["text_h"] = str(c.prop("text", ""))
                c.props["text"] = to_vertical(c.props["text_h"])
        self.end_edit(f"縦書き / 横書き（{len(clips)} 個）")
        return len(clips)

    def telop_png_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "テロップを PNG で書き出す先")
        if d:
            n = len(self.telop_png(d))
            self.status.showMessage(f"{n} 枚書き出しました: {d}", 5000)

    def telop_png(self, folder: str) -> List[str]:
        """選んだテロップ（無ければすべて）を、背景が透明な PNG に（サムネや他のソフトで使う）。"""
        from PIL import Image
        from .render import text_patch
        out = []
        for i, c in enumerate(sorted(self._text_targets(), key=lambda c: c.start), 1):
            arr, _x, _y = text_patch(self.project, c, 1.0)
            if arr is None:
                continue
            name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(c.prop("text", "")).strip())[:24] or "telop"
            p = os.path.join(folder, f"{i:03d}_{name}.png")
            Image.fromarray(arr).save(p)
            out.append(p)
        return out

    def telop_csv_dialog(self):
        p, _ = QFileDialog.getSaveFileName(self, "テロップを CSV で書き出し", f"{self.project.sequence.name}_テロップ.csv", "CSV (*.csv)")
        if p:
            self.telop_csv(p)

    def telop_csv(self, path: str) -> str:
        """テロップを CSV（Excel で開ける・直して ファイル → テロップを読み込み で戻せる）。"""
        from core.exporter import export_csv
        from core.models import TelopEntry
        seq = self.project.sequence
        clips = sorted((c for _, c in seq.all_clips() if c.kind == "text"), key=lambda c: c.start)
        ents = [TelopEntry(id=i, start_time=_fmt_s(c.start / seq.fps), end_time=_fmt_s(c.end / seq.fps),
                           text=str(c.prop("text_h", c.prop("text", ""))), enabled=c.enabled,
                           speaker=str(c.props.get("speaker") or "")) for i, c in enumerate(clips, 1)]
        export_csv(ents, path)
        return path

    def telop_replace_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("テロップの文字を置換")
        f = QFormLayout(dlg)
        a, b = QLineEdit(), QLineEdit()
        rx = QCheckBox("正規表現")
        f.addRow("探す", a)
        f.addRow("置き換える", b)
        f.addRow(rx)
        f.addRow(QLabel("選んだテロップ（無ければすべて）が対象です。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec() and a.text():
            n = self.telop_replace(a.text(), b.text(), rx.isChecked())
            self.status.showMessage(f"{n} 個のテロップを置き換えました", 4000)

    def telop_replace(self, find: str, repl: str, regex: bool = False) -> int:
        n = 0
        self.begin_edit()
        for c in self._text_targets():
            old = str(c.prop("text", ""))
            try:
                new = re.sub(find, repl, old) if regex else old.replace(find, repl)
            except re.error as ex:
                self.end_edit(None)
                QMessageBox.warning(self, "置換", f"正規表現が正しくありません: {ex}")
                return 0
            if new != old:
                c.props["text"] = new
                n += 1
        self.end_edit(f"テロップの文字を置換（{n} 個）")
        return n

    def telop_anim_dialog(self):
        labels = [lab for _k, lab in TEXT_ANIMS]
        a, ok = QInputDialog.getItem(self, "テロップの動きを一括", "出るとき:", labels, 2, False)
        if not ok:
            return
        b, ok = QInputDialog.getItem(self, "テロップの動きを一括", "消えるとき:", [labels[0], labels[1]], 1, False)
        if ok:
            self.telop_anim_all(TEXT_ANIMS[labels.index(a)][0], "fade" if b == labels[1] else "")

    def telop_anim_all(self, anim_in: str = "pop", anim_out: str = "fade") -> int:
        clips = self._text_targets()
        self.begin_edit()
        for c in clips:
            for key, v in (("text_anim", anim_in), ("text_anim_out", anim_out)):
                if v:
                    c.props[key] = v
                else:
                    c.props.pop(key, None)
        self.end_edit(f"テロップの動きを一括（{len(clips)} 個）")
        return len(clips)

    # ─────────────────────────── 編集 ───────────────────────────
    def move_clips_track(self, d: int) -> int:
        """選んだクリップを 1 つ上（d=1）/ 下（d=-1）のトラックへ。上が無ければ新しいトラックを作る。ふさがっていれば動かさない。"""
        seq = self.project.sequence
        sel = list(self.canvas.selected_clips())
        if not sel:
            return 0
        self.begin_edit()
        n = 0
        for t, c in sel:
            tracks = seq.video_tracks if t.kind == "video" else seq.audio_tracks
            # 映像は上 = 番号が大きい・音声は下 = 番号が大きい（画面の上下に合わせる）
            step = d if t.kind == "video" else -d
            i = tracks.index(t) + step
            if i < 0:
                continue
            if i >= len(tracks):
                nt = M.Track(t.kind, f"{'V' if t.kind == 'video' else 'A'}{len(tracks) + 1}", selected=False)
                tracks.append(nt)
            dst = tracks[i]
            if dst.locked or not dst.is_empty(c.start, c.end):
                continue
            t.clips.remove(c)
            dst.clips.append(c)
            dst.sort()
            n += 1
        self.end_edit(("上" if d > 0 else "下") + f"のトラックへ（{n} 個）")
        return n

    def trim_to_playhead(self, edge: str) -> int:
        """選んだクリップ（無ければ再生ヘッドの下）の頭 / 終わりを再生ヘッドに合わせる（後ろは詰めない）。"""
        seq = self.project.sequence
        f = self.record_player.position
        sel = list(self.canvas.selected_clips())
        if not sel:
            sel = [(t, t.clip_at(f)) for t in seq.tracks() if t.clip_at(f) is not None and not t.locked]
        self.begin_edit()
        n = 0
        for t, c in sel:
            delta = (f - c.start) if edge == "in" else (f - c.end)
            if delta == 0:
                continue
            moved = M.trim_edge(seq, t, c, edge, delta, self.project)
            n += bool(moved)
        self.end_edit(("頭" if edge == "in" else "終わり") + f"を再生ヘッドに（{n} 個）")
        return n

    def speed_range(self, k: float) -> int:
        """IN〜OUT を k 倍の速さに（0.5 = スロー・2 = 早送り）。全部のトラックで後ろをずらして同期を保つ。早送りには ▶▶ の表示。"""
        seq = self.project.sequence
        a, b = (seq.mark_in, seq.mark_out) if 0 <= seq.mark_in < seq.mark_out else (-1, -1)
        if b <= a:
            sel = [c for _, c in self.canvas.selected_clips()]
            if sel:
                a, b = min(c.start for c in sel), max(c.end for c in sel)
        if b <= a:
            QMessageBox.information(self, "スロー / 早送り", "IN と OUT を付けるか、クリップを選んでください。")
            return 0
        new_len = max(1, int(round((b - a) / k)))
        delta = new_len - (b - a)
        self.begin_edit()
        n = 0
        for t in seq.tracks():
            if t.locked:
                continue
            M.split_clip(t, a)
            M.split_clip(t, b)
            inner = [c for c in t.clips if c.start >= a and c.end <= b]
            after = [c for c in t.clips if c.start >= b]
            for c in after:
                c.start += delta
            for c in inner:
                c.start = a + int(round((c.start - a) / k))
                c.duration = max(1, int(round(c.duration / k)))
                if c.kind in ("video", "audio", "nest"):
                    c.props["speed"] = round(float(c.prop("speed", 1.0) or 1.0) * k, 4)
                n += 1
            t.sort()
        for m in seq.markers:
            if m.frame >= b:
                m.frame += delta
        if k > 1:
            lab = M.Track("video", "早送り", selected=False)
            seq.video_tracks.append(lab)
            lab.clips.append(M.Clip(M.new_id(), "text", a, new_len, name="早送り",
                                    props={"text": f"▶▶ ×{k:g}", "style": {"font_size": 54, "bold": True,
                                                                          "position_h": "right", "position_v": "top",
                                                                          "offset_x": 50, "offset_y": 40}}))
        self.end_edit(("早送り" if k > 1 else "スロー") + f"（×{k:g}・{n} クリップ）")
        return n

    def select_label_dialog(self):
        cols = sorted({str(c.props.get("label")) for _, c in self.project.sequence.all_clips() if c.props.get("label")})
        if not cols:
            QMessageBox.information(self, "ラベルの色で選択", "クリップカラーの付いたクリップがありません（右クリック → クリップカラー）。")
            return
        col, ok = QInputDialog.getItem(self, "ラベルの色で選択", "色:", cols, 0, False)
        if ok:
            self.select_label(col)

    def select_label(self, color: str) -> int:
        ids = {c.id for _, c in self.project.sequence.all_clips() if str(c.props.get("label", "")).lower() == color.lower()}
        self.canvas.selected = ids
        self.canvas.selection_changed.emit()
        self.canvas.update()
        return len(ids)

    # ─────────────────────────── 音声 ───────────────────────────
    def nudge_volume(self, db: float) -> int:
        clips = [c for _, c in self.canvas.selected_clips() if c.kind in ("audio", "nest")]
        if not clips:
            self.status.showMessage("音声のクリップを選んでください", 2000)
            return 0
        self.begin_edit()
        for c in clips:
            c.props["volume_db"] = round(max(-60.0, min(24.0, float(c.prop("volume_db", 0.0) or 0.0) + db)), 1)
        self.end_edit(f"音量 {db:+g} dB（{len(clips)} 個）")
        self.status.showMessage(f"音量 {clips[0].props['volume_db']:+.1f} dB", 2000)
        return len(clips)

    # ─────────────────────────── 映像 ───────────────────────────
    def _visual_sel(self) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image", "nest", "color", "shape")]

    def look_dialog(self):
        names = list(LOOKS)
        n, ok = QInputDialog.getItem(self, "ルック", "色の雰囲気:", names, 0, False)
        if ok:
            self.apply_look(n)

    def apply_look(self, name: str) -> int:
        """ワンクリックの色調（前に付けたルックは置き換える。ほかのエフェクトは残す）。"""
        clips = self._visual_sel()
        if not clips:
            QMessageBox.information(self, "ルック", "映像か画像のクリップを選んでください。")
            return 0
        self.begin_edit()
        for c in clips:
            fx = [e for e in (c.prop("effects") or []) if not e.get("look")]
            fx += [{**e, "on": True, "look": name} for e in LOOKS.get(name, [])]
            c.props["effects"] = fx
        self.end_edit(f"ルック「{name}」（{len(clips)} 個）")
        return len(clips)

    def add_mosaic(self, rect=(35.0, 30.0, 30.0, 40.0), block: int = 16) -> int:
        """選んだ映像にモザイク（範囲はエフェクトエディターで直せる・動く物には モーション → トラッキング）。"""
        clips = self._visual_sel()
        self.begin_edit()
        for c in clips:
            c.props["effects"] = list(c.prop("effects") or []) + [{"name": "mosaic", "value": block, "rect": list(rect), "on": True}]
        self.end_edit(f"モザイク（{len(clips)} 個）")
        return len(clips)

    def _split_range_visual(self, a: int, b: int) -> List[M.Clip]:
        """映像のトラックで a〜b を切り分けて、間のクリップを返す。"""
        seq = self.project.sequence
        out = []
        for t in seq.video_tracks:
            if t.locked or t.muted:
                continue
            M.split_clip(t, a)
            M.split_clip(t, b)
            out += [c for c in t.clips if c.start >= a and c.end <= b and c.kind in ("video", "image", "nest", "color")]
        return out

    def shake_range(self, amount: float = 18.0) -> int:
        """IN〜OUT（無ければ再生ヘッドから 0.5 秒）の映像を揺らす（ドン！の演出）。"""
        seq = self.project.sequence
        if 0 <= seq.mark_in < seq.mark_out:
            a, b = seq.mark_in, seq.mark_out
        else:
            a = self.record_player.position
            b = a + int(round(seq.fps * 0.5))
        self.begin_edit()
        clips = self._split_range_visual(a, b)
        for c in clips:
            fx = [e for e in (c.prop("effects") or []) if e.get("name") != "shake"]
            c.props["effects"] = fx + [{"name": "shake", "value": amount, "on": True}]
            c.props["scale"] = max(float(c.prop("scale", 1.0) or 1.0), 1.06)      # 揺れても端が見えないように少し大きく
        self.end_edit(f"揺れ（{len(clips)} 個）")
        return len(clips)

    def key_out_dialog(self):
        names = list(KEY_COLORS)
        n, ok = QInputDialog.getItem(self, "背景を抜く", "抜く色:", names, 0, False)
        if ok:
            self.key_out(n)

    def key_out(self, name: str) -> int:
        col, tol = KEY_COLORS.get(name, (name, 0.2))
        clips = [c for c in self._visual_sel() if c.kind in ("video", "image")]
        self.begin_edit()
        for c in clips:
            c.props["chroma"] = {"color": col, "tol": tol, "soft": 0.08}
        self.end_edit(f"背景を抜く（{len(clips)} 個）")
        return len(clips)

    def reset_transform(self) -> int:
        """位置・大きさ・回転・切り抜き・不透明度・アニメを元に戻す（エフェクトは残す）。"""
        clips = self._visual_sel() + [c for _, c in self.canvas.selected_clips() if c.kind == "text"]
        self.begin_edit()
        for c in clips:
            for k in ("scale", "pos_x", "pos_y", "rotation", "crop", "opacity", "anim", "zoomcut_base"):
                c.props.pop(k, None)
            keys = c.props.get("keys") or {}
            for k in ("scale", "pos_x", "pos_y", "rotation", "opacity"):
                keys.pop(k, None)
        self.end_edit(f"位置・大きさをリセット（{len(clips)} 個）")
        return len(clips)

    def toggle_frame_step(self, step: int = 3) -> int:
        """カクカク（コマを間引いてアニメや昔のゲーム風に）。もう一度で戻す。"""
        clips = [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "nest")]
        on = not all(c.props.get("frame_step") for c in clips)
        self.begin_edit()
        for c in clips:
            if on:
                c.props["frame_step"] = step
            else:
                c.props.pop("frame_step", None)
        self.end_edit(("カクカク" if on else "カクカクを外す") + f"（{len(clips)} 個）")
        return len(clips)

    # ─────────────────────────── 制作 ───────────────────────────
    def target_length_dialog(self):
        cur = float(self.project.settings.get("target_seconds", 0) or 0)
        txt, ok = QInputDialog.getText(self, "目標の長さ", "目標の長さ（分:秒。空 = 使わない）:",
                                       text=f"{int(cur // 60)}:{int(cur % 60):02d}" if cur else "10:00")
        if not ok:
            return
        txt = txt.strip()
        if not txt:
            self.set_target_length(0)
            return
        try:
            mm, _, ss = txt.partition(":")
            self.set_target_length(int(mm) * 60 + (int(ss) if ss else 0))
        except ValueError:
            QMessageBox.warning(self, "目標の長さ", "「10:00」のように入れてください。")

    def set_target_length(self, seconds: float):
        self.project.settings["target_seconds"] = float(seconds)
        self.dirty = True
        self.update_target_label()

    def update_target_label(self) -> str:
        tgt = float(self.project.settings.get("target_seconds", 0) or 0)
        if tgt <= 0:
            self.target_label.setText("")
            return ""
        seq = self.project.sequence
        now = seq.duration / seq.fps
        diff = tgt - now
        mmss = lambda s: f"{int(abs(s) // 60)}:{int(abs(s) % 60):02d}"
        txt = f"⏱ 目標 {mmss(tgt)} / 今 {mmss(now)}（" + (f"あと {mmss(diff)}" if diff >= 0 else f"{mmss(diff)} 超過") + "）"
        self.target_label.setText(txt)
        self.target_label.setStyleSheet("color: #ff6b5b;" if diff < 0 else "")
        return txt

    def backup_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "保存のたびにコピーを置くフォルダ（キャンセル = 使わない）",
                                             SETTINGS.backup_dir or "")
        SETTINGS.backup_dir = d or ""
        SETTINGS.save()
        self.status.showMessage(f"バックアップ先: {d}" if d else "保存時のバックアップを止めました", 4000)

    def after_save(self) -> Optional[str]:
        """保存のたびに、バックアップ先へ日時付きのコピーを置く（プロジェクトごとに新しい 20 個を残す）。"""
        d = SETTINGS.backup_dir
        if not d or not self.project_path:
            return None
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            stem = Path(self.project_path).stem
            dst = Path(d) / f"{stem}_{datetime.datetime.now():%Y%m%d_%H%M%S}.tcut"
            shutil.copy2(self.project_path, dst)
            old = sorted(Path(d).glob(f"{stem}_*.tcut"))
            for p in old[:-20]:
                p.unlink(missing_ok=True)
            return str(dst)
        except OSError as ex:
            self.status.showMessage(f"バックアップに失敗しました: {ex}", 6000)
            return None

    def export_name_dialog(self):
        txt, ok = QInputDialog.getText(
            self, "書き出しファイル名のルール",
            "使える言葉: {project} {seq} {date} {time} {user} {v}（版の番号・同じ名前があれば増える）",
            text=SETTINGS.export_name_template or "{seq}")
        if ok:
            SETTINGS.export_name_template = txt.strip() or "{seq}"
            SETTINGS.save()

    def export_file_name(self, folder: str, ext: str) -> str:
        """書き出しファイル名のルールで名前を作る（{v} は空いている番号）。"""
        seq = self.project.sequence
        now = datetime.datetime.now()
        rule = SETTINGS.export_name_template or "{seq}"
        vals = {"project": self.project.name or Path(self.project_path or "無題").stem, "seq": seq.name,
                "date": f"{now:%Y%m%d}", "time": f"{now:%H%M}", "user": SETTINGS.user_name or ""}
        clean = lambda s: re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "export"
        for v in range(1, 1000):
            name = clean(rule.format(**vals, v=v) if "{v}" in rule else rule.format(**vals, v=""))
            p = os.path.join(folder, name + ext)
            if "{v}" not in rule or not os.path.exists(p):
                return p
        return os.path.join(folder, clean(seq.name) + ext)

    def key_cheatsheet_dialog(self):
        p = self.key_cheatsheet(str(self._out_dir9("Reports") / "ショートカット早見表.html"))
        import webbrowser
        webbrowser.open(Path(p).as_uri())

    def key_cheatsheet(self, path: str) -> str:
        """今のキー設定を、印刷できる早見表（HTML）に。"""
        from .commands import COMMAND_INFO, CATEGORIES
        rows: Dict[str, List[str]] = {}
        for cid, keys in self.keymap.items():
            info = COMMAND_INFO.get(cid)
            if info and keys:
                rows.setdefault(info[2], []).append(
                    f"<tr><td>{html.escape(info[1])}</td><td>{' / '.join(f'<kbd>{html.escape(k)}</kbd>' for k in keys)}</td></tr>")
        body = "".join(f"<section><h2>{html.escape(cat)}</h2><table>{''.join(rows[cat])}</table></section>"
                       for cat in CATEGORIES if cat in rows)
        doc = f"""<!doctype html><meta charset="utf-8"><title>TeloCut ショートカット早見表</title>
<style>body{{font-family:"Yu Gothic UI",sans-serif;margin:16px;columns:3 300px;font-size:12px}}section{{break-inside:avoid;margin-bottom:12px}}
h1{{column-span:all;font-size:18px}}h2{{font-size:14px;border-bottom:2px solid #5b8cff;margin:4px 0}}table{{width:100%;border-collapse:collapse}}
td{{padding:2px 4px;border-bottom:1px solid #ddd}}kbd{{font-family:Consolas,monospace;background:#eef;border:1px solid #bbc;border-radius:3px;padding:0 4px}}</style>
<h1>TeloCut ショートカット早見表（{html.escape(SETTINGS.keyboard_preset)}・{html.escape(SETTINGS.current_profile)}）</h1>{body}"""
        Path(path).write_text(doc, encoding="utf-8")
        return path

    def copy_clip_info(self) -> str:
        """選んだクリップの情報（名前・TC・長さ・ファイル）をコピー（チャットで修正の場所を伝えるときに）。"""
        seq = self.project.sequence
        lines = []
        for t, c in sorted(self.canvas.selected_clips(), key=lambda x: x[1].start):
            m = self.project.media_by_id(c.media_id) if c.media_id else None
            name = c.name or (str(c.prop("text", "")).replace("\n", " ") if c.kind == "text" else c.kind)
            lines.append(f"{t.name}  {self.tc_str(c.start)} - {self.tc_str(c.end)}（{c.duration / seq.fps:.2f} 秒）  {name}"
                         + (f"  {m.path}" if m else ""))
        txt = "\n".join(lines) or f"{seq.name}  {self.tc_str(self.record_player.position)}"
        QApplication.clipboard().setText(txt)
        self.status.showMessage("クリップの情報をコピーしました", 3000)
        return txt

    def timeline_image(self) -> str:
        """タイムライン（見えている所）を画像にして保存・クリップボードへ（進み具合の共有に）。"""
        img = self.canvas.grab()
        p = str(self._out_dir9("Reports") / f"タイムライン_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
        img.save(p)
        QApplication.clipboard().setPixmap(img)
        self.status.showMessage(f"タイムラインを画像にしました（コピー済み）: {p}", 5000)
        return p
