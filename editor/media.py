"""メディアの解析・デコード・音声キャッシュ。

- 映像: PyAV でシーク＋順次デコード（再生中は順方向に読み進めるだけなので速い）
- 音声: 取り込み時に 48kHz/16bit/ステレオの raw PCM を作り、numpy.memmap で参照
- 波形: 1/100 秒単位のピーク値を .npy に保存
- プロキシ: 重い素材（4K / Long GOP）から全イントラの軽量 H.264 を作り、再生に使う
キャッシュ類はすべてローカル（settings.py）に置く。素材が NAS にあっても再生は軽い。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from .model import MediaItem, new_id
from .settings import SETTINGS

SAMPLE_RATE = 48000
CHANNELS = 2
PEAKS_PER_SEC = 100

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".psd", ".psb"}
AUDIO_EXT = {".wav", ".mp3", ".aac", ".m4a", ".flac", ".ogg", ".opus", ".aif", ".aiff"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".mxf", ".m4v", ".webm", ".wmv", ".mts", ".m2ts", ".ts"}

def cache_dir() -> Path:
    return Path(SETTINGS.cache_dir)

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def ffmpeg_exe() -> Optional[str]:
    """外部 ffmpeg（あれば）。EXE の隣 → PATH の順に探す。無くても動作する（PyAV を使う）。"""
    import sys
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else None
    if here is not None and (here / "ffmpeg.exe").exists():
        return str(here / "ffmpeg.exe")
    return shutil.which("ffmpeg")


_KEY_MEMO: dict = {}


def invalidate(path: str):
    """素材ファイルが更新されたときに呼ぶ（キャッシュキーを作り直す）。"""
    _KEY_MEMO.pop(path, None)


def open_image(path: str) -> Image.Image:
    """画像を開く。PSD は全レイヤーを合成した画像（キャッシュ）を返す。"""
    if path.lower().endswith((".psd", ".psb")):
        cp = cache_dir() / f"{_cache_key(path)}.psd.png"
        if cp.exists():
            return Image.open(cp)
        from psd_tools import PSDImage
        im = PSDImage.open(path).composite()
        cp.parent.mkdir(parents=True, exist_ok=True)
        im.save(cp)
        return im
    return Image.open(path)


def _cache_key(path: str) -> str:
    """素材の識別キー（パス・サイズ・更新日時）。NAS への stat を減らすためメモ化。"""
    k = _KEY_MEMO.get(path)
    if k is not None:
        return k
    try:
        st = os.stat(path)
        sig = f"{os.path.basename(path)}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = os.path.abspath(path)
    k = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:20]
    _KEY_MEMO[path] = k
    return k


def audio_cache_path(path: str) -> Path:
    return cache_dir() / f"{_cache_key(path)}.pcm"


def peaks_cache_path(path: str) -> Path:
    return cache_dir() / f"{_cache_key(path)}.peaks.npy"


def proxy_path(path: str) -> Path:
    return Path(SETTINGS.proxy_dir) / f"{_cache_key(path)}_{SETTINGS.proxy_width}.mp4"


_OFFLINE: dict = {}


def is_offline(path: str, ttl: float = 5.0) -> bool:
    """素材ファイルが無いか（画面の描き直しのたびに NAS へ問い合わせないよう、5 秒ごとに確認）。"""
    now = time.time()
    hit = _OFFLINE.get(path)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    off = not os.path.exists(path)
    _OFFLINE[path] = (now, off)
    if len(_OFFLINE) > 5000:
        _OFFLINE.clear()
    return off


def has_proxy(path: str) -> bool:
    try:
        p = proxy_path(path)
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


# ─────────────────────────────────────────────
#  解析
# ─────────────────────────────────────────────

def probe(path: str) -> MediaItem:
    item = _probe(path)
    try:
        item.file_size = os.path.getsize(path)
        item.mtime = os.path.getmtime(path)
    except OSError:
        pass
    return item


def _probe(path: str) -> MediaItem:
    ext = Path(path).suffix.lower()
    name = Path(path).name
    if ext in IMAGE_EXT:
        im = open_image(path)
        w, h = im.size
        return MediaItem(id=new_id(), path=path, name=name, kind="image",
                         width=w, height=h, has_video=True)
    import av
    with av.open(path) as c:
        v = next((s for s in c.streams.video), None)
        a = next((s for s in c.streams.audio), None)
        dur = 0.0
        if c.duration:
            dur = c.duration / 1_000_000
        fps = 0.0
        w = h = 0
        if v is not None:
            rate = v.average_rate or v.guessed_rate or v.base_rate
            fps = float(rate) if rate else 30.0
            w, h = v.codec_context.width, v.codec_context.height
            if v.duration and v.time_base:
                dur = max(dur, float(v.duration * v.time_base))
        if a is not None and a.duration and a.time_base and not dur:
            dur = float(a.duration * a.time_base)
        kind = "video" if v is not None and ext not in AUDIO_EXT else "audio"
        if v is not None and v.frames == 1 and dur < 0.1:
            kind = "image"
        return MediaItem(id=new_id(), path=path, name=name, kind=kind, duration_s=dur,
                         fps=fps, width=w, height=h, has_video=v is not None and kind != "audio",
                         has_audio=a is not None,
                         audio_channels=a.codec_context.channels if a is not None else 0)


# ─────────────────────────────────────────────
#  音声キャッシュ・波形
# ─────────────────────────────────────────────

_AUDIO_LOCKS: dict = {}
_AUDIO_LOCKS_LOCK = threading.Lock()


def build_audio_cache(path: str, cancel: Optional[threading.Event] = None) -> Optional[Path]:
    """PyAV で raw PCM (s16le / 48k / stereo) を作る。既にあれば再利用。
    同じ素材を別のスレッドが作っている最中なら、それが終わるのを待つ（同じ一時ファイルに 2 つ書かない）。"""
    out = audio_cache_path(path)
    if out.exists() and out.stat().st_size > 0:
        return out
    with _AUDIO_LOCKS_LOCK:
        lock = _AUDIO_LOCKS.setdefault(str(out), threading.Lock())
    with lock:
        if out.exists() and out.stat().st_size > 0:
            return out
        return _build_audio_cache(path, out, cancel)


def _build_audio_cache(path: str, out: Path, cancel: Optional[threading.Event]) -> Optional[Path]:
    cache_dir().mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    try:
        import av
        with av.open(path) as c, open(tmp, "wb") as f:
            if not c.streams.audio:
                return None
            st = c.streams.audio[0]
            st.thread_type = "AUTO"
            res = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
            for frame in c.decode(st):
                if cancel is not None and cancel.is_set():
                    break
                for r in res.resample(frame):
                    f.write(r.to_ndarray().tobytes())
            else:
                for r in res.resample(None):
                    f.write(r.to_ndarray().tobytes())
        if cancel is not None and cancel.is_set():
            tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, out)
        return out
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None


def build_peaks(path: str) -> Optional[np.ndarray]:
    pk = peaks_cache_path(path)
    if pk.exists():
        try:
            return np.load(str(pk))
        except (OSError, ValueError):
            pass
    pcm = audio_cache_path(path)
    if not pcm.exists():
        return None
    data = np.memmap(str(pcm), dtype=np.int16, mode="r")
    frames = len(data) // CHANNELS
    if frames == 0:
        return None
    win = SAMPLE_RATE // PEAKS_PER_SEC
    n = frames // win
    out = np.zeros(n, dtype=np.float32)
    step = 4096
    for i in range(0, n, step):
        j = min(n, i + step)
        chunk = np.asarray(data[i * win * CHANNELS: j * win * CHANNELS], dtype=np.int16)
        chunk = np.abs(chunk.reshape(j - i, win * CHANNELS).astype(np.int32))
        out[i:j] = chunk.max(axis=1) / 32768.0
    np.save(str(pk), out)
    return out


class AudioSource:
    """キャッシュ済み PCM への読み取り専用アクセス。"""

    def __init__(self, path: str):
        p = audio_cache_path(path)
        self.data = np.memmap(str(p), dtype=np.int16, mode="r").reshape(-1, CHANNELS) \
            if p.exists() and p.stat().st_size >= 4 else None

    def read(self, start_sample: int, n: int) -> np.ndarray:
        """float32 (n, 2)。範囲外は無音。"""
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        if self.data is None:
            return out
        total = self.data.shape[0]
        a, b = max(0, start_sample), min(total, start_sample + n)
        if b > a:
            out[a - start_sample: b - start_sample] = self.data[a:b].astype(np.float32) / 32768.0
        return out


# ─────────────────────────────────────────────
#  映像デコード
# ─────────────────────────────────────────────

# GPU（NVDEC）でデコードできるもの
_GPU_CODECS = {"h264", "hevc", "av1", "vp9", "vp8", "mpeg2video", "mpeg4", "mjpeg"}
_GPU_PIXFMTS = {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le"}


class VideoDecoder:
    """1ファイル分のデコーダ。get(t) で t 秒のフレームを PIL.Image で返す。"""

    def __init__(self, path: str):
        import av
        self.path = path
        self.container = av.open(path)
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "AUTO"
        self.tb = float(self.stream.time_base)
        # 透過（アルファ）付きの動画か（Blender / After Effects の ProRes 4444・PNG・QTRLE など）
        pf = str(getattr(self.stream.codec_context, "pix_fmt", "") or "")
        self.has_alpha = pf.startswith(("rgba", "bgra", "argb", "abgr", "yuva", "gbrap", "ya8", "ya16", "pal8"))
        st = self.stream.start_time
        self.t0 = float(st * self.stream.time_base) if st is not None else 0.0
        rate = self.stream.average_rate or self.stream.guessed_rate
        self.frame_dur = 1.0 / float(rate) if rate else 1 / 30
        self._iter = None
        self._last = None          # (t, frame)
        self._next = None
        # NVIDIA の GPU でのデコード（続けて再生している間だけ。gpu.py）
        cc = self.stream.codec_context
        self._gpu_ok = (not self.has_alpha and str(cc.name) in _GPU_CODECS and pf in _GPU_PIXFMTS)
        self._gpu = None
        self._gpu_last = None
        self._gpu_prev = None

    def close(self):
        if self._gpu is not None:
            self._gpu.close()
            self._gpu = None
        try:
            self.container.close()
        except Exception:
            pass

    def _gpu_array(self, t: float, size):
        """続けて再生しているときは GPU（ffmpeg の CUDA デコード + 縮小）の絵を使う。使えなければ None。"""
        from . import gpu as G
        prev, self._gpu_prev = self._gpu_prev, t
        r = self._gpu
        if r is not None and r.covers(t, size):
            if not r.ready.is_set():
                return None                      # ffmpeg の立ち上がり中は今まで通り
            a = r.read_until(t, self._gpu_last)
            if a is not None:
                self._gpu_last = a
                return a
        if r is not None:
            r.close()
            self._gpu = None
            self._gpu_last = None
        if prev is not None and 0 < t - prev <= self.frame_dur * 3.5 and G.use_gpu_decode():
            try:
                self._gpu = G.GpuReader(self.path, t, size, self.frame_dur)
            except Exception:
                self._gpu_ok = False
        return None

    def _frames(self):
        for f in self.container.decode(self.stream):
            yield f

    def _seek(self, t: float):
        ts = int(max(0.0, t + self.t0) / self.tb)
        self.container.seek(ts, stream=self.stream, backward=True, any_frame=False)
        self._iter = self._frames()
        self._last = None
        self._next = None

    def get_array(self, t: float, size, alpha: bool = False) -> Optional[np.ndarray]:
        """t 秒のフレームを size に縮小した RGB（alpha=True なら RGBA）配列で返す（PIL を経由しないので速い）。"""
        if self._gpu_ok and not alpha:
            a = self._gpu_array(t, size)
            if a is not None:
                return a
        f = self._get_frame(t)
        if f is None:
            return None
        return f.to_ndarray(format="rgba" if alpha else "rgb24", width=size[0], height=size[1])

    def get(self, t: float, size=None) -> Optional[Image.Image]:
        f = self._get_frame(t)
        if f is None:
            return None
        if size:
            return f.to_image(width=size[0], height=size[1])
        return f.to_image()

    def _get_frame(self, t: float):
        eps = self.frame_dur * 0.5
        if self._last is not None:
            lt = self._last[0]
            if lt - eps <= t < lt + self.frame_dur - 1e-6:
                return self._last[1]
            # 少し先なら読み進める。後ろ or 遠い先ならシーク
            if not (lt < t < lt + 2.0):
                self._seek(t)
        else:
            self._seek(t)
        try:
            while True:
                if self._next is not None:
                    f, self._next = self._next, None
                else:
                    f = next(self._iter)
                ft = (f.pts or 0) * self.tb - self.t0
                if ft + self.frame_dur - 1e-6 > t:
                    if ft - eps > t and self._last is not None:
                        # 次のフレームが目標より先 → 1つ前を返す
                        self._next = f
                        return self._last[1]
                    self._last = (ft, f)
                    return f
                self._last = (ft, f)
        except StopIteration:
            return self._last[1] if self._last else None
        except Exception:
            self._iter = None
            self._last = None
            return None


class DecoderPool:
    """スレッドごとに使うデコーダ・画像のキャッシュ（LRU）。"""

    def __init__(self, max_decoders: int = 8):
        self._dec: "OrderedDict[str, VideoDecoder]" = OrderedDict()
        self._img: "OrderedDict[str, Image.Image]" = OrderedDict()
        self._aud: dict = {}
        self.max = max_decoders
        self.use_proxy = SETTINGS.use_proxy
        self._stamp: dict = {}

    def _changed(self, path: str) -> bool:
        """前回確認からファイルが更新されたか（1 秒に 1 回だけ確認）。"""
        now = time.time()
        rec = self._stamp.get(path)
        if rec is not None and now - rec[0] < 1.0:
            return False
        try:
            st = os.stat(path)
            sig = (st.st_size, st.st_mtime)
        except OSError:
            sig = None
        old = rec[1] if rec else None
        self._stamp[path] = (now, sig)
        return rec is not None and sig != old

    def video(self, path: str) -> Optional[VideoDecoder]:
        if self.use_proxy and has_proxy(path):
            path = str(proxy_path(path))
        if self._changed(path) and path in self._dec:
            self._dec.pop(path).close()
            invalidate(path)
        d = self._dec.get(path)
        if d is None:
            try:
                d = VideoDecoder(path)
            except Exception:
                return None
            self._dec[path] = d
            while len(self._dec) > self.max:
                _, old = self._dec.popitem(last=False)
                old.close()
        else:
            self._dec.move_to_end(path)
        return d

    def image(self, path: str) -> Optional[Image.Image]:
        if self._changed(path):
            self._img.pop(path, None)
            invalidate(path)
        im = self._img.get(path)
        if im is None:
            try:
                im = open_image(path).convert("RGBA")
            except Exception:
                return None
            self._img[path] = im
            while len(self._img) > 32:
                self._img.popitem(last=False)
        return im

    def audio(self, path: str) -> AudioSource:
        a = self._aud.get(path)
        if a is None or a.data is None:
            a = AudioSource(path)
            self._aud[path] = a
        return a

    def close(self):
        for d in self._dec.values():
            d.close()
        self._dec.clear()


def thumbnail(item: MediaItem, size=(160, 90)) -> Optional[Image.Image]:
    """ビンに表示するサムネイル。"""
    try:
        if item.kind == "image":
            im = open_image(item.path).convert("RGB")
        elif item.kind == "video":
            d = VideoDecoder(item.path)
            im = d.get(min(1.0, item.duration_s / 3))
            d.close()
            if im is None:
                return None
        else:
            return None
        im.thumbnail(size)
        bg = Image.new("RGB", size, (16, 16, 16))
        bg.paste(im, ((size[0] - im.width) // 2, (size[1] - im.height) // 2))
        return bg
    except Exception:
        return None


# ─────────────────────────────────────────────
#  プロキシ作成
# ─────────────────────────────────────────────

def build_proxy(item: MediaItem, cancel: Optional[threading.Event] = None, progress=None) -> Optional[Path]:
    """全イントラ（キーフレームのみ）の軽量 H.264 を作る。スクラブ・逆再生も軽い。"""
    if item.kind != "video":
        return None
    out = proxy_path(item.path)
    if has_proxy(item.path):
        return out
    import av
    from fractions import Fraction
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    w = min(SETTINGS.proxy_width, item.width or SETTINGS.proxy_width) & ~1
    h = max(2, int(round((item.height or 540) * w / max(1, item.width or 960)))) & ~1
    from . import gpu as G
    if G.use_nvenc("h264") and G.use_gpu_decode():
        # NVIDIA: CUDA でデコード → GPU で縮小 → NVENC（全イントラ）。CPU の何倍も速い
        ok = G.ffmpeg_transcode(item.path, str(tmp), G.nvenc_args("h264", 23, intra=True, fast=True), w, h,
                                audio=None, progress=(lambda p: progress(int(p * 1000), 1000)) if progress else None,
                                cancel=cancel, duration=item.duration_s or 0)
        if ok:
            os.replace(tmp, out)
            return out
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if cancel is not None and cancel.is_set():
            return None
    try:
        with av.open(item.path) as src:
            vs = src.streams.video[0]
            vs.thread_type = "AUTO"
            rate = vs.average_rate or vs.guessed_rate or Fraction(30)
            total = int((item.duration_s or 1) * float(rate))
            dst = av.open(str(tmp), "w")
            os_ = dst.add_stream("libx264", rate=Fraction(rate).limit_denominator(1001))
            os_.width, os_.height, os_.pix_fmt = w, h, "yuv420p"
            os_.codec_context.gop_size = 1
            os_.options = {"crf": "23", "preset": "veryfast", "tune": "fastdecode"}
            t0 = float(vs.start_time * vs.time_base) if vs.start_time is not None else 0.0
            tb = float(vs.time_base)
            last = -1
            for i, f in enumerate(src.decode(vs)):
                if cancel is not None and cancel.is_set():
                    dst.close()
                    tmp.unlink(missing_ok=True)
                    return None
                # 元のタイミングを保つ（可変フレームレートでも位置がずれないように）
                idx = int(round(((f.pts or 0) * tb - t0) * float(rate)))
                if idx <= last:
                    idx = last + 1
                last = idx
                nf = f.reformat(width=w, height=h, format="yuv420p")
                nf.pts = idx
                nf.time_base = os_.codec_context.time_base
                for pkt in os_.encode(nf):
                    dst.mux(pkt)
                if progress is not None and i % 15 == 0:
                    progress(i, total)
            for pkt in os_.encode(None):
                dst.mux(pkt)
            dst.close()
        os.replace(tmp, out)
        return out
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def copy_file(src: str, dst: str, cancel: Optional[threading.Event] = None, progress=None) -> bool:
    """大きなファイルを進捗付きでコピー（NAS → ローカル等）。途中で止めた場合は消す。"""
    tmp = dst + ".part"
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        total = os.path.getsize(src)
        done = 0
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            while True:
                if cancel is not None and cancel.is_set():
                    raise InterruptedError
                buf = fi.read(8 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                if progress is not None:
                    progress(done, total)
        shutil.copystat(src, tmp)
        os.replace(tmp, dst)
        return True
    except (OSError, InterruptedError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
