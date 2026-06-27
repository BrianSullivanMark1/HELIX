"""SelfDevLane — drafts a self-change (SelfDevService.propose) on a BACKGROUND thread so the orb keeps
talking, announcing the result on the bus. One draft at a time.

This is the unprotected orchestration around the protected SelfDevService gate: it adds threading +
cancel + bus events WITHOUT putting any of that into the safety core. propose() still does all the
Constitution scanning, the worktree isolation, and the smoke-checked approval path unchanged.
"""
from __future__ import annotations

import threading

from helix.domain.events import SelfChangeFinished, SelfChangeProgress
from helix.logging_setup import get_logger
from helix.ports.events import EventBus
from helix.services.cancel import CancelToken
from helix.services.selfdev import SelfDevService

_LOG = get_logger("selfdev_lane")


class SelfDevLane:
    def __init__(self, selfdev: SelfDevService, bus: EventBus) -> None:
        self._selfdev = selfdev
        self._bus = bus
        self._lock = threading.Lock()
        self._busy = False
        self._cancel: CancelToken | None = None
        self._thread: threading.Thread | None = None

    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def start(self, request: str) -> bool:
        """Begin drafting in the background. Returns False if a draft is already running (one at a time)."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel = CancelToken()
            cancel = self._cancel
        self._thread = threading.Thread(
            target=self._run, args=(request, cancel), daemon=True, name="helix-selfdev"
        )
        self._thread.start()
        return True

    def cancel(self) -> None:
        with self._lock:
            c = self._cancel
        if c is not None:
            c.cancel()

    def _run(self, request: str, cancel: CancelToken) -> None:
        def on_progress(line: str) -> None:
            self._bus.publish(SelfChangeProgress(line))

        try:
            change = self._selfdev.propose(request, on_progress=on_progress, cancel=cancel)
            self._bus.publish(SelfChangeFinished(ok=True, summary=change.summary, branch=change.branch))
        except Exception as exc:  # noqa: BLE001 - surface any drafting failure as an announcement
            stopped = cancel.is_set()
            _LOG.info("self-change draft %s: %s", "stopped" if stopped else "failed", exc)
            self._bus.publish(SelfChangeFinished(ok=False, error=str(exc), stopped=stopped))
        finally:
            with self._lock:
                self._busy = False
                self._cancel = None

    def shutdown(self, timeout: float = 3.0) -> None:
        """Cancel an in-flight draft and wait briefly for it to unwind (called on app close)."""
        self.cancel()
        t = self._thread
        if t is not None:
            t.join(timeout)
