"""レンダーファーム: アセットサーバー経由で区間レンダリングを複数 PC に分散する。

- FarmClient: 書き出す側。ジョブを登録し、自分でも区間をレンダリングしつつ、他の PC の結果を受け取る
- run_node: レンダーノード（レンダリング用 PC）。TeloCut.exe --render-node --server http://サーバー:8770
  素材は各ノードから同じパス（NAS）で見えるか、パス置換ルール（環境設定）で解決できる必要がある
"""
from __future__ import annotations

import json
import os
import platform
import socket
import tempfile
import threading
import time
import urllib.request
from typing import Callable, List, Optional, Tuple


def _req(url: str, data=None, method=None, timeout=30, raw=False):
    headers = {"X-TeloCut": "1"}             # アセットサーバーが「TeloCut からの要求」と分かるように（CSRF 対策）
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                               headers=headers)
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read()
    return body if raw else json.loads(body.decode("utf-8") or "null")


def server_ok(url: str) -> bool:
    try:
        return bool(_req(url.rstrip("/") + "/api/info", timeout=3))
    except Exception:
        return False


def online_nodes(url: str) -> list:
    try:
        return [n for n in _req(url.rstrip("/") + "/api/render/nodes", timeout=5) if n.get("online")]
    except Exception:
        return []


class FarmClient:
    def __init__(self, server: str, name: Optional[str] = None):
        self.server = server.rstrip("/")
        self.name = (name or socket.gethostname()) + "（書き出し元）"

    def render(self, project, todo: List[Tuple[int, int, str]], codec: str, crf: int,
               path_fn: Callable[[str], object], tick, cancel=None):
        from . import smartrender as SR
        job = _req(self.server + "/api/render/jobs", {
            "project": project.to_json(), "codec": codec, "crf": crf, "client": self.name,
            "tasks": [{"a": a, "b": b, "key": k} for a, b, k in todo]})["id"]
        keys = {k for _, _, k in todo}
        have = set()
        while have != keys:
            if cancel is not None and cancel.is_set():
                raise InterruptedError
            t = _req(self.server + "/api/render/claim", {"node": self.name, "job": job})
            if t:
                out = str(path_fn(t["key"]))
                ok = SR.render_segment(project, project.sequence, t["a"], t["b"], out, codec, crf)
                if ok:
                    with open(out, "rb") as f:
                        _req(f"{self.server}/api/render/segment/{t['key']}?node={urllib.request.quote(self.name)}",
                             f.read(), method="PUT", timeout=300)
                    have.add(t["key"])
                    tick("レンダリング中（自分）")
                else:
                    _req(self.server + "/api/render/fail", {"job": job, "key": t["key"], "error": "failed"})
            st = _req(f"{self.server}/api/render/jobs/{job}")
            for task in st["tasks"]:
                k = task["key"]
                if task["status"] == "done" and k not in have:
                    p = path_fn(k)
                    if not os.path.exists(str(p)):
                        data = _req(f"{self.server}/api/render/segment/{k}", raw=True, timeout=300)
                        tmp = str(p) + ".dl"
                        with open(tmp, "wb") as f:
                            f.write(data)
                        os.replace(tmp, str(p))
                        tick(f"レンダリング中（{task['node']}）")
                    have.add(k)
            if not t:
                time.sleep(0.5)


def _resolve_media(project) -> list:
    """素材のパスをこの PC で見える場所に直す（パス置換ルール）。Qt を使わない版。"""
    from .settings import SETTINGS
    offline = []
    for m in project.media:
        if os.path.exists(m.path):
            continue
        for c in (SETTINGS.map_path(m.path), m.original_path, SETTINGS.map_path(m.original_path or "")):
            if c and os.path.exists(c):
                m.path = c
                break
        else:
            offline.append(m)
    return offline


def run_node(server: str, name: Optional[str] = None, once: bool = False, stop: Optional[threading.Event] = None):
    """レンダーノードとして待機し、配られた区間をレンダリングし続ける。"""
    from .model import Project
    from . import smartrender as SR
    server = server.rstrip("/")
    name = name or socket.gethostname()
    info = {"os": platform.platform(), "cpu": os.cpu_count()}
    print(f"レンダーノード「{name}」: {server} に接続しました。Ctrl+C で終了。")
    last_hb = 0
    cache = {}
    while stop is None or not stop.is_set():
        try:
            if time.time() - last_hb > 10:
                _req(server + "/api/render/heartbeat", {"node": name, "info": info})
                last_hb = time.time()
            t = _req(server + "/api/render/claim", {"node": name})
        except Exception as ex:
            print("サーバーに接続できません:", ex)
            time.sleep(5)
            continue
        if not t:
            if once:
                return
            time.sleep(1.0)
            continue
        try:
            proj = cache.get(t["job_id"])
            if proj is None:
                proj = Project.from_json(t["project"])
                offline = _resolve_media(proj)
                if offline:
                    print(f"⚠ 見つからない素材 {len(offline)} 個（パス置換ルールを確認）")
                cache = {t["job_id"]: proj}
            fd, tmp = tempfile.mkstemp(suffix=SR.CODECS.get(t["codec"], ".mp4"))
            os.close(fd)
            ok = SR.render_segment(proj, proj.sequence, t["a"], t["b"], tmp, t["codec"], int(t["crf"]))
            if not ok:
                raise RuntimeError("render failed")
            with open(tmp, "rb") as f:
                _req(f"{server}/api/render/segment/{t['key']}?node={urllib.request.quote(name)}", f.read(),
                     method="PUT", timeout=300)
            os.remove(tmp)
            print(f"区間 {t['a']}-{t['b']} 完了")
        except Exception as ex:
            print("失敗:", ex)
            try:
                _req(server + "/api/render/fail", {"job": t["job_id"], "key": t["key"], "error": str(ex)})
            except Exception:
                pass
            time.sleep(2)
