"""CancelToken — a one-shot 'stop' signal threaded from the Console down into a running build.

A turn runs on a worker thread; the user can hit Esc, tap the orb, or say "stop". That sets the token,
which the coder polls (the CLI subprocess is killed; the API loop breaks). The token also carries a
BuildHandle once a build has started, so after a stop the Console knows WHAT was being built and can
offer to remove the half-finished app/model/task.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class BuildHandle:
    """Identifies the build a stop interrupted, so the UI can offer to remove or roll it back."""
    slug: str
    name: str
    iterating: bool  # True = an existing build was being changed (roll back); False = a new one (delete)
    is_model: bool = False


class CancelToken:
    """Thread-safe stop flag. `.cancel()` from the UI thread; `.is_set()` polled by the worker."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.build: BuildHandle | None = None  # set by ForgeService when a build begins

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()
