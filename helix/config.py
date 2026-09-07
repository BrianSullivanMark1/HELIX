"""Filesystem layout — the one place that knows where things live on disk.

Resolves the app root for both development (the repo) and a frozen PyInstaller build (the folder next to
the executable), and derives every read/write path from it. Nothing else hard-codes a path.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from helix.logging_setup import get_logger

_LOG = get_logger("config")

# THE BUILD STAMP (READ_ME/DREAM.md §3). build.py writes this file into the bundle (--add-data) so a
# FROZEN HELIX knows which source repository it was built from and which Python built it — the two
# facts the dream session needs to draft against the real source and to rebuild the app at dawn.
# Absent in development (nothing writes it into the tree), so build_info() is empty there.
BUILD_INFO_NAME = "build_info.json"
# Settings that override the stamp in a frozen build (the Settings card can point a moved repo).
SOURCE_ROOT_SETTING = "source_root"
DEV_PYTHON_SETTING = "dev_python"


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _app_root() -> Path:
    # Frozen (PyInstaller --onedir): the folder next to the executable.
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    # Development: the repo root (helix/config.py -> repo/).
    return Path(__file__).resolve().parent.parent


def _build_info_candidates() -> tuple[Path, ...]:
    """Where the stamp can sit: beside this module (the package-relative path that is right both in
    dev and in the bundle, exactly as helix/ui/assets is found), and the bundle root as a backstop."""
    here = Path(__file__).resolve().parent / BUILD_INFO_NAME
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return (here, Path(meipass) / "helix" / BUILD_INFO_NAME)
    return (here,)


def build_info(path: Path | None = None) -> dict:
    """What build.py stamped into this bundle: {"source_root", "python", "sha", "built_at"}. Empty in
    development or when the stamp is missing or unreadable — callers treat every key as optional."""
    candidates = (Path(path),) if path is not None else _build_info_candidates()
    for candidate in candidates:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        return data if isinstance(data, dict) else {}
    return {}


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
    # The Amazon faculty's stores: the staged cart (survives a restart mid-shop), the parts lists /
    # handoff ledger, and the Chrome PROFILE FOLDER HELIX drives to build the cart — a browser writes
    # into its profile constantly while open, exactly the kind of live churn a coder guard must skip.
    "helix_cart.json",
    "helix_parts.json",
    "amazon-chrome",
    # The dream session's journal (services/dream.py): the session thread writes it after every plan,
    # draft and apply — i.e. WHILE its own coder draft runs — so both guards must skip it or every
    # overnight draft would be refused as an escape the moment the journal recorded it.
    "helix_dream.json",
    # The Dream Mind's stores (READ_ME/DREAM_MIND.md §10–§11): the verified-knowledge record (a
    # night notes facts while its own coder drafts run) and the self-model — written mid-draft
    # by the session thread, so both guards must skip them like the dream journal.
    "helix_verified.json",
    "helix_self.json",
    # The improvement backlog (services/backlog.py): the night queues research-found ideas on it
    # while its own coder draft runs, so both guards must skip it like the dream journal.
    "helix_backlog.json",
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
    """Resolved, absolute paths for everything HELIX reads or writes.

    `frozen` pins whether this is a packaged build (None = ask sys.frozen, the real answer; tests
    set it explicitly to exercise the frozen paths without freezing anything)."""

    root: Path
    data: Path
    frozen: bool | None = None

    @classmethod
    def resolve(cls) -> "AppPaths":
        root = _app_root()
        override = (os.environ.get("HELIX_DATA_DIR") or "").strip()
        if override:
            # An explicit data-dir override (tests, a scratch instance beside the real one). The
            # single-instance lock is per-data-dir, so an override instance never fights the real app.
            return cls(root=root, data=Path(override))
        if _is_frozen():
            data = _frozen_data_dir()
            migrate_legacy_data(root / "data", data)
        else:
            data = root / "data"  # development: repo-local, unchanged
        return cls(root=root, data=data)

    @property
    def is_frozen(self) -> bool:
        return _is_frozen() if self.frozen is None else bool(self.frozen)

    def _setting(self, key: str) -> str:
        """One string setting, read fresh from helix_settings.json. config sits below the adapters,
        so it reads the file itself rather than importing the settings store; the two keys it reads
        are written only by hand or by the Settings card, so a fresh read per call is the right
        trade — nothing here caches a path the user just corrected."""
        try:
            data = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return ""
        value = data.get(key) if isinstance(data, dict) else None
        return value.strip() if isinstance(value, str) else ""

    @property
    def source_root(self) -> Path | None:
        """The git repository self-changes draft against (READ_ME/DREAM.md §3).

        Development: the repo itself (`root`). Frozen: `root` is dist/HELIX — the install folder next
        to the exe, which is no git repository at all — so the answer is the SOURCE tree the build
        came from: the `source_root` setting when set, else the build stamp; either counts only when
        that folder really holds a `.git` (a linked worktree has a .git FILE, a repo a directory —
        both pass). None means "no usable source": the dream session says so instead of pretending."""
        if not self.is_frozen:
            return self.root
        for candidate in (self._setting(SOURCE_ROOT_SETTING),
                          str(build_info().get("source_root") or "")):
            if candidate:
                folder = Path(candidate)
                if (folder / ".git").exists():
                    return folder
        return None

    @property
    def dev_python(self) -> str | None:
        """The interpreter that runs the test suite and build.py. Development: this very Python.
        Frozen: sys.executable is HELIX.exe, so it is the `dev_python` setting when set, else the
        interpreter recorded in the build stamp — and only if that file still exists."""
        if not self.is_frozen:
            return sys.executable
        for candidate in (self._setting(DEV_PYTHON_SETTING), str(build_info().get("python") or "")):
            if candidate and Path(candidate).is_file():
                return candidate
        return None

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
