"""使いやすい画面と、演出・整理の新機能（MainWindow のミックスイン）。

画面: ページ（編集 / テロップ / 音声 / エフェクト / 仕上げ）・やりたいこと検索・かんたんメニュー・スタート画面・
      フォーカスモード・パネルの配置ロック・ショートカットのヒント・ツールバーの大きさ・メニューの並びの整理
演出: ピー音・リプレイ・ツッコミ（止めて寄る）・ズームカット・場面転換（白フラッシュ / 黒）・チャンネル登録ボタン・
      目次スライドとチャプター見出し・ダイジェスト（予告）・盛り上がりにマーカー
テロップ: 表記チェック・句読点を整える・話者ごとのトラックに分ける・テロップに効果音
映像・音声: 黒帯を自動でカット・ワイプにする・ぼかし背景で埋める・BGM の終わりをそろえる・空白を挿入
整理: ビンを種類で整理・重複した素材をまとめる・作業レポート・書き出し完了の通知・セーフエリア / 画面比率のガイド
"""
from __future__ import annotations

import copy
import datetime
import math
import os
import re
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, QStringListModel, QTimer
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QDialog, QDialogButtonBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMessageBox, QPushButton, QSizePolicy, QTabBar, QVBoxLayout,
                             QWidget, QDockWidget)

from . import model as M
from .settings import SETTINGS

# ─────────────────────────── ページ（DaVinci Resolve のページのように、目的ごとのパネル配置）───────────────────────────
# 右・下に出すパネル（objectName）。最初のものを前に出す。プロジェクト・ビン・タイムラインはどのページでも出す。
PAGES: List[Tuple[str, str, List[str]]] = [
    ("edit", "編集", ["insp", "telops", "markers"]),
    ("telop", "テロップ", ["telop_editor", "telops", "insp", "presets"]),
    ("audio", "音声", ["mixer", "voice", "sepad", "insp"]),
    ("effects", "エフェクト", ["motion", "insp", "presets", "scopes", "graph", "nodes"]),
    ("finish", "仕上げ", ["scopes", "markers", "todo", "history", "farm", "insp"]),
]
ALWAYS = ("project", "bin", "tl")

# かんたんメニューで出すメニュー（ほかは隠す。設定 → ユーザー設定 → 画面と操作 で「すべて」に戻せる）
SIMPLE_MENUS = ("ファイル", "編集", "クリップ", "テロップ", "オーディオ", "シーケンス", "再生", "YouTube", "設定", "ウィンドウ", "ヘルプ")
# メニューの並び（同じ仕事が近くに来るように）。ここに無いものは最後（ヘルプの前）
MENU_ORDER = ("ファイル", "編集", "クリップ", "テロップ", "オーディオ", "シーケンス", "トリム", "再生", "レンダリング", "ツール",
              "YouTube", "モーション", "3D / MMD", "整理・チェック", "変換・仕上げ", "便利", "AI / 連携", "連携・管理",
              "設定", "ウィンドウ", "ヘルプ")

# 同じ名前のメニューを 1 つにするときの、サブメニューの名前
MORE_MENUS = {"クリップ": "ネスト・テイク・スピードランプ など"}

TOOLBAR_SIZES = {"small": (2, 8.5), "medium": (4, 9.5), "large": (8, 12.0)}

GUIDES = [("", "なし"), ("title", "タイトルセーフ（90% / 80%）"), ("shorts", "ショート / TikTok の UI の位置"),
          ("4:3", "4:3 の範囲"), ("1:1", "1:1（正方形）の範囲"), ("9:16", "9:16（縦）の範囲")]

FULLWIDTH_ALNUM = re.compile(r"[Ａ-Ｚａ-ｚ０-９]")
HALFWIDTH_KANA = re.compile(r"[ｦ-ﾟ]")
DOUBLED = re.compile(r"(の|が|を|に|は|て|で|と|も)\1")


