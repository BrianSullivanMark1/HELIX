from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HelixConfig:
    root_dir: Path
    data_dir: Path
    db_path: Path
    settings_path: Path


def load_config(root_dir: Path | None = None) -> HelixConfig:
    root = root_dir or Path(__file__).resolve().parents[2]
    data_dir = root / "data"
    data_dir.mkdir(exist_ok=True)
    return HelixConfig(
        root_dir=root,
        data_dir=data_dir,
        db_path=data_dir / "helix.db",
        settings_path=data_dir / "helix_settings.json",
    )
