"""Blackmagic Design の入出力機器（DeckLink / UltraStudio / Intensity / Avid Artist DNxIO など）と Desktop Video。

Desktop Video（ドライバ）の DeckLink API（COM・DeckLinkAPI64.dll）を comtypes で使う。
- 機器の一覧・API の版・出力できる形式（1080p29.97 など）
- 出力: TeloCut のレコードモニターの絵を機器に出す（外部モニター・放送用モニターで確認）
  シーケンスの大きさ・フレームレートに合う形式を自動で選ぶ。8bit BGRA で出せない機器は 8bit YUV（4:2:2）に変換
- つないだ / 外したことの見張り（数秒ごとに一覧を見る）
COM は使うスレッドごとに初期化する（出力は専用のスレッド）。
"""
from __future__ import annotations

import os
import queue
import threading
from typing import List, Optional, Tuple

import numpy as np

DV_DIR = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Blackmagic Design", "Blackmagic Desktop Video")
API_DLL = os.path.join(DV_DIR, "DeckLinkAPI64.dll")
SETUP_EXE = os.path.join(DV_DIR, "BlackmagicDesktopVideoSetup.exe")
MEDIA_EXPRESS = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Blackmagic Design", "Media Express",
                             "Blackmagic Media Express.exe")
_D = None


def _api():
    """DeckLink API の型（comtypes.gen.DeckLinkAPI）。Desktop Video が無ければ None。"""
    global _D
    if _D is not None:
        return _D
    if os.name != "nt" or not os.path.exists(API_DLL):
        return None
    try:
        import comtypes.client as cc
        cc.GetModule(API_DLL)
        import comtypes.gen.DeckLinkAPI as D
        _D = D
    except Exception:
        _D = None
    return _D


def _co_init():
    try:
        import comtypes
        comtypes.CoInitialize()
    except OSError:
        pass


def installed() -> bool:
    return _api() is not None


def api_version() -> str:
    D = _api()
    if D is None:
        return ""
    try:
        import comtypes.client as cc
        _co_init()
        info = cc.CreateObject(D.CDeckLinkAPIInformation, interface=D.IDeckLinkAPIInformation)
        v = int(info.GetInt(D.BMDDeckLinkAPIVersion))
        return f"{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"
    except Exception:
        return "?"


def _iterate():
    D = _api()
    if D is None:
        return []
    import comtypes.client as cc
    _co_init()
    try:
        it = cc.CreateObject(D.CDeckLinkIterator, interface=D.IDeckLinkIterator)
    except Exception:
        return []
    out = []
    while True:
        try:
            dl = it.Next()
        except Exception:
            break
        if not dl:
            break
        out.append(dl)
    return out


def devices() -> List[dict]:
    """[{index, name, model, input, output}]"""
    D = _api()
    res = []
    for i, dl in enumerate(_iterate()):
        d = {"index": i, "name": "", "model": "", "input": False, "output": False}
        try:
            d["name"] = dl.GetDisplayName()
            d["model"] = dl.GetModelName()
        except Exception:
            pass
        for key, itf in (("output", D.IDeckLinkOutput), ("input", D.IDeckLinkInput)):
            try:
                dl.QueryInterface(itf)
                d[key] = True
            except Exception:
                pass
        res.append(d)
    return res


def display_modes(index: int = 0) -> List[dict]:
    """出力できる形式 [{name, width, height, fps, mode, progressive}]"""
    D = _api()
    devs = _iterate()
    if D is None or index >= len(devs):
        return []
    try:
        out = devs[index].QueryInterface(D.IDeckLinkOutput)
        it = out.GetDisplayModeIterator()
    except Exception:
        return []
    res = []
    while True:
        m = it.Next()
        if not m:
            break
        fd, ts = m.GetFrameRate()
        res.append({"name": m.GetName(), "width": m.GetWidth(), "height": m.GetHeight(),
                    "fps": round(ts / max(1, fd), 3), "mode": int(m.GetDisplayMode()),
                    "progressive": int(m.GetFieldDominance()) == 0x70726f67})     # 'prog'
    return res


