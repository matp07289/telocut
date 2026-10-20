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


# ─────────────────────────────────────────────
#  テキスト加工（テロップ一括加工メニュー用）
# ─────────────────────────────────────────────

import unicodedata

_PUNCT = "、。，．,."
_BREAK_AFTER = "、。，．,.!?！？…」』）)】 　"
_PARTICLES = ("から", "まで", "けど", "ので", "のに", "は", "が", "を", "に", "で",
              "と", "も", "へ", "や", "の", "て")


def to_halfwidth_alnum(text: str) -> str:
    """全角英数記号・全角スペースを半角に。"""
    out = []
    for ch in text:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif ch == "　":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def to_fullwidth_alnum(text: str) -> str:
    """半角英数記号を全角に（改行はそのまま）。"""
    out = []
    for ch in text:
        o = ord(ch)
        if 0x21 <= o <= 0x7E:
            out.append(chr(o + 0xFEE0))
        elif ch == " ":
            out.append("　")
        else:
            out.append(ch)
    return "".join(out)


def halfkana_to_fullkana(text: str) -> str:
    """半角カタカナ（濁点含む）を全角に。英数字は変えない。"""
    return re.sub(r"[｡-ﾟ]+",
                  lambda m: unicodedata.normalize("NFKC", m.group(0)), text)


def normalize_spaces(text: str) -> str:
    """各行の前後空白を除去し、連続する空白を1つに。空行も削除。"""
    lines = [re.sub(r"[ 　\t]{2,}", " ", l).strip(" 　\t") for l in text.split("\n")]
    return "\n".join(l for l in lines if l)


def remove_punct(text: str, replace_with: str = "") -> str:
    """句読点（、。，．）を削除または置換。行末に残る空白は除去。"""
    t = re.sub(f"[{re.escape(_PUNCT)}]", replace_with, text)
    return "\n".join(l.rstrip(" 　") for l in t.split("\n"))


def remove_trailing_period(text: str) -> str:
    """各行末の「。」「.」を削除。"""
    return "\n".join(re.sub(r"[。．.]+$", "", l) for l in text.split("\n"))


def join_lines(text: str, sep: str = "") -> str:
    """改行を削除して1行に。"""
    return sep.join(l.strip() for l in text.split("\n") if l.strip())


def normalize_ellipsis(text: str) -> str:
    """「...」「・・・」などを「…」に統一。"""
    t = re.sub(r"\.{3,}|・{3,}|。{3,}", "……", text)
    t = re.sub(r"…{3,}", "……", t)
    return t


def add_brackets(text: str, left: str, right: str) -> str:
    """全体を括弧で囲む（既に囲まれていれば何もしない）。"""
    t = text.strip()
    if t.startswith(left) and t.endswith(right):
        return t
    return f"{left}{t}{right}"


_BRACKET_PAIRS = [("「", "」"), ("『", "』"), ("（", "）"), ("(", ")"),
                  ("【", "】"), ("“", "”"), ("\"", "\""), ("［", "］"), ("[", "]")]


def strip_brackets(text: str) -> str:
    """全体を囲む括弧を外す。"""
    t = text.strip()
    for l, r in _BRACKET_PAIRS:
        if len(t) >= 2 and t.startswith(l) and t.endswith(r):
            return t[len(l):-len(r)].strip()
    return t


def _ctype(ch: str) -> str:
    o = ord(ch)
    if 0x3041 <= o <= 0x309F:
        return "h"      # ひらがな
    if 0x30A0 <= o <= 0x30FF or 0xFF66 <= o <= 0xFF9F:
        return "k"      # カタカナ
    if 0x4E00 <= o <= 0x9FFF or ch in "々〆":
        return "c"      # 漢字
    if ch.isascii() and ch.isalnum():
        return "a"
    return "o"


def balanced_break(text: str, lines: int = 2) -> str:
    """1行のテキストを lines 行にできるだけ均等に改行する。
    句読点・空白の直後、助詞の直後（次が漢字/カタカナ）を優先し、
    単語の途中（漢字同士・ひらがな同士・英数字同士）は避ける。"""
    flat = join_lines(text)
    if lines < 2 or len(flat) < 4:
        return flat
    result = []
    rest = flat
    for k in range(lines, 1, -1):
        target = len(rest) / k
        punct_bonus = max(3.0, len(rest) * 0.15)
        best, best_score = None, None
        for i in range(1, len(rest)):
            prev, nxt = rest[:i], rest[i]
            if nxt in "、。，．,.!?！？…」』）)】ーっゃゅょぁぃぅぇぉッャュョァィゥェォ":
                continue    # 行頭禁則
            if prev[-1] in "「『（(【":
                continue    # 行末禁則
            a, b = _ctype(prev[-1]), _ctype(nxt)
            # 接頭辞「お/ご」+漢字 はひとかたまりとして扱う（お越し・ご案内）
            if nxt in "おご" and i + 1 < len(rest) and _ctype(rest[i + 1]) == "c":
                b = "c"
            if prev[-1] in "おご" and b == "c" and (i < 2 or _ctype(prev[-2]) != "c"):
                continue
            score = abs(i - target)
            if prev[-1] in _BREAK_AFTER:
                score -= punct_bonus
            elif a == "h" and b in "ck" and any(prev.endswith(p) for p in _PARTICLES):
                score -= 2.0
            elif a == "h" and b in "ck":
                score -= 0.5
            elif a == b and a in "hca":
                score += 2.0
            if best_score is None or score < best_score:
                best, best_score = i, score
        if best is None:
            break
        result.append(rest[:best].rstrip(" 　"))
        rest = rest[best:].lstrip(" 　")
    result.append(rest)
    return "\n".join(l for l in result if l)
