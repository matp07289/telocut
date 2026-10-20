"""TeloCut の MCP サーバー（標準入出力・JSON-RPC）。

Claude Code に登録すると、ターミナルの Claude Code から起動中の TeloCut を操作できる:
    claude mcp add --scope user telocut -- TeloCut.exe --mcp
TeloCut 本体とはブリッジ（bridge.py、127.0.0.1 の TCP）でつながる。Qt は使わない。
"""
from __future__ import annotations

import json
import sys

from . import bridge
from . import ops as OPS

PROTOCOL = "2024-11-05"

_POS = {"type": "string", "description": "タイムコード HH:MM:SS:FF（シーケンス開始 TC 基準）"}
TOOLS = [
    {"name": "get_state", "description": "開いているシーケンスの要約（トラック・クリップ・テロップ・マーカー・再生位置）を返す",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_telops", "description": "テロップの一覧（開始・終了・本文・スタイル）を返す",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "apply_ops", "description": "編集命令をまとめて実行する（1 回の取り消しで戻せる）。使える命令:\n"
                                         + OPS.__doc__.split("対応する命令（op）:")[1].strip(),
     "inputSchema": {"type": "object", "required": ["ops"], "properties": {
         "ops": {"type": "array", "items": {"type": "object"}, "description": "例: [{\"op\": \"add_telop\", \"start\": \"01:00:01:00\", \"end\": \"01:00:03:00\", \"text\": \"こんにちは\"}]"},
         "label": {"type": "string", "description": "取り消し履歴に出る名前"}}}},
    {"name": "seek", "description": "再生ヘッドを移動する",
     "inputSchema": {"type": "object", "required": ["at"], "properties": {"at": _POS}}},
    {"name": "render_frame", "description": "現在のフレームを PNG に書き出してパスを返す（画面の確認用）",
     "inputSchema": {"type": "object", "properties": {"scale": {"type": "number", "description": "0.1〜1.0（既定 0.5）"}}}},
    {"name": "import_files", "description": "ファイルをビンに取り込む",
     "inputSchema": {"type": "object", "required": ["paths"], "properties": {"paths": {"type": "array", "items": {"type": "string"}}}}},
]


def _call_tool(name: str, args: dict):
    if name == "get_state":
        return bridge.call("get_state")
    if name == "get_telops":
        return bridge.call("get_telops")
    if name == "apply_ops":
        return bridge.call("apply_ops", ops=args.get("ops", []), label=args.get("label", "Claude Code"), timeout=120)
    if name == "seek":
        return bridge.call("seek", tc=args["at"])
    if name == "render_frame":
        return bridge.call("render_frame", scale=float(args.get("scale", 0.5)), timeout=60)
    if name == "import_files":
        return bridge.call("import_files", paths=args.get("paths", []))
    raise ValueError(f"unknown tool: {name}")


def handle(msg: dict):
    """1 件の JSON-RPC リクエストを処理して応答（通知なら None）を返す。"""
    mid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    if mid is None:
        return None          # notifications/initialized など
    try:
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "telocut", "version": "0.8.0"},
                   "instructions": "起動中の TeloCut（動画編集ソフト）を操作します。まず get_state で状況を確認し、"
                                   "編集は apply_ops でまとめて行ってください。"}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                out = _call_tool(params.get("name", ""), params.get("arguments") or {})
                res = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False, indent=1)}]}
            except Exception as ex:
                res = {"content": [{"type": "text", "text": f"エラー: {ex}"}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as ex:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(ex)}}


def main():
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            r = handle(m)
            if r is not None:
                stdout.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
                stdout.flush()


if __name__ == "__main__":
    main()
