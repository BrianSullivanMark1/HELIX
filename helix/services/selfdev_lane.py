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

    def start(self, request: str, model: str | None = None, unattended: bool = False) -> bool:
        """Begin drafting in the background. Returns False if a draft is already running (one at a
        time). `model` optionally pins the coder model for this draft — Evolve's Fable-5 proposal sizes
        it to the task (deep=Fable 5, standard=Opus 4.8 floor); None keeps the growth coder's default.

        `unattended` says nobody asked for this draft and nobody is in the room: it rides on every
        event this lane publishes so the console can keep the overnight Evolve pass SILENT. It has to
        be decided here, by the caller, because by the time the console sees a line there is nothing
        left to tell a 3 AM pass apart from a draft the user is watching — both look identical on the
        bus. Defaulted False so improve_helix, which the user asked for and is sitting through, keeps
        narrating aloud exactly as before."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel = CancelToken()
            cancel = self._cancel
        self._thread = threading.Thread(
            target=self._run, args=(request, cancel, model, unattended), daemon=True,
            name="helix-selfdev",
        )
        self._thread.start()
        return True

    def cancel(self) -> None:
        with self._lock:
            c = self._cancel
        if c is not None:
            c.cancel()

    def _run(self, request: str, cancel: CancelToken, model: str | None = None,
             unattended: bool = False) -> None:
        # The flag is captured on the worker thread and stamped onto EVERY announcement this draft
        # makes — progress AND both endings. Marking only some of them would be worse than marking
        # none: the overnight pass would narrate its coder steps into a dark house and then fall
        # silent at the end, or whisper through the night and shout "Couldn't draft that change" at
        # 4 AM. Whoever hears one of these events hears all of them.
        def on_progress(line: str) -> None:
            self._bus.publish(SelfChangeProgress(line, unattended=unattended))

        try:
            change = self._selfdev.propose(
                request, on_progress=on_progress, cancel=cancel, model=model
            )
            self._bus.publish(SelfChangeFinished(ok=True, summary=change.summary,
                                                 branch=change.branch, unattended=unattended))
        except Exception as exc:  # noqa: BLE001 - surface any drafting failure as an announcement
            stopped = cancel.is_set()
            _LOG.info("self-change draft %s: %s", "stopped" if stopped else "failed", exc)
            self._bus.publish(SelfChangeFinished(ok=False, error=str(exc), stopped=stopped,
                                                 unattended=unattended))
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
