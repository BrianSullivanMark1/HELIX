"""Filesystem layout — the one place that knows where things live on disk.

Resolves the app root for both development (the repo) and a frozen PyInstaller build (the folder next to
the executable), and derives every read/write path from it. Nothing else hard-codes a path.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("config")


def _app_root() -> Path:
    # Frozen (PyInstaller --onedir): the folder next to the executable.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # Development: the repo root (helix/config.py -> repo/).
    return Path(__file__).resolve().parent.parent


def _frozen_data_dir() -> Path:
    """Where a packaged HELIX keeps user data: %LOCALAPPDATA%/HELIX/data — OUTSIDE the install folder,
    so a rebuild, reinstall, or folder swap can never wipe the user's keys, history, and built apps."""
    base = os.environ.get("LOCALAPPDATA")
    local = Path(base) if base else Path.home() / "AppData" / "Local"
    return local / "HELIX" / "data"


_MIGRATED_MARKER = ".helix-data-migrated"  # written ONLY after a fully-successful migration

# The app's OWN volatile stores — files the LIVE running app rewrites on its own during normal
# operation: sqlite checkpoints (helix.db), the log, the heartbeat stamping scheduled agents, the
# background distillers (memory/lessons), usage/recency ledgers, a key connected mid-run (secrets).
# BOTH escape guards — the Forge build guard AND the self-dev data guard — must SKIP these when they
# scan data/ for a coder that wrote outside its workspace. A coder run (a build, or a self-change
# draft) lasts minutes; a write to one of these files DURING that window is HELIX itself, not the
# coder, and must never be mistaken for an escape and fail an otherwise-good build/draft. ONE list,
# so the two guards can never drift apart (a drift is exactly how helix_reflexes.json was once missed).
VOLATILE_STORE_NAMES: tuple[str, ...] = (
    "helix.db",
    "helix.log",
    "helix_agents.json",
    "helix_memory.json",
    "helix_lessons.json",
    "helix_locations.json",
    "helix_usage.json",
    "helix_workflows.json",
    "helix_voices.json",
    "helix_reminders.json",
    "helix_reflexes.json",
    "helix_secrets.json",
)


def volatile_data_paths(data_dir: "Path") -> tuple["Path", ...]:
    """The absolute paths of the app's volatile stores under `data_dir` — the skip set both coder
    guards use so the live app's own writes are never mistaken for a coder escape."""
    d = Path(data_dir)
    return tuple(d / name for name in VOLATILE_STORE_NAMES)


def _has_real_data(d: Path) -> bool:
    """True if `d` already holds actual user data (settings, DB, secrets, or built apps) — as opposed to
    an empty scaffold that ensure() creates (a bare, empty builds/ subdir). Lets migration tell 'the user
    is already established here, leave it alone' from 'a failed prior attempt left an empty shell'."""
    if not d.is_dir():
        return False
    for name in ("helix_settings.json", "helix.db", "helix_secrets.json"):
        if (d / name).exists():
            return True
    builds = d / "builds"
    try:
        return builds.is_dir() and any(builds.iterdir())
    except OSError:
        return False


def migrate_legacy_data(old: Path, new: Path) -> None:
    """One-time move of exe-adjacent data/ (the legacy frozen location) to the new home.

    Robust against a FAILED attempt. "Already migrated" is tracked by a marker file written ONLY on full
    success — NOT by "the new dir exists / is non-empty", because ensure() legitimately creates an empty
    builds/ subdir under new before this ever completes, which would otherwise mask a pending migration
    and present the user a permanent factory reset while their real data sits stranded in the old dir.
      1. new absent → same-volume atomic rename (fast path);
      2. new already scaffolded (a prior failed attempt, or ensure() ran first) or cross-volume →
         copy-merge the legacy tree in, keeping the legacy folder as a backup.
    A failure is LOGGED and retried on the next launch; it never blocks startup."""
    try:
        marker = new / _MIGRATED_MARKER
        if marker.exists():
            return  # definitively migrated already
        if not old.is_dir():
            return  # nothing to migrate (a fresh install)
        if _has_real_data(new):
            # The user is already established on the new location (a prior successful migration whose
            # marker predates this version, or they started fresh here). NEVER overwrite it; just record
            # that migration is settled so we stop re-checking.
            try:
                marker.write_text("1", encoding="utf-8")
            except OSError:
                pass
            return
        new.parent.mkdir(parents=True, exist_ok=True)
        if not new.exists():
            try:
                old.rename(new)  # atomic same-volume move of the whole legacy tree
                (new / _MIGRATED_MARKER).write_text("1", encoding="utf-8")
                _LOG.info("migrated legacy data %s -> %s", old, new)
                return
            except OSError:
                pass  # cross-volume or locked — fall through to copy-merge
        # new exists (empty scaffold from a prior launch's ensure(), or a partial earlier attempt): fill
        # in the legacy data, keeping old as a backup. Only mark done on full success, so a failure here
        # (locked file, disk full) leaves NO marker and the next launch retries instead of stranding data.
        shutil.copytree(old, new, dirs_exist_ok=True)
        marker.write_text("1", encoding="utf-8")
        _LOG.info("migrated legacy data %s -> %s (legacy copy kept as backup)", old, new)
    except OSError:
        _LOG.warning("legacy data migration failed; will retry next launch", exc_info=True)


@dataclass(frozen=True)
class AppPaths:
    """Resolved, absolute paths for everything HELIX reads or writes."""

    root: Path
    data: Path

    @classmethod
    def resolve(cls) -> "AppPaths":
        root = _app_root()
        if getattr(sys, "frozen", False):
            data = _frozen_data_dir()
            migrate_legacy_data(root / "data", data)
        else:
            data = root / "data"  # development: repo-local, unchanged
        return cls(root=root, data=data)

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
