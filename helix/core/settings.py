from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from helix.core.config import load_config


class AppSettings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_config().settings_path
        self.path.parent.mkdir(exist_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self.load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        data = self.load()
        data[key] = value
        self.save(data)

    def remove(self, key: str) -> None:
        data = self.load()
        data.pop(key, None)
        self.save(data)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
