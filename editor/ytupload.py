"""YouTube へのアップロード（YouTube Data API v3・Google のアカウントで許可）。

使う前に 1 回だけ:
  1. Google Cloud で「YouTube Data API v3」を有効にして、OAuth クライアント ID（種類: デスクトップ アプリ）を作る
  2. その JSON（client_secret_….json）を、アップロード画面の「設定ファイルを選ぶ」で読み込む（%USERPROFILE%\\.davintelop に保存）
  3. 「Google にログイン」→ ブラウザで許可（以後は自動。許可を外すには「ログアウト」）

アップロードは「再開できる方式」（8MB ずつ送る）。途中で切れても、続きから送り直せる（アップロード履歴の「再開」）。
注意: Google の審査を通していないクライアントでアップロードした動画は「非公開」に固定されます（YouTube の決まり）。

送るもの: タイトル・説明・タグ・カテゴリ・公開範囲（非公開 / 限定公開 / 公開）・予約公開の日時・子ども向けかどうか・
          言語・サムネイル・再生リスト
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .settings import BASE_DIR

CLIENT_PATH = BASE_DIR / "youtube_client_secret.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
HISTORY_PATH = BASE_DIR / "youtube_history.json"

# テストで偽のサーバーに向けられるように（TELOCUT_YT_BASE=http://127.0.0.1:xxxx）
_BASE = os.environ.get("TELOCUT_YT_BASE", "").rstrip("/")
AUTH_URL = (_BASE + "/o/oauth2/v2/auth") if _BASE else "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = (_BASE + "/token") if _BASE else "https://oauth2.googleapis.com/token"
API = (_BASE + "/youtube/v3") if _BASE else "https://www.googleapis.com/youtube/v3"
UPLOAD = (_BASE + "/upload/youtube/v3") if _BASE else "https://www.googleapis.com/upload/youtube/v3"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube"
CHUNK = 8 * 1024 * 1024

PRIVACY = [("private", "非公開"), ("unlisted", "限定公開"), ("public", "公開")]
CATEGORIES = [("22", "ブログ"), ("20", "ゲーム"), ("24", "エンターテイメント"), ("23", "コメディー"), ("27", "教育"),
              ("28", "科学と技術"), ("26", "ハウツーとスタイル"), ("10", "音楽"), ("1", "映画とアニメ"), ("17", "スポーツ"),
              ("19", "旅行とイベント"), ("15", "ペットと動物"), ("2", "自動車と乗り物"), ("25", "ニュースと政治")]


class YouTubeError(RuntimeError):
    pass


# ─────────────────────────── 設定ファイル・ログイン ───────────────────────────
def install_client(path: str) -> dict:
    """Google Cloud でダウンロードした OAuth クライアントの JSON を読み込んで保存する。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    c = d.get("installed") or d.get("web") or d
    if not c.get("client_id") or not c.get("client_secret"):
        raise YouTubeError("OAuth クライアントの JSON ではありません（client_id / client_secret がありません）")
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    CLIENT_PATH.write_text(json.dumps({"installed": c}, ensure_ascii=False), encoding="utf-8")
    return c


def client() -> Optional[dict]:
    try:
        d = json.loads(CLIENT_PATH.read_text(encoding="utf-8"))
        return d.get("installed") or d.get("web")
    except (OSError, ValueError):
        return None


def _token() -> Optional[dict]:
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_token(t: dict):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(t), encoding="utf-8")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass


def signed_in() -> bool:
    t = _token()
    return bool(t and t.get("refresh_token"))


def sign_out():
    t = _token()
    try:
        TOKEN_PATH.unlink()
    except OSError:
        pass
    if t and t.get("refresh_token") and not _BASE:
        try:                                               # Google 側の許可も外す
            urllib.request.urlopen(urllib.request.Request(
                "https://oauth2.googleapis.com/revoke?token=" + urllib.parse.quote(t["refresh_token"]), method="POST"), timeout=10)
        except Exception:
            pass


