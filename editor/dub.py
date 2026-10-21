"""英語の声 → 日本語の吹き替え（日本語テロップ＋日本語の音声合成）。

1) 範囲の音声（全トラックのミックス / 選んだ音声トラック）を faster-whisper で英語として文字起こし（NVIDIA があれば CUDA）
2) Claude Code（claude -p）でセリフごとに日本語へ翻訳（字幕向けに短く自然に）
3) VOICEVOX 系（VOICEVOX / AivisSpeech …）で日本語の声を合成。元の英語の長さに収まらなければ話速を上げる
4) 日本語テロップ（新しい映像トラック）・日本語の声（新しい音声トラック）を並べ、元の声を小さくする（任意）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import wave
from dataclasses import dataclass, field
from typing import Callable, List, Optional

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class DubLine:
    start: float                 # 秒（範囲の頭から）
    end: float
    en: str
    ja: str = ""
    wav: str = ""
    speed: float = 1.0
    voice_dur: float = 0.0


@dataclass
class DubOptions:
    model: str = "small"
    use_gpu: bool = True
    language: str = "en"          # 元の言語（"auto" で自動）
    translate: str = "claude"     # claude / none（英語のまま）
    engine: str = "voicevox"
    style_id: int = 3             # ずんだもん（ノーマル）
    max_speed: float = 1.6
    make_voice: bool = True
    make_telops: bool = True
    keep_english: bool = False    # 英語の字幕も残す
    duck_db: float = -14.0        # 元の声を下げる（0 = 下げない）
    max_chars: int = 18
    extra: dict = field(default_factory=dict)


# ─────────────────────────── 翻訳（Claude Code）───────────────────────────
def parse_json_list(text: str) -> list:
    """応答の中の JSON 配列を取り出す（```json …``` で囲まれていても）。"""
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        raise ValueError("翻訳の応答に JSON の配列がありません")
    return json.loads(m.group(0))


def translate_prompt(lines: List[str], src_lang: str = "英語") -> str:
    items = [{"i": i, "text": t} for i, t in enumerate(lines)]
    return (f"次の{src_lang}のセリフ（動画の字幕・吹き替え用）を、自然で短い日本語に訳してください。\n"
            "- 話し言葉で、意味を落とさずに（あいさつ・呼びかけも訳す）。字幕として読みやすく、とても長いときだけ言い回しを短く\n"
            "- 固有名詞はカタカナか元のまま。前後の流れを踏まえて訳す\n"
            "- ツールやファイル操作は使わず、JSON の配列だけを返す: [{\"i\": 0, \"ja\": \"…\"}, …]\n\n"
            + json.dumps(items, ensure_ascii=False))


def translate_claude(lines: List[str], exe: str = "", timeout: int = 600, batch: int = 60,
                     progress: Optional[Callable[[int, int], None]] = None, runner=None) -> List[str]:
    """Claude Code（claude -p）で翻訳。runner(prompt) -> 応答テキスト を渡せば差し替えられる（テスト用）。"""
    if runner is None:
        from .assistant import find_claude
        exe = exe or find_claude()
        if not exe:
            raise RuntimeError("Claude Code（claude）が見つかりません。npm i -g @anthropic-ai/claude-code で入れて、"
                               "claude で一度ログインしてください（または「翻訳しない」を選んでください）")

        def runner(prompt: str) -> str:
            p = subprocess.run([exe, "-p", "--output-format", "json", "--disallowedTools",
                                "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"],
                               input=prompt.encode("utf-8"), capture_output=True, timeout=timeout,
                               creationflags=_NO_WINDOW)
            out = p.stdout.decode("utf-8", "replace")
            try:
                d = json.loads(out)
            except json.JSONDecodeError:
                raise RuntimeError((p.stderr.decode("utf-8", "replace") or out)[:500] or "翻訳の応答がありません")
            if d.get("is_error"):
                raise RuntimeError(str(d.get("result"))[:500])
            return str(d.get("result", ""))
    out = [""] * len(lines)
    for b0 in range(0, len(lines), batch):
        part = lines[b0:b0 + batch]
        res = parse_json_list(runner(translate_prompt(part)))
        for it in res:
            try:
                i = int(it.get("i"))
            except (TypeError, ValueError, AttributeError):
                continue
            if 0 <= i < len(part):
                out[b0 + i] = str(it.get("ja", "")).strip()
        if progress:
            progress(min(len(lines), b0 + batch), len(lines))
    # 訳が抜けたものは英語のまま
    return [o or l for o, l in zip(out, lines)]


# ─────────────────────────── 声の合成（長さに合わせる）───────────────────────────
def wav_seconds(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def fit_speed(voice_sec: float, slot_sec: float, max_speed: float = 1.6) -> float:
    """話速 1.0 の長さ voice_sec を slot_sec に収める話速（1.0〜max_speed）。"""
    if slot_sec <= 0 or voice_sec <= slot_sec * 1.03:
        return 1.0
    return round(min(max_speed, voice_sec / slot_sec * 1.02), 2)


def synth_lines(lines: List[DubLine], out_dir: str, opt: DubOptions, synth=None,
                progress: Optional[Callable[[int, int, str], None]] = None, cancel=None) -> None:
    """各行の日本語を合成して line.wav / speed / voice_dur を埋める。synth(text, path, speed) を差し替え可能。"""
    if synth is None:
        from . import voiceengine as VE

        def synth(text, path, speed):
            return VE.synthesize(text, opt.engine, int(opt.style_id), path, speed=speed)
    os.makedirs(out_dir, exist_ok=True)
    for i, ln in enumerate(lines):
        if cancel is not None and cancel():
            raise InterruptedError("中止しました")
        if not ln.ja.strip():
            continue
        path = os.path.join(out_dir, f"dub_{i:04d}.wav")
        synth(ln.ja, path, 1.0)
        d = wav_seconds(path)
        sp = fit_speed(d, ln.end - ln.start, opt.max_speed)
        if sp > 1.0:
            synth(ln.ja, path, sp)
            d = wav_seconds(path)
        ln.wav, ln.speed, ln.voice_dur = path, sp, d
        if progress:
            progress(i + 1, len(lines), ln.ja[:20])


# ─────────────────────────── タイムラインに並べる ───────────────────────────
def place(mw, lines: List[DubLine], start_frame: int, opt: DubOptions) -> dict:
    """日本語テロップ・日本語の声を新しいトラックに並べる。元の声を小さくする。戻り値 {"telops": n, "voices": n}"""
    from . import media as MD
    from . import model as M
    from .yukkuri import subtitle_chunks
    seq = mw.project.sequence
    fps = seq.fps
    n_tel = n_voice = 0
    mw.begin_edit()
    if opt.make_telops:
        tt = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
        seq.video_tracks.append(tt)
        et = None
        if opt.keep_english:
            et = M.Track("video", f"V{len(seq.video_tracks) + 1}", selected=False)
            seq.video_tracks.append(et)
        for ln in lines:
            a = start_frame + int(round(ln.start * fps))
            b = start_frame + int(round(max(ln.end, ln.start + ln.voice_dur) * fps))
            for track, text, style in ((tt, ln.ja, {}),
                                       (et, ln.en, {"font_size": 40, "position_v": "top", "offset_y": 40})):
                if track is None or not text.strip():
                    continue
                chunks = subtitle_chunks(text, opt.max_chars if track is tt else 40)
                total = sum(len(c) for c in chunks) or 1
                pos = a
                for k, ch in enumerate(chunks):
                    d = (b - pos) if k == len(chunks) - 1 else max(1, int((b - a) * len(ch) / total))
                    if d <= 0:
                        continue
                    track.clips.append(M.Clip(M.new_id(), "text", pos, d, name="吹替" if track is tt else "英語",
                                              props={"text": ch, "style": dict(style)}))
                    pos += d
                    if track is tt:
                        n_tel += 1
        for t in (tt, et):
            if t is None:
                continue
            t.sort()
            for c0, c1 in zip(t.clips, t.clips[1:]):
                if c0.end > c1.start:
                    c0.duration = max(1, c1.start - c0.start)
    if opt.make_voice and any(ln.wav for ln in lines):
        orig_tracks = list(seq.audio_tracks)
        at = M.Track("audio", f"A{len(seq.audio_tracks) + 1}")
        seq.audio_tracks.append(at)
        b = mw.project.bin_by_id(mw.bin.current_bin_id()) if hasattr(mw, "bin") else None
        b = b or mw.project.bins[0]
        for ln in lines:
            if not ln.wav:
                continue
            item = MD.probe(ln.wav)
            MD.build_audio_cache(item.path)
            MD.build_peaks(item.path)
            mw.project.media.append(item)
            b.add("media", item.id)
            f = start_frame + int(round(ln.start * fps))
            dur = max(1, int(round(item.duration_s * fps)))
            at.clips.append(M.Clip(M.new_id(), "audio", f, dur, 0, item.id, f"吹替: {ln.ja[:12]}",
                                   props={"line_text": ln.ja, "speaker": "吹き替え", "en_text": ln.en}))
            n_voice += 1
        at.sort()
        for c0, c1 in zip(at.clips, at.clips[1:]):
            if c0.end > c1.start:              # 次の声と重なるなら次を後ろへ（声が重ならない）
                shift = c0.end - c1.start
                c1.start += shift
        # 元の声を小さく（範囲に入る元の音声クリップ）
        if opt.duck_db:
            a0 = start_frame + int(round(min(ln.start for ln in lines) * fps))
            a1 = start_frame + int(round(max(ln.end for ln in lines) * fps))
            for t in orig_tracks:
                for c in t.clips:
                    if c.end > a0 and c.start < a1:
                        c.props["volume_db"] = float(c.props.get("volume_db", 0.0)) + float(opt.duck_db)
    mw.end_edit(f"吹き替え（テロップ {n_tel}・声 {n_voice}）")
    return {"telops": n_tel, "voices": n_voice}


def segments_to_lines(segs, merge_gap: float = 0.25, max_len: float = 8.0) -> List[DubLine]:
    """Whisper の区切りを、短すぎるものはつないでセリフ単位に。"""
    out: List[DubLine] = []
    for s in segs:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        if out and s.start - out[-1].end <= merge_gap and (s.end - out[-1].start) <= max_len \
                and not out[-1].en.rstrip().endswith((".", "?", "!")):
            out[-1].en = out[-1].en.rstrip() + " " + text
            out[-1].end = s.end
        else:
            out.append(DubLine(float(s.start), float(s.end), text))
    return out
