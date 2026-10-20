"""DavinTelop ⇔ TeloCut 連携クライアント（標準ライブラリのみ。Qt 不要）。

TeloCut が起動していると 127.0.0.1 でブリッジ（JSON 1 行ずつ）を開いている。
- テロップの取得 / 反映（TeloCut のシーケンスのテロップを丸ごと置き換え。TeloCut 側で 1 回の取り消し）
- 再生位置の取得 / 移動、再生位置の変化を受け取る（PlayheadFollower）
"""
from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

DEFAULT_PORT = 47310


def _port_file() -> Path:
    base = Path(os.environ.get("TELOCUT_HOME") or (Path.home() / ".davintelop"))
    return base / "telocut_bridge.json"


def find_port() -> int:
    try:
        return int(json.loads(_port_file().read_text(encoding="utf-8")).get("port", 0)) or DEFAULT_PORT
    except (OSError, ValueError):
        return DEFAULT_PORT


def call(cmd: str, timeout: float = 10.0, port: int = 0, **kw):
    port = port or find_port()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        raise ConnectionError("TeloCut が起動していません（TeloCut を起動してから実行してください）")
    with s:
        s.sendall((json.dumps({"id": 1, "cmd": cmd, **kw}, ensure_ascii=False) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                raise ConnectionError("TeloCut との接続が切れました")
            d = json.loads(line.decode("utf-8"))
            if "event" in d:
                continue
            if not d.get("ok"):
                raise RuntimeError(d.get("error"))
            return d.get("result")


def is_running() -> bool:
    try:
        call("ping", timeout=1.5)
        return True
    except Exception:
        return False


def get_telops() -> dict:
    """{"fps", "start_tc", "design", "telops": [{start_time, end_time, text, style, clip_id}], ...}"""
    return call("get_telops")


def put_telops(entries, design: Optional[dict] = None):
    """TelopEntry（または dict）のリストで TeloCut のテロップを置き換える。"""
    tl = []
    for e in entries:
        d = e.to_dict() if hasattr(e, "to_dict") else dict(e)
        if not d.get("enabled", True):
            continue
        tl.append({"start": d["start_time"], "end": d["end_time"], "text": d.get("text", ""),
                   "style": d.get("style") or {}})
    return call("put_telops", telops=tl, design=design, timeout=60)


def seek(tc: str):
    return call("seek", tc=tc)


def playhead() -> str:
    return call("get_playhead")


# ─────────────────────────────────────────────
#  リアルタイム連携（双方向）
# ─────────────────────────────────────────────
# 両方のソフトが同じ形（items）でテロップを表し、同じ関数でハッシュを取る。
# 受け取った状態を反映したらそのハッシュを覚え、自分の状態のハッシュが変わったときだけ送る → 往復しない。

CANVAS_KEYS = ("canvas_width", "canvas_height", "frame_rate")


def item(uid: str, start: str, end: str, text: str, style=None, speaker: str = "", enabled: bool = True,
         track: int = 0) -> dict:
    return {"uid": uid or "", "start": start, "end": end, "text": text or "", "style": dict(style or {}),
            "speaker": speaker or "", "enabled": bool(enabled), "track": int(track or 0)}


def entry_item(e) -> dict:
    """DavinTelop の TelopEntry → items の 1 件。"""
    return item(getattr(e, "uid", ""), e.start_time, e.end_time, e.text, e.style, e.speaker, e.enabled, e.track)


def design_part(design: Optional[dict]) -> dict:
    return {k: v for k, v in (design or {}).items() if k not in CANVAS_KEYS}


def state_hash(items, design: Optional[dict]) -> str:
    import hashlib
    norm = [[i["uid"], i["start"], i["end"], i["text"], i["speaker"], i["enabled"], i["track"],
             sorted((i.get("style") or {}).items())] for i in items]
    d = sorted(design_part(design).items())
    return hashlib.md5(json.dumps([norm, d], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def match_items(remote: list, local: list, key=lambda e: (e.start_time, e.end_time, e.text)):
    """remote の各 item に対応する local の要素（無ければ None）を返す。uid → 時間と本文 の順で探す。"""
    by_uid = {}
    for e in local:
        u = getattr(e, "uid", "")
        if u and u not in by_uid:
            by_uid[u] = e
    used = set()
    out = []
    loose = {}
    for e in local:
        loose.setdefault(key(e), []).append(e)
    for it in remote:
        e = by_uid.get(it["uid"])
        if e is not None and id(e) in used:
            e = None
        if e is None:
            for cand in loose.get((it["start"], it["end"], it["text"]), []):
                if id(cand) not in used:
                    e = cand
                    break
        if e is not None:
            used.add(id(e))
        out.append(e)
    return out


def sync_get() -> dict:
    return call("sync_get")


def sync_put(items: list, design: Optional[dict] = None) -> dict:
    return call("sync_put", items=items, design=design_part(design) if design is not None else None, timeout=60)


def select(uids: list, seek: bool = True):
    return call("select", uids=list(uids), seek=bool(seek))


class Session:
    """TeloCut からのイベント（sync / selection / playhead）を受け取り続ける。切れたら自動で再接続。
    on_event(dict) と on_state(bool: つながった / 切れた) は受信スレッドから呼ばれる。"""

    def __init__(self, on_event: Callable[[dict], None], on_state: Optional[Callable[[bool], None]] = None):
        self.on_event, self.on_state = on_event, on_state
        self.connected = False
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        threading.Thread(target=self._run, daemon=True).start()

    def _set(self, v: bool):
        if v != self.connected:
            self.connected = v
            if self.on_state:
                self.on_state(v)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sock = socket.create_connection(("127.0.0.1", find_port()), timeout=3)
                self._sock.settimeout(None)
                self._sock.sendall(b'{"id": 0, "cmd": "ping"}\n')
                f = self._sock.makefile("rb")
                self._set(True)
                for line in f:
                    if self._stop.is_set():
                        break
                    try:
                        d = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if "event" in d:
                        self.on_event(d)
            except OSError:
                pass
            self._set(False)
            self._stop.wait(2.0)

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass


class PlayheadFollower:
    """TeloCut の再生位置の変化を受け取り続ける（別スレッド）。on_tc はそのスレッドから呼ばれる。"""

    def __init__(self, on_tc: Callable[[str], None]):
        self.on_tc = on_tc
        self._stop = threading.Event()
        self._sock: Optional[socket.socket] = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self._sock = socket.create_connection(("127.0.0.1", find_port()), timeout=3)
                self._sock.settimeout(None)
                # 接続したことをブリッジに知らせる（以後イベントが届く）
                self._sock.sendall(b'{"id": 0, "cmd": "ping"}\n')
                f = self._sock.makefile("rb")
                for line in f:
                    if self._stop.is_set():
                        break
                    try:
                        d = json.loads(line.decode("utf-8"))
                    except ValueError:
                        continue
                    if d.get("event") == "playhead":
                        self.on_tc(d.get("tc", ""))
            except OSError:
                pass
            self._stop.wait(3.0)

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