def _post_form(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        raise YouTubeError(f"Google のログインに失敗しました: {ex.read().decode(errors='replace')[:300]}")


def sign_in(open_browser: Optional[Callable[[str], None]] = None, timeout: float = 300) -> dict:
    """ブラウザで Google にログインして許可（この PC の 127.0.0.1 で結果を受け取る・PKCE 付き）。"""
    c = client()
    if not c:
        raise YouTubeError("先に OAuth クライアントの JSON を読み込んでください")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    got: Dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            if q.get("state") == state:
                got.update(q)
            ok_ = "code" in q and q.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "許可しました。この画面を閉じて TeloCut に戻ってください。" if ok_ else "許可されませんでした。"
            self.wfile.write(f"<meta charset='utf-8'><h2>{msg}</h2>".encode("utf-8"))

        def log_message(self, *_a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code", "scope": SCOPES,
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})
    th = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.2), daemon=True)
    th.start()
    try:
        if open_browser:
            open_browser(url)
        else:
            import webbrowser
            webbrowser.open(url)
        t0 = time.time()
        while "code" not in got and "error" not in got and time.time() - t0 < timeout:
            time.sleep(0.2)
    finally:
        srv.shutdown()
        srv.server_close()
    if "code" not in got:
        raise YouTubeError("ログインが完了しませんでした" + (f"（{got.get('error')}）" if got.get("error") else "（時間切れ）"))
    tok = _post_form(TOKEN_URL, {"code": got["code"], "client_id": c["client_id"], "client_secret": c["client_secret"],
                                 "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _save_token(tok)
    return tok


def access_token() -> str:
    t = _token()
    if not t or not t.get("refresh_token"):
        raise YouTubeError("Google にログインしていません")
    if t.get("access_token") and t.get("expires_at", 0) > time.time():
        return t["access_token"]
    c = client() or {}
    n = _post_form(TOKEN_URL, {"refresh_token": t["refresh_token"], "client_id": c.get("client_id", ""),
                               "client_secret": c.get("client_secret", ""), "grant_type": "refresh_token"})
    t["access_token"] = n["access_token"]
    t["expires_at"] = time.time() + int(n.get("expires_in", 3600)) - 60
    _save_token(t)
    return t["access_token"]


def _api(method: str, url: str, body=None, headers: Optional[dict] = None, raw: bool = False):
    h = {"Authorization": "Bearer " + access_token(), **(headers or {})}
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h.setdefault("Content-Type", "application/json; charset=UTF-8")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            b = r.read()
            return b if raw else (json.loads(b.decode()) if b else {})
    except urllib.error.HTTPError as ex:
        raise YouTubeError(_explain(ex.code, ex.read().decode(errors="replace")))


def _explain(code: int, text: str) -> str:
    low = text.lower()
    if "quotaexceeded" in low:
        return "今日の YouTube API の上限に達しました（明日また試してください）"
    if "uploadlimitexceeded" in low:
        return "このチャンネルの 1 日のアップロード数の上限に達しました"
    if code == 401:
        return "ログインの期限が切れました。もう一度ログインしてください"
    if code == 403 and "forbidden" in low and "thumbnail" in low:
        return "サムネイルを設定する権限がありません（チャンネルの電話番号認証が必要です）"
    return f"YouTube がエラーを返しました（{code}）: {text[:300]}"


# ─────────────────────────── アップロード ───────────────────────────
def build_body(meta: dict) -> dict:
    """タイトルなどを YouTube の形に（タイトル 100 文字・説明 5000 バイト・タグ 500 文字までに切る）。"""
    title = (meta.get("title") or "無題").replace("<", "＜").replace(">", "＞")[:100]
    desc = (meta.get("description") or "").replace("<", "＜").replace(">", "＞")
    while len(desc.encode("utf-8")) > 5000:
        desc = desc[:-50]
    tags, total = [], 0
    for t in meta.get("tags") or []:
        t = str(t).strip().replace("<", "").replace(">", "")
        if t and total + len(t) + 2 <= 500:
            tags.append(t)
            total += len(t) + 2
    status = {"privacyStatus": meta.get("privacy") or "private", "selfDeclaredMadeForKids": bool(meta.get("for_kids")),
              "embeddable": True}
    if meta.get("publish_at"):
        status["privacyStatus"] = "private"              # 予約公開は「非公開」で上げて、時間になったら公開される
        status["publishAt"] = meta["publish_at"]
    snippet = {"title": title, "description": desc, "tags": tags, "categoryId": str(meta.get("category") or "22")}
    if meta.get("language"):
        snippet["defaultLanguage"] = snippet["defaultAudioLanguage"] = meta["language"]
    return {"snippet": snippet, "status": status}


def start_session(path: str, meta: dict) -> str:
    """再開できるアップロードの受付（戻り値: 送り先の URL。これを覚えておけば続きから送れる）。"""
    size = os.path.getsize(path)
    h = {"Authorization": "Bearer " + access_token(), "Content-Type": "application/json; charset=UTF-8",
         "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": _mime(path)}
    req = urllib.request.Request(UPLOAD + "/videos?uploadType=resumable&part=snippet,status",
                                 data=json.dumps(build_body(meta), ensure_ascii=False).encode("utf-8"), method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            loc = r.headers.get("Location")
    except urllib.error.HTTPError as ex:
        raise YouTubeError(_explain(ex.code, ex.read().decode(errors="replace")))
    if not loc:
        raise YouTubeError("アップロードを始められませんでした（送り先が返ってきません）")
    return loc


def _mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".mp4": "video/mp4", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".webm": "video/webm",
            ".avi": "video/x-msvideo", ".mxf": "application/mxf", ".m4v": "video/mp4"}.get(ext, "application/octet-stream")


def _uploaded(session: str, size: int) -> int:
    """送り先に何バイト届いているか（途中で切れたときの続きの位置）。"""
    req = urllib.request.Request(session, data=b"", method="PUT",
                                 headers={"Authorization": "Bearer " + access_token(), "Content-Range": f"bytes */{size}",
                                          "Content-Length": "0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return size if r.status in (200, 201) else 0
    except urllib.error.HTTPError as ex:
        if ex.code == 308:
            rng = ex.headers.get("Range", "")
            return int(rng.split("-")[-1]) + 1 if rng else 0
        raise YouTubeError(_explain(ex.code, ex.read().decode(errors="replace")))


def send(path: str, session: str, progress: Optional[Callable[[int, int], None]] = None,
         cancel: Optional[threading.Event] = None, resume: bool = False) -> dict:
    """ファイルを 8MB ずつ送る。終わったら動画の情報（id など）を返す。切れたら 3 回まで続きから送り直す。"""
    size = os.path.getsize(path)
    pos = _uploaded(session, size) if resume else 0
    tries = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise YouTubeError("中止しました（履歴の「再開」で続きから送れます）")
            f.seek(pos)
            chunk = f.read(CHUNK)
            end = pos + len(chunk) - 1
            req = urllib.request.Request(session, data=chunk, method="PUT", headers={
                "Authorization": "Bearer " + access_token(), "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {pos}-{end}/{size}" if chunk else f"bytes */{size}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    if progress:
                        progress(size, size)
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as ex:
                if ex.code == 308:                          # まだ途中（届いた所まで進める）
                    rng = ex.headers.get("Range", "")
                    pos = int(rng.split("-")[-1]) + 1 if rng else 0
                    tries = 0
                    if progress:
                        progress(pos, size)
                    continue
                if ex.code in (500, 502, 503, 504) and tries < 3:
                    tries += 1
                    time.sleep(2 * tries)
                    pos = _uploaded(session, size)
                    continue
                raise YouTubeError(_explain(ex.code, ex.read().decode(errors="replace")))
            except (urllib.error.URLError, ConnectionError, TimeoutError) as ex:
                if tries < 3:
                    tries += 1
                    time.sleep(2 * tries)
                    pos = _uploaded(session, size)
                    continue
                raise YouTubeError(f"通信が切れました（{ex}）。履歴の「再開」で続きから送れます")


def set_thumbnail(video_id: str, image: str) -> dict:
    data = Path(image).read_bytes()
    if len(data) > 2 * 1024 * 1024:
        raise YouTubeError("サムネイルは 2MB までです（JPEG にすると小さくなります）")
    ct = "image/png" if image.lower().endswith(".png") else "image/jpeg"
    return _api("POST", UPLOAD + f"/thumbnails/set?videoId={urllib.parse.quote(video_id)}", data, {"Content-Type": ct})


def playlists() -> List[dict]:
    d = _api("GET", API + "/playlists?part=snippet&mine=true&maxResults=50")
    return [{"id": x["id"], "title": x["snippet"]["title"]} for x in d.get("items", [])]


def add_to_playlist(video_id: str, playlist_id: str) -> dict:
    return _api("POST", API + "/playlistItems?part=snippet",
                {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}})


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


# ─────────────────────────── 履歴 ───────────────────────────
def history() -> List[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_history(rows: List[dict]):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(rows[:300], ensure_ascii=False, indent=1), encoding="utf-8")


def _put_history(entry: dict):
    rows = [r for r in history() if r.get("key") != entry["key"]]
    _save_history([entry] + rows)


def upload(path: str, meta: dict, progress=None, cancel=None, resume_key: str = "") -> dict:
    """アップロードの全部（受付 → 送る → サムネイル → 再生リスト）。履歴に残し、切れても resume_key で続きから。"""
    key = resume_key or f"{os.path.abspath(path)}|{time.time():.0f}|{secrets.token_hex(4)}"   # 同じ秒に 2 回でも別の履歴
    old = next((r for r in history() if r.get("key") == key), None) if resume_key else None
    entry = old or {"key": key, "path": os.path.abspath(path), "title": meta.get("title", ""), "meta": meta,
                    "time": datetime.datetime.now().isoformat(timespec="seconds"), "status": "uploading"}
    if not old or not old.get("session"):
        entry["session"] = start_session(path, meta)
    entry["status"] = "uploading"
    _put_history(entry)
    try:
        res = send(path, entry["session"], progress, cancel, resume=bool(old))
    except YouTubeError as ex:
        entry["status"], entry["error"] = "failed", str(ex)
        _put_history(entry)
        raise
    vid = res.get("id", "")
    entry.update({"status": "done", "video_id": vid, "url": video_url(vid) if vid else "", "error": ""})
    notes = []
    m = entry.get("meta") or meta
    if vid and m.get("thumbnail"):
        try:
            set_thumbnail(vid, m["thumbnail"])
        except YouTubeError as ex:
            notes.append(f"サムネイル: {ex}")
    if vid and m.get("playlist"):
        try:
            add_to_playlist(vid, m["playlist"])
        except YouTubeError as ex:
            notes.append(f"再生リスト: {ex}")
    entry["notes"] = notes
    _put_history(entry)
    return entry


def fill_template(text: str, **vals) -> str:
    """説明のひな形の {title} {date} {file} などを埋める（無い言葉はそのまま）。"""
    now = datetime.datetime.now()
    vals = {"date": f"{now:%Y-%m-%d}", "year": f"{now:%Y}", **vals}
    out = text
    for k, v in vals.items():
        out = out.replace("{" + k + "}", str(v))
    return out
