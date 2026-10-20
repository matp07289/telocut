"""アプリ間連携のブリッジ（TeloCut 側のサーバー）。

127.0.0.1 の TCP で 1 行 1 JSON（JSON Lines）のやり取りをする。
- DavinTelop（テロップ作成ツール）: テロップの受け渡し・再生ヘッドの同期
- MCP サーバー（Claude Code から TeloCut を操作）: 状態の取得・編集命令の実行

要求: {"id": 1, "cmd": "get_state" | "get_telops" | "put_telops" | "apply_ops" | "seek" | "get_playhead"
        | "import_files" | "ping", ...}
応答: {"id": 1, "ok": true, "result": ...}
通知（TeloCut → 接続先）: {"event": "playhead", "tc": "..."} / {"event": "telops_changed"}

安全のため、要求には telocut_bridge.json に書いた合言葉（"token"）が必要（同じ PC のユーザーだけが読める）。
ブラウザなどから HTTP で送られてきた接続はすぐ切る（Web ページから勝手に操作されないように）。
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal, QTimer
from PyQt6.QtNetwork import QTcpServer, QHostAddress, QTcpSocket

from core.timecode import frames_to_timecode, normalize_timecode, timecode_to_frames
from .settings import BASE_DIR

PORT_FILE = BASE_DIR / "telocut_bridge.json"


class Bridge(QObject):
    status = pyqtSignal(str)

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.server = QTcpServer(self)
        self.server.newConnection.connect(self._on_conn)
        self.clients = []
        self._buf = {}
        self._authed = set()
        self.token = secrets.token_hex(16)
        self._last_tc = ""
        self._last_hash = ""
        self._last_sel: tuple = ()
        # 接続中のソフト（DavinTelop など）があるとき、テロップと選択の変化を 0.25 秒ごとに確認して知らせる
        from PyQt6.QtCore import QTimer
        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(250)
        self._sync_timer.timeout.connect(self._push_changes)
        self._sync_timer.start()

    def start(self, port: int = 0) -> int:
        if not self.server.listen(QHostAddress.SpecialAddress.LocalHost, port):
            self.server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
        p = self.server.serverPort()
        try:
            BASE_DIR.mkdir(parents=True, exist_ok=True)
            PORT_FILE.write_text(json.dumps({"port": p, "pid": os.getpid(), "token": self.token}), encoding="utf-8")
        except OSError:
            pass
        self.mw.record_player.position_changed.connect(self._push_playhead)
        return p

    @property
    def port(self) -> int:
        return self.server.serverPort()

    def stop(self):
        try:
            if PORT_FILE.exists() and json.loads(PORT_FILE.read_text()).get("pid") == os.getpid():
                PORT_FILE.unlink()
        except (OSError, ValueError):
            pass
        self.server.close()

    def _on_conn(self):
        while self.server.hasPendingConnections():
            s = self.server.nextPendingConnection()
            self.clients.append(s)
            self._buf[id(s)] = b""
            s.readyRead.connect(lambda s=s: self._read(s))
            s.disconnected.connect(lambda s=s: self._gone(s))
            self.status.emit(f"連携: 接続 {len(self.clients)}")

    def _gone(self, s):
        if s in self.clients:
            self.clients.remove(s)
        self._buf.pop(id(s), None)
        self._authed.discard(id(s))
        self.status.emit(f"連携: 接続 {len(self.clients)}")

    HTTP_START = (b"GET ", b"POST ", b"PUT ", b"DELETE ", b"OPTIONS ", b"HEAD ", b"PATCH ", b"CONNECT ")

    def _read(self, s):
        if id(s) not in self._buf:
            return
        self._buf[id(s)] += bytes(s.readAll())
        if id(s) not in self._authed and (self._buf[id(s)].lstrip().startswith(self.HTTP_START)
                                          or len(self._buf[id(s)]) > 1 << 20):
            self._buf[id(s)] = b""
            s.abort()                               # ブラウザからの HTTP（Web ページの攻撃）や巨大なデータは切る
            return
        while b"\n" in self._buf.get(id(s), b""):
            line, rest = self._buf[id(s)].split(b"\n", 1)
            self._buf[id(s)] = rest
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
                if id(s) not in self._authed:
                    if not isinstance(req, dict) or not secrets.compare_digest(str(req.get("token", "")), self.token):
                        raise PermissionError("合言葉が違います（TeloCut を起動し直すか、連携するソフトを更新してください）")
                    self._authed.add(id(s))
                res = self.handle(req)
                resp = {"id": req.get("id"), "ok": True, "result": res}
            except Exception as ex:
                resp = {"id": None, "ok": False, "error": str(ex)}
                try:
                    resp["id"] = json.loads(line.decode("utf-8")).get("id")
                except Exception:
                    pass
            s.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            s.flush()

    def broadcast(self, event: dict):
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        for s in list(self.clients):
            if id(s) in self._authed and s.state() == QTcpSocket.SocketState.ConnectedState:
                s.write(data)

    def _push_changes(self):
        if not self.clients:
            return
        from . import ops
        try:
            p = ops.sync_payload(self.mw)
        except Exception:
            return
        if p["hash"] != self._last_hash:
            self._last_hash = p["hash"]
            self.broadcast({"event": "sync", **p})
        sel = tuple(sorted(c.id for _, c in self.mw.canvas.selected_clips() if c.kind == "text"))
        if sel != self._last_sel:
            self._last_sel = sel
            if sel:
                self.broadcast({"event": "selection", "uids": list(sel)})

    def _push_playhead(self, f):
        if not self.clients:
            return
        seq = self.mw.project.sequence
        tc = frames_to_timecode(f + self.mw.tc_offset(seq), seq.fps)
        if tc != self._last_tc:
            self._last_tc = tc
            self.broadcast({"event": "playhead", "tc": tc})

    # ── 命令 ─────────────────────────────────
    def handle(self, req: dict):
        from . import ops
        cmd = req.get("cmd")
        mw = self.mw
        seq = mw.project.sequence
        if cmd == "ping":
            return {"app": "TeloCut", "project": mw.project.name, "sequence": seq.name}
        if cmd == "get_state":
            return ops.describe_state(mw)
        if cmd == "get_telops":
            return ops.telops_payload(mw)
        if cmd == "put_telops":
            log = ops.apply_ops(mw, [{"op": "replace_telops", "telops": req.get("telops", [])}],
                                 "DavinTelop からテロップを反映")
            if req.get("design"):
                mw.begin_edit()
                mw.project.design = req["design"]
                mw.end_edit("テロップデザインを反映")
            return log
        if cmd == "apply_ops":
            return ops.apply_ops(mw, req.get("ops", []), req.get("label") or "外部からの編集")
        if cmd == "sync_get":
            return ops.sync_payload(mw)
        if cmd == "sync_put":
            p = ops.sync_apply(mw, req.get("items", []), req.get("design"))
            self._last_hash = p["hash"]          # 他の接続先にも配る（送り主は同じハッシュなので何もしない）
            self.broadcast({"event": "sync", **p})
            return p
        if cmd == "select":
            n = ops.select_uids(mw, req.get("uids", []), bool(req.get("seek", True)))
            self._last_sel = tuple(sorted(req.get("uids", [])))
            return n
        if cmd == "get_playhead":
            return frames_to_timecode(mw.record_player.position + mw.tc_offset(seq), seq.fps)
        if cmd == "seek":
            f = timecode_to_frames(normalize_timecode(str(req["tc"]), seq.fps), seq.fps) - mw.tc_offset(seq)
            mw.seek_record(max(0, f))
            return True
        if cmd == "import_files":
            mw.import_files([p for p in req.get("paths", []) if os.path.exists(p)])
            return True
        if cmd == "render_frame":
            from . import media as MD, render as R
            out = req.get("path") or str(BASE_DIR / "telocut_frame.png")
            pool = MD.DecoderPool()
            try:
                R.render_frame(seq, mw.record_player.position, mw.project, pool, float(req.get("scale", 0.5))).save(out)
            finally:
                pool.close()
            return out
        raise ValueError(f"未対応のコマンド: {cmd}")


def find_running_port() -> int:
    """起動中の TeloCut のブリッジのポート（無ければ 0）。"""
    try:
        return int(json.loads(PORT_FILE.read_text(encoding="utf-8")).get("port", 0))
    except (OSError, ValueError):
        return 0


def call(cmd: str, port: int = 0, timeout: float = 10.0, **kw):
    """Qt を使わずにブリッジを呼ぶ（MCP サーバー・DavinTelop から使う）。"""
    import socket
    port = port or find_running_port()
    if not port:
        raise ConnectionError("TeloCut が起動していません")
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        try:
            token = json.loads(PORT_FILE.read_text(encoding="utf-8")).get("token", "")
        except (OSError, ValueError):
            token = ""
        s.sendall((json.dumps({"id": 1, "cmd": cmd, "token": token, **kw}, ensure_ascii=False) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                raise ConnectionError("接続が切れました")
            d = json.loads(line.decode("utf-8"))
            if "event" in d:
                continue
            if not d.get("ok"):
                raise RuntimeError(d.get("error"))
            return d.get("result")
