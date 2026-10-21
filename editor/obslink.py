"""OBS Studio 連携: 録画が終わったら 自動で取り込み → 変換 → 無音カット → シーケンスを作る。

- obs-websocket v5（OBS 28 以降に内蔵。ツール → WebSocket サーバー設定）につなぐ。録画の開始 / 停止を TeloCut からもできる
  録画が止まった通知（RecordStateChanged・outputPath 付き）で処理を始める
- つながらないときは 録画フォルダの見張り（新しい動画ができて大きさが止まったら）でも動く
- 変換: MKV / FLV / TS → MP4（再エンコードなしで入れ替え・速い）/ 編集向け（NVIDIA なら NVENC）/ そのまま
  OBS の複数の音声トラック（マイク・デスクトップ音声 など）は別々の WAV にして、別の音声トラックに並べる
- 無音カット: 選んだ音声トラック（ふつうはマイク）が しきい値より小さい所を詰めて、話している所だけのシーケンスを作る

WebSocket は標準ライブラリだけで（クライアントの最小限: テキストのやり取り・ping / pong・閉じる）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
VIDEO_EXT = {".mkv", ".mp4", ".mov", ".flv", ".ts", ".m4v", ".fragmented.mp4"}


# ─────────────────────────── WebSocket（最小限のクライアント）───────────────────────────
class WebSocket:
    def __init__(self, host: str, port: int, path: str = "/", timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n")
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("WebSocket の接続が切れました")
            head += chunk
        head, self._buf = head.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("WebSocket の接続を断られました: " + head.split(b"\r\n")[0].decode("latin1"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        if accept not in head:
            raise ConnectionError("WebSocket の応答が正しくありません")
        self.lock = threading.Lock()
        self.closed = False

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket の接続が切れました")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.lock:
            self.sock.sendall(head + mask + body)

    def send(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> Optional[str]:
        """次のテキスト（閉じたら None）。ping には自動で pong。"""
        parts = b""
        while True:
            b1, b2 = self._recv_exact(2)
            op = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else b""
            data = self._recv_exact(n)
            if mask:
                data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
            if op == 0x8:
                self.closed = True
                return None
            if op == 0x9:
                self._send_frame(0xA, data)
                continue
            if op == 0xA:
                continue
            parts += data
            if b1 & 0x80:
                return parts.decode("utf-8", "replace")

    def close(self):
        if not self.closed:
            try:
                self._send_frame(0x8, b"")
            except OSError:
                pass
        self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


# ─────────────────────────── obs-websocket v5 ───────────────────────────
SUB_GENERAL, SUB_OUTPUTS = 1, 64


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    """OBS につないで、録画の開始 / 停止・録画フォルダを扱い、録画が終わったら on_recorded(パス) を呼ぶ。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 4455, password: str = "",
                 on_recorded: Optional[Callable[[str], None]] = None,
                 on_event: Optional[Callable[[str, dict], None]] = None):
        self.host, self.port, self.password = host, int(port), password
        self.on_recorded = on_recorded or (lambda p: None)
        self.on_event = on_event or (lambda t, d: None)
        self.ws: Optional[WebSocket] = None
        self._pending: Dict[str, "queue.Queue"] = {}
        self.version = {}
        self.recording = False
        self.thread: Optional[threading.Thread] = None

    def connect(self, timeout: float = 5.0):
        ws = WebSocket(self.host, self.port, timeout=timeout)
        hello = json.loads(ws.recv() or "{}")
        if hello.get("op") != 0:
            ws.close()
            raise ConnectionError("OBS ではありません（Hello が来ません）")
        d = hello.get("d", {})
        ident = {"rpcVersion": 1, "eventSubscriptions": SUB_GENERAL | SUB_OUTPUTS}
        if d.get("authentication"):
            if not self.password:
                ws.close()
                raise PermissionError("OBS の WebSocket にパスワードが設定されています。パスワードを入れてください")
            a = d["authentication"]
            ident["authentication"] = auth_string(self.password, a["salt"], a["challenge"])
        ws.send(json.dumps({"op": 1, "d": ident}))
        try:
            r = ws.recv()
        except (OSError, ConnectionError):
            r = None
        if r is None:
            ws.close()
            raise PermissionError("OBS に断られました（パスワードが違います）")
        msg = json.loads(r)
        if msg.get("op") != 2:
            ws.close()
            raise ConnectionError("OBS とのあいさつに失敗しました")
        ws.sock.settimeout(None)
        self.ws = ws
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.version = self.request("GetVersion")
        try:
            self.recording = bool(self.request("GetRecordStatus").get("outputActive"))
        except Exception:
            pass
        return self.version

    def _loop(self):
        while self.ws is not None and not self.ws.closed:
            try:
                text = self.ws.recv()
            except (OSError, ConnectionError):
                break
            if text is None:
                break
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            op, d = msg.get("op"), msg.get("d", {})
            if op == 7:
                q = self._pending.pop(d.get("requestId", ""), None)
                if q is not None:
                    q.put(d)
            elif op == 5:
                et, ed = d.get("eventType", ""), d.get("eventData", {}) or {}
                if et == "RecordStateChanged":
                    self.recording = bool(ed.get("outputActive"))
                    if ed.get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED" and ed.get("outputPath"):
                        try:
                            self.on_recorded(ed["outputPath"])
                        except Exception:
                            pass
                try:
                    self.on_event(et, ed)
                except Exception:
                    pass
        self.on_event("_closed", {})

    def request(self, rtype: str, data: Optional[dict] = None, timeout: float = 5.0) -> dict:
        if self.ws is None:
            raise ConnectionError("OBS につながっていません")
        rid = uuid.uuid4().hex
        q: "queue.Queue" = queue.Queue()
        self._pending[rid] = q
        body = {"requestType": rtype, "requestId": rid}
        if data:
            body["requestData"] = data
        self.ws.send(json.dumps({"op": 6, "d": body}))
        try:
            r = q.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise TimeoutError(f"OBS から返事がありません（{rtype}）")
        st = r.get("requestStatus", {})
        if not st.get("result"):
            raise RuntimeError(f"OBS: {rtype} に失敗しました（{st.get('comment') or st.get('code')}）")
        return r.get("responseData") or {}

    def record_directory(self) -> str:
        return self.request("GetRecordDirectory").get("recordDirectory", "")

    def start_record(self):
        self.request("StartRecord")

    def stop_record(self) -> str:
        return self.request("StopRecord").get("outputPath", "")

    def close(self):
        if self.ws is not None:
            self.ws.close()
            self.ws = None


