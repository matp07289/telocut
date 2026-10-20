"""【開発者用・配布しない】TeloCut のライセンスキーを作る。

秘密鍵はリポジトリの外（既定: ~/.telocut_dev/license_signing_key.txt）に置く。絶対に Git に入れない・人に渡さない。
なくすと、今まで出したキーは使えるが、新しいキーが作れなくなる（バックアップを取っておく）。

  最初に 1 回（鍵を作る。表示された公開鍵を editor/license.py の PUBLIC_KEY に）:
    python tools/make_license.py --init
  キーを作る:
    python tools/make_license.py --name "山田 太郎" --email taro@example.com --edition pro --days 365
    python tools/make_license.py --name "制作会社A" --email a@example.com --edition studio --seats 5
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from editor import ed25519  # noqa: E402

DEFAULT_KEY = Path(os.environ.get("TELOCUT_LICENSE_KEY") or (Path.home() / ".telocut_dev" / "license_signing_key.txt"))


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true", help="署名用の鍵を作る（すでにあれば何もしない）")
    ap.add_argument("--key", default=str(DEFAULT_KEY))
    ap.add_argument("--name", default="")
    ap.add_argument("--email", default="")
    ap.add_argument("--edition", default="pro", choices=["pro", "studio"])
    ap.add_argument("--days", type=int, default=0, help="有効日数（0 = 期限なし・買い切り）")
    ap.add_argument("--seats", type=int, default=1)
    ap.add_argument("--updates-days", type=int, default=365, help="無料アップデートの期間（買い切り用）")
    a = ap.parse_args()
    kp = Path(a.key)
    if a.init:
        if kp.exists():
            sk = bytes.fromhex(kp.read_text().strip())
            print("すでに鍵があります:", kp)
        else:
            kp.parent.mkdir(parents=True, exist_ok=True)
            sk = secrets.token_bytes(32)
            kp.write_text(sk.hex())
            try:
                os.chmod(kp, 0o600)
            except OSError:
                pass
            print("秘密鍵を作りました（バックアップしてください・Git に入れない）:", kp)
        print("公開鍵（editor/license.py の PUBLIC_KEY）:", ed25519.public_key(sk).hex())
        return
    if not kp.exists():
        sys.exit(f"秘密鍵がありません: {kp}（--init で作るか --key で指定）")
    if not a.name:
        sys.exit("--name を指定してください")
    sk = bytes.fromhex(kp.read_text().strip())
    today = datetime.date.today()
    payload = {"v": 1, "id": secrets.token_hex(6), "name": a.name, "email": a.email, "edition": a.edition,
               "seats": a.seats, "issued": today.isoformat(),
               "expires": (today + datetime.timedelta(days=a.days)).isoformat() if a.days else "",
               "updates_until": (today + datetime.timedelta(days=a.updates_days)).isoformat() if not a.days else ""}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key = "TELOCUT-" + b64(body) + "." + b64(ed25519.sign(sk, body))
    print(key)


if __name__ == "__main__":
    main()
