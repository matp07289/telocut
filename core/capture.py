"""スクリーンキャプチャ・ファイル監視ワーカースレッド (tkinter 向け queue ベース)"""
from __future__ import annotations
import os
import time
import queue
import threading
from pathlib import Path
from typing import Optional

try:
    import mss as _mss_mod
    HAS_MSS = True
except ImportError:
    HAS_MSS = False

try:
    from PIL import Image as _PILImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


# ─────────────────────────────────────────────
#  モニター情報 (マルチディスプレイ対応)
# ─────────────────────────────────────────────

def get_virtual_screen() -> dict:
    """全モニターを含む仮想デスクトップ全体の矩形を返す。
    返値: {"left", "top", "width", "height"}（left/top は負になりうる）。
    mss の座標系（物理ピクセル）に一致する。
    """
    if HAS_MSS:
        try:
            with _mss_mod.mss() as sct:
                return dict(sct.monitors[0])   # [0] = 全モニター結合
        except Exception:
            pass
    return {"left": 0, "top": 0, "width": 1920, "height": 1080}


def list_monitors() -> list[dict]:
    """
    利用可能なモニター一覧を返す。
    各要素: {"name", "left", "top", "width", "height"}
    先頭は「全モニター(仮想画面)」、以降は個別モニター。
    """
    if not HAS_MSS:
        return []
    try:
        with _mss_mod.mss() as sct:
            mons = sct.monitors        # [0]=全体, [1..]=個別
            result: list[dict] = []
            allm = mons[0]
            result.append({
                "name": f"全モニター ({allm['width']}x{allm['height']})",
                "left": allm["left"], "top": allm["top"],
                "width": allm["width"], "height": allm["height"],
            })
            for i, m in enumerate(mons[1:], start=1):
                result.append({
                    "name": f"モニター{i} ({m['width']}x{m['height']})  @({m['left']},{m['top']})",
                    "left": m["left"], "top": m["top"],
                    "width": m["width"], "height": m["height"],
                })
            return result
    except Exception:
        return []


# ─────────────────────────────────────────────
#  スクリーンキャプチャスレッド
# ─────────────────────────────────────────────

class ScreenCaptureThread(threading.Thread):
    """指定画面領域を連続キャプチャして out_queue に ("frame", PIL.Image) を積む。"""

    def __init__(self, out_queue: queue.Queue, fps: float = 15.0):
        super().__init__(daemon=True)
        self._q       = out_queue
        self._fps     = float(fps)
        self._region: Optional[dict] = None
        self._running = False
        self._lock    = threading.Lock()

    def set_region(self, x: int, y: int, w: int, h: int):
        with self._lock:
            self._region = {"left": x, "top": y, "width": w, "height": h}

    def clear_region(self):
        with self._lock:
            self._region = None

    def set_fps(self, fps: float):
        with self._lock:
            self._fps = max(1.0, min(30.0, float(fps)))

    def stop(self):
        self._running = False

    def run(self):
        if not HAS_MSS:
            self._q.put(("error", "mss が未インストールです\npip install mss"))
            return
        if not HAS_PILLOW:
            self._q.put(("error", "Pillow が未インストールです"))
            return

        self._running = True
        with _mss_mod.mss() as sct:
            while self._running:
                t0 = time.perf_counter()

                with self._lock:
                    region = dict(self._region) if self._region else None
                    fps    = self._fps

                if region and region["width"] > 0 and region["height"] > 0:
                    try:
                        shot = sct.grab(region)
                        # BGRA → RGBA (numpy が使えるなら高速パス)
                        try:
                            import numpy as _np
                            arr = _np.array(shot)[:, :, [2, 1, 0, 3]]
                            img = _PILImage.fromarray(arr, "RGBA")
                        except ImportError:
                            img = _PILImage.frombytes(
                                "RGBA", shot.size, bytes(shot.raw), "raw", "BGRA"
                            )
                        # キューを溜めすぎない
                        while self._q.qsize() > 2:
                            try:
                                self._q.get_nowait()
                            except queue.Empty:
                                break
                        self._q.put(("frame", img))
                    except Exception as e:
                        self._q.put(("error", f"キャプチャエラー: {e}"))

                elapsed = time.perf_counter() - t0
                remain  = 1.0 / fps - elapsed
                if remain > 0:
                    time.sleep(remain)


