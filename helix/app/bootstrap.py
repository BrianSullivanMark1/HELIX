"""Bootstrap — build the container, create the window, run the Qt loop.

Wrapped in a self-heal: if startup fails (e.g. a merged self-change bricks the shell), restore the last
commit that booted cleanly and relaunch — so the Archive lifeline is never trapped behind a broken
shell. This module is a PROTECTED_PATH (under helix/app/); the coder may never edit it.
"""
from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication

from helix.adapters.git_repo import GitRepo
from helix.adapters.json_settings import JsonSettings
from helix.adapters.restart import Restarter
from helix.app.container import Container
from helix.config import AppPaths
from helix.logging_setup import get_logger, setup_logging
from helix.ui.main_window import HelixMainWindow
from helix.ui.theme import apply_theme

_LOG = get_logger("bootstrap")
_LAST_GOOD = "last_good_commit"
_HEALING = "healing_in_progress"


def run_app(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("HELIX")
    apply_theme(app)

    try:
        container = Container()
        window = HelixMainWindow(container)
    except Exception:
        _LOG.exception("startup failed — attempting self-heal")
        if _self_heal():
            return 0  # a fresh, restored process was spawned
        raise

    window.show()
    _record_good(container)
    return app.exec()


def _record_good(container: Container) -> None:
    """Remember this commit as the last that booted cleanly, and clear the heal flag."""
    try:
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
