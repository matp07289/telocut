"""音声の自動文字起こし（faster-whisper / ローカル）。

タイムラインや動画/音声ファイルの音声を Whisper でテキスト化し、
タイムコード付きのセグメント（字幕）を返す。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

try:
    from faster_whisper import WhisperModel
    HAS_FASTER_WHISPER = True
except Exception:
    HAS_FASTER_WHISPER = False

# 利用可能なモデル（小さいほど速い／大きいほど高精度）
MODELS = ["tiny", "base", "small", "medium", "large-v3"]
MODEL_NOTE = {
    "tiny":     "最速・低精度（~75MB）",
    "base":     "速い（~145MB）",
    "small":    "標準・おすすめ（~470MB）",
    "medium":   "高精度・やや重い（~1.5GB）",
    "large-v3": "最高精度・重い（~3GB）",
}

_model_cache: dict = {}


@dataclass
class Segment:
    start: float   # 秒（音声先頭=0 起点）
    end: float
    text: str


def _get_model(model_size: str, use_gpu: bool):
    device = "cuda" if use_gpu else "cpu"
    compute = "float16" if use_gpu else "int8"
    key = (model_size, device, compute)
    m = _model_cache.get(key)
    if m is not None:
        return m
    try:
        m = WhisperModel(model_size, device=device, compute_type=compute)
    except Exception:
        # GPU 失敗時は CPU にフォールバック
        m = WhisperModel(model_size, device="cpu", compute_type="int8")
        key = (model_size, "cpu", "int8")
    _model_cache[key] = m
    return m


def _find_system_python() -> list:
    """faster-whisper が使えそうな外部 Python 実行ファイル候補を返す。"""
    import sys, shutil, os
    cands = []
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)          # ソース実行時は自分自身
    for name in ("python", "python3", "py"):
        p = shutil.which(name)
        if p and p not in cands:
            cands.append(p)
    # よくある場所
    for p in (r"C:\Program Files\Python38\python.exe",
              os.path.expanduser(r"~\AppData\Local\Programs\Python\Python38\python.exe")):
        if os.path.exists(p) and p not in cands:
            cands.append(p)
    return cands


_HELPER = r'''
import sys, json
from faster_whisper import WhisperModel
audio, model_size, lang, gpu = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
device = "cuda" if gpu == "1" else "cpu"
compute = "float16" if gpu == "1" else "int8"
try:
    m = WhisperModel(model_size, device=device, compute_type=compute)
except Exception:
    m = WhisperModel(model_size, device="cpu", compute_type="int8")
kw = {"beam_size": 5}
if lang and lang != "auto":
    kw["language"] = lang
try:
    segs, info = m.transcribe(audio, vad_filter=True, **kw)
except Exception:
    segs, info = m.transcribe(audio, **kw)
dur = float(getattr(info, "duration", 0.0) or 0.0)
print(json.dumps({"_dur": dur}), flush=True)
for s in segs:
    print(json.dumps({"start": float(s.start), "end": float(s.end),
                      "text": s.text or ""}), flush=True)
'''


def _transcribe_subprocess(audio_path, model_size, language, use_gpu,
                           progress_cb=None) -> list:
    """外部 Python の faster-whisper をサブプロセス実行して文字起こし。"""
    import subprocess, tempfile, os, json, sys
    helper = os.path.join(tempfile.gettempdir(), "_davintelop_whisper.py")
    with open(helper, "w", encoding="utf-8") as f:
        f.write(_HELPER)

    last_err = ""
    for py in _find_system_python():
        # その python に faster_whisper があるか確認
        try:
            chk = subprocess.run([py, "-c", "import faster_whisper"],
                                 capture_output=True, timeout=30)
            if chk.returncode != 0:
                continue
        except Exception:
            continue
        # 実行
        try:
            si = None
            if os.name == "nt":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.Popen(
                [py, helper, audio_path, model_size,
                 language or "ja", "1" if use_gpu else "0"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", startupinfo=si)
            out = []
            total = 0.0
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if "_dur" in d:
                    total = d["_dur"]
                    continue
                txt = (d.get("text") or "").strip()
                if not txt:
                    continue
                seg = Segment(float(d["start"]), float(d["end"]), txt)
                out.append(seg)
                if progress_cb:
                    try: progress_cb(seg.end, total, txt)
                    except Exception: pass
            proc.wait(timeout=10)
            if out:
                return out
            last_err = (proc.stderr.read() or "")[-500:]
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(
        "faster-whisper を実行できる Python が見つかりませんでした。\n"
        "コマンドプロンプトで次を実行してください:\n"
        "  pip install faster-whisper\n\n"
        + (f"詳細: {last_err}" if last_err else ""))


def transcribe(
    audio_path: str,
    model_size: str = "small",
    language: str = "ja",
    use_gpu: bool = False,
    vad: bool = True,
    progress_cb: Optional[Callable[[float, float, str], None]] = None,
) -> list:
    """音声ファイルを文字起こしして Segment のリストを返す。

    language: "ja" 固定、"auto" で自動判定。
    progress_cb(done_sec, total_sec, text): 進捗通知（任意）。

    本体に faster-whisper が無い場合（EXE版など）は、システムの Python に
    インストールされた faster-whisper をサブプロセス実行して処理する。
    """
    if not HAS_FASTER_WHISPER:
        return _transcribe_subprocess(audio_path, model_size, language,
                                      use_gpu, progress_cb)

    model = _get_model(model_size, use_gpu)

    kwargs = dict(beam_size=5)
    if language and language != "auto":
        kwargs["language"] = language
    if vad:
        # VAD（無音除去）。onnxruntime 未導入なら自動で無効化される。
        kwargs["vad_filter"] = True

    try:
        seg_iter, info = model.transcribe(audio_path, **kwargs)
    except Exception:
        # VAD まわりで失敗したら VAD なしで再試行
        kwargs.pop("vad_filter", None)
        seg_iter, info = model.transcribe(audio_path, **kwargs)

    total = float(getattr(info, "duration", 0.0) or 0.0)
    out: list = []
    for seg in seg_iter:
        text = (seg.text or "").strip()
        if not text:
            continue
        s = Segment(float(seg.start), float(seg.end), text)
        out.append(s)
        if progress_cb:
            try:
                progress_cb(float(seg.end), total, text)
            except Exception:
                pass
    return out


def _srt_tc(sec: float) -> str:
    ms = int(round(max(0.0, sec) * 1000))
    h = ms // 3600000; ms -= h * 3600000
    m = ms // 60000;   ms -= m * 60000
    s = ms // 1000;    ms -= s * 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: list) -> str:
    """Segment リストを SRT 文字列に変換（0 起点）。"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{_srt_tc(seg.start)} --> {_srt_tc(seg.end)}")
        lines.append(seg.text)
        lines.append("")
    return "\n".join(lines)


def _frames_tc(sec: float, fps: float) -> str:
    """秒 → "HH:MM:SS:FF"（fps 基準・0 起点）。"""
    if fps <= 0:
        fps = 30.0
    total_f = int(round(max(0.0, sec) * fps))
    fpsi = int(round(fps))
    if fpsi <= 0:
        fpsi = 30
    f = total_f % fpsi
    s = (total_f // fpsi)
    sec_i = s % 60
    m = (s // 60) % 60
    h = s // 3600
    return f"{h:02d}:{m:02d}:{sec_i:02d}:{f:02d}"


def segments_to_telops(segments: list, fps: float, start_id: int = 1) -> list:
    """Segment リストを TelopEntry のリストに変換（0 起点のタイムコード）。"""
    from .models import TelopEntry
    telops = []
    for i, seg in enumerate(segments):
        telops.append(TelopEntry(
            id=start_id + i,
            start_time=_frames_tc(seg.start, fps),
            end_time=_frames_tc(seg.end, fps),
            text=seg.text,
            enabled=True,
            note="自動字幕",
        ))
    return telops
