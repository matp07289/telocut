"""字幕テキストの整形・話者解析ユーティリティ。"""
from __future__ import annotations

import re

# よくあるフィラー（言いよどみ）。長いものから順に除去する。
DEFAULT_FILLERS = [
    "えーと", "えっと", "ええと", "えと",
    "あのー", "あの", "そのー",
    "なんか", "まぁ", "まあ", "ま、",
    "うーんと", "うーん", "んーと", "んー",
    "そうですね", "なんていうか",
]

# 話者プレフィックスの正規表現
#   "名前: セリフ" / "名前：セリフ" / "【名前】セリフ" / "[名前] セリフ"
_SPK_COLON = re.compile(r"^\s*([^:：\n]{1,16})[：:]\s*(.+)$", re.S)
_SPK_BRACKET = re.compile(r"^\s*[【\[]\s*([^】\]\n]{1,16})\s*[】\]]\s*(.+)$", re.S)


def split_speaker(text: str):
    """テキストから話者名と本文を分離。返値 (speaker or None, body)。"""
    if not text:
        return (None, text)
    m = _SPK_BRACKET.match(text)
    if m:
        return (m.group(1).strip(), m.group(2).strip())
    m = _SPK_COLON.match(text)
    if m:
        spk = m.group(1).strip()
        # コロン前が長すぎる/改行を含む場合は話者ではないとみなす
        if "\n" not in spk and len(spk) <= 16:
            return (spk, m.group(2).strip())
    return (None, text)


def remove_fillers(text: str, fillers=None) -> str:
    """フィラー語を除去して整える。"""
    if not text:
        return text
    fillers = fillers or DEFAULT_FILLERS
    out = text
    for f in sorted(fillers, key=len, reverse=True):
        out = out.replace(f, "")
    # 連続する読点・空白を整理
    out = re.sub(r"[、，]{2,}", "、", out)
    out = re.sub(r"[ 　]{2,}", " ", out)
    out = re.sub(r"^[、，。\.\s　]+", "", out)
    return out.strip()


def wrap_text(text: str, max_chars: int = 20, max_lines: int = 2) -> str:
    """1行 max_chars で折り返し（既存改行は尊重）。句読点で優先的に改行。"""
    if max_chars <= 0:
        return text
    result_lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        while len(line) > max_chars:
            # max_chars 以内で句読点の直後を探す
            cut = -1
            for i in range(min(max_chars, len(line) - 1), 0, -1):
                if line[i] in "、。，．,.!?！？":
                    cut = i + 1
                    break
            if cut <= 0:
                cut = max_chars
            result_lines.append(line[:cut].strip("　 "))
            line = line[cut:].strip("　 ")
        if line:
            result_lines.append(line)
    if max_lines and len(result_lines) > max_lines:
        # 行数超過分は最終行に連結（切り捨てない）
        head = result_lines[:max_lines - 1]
        tail = " ".join(result_lines[max_lines - 1:])
        result_lines = head + [tail]
    return "\n".join(result_lines)


def format_text(text: str, *, remove_filler: bool = True,
                wrap: bool = True, max_chars: int = 20,
                max_lines: int = 2, strip_speaker: bool = False) -> str:
    """まとめて整形。"""
    t = text
    if strip_speaker:
        _spk, t = split_speaker(t)
    if remove_filler:
        t = remove_fillers(t)
    if wrap:
        t = wrap_text(t, max_chars=max_chars, max_lines=max_lines)
    return t
