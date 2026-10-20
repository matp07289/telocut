"""TeloCut の環境設定（ローカル PC ごと）。

NAS で素材やプロジェクトを共有しても、キャッシュ類（音声 / 波形 / プロキシ / レンダー）は
各 PC のローカルディスクに置くのが基本。ここで場所と動作を管理する。
"""
from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

BASE_DIR = Path(os.environ.get("TELOCUT_HOME") or (Path.home() / ".davintelop"))
SETTINGS_PATH = BASE_DIR / "telocut_settings.json"     # この PC の設定（Avid の「サイト設定」）
USERS_DIR = BASE_DIR / "users"                          # ユーザー設定（Avid の「ユーザー設定」）: users/<名前>.json

# ユーザーごとに持つ設定（ほかはこの PC 共通）
USER_KEYS = ("user_name", "keyboard_preset", "keymap", "keyboard_layout", "theme", "theme_brightness", "theme_accent",
             "theme_follow_os", "ui_font_pt", "autosave_minutes", "attic_keep", "open_last_project", "last_project",
             "last_location", "export_presets", "favorites", "palette_favorites", "effect_presets", "fx_mode_default",
             "telop_dict", "autoclip", "asset_saved_searches", "asset_recent", "se_dir", "timeline_track_height",
             "timeline_show_waveforms", "timeline_show_thumbs", "default_telop_template", "use_proxy",
             "auto_dual", "dual_swap", "screen_roles", "idle_render")


