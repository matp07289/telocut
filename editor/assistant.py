"""Claude Code（Pro プラン）による編集アシスト。

- `claude -p ... --output-format json` を呼び、シーケンスの要約と編集ルールを渡して編集命令（ops.py）を提案させる
- 提案は一覧で確認してから適用（チェックを外した命令は実行しない）。適用は 1 回の取り消しで戻せる
- 続けて会話できる（--resume でセッションを引き継ぐ）
- MCP サーバー（mcp_server.py）を Claude Code に登録すると、ターミナルの Claude Code から直接 TeloCut を操作できる
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QTextBrowser, QPushButton,
                             QListWidget, QListWidgetItem, QLabel, QGridLayout, QMessageBox, QCheckBox)

from . import ops as OPS
from .settings import SETTINGS

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 編集ルール（YouTube 向けの一般的な基準。ユーザーが環境設定で書き換えられるようにファイルにも置く）
DEFAULT_RULES = """\
- 不要な間・言い直し・噛み・フィラー（えー、あのー）はカットする。ただし自然な間（文の切れ目 0.2〜0.4 秒）は残す
- テロップは 1 枚 10〜20 文字程度。意味の切れ目（文節）で分け、1 行が長くなりすぎないようにする
- 固有名詞・数字・専門用語は正確に。自信がないものは marker の comment で確認を促す
- 見出し・話題の切り替わりにはポップな SE（例: ポン、シャキーン）を入れる
- テロップの表示時間は読める長さ（1 秒あたり 8 文字以下）を目安にする
- 変更は最小限にし、元の発言の意味を変えない
"""

QUICK = [
    ("テロップ校正", "テロップの誤字脱字・表記ゆれ・固有名詞を校正して edit_telop で直して。直した理由は message に。"),
    ("テロップ分割", "長いテロップを 10〜20 文字程度で意味の切れ目に分割して（replace_telops か edit_telop/add_telop で）。"),
    ("見出しに SE", "話題の切り替わり・見出しになるテロップの先頭に add_se でポップな効果音を入れて。"),
    ("カット候補", "言い直し・噛み・長い間などカットした方がよい箇所を add_marker で示して（cut はまだしない）。name は「⚠ カット候補」、comment に理由。"),
    ("チャプター", "内容から YouTube のチャプター（概要欄用のタイムスタンプと見出し）を作って message に書き、各位置に add_marker して。"),
    ("タイトル案", "この動画のタイトル案を 5 個と、サムネイルの文言案を 3 個、message に書いて。ops は空でよい。"),
]


def find_claude() -> str:
    cands = [SETTINGS.claude_exe, shutil.which("claude"), shutil.which("claude.cmd"),
             os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
             str(Path.home() / ".local" / "bin" / "claude.exe"),
             str(Path.home() / ".claude" / "local" / "claude.exe")]
    return next((c for c in cands if c and os.path.exists(c)), "")


def rules_path() -> Path:
    from .settings import BASE_DIR
    return BASE_DIR / "telocut_edit_rules.md"


def load_rules() -> str:
    try:
        return rules_path().read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_RULES


def build_prompt(state: dict, request: str, rules: str) -> str:
    return f"""あなたは動画編集ソフト TeloCut の編集アシスタントです。ファイル操作やツールは使わず、
下のシーケンス情報だけを見て、編集命令を JSON で提案してください。

## 編集ルール
{rules}

## 使える編集命令（ops）
{OPS.__doc__.split('対応する命令（op）:')[1].strip()}
位置（start, end, at）はタイムコード文字列 "HH:MM:SS:FF" で指定します。clip_id はシーケンス情報の id です。

## シーケンス情報
```json
{json.dumps(state, ensure_ascii=False)}
```

## 依頼
{request}

