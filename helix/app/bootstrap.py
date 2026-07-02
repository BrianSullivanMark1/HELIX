"""Bootstrap — build the container, create the window, run the Qt loop.

Wrapped in a self-heal: if startup fails (e.g. a merged self-change bricks the shell), restore the last
commit that booted cleanly and relaunch — so the Archive lifeline is never trapped behind a broken
shell. This module is a PROTECTED_PATH (under helix/app/); the coder may never edit it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Let QtWebEngine render heavier WebGL (high-poly 3D models) without the GPU process being killed — the
# "sandbox limit" symptom: a blank viewer on a big mesh. Force GPU use even on blocklisted/integrated
# chips, enable GPU rasterization, and drop the GPU-PROCESS sandbox (the renderer sandbox stays). Must be
# set before QtWebEngine initializes (i.e. before importing the UI below). A user env override wins.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy --disable-gpu-sandbox",
)

import time

from PyQt6.QtCore import Qt, QLockFile
from PyQt6.QtWidgets import QApplication

from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.adapters.watchdog import clear_clean_exit, mark_clean_exit, spawn_watchdog
from helix.app.container import Container
from helix.config import AppPaths
from helix.logging_setup import get_logger, setup_logging
from helix.ui.main_window import HelixMainWindow
from helix.ui.theme import apply_theme

_LOG = get_logger("bootstrap")
_LAST_GOOD = "last_good_commit"
_HEALING = "healing_in_progress"


def _acquire_instance_lock(data_dir: Path) -> QLockFile | None:
    """One HELIX per data dir. QLockFile clears stale locks from dead processes itself; the short retry
    loop covers the restart path, where the old process may not have released the lock yet. Returns the
    held lock (keep it alive for the process lifetime), or None when another live instance owns it."""
    lock = QLockFile(str(data_dir / "helix.lock"))
    deadline = time.monotonic() + 10.0
    while True:
        if lock.tryLock(0):
            return lock
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def run_app(argv: list[str] | None = None) -> int:
    # QtWebEngine (the in-app viewer for built apps/models) needs shared GL contexts, set before the
    # QApplication is created. Harmless when WebEngine isn't installed.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("HELIX")
    apply_theme(app)

    # Single instance BEFORE any state is touched: a second launch (double-click races, a watchdog
    # relaunch crossing a manual one) must not fight the first over the mic, the DB, and the lock files.
    paths = AppPaths.resolve().ensure()
    instance_lock = _acquire_instance_lock(paths.data)
    if instance_lock is None:
        _LOG.info("another HELIX instance is already running — exiting")
        return 0

    try:
        container = Container()
        window = HelixMainWindow(container)
    except Exception:
        _LOG.exception("startup failed — attempting self-heal")
        if _self_heal():
            return 0  # a fresh, restored process was spawned
        raise

    # Clean up anything a previous crash/kill/power-loss left half-done before the menu is shown: partial
    # builds, and leaked self-change draft worktrees/branches.
    try:
        container.forge.recover_interrupted()
    except Exception:
        _LOG.exception("interrupted-build recovery failed")
    try:
        container.selfdev.recover_interrupted()
    except Exception:
        _LOG.exception("interrupted self-change recovery failed")

    # Crash watchdog protocol: clear any stale clean-exit sentinel now, and write a fresh one FIRST on
    # aboutToQuit (before the other teardown hooks — a hang in one of them must not turn a clean quit
    # into a phantom "crash" the watchdog would resurrect).
    clear_clean_exit(container.paths.data)
    app.aboutToQuit.connect(lambda: mark_clean_exit(container.paths.data))

    # Belt-and-suspenders: tear down on ANY exit path — including a restart's quit() / OS logoff, which
    # bypass the window's closeEvent. Order matters (release the mic + join workers and reap the coder
    # BEFORE closing the DB). All steps are idempotent, so a normal closeEvent close runs them twice
    # harmlessly. console.shutdown releases the microphone + joins QtWorker threads — essential on the
    # restart path so the old process frees the mic before the new one opens it.
    app.aboutToQuit.connect(container.build_queue.shutdown)
    app.aboutToQuit.connect(container.selfdev_lane.shutdown)
    app.aboutToQuit.connect(window.console.shutdown)
    app.aboutToQuit.connect(container.store.close)

    window.show()
    _record_good(container)
    # Only a HEALTHY startup gets a watchdog (a boot failure is self-heal's job) — from here on, any
    # death without the clean-exit sentinel gets the app relaunched so the always-on cadence survives.
    spawn_watchdog(container.paths.data, container.paths.root / "main.py", container.paths.root)
    return app.exec()


def _record_good(container: Container) -> None:
    """Remember this commit as the last that booted cleanly, and clear the heal flag."""
    try:
        # Never record a self-change branch as last-good — self-heal must always restore to a real base
        # commit, not a half-approved selfdev branch. (Worktree-isolated drafts shouldn't leave the live
        # tree here, but guard anyway.)
        if container.repo.current_branch(container.paths.root).startswith("selfdev/"):
            _LOG.warning("on a selfdev branch at startup — not recording it as last-good")
            return
        head = container.repo.branch_head(container.paths.root, "HEAD")
        container.settings.set(_LAST_GOOD, head.sha)
        container.settings.set(_HEALING, False)
    except Exception:
        _LOG.exception("could not record last-good commit")


def _self_heal() -> bool:
    """Restore the last-known-good commit and relaunch once. Returns True if a relaunch was spawned."""
    try:
        paths = AppPaths.resolve()
        setup_logging(paths.log_file)
        settings = JsonSettings(paths.settings_file)
        good = settings.get(_LAST_GOOD)
        if not good or settings.get(_HEALING):  # nothing to restore, or we already tried — give up
            return False
        settings.set(_HEALING, True)
        GitRepo().restore_to(paths.root, good)
        Restarter(paths.root / "main.py", paths.root).restart()
        _LOG.info("self-healed to %s and relaunched", good)
        return True
    except Exception:
        _LOG.exception("self-heal failed")
        return False