@dataclass
class AppSettings:
    cache_dir: str = str(BASE_DIR / "editor_cache")          # 音声 PCM・波形
    render_dir: str = str(BASE_DIR / "telocut_render")       # レンダーキャッシュ
    proxy_dir: str = str(BASE_DIR / "telocut_proxy")         # プロキシ
    local_media_dir: str = str(BASE_DIR / "telocut_local_media")  # NAS 素材のローカルコピー先
    attic_dir: str = str(BASE_DIR / "telocut_attic")         # バックアップ（Avid の Attic）
    render_cache_gb: float = 20.0
    ram_cache_frames: int = 400
    autosave_minutes: int = 5
    attic_keep: int = 30
    use_proxy: bool = True             # プロキシがあれば再生に使う
    proxy_width: int = 960
    auto_proxy_over: int = 2560        # この幅を超える素材は読み込み時に自動でプロキシ作成（0=しない）
    idle_render: bool = False          # 停止中に自動でバックグラウンドレンダリング
    render_threads: int = max(1, min(4, (os.cpu_count() or 2) // 2))
    path_maps: List[List[str]] = field(default_factory=list)   # [[置換前, 置換後], ...]
    keyboard_preset: str = "avid"
    keymap: dict = field(default_factory=dict)      # command_id -> [key, ...]（プリセットとの差分ではなく全体）
    keyboard_layout: str = "jis"
    fullscreen_screen: int = -1        # -1 = 自動（メイン以外の画面）
    user_name: str = ""
    # プロジェクトの場所（Avid の Private / Shared / External）
    private_dir: str = str(Path.home() / "Documents" / "TeloCut Projects")
    shared_dir: str = ""                # NAS の共有フォルダ
    external_dir: str = ""
    last_location: str = "private"
    last_project: str = ""
    se_dir: str = ""                   # 効果音フォルダ（SE パッド）
    favorites: List[dict] = field(default_factory=list)   # お気に入りアセット（全プロジェクト共通）
    asset_server: str = ""             # アセットサーバーの URL（例 http://192.168.0.10:8770）
    comfy_url: str = "http://192.168.0.53:8188"
    photoshop_exe: str = ""
    aftereffects_exe: str = ""
    blender_exe: str = ""
    claude_exe: str = ""
    palette_favorites: List[str] = field(default_factory=list)   # コマンドパレットの★
    effect_presets: List[dict] = field(default_factory=list)     # エフェクトプリセット {name, star, props}
    bridge_enabled: bool = True        # DavinTelop / MCP 連携用のローカル接続を開く
    auto_dual: bool = True             # ディスプレイをつないだら自動でマルチディスプレイ用レイアウトに
    dual_swap: bool = False            # 編集画面とパネル画面を入れ替える
    screen_roles: dict = field(default_factory=dict)   # 画面の組み合わせ → 各画面の役割（edit/panel/finish/client/none）
    voice_engine_paths: dict = field(default_factory=dict)   # 音声合成ソフトの場所 {"voicevox_app": ..., "voicevox_engine": ...}
    voice_auto_launch: bool = True     # 合成するとき音声合成ソフトが動いていなければ自動で起動
    voice_launch_app: bool = False     # 起動するときアプリ（画面あり）で起動する（False = エンジンだけ）
    plugin_dir: str = ""               # 追加のエフェクトプラグインのフォルダ（<設定フォルダ>/plugins も読む）
    fx_mode_default: str = "stack"     # 新しくエフェクトを付けるときの管理方法（stack = レイヤー / nodes = ノード）
    mmd_exe: str = ""                  # MikuMikuDance.exe の場所（空なら自動で探す）
    autoclip: dict = field(default_factory=dict)                 # 自動切り抜きの設定（出力先・OP / ED・しきい値など）
    version_limit_mb: float = 300.0                             # 更新履歴に版を残すファイルの大きさの上限
    telop_dict: List[list] = field(default_factory=list)      # 表記統一辞書 [[元, 直す形], ...]
    asset_recent: List[dict] = field(default_factory=list)          # アセットから最近取り込んだ素材
    asset_saved_searches: List[dict] = field(default_factory=list)  # 保存した検索 {name, params}
    # ── 画面（ユーザー設定）
    theme: str = "avid"                # 配色パターン（theme.THEMES）
    theme_brightness: int = 0          # 明るさ（−50〜50）
    theme_accent: str = ""             # 強調色（空 = 配色パターンの色）
    theme_follow_os: bool = False      # Windows のライト / ダークに合わせる
    ui_font_pt: float = 9.0            # 文字の大きさ
    open_last_project: bool = False    # 起動したら前回のプロジェクトを開く
    export_presets: List[dict] = field(default_factory=list)   # 書き出しプリセット {name, preset, range, alpha, quality}
    timeline_track_height: int = 0     # 新しいトラックの高さ（0 = 標準）
    timeline_show_waveforms: bool = True
    timeline_show_thumbs: bool = True
    default_telop_template: str = ""   # テロップを追加するときのテンプレート
    current_profile: str = "default"   # 使っているユーザー設定の名前（サイト設定にだけ保存）
    trial_started: str = ""            # Pro のお試しを始めた日（YYYY-MM-DD）

    @classmethod
    def load(cls) -> "AppSettings":
        try:
            d = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            keys = cls.__dataclass_fields__
            s = cls(**{k: v for k, v in d.items() if k in keys})
        except (OSError, ValueError, TypeError):
            s = cls()
        s._load_user(s.current_profile or "default")
        if not s.user_name:
            s.user_name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        return s

    # ── ユーザー設定（Avid のように、人ごとにキーボード・配色・書き出しプリセットなどを持つ）──
    @staticmethod
    def _user_path(name: str) -> Path:
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "default"
        return USERS_DIR / f"{safe}.json"

    def _load_user(self, name: str):
        p = self._user_path(name)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False                      # まだ無い（今の値をそのまま使う = 最初の保存でできる）
        for k in USER_KEYS:
            if k in d:
                setattr(self, k, d[k])
        return True

    def _user_dict(self) -> dict:
        d = asdict(self)
        return {k: d[k] for k in USER_KEYS if k in d}

    def save(self):
        try:
            BASE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, SETTINGS_PATH)
            USERS_DIR.mkdir(parents=True, exist_ok=True)
            up = self._user_path(self.current_profile or "default")
            tmp = up.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._user_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, up)
        except OSError:
            pass

    def profiles(self) -> List[str]:
        names = sorted(p.stem for p in USERS_DIR.glob("*.json")) if USERS_DIR.is_dir() else []
        if (self.current_profile or "default") not in names:
            names.append(self.current_profile or "default")
        return sorted(set(names))

    def switch_profile(self, name: str, copy_current: bool = False):
        """ユーザー設定を切り替える（無ければ作る。copy_current なら今の設定を引き継ぐ）。"""
        self.save()
        self.current_profile = name
        if not self._load_user(name) and not copy_current:
            fresh = AppSettings()
            for k in USER_KEYS:
                setattr(self, k, getattr(fresh, k))
            self.user_name = name
        self.save()

    def delete_profile(self, name: str) -> bool:
        if name == (self.current_profile or "default"):
            return False
        try:
            self._user_path(name).unlink()
            return True
        except OSError:
            return False

    def export_profile(self, path: str):
        Path(path).write_text(json.dumps({"telocut_user_settings": 1, "name": self.current_profile,
                                          **self._user_dict()}, ensure_ascii=False, indent=1), encoding="utf-8")

    def import_profile(self, path: str, name: str = "") -> str:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        name = name or d.get("name") or Path(path).stem
        USERS_DIR.mkdir(parents=True, exist_ok=True)
        self._user_path(name).write_text(json.dumps({k: d[k] for k in USER_KEYS if k in d}, ensure_ascii=False,
                                                    indent=1), encoding="utf-8")
        return name

    def ensure_dirs(self):
        for d in (self.cache_dir, self.render_dir, self.proxy_dir, self.attic_dir):
            try:
                Path(d).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def map_path(self, path: str) -> str:
        """パス置換ルール（NAS のドライブ文字の違いなど）を適用した候補を返す。"""
        norm = path.replace("/", "\\")
        for src, dst in self.path_maps:
            s = src.replace("/", "\\").rstrip("\\")
            if s and norm.lower().startswith(s.lower()):
                out = dst.replace("/", "\\").rstrip("\\") + norm[len(s):]
                # Linux（VM のレンダーノード等）では / 区切りに戻す
                return out if os.name == "nt" else out.replace("\\", "/")
        return path

    @staticmethod
    def host() -> str:
        try:
            return socket.gethostname()
        except OSError:
            return "unknown"


SETTINGS = AppSettings.load()
