"""Always-on reliability (§39): keep the permanently-running desktop app alive and diagnosable.

Brian runs HELIX as a permanently-open app rather than via OS schedulers, so the app itself has to
(a) survive an unexpected error in any UI callback and (b) leave a trail to diagnose unattended
issues. PyQt6 aborts the whole process (qFatal) on an unhandled exception in a slot *unless*
`sys.excepthook` has been replaced — so installing a custom hook is the single most important
safeguard for an unattended trader. Stdlib-only, no third-party deps."""
from __future__ import annotations

import logging
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from helix.core.config import load_config

LOGGER_NAME = "helix"
_configured = False


def default_log_path() -> Path:
    """data/helix.log — next to the DB and settings (git-ignored data dir)."""
    return load_config().data_dir / "helix.log"


def setup_logging(path: Path | None = None, *, echo: bool = True) -> logging.Logger:
    """Configure HELIX's rotating file log (default data/helix.log). Idempotent — safe to call from
    every entry point; only the first call installs handlers. Returns the 'helix' logger. A failure
    to open the file (e.g. read-only FS) is swallowed so logging can never stop the app."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    target = path or default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception:  # noqa: BLE001 — never let logging setup sink the app
        pass
    if echo:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        logger.addHandler(stream)
    _configured = True
    return logger


def install_crash_guard(logger: logging.Logger | None = None) -> logging.Logger:
    """Replace `sys.excepthook` so an unhandled exception in ANY Qt slot/callback is LOGGED and the
    app keeps running, instead of PyQt calling qFatal() and aborting the process. This is the core
    always-on safeguard: a permanently-running trader must not die from an unexpected error in a UI
    callback (the trading cycle itself reschedules independently, so surviving one bad tick means the
    next tick still runs). KeyboardInterrupt is left to the default hook so Ctrl+C still exits."""
    log = logger or setup_logging()

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error(
            "Unhandled exception (app kept alive):\n%s",
            "".join(traceback.format_exception(exc_type, exc, tb)),
        )

    sys.excepthook = _hook
    return log
