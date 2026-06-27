"""BuildQueue — runs builds as background jobs so the orb keeps talking while it works.

The build tools enqueue a job and return immediately; a single daemon worker runs them one at a time
(forge.build is called UNCHANGED, so the sandbox / Constitution / escape-revert guards are byte-for-byte
the same as the old inline path). Progress and completion are published on the EventBus, and a snapshot
lets the model answer "what are you building?" without touching anything.

Strictly single-worker: never two builds at once — that matches today's reality and keeps forge's
whole-tree snapshot guard correct.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from itertools import count

from helix.domain.errors import BuildCancelled
from helix.domain.events import BuildFinished, BuildProgress
from helix.domain.models import BuildKind
from helix.logging_setup import get_logger
from helix.ports.events import EventBus
from helix.services.cancel import CancelToken
from helix.services.forge import ForgeService

_LOG = get_logger("build_queue")
_ids = count(1)


@dataclass
class BuildJob:
    name: str
    request: str
    kind: BuildKind
    prompt: str | None = None
    id: int = field(default_factory=lambda: next(_ids))
    cancel: CancelToken = field(default_factory=CancelToken)
    status: str = "queued"  # queued | running | done | failed | stopped


class BuildQueue:
    def __init__(self, forge: ForgeService, bus: EventBus) -> None:
        self._forge = forge
        self._bus = bus
        self._lock = threading.Lock()
        self._pending: deque[BuildJob] = deque()
        self._active: BuildJob | None = None
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="helix-build-queue")
        self._thread.start()

    # ----- enqueue / status (called from the conversation/tool thread) -----
    def enqueue(self, name: str, request: str, *, kind: BuildKind, prompt: str | None = None) -> int:
        """Add a build. Returns its position: 0 = starts now, N = N builds ahead of it."""
        job = BuildJob(name=name.strip(), request=request, kind=kind, prompt=prompt)
        with self._lock:
            ahead = (1 if self._active is not None else 0) + len(self._pending)
            self._pending.append(job)
        self._wake.set()
        return ahead

    def active_name(self) -> str | None:
        with self._lock:
            return self._active.name if self._active else None

    def snapshot(self) -> tuple[str | None, list[str]]:
        with self._lock:
            return (self._active.name if self._active else None, [j.name for j in self._pending])

    def status_line(self) -> str:
        active, queued = self.snapshot()
        parts: list[str] = []
        if active:
            parts.append(f"Building now: {active}.")
        if queued:
            parts.append("Queued: " + ", ".join(queued) + ".")
        return " ".join(parts) or "Nothing building right now."

    # ----- control -----
    def cancel_active(self) -> str | None:
        """Stop the running build (its cleanup offer fires via BuildFinished). Returns its name, if any."""
        with self._lock:
            job = self._active
        if job is not None:
            job.cancel.cancel()
            return job.name
        return None

    def cancel_queued(self, name: str) -> bool:
        slug = name.strip().lower()
        with self._lock:
            for j in list(self._pending):
                if j.name.lower() == slug:
                    self._pending.remove(j)
                    return True
        return False

    def clear_queued(self) -> list[str]:
        with self._lock:
            names = [j.name for j in self._pending]
            self._pending.clear()
        return names

    def move_first(self, name: str) -> bool:
        """Bump a PENDING build to the front of the queue. Can't touch the one already running."""
        slug = name.strip().lower()
        with self._lock:
            for j in list(self._pending):
                if j.name.lower() == slug:
                    self._pending.remove(j)
                    self._pending.appendleft(j)
                    return True
        return False

    # ----- worker thread -----
    def _run(self) -> None:
        while True:
            self._wake.wait()
            while True:
                with self._lock:
                    if not self._pending:
                        self._active = None
                        self._wake.clear()
                        break
                    job = self._pending.popleft()
                    self._active = job
                    job.status = "running"
                self._build(job)

    def _build(self, job: BuildJob) -> None:
        def on_progress(line: str) -> None:
            self._bus.publish(BuildProgress(job.name, line))

        try:
            app = self._forge.build(
                job.name, job.request, prompt=job.prompt, kind=job.kind,
                on_progress=on_progress, cancel=job.cancel,
            )
            job.status = "done"
            self._bus.publish(BuildFinished(name=app.name, ok=True))
        except BuildCancelled:
            job.status = "stopped"
            # job.cancel.build is the BuildHandle the Forge stamped on the token before running.
            self._bus.publish(
                BuildFinished(name=job.name, ok=False, stopped=True, handle=getattr(job.cancel, "build", None))
            )
        except Exception as exc:  # noqa: BLE001 - surface any build failure as an announcement
            _LOG.warning("build job failed: %s", exc)
            job.status = "failed"
            self._bus.publish(BuildFinished(name=job.name, ok=False, error=str(exc)))
        finally:
            with self._lock:
                if self._active is job:
                    self._active = None
