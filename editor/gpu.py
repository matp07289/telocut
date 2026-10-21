"""NVIDIA の GPU を優先して使う（再生のデコード・プロキシ・変換・書き出し・文字起こし）。

同梱の PyAV には NVENC / NVDEC が無いので、GPU の仕事は外部の ffmpeg（NVENC / CUDA 入り）に任せる。
- 再生: 続けて再生している間だけ、ffmpeg で CUDA デコード → GPU で縮小（scale_cuda）→ 小さい絵をパイプで受け取る
  （シーク・コマ送りは今まで通り PyAV。止まると ffmpeg も止める）
- プロキシ・変換・書き出し（H.264 / H.265）: h264_nvenc / hevc_nvenc
- 文字起こし（faster-whisper）: CUDA

設定 SETTINGS.gpu_mode: "auto"（NVIDIA があれば使う）/ "on" / "off"。
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
from typing import Optional, Tuple

import numpy as np

from .settings import SETTINGS

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_info: Optional[dict] = None
_lock = threading.Lock()


def _ffmpeg() -> Optional[str]:
    from .media import ffmpeg_exe
    return ffmpeg_exe()


def _run(cmd, timeout=20) -> Tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as ex:
        return -1, str(ex)


def detect(force: bool = False) -> dict:
    """{"name": GPU 名, "nvenc": bool, "cuda_decode": bool, "scale_cuda": bool, "ffmpeg": パス, "whisper_cuda": bool}"""
    global _info
    with _lock:
        if _info is not None and not force:
            return _info
        info = {"name": "", "nvenc": False, "hevc_nvenc": False, "cuda_decode": False, "scale_cuda": False,
                "ffmpeg": "", "whisper_cuda": False}
        code, out = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], 10)
        if code == 0 and out.strip():
            info["name"] = out.strip().splitlines()[0].strip()
        ff = _ffmpeg()
        if ff and info["name"]:
            info["ffmpeg"] = ff
            # 実際に小さな絵で試す（ドライバが古い・ffmpeg に NVENC が無い などを見分ける）
            code, _ = _run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
                            "-c:v", "h264_nvenc", "-f", "null", "-"])
            info["nvenc"] = code == 0
            code, _ = _run([ff, "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2",
                            "-c:v", "hevc_nvenc", "-f", "null", "-"])
            info["hevc_nvenc"] = code == 0
            code, out = _run([ff, "-hide_banner", "-filters"])
            info["scale_cuda"] = code == 0 and "scale_cuda" in out
            code, out = _run([ff, "-hide_banner", "-hwaccels"])
            info["cuda_decode"] = code == 0 and "cuda" in out.split()
        try:
            from core.transcribe import cuda_ready
            info["whisper_cuda"] = cuda_ready()
        except Exception:
            info["whisper_cuda"] = False
        _info = info
        return info


def detect_async():
    """起動時に裏で調べておく（再生中に待たせない）。"""
    if _info is None:
        threading.Thread(target=detect, daemon=True).start()


def enabled() -> bool:
    mode = getattr(SETTINGS, "gpu_mode", "auto")
    if mode == "off":
        return False
    return bool(detect()["name"])


def use_nvenc(codec: str = "h264") -> bool:
    """H.264 / H.265 を NVENC で書けるか（設定でオフなら False）。"""
    if not enabled():
        return False
    d = detect()
    return d["hevc_nvenc"] if codec in ("h265", "hevc", "libx265") else d["nvenc"]


def use_gpu_decode() -> bool:
    if getattr(SETTINGS, "gpu_mode", "auto") == "off" or not getattr(SETTINGS, "gpu_decode", True):
        return False
    if _info is None:              # まだ調べていない（再生のスレッドで待たない）
        detect_async()
        return False
    d = _info
    if not d["name"]:
        return False
    return d["cuda_decode"] and d["scale_cuda"]


def use_whisper_gpu() -> bool:
    return enabled() and detect()["whisper_cuda"]


def nvenc_args(codec: str, quality: int = 20, intra: bool = False, fast: bool = False) -> list:
    """ffmpeg の映像エンコードの引数（CRF の数字をそのまま CQ に）。"""
    enc = "hevc_nvenc" if codec in ("h265", "hevc", "libx265") else "h264_nvenc"
    a = ["-c:v", enc, "-preset", "p1" if fast else "p5", "-rc", "vbr", "-cq", str(int(quality)), "-b:v", "0"]
    if intra:
        a += ["-g", "1", "-bf", "0"]
    if enc == "hevc_nvenc":
        a += ["-tag:v", "hvc1"]
    return a + ["-pix_fmt", "yuv420p"]


def summary() -> str:
    d = detect()
    if not d["name"]:
        return "NVIDIA の GPU が見つかりません（CPU で処理します）"
    if not d["ffmpeg"]:
        return f"{d['name']}: ffmpeg が見つからないため GPU を使えません（ffmpeg を PATH に置いてください）"
    yes = lambda b: "○" if b else "×"
    return (f"{d['name']}\n"
            f"  書き出し・変換 H.264 NVENC {yes(d['nvenc'])} / H.265 NVENC {yes(d['hevc_nvenc'])}\n"
            f"  再生のデコード CUDA {yes(d['cuda_decode'] and d['scale_cuda'])}\n"
            f"  文字起こし CUDA {yes(d['whisper_cuda'])}"
            + ("" if d["whisper_cuda"] else "（pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 で使えます）") + "\n"
            f"  設定: {'使わない' if getattr(SETTINGS, 'gpu_mode', 'auto') == 'off' else '優先して使う'}")


# ─────────────────────────── ffmpeg での変換（プロキシ・トランスコード）───────────────────────────
def ffmpeg_transcode(src: str, dst: str, vargs: list, w: int = 0, h: int = 0, audio: Optional[list] = None,
                     progress=None, cancel: Optional[threading.Event] = None, duration: float = 0.0,
                     t0: Optional[float] = None, t1: Optional[float] = None) -> bool:
    """CUDA でデコード → （縮小）→ NVENC。audio=None なら音声なし、[] なら音声そのままコピー。"""
    ff = detect()["ffmpeg"] or _ffmpeg()
    if not ff:
        return False
    cmd = [ff, "-v", "error", "-y", "-progress", "pipe:1", "-nostats",
           "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if t0:
        cmd += ["-ss", f"{t0:.3f}"]
    cmd += ["-i", src]
    if t1:
        cmd += ["-t", f"{max(0.0, t1 - (t0 or 0)):.3f}"]
    if w and h:
        cmd += ["-vf", f"scale_cuda={w}:{h}:format=nv12"]
    else:
        cmd += ["-vf", "scale_cuda=format=nv12"]
    cmd += ["-map", "0:v:0", "-fps_mode", "passthrough"] + [a for a in vargs if a not in ("-pix_fmt", "yuv420p")]
    if audio is None:
        cmd += ["-an"]
    else:
        cmd += ["-map", "0:a:0?"] + (audio or ["-c:a", "copy"])
    cmd += ["-movflags", "+faststart", dst]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    except OSError:
        return False
    total = max(0.001, (t1 or duration or 0) - (t0 or 0)) if (t1 or duration) else 0
    for line in p.stdout:
        if cancel is not None and cancel.is_set():
            p.kill()
            p.wait()
            return False
        s = line.decode("ascii", "ignore").strip()
        if s.startswith("out_time_us=") and progress and total:
            try:
                progress(min(0.99, int(s.split("=")[1]) / 1e6 / total))
            except ValueError:
                pass
    p.wait()
    return p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0


# ─────────────────────────── 再生: ffmpeg の CUDA デコードを順番に読む ───────────────────────────
class GpuReader:
    """path を t 秒から CUDA でデコードし、size に GPU で縮小した RGB を順に返す（先読みのスレッド付き）。"""

    def __init__(self, path: str, t: float, size: Tuple[int, int], frame_dur: float):
        self.size = (int(size[0]) & ~1, int(size[1]) & ~1)
        self.frame_dur = frame_dur
        self.start = t
        self.n = 0                       # 次に返すフレームの番号（start + n * frame_dur）
        self.ready = threading.Event()
        self.dead = False
        self.q: "queue.Queue" = queue.Queue(maxsize=12)
        w, h = self.size
        ff = detect()["ffmpeg"]
        cmd = [ff, "-v", "error", "-nostdin", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
               "-ss", f"{max(0.0, t):.4f}", "-i", path, "-an", "-sn",
               "-vf", f"scale_cuda={w}:{h}:format=nv12,hwdownload,format=nv12",
               "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=_NO_WINDOW, bufsize=w * h * 3 * 4)
        self.th = threading.Thread(target=self._pump, daemon=True)
        self.th.start()

    def _pump(self):
        w, h = self.size
        nbytes = w * h * 3
        try:
            while True:
                buf = self.proc.stdout.read(nbytes)
                if not buf or len(buf) < nbytes:
                    break
                self.q.put(np.frombuffer(buf, np.uint8).reshape(h, w, 3))
                self.ready.set()
        except Exception:
            pass
        self.dead = True
        self.ready.set()
        self.q.put(None)

    def covers(self, t: float, size) -> bool:
        """t（秒）がこれから読むフレーム以降で、近い（2 秒以内）か。"""
        if self.dead and self.q.empty():
            return False
        nxt = self.start + self.n * self.frame_dur
        return tuple(size) == self.size and nxt - self.frame_dur * 1.5 <= t < nxt + 2.0

    def read_until(self, t: float, last=None, timeout: float = 2.0):
        """t 秒のフレーム（配列）。読み終わり・時間切れなら None。"""
        arr = last
        while self.start + self.n * self.frame_dur <= t + self.frame_dur * 0.5:
            try:
                a = self.q.get(timeout=timeout)
            except queue.Empty:
                return None
            if a is None:
                self.dead = True
                return arr
            arr = a
            self.n += 1
        return arr

    def close(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.stdout.close()
        except Exception:
            pass
