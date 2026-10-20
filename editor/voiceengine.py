"""音声合成ソフトの起動・接続と、口パク用の母音タイムライン。

対応（VOICEVOX 互換 API のもの）: VOICEVOX / AivisSpeech / SHAREVOX / VOICEVOX NEMO / COEIROINK v1
- 起動していなければ、アプリ（画面あり）またはエンジンだけ（画面なし・軽い）を起動して準備完了まで待つ
- 合成した WAV の横に「<wav>.mouth.json」を保存する: 1/100 秒ごとの口の形（あいうえおん・閉じ）
  立ち絵はこれを使って母音に合わせた口パクをする（無ければ音量で開け閉め）
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .settings import SETTINGS

MOUTH_RATE = 100            # 口の形のタイムラインの細かさ（1 秒あたり）
SHAPE_CHARS = {"a": "a", "i": "i", "u": "u", "e": "e", "o": "o", "N": "n", "closed": "_"}
CHAR_SHAPES = {v: k for k, v in SHAPE_CHARS.items()}

_LOCAL = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
_PF = os.environ.get("ProgramFiles", r"C:\Program Files")

ENGINES: List[dict] = [
    {"key": "voicevox", "name": "VOICEVOX", "port": 50021, "credit": "VOICEVOX:{voice}",
     "app": [f"{_LOCAL}/Programs/VOICEVOX/VOICEVOX.exe", f"{_PF}/VOICEVOX/VOICEVOX.exe"],
     "engine": [f"{_LOCAL}/Programs/VOICEVOX/vv-engine/run.exe", f"{_PF}/VOICEVOX/vv-engine/run.exe",
                f"{_LOCAL}/Programs/VOICEVOX/run.exe"]},
    {"key": "aivis", "name": "AivisSpeech", "port": 10101, "credit": "AivisSpeech:{voice}",
     "app": [f"{_LOCAL}/Programs/AivisSpeech/AivisSpeech.exe", f"{_PF}/AivisSpeech/AivisSpeech.exe"],
     "engine": [f"{_LOCAL}/Programs/AivisSpeech/AivisSpeech-Engine/run.exe",
                f"{_PF}/AivisSpeech/AivisSpeech-Engine/run.exe"]},
    {"key": "sharevox", "name": "SHAREVOX", "port": 50025, "credit": "SHAREVOX:{voice}",
     "app": [f"{_LOCAL}/Programs/sharevox/SHAREVOX.exe", f"{_LOCAL}/Programs/SHAREVOX/SHAREVOX.exe"],
     "engine": [f"{_LOCAL}/Programs/sharevox/run.exe", f"{_LOCAL}/Programs/SHAREVOX/run.exe"]},
    {"key": "nemo", "name": "VOICEVOX NEMO", "port": 50121, "credit": "VOICEVOX NEMO:{voice}",
     "app": [f"{_LOCAL}/Programs/VOICEVOX NEMO/VOICEVOX NEMO.exe"],
     "engine": [f"{_LOCAL}/Programs/VOICEVOX NEMO/vv-engine/run.exe"]},
    {"key": "coeiroink", "name": "COEIROINK v1", "port": 50031, "credit": "COEIROINK:{voice}",
     "app": [f"{_LOCAL}/Programs/COEIROINK/COEIROINK.exe"], "engine": [f"{_LOCAL}/Programs/COEIROINK/engine/engine.exe"]},
]
ENGINE_BY_KEY = {e["key"]: e for e in ENGINES}
_procs: Dict[str, subprocess.Popen] = {}


def _url(key: str) -> str:
    return f"http://127.0.0.1:{ENGINE_BY_KEY[key]['port']}"


def _get(url: str, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def is_running(key: str) -> bool:
    try:
        _get(_url(key) + "/version", 1.5)
        return True
    except Exception:
        return False


def find_exe(key: str, kind: str = "engine") -> str:
    """kind: app（画面あり）/ engine（エンジンだけ）。環境設定の voice_engine_paths が優先。"""
    saved = (SETTINGS.voice_engine_paths or {}).get(f"{key}_{kind}", "")
    if saved and os.path.exists(saved):
        return saved
    for p in ENGINE_BY_KEY[key][kind]:
        hits = sorted(glob.glob(p)) if any(c in p for c in "*?") else ([p] if os.path.exists(p) else [])
        if hits:
            return os.path.normpath(hits[-1])
    return ""


def installed() -> List[dict]:
    """インストールされている / 起動しているエンジン。"""
    return [e for e in ENGINES if is_running(e["key"]) or find_exe(e["key"], "app") or find_exe(e["key"], "engine")]


def launch(key: str, with_app: bool = False) -> bool:
    """起動する（すでに動いていれば何もしない）。with_app=False ならエンジンだけ（画面なし）。"""
    if is_running(key):
        return True
    exe = find_exe(key, "app" if with_app else "engine") or find_exe(key, "engine" if with_app else "app")
    if not exe:
        return False
    args = [exe]
    if exe.lower().endswith("run.exe"):
        args += ["--host", "127.0.0.1", "--port", str(ENGINE_BY_KEY[key]["port"])]
    flags = 0x08000000 if (os.name == "nt" and exe.lower().endswith("run.exe")) else 0
    _procs[key] = subprocess.Popen(args, cwd=os.path.dirname(exe), creationflags=flags,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def wait_ready(key: str, timeout: float = 120, tick: Optional[Callable[[str], None]] = None,
               cancel: Optional[Callable[[], bool]] = None) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if is_running(key):
            return True
        if cancel and cancel():
            return False
        p = _procs.get(key)
        if p is not None and p.poll() is not None and not is_running(key):
            return False                 # 起動に失敗して終了した
        if tick:
            tick(f"{ENGINE_BY_KEY[key]['name']} を起動中… {int(time.time() - t0)} 秒")
        time.sleep(1.0)
    return False


def ensure(key: str, tick=None) -> None:
    """合成の前に呼ぶ: 動いていなければエンジンを起動して待つ（環境設定 voice_auto_launch）。"""
    if is_running(key):
        return
    if not SETTINGS.voice_auto_launch:
        raise RuntimeError(f"{ENGINE_BY_KEY[key]['name']} が起動していません")
    if not launch(key, with_app=False):
        raise RuntimeError(f"{ENGINE_BY_KEY[key]['name']} が見つかりません（インストールするか、環境設定で場所を指定してください）")
    if not wait_ready(key, tick=tick):
        raise RuntimeError(f"{ENGINE_BY_KEY[key]['name']} を起動できませんでした")


def stop_launched():
    """TeloCut が起動したエンジンだけを終了する（アプリ終了時）。"""
    for key, p in list(_procs.items()):
        if p.poll() is None and p.args and str(p.args[0]).lower().endswith("run.exe"):
            p.terminate()
        _procs.pop(key, None)


def speakers(key: str) -> List[Tuple[str, int]]:
    """[(「キャラ（スタイル）」, style_id)]"""
    try:
        d = json.loads(_get(_url(key) + "/speakers", 10).decode("utf-8"))
    except Exception:
        return []
    out = []
    for sp in d:
        for st in sp.get("styles", []):
            out.append((f"{sp.get('name')}（{st.get('name')}）", int(st.get("id"))))
    return out


def _client(key: str):
    from core.voicevox import VoiceVoxClient
    return VoiceVoxClient(port=ENGINE_BY_KEY[key]["port"])


def synthesize(text: str, key: str, style_id: int, out_path: str, speed: float = 1.0, pitch: float = 0.0,
               intonation: float = 1.0, anim_type: str = "aiueo2") -> str:
    """合成して WAV と口の形のタイムライン（<wav>.mouth.json）を保存する。"""
    from core.voicevox import build_mora_timeline
    ensure(key)
    wav, q = _client(key).synthesize(text, int(style_id), speed=speed, pitch=pitch, intonation=intonation)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(wav)
    try:
        # 音素の長さは話速 1.0 のときの値。話速 n 倍なら実時間は 1/n なので、細かさを 1/n にして合わせる
        tl = build_mora_timeline(q, MOUTH_RATE / max(0.1, speed), anim_type)
        write_mouth(out_path, [SHAPE_CHARS.get(s, "a") for s in tl])
    except Exception:
        pass
    return out_path


def mouth_path(wav_path: str) -> str:
    return wav_path + ".mouth.json"


def write_mouth(wav_path: str, chars: List[str]):
    Path(mouth_path(wav_path)).write_text(json.dumps({"rate": MOUTH_RATE, "shapes": "".join(chars)}),
                                          encoding="utf-8")


_mouth_cache: Dict[str, Optional[Tuple[int, str]]] = {}


def mouth_shape_at(wav_path: str, sec: float) -> Optional[str]:
    """その時刻の口の形（a/i/u/e/o/N/closed）。タイムラインが無ければ None（音量で判断する）。"""
    rec = _mouth_cache.get(wav_path, False)
    if rec is False:
        rec = None
        try:
            d = json.loads(Path(mouth_path(wav_path)).read_text(encoding="utf-8"))
            rec = (int(d["rate"]), str(d["shapes"]))
        except (OSError, ValueError, KeyError):
            pass
        _mouth_cache[wav_path] = rec
    if rec is None:
        return None
    rate, s = rec
    i = int(sec * rate)
    if i < 0 or i >= len(s):
        return "closed"
    return CHAR_SHAPES.get(s[i], "closed")


def credit_for(key: str, voice_name: str) -> str:
    e = ENGINE_BY_KEY.get(key)
    if not e:
        return ""
    return e["credit"].format(voice=voice_name.split("（")[0])
