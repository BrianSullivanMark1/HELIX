"""Bootstrap — build the container, create the window, run the Qt loop.

Wrapped in a self-heal: if startup fails (e.g. a merged self-change bricks the shell), restore the last
commit that booted cleanly and relaunch — so the Archive lifeline is never trapped behind a broken
shell. This module is a PROTECTED_PATH (under helix/app/); the coder may never edit it.
"""
from __future__ import annotations

import os
import sys

# Let QtWebEngine render heavier WebGL (high-poly 3D models) without the GPU process being killed — the
# "sandbox limit" symptom: a blank viewer on a big mesh. Force GPU use even on blocklisted/integrated
# chips, enable GPU rasterization, and drop the GPU-PROCESS sandbox (the renderer sandbox stays). Must be
# set before QtWebEngine initializes (i.e. before importing the UI below). A user env override wins.
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy --disable-gpu-sandbox",
)

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.adapters.watchdog import clear_clean_exit, mark_clean_exit, spawn_watchdog
from helix.app.container import Container
from helix.app.single_instance import start_activation_server
from helix.config import AppPaths
from helix.logging_setup import get_logger, setup_logging
from helix.ui.main_window import HelixMainWindow
from helix.ui.theme import apply_theme

_LOG = get_logger("bootstrap")
_LAST_GOOD = "last_good_commit"
_HEALING = "healing_in_progress"


def _raise_window(window) -> None:
    """Bring the already-running window to the foreground when a second launch pings the activation
    server (single_instance). On Windows the OS may only flash the taskbar if it blocks the focus
    steal — acceptable; the point is that a second icon click surfaces HELIX rather than doing nothing."""
    try:
        window.setWindowState(
            (window.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
        )
        window.show()
        window.raise_()
        window.activateWindow()
        QApplication.alert(window, 0)
    except Exception:
        _LOG.exception("could not raise the window for a second launch")


def run_app(argv: list[str] | None = None) -> int:
    # QtWebEngine (the in-app viewer for built apps/models) needs shared GL contexts, set before the
    # QApplication is created. Harmless when WebEngine isn't installed.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("HELIX")
    apply_theme(app)

    # Single instance is already guaranteed upstream in main.py (a per-data-dir lock taken BEFORE the
    # voice pre-warm, so a duplicate launch never even reaches here). This process holds that lock.
    try:
        container = Container()
        window = HelixMainWindow(container)
    except Exception:
        _LOG.exception("startup failed — attempting self-heal")
        if _self_heal():
            return 0  # a fresh, restored process was spawned
        raise

    # Start listening for second-launch pings AS EARLY AS POSSIBLE — before the (potentially several-
    # second) interrupted-work recovery below — so a second desktop-icon click during a cold boot still
    # surfaces this window instead of doing nothing. Raising a not-yet-shown window just shows it early.
    # Best-effort: the hard single-instance guarantee is the lock held since main.py; this is the nicety.
    window._activation_server = start_activation_server(
        app, container.paths.data, lambda: _raise_window(window)
    )

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
    # bypass the window's closeEvent. window.teardown() is the SINGLE full-cleanup path (it also stops the
    # heartbeat and the backend-app servers, which the old partial hook here missed — leaking watcher
    # timers + server processes on a restart). It's guarded to run once, so a normal closeEvent that
    # already tore down is a harmless no-op here. console.shutdown (inside teardown) releases the mic +
    # joins QtWorker threads — essential on restart so the old process frees the mic before the new opens it.
    app.aboutToQuit.connect(window.teardown)

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