def best_mode(modes: List[dict], width: int, height: int, fps: float) -> Optional[dict]:
    """シーケンスに合う形式（同じ大きさ・近いフレームレート・プログレッシブ優先）。無ければ 1080p の近いもの。"""
    def score(m):
        return (m["width"] != width or m["height"] != height, not m["progressive"], abs(m["fps"] - fps))
    same = [m for m in modes if abs(m["fps"] - fps) < 0.02 or abs(m["fps"] * 2 - fps) < 0.02]
    pool = same or modes
    if not pool:
        return None
    best = min(pool, key=score)
    if (best["width"], best["height"]) != (width, height):
        hd = [m for m in modes if (m["width"], m["height"]) == (1920, 1080) and m["progressive"]]
        if hd:
            best = min(hd, key=lambda m: abs(m["fps"] - fps))
    return best


_M709 = np.array([[0.1826, 0.6142, 0.0620], [-0.1006, -0.3386, 0.4392], [0.4392, -0.3989, -0.0403]], np.float32)
_OFS = np.array([16, 128, 128], np.float32)


def yuv_planes(rgb: np.ndarray):
    """RGB → Y（H×W）・U・V（H×W/2）の 8bit（BT.709・ビデオレベル）。"""
    h, w = rgb.shape[:2]
    w -= w % 2
    yuv = rgb[:, :w].reshape(-1, 3).astype(np.float32) @ _M709.T
    yuv = yuv.reshape(h, w, 3)
    y = np.clip(yuv[..., 0] + 16.5, 16, 235).astype(np.uint8)
    u = np.clip((yuv[:, 0::2, 1] + yuv[:, 1::2, 1]) * 0.5 + 128.5, 16, 240).astype(np.uint8)
    v = np.clip((yuv[:, 0::2, 2] + yuv[:, 1::2, 2]) * 0.5 + 128.5, 16, 240).astype(np.uint8)
    return y, u, v