def _beep_wav(path: str, seconds: float, freq: float = 1000.0, db: float = -12.0, rate: int = 48000) -> str:
    """ピー音（正弦波）の WAV を作る。端は 5ms でフェード（プツッと鳴らないように）。"""
    n = max(1, int(seconds * rate))
    t = np.arange(n) / rate
    y = np.sin(2 * math.pi * freq * t) * (10 ** (db / 20))
    f = min(n // 2, int(rate * 0.005))
    if f > 0:
        ramp = np.linspace(0, 1, f)
        y[:f] *= ramp
        y[-f:] *= ramp[::-1]
    pcm = (np.clip(y, -1, 1) * 32767).astype("<i2")
    st = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(st.tobytes())
    return path


def check_text(text: str) -> List[str]:
    """テロップ 1 つの表記の問題（誤字っぽい重なり・全角英数・半角カナ・カッコの対応・長すぎる行）。"""
    out = []
    if not text.strip():
        return ["文字がない"]
    m = DOUBLED.search(text)
    if m:
        out.append(f"「{m.group(0)}」が続いている（打ち間違い？）")
    if FULLWIDTH_ALNUM.search(text):
        out.append("全角の英数字がある")
    if HALFWIDTH_KANA.search(text):
        out.append("半角カナがある")
    for a, b in (("「", "」"), ("（", "）"), ("(", ")"), ("『", "』"), ("【", "】")):
        if text.count(a) != text.count(b):
            out.append(f"{a}{b} の数が合わない")
    for line in text.split("\n"):
        if len(line) > 26:
            out.append(f"1 行が長い（{len(line)} 文字）")
            break
    return out


def tidy_text(text: str, drop_period: bool = True, fullwidth_marks: bool = True, halfwidth_digits: bool = True,
              comma_space: bool = False) -> str:
    """句読点を整える（YouTube のテロップでよく使う形に）。"""
    s = text
    if drop_period:
        s = re.sub(r"。+(\s*)$", r"\1", s, flags=re.M)
        s = re.sub(r"。(\s*\n)", r"\1", s)
    if fullwidth_marks:
        s = s.replace("!", "！").replace("?", "？")
    if halfwidth_digits:
        s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    if comma_space:
        s = s.replace("、", " ")
    return s


class StartPanel(QDialog):
    """スタート画面（空のプロジェクトで最初にやることを大きなボタンで）。"""

    ITEMS = [("import", "📥", "素材を読み込む", "動画・画像・音声を選ぶ（ドラッグ＆ドロップでも）"),
             ("telop_template", "🅣", "テロップを置く", "14 種類のテンプレートから"),
             ("script_gen", "📝", "台本から作る", "ゆっくり・解説動画（音声・字幕・立ち絵）"),
             ("biim_layout", "⏱", "biim 式に配置", "ゲーム画面・RTA タイマー・立ち絵"),
             ("auto_clip", "✂", "配信を切り抜く", "見どころ探し・無音カット・字幕まで自動"),
             ("guidebook", "📖", "ガイドブック", "はじめての人向けの使い方")]

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("スタート")
        self.setModal(False)
        v = QVBoxLayout(self)
        head = QLabel("<h2>なにを作りますか？</h2>")
        v.addWidget(head)
        g = QGridLayout()
        for i, (cid, icon, title, sub) in enumerate(self.ITEMS):
            b = QPushButton(f"{icon}  {title}\n{sub}")
            b.setObjectName("start_" + cid)
            b.setMinimumSize(250, 74)
            b.setStyleSheet("QPushButton { text-align: left; padding: 10px 14px; font-size: 11pt; }")
            b.clicked.connect(lambda _=False, c=cid: self._go(c))
            g.addWidget(b, i // 2, i % 2)
        v.addLayout(g)
        self.again = QCheckBox("次から表示しない（ヘルプ → スタート画面 でいつでも開けます）")
        self.again.setChecked(not SETTINGS.show_start_panel)
        v.addWidget(self.again)
        h = QHBoxLayout()
        h.addStretch(1)
        close = QPushButton("閉じる")
        close.clicked.connect(self.close)
        h.addWidget(close)
        v.addLayout(h)

    def _go(self, cid: str):
        self.close()
        self.mw.run_command(cid)

    def closeEvent(self, e):
        if SETTINGS.show_start_panel == self.again.isChecked():
            SETTINGS.show_start_panel = not self.again.isChecked()
            SETTINGS.save()
        super().closeEvent(e)


class Features9Mixin:
    def _feature9_commands(self) -> dict:
        return {
            "page_edit": lambda: self.set_page("edit"), "page_telop": lambda: self.set_page("telop"),
            "page_audio": lambda: self.set_page("audio"), "page_effects": lambda: self.set_page("effects"),
            "page_finish": lambda: self.set_page("finish"), "find_action": self.focus_search,
            "menu_mode": self.toggle_menu_mode, "start_panel": self.show_start_panel,
            "focus_mode": self.toggle_focus_mode, "lock_layout": self.toggle_lock_layout,
            "bleep": self.bleep, "replay": self.replay_dialog, "tsukkomi": self.tsukkomi_dialog,
            "zoom_cut": self.zoom_cut_dialog, "transition_flash": lambda: self.transition_at_playhead("white"),
            "transition_black": lambda: self.transition_at_playhead("black"), "subscribe_button": self.subscribe_button,
            "chapter_slides": self.chapter_slides_dialog, "digest": self.digest_dialog,
            "hype_markers": self.hype_markers, "telop_check": self.telop_check_dialog,
            "telop_tidy": self.telop_tidy_dialog, "subs_by_speaker": self.subs_by_speaker,
            "telop_se": self.telop_se_dialog, "auto_crop": self.auto_crop, "wipe": self.wipe,
            "blur_fill": self.blur_fill, "bgm_end": self.bgm_end, "insert_space": self.insert_space_dialog,
            "bins_by_kind": self.bins_by_kind, "merge_duplicates": self.merge_duplicate_media,
            "work_report": self.work_report_dialog, "monitor_guide": self.monitor_guide_dialog,
        }

    def _build_feature9_menus(self):
        def into(title: str, items, sub: str = ""):
            m = self._menu_named(title)
            if m is None:
                return
            m.addSeparator()
            if sub:
                m = m.addMenu(sub)
            for c in items:
                m.addSeparator() if c is None else self._a(m, c)
        into("YouTube", ("bleep", "replay", "tsukkomi", "zoom_cut", "transition_flash", "transition_black", None,
                         "subscribe_button", "chapter_slides", "digest", "hype_markers"), "演出（ピー音・リプレイ・ツッコミ など）")
        into("テロップ", ("telop_check", "telop_tidy", "subs_by_speaker", "telop_se"))
        into("クリップ", ("auto_crop", "wipe", "blur_fill"))
        into("オーディオ", ("bgm_end",))
        into("シーケンス", ("insert_space",))
        into("整理・チェック", ("bins_by_kind", "merge_duplicates", "work_report"))
        into("再生", ("monitor_guide",))
        into("ウィンドウ", ("page_edit", "page_telop", "page_audio", "page_effects", "page_finish"), "ページ")
        into("ウィンドウ", ("focus_mode", "lock_layout", "menu_mode"))
        into("ヘルプ", ("start_panel", "find_action"))
        self.reorganize_menus()

    # ─────────────────────────── 画面の部品 ───────────────────────────
    def _menu_named(self, title: str):
        for a in self.menuBar().actions():
            if a.menu() is not None and a.text() == title:
                return a.menu()
        return None

    def reorganize_menus(self):
        """メニューの並びを仕事の順に（同じ名前のメニューは 1 つにまとめる）。"""
        mb = self.menuBar()
        acts = [a for a in mb.actions() if a.menu() is not None]
        by: Dict[str, object] = {}
        for a in acts:
            t = a.text()
            if t in by:                                # 同じ名前（例: クリップ が 2 つ）→ 前のもののサブメニューにして消す
                dst = by[t].menu()
                dst.addSeparator()
                sub_menu = dst.addMenu(MORE_MENUS.get(t, "もっと"))
                for sub in list(a.menu().actions()):
                    sub_menu.addAction(sub)
                mb.removeAction(a)
            else:
                by[t] = a
        order = [t for t in MENU_ORDER if t in by] + [t for t in by if t not in MENU_ORDER and t != "ヘルプ"]
        if "ヘルプ" in by and "ヘルプ" not in order:
            order.append("ヘルプ")
        order = [t for t in order if t != "ヘルプ"] + (["ヘルプ"] if "ヘルプ" in by else [])
        for t in order:
            mb.removeAction(by[t])
        for t in order:
            mb.addAction(by[t])
        self._menu_actions = by

    def _init_feature9_ui(self):
        """ツールバーにページの切り替えと検索欄を足す・画面の設定を当てる（ツールバーを作ったあとに呼ぶ）。"""
        tb = self.toolbar
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.search_box = QLineEdit()
        self.search_box.setObjectName("find_action")
        self.search_box.setPlaceholderText("🔍 やりたいこと（例: テロップ、書き出し、ピー音）")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(240)
        self.search_box.setMaximumWidth(360)
        self._search_model = QStringListModel()
        comp = QCompleter(self._search_model, self.search_box)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setMaxVisibleItems(14)
        comp.activated.connect(self._search_run)
        self.search_box.setCompleter(comp)
        self.search_box.returnPressed.connect(lambda: self._search_run(self.search_box.text()))
        self.search_box.textEdited.connect(self._refresh_search_model)
        tb.addWidget(self.search_box)
        self.page_bar = QTabBar()
        self.page_bar.setObjectName("page_bar")
        self.page_bar.setDrawBase(False)
        self.page_bar.setExpanding(False)
        for key, label, _docks in PAGES:
            self.page_bar.addTab(label)
            self.page_bar.setTabData(self.page_bar.count() - 1, key)
            self.page_bar.setTabToolTip(self.page_bar.count() - 1, f"{label}のページ（パネルの配置を切り替え）")
        self.page_bar.currentChanged.connect(lambda i: self.set_page(self.page_bar.tabData(i)))
        tb.addWidget(self.page_bar)
        self._refresh_search_model()
        self._focus_saved = None
        for a in self.menuBar().actions():
            if a.menu() is not None:
                a.menu().triggered.connect(self._hint_shortcut)
        for btn, a in getattr(self, "_tb_buttons", []):
            btn.clicked.connect(lambda _=False, a=a: self._hint_shortcut(a))
        self.apply_ui_prefs()

    def apply_ui_prefs(self):
        """ユーザー設定の「画面と操作」を当てる（メニュー・ツールバー・パネルのロック）。"""
        self.apply_menu_mode()
        self.apply_toolbar_style()
        self.apply_lock_layout()

    def _menu_only_actions(self):
        """コマンド一覧に無い、メニューだけの項目（名前, QAction, メニューの道筋）。"""
        known = set(id(a) for a in self.actions.values())
        out = []

        def walk(menu, path):
            for a in menu.actions():
                if a.isSeparator():
                    continue
                if a.menu() is not None:
                    walk(a.menu(), path + [a.text()])
                elif id(a) not in known and a.text().strip() and not a.text().startswith("（"):
                    out.append((a.text().replace("&", ""), a, " → ".join(path)))
        for top in self.menuBar().actions():
            if top.menu() is not None:
                walk(top.menu(), [top.text()])
        return out

    def _labels(self) -> Dict[str, object]:
        from .commands import COMMAND_INFO
        out = {}
        for text, act, path in self._menu_only_actions():
            out[f"{text}  — {path}"] = act
        for cid, a in self.actions.items():
            info = COMMAND_INFO.get(cid)
            if info is None:
                continue
            keys = self.keymap.get(cid, []) if hasattr(self, "keymap") else []
            out[f"{info[1]}  — {info[2]}" + (f"  [{keys[0]}]" if keys else "")] = cid
        return out

    def _refresh_search_model(self, text: str = ""):
        """候補の並び: 名前が入力で始まる → 言葉の頭で合う → 含む。同じなら最近使ったものが先。"""
        lab = self._labels()
        self._search_index = lab
        recent = [c for c in SETTINGS.recent_commands if c in self.actions]
        rank = {c: i for i, c in enumerate(recent)}
        t = text.strip().lower()

        def score(k: str):
            name = k.lower()
            if not t:
                hit = 0
            elif name.startswith(t):
                hit = 0
            elif re.search(r"(^|[\s（(・:：/])" + re.escape(t), name):
                hit = 1
            else:
                hit = 2
            return (hit, rank.get(lab[k], 99) if isinstance(lab[k], str) else 100, k)
        self._search_model.setStringList(sorted(lab, key=score))

    def search_commands(self, text: str) -> List[str]:
        """検索欄と同じ探し方（名前・分類・キーに含まれる）。"""
        t = text.strip().lower()
        self._refresh_search_model(text)
        return [self._search_index[s] for s in self._search_model.stringList() if t and t in s.lower()]

    def _search_run(self, text: str):
        cid = self._search_index.get(text) if hasattr(self, "_search_index") else None
        if cid is None:
            hits = self.search_commands(text)
            cid = hits[0] if hits else None
        if cid is None:
            self.status.showMessage(f"「{text}」に合う機能がありません", 3000)
            return
        QTimer.singleShot(0, self.search_box.clear)
        if isinstance(cid, str):
            self.run_command(cid)
        else:
            cid.trigger()                               # メニューだけの項目

    def focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def run_command(self, cid: str):
        """コマンドを実行して、最近使ったものに残す（検索欄の上に出る）。"""
        fn = self.cmd_fns.get(cid)
        if fn is None:
            return None
        rc = [c for c in SETTINGS.recent_commands if c != cid]
        SETTINGS.recent_commands = ([cid] + rc)[:12]
        return fn()

    def _hint_shortcut(self, action):
        """メニュー・ボタンから使ったとき、キーがあれば教える（覚えると速くなる）。"""
        if not SETTINGS.shortcut_hints:
            return
        cid = next((c for c, a in self.actions.items() if a is action), None)
        if cid is None:
            return
        rc = [c for c in SETTINGS.recent_commands if c != cid]
        SETTINGS.recent_commands = ([cid] + rc)[:12]
        keys = self.keymap.get(cid, [])
        if keys:
            self.status.showMessage(f"ヒント: 「{action.text()}」は {keys[0]} キーでも使えます", 5000)

    # ── ページ ──
    def set_page(self, key: str) -> List[str]:
        """目的ごとのパネル配置にする。出したパネルの objectName を返す。"""
        page = next((p for p in PAGES if p[0] == key), None)
        if page is None:
            return []
        if getattr(self, "_focus_saved", None) is not None:
            self.toggle_focus_mode()
        names = set(ALWAYS) | set(page[2])
        shown = []
        for d in self.docks:
            n = d.objectName()
            if n in names:
                d.show()
                shown.append(n)
            elif not getattr(self, "dual_mode", False):          # 複数画面のときは配置を崩さない（出すだけ）
                d.hide()
        front = next((d for d in self.docks if d.objectName() == page[2][0]), None)
        if front is not None:
            front.raise_()
        if SETTINGS.page != key:
            SETTINGS.page = key
        bar = getattr(self, "page_bar", None)
        if bar is not None:
            i = [p[0] for p in PAGES].index(key)
            if bar.currentIndex() != i:
                bar.blockSignals(True)
                bar.setCurrentIndex(i)
                bar.blockSignals(False)
        if hasattr(self, "fill_page_toolbar") and hasattr(self, "page_toolbar"):
            self.fill_page_toolbar(key)
        self.status.showMessage(f"{page[1]}のページ", 2000)
        return shown

    # ── メニュー・ツールバー・ロック ──
    def apply_menu_mode(self):
        simple = SETTINGS.menu_mode == "simple"
        for a in self.menuBar().actions():
            if a.menu() is not None:
                a.setVisible(not simple or a.text() in SIMPLE_MENUS)

    def toggle_menu_mode(self) -> str:
        SETTINGS.menu_mode = "full" if SETTINGS.menu_mode == "simple" else "simple"
        SETTINGS.save()
        self.apply_menu_mode()
        self.status.showMessage("メニュー: " + ("かんたん（よく使うものだけ）" if SETTINGS.menu_mode == "simple" else "すべて"), 3000)
        return SETTINGS.menu_mode

    def apply_toolbar_style(self):
        pad, pt = TOOLBAR_SIZES.get(SETTINGS.toolbar_size, TOOLBAR_SIZES["medium"])
        self.toolbar.setStyleSheet(f"QToolButton {{ padding: {pad}px {pad + 4}px; font-size: {pt:g}pt; }}")
        for btn, a in getattr(self, "_tb_buttons", []):
            full = btn.property("full_text") or btn.text()
            btn.setProperty("full_text", full)
            head, _, rest = full.partition(" ")
            btn.setText(full if SETTINGS.toolbar_text or not rest else head)

    def apply_lock_layout(self):
        lock = bool(SETTINGS.lock_layout)
        F = QDockWidget.DockWidgetFeature
        for d in self.docks:
            d.setFeatures(F.DockWidgetClosable if lock else (F.DockWidgetMovable | F.DockWidgetFloatable | F.DockWidgetClosable))
        self.toolbar.setMovable(not lock)

    def toggle_lock_layout(self) -> bool:
        SETTINGS.lock_layout = not SETTINGS.lock_layout
        SETTINGS.save()
        self.apply_lock_layout()
        self.status.showMessage("パネルの配置をロックしました" if SETTINGS.lock_layout else "パネルの配置のロックを外しました", 3000)
        return SETTINGS.lock_layout

    def toggle_focus_mode(self) -> bool:
        """モニターとタイムラインだけにする / 戻す。"""
        if self._focus_saved is None:
            self._focus_saved = [d for d in self.docks if d.isVisible()]
            for d in self.docks:
                if d.objectName() != "tl":
                    d.hide()
            self.dock_tl.show()
            self.status.showMessage("フォーカスモード（もう一度で元に戻ります）", 3000)
            return True
        for d in self._focus_saved:
            d.show()
        self._focus_saved = None
        return False

    def show_start_panel(self, force: bool = True) -> Optional[StartPanel]:
        if not force and not SETTINGS.show_start_panel:
            return None
        p = getattr(self, "_start_panel", None)
        if p is None:
            p = self._start_panel = StartPanel(self)
        p.show()
        p.raise_()
        return p

    def maybe_start_panel(self):
        """新しい（空の）プロジェクトならスタート画面を出す。"""
        if all(not t.clips for t in self.project.sequence.tracks()) and not self.project.media:
            QTimer.singleShot(300, lambda: self.show_start_panel(force=False))

    # ── 書き出しの完了 ──
    def on_export_done(self, path: str):
        """書き出しが終わったら知らせる（音・タスクバーの点滅・設定でフォルダを開く）。"""
        if path:
            SETTINGS.last_export = str(path)                 # YouTube にアップロードの既定のファイル
        if SETTINGS.notify_export:
            QApplication.beep()
            QApplication.alert(self, 0)
            self.status.showMessage(f"書き出しが終わりました: {path}", 10000)
        if SETTINGS.open_folder_after_export and path:
            folder = str(Path(path).parent)
            if os.name == "nt":
                os.startfile(folder)                                  # noqa: S606
        return path

    # ─────────────────────────── モニターのガイド ───────────────────────────
    def monitor_guide_dialog(self):
        names = [n for _k, n in GUIDES]
        cur = [k for k, _n in GUIDES].index(getattr(self.record_mon.view, "guide", "") or "")
        n, ok = QInputDialog.getItem(self, "ガイドを表示", "モニターに出すガイド:", names, cur, False)
        if ok:
            self.set_monitor_guide(GUIDES[names.index(n)][0])

    def set_monitor_guide(self, kind: str) -> str:
        for mon in (self.record_mon, self.source_mon):
            mon.view.guide = kind
            mon.view.update()
        return kind

    # ─────────────────────────── 共通 ───────────────────────────
    def _rng9(self, default_sec: float = 0.0) -> Tuple[int, int]:
        seq = self.project.sequence
        if 0 <= seq.mark_in < seq.mark_out:
            return seq.mark_in, seq.mark_out
        f = self.record_player.position
        if default_sec > 0:
            return max(0, f - int(round(default_sec * seq.fps))), f
        return f, f

    def _top_track(self, name: str) -> M.Track:
        seq = self.project.sequence
        t = M.Track("video", name, selected=False)
        seq.video_tracks.append(t)
        return t

    def _audio_track(self, name: str, a: int, b: int) -> M.Track:
        """name のトラックで a〜b が空いていればそこ、無ければ新しく作る。"""
        seq = self.project.sequence
        for t in seq.audio_tracks:
            if t.name == name and not t.locked and t.is_empty(a, b):
                return t
        t = M.Track("audio", name, selected=False)
        seq.audio_tracks.append(t)
        return t

    def _add_media(self, path: str, bin_name: str) -> M.MediaItem:
        """素材を足す（同じファイルがもうあれば、それを使う）。"""
        from . import media as MD
        key = os.path.normcase(os.path.abspath(path))
        for m in self.project.media:
            if m.path and os.path.normcase(os.path.abspath(m.path)) == key:
                return m
        info = MD.probe(path)
        info.id = M.new_id()
        if info.kind in ("audio", "video"):
            MD.build_audio_cache(path)
        self.project.media.append(info)
        self.project.bin_by_id(self.named_bin(bin_name)).add("media", info.id)
        return info

    def _clip_under_playhead(self, kinds=("video",)) -> Tuple[Optional[M.Track], Optional[M.Clip]]:
        seq = self.project.sequence
        f = self.record_player.position
        for t in reversed(seq.video_tracks):
            if t.locked or t.muted:
                continue
            c = t.clip_at(f)
            if c is not None and c.kind in kinds:
                return t, c
        return None, None

    def _out_dir9(self, sub: str) -> Path:
        d = (Path(self.project_path).parent / sub) if self.project_path else Path(SETTINGS.cache_dir) / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ─────────────────────────── 演出 ───────────────────────────
    def bleep(self) -> Optional[M.Clip]:
        """IN〜OUT の声を消して、ピー音（1kHz）を入れる（放送禁止用語・個人名など）。"""
        seq = self.project.sequence
        a, b = self._rng9()
        if b <= a:
            QMessageBox.information(self, "ピー音", "消したい所に IN と OUT を付けてください。")
            return None
        path = str(self._out_dir9("SE") / f"beep_{(b - a)}f_{M.new_id()[:4]}.wav")
        _beep_wav(path, (b - a) / seq.fps)
        self.begin_edit()
        info = self._add_media(path, "効果音")
        for t in seq.audio_tracks:
            if t.locked or t.name == "ピー":
                continue
            M.split_clip(t, a)
            M.split_clip(t, b)
            for c in t.clips:
                if c.start >= a and c.end <= b:
                    c.enabled = False
        tr = self._audio_track("ピー", a, b)
        c = M.Clip(M.new_id(), "audio", a, b - a, 0, info.id, "ピー")
        tr.clips.append(c)
        tr.sort()
        self.end_edit("ピー音（元の声は無効に）")
        self.bin.refresh()
        return c

    def replay_dialog(self):
        sec, ok = QInputDialog.getDouble(self, "リプレイ", "IN〜OUT が無いとき、再生ヘッドの前の何秒をもう一度流しますか:",
                                         3.0, 0.5, 30.0, 1)
        if ok:
            sp, ok2 = QInputDialog.getItem(self, "リプレイ", "速さ:", ["0.5 倍（スロー）", "1 倍", "0.25 倍"], 0, False)
            if ok2:
                self.replay(sec, {"0.5 倍（スロー）": 0.5, "1 倍": 1.0, "0.25 倍": 0.25}[sp])

    def replay(self, seconds: float = 3.0, speed: float = 0.5) -> Optional[M.Clip]:
        """今の場面をもう一度（スローで）流す。後ろは押し出す・「REPLAY」の表示付き。"""
        from . import render as R
        seq = self.project.sequence
        a, b = self._rng9(seconds)
        t, c = None, None
        for tr in reversed(seq.video_tracks):
            cc = tr.clip_at(max(a, b - 1))
            if cc is not None and cc.kind == "video" and not tr.locked:
                t, c = tr, cc
                break
        if c is None or b <= a:
            QMessageBox.information(self, "リプレイ", "映像のある所で使ってください（IN〜OUT か、再生ヘッドの前）。")
            return None
        a = max(a, c.start)
        base_sp = float(c.prop("speed", 1.0) or 1.0)
        dur = int(round((b - a) / speed))
        rp = c.copy(id=M.new_id(), start=b, duration=dur, link="", src_in=int(R.src_frame(c, a)))
        rp.props["speed"] = round(base_sp * speed, 4)
        rp.props["dissolve"] = 0
        rp.props.pop("ramp", None)
        rp.name = (c.name or "") + "（リプレイ）"
        self.begin_edit()
        M.place_clips(seq, [(t, rp)], "insert")
        lab = self._top_track("リプレイ")
        lab.clips.append(M.Clip(M.new_id(), "text", b, dur, name="REPLAY",
                                props={"text": "▶ REPLAY", "text_anim": "fade",
                                       "style": {"font_size": 56, "bold": True, "font_color": "#ffffff",
                                                 "stroke_enabled": True, "stroke_color": "#d01c1c", "stroke_width": 8,
                                                 "position_h": "left", "position_v": "top", "offset_x": 60, "offset_y": 50}}))
        self.end_edit(f"リプレイ（{(b - a) / seq.fps:.1f} 秒を {speed:g} 倍で）")
        return rp

    def tsukkomi_dialog(self):
        text, ok = QInputDialog.getText(self, "ツッコミ（止めて寄る）", "ツッコミの文字:", text="なんでやねん！")
        if ok:
            self.tsukkomi(text)

    def tsukkomi(self, text: str = "なんでやねん！", seconds: float = 1.5, zoom: float = 1.35) -> Optional[M.Clip]:
        """再生ヘッドで映像を止めて、寄りながら集中線とツッコミのテロップ（バラエティ番組の演出）。"""
        from . import render as R
        from . import templates as TP
        seq = self.project.sequence
        f = self.record_player.position
        t, c = self._clip_under_playhead(("video", "image"))
        if c is None:
            QMessageBox.information(self, "ツッコミ", "再生ヘッドの位置に映像を置いてください。")
            return None
        dur = int(round(seconds * seq.fps))
        fr = c.copy(id=M.new_id(), start=f, duration=dur, link="",
                    src_in=int(R.src_frame(c, f)) if c.kind == "video" else c.src_in)
        if c.kind == "video":
            fr.props["speed"] = 0.0
        base = float(c.prop("scale", 1.0) or 1.0)
        fr.props["anim"] = {"scale": [base, base * zoom], "_ease": "ease_out"}
        fr.props["effects"] = list(c.prop("effects") or []) + [{"name": "speedlines", "on": True}]
        fr.props["dissolve"] = 0
        fr.name = (c.name or "") + "（ツッコミ）"
        self.begin_edit()
        M.place_clips(seq, [(t, fr)], "insert")
        tp = next((x for x in TP.telop_templates() if x["name"].startswith("ツッコミ")), None)
        if tp is not None:
            TP.apply_telop(seq, tp, f, {"ツッコミ": text}, seconds)
        self.end_edit("ツッコミ（止めて寄る）")
        return fr

    def zoom_cut_dialog(self):
        z, ok = QInputDialog.getInt(self, "ズームカット", "寄るカットの大きさ（%）:", 115, 101, 200)
        if ok:
            self.zoom_cut(z / 100)

    def zoom_cut(self, zoom: float = 1.15) -> int:
        """選んだカット（無ければ IN〜OUT / 全体の V1）を 1 つおきに少し寄せる（トークの単調さを消す「ジャンプズーム」）。"""
        seq = self.project.sequence
        clips = [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image")]
        if not clips and seq.video_tracks:
            a, b = self._rng9()
            if b <= a:
                a, b = 0, seq.duration
            clips = [c for c in seq.video_tracks[0].clips if c.kind in ("video", "image") and c.start < b and c.end > a]
        clips.sort(key=lambda c: c.start)
        self.begin_edit()
        n = 0
        for i, c in enumerate(clips):
            base = float(c.props.get("zoomcut_base", c.prop("scale", 1.0)) or 1.0)
            c.props["zoomcut_base"] = base
            c.props["scale"] = round(base * zoom, 4) if i % 2 == 1 else base
            n += i % 2
        self.end_edit(f"ズームカット（{n} カットを {zoom * 100:.0f}% に）")
        return n

    def transition_at_playhead(self, kind: str = "white", seconds: float = 0.4) -> List[M.Clip]:
        """場面転換: 選んだクリップの頭（無ければ再生ヘッド）に、白フラッシュ / 黒にフェード。"""
        seq = self.project.sequence
        frames = sorted({c.start for _, c in self.canvas.selected_clips() if c.start > 0}) or [self.record_player.position]
        half = max(1, int(round(seconds * seq.fps / (2 if kind == "white" else 1))))
        col = "#FFFFFF" if kind == "white" else "#000000"
        self.begin_edit()
        tr = self._top_track("転換")
        made = []
        for f in frames:
            a = max(0, f - half)
            c = M.Clip(M.new_id(), "color", a, f + half - a, name="白フラッシュ" if kind == "white" else "黒",
                       props={"color": col, "fade_in": f - a, "fade_out": half})
            tr.clips.append(c)
            made.append(c)
        tr.sort()
        self.end_edit(("白フラッシュ" if kind == "white" else "黒にフェード") + f"（{len(made)} か所）")
        return made

    def subscribe_button(self, seconds: float = 4.0) -> M.Clip:
        """チャンネル登録ボタン（左下・字幕の上。ポンと出て、途中で押されて「登録済み」になる）。"""
        seq = self.project.sequence
        f = self.record_player.position
        self.begin_edit()
        tr = self._top_track("登録ボタン")
        c = M.Clip(M.new_id(), "shape", f, int(round(seconds * seq.fps)), name="チャンネル登録",
                   props={"shape": "subscribe", "rect": [4, 62, 26, 10], "color": "#e62117", "fill_opacity": 1.0,
                          "label": "チャンネル登録", "label_done": "登録済み", "clip_in": "pop", "clip_out": "fade"})
        tr.clips.append(c)
        self.end_edit("チャンネル登録ボタン")
        return c

    def chapter_slides_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("目次とチャプター見出し")
        f = QFormLayout(dlg)
        toc = QCheckBox("目次スライド（再生ヘッドに 5 秒）")
        toc.setChecked(True)
        heads = QCheckBox("各チャプターの頭に見出し（左上に 3 秒）")
        heads.setChecked(True)
        f.addRow(toc)
        f.addRow(heads)
        f.addRow(QLabel("マーカーの名前がチャプター名になります（名前の無いマーカーは使いません）。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.chapter_slides(toc.isChecked(), heads.isChecked())

    def chapter_slides(self, toc: bool = True, heads: bool = True) -> List[M.Clip]:
        seq = self.project.sequence
        ms = [m for m in sorted(seq.markers, key=lambda m: m.frame) if (m.name or "").strip()]
        if not ms:
            QMessageBox.information(self, "目次", "名前の付いたマーカーがありません（マーカー一覧で名前を付けてください）。")
            return []
        made = []
        self.begin_edit()
        tr = self._top_track("目次・見出し")
        if toc:
            f = self.record_player.position
            lines = "\n".join(f"{i}. {m.name.strip()}" for i, m in enumerate(ms, 1))
            c = M.Clip(M.new_id(), "text", f, int(seq.fps * 5), name="目次",
                       props={"text": "目次\n" + lines, "text_anim": "fade", "text_anim_out": "fade",
                              "style": {"font_size": max(30, min(60, int(seq.height / (len(ms) + 3) * 0.6))),
                                        "position_v": "middle", "offset_y": 0, "bg_enabled": True, "bg_color": "#101018",
                                        "bg_opacity": 200, "bg_padding": 30, "stroke_enabled": False}})
            tr.clips.append(c)
            made.append(c)
        if heads:
            for i, m in enumerate(ms, 1):
                d = int(seq.fps * 3)
                if not tr.is_empty(m.frame, m.frame + d):
                    tr = self._top_track("目次・見出し")
                c = M.Clip(M.new_id(), "text", m.frame, d, name=f"見出し {i}",
                           props={"text": f"{i}. {m.name.strip()}", "text_anim": "slide", "text_anim_out": "fade",
                                  "style": {"font_size": 46, "bold": True, "position_h": "left", "position_v": "top",
                                            "offset_x": 50, "offset_y": 50, "bg_enabled": True, "bg_color": "#000000",
                                            "bg_opacity": 160, "bg_padding": 14, "stroke_enabled": False}})
                tr.clips.append(c)
                made.append(c)
        tr.sort()
        self.end_edit(f"目次・チャプター見出し（{len(made)} 個）")
        return made

    def digest_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("ダイジェスト（予告）を作る")
        f = QFormLayout(dlg)
        pre, post = QDoubleSpinBox(), QDoubleSpinBox()
        for s, v in ((pre, 2.0), (post, 4.0)):
            s.setRange(0, 60)
            s.setSuffix(" 秒")
            s.setValue(v)
        f.addRow("マーカーの前", pre)
        f.addRow("マーカーの後", post)
        f.addRow(QLabel("マーカーの周りをつないで、新しいシーケンスにします（元のシーケンスはそのまま）。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.digest(pre.value(), post.value())

    def digest(self, pre: float = 2.0, post: float = 4.0) -> Optional[M.Sequence]:
        """マーカーの前後をつないだダイジェスト（予告・ハイライト）のシーケンス。"""
        src = self.project.sequence
        if not src.markers:
            QMessageBox.information(self, "ダイジェスト", "マーカーがありません（見せたい所にマーカーを付けてください）。")
            return None
        rngs = []
        for m in sorted(src.markers, key=lambda m: m.frame):
            a = max(0, m.frame - int(round(pre * src.fps)))
            b = min(src.duration, m.frame + int(round(post * src.fps)))
            if b <= a:
                continue
            if rngs and a <= rngs[-1][1]:
                rngs[-1] = (rngs[-1][0], max(rngs[-1][1], b))
            else:
                rngs.append((a, b))
        if not rngs:
            return None
        self.begin_edit()
        ns = M.Sequence.create(name=f"{src.name} ダイジェスト", fps=src.fps, width=src.width, height=src.height,
                               start_tc=src.start_tc)
        ns.video_tracks = [M.Track("video", t.name, selected=(i == 0)) for i, t in enumerate(src.video_tracks)]
        ns.audio_tracks = [M.Track("audio", t.name, selected=(i < 2)) for i, t in enumerate(src.audio_tracks)]
        off = 0
        for a, b in rngs:
            for srcs, dsts in ((src.video_tracks, ns.video_tracks), (src.audio_tracks, ns.audio_tracks)):
                for st, dt in zip(srcs, dsts):
                    tmp = M.Track(st.kind, st.name, clips=[c.copy() for c in st.clips])
                    M.split_clip(tmp, a)
                    M.split_clip(tmp, b)
                    links: Dict[str, str] = {}
                    for c in tmp.clips:
                        if c.start >= a and c.end <= b:
                            nl = links.setdefault(c.link, M.new_id()) if c.link else ""
                            dt.clips.append(c.copy(id=M.new_id(), start=c.start - a + off, link=nl))
            off += b - a
        for t in ns.tracks():
            t.sort()
        self.project.sequences.append(ns)
        (self.project.bin_of(src.id) or self.project.bins[0]).add("sequence", ns.id)
        self.end_edit(f"ダイジェスト（{len(rngs)} 場面・{off / src.fps:.1f} 秒）")
        self.bin.refresh()
        return ns

    def hype_markers(self, count: int = 8, min_gap_s: float = 20.0) -> List[M.Marker]:
        """シーケンスの音から盛り上がり（声が大きい・笑い・驚き）を探して、マーカーを付ける。"""
        from . import autoclip as AC
        from .smartrender import write_audio
        seq = self.project.sequence
        if seq.duration <= 0:
            return []
        tmp = str(self._out_dir9("Analysis") / f"hype_{M.new_id()[:6]}.wav")
        write_audio(self.project, seq, 0, seq.duration, tmp)
        try:
            ld = AC.measure(tmp)
            sc = AC.score_series(ld)["score"]
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        picked: List[int] = []
        for i in np.argsort(-sc):
            if len(picked) >= count or sc[i] < 35:
                break
            if all(abs(int(i) - p) >= min_gap_s for p in picked):
                picked.append(int(i))
        self.begin_edit()
        made = []
        for sec in sorted(picked):
            f = int(round(sec * seq.fps))
            if any(abs(m.frame - f) < seq.fps for m in seq.markers):
                continue
            mk = M.Marker(f, f"盛り上がり {sc[sec]:.0f}", "#FF7A00", "音の大きさ・急な変化から")
            seq.markers.append(mk)
            made.append(mk)
        seq.markers.sort(key=lambda m: m.frame)
        self.end_edit(f"盛り上がりにマーカー（{len(made)} 個）")
        return made

    # ─────────────────────────── テロップ ───────────────────────────
    def telop_check(self) -> List[Tuple[M.Clip, str]]:
        clips = sorted((c for _, c in self.project.sequence.all_clips() if c.kind == "text"), key=lambda c: c.start)
        out = []
        periods = [str(c.prop("text", "")).rstrip().endswith("。") for c in clips if str(c.prop("text", "")).strip()]
        mixed = 0 < sum(periods) < len(periods)
        for c in clips:
            txt = str(c.prop("text", ""))
            probs = check_text(txt)
            if mixed and txt.rstrip().endswith("。"):
                probs.append("「。」で終わるテロップと終わらないテロップが混ざっている")
            if c.duration < self.project.sequence.fps * 0.7 and txt.strip():
                probs.append("表示が短い（0.7 秒未満）")
            out += [(c, p) for p in probs]
        return out

    def telop_check_dialog(self):
        found = self.telop_check()
        dlg = QDialog(self)
        dlg.setWindowTitle(f"テロップの表記チェック（{len(found)} 件）")
        dlg.resize(640, 460)
        v = QVBoxLayout(dlg)
        lw = QListWidget()
        for c, p in found:
            it = QListWidgetItem(f"{self.tc_str(c.start)}  {p}  —  {str(c.prop('text', ''))[:30]}")
            it.setData(Qt.ItemDataRole.UserRole, c.id)
            lw.addItem(it)
        if not found:
            lw.addItem("問題は見つかりませんでした")

        def jump(it):
            cid = it.data(Qt.ItemDataRole.UserRole)
            _t, c = self.project.sequence.find(cid) if cid else (None, None)
            if c is not None:
                self.seek_record(c.start)
                self.canvas.selected = {c.id}
                self.canvas.selection_changed.emit()
        lw.itemDoubleClicked.connect(jump)
        v.addWidget(QLabel("ダブルクリックでその場所へ。直しやすい所は「テロップ → 句読点を整える」も使えます。"))
        v.addWidget(lw, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        dlg.exec()

    def telop_tidy_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("句読点を整える")
        f = QFormLayout(dlg)
        boxes = [QCheckBox("行の終わりの「。」を消す"), QCheckBox("! ? を全角（！？）に"), QCheckBox("全角の数字を半角に"),
                 QCheckBox("「、」をスペースに")]
        for b, on in zip(boxes, (True, True, True, False)):
            b.setChecked(on)
            f.addRow(b)
        f.addRow(QLabel("選んだテロップ（無ければすべて）に使います。"))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        f.addRow(bb)
        if dlg.exec():
            self.telop_tidy(*[b.isChecked() for b in boxes])

    def telop_tidy(self, drop_period=True, fullwidth_marks=True, halfwidth_digits=True, comma_space=False) -> int:
        n = 0
        self.begin_edit()
        for c in self._text_targets():
            old = str(c.prop("text", ""))
            new = tidy_text(old, drop_period, fullwidth_marks, halfwidth_digits, comma_space)
            if new != old:
                c.props["text"] = new
                n += 1
        self.end_edit(f"句読点を整える（{n} 個）")
        return n

    def subs_by_speaker(self) -> Dict[str, M.Track]:
        """字幕を話者ごと（話者の設定、無ければ文字の色）のトラックに分ける。話者ごとに消す・ずらすのが楽になる。"""
        seq = self.project.sequence
        texts = [(t, c) for t in seq.video_tracks for c in t.clips if c.kind == "text" and not t.locked]
        if not texts:
            return {}

        def key(c):
            spk = str(c.props.get("speaker") or "")
            if spk:
                return spk
            return "色 " + str((c.prop("style") or {}).get("font_color", "#FFFFFF")).upper()
        groups: Dict[str, List[Tuple[M.Track, M.Clip]]] = {}
        for t, c in texts:
            groups.setdefault(key(c), []).append((t, c))
        if len(groups) < 2:
            QMessageBox.information(self, "話者ごとに分ける", "話者（または文字の色）が 1 種類だけなので、分ける必要がありません。")
            return {}
        self.begin_edit()
        made: Dict[str, M.Track] = {}
        for k, items in groups.items():
            tracks: List[M.Track] = []
            for t, c in sorted(items, key=lambda x: x[1].start):
                t.clips.remove(c)
                dst = next((x for x in tracks if x.is_empty(c.start, c.end)), None)
                if dst is None:
                    dst = M.Track("video", f"字幕: {k}" + (f" {len(tracks) + 1}" if tracks else ""), selected=False)
                    seq.video_tracks.append(dst)
                    tracks.append(dst)
                dst.clips.append(c)
            for x in tracks:
                x.sort()
            made[k] = tracks[0]
        self.end_edit(f"字幕を話者ごとのトラックに（{len(made)} 人）")
        return made

    def telop_se_dialog(self):
        start = SETTINGS.se_dir or ""
        p, _ = QFileDialog.getOpenFileName(self, "テロップに付ける効果音", start, "音声 (*.wav *.mp3 *.ogg *.flac *.m4a)")
        if p:
            self.telop_se(p)

    def telop_se(self, path: str, offset_s: float = 0.0) -> int:
        """選んだテロップ（無ければすべて）の頭に効果音を置く（「SE」トラック）。"""
        seq = self.project.sequence
        clips = sorted(self._text_targets(), key=lambda c: c.start)
        if not clips:
            return 0
        self.begin_edit()
        info = self._add_media(path, "効果音")
        full = max(1, int(round(info.duration * seq.fps))) if getattr(info, "duration", 0) else int(seq.fps)
        starts = sorted({max(0, c.start + int(round(offset_s * seq.fps))) for c in clips})
        tr = self._audio_track("SE", starts[0], starts[0] + 1)
        n = 0
        for i, s in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else s + full
            d = min(full, nxt - s)
            if d <= 0 or not tr.is_empty(s, s + d):
                continue
            tr.clips.append(M.Clip(M.new_id(), "audio", s, d, 0, info.id, info.name))
            n += 1
        tr.sort()
        self.end_edit(f"テロップに効果音（{n} 個）")
        self.bin.refresh()
        return n

    # ─────────────────────────── 映像 ───────────────────────────
    def _sel_visual(self) -> List[M.Clip]:
        return [c for _, c in self.canvas.selected_clips() if c.kind in ("video", "image")]

    def detect_borders(self, clip: M.Clip, thresh: int = 18) -> Optional[List[float]]:
        """クリップの真ん中のフレームから、上下左右の黒帯（%）を探す。"""
        from .media import DecoderPool
        from . import render as R
        m = self.project.media_by_id(clip.media_id)
        if m is None or not os.path.exists(m.path):
            return None
        pool = DecoderPool()
        size = (min(640, m.width or 640), min(360, m.height or 360))
        if clip.kind == "image":
            im = pool.image(m.path)
            arr = None if im is None else np.asarray(im.convert("RGB").resize(size))
        else:
            dec = pool.video(m.path)
            fps = m.fps or self.project.sequence.fps
            arr = None if dec is None else dec.get_array(R.src_frame(clip, clip.start + clip.duration // 2) / fps, size)
        if arr is None:
            return None
        lum = arr[..., :3].astype(np.float32).mean(axis=2)
        rows = lum.max(axis=1) > thresh
        cols = lum.max(axis=0) > thresh
        if not rows.any() or not cols.any():
            return None
        h, w = lum.shape
        top, bottom = int(np.argmax(rows)), int(np.argmax(rows[::-1]))
        left, right = int(np.argmax(cols)), int(np.argmax(cols[::-1]))
        pct = [left / w * 100, top / h * 100, right / w * 100, bottom / h * 100]
        return [round(v, 2) if v >= 1.0 else 0.0 for v in pct]

    def auto_crop(self, fill: bool = True) -> int:
        """ゲーム・配信の画面の黒帯を探して切り取り、残りを画面いっぱいに（fill=False なら全体が入る大きさ）。"""
        clips = self._sel_visual()
        if not clips:
            QMessageBox.information(self, "黒帯を自動でカット", "映像か画像のクリップを選んでください。")
            return 0
        seq = self.project.sequence
        self.begin_edit()
        n = 0
        for c in clips:
            crop = self.detect_borders(c)
            if not crop or not any(crop):
                continue
            m = self.project.media_by_id(c.media_id)
            w, h = (m.width, m.height) if m and m.width else (seq.width, seq.height)
            cw, ch = w * (1 - (crop[0] + crop[2]) / 100), h * (1 - (crop[1] + crop[3]) / 100)
            fit = min(seq.width / w, seq.height / h)
            c.props["crop"] = crop
            c.props["scale"] = round((max if fill else min)(seq.width / cw, seq.height / ch) / fit, 4)
            # 切り取った後の中心が画面の中心に来るように
            c.props["pos_x"] = round(-(crop[0] - crop[2]) / 200 * w * fit * c.props["scale"], 1)
            c.props["pos_y"] = round(-(crop[1] - crop[3]) / 200 * h * fit * c.props["scale"], 1)
            n += 1
        self.end_edit(f"黒帯を自動でカット（{n} 個）")
        return n

    def wipe(self, corner: str = "br", size: float = 0.3) -> int:
        """選んだ映像をワイプ（角に小さく・角丸・白い枠・影）にする。"""
        n = self.pip(corner, size)
        if not n:
            return 0
        self.begin_edit()
        for c in self._sel_visual():
            fx = [e for e in (c.prop("effects") or []) if e.get("name") not in ("roundrect", "border", "shadow")]
            fx += [{"name": "roundrect", "on": True}, {"name": "border", "on": True}, {"name": "shadow", "on": True}]
            c.props["effects"] = fx
        self.end_edit("ワイプにする")
        return n

    def blur_fill(self, blur: float = 2.5) -> int:
        """縦長の写真・動画の左右（上下）の黒い所を、同じ絵をぼかして大きくしたもので埋める。"""
        seq = self.project.sequence
        sel = [(t, c) for t, c in self.canvas.selected_clips() if c.kind in ("video", "image") and t.kind == "video"]
        if not sel:
            QMessageBox.information(self, "ぼかし背景で埋める", "映像か画像のクリップを選んでください。")
            return 0
        self.begin_edit()
        bg_tracks: Dict[int, M.Track] = {}
        n = 0
        for t, c in sel:
            idx = seq.video_tracks.index(t)
            bt = bg_tracks.get(id(t))
            if bt is None or not bt.is_empty(c.start, c.end):
                bt = M.Track("video", f"{t.name} 背景", selected=False)
                seq.video_tracks.insert(idx, bt)
                bg_tracks[id(t)] = bt
            m = self.project.media_by_id(c.media_id)
            w, h = (m.width, m.height) if m and m.width else (seq.width, seq.height)
            fill = max(seq.width / w, seq.height / h) / min(seq.width / w, seq.height / h)
            bg = c.copy(id=M.new_id(), link="", name=(c.name or "") + "（背景）")
            bg.props.update({"scale": round(fill * 1.05, 4), "pos_x": 0.0, "pos_y": 0.0, "anim": {}, "volume_db": -96.0,
                             "effects": [{"name": "blur", "value": blur, "on": True},
                                         {"name": "brightness", "value": 0.8, "on": True}]})
            bt.clips.append(bg)
            bt.sort()
            n += 1
        self.end_edit(f"ぼかし背景で埋める（{n} 個）")
        return n

    # ─────────────────────────── 音声・シーケンス ───────────────────────────
    def bgm_end(self, fade_s: float = 2.0) -> int:
        """BGM（選んだ音声・無ければ「BGM」という名前のトラック）の終わりを、映像の終わりにそろえてフェードアウト。"""
        seq = self.project.sequence
        end = max((c.end for t in seq.video_tracks for c in t.clips), default=0)
        if end <= 0:
            return 0
        clips = [c for t, c in self.canvas.selected_clips() if c.kind == "audio"]
        if not clips:
            clips = [c for t in seq.audio_tracks if "BGM" in t.name.upper() for c in t.clips]
        if not clips:
            QMessageBox.information(self, "BGM の終わりをそろえる", "BGM のクリップを選ぶか、トラックの名前を「BGM」にしてください。")
            return 0
        fade = int(round(fade_s * seq.fps))
        ids = {c.id for c in clips}
        self.begin_edit()
        n = 0
        for t in seq.audio_tracks:                         # 映像の後ろにはみ出した BGM は消す
            for c in list(t.clips):
                if c.id in ids and c.start >= end:
                    t.clips.remove(c)
                    n += 1
        rest = [c for c in clips if c.start < end]
        if rest:
            last = max(rest, key=lambda c: c.end)
            if last.end > end:
                last.duration = end - last.start
            last.props["fade_out"] = min(fade, last.duration)
            n += 1
        self.end_edit("BGM の終わりをそろえる")
        return n

    def insert_space_dialog(self):
        sec, ok = QInputDialog.getDouble(self, "空白を挿入", "再生ヘッドの位置に入れる長さ（秒）:", 2.0, 0.1, 600.0, 1)
        if ok:
            self.insert_space(sec)

    def insert_space(self, seconds: float = 2.0) -> int:
        """再生ヘッドの位置に、すべてのトラックで空白を入れる（後ろは同じだけずれる・マーカーも）。"""
        seq = self.project.sequence
        f = self.record_player.position
        n = int(round(seconds * seq.fps))
        self.begin_edit()
        for t in seq.tracks():
            if not t.locked:
                M.insert_gap(t, f, n)
        for m in seq.markers:
            if m.frame >= f:
                m.frame += n
        self.end_edit(f"空白を挿入（{seconds:g} 秒）")
        return n

    # ─────────────────────────── 整理 ───────────────────────────
    KIND_BINS = {"video": "映像", "image": "画像", "audio": "音声"}

    def bins_by_kind(self) -> Dict[str, int]:
        """ビンに散らばった素材を、種類ごとのビン（映像 / 画像 / 音声）にまとめる（シーケンスはそのまま）。"""
        counts: Dict[str, int] = {}
        self.begin_edit()
        for m in self.project.media:
            name = self.KIND_BINS.get(m.kind)
            if name is None:
                continue
            dst = self.project.bin_by_id(self.named_bin(name))
            cur = self.project.bin_of(m.id)
            if cur is dst:
                continue
            if cur is not None:
                cur.remove(m.id)
            dst.add("media", m.id)
            counts[name] = counts.get(name, 0) + 1
        self.end_edit("ビンを種類で整理（" + "・".join(f"{k} {v}" for k, v in counts.items()) + "）")
        self.bin.refresh()
        return counts

    def merge_duplicate_media(self) -> int:
        """同じファイルを 2 回以上読み込んだ素材を 1 つにまとめる（クリップは残した方を指す）。"""
        seen: Dict[str, M.MediaItem] = {}
        remap: Dict[str, str] = {}
        for m in self.project.media:
            k = os.path.normcase(os.path.abspath(m.path)) if m.path else ""
            if not k:
                continue
            if k in seen:
                remap[m.id] = seen[k].id
            else:
                seen[k] = m
        if not remap:
            self.status.showMessage("重複した素材はありません", 3000)
            return 0
        self.begin_edit()
        for s in self.project.sequences:
            for _t, c in s.all_clips():
                if c.media_id in remap:
                    c.media_id = remap[c.media_id]
        for b in self.project.bins:
            for mid in remap:
                if mid in b.ids("media"):
                    b.remove(mid)
        self.project.media = [m for m in self.project.media if m.id not in remap]
        self.end_edit(f"重複した素材をまとめる（{len(remap)} 個）")
        self.bin.refresh()
        return len(remap)

    def work_report(self) -> str:
        """作業レポート（作業時間・シーケンスの長さ・カット数・テロップ数・今日の編集）を文字で。"""
        p = self.project
        sec = float(p.settings.get("work_seconds", 0)) + (self.work_session_seconds() if hasattr(self, "work_session_seconds") else 0)
        lines = [f"作業レポート  {datetime.datetime.now():%Y-%m-%d %H:%M}",
                 f"プロジェクト: {p.name or Path(self.project_path or '無題').stem}",
                 f"作業時間（合計）: {int(sec // 3600)} 時間 {int(sec // 60 % 60)} 分",
                 f"担当: {SETTINGS.user_name}", "", "■ シーケンス"]
        for s in p.sequences:
            cuts = len(s.video_tracks[0].clips) if s.video_tracks else 0
            tel = sum(1 for _t, c in s.all_clips() if c.kind == "text")
            lines.append(f"  {s.name}: {self.tc_str(s.duration) if s is p.sequence else f'{s.duration / s.fps:.1f} 秒'}"
                         f"・カット {cuts}・テロップ {tel}・マーカー {len(s.markers)}")
        lines += ["", f"■ 素材: {len(p.media)} 個", "", "■ 最近の編集（新しい順）"]
        for lab, _snap in list(reversed(self._undo))[:30]:
            lines.append(f"  - {lab}")
        notes = (p.settings.get("notes") or "").strip()
        if notes:
            lines += ["", "■ メモ", notes]
        return "\n".join(lines) + "\n"

    def work_report_dialog(self) -> Optional[str]:
        txt = self.work_report()
        out = self._out_dir9("Reports") / f"作業レポート_{datetime.datetime.now():%Y%m%d_%H%M}.txt"
        out.write_text(txt, encoding="utf-8")
        QApplication.clipboard().setText(txt)
        QMessageBox.information(self, "作業レポート", f"作りました（クリップボードにもコピーしました）:\n{out}")
        return str(out)
