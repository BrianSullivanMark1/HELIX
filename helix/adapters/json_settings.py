"""SettingsStore adapter — one JSON file, atomic writes, thread-safe."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any


class JsonSettings:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        try:
            self._data = json.loads(self._path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), "utf-8")
        tmp.replace(self._path)  # atomic on the same volume

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    def all(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