# ─────────────────────────── 録画フォルダの見張り（つながらないとき）───────────────────────────
class FolderWatcher(threading.Thread):
    """フォルダに新しく増えた動画を、書き終わって（大きさが stable 秒止まって）から on_new(パス) に渡す。"""

    def __init__(self, folder: str, on_new: Callable[[str], None], stable: float = 4.0, interval: float = 2.0):
        super().__init__(daemon=True)
        self.folder, self.on_new, self.stable, self.interval = folder, on_new, stable, interval
        self.running = True
        self.known = set(self._list())
        self.sizes: Dict[str, Tuple[int, float]] = {}

    def _list(self):
        try:
            return [str(p) for p in Path(self.folder).iterdir() if p.suffix.lower() in VIDEO_EXT and p.is_file()]
        except OSError:
            return []

    def stop(self):
        self.running = False

    def run(self):
        while self.running:
            now = time.time()
            for p in self._list():
                if p in self.known:
                    continue
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                last = self.sizes.get(p)
                if last is None or last[0] != sz:
                    self.sizes[p] = (sz, now)
                elif sz > 0 and now - last[1] >= self.stable and not _locked(p):
                    self.known.add(p)
                    self.sizes.pop(p, None)
                    try:
                        self.on_new(p)
                    except Exception:
                        pass
            time.sleep(self.interval)


def _locked(path: str) -> bool:
    """まだ書いている途中か（Windows では書き込み中のファイルは名前を変えられない）。"""
    if os.name != "nt":
        return False
    try:
        os.rename(path, path)
        return False
    except OSError:
        return True


# ─────────────────────────── 変換 ───────────────────────────
def _ffmpeg() -> str:
    from .media import ffmpeg_exe
    ff = ffmpeg_exe()
    if not ff:
        raise RuntimeError("ffmpeg が見つかりません（PATH に置いてください）")
    return ff


def audio_streams(path: str) -> int:
    import av
    with av.open(path) as c:
        return len(c.streams.audio)


