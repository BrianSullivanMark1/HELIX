"""QtWorker — run a service call off the UI thread; results return via signals.

The UI thread only ever touches widgets. A long call (a Claude turn, a build) runs here; `progress`,
`finished`, and `failed` are delivered back on the UI thread by Qt's queued signal delivery.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QThread, pyqtSignal

from helix.logging_setup import get_logger

_LOG = get_logger("ui.worker")


class QtWorker(QThread):
    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], object]) -> None:
        """`fn` receives a progress callback and returns the result."""
        super().__init__()
        self._fn = fn

    def run(self) -> None:  # executes on the worker thread
        try:
            result = self._fn(self.progress.emit)
            self.finished_ok.emit(result)
        except Exception as exc:  # never let an exception kill the thread silently
            # Log the full traceback for diagnosis; surface a typed one-line message to the UI (the
            # `failed` signal stays `str` so existing slots keep working).
            _LOG.exception("worker task failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
