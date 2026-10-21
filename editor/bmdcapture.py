"""Blackmagic（DeckLink / UltraStudio / Avid Artist DNxIO など）から直接収録する。

Desktop Video が入れる DirectShow の入力（「Decklink Video Capture」「Decklink Audio Capture」）を ffmpeg で読む。
- 入力の形式（1080p29.97 など）は機器の一覧から選ぶ（入力の信号と同じものにする）
- 保存の形式: H.264（NVIDIA なら NVENC・軽い）/ H.264（CPU）/ ProRes 422 HQ / DNxHR HQ（MOV）
- 止めたら（ffmpeg に q を送ってきれいに閉じる）→ そのまま取り込み・無音カット（OBS 連携と同じ流れ）
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from typing import List, Tuple

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CODECS = {
    "h264_nvenc": ("H.264（NVIDIA NVENC・軽い）", ".mp4",
                   ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "19", "-b:v", "0", "-pix_fmt", "yuv420p"],
                   ["-c:a", "aac", "-b:a", "256k"]),
    "h264": ("H.264（CPU）", ".mp4", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"],
             ["-c:a", "aac", "-b:a", "256k"]),
    "prores_hq": ("ProRes 422 HQ（高画質・大きい）", ".mov",
                  ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"], ["-c:a", "pcm_s24le"]),
    "dnxhr_hq": ("DNxHR HQ（Avid 向け・大きい）", ".mov",
                 ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"], ["-c:a", "pcm_s16le"]),
}


def _ff() -> str:
    from .media import ffmpeg_exe
    ff = ffmpeg_exe()
    if not ff:
        raise RuntimeError("ffmpeg が見つかりません（PATH に置いてください）")
    return ff


def devices() -> Tuple[List[str], List[str]]:
    """(Blackmagic の映像の入力, 音声の入力) の DirectShow の名前。"""
    try:
        r = subprocess.run([_ff(), "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        return [], []
    text = r.stderr.decode("utf-8", "replace")
    vids, auds = [], []
    for m in re.finditer(r'"([^"]+)" \((video|audio|audio, video)\)', text):
        name, kind = m.group(1), m.group(2)
        if "decklink" not in name.lower() and "blackmagic" not in name.lower():
            continue
        if "video" in kind and name not in vids and "WDM" not in name:
            vids.append(name)
        if kind == "audio" and name not in auds:
            auds.append(name)
    return vids, auds


def formats(video_device: str) -> List[dict]:
    """入力の形式 [{size: "1920x1080", fps: 29.97, label: "1080p29.97"}]（同じものは 1 つに）。"""
    try:
        r = subprocess.run([_ff(), "-hide_banner", "-f", "dshow", "-list_options", "true", "-i", f"video={video_device}"],
                           capture_output=True, timeout=20, creationflags=_NO_WINDOW)
    except Exception:
        return []
    out, seen = [], set()
    for m in re.finditer(r"s=(\d+)x(\d+) fps=([\d.]+)", r.stderr.decode("utf-8", "replace")):
        w, h, fps = int(m.group(1)), int(m.group(2)), float(m.group(3))
        fps = {59.9402: 59.94, 60.0002: 60.0, 29.97: 29.97}.get(round(fps, 4), round(fps, 3))
        key = (w, h, fps)
        if key in seen:
            continue
        seen.add(key)
        lab = {1080: "1080p", 720: "720p", 486: "NTSC", 576: "PAL", 2160: "2160p"}.get(h, f"{w}x{h} ")
        out.append({"size": f"{w}x{h}", "fps": fps, "label": f"{lab}{fps:g}" if h in (1080, 720, 2160) else f"{lab} {fps:g}"})
    return out


class Recorder:
    """ffmpeg で収録（stop() で q を送って閉じる）。"""

    def __init__(self, video_device: str, audio_device: str, size: str, fps: float, codec: str, out_path: str):
        name, ext, vargs, aargs = CODECS[codec]
        self.path = out_path if out_path.lower().endswith(ext) else os.path.splitext(out_path)[0] + ext
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        src = f"video={video_device}" + (f":audio={audio_device}" if audio_device else "")
        cmd = [_ff(), "-hide_banner", "-loglevel", "error", "-stats", "-y", "-f", "dshow", "-rtbufsize", "512M",
               "-video_size", size, "-framerate", f"{fps:g}", "-pixel_format", "uyvy422", "-i", src] + vargs
        cmd += aargs if audio_device else ["-an"]
        if ext == ".mp4":
            cmd += ["-movflags", "+faststart"]
        cmd.append(self.path)
        self.cmd = cmd
        self.started = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     creationflags=_NO_WINDOW)
        self.log: List[str] = []
        self.frames = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        while True:
            ch = self.proc.stderr.read(1)
            if not ch:
                break
            if ch in (b"\r", b"\n"):
                line = buf.decode("utf-8", "replace").strip()
                buf = b""
                if line:
                    m = re.search(r"frame=\s*(\d+)", line)
                    if m:
                        self.frames = int(m.group(1))
                    else:
                        self.log.append(line)
            else:
                buf += ch

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def elapsed(self) -> float:
        return time.time() - self.started

    def size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def stop(self, timeout: float = 15.0) -> str:
        """止めてファイルを閉じる（戻り値: 保存したパス）。"""
        if self.running:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if not os.path.exists(self.path) or self.size() == 0:
            raise RuntimeError("収録できませんでした: " + (" / ".join(self.log[-3:]) or "入力の信号・形式を確かめてください"))
        return self.path