## 返答の形式
次の JSON だけを ```json のコードブロックで返してください（説明は message に書く）:
{{"message": "ユーザーへの説明（日本語）", "ops": [{{"op": "...", ...}}]}}
"""


def parse_reply(text: str) -> dict:
    """Claude の返答から {"message", "ops"} を取り出す。"""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"):text.rfind("}") + 1]
    try:
        d = json.loads(cand)
        if isinstance(d, dict):
            return {"message": str(d.get("message", "")), "ops": [o for o in d.get("ops", []) if isinstance(o, dict) and o.get("op")]}
    except (json.JSONDecodeError, ValueError):
        pass
    return {"message": text.strip(), "ops": []}


def describe_op(op: dict) -> str:
    o = dict(op)
    name = o.pop("op")
    jp = {"add_telop": "テロップ追加", "edit_telop": "テロップ変更", "cut": "カット（詰める）", "lift": "削除（隙間）",
          "split": "分割", "delete_clip": "クリップ削除", "move_clip": "移動", "set_props": "プロパティ",
          "add_marker": "マーカー", "add_se": "効果音", "place_media": "素材配置", "replace_telops": "テロップ一括置換"}.get(name, name)
    if name == "replace_telops":
        return f"{jp}: {len(o.get('telops', []))} 枚"
    pos = o.pop("start", None) or o.pop("at", None) or ""
    end = o.pop("end", None)
    txt = o.pop("text", None) or o.pop("name", None) or o.pop("comment", None) or ""
    rest = {k: v for k, v in o.items() if k not in ("clip_id", "track")}
    s = f"{jp} {pos}" + (f"–{end}" if end else "") + (f"「{txt}」" if txt else "")
    return s + (f" {json.dumps(rest, ensure_ascii=False)}" if rest else "")


class ClaudeWorker(QThread):
    finished_ok = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, exe: str, prompt: str, session: str = "", timeout: int = 600):
        super().__init__()
        self.exe, self.prompt, self.session, self.timeout = exe, prompt, session, timeout
        self.proc: Optional[subprocess.Popen] = None

    def cancel(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def run(self):
        args = [self.exe, "-p", "--output-format", "json"]
        if self.session:
            args += ["--resume", self.session]
        try:
            # プロンプトは長いので標準入力で渡す。ツールは使わせない（提案だけ）
            self.proc = subprocess.Popen(args + ["--disallowedTools", "Bash,Edit,Write,NotebookEdit,WebFetch"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         creationflags=_NO_WINDOW)
            out, err = self.proc.communicate(self.prompt.encode("utf-8"), timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            self.failed.emit("時間切れになりました")
            return
        except OSError as ex:
            self.failed.emit(f"Claude Code を起動できません: {ex}")
            return
        text = out.decode("utf-8", "replace")
        try:
            d = json.loads(text)
        except json.JSONDecodeError:
            self.failed.emit((err.decode("utf-8", "replace") or text)[:1500] or "応答がありません")
            return
        if d.get("is_error"):
            self.failed.emit(str(d.get("result") or d.get("error") or d)[:1500])
            return
        r = parse_reply(str(d.get("result", "")))
        r["session"] = d.get("session_id", "")
        r["cost"] = d.get("total_cost_usd")
        self.finished_ok.emit(r)


class AssistantPanel(QWidget):
    """AI 編集アシスト（ドック）。"""

    def __init__(self, mw):
        super().__init__()
        self.mw = mw
        self.worker: Optional[ClaudeWorker] = None
        self.session = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.log = QTextBrowser()
        self.log.setOpenExternalLinks(True)
        lay.addWidget(self.log, 2)
        grid = QGridLayout()
        for i, (lab, req) in enumerate(QUICK):
            b = QPushButton(lab)
            b.setToolTip(req)
            b.clicked.connect(lambda _=False, r=req: self.ask(r))
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid)
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("依頼を書く（例: 冒頭 10 秒の間を詰めて / 01:02:00:00 付近のテロップを短く）  Ctrl+Enter で送信")
        self.input.setFixedHeight(64)
        lay.addWidget(self.input)
        row = QHBoxLayout()
        self.sel_only = QCheckBox("イン〜アウトの範囲だけ")
        self.send = QPushButton("送信")
        self.send.clicked.connect(lambda: self.ask(self.input.toPlainText().strip()))
        self.stop = QPushButton("中止")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(lambda: self.worker and self.worker.cancel())
        new = QPushButton("新しい会話")
        new.clicked.connect(self.reset)
        row.addWidget(self.sel_only)
        row.addStretch(1)
        row.addWidget(new)
        row.addWidget(self.stop)
        row.addWidget(self.send)
        lay.addLayout(row)
        lay.addWidget(QLabel("提案された編集（チェックしたものを適用）"))
        self.ops = QListWidget()
        lay.addWidget(self.ops, 1)
        arow = QHBoxLayout()
        self.apply_btn = QPushButton("適用")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        clear = QPushButton("破棄")
        clear.clicked.connect(self.ops.clear)
        arow.addStretch(1)
        arow.addWidget(clear)
        arow.addWidget(self.apply_btn)
        lay.addLayout(arow)
        self._say("system", "Claude Code（Pro プラン）に編集を相談できます。提案は確認してから適用されます。"
                  + ("" if find_claude() else "<br><b>Claude Code が見つかりません。</b> 環境設定で claude の場所を指定してください。"))

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.ask(self.input.toPlainText().strip())
            return
        super().keyPressEvent(e)

    def _say(self, who: str, text: str):
        color = {"user": "#8cf", "claude": "#eee", "system": "#999", "error": "#f88"}[who]
        label = {"user": "あなた", "claude": "Claude", "system": "", "error": "エラー"}[who]
        body = text if who == "system" else text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "<br>")
        self.log.append(f"<div style='color:{color}'>{'<b>' + label + '</b>: ' if label else ''}{body}</div>")

    def reset(self):
        self.session = ""
        self.ops.clear()
        self.log.clear()
        self._say("system", "新しい会話を始めます。")

    def state(self) -> dict:
        st = OPS.describe_state(self.mw)
        if self.sel_only.isChecked():
            st["focus"] = "イン点〜アウト点の範囲だけを対象にしてください"
        return st

    def ask(self, request: str):
        if not request or (self.worker and self.worker.isRunning()):
            return
        exe = find_claude()
        if not exe:
            self._say("error", "Claude Code（claude）が見つかりません。npm i -g @anthropic-ai/claude-code でインストールし、claude で一度ログインしてください。")
            return
        self.input.clear()
        self._say("user", request)
        prompt = build_prompt(self.state(), request, load_rules()) if not self.session else \
            f"## シーケンス情報（最新）\n```json\n{json.dumps(self.state(), ensure_ascii=False)}\n```\n\n## 依頼\n{request}\n\n前と同じ JSON 形式で返してください。"
        self.worker = ClaudeWorker(exe, prompt, self.session)
        self.worker.finished_ok.connect(self._reply)
        self.worker.failed.connect(lambda e: self._say("error", e))
        self.worker.finished.connect(self._done)
        self.send.setEnabled(False)
        self.stop.setEnabled(True)
        self._say("system", "考え中…")
        self.worker.start()

    def _done(self):
        self.send.setEnabled(True)
        self.stop.setEnabled(False)

    def _reply(self, r: dict):
        self.session = r.get("session") or self.session
        self._say("claude", r["message"] or "（説明なし）")
        self.show_ops(r["ops"])

    def show_ops(self, ops: List[dict]):
        self.ops.clear()
        for o in ops:
            it = QListWidgetItem(describe_op(o))
            it.setData(Qt.ItemDataRole.UserRole, o)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.ops.addItem(it)
        self.apply_btn.setEnabled(bool(ops))

    def apply(self):
        ops = [self.ops.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.ops.count())
               if self.ops.item(i).checkState() == Qt.CheckState.Checked]
        if not ops:
            return
        errs = [m for m in OPS.apply_ops(self.mw, ops, "AI アシスト") if m.startswith("⚠")]
        self._say("system", f"{len(ops) - len(errs)} 件を適用しました（取り消しで戻せます）。"
                  + ("<br>" + "<br>".join(errs) if errs else ""))
        self.ops.clear()
        self.apply_btn.setEnabled(False)


def register_mcp(parent=None) -> bool:
    """Claude Code に TeloCut の MCP サーバーを登録する（claude mcp add）。"""
    import sys
    exe = find_claude()
    if not exe:
        QMessageBox.warning(parent, "MCP", "Claude Code が見つかりません。")
        return False
    if getattr(sys, "frozen", False):
        # ウィンドウ版 EXE は標準入出力が無いので、同じフォルダのコンソール版を使う
        cli = Path(sys.executable).with_name("TeloCutCLI.exe")
        cmd = [str(cli if cli.exists() else sys.executable), "--mcp"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "telocut.py"), "--mcp"]
    args = [exe, "mcp", "add", "--scope", "user", "telocut", "--"] + cmd
    if QMessageBox.question(parent, "Claude Code に登録",
                            "次のコマンドで Claude Code に TeloCut を登録します（ユーザー全体の設定）。\n\n"
                            + " ".join(f'"{a}"' if " " in a else a for a in args[1:])
                            + "\n\n登録後はターミナルの Claude Code から「TeloCut のテロップを校正して」のように頼めます。") \
            != QMessageBox.StandardButton.Yes:
        return False
    r = subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW)
    msg = (r.stdout + r.stderr).decode("utf-8", "replace")
    (QMessageBox.information if r.returncode == 0 else QMessageBox.warning)(parent, "Claude Code に登録", msg or "完了しました")
    return r.returncode == 0
