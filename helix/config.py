"""Filesystem layout — the one place that knows where things live on disk.

Resolves the app root for both development (the repo) and a frozen PyInstaller build (the folder next to
the executable), and derives every read/write path from it. Nothing else hard-codes a path.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


def _app_root() -> Path:
    # Frozen (PyInstaller --onedir): the folder next to the executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Development: the repo root (helix/config.py -> repo/).
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class AppPaths:
    """Resolved, absolute paths for everything HELIX reads or writes."""

    root: Path
    data: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        root = _app_root()
        return cls(root=root, data=root / "data")

    def ensure(self) -> "AppPaths":
        """Create data/ and its subfolders if missing. Safe to call repeatedly."""
        self.data.mkdir(parents=True, exist_ok=True)
        self.builds.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def settings_file(self) -> Path:
        return self.data / "helix_settings.json"

    @property
    def db_file(self) -> Path:
        return self.data / "helix.db"

    @property
    def builds(self) -> Path:
        return self.data / "builds"

    @property
    def log_file(self) -> Path:
        return self.data / "helix.log"
