"""エディション（無料版 / Pro / Studio）とライセンスキー。

- 無料版: 編集・テロップ・テンプレートを使う・ゆっくり・自動切り抜き（書き出しに小さな透かし）・H.264 / H.265 の書き出し（1080p まで）
- Pro: 4K 以上・DNx / ProRes / QuickTime / 透明・トランスコード・モーション（トラッキング / 手ぶれ補正）・Blender / MMD・
       Dynamic Link・AI（Claude / ComfyUI）・自動切り抜きの透かしなし・テンプレートの作成と保存
- Studio: Pro ＋ チーム（ファーム = 複数 PC で書き出し / 変換・制作管理）

ライセンスキーは開発者の秘密鍵で署名したもの（tools/make_license.py）。アプリは公開鍵で確かめるだけなので、キーを作ることはできない。
ソースから動かしているとき（開発中）は Studio 扱い。TELOCUT_EDITION=free/pro/studio で切り替えて試せる（ソースから動かすときだけ）。

EXE は 3 種類に分けてビルドする（build/build_telocut.bat personal / free / paid。ビルドの種類は editor/build_kind.py に書き込まれる）:
- personal（自分用）: キーなしで Studio。配らないこと
- free（無料版）: 無料版に固定。キー入力・お試しはなく、有料版の案内だけ
- paid（有料版）: キーで Pro / Studio。キーが無ければ無料版の機能＋14 日間のお試し
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import sys
from dataclasses import dataclass

from typing import Optional

from .settings import BASE_DIR, SETTINGS

try:
    from .build_kind import KIND as BUILD_KIND        # ビルドのときに作られる（Git には入れない）
except ImportError:
    BUILD_KIND = ""
if BUILD_KIND not in ("personal", "free", "paid"):
    BUILD_KIND = "paid"
KIND_NAMES = {"personal": "自分用", "free": "無料版", "paid": "有料版"}

PUBLIC_KEY = "c16bc99038e91d615710710f303b33dfc7bf4c37fe6ff8448630dec789b2943f"
LICENSE_PATH = BASE_DIR / "license.key"
PURCHASE_URL = os.environ.get("TELOCUT_PURCHASE_URL", "")      # 公開するときに販売ページの URL を入れる
TRIAL_DAYS = 14
RANK = {"free": 0, "pro": 1, "studio": 2}
NAMES = {"free": "無料版", "pro": "Pro", "studio": "Studio"}

# 機能 → 必要なエディション
FEATURES = {
    "export_4k": ("pro", "4K 以上の書き出し"),
    "export_pro_codecs": ("pro", "DNx / ProRes / QuickTime などの書き出し"),
    "export_alpha": ("pro", "背景が透明な書き出し"),
    "transcode": ("pro", "トランスコード"),
    "autoclip_clean": ("pro", "自動切り抜きの書き出し（透かしなし）"),
    "motion_tracking": ("pro", "モーショントラッキング・手ぶれ補正"),
    "blender": ("pro", "Blender 連携（3D テロップ・3D ロゴ・レンダリング）"),
    "mmd": ("pro", "MMD 連携"),
    "dynamic_link": ("pro", "Dynamic Link（AE / Blender をそのまま置く）"),
    "ai": ("pro", "AI アシスト（Claude）・AI 画像生成（ComfyUI）"),
    "template_save": ("pro", "テンプレートの作成・保存"),
    "farm": ("studio", "ファーム（複数 PC で書き出し・変換）"),
    "production": ("studio", "制作管理（ショット・担当・期限）"),
}
# コマンド → 機能（メニュー・ショートカット・コマンドパレットのどこから呼んでも確かめる）
GATED_COMMANDS = {
    "transcode": "transcode", "track_motion": "motion_tracking", "stabilize": "motion_tracking",
    "text3d": "blender", "logo3d": "blender", "send_blender": "blender", "render_blender": "blender",
    "mmd_lipsync": "mmd", "mmd_render": "mmd", "mmd_tools_install": "mmd",
    "dynlink_import": "dynamic_link", "dynlink_rerender": "dynamic_link", "dynlink_open": "dynamic_link",
    "ai_assist": "ai", "ai_image": "ai", "farm_panel": "farm", "farm_export": "farm",
    "production_panel": "production", "shots_from_markers": "production",
    "telop_template_save": "template_save", "text3d_template_save": "template_save",
}


@dataclass
class License:
    edition: str = "free"
    name: str = ""
    email: str = ""
    expires: str = ""
    updates_until: str = ""
    seats: int = 1
    id: str = ""
    source: str = "free"          # key / trial / dev / env / free
    trial_left: int = 0
    error: str = ""

    @property
    def label(self) -> str:
        s = NAMES.get(self.edition, self.edition)
        if self.source == "trial":
            s += f"（お試し あと {self.trial_left} 日）"
        elif self.source == "dev":
            s += "（開発用）"
        elif self.source == "personal":
            s += "（自分用）"
        return s


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def parse_key(key: str) -> License:
    """キーを確かめる。正しくなければ error に理由。"""
    from . import ed25519
    key = "".join(key.split())
    if not key.startswith("TELOCUT-") or "." not in key:
        return License(error="キーの形が違います（TELOCUT- で始まる文字列を貼ってください）")
    body_s, sig_s = key[8:].split(".", 1)
    try:
        body, sig = _b64d(body_s), _b64d(sig_s)
    except Exception:
        return License(error="キーが壊れています（途中で切れていないか確認してください）")
    if not ed25519.verify(bytes.fromhex(PUBLIC_KEY), body, sig):
        return License(error="正しいキーではありません")
    d = json.loads(body.decode("utf-8"))
    lic = License(edition=d.get("edition", "pro"), name=d.get("name", ""), email=d.get("email", ""),
                  expires=d.get("expires", ""), updates_until=d.get("updates_until", ""), seats=int(d.get("seats", 1)),
                  id=d.get("id", ""), source="key")
    if lic.expires and lic.expires < datetime.date.today().isoformat():
        lic.error = f"期限が切れています（{lic.expires}）"
    return lic


_cache: Optional[License] = None


def current(refresh: bool = False) -> License:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    if not getattr(sys, "frozen", False):
        env = os.environ.get("TELOCUT_EDITION", "").lower()      # EXE では効かない（環境変数で制限を外せないように）
        _cache = License(edition=env, source="env") if env in RANK else License(edition="studio", source="dev")
        return _cache
    if BUILD_KIND == "personal":
        _cache = License(edition="studio", source="personal")
        return _cache
    if BUILD_KIND == "free":
        _cache = License(edition="free", source="free")
        return _cache
    lic = None
    try:
        if LICENSE_PATH.exists():
            lic = parse_key(LICENSE_PATH.read_text(encoding="utf-8"))
            if lic.error:
                lic = None
    except OSError:
        lic = None
    if lic is None:
        lic = _trial() or License(edition="free", source="free")
    _cache = lic
    return lic


def _trial() -> Optional[License]:
    start = getattr(SETTINGS, "trial_started", "") or ""
    if not start:
        return None
    try:
        left = TRIAL_DAYS - (datetime.date.today() - datetime.date.fromisoformat(start)).days
    except ValueError:
        return None
    if left <= 0:
        return None
    return License(edition="pro", source="trial", trial_left=left)


def can_register() -> bool:
    """キーの登録・お試しができるビルドか（無料版の EXE ではできない）。"""
    return BUILD_KIND != "free" or not getattr(sys, "frozen", False)


def start_trial() -> License:
    if not getattr(SETTINGS, "trial_started", ""):
        SETTINGS.trial_started = datetime.date.today().isoformat()
        SETTINGS.save()
    return current(refresh=True)


def install_key(key: str) -> License:
    lic = parse_key(key)
    if not lic.error:
        LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LICENSE_PATH.write_text("".join(key.split()), encoding="utf-8")
        current(refresh=True)
    return lic


def remove_key():
    try:
        LICENSE_PATH.unlink()
    except OSError:
        pass
    current(refresh=True)


def allowed(feature: str) -> bool:
    need = FEATURES.get(feature, ("free", ""))[0]
    return RANK.get(current().edition, 0) >= RANK[need]


def require(parent, feature: str) -> bool:
    """使えれば True。使えなければ案内を出して False。"""
    if allowed(feature):
        return True
    need, label = FEATURES.get(feature, ("pro", feature))
    from PyQt6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle(f"{NAMES[need]} の機能です")
    box.setText(f"「{label}」は {NAMES[need]} で使えます。\n今は {current().label} です。")
    reg = box.addButton("ライセンスを入力 / お試し..." if can_register() else "有料版について...", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("閉じる", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is reg and parent is not None and hasattr(parent, "license_dialog"):
        parent.license_dialog()
        return allowed(feature)
    return False


def watermark_text() -> str:
    """無料版で付ける透かし（自動切り抜きの書き出し）。Pro 以上は空。"""
    return "" if allowed("autoclip_clean") else "Made with TeloCut Free"
