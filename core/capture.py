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

try:
    import cv2 as _cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


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
#  映像キャプチャデバイス (Blackmagic / カメラ等)
# ─────────────────────────────────────────────

def list_capture_devices() -> list[str]:
    """
    DirectShow の映像入力デバイス名一覧を返す。
    例: ['Blackmagic WDM Capture', 'Decklink Video Capture', ...]
    インデックスが OpenCV(CAP_DSHOW) のデバイス番号に対応する。
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


class CaptureDeviceThread(threading.Thread):
    """
    OpenCV(DirectShow) で映像キャプチャデバイスを取り込み、
    out_queue に ("frame", PIL.Image) を積む。
    Blackmagic WDM Capture / DeckLink Video Capture / Webカメラ等に対応。
    """

    def __init__(self, out_queue: queue.Queue, device_index: int = 0,
                 fps: float = 30.0):
        super().__init__(daemon=True)
        self._q       = out_queue
        self._index   = int(device_index)
        self._fps     = float(fps)
        self._running = False

    def stop(self):
        self._running = False

    def run(self):
        if not HAS_CV2:
            self._q.put(("error",
                "OpenCV が未インストールです\npip install opencv-python"))
            return
        if not HAS_PILLOW:
            self._q.put(("error", "Pillow が未インストールです"))
            return

        self._running = True
        self._q.put(("status", f"デバイス {self._index} を開いています..."))

        # DirectShow バックエンドで開く（Blackmagic WDM Capture 等）
        cap = None
        try:
            cap = _cv2.VideoCapture(self._index, _cv2.CAP_DSHOW)
        except Exception as e:
            self._q.put(("error", f"デバイスを開けません: {e}"))
            return

        if cap is None or not cap.isOpened():
            self._q.put(("error",
                f"キャプチャデバイス {self._index} を開けませんでした。\n"
                "他アプリが使用中でないか、信号が来ているか確認してください。"))
            if cap is not None:
                cap.release()
            return

        self._q.put(("status", "キャプチャ中..."))

        import numpy as _np
        frame_count = 0
        fps_time    = time.time()
        interval    = 1.0 / max(1.0, self._fps)
        no_frame    = 0

        try:
            while self._running:
                t0 = time.perf_counter()
                ok, frame = cap.read()

                if not ok or frame is None:
                    no_frame += 1
                    if no_frame == 30:
                        self._q.put(("status",
                            "信号待ち... (デバイスは開いていますが映像がありません)"))
                    time.sleep(0.05)
                    continue
                no_frame = 0

                try:
                    # OpenCV は BGR。RGB へ変換して PIL Image 化
                    rgb = _np.ascontiguousarray(frame[:, :, ::-1])
                    img = _PILImage.fromarray(rgb, "RGB")
                    # プレビュー用に縮小
                    if img.width > 1280:
                        ratio = 1280 / img.width
                        img = img.resize(
                            (1280, max(1, int(img.height * ratio))),
                            _PILImage.BILINEAR)
                    while self._q.qsize() > 2:
                        try:
                            self._q.get_nowait()
                        except queue.Empty:
                            break
                    self._q.put(("frame", img))
                    frame_count += 1
                except Exception:
                    pass

                now = time.time()
                if now - fps_time >= 1.0:
                    fps = frame_count / (now - fps_time)
                    self._q.put(("status", f"キャプチャ中  {fps:.1f} fps"))
                    frame_count = 0
                    fps_time    = now

                # フレームレート制限 + GIL 解放
                elapsed = time.perf_counter() - t0
                remain  = interval - elapsed
                if remain > 0:
                    time.sleep(remain)
                else:
                    time.sleep(0.005)
        finally:
            try:
                cap.release()
            except Exception:
                pass


# ─────────────────────────────────────────────
#  スクリーンキャプチャスレッド
# ─────────────────────────────────────────────

class TeloCutFrameThread(threading.Thread):
    """TeloCut の再生ヘッドの絵を取り続けて out_queue に ("frame", PIL.Image) を積む（画面キャプチャより正確・軽い）。
    TeloCut が起動していなければ待ち続け、起動したら自動でつながる。"""

    def __init__(self, out_queue: queue.Queue, fps: float = 12.0, width: int = 960, telops: bool = False):
        super().__init__(daemon=True)
        self._q = out_queue
        self.fps = max(1.0, min(30.0, float(fps)))
        self.width = int(width)
        self.telops = bool(telops)
        self._running = False

    def stop(self):
        self._running = False

    def run(self):
        from core import telocut_link as L
        self._running = True
        client = None
        waiting = False
        last_tc = ""
        while self._running:
            t0 = time.perf_counter()
            try:
                if client is None:
                    client = L.FrameClient(timeout=3.0)
                    self._q.put(("status", "TeloCut につながりました"))
                    waiting = False
                img, tc = client.get_frame(self.width, self.telops)
                if img is not None:
                    self._q.put(("frame", img))
                if tc and tc != last_tc:
                    last_tc = tc
                    self._q.put(("status", f"TeloCut  {tc}"))
            except Exception as ex:
                if client is not None:
                    client.close()
                client = None
                if not waiting:
                    self._q.put(("status", f"TeloCut を待っています…（{str(ex)[:30]}）"))
                    waiting = True
                time.sleep(1.0)
                continue
            wait = 1.0 / self.fps - (time.perf_counter() - t0)
            if wait > 0:
                time.sleep(wait)
        if client is not None:
            client.close()


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


# ─────────────────────────────────────────────
#  NDI 送信 (合成映像を NDI ソースとして配信)
# ─────────────────────────────────────────────

class NDISender:
    """
    合成映像を NDI ソースとして配信する。
    NDI Studio Monitor 等で受信し、DeckLink/DNxIO の SDI 出力へ
    ルーティングできる（Studio Monitor のビデオ出力機能等）。

    使い方:
        s = NDISender("DavinTelop")
        ok, err = s.start()
        s.send_pil(pil_image)   # 合成更新のたびに呼ぶ
        s.stop()
    """

    def __init__(self, name: str = "DavinTelop", fps_n: int = 30000, fps_d: int = 1001):
        self._name    = name
        self._fps_n   = fps_n
        self._fps_d   = fps_d
        self._sender  = None
        self._buf     = None          # numpy バッファの参照を保持
        self._lock    = threading.Lock()

    @property
    def active(self) -> bool:
        return self._sender is not None

    def start(self) -> tuple:
        """送信を開始。返値 (成功bool, エラー文字列)。"""
        if not HAS_NDI or _ndi is None:
            return False, ("NDI が利用できません。\n"
                           "NDI Tools をインストールして再起動してください。")
        try:
            sc = _ndi.SendCreate()
            sc.ndi_name = self._name
            with self._lock:
                self._sender = _ndi.send_create(sc)
            if self._sender is None:
                return False, "NDI 送信の作成に失敗しました。"
            return True, ""
        except Exception as e:
            return False, f"NDI 送信開始エラー: {e}"

    def send_pil(self, img) -> None:
        """PIL.Image（RGB/RGBA）を1フレーム送信する。"""
        if self._sender is None or not HAS_NDI:
            return
        try:
            import numpy as _np
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            # NDI は偶数サイズを要求するため奇数なら切り詰める
            w = img.width  - (img.width  % 2)
            h = img.height - (img.height % 2)
            if (w, h) != img.size:
                img = img.crop((0, 0, w, h))
            rgba = _np.asarray(img)
            # RGBA → BGRA（R と B を入替）して連続配列化
            bgra = _np.ascontiguousarray(rgba[:, :, [2, 1, 0, 3]])
            self._buf = bgra      # 送信完了まで参照保持

            vf = _ndi.VideoFrameV2()
            vf.data         = bgra
            vf.FourCC       = _ndi.FOURCC_VIDEO_TYPE_BGRA
            vf.xres         = bgra.shape[1]
            vf.yres         = bgra.shape[0]
            vf.frame_rate_N = self._fps_n
            vf.frame_rate_D = self._fps_d
            with self._lock:
                if self._sender is not None:
                    _ndi.send_send_video_v2(self._sender, vf)
        except Exception:
            pass

    def stop(self) -> None:
        with self._lock:
            if self._sender is not None:
                try:
                    _ndi.send_destroy(self._sender)
                except Exception:
                    pass
                self._sender = None
        self._buf = None