# ─────────────────────────────────────────────
#  単一ファイル監視スレッド (Photoshop PNG)
# ─────────────────────────────────────────────

class FileWatchThread(threading.Thread):
    """単一ファイルの更新を監視して out_queue に ("frame", PIL.Image) を積む。"""

    POLL = 0.15

    def __init__(self, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self._q:          queue.Queue  = out_queue
        self._path:       Optional[str] = None
        self._last_mtime: float        = 0.0
        self._running:    bool         = False

    def set_path(self, path: str):
        self._path       = path
        self._last_mtime = 0.0

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            path = self._path
            if path and os.path.isfile(path):
                try:
                    mtime = os.path.getmtime(path)
                    if mtime != self._last_mtime:
                        self._last_mtime = mtime
                        time.sleep(0.05)   # 書き込み完了を待つ
                        img = _PILImage.open(path).convert("RGBA")
                        self._q.put(("frame", img))
                        self._q.put(("status", "更新を検出"))
                except Exception as e:
                    self._q.put(("error", f"読込エラー: {e}"))
            time.sleep(self.POLL)


# ─────────────────────────────────────────────
#  DavinTelop 出力フォルダ監視スレッド
# ─────────────────────────────────────────────

class DirWatchThread(threading.Thread):
    """telop_*.png の変更を監視して out_queue に ("dir_changed", [path, ...]) を積む。"""

    POLL = 0.5

    def __init__(self, out_queue: queue.Queue):
        super().__init__(daemon=True)
        self._q:       queue.Queue  = out_queue
        self._dir:     Optional[str] = None
        self._mtimes:  dict         = {}
        self._running: bool         = False

    def set_dir(self, dir_path: str):
        self._dir    = dir_path
        self._mtimes = {}

    def stop(self):
        self._running = False

    def run(self):
        self._running = True
        while self._running:
            dir_path = self._dir
            if dir_path and os.path.isdir(dir_path):
                try:
                    pngs = sorted(Path(dir_path).glob("telop_*.png"))
                    changed = False
                    for p in pngs:
                        mtime = p.stat().st_mtime
                        key   = str(p)
                        if self._mtimes.get(key) != mtime:
                            self._mtimes[key] = mtime
                            changed = True
                    if changed:
                        self._q.put(("dir_changed", [str(p) for p in pngs]))
                except Exception as e:
                    self._q.put(("error", f"フォルダ監視エラー: {e}"))
            time.sleep(self.POLL)


# ─────────────────────────────────────────────
#  NDI 受信スレッド
# ─────────────────────────────────────────────

try:
    import NDIlib as _ndi
    HAS_NDI = _ndi.initialize()
    if not HAS_NDI:
        _ndi = None
except ImportError:
    _ndi   = None
    HAS_NDI = False

# NDI Tools インストーラーの既知ダウンロード URL
NDI_DOWNLOAD_URLS: dict[str, str] = {
    "NDI 6 Tools (最新 / 推奨)":
        "https://downloads.ndi.tv/Tools/NDI%206%20Tools.exe",
    "NDI 5 Tools (旧バージョン)":
        "https://downloads.ndi.tv/Tools/NDI%205%20Tools.exe",
}
NDI_TOOLS_PAGE = "https://ndi.video/tools/"

# Windows での NDI Tools インストールパス候補
_NDI_INSTALL_DIRS: list[str] = [
    r"C:\Program Files\NDI\NDI 6 Tools",
    r"C:\Program Files\NDI\NDI 5 Tools",
    r"C:\Program Files\NDI\NDI 4 Tools",
    r"C:\Program Files\NewTek\NDI 5 SDK",
    r"C:\Program Files\NewTek\NDI 4 SDK",
]


def check_ndi_installation() -> dict:
    """
    NDI Tools のインストール状態を確認する。
    返値:
        {
          "ndilib_ok":   bool  – NDIlib で initialize() が成功したか
          "dll_found":   bool  – DLL ファイルが存在するか
          "install_dir": str   – インストールフォルダ (見つかった場合)
          "dll_path":    str   – DLL パス (見つかった場合)
        }
    """
    info = {
        "ndilib_ok":   HAS_NDI,
        "dll_found":   False,
        "install_dir": "",
        "dll_path":    "",
    }
    import platform
    if platform.system() != "Windows":
        return info

    for d in _NDI_INSTALL_DIRS:
        dll = os.path.join(d, "Processing.NDI.Lib.x64.dll")
        if os.path.exists(dll):
            info["dll_found"]   = True
            info["install_dir"] = d
            info["dll_path"]    = dll
            return info

    # ndi-python バンドルの DLL を確認
    try:
        import NDIlib as _check_ndi
        pkg_dir = os.path.dirname(_check_ndi.__file__)
        bundled = os.path.join(pkg_dir, "Processing.NDI.Lib.x64.dll")
        if os.path.exists(bundled):
            info["dll_found"]   = True
            info["install_dir"] = pkg_dir
            info["dll_path"]    = bundled
    except Exception:
        pass

    return info


def scan_ndi_sources(timeout_ms: int = 3000) -> list[str]:
    """
    ネットワーク/ローカル上の NDI ソース名を検索して返す。
    DaVinci Resolve や Premiere が NDI 出力していれば検出される。

    注意: ndi-python の find_wait_for_sources は呼び出し中 GIL を保持するため、
    長いタイムアウトで一括待機すると呼び出し元スレッドがブロックされる。
    短い待機を繰り返し、間に sleep を挟んで GIL を解放する。
    """
    if not HAS_NDI or _ndi is None:
        return []
    finder = None
    try:
        finder = _ndi.find_create_v2()
        deadline = time.time() + timeout_ms / 1000.0
        names: list[str] = []
        while time.time() < deadline:
            _ndi.find_wait_for_sources(finder, 100)   # 短く待つ（GIL保持を短縮）
            sources = _ndi.find_get_current_sources(finder)
            if sources:
                names = [s.ndi_name for s in sources]
                break
            time.sleep(0.03)   # GIL を解放して呼び出し元を進ませる
        return names
    except Exception:
        return []
    finally:
        if finder is not None:
            try:
                _ndi.find_destroy(finder)
            except Exception:
                pass


class NDIReceiverThread(threading.Thread):
    """
    NDI ソースを受信して out_queue に ("frame", PIL.Image) を積む。

    Resolve / Premiere Pro が NDI 出力している場合にリアルタイム受信する。
    使い方:
        t = NDIReceiverThread(q, source_name="RESOLVE (Program)")
        t.start()
        # q から ("frame", PIL.Image RGBA) を取り出してプレビューに使う
    """

    TIMEOUT_MS = 100   # recv_capture_v2 タイムアウト (ms)

    def __init__(self, out_queue: queue.Queue, source_name: str = ""):
        super().__init__(daemon=True)
        self._q           = out_queue
        self._source_name = source_name
        self._running     = False

    def set_source(self, name: str):
        self._source_name = name

    def stop(self):
        self._running = False

    def run(self):
        if not HAS_NDI or _ndi is None:
            self._q.put(("error",
                "NDIlib が未インストールまたは初期化失敗\n"
                "NDI Tools をインストール後に再起動してください\n"
                "https://ndi.video/tools/"))
            return
        if not HAS_PILLOW:
            self._q.put(("error", "Pillow が未インストールです"))
            return

        self._running = True
        self._q.put(("status", f"NDI ソース検索中: {self._source_name}"))

        # ── ソース検索 ───────────────────────────────
        # find_wait_for_sources は GIL を保持するため短時間（200ms）に分割し、
        # 間に sleep を入れて呼び出し元（GUI）スレッドを進ませる。
        target = None
        finder = _ndi.find_create_v2()
        try:
            for _ in range(100):   # 100ms * 100 = 最大 ~10 秒
                if not self._running:
                    return
                _ndi.find_wait_for_sources(finder, 100)
                sources = _ndi.find_get_current_sources(finder)
                if sources:
                    if self._source_name:
                        for s in sources:
                            if self._source_name.lower() in s.ndi_name.lower():
                                target = s
                                break
                    if target is None:
                        target = sources[0]     # 名前未指定なら最初のソース
                if target:
                    break
                time.sleep(0.05)   # GIL 解放
        finally:
            _ndi.find_destroy(finder)

        if target is None:
            self._q.put(("error", f"NDI ソースが見つかりません: {self._source_name}"))
            return

        self._q.put(("status", f"NDI 接続: {target.ndi_name}"))

        # ── レシーバー作成 ────────────────────────────
        recv_desc = _ndi.RecvCreateV3()
        recv_desc.color_format       = _ndi.RECV_COLOR_FORMAT_RGBX_RGBA
        recv_desc.bandwidth          = _ndi.RECV_BANDWIDTH_HIGHEST
        recv_desc.allow_video_fields = False
        recv = _ndi.recv_create_v3(recv_desc)
        if recv is None:
            self._q.put(("error", "NDI レシーバー作成失敗"))
            return

        # ndi-python では descriptor の source_to_connect_to だけでは接続されない。
        # 明示的に recv_connect を呼ぶ必要がある（公式サンプル準拠）。
        try:
            _ndi.recv_connect(recv, target)
        except Exception as e:
            self._q.put(("error", f"NDI 接続失敗: {e}"))
            _ndi.recv_destroy(recv)
            return

        # ── フレーム受信ループ ────────────────────────
        # recv_capture_v2 は GIL を保持するためタイムアウトを短くし、
        # 各反復で sleep を入れて GUI スレッドに GIL を渡す（フリーズ防止）。
        # プレビュー用途なので受信レートを ~20fps に制限して CPU 負荷も抑える。
        frame_count = 0
        fps_time    = time.time()
        status_typo = getattr(_ndi, 'FRANE_TYPE_STATUS_CHANGE',
                              getattr(_ndi, 'FRAME_TYPE_STATUS_CHANGE', None))
        min_interval = 1.0 / 20.0   # 最大 20fps
        last_emit    = 0.0

        try:
            while self._running:
                loop_start = time.time()
                t, video, audio, meta = _ndi.recv_capture_v2(recv, 30)

                if t == _ndi.FRAME_TYPE_VIDEO and video is not None:
                    # レート制限: 直近の送出から min_interval 未満なら破棄
                    if loop_start - last_emit < min_interval:
                        _ndi.recv_free_video_v2(recv, video)
                    else:
                        img = None
                        try:
                            import numpy as _np
                            arr = _np.array(video.data)   # (H, W, 4) RGBX/RGBA
                            # 背景映像は不透明として扱う。RGBX の X(4ch目) は
                            # パディングで 0 のことがあり、そのまま RGBA にすると
                            # 完全透明になり表示されない → RGB(先頭3ch) のみ使用。
                            if arr.ndim == 3 and arr.shape[2] >= 3:
                                rgb = _np.ascontiguousarray(arr[:, :, :3])
                                img = _PILImage.fromarray(rgb, "RGB")
                        except Exception:
                            # numpy 不可時のフォールバック（RGBA→RGB変換）
                            try:
                                w, h = video.xres, video.yres
                                raw  = bytes(video.data)
                                img  = _PILImage.frombytes("RGBA", (w, h), raw).convert("RGB")
                            except Exception:
                                img = None
                        finally:
                            _ndi.recv_free_video_v2(recv, video)

                        if img is not None:
                            # プレビュー用に縮小（4K をそのまま GUI に渡すと重い）
                            if img.width > 1280:
                                ratio = 1280 / img.width
                                try:
                                    img = img.resize(
                                        (1280, max(1, int(img.height * ratio))),
                                        _PILImage.BILINEAR)
                                except Exception:
                                    pass
                            while self._q.qsize() > 2:
                                try:
                                    self._q.get_nowait()
                                except queue.Empty:
                                    break
                            self._q.put(("frame", img))
                            last_emit = loop_start
                            frame_count += 1

                    now = time.time()
                    if now - fps_time >= 1.0:
                        fps = frame_count / (now - fps_time)
                        self._q.put(("status", f"NDI 受信中  {fps:.1f} fps  {target.ndi_name}"))
                        frame_count = 0
                        fps_time    = now

                elif t == _ndi.FRAME_TYPE_AUDIO and audio is not None:
                    # 音声フレームは使わないが必ず解放（リーク防止）
                    try:
                        _ndi.recv_free_audio_v2(recv, audio)
                    except Exception:
                        pass

                elif t == _ndi.FRAME_TYPE_METADATA and meta is not None:
                    try:
                        _ndi.recv_free_metadata(recv, meta)
                    except Exception:
                        pass

                elif t == _ndi.FRAME_TYPE_ERROR:
                    self._q.put(("error", "NDI ストリームエラー"))
                    break

                elif status_typo is not None and t == status_typo:
                    self._q.put(("status", "NDI 接続状態変化"))

                # GIL を解放して GUI スレッドを進ませる（フリーズ防止の要）
                time.sleep(0.01)

        finally:
            try:
                _ndi.recv_destroy(recv)
            except Exception:
                pass
