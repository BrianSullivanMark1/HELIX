"""Logging + a crash guard. Call setup_logging() once at startup."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG = logging.getLogger("helix")


def setup_logging(log_file: Path, *, level: int = logging.INFO) -> logging.Logger:
    """Configure the 'helix' logger with a rotating file + console handler. Idempotent."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _LOG.setLevel(level)
    if _LOG.handlers:  # already configured
        return _LOG

    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s: %(message)s")

    file_h = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_h.setFormatter(fmt)
    _LOG.addHandler(file_h)

    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    _LOG.addHandler(stream_h)

    _install_excepthook()
    return _LOG


def get_logger(name: str = "helix") -> logging.Logger:
    return logging.getLogger(name if name.startswith("helix") else f"helix.{name}")


def _install_excepthook() -> None:
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            _LOG.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        prev(exc_type, exc, tb)

    sys.excepthook = hook