def rgb_to_uyvy(rgb: np.ndarray, size: Optional[Tuple[int, int]] = None) -> np.ndarray:
    """RGB（H×W×3）→ 8bit YUV 4:2:2（UYVY）。size=(幅, 高さ) なら色を変換してから広げる（小さい絵のまま変換するので速い）。"""
    y, u, v = yuv_planes(rgb)
    if size is not None and (y.shape[1], y.shape[0]) != tuple(size):
        from PIL import Image
        W, H = int(size[0]) & ~1, int(size[1])
        y = np.asarray(Image.fromarray(y).resize((W, H), Image.BILINEAR))
        # 色差は目立ちにくいので速い最近傍で広げる（明るさはなめらかに）
        u = np.asarray(Image.fromarray(u).resize((W // 2, H), Image.NEAREST))
        v = np.asarray(Image.fromarray(v).resize((W // 2, H), Image.NEAREST))
    h, w = y.shape
    out = np.empty((h, w * 2), np.uint8)
    out[:, 0::4] = u
    out[:, 1::4] = y[:, 0::2]
    out[:, 2::4] = v
    out[:, 3::4] = y[:, 1::2]
    return out


class Output:
    """1 台の機器に絵を出す（このオブジェクトを作ったスレッドで使う）。"""

    def __init__(self, index: int, width: int, height: int, fps: float):
        import comtypes.client  # noqa: F401
        D = _api()
        if D is None:
            raise RuntimeError("Blackmagic Desktop Video が入っていません")
        _co_init()
        devs = _iterate()
        if index >= len(devs):
            raise RuntimeError("Blackmagic の機器が見つかりません")
        self.D = D
        self.name = devs[index].GetDisplayName()
        self.out = devs[index].QueryInterface(D.IDeckLinkOutput)
        modes = display_modes(index)
        m = best_mode(modes, width, height, fps)
        if m is None:
            raise RuntimeError("出力できる形式がありません")
        self.mode = m
        self.w, self.h = m["width"], m["height"]
        # BGRA で出せるか（出せなければ YUV 4:2:2）
        self.fmt = D.bmdFormat8BitBGRA
        if not self._supported(D.bmdFormat8BitBGRA):
            self.fmt = D.bmdFormat8BitYUV
        self.out.EnableVideoOutput(m["mode"], D.bmdVideoOutputFlagDefault)
        self.row = self.w * 4 if self.fmt == D.bmdFormat8BitBGRA else self.w * 2
        self.frame = self.out.CreateVideoFrame(self.w, self.h, self.row, self.fmt, D.bmdFrameFlagDefault)
        self.closed = False

    def _supported(self, fmt) -> bool:
        try:
            r = self.out.DoesSupportVideoMode(0, self.mode["mode"], fmt, 0, self.D.bmdSupportedVideoModeDefault)
            return bool(r[-1] if isinstance(r, tuple) else r)
        except Exception:
            return fmt == self.D.bmdFormat8BitYUV

    def send(self, rgb: np.ndarray):
        """RGB の絵（大きさは何でもよい。形式の大きさに合わせる）を出す。"""
        import ctypes
        if self.fmt == self.D.bmdFormat8BitBGRA:
            if rgb.shape[0] != self.h or rgb.shape[1] != self.w:
                from PIL import Image
                rgb = np.asarray(Image.fromarray(rgb).resize((self.w, self.h), Image.BILINEAR))
            data = np.empty((self.h, self.w, 4), np.uint8)
            data[..., 0] = rgb[..., 2]
            data[..., 1] = rgb[..., 1]
            data[..., 2] = rgb[..., 0]
            data[..., 3] = 255
        else:
            data = rgb_to_uyvy(rgb, (self.w, self.h))
        data = np.ascontiguousarray(data)
        buf = self.frame.QueryInterface(self.D.IDeckLinkVideoBuffer)
        buf.StartAccess(self.D.bmdBufferAccessWrite)
        try:
            ptr = buf.GetBytes()
            addr = ptr if isinstance(ptr, int) else ctypes.cast(ptr, ctypes.c_void_p).value
            ctypes.memmove(addr, data.ctypes.data, min(data.nbytes, self.row * self.h))
        finally:
            buf.EndAccess(self.D.bmdBufferAccessWrite)
        self.out.DisplayVideoFrameSync(self.frame)

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.out.DisableVideoOutput()
        except Exception:
            pass


class OutputThread(threading.Thread):
    """出力専用のスレッド: 一番新しい絵だけを機器に出す（再生が速くても溜めない）。"""

    def __init__(self, index: int, width: int, height: int, fps: float, on_status=None):
        super().__init__(daemon=True)
        self.args = (index, width, height, fps)
        self.q: "queue.Queue" = queue.Queue(maxsize=1)
        self.on_status = on_status or (lambda s: None)
        self.running = True
        self.sent = 0
        self.error = ""
        self.info = ""
        self.ready = threading.Event()

    def push(self, rgb: np.ndarray):
        try:
            self.q.get_nowait()
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(rgb)
        except queue.Full:
            pass

    def stop(self):
        self.running = False
        self.push(None)

    def run(self):
        try:
            o = Output(*self.args)
        except Exception as ex:
            self.error = str(ex)
            self.on_status(f"Blackmagic に出せません: {ex}")
            self.ready.set()
            return
        fmt = "BGRA" if o.fmt == o.D.bmdFormat8BitBGRA else "YUV 4:2:2"
        self.info = f"{o.name}: {o.mode['name']}（{fmt}）"
        self.on_status(f"Blackmagic に出力中 — {self.info}")
        self.ready.set()
        try:
            while self.running:
                try:
                    img = self.q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if img is None:
                    break
                try:
                    o.send(img)
                    self.sent += 1
                except Exception as ex:
                    self.error = str(ex)
                    self.on_status(f"Blackmagic への出力でエラー: {ex}")
                    break
        finally:
            o.close()


def test_pattern(w: int = 1920, h: int = 1080) -> np.ndarray:
    """カラーバー（75%）。"""
    cols = [(191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0), (191, 0, 191), (191, 0, 0), (0, 0, 191)]
    img = np.zeros((h, w, 3), np.uint8)
    for i, c in enumerate(cols):
        img[:, i * w // 7:(i + 1) * w // 7] = c
    return img
