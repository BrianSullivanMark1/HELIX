from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HelixConfig:
    root_dir: Path
    data_dir: Path
    db_path: Path
    settings_path: Path


def _default_root() -> Path:
    """Repo root in dev; the folder NEXT TO the .exe when frozen (PyInstaller, §41) — so `data/`
    (the DB + keys) lives beside the executable on the tablet: persistent, and easy to find/edit."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def load_config(root_dir: Path | None = None) -> HelixConfig:
    root = root_dir or _default_root()
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return HelixConfig(
        root_dir=root,
        data_dir=data_dir,
        db_path=data_dir / "helix.db",
        settings_path=data_dir / "helix_settings.json",
    )
