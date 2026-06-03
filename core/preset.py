"""プリセット管理 (JSON形式で保存/読み込み)"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import json

from .models import DesignSettings

_DEFAULT_DIR = Path.home() / ".davintelop" / "presets"


class PresetManager:
    def __init__(self, preset_dir: Optional[Path] = None):
        self.preset_dir = preset_dir or _DEFAULT_DIR
        self.preset_dir.mkdir(parents=True, exist_ok=True)

    def list_presets(self) -> list[str]:
        """プリセット名の一覧を返す。"""
        return sorted(p.stem for p in self.preset_dir.glob("*.json"))

    def save_preset(self, name: str, settings: DesignSettings) -> None:
        """プリセットを保存する。"""
        path = self.preset_dir / f"{name}.json"
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(settings.to_dict(), f, ensure_ascii=False, indent=2)

    def load_preset(self, name: str) -> DesignSettings:
        """プリセットを読み込む。"""
        path = self.preset_dir / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"プリセットが見つかりません: {name}")
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return DesignSettings.from_dict(data)

    def delete_preset(self, name: str) -> None:
        """プリセットを削除する。"""
        path = self.preset_dir / f"{name}.json"
        if path.exists():
            path.unlink()

    def rename_preset(self, old_name: str, new_name: str) -> None:
        """プリセットをリネームする。"""
        old_path = self.preset_dir / f"{old_name}.json"
        new_path = self.preset_dir / f"{new_name}.json"
        if old_path.exists():
            old_path.rename(new_path)