def prepare(path: str, out_dir: str, mode: str = "remux", progress=None, cancel=None) -> Tuple[str, List[str]]:
    """録画を編集しやすくする。戻り値 (映像ファイル, [2 本目以降の音声トラックの WAV])。
    mode: remux（MKV などを MP4 に入れ替え・再エンコードなし）/ edit（編集向けに再エンコード・NVIDIA なら NVENC）/ none"""
    os.makedirs(out_dir, exist_ok=True)
    ff = _ffmpeg()
    src = Path(path)
    stem = src.stem
    n_audio = audio_streams(path)
    main = path
    if mode == "remux" and src.suffix.lower() not in (".mp4", ".mov", ".m4v"):
        main = os.path.join(out_dir, stem + ".mp4")
        _run([ff, "-v", "error", "-y", "-i", path, "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
              "-movflags", "+faststart", main], cancel)
    elif mode == "edit":
        from . import gpu as G
        main = os.path.join(out_dir, stem + "_edit.mp4")
        if G.use_nvenc("h264") and G.use_gpu_decode():
            ok = G.ffmpeg_transcode(path, main, G.nvenc_args("h264", 19), audio=["-c:a", "aac", "-b:a", "256k"],
                                    progress=progress, cancel=cancel)
            if not ok:
                raise RuntimeError("NVENC での変換に失敗しました")
        else:
            _run([ff, "-v", "error", "-y", "-i", path, "-map", "0:v:0?", "-map", "0:a?", "-c:v", "libx264",
                  "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
                  "-movflags", "+faststart", main], cancel)
    extras = []
    for i in range(1, n_audio):
        wav = os.path.join(out_dir, f"{stem}_track{i + 1}.wav")
        _run([ff, "-v", "error", "-y", "-i", path, "-map", f"0:a:{i}", "-c:a", "pcm_s16le", "-ar", "48000", wav], cancel)
        extras.append(wav)
    return main, extras


def _run(cmd, cancel=None):
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
    while p.poll() is None:
        if cancel is not None and cancel():
            p.kill()
            raise InterruptedError("中止しました")
        time.sleep(0.1)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg でエラー: " + p.stderr.read().decode("utf-8", "replace")[-400:])


# ─────────────────────────── 無音カット → シーケンス ───────────────────────────
def speech_ranges(audio_path: str, total_frames: int, fps: float, threshold_db: float = -40.0,
                  min_silence: float = 0.8, padding: float = 0.15, min_keep: float = 0.4) -> List[Tuple[int, int]]:
    """声のある範囲（素材の頭からのフレーム [a, b)）。無音の区間を除いた残り。"""
    from . import analysis as AN
    from . import media as MD
    from .model import Clip, MediaItem
    MD.build_audio_cache(audio_path)
    m = MediaItem(id="x", path=audio_path, name="", kind="audio")
    c = Clip(id="x", kind="audio", start=0, duration=total_frames, src_in=0)
    quiet = AN.silent_ranges(m, c, fps, threshold_db, min_silence, padding)
    keep, pos = [], 0
    for a, b in quiet:
        if a > pos:
            keep.append((pos, a))
        pos = max(pos, b)
    if pos < total_frames:
        keep.append((pos, total_frames))
    mk = int(round(min_keep * fps))
    return [(a, b) for a, b in keep if b - a >= mk]


def build_sequence(project, name: str, video, extra_audio: list, ranges: List[Tuple[int, int]], fps: float,
                   width: int, height: int, start_tc: str = "01:00:00:00"):
    """話している範囲だけを詰めて並べたシーケンス（映像 V1・音声 A1・2 本目以降の音声 A2…・リンク付き）。"""
    from . import model as M
    seq = M.Sequence.create(name=name, fps=fps, width=width, height=height, start_tc=start_tc)
    while len(seq.audio_tracks) < 1 + len(extra_audio):
        seq.audio_tracks.append(M.Track("audio", f"A{len(seq.audio_tracks) + 1}"))
    pos = 0
    for a, b in ranges:
        d = b - a
        link = M.new_id()
        if video.has_video:
            seq.video_tracks[0].clips.append(M.Clip(M.new_id(), "video", pos, d, a, video.id, video.name, link=link))
        if video.has_audio:
            seq.audio_tracks[0].clips.append(M.Clip(M.new_id(), "audio", pos, d, a, video.id, video.name, link=link))
        for i, ex in enumerate(extra_audio):
            seq.audio_tracks[1 + i].clips.append(M.Clip(M.new_id(), "audio", pos, d, a, ex.id, ex.name, link=link))
        pos += d
    return seq
