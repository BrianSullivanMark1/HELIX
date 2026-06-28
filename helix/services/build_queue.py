"""BuildQueue — runs builds as background jobs so the orb keeps talking while it works.

The build tools enqueue a job and return immediately; a small POOL of daemon workers runs them
concurrently (forge.build is called UNCHANGED per job, so the sandbox / Constitution / escape guards are
the same as the old inline path — the guard was widened to skip ALL build workspaces so concurrent
siblings don't flag each other; see ForgeService). Progress and completion are published on the EventBus,
and a snapshot lets the model answer "what are you building?" without touching anything.

Concurrency rules:
  - Up to `max_workers` builds run at once (default 2).
  - Two builds with the SAME name never run at once — a same-name job waits until the first finishes, so
    rapid "update X" requests apply IN ORDER to one workspace instead of two coders clobbering it.
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
    def __init__(self, forge: ForgeService, bus: EventBus, max_workers: int = 2) -> None:
        self._forge = forge
        self._bus = bus
        self._lock = threading.Lock()
        self._pending: deque[BuildJob] = deque()
        self._active: dict[int, BuildJob] = {}  # id -> running job (concurrent)
        self._slots = threading.Semaphore(0)  # counts runnable signals; one per enqueue, plus re-signals
        self._stopping = False  # set by shutdown(): stop promoting jobs and let the workers exit
        self._threads = [
            threading.Thread(target=self._run, daemon=True, name=f"helix-build-{i}")
            for i in range(max(1, max_workers))
        ]
        for t in self._threads:
            t.start()

    # ----- enqueue / status (called from the conversation/tool thread) -----
    def enqueue(self, name: str, request: str, *, kind: BuildKind, prompt: str | None = None) -> int:
        """Add a build. Returns roughly how many builds are ahead of it (0 = it can start now)."""
        job = BuildJob(name=name.strip(), request=request, kind=kind, prompt=prompt)
        with self._lock:
            ahead = len(self._active) + len(self._pending)
            self._pending.append(job)
        self._slots.release()  # signal one unit of work
        return ahead

    def active_name(self) -> str | None:
        """A representative running build's name (or None) — used for the simple 'is anything building?'."""
        with self._lock:
            return next(iter(self._active.values())).name if self._active else None

    def active_names(self) -> list[str]:
        with self._lock:
            return [j.name for j in self._active.values()]

    def is_active_named(self, name: str) -> bool:
        key = name.strip().lower()
        with self._lock:
            return any(j.name.lower() == key for j in self._active.values())

    def snapshot(self) -> tuple[list[str], list[str]]:
        """(names building now, names queued behind them)."""
        with self._lock:
            return ([j.name for j in self._active.values()], [j.name for j in self._pending])

    def status_line(self) -> str:
        active, queued = self.snapshot()
        parts: list[str] = []
        if active:
            parts.append("Building now: " + ", ".join(active) + ".")
        if queued:
            parts.append("Queued: " + ", ".join(queued) + ".")
        return " ".join(parts) or "Nothing building right now."

    # ----- control -----
    def cancel_active(self) -> list[str]:
        """Stop ALL running builds (each cleanup offer fires via its own BuildFinished). Returns their
        names. Tokens are set under the lock so a stop landing in a hand-off gap can't miss a job."""
        with self._lock:
            jobs = list(self._active.values())
            for j in jobs:
                j.cancel.cancel()
        return [j.name for j in jobs]

    def cancel_active_named(self, name: str) -> bool:
        """Stop a SPECIFIC running build by name (the model's cancel_build for the one in progress)."""
        key = name.strip().lower()
        with self._lock:
            hit = [j for j in self._active.values() if j.name.lower() == key]
            for j in hit:
                j.cancel.cancel()
        return bool(hit)

    def shutdown(self, timeout: float = 3.0) -> None:
        """Reap the workers on app close. Closing HELIX mid-build must NOT orphan a coder subprocess — it
        would keep running (and billing) and leave a file-locked, half-written workspace. Cancel every
        active build (each watcher kills its claude.exe child and the Forge reverts any escaped write),
        drop the queue, wake all workers, and wait briefly for them to unwind."""
        self._stopping = True
        self.cancel_active()
        with self._lock:
            self._pending.clear()
        for _ in self._threads:
            self._slots.release()  # wake every worker so it sees _stopping and exits
        for t in self._threads:
            t.join(timeout)

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
        """Bump a PENDING build to the front of the queue. Can't touch one already running."""
        slug = name.strip().lower()
        with self._lock:
            for j in list(self._pending):
                if j.name.lower() == slug:
                    self._pending.remove(j)
                    self._pending.appendleft(j)
                    return True
        return False

    # ----- worker threads -----
    def _next_runnable_locked(self) -> BuildJob | None:
        """Pop the first pending job whose name isn't already building (no same-name concurrency), mark it
        active, and return it. Returns None when every pending job is blocked by a same-name active build.
        Caller must hold the lock."""
        active_names = {j.name.lower() for j in self._active.values()}
        for cand in list(self._pending):
            if cand.name.lower() not in active_names:
                self._pending.remove(cand)
                self._active[cand.id] = cand
                cand.status = "running"
                return cand
        return None

    def _run(self) -> None:
        while True:
            self._slots.acquire()
            if self._stopping:
                return
            with self._lock:
                job = None if not self._pending else self._next_runnable_locked()
            if job is None:
                # Nothing runnable right now (empty, or all pending blocked by a same-name active build).
                # The slot is consumed; a finishing build re-signals when work still remains.
                continue
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
            handle = getattr(job.cancel, "build", None)  # the Forge stamps it with iterating-ness
            self._bus.publish(
                BuildFinished(
                    name=app.name, ok=True, iterating=bool(handle and handle.iterating),
                    slug=getattr(app, "slug", ""),
                )
            )
        except BuildCancelled:
            job.status = "stopped"
            # job.cancel.build is the BuildHandle the Forge stamped on the token before running. On app
            # shutdown the UI is gone, so skip the cleanup announcement (nothing can answer it).
            if not self._stopping:
                handle = getattr(job.cancel, "build", None)
                self._bus.publish(
                    BuildFinished(
                        name=job.name, ok=False, stopped=True, handle=handle,
                        slug=getattr(handle, "slug", ""),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - surface any build failure as an announcement
            _LOG.warning("build job failed: %s", exc)
            job.status = "failed"
            handle = getattr(job.cancel, "build", None)
            self._bus.publish(
                BuildFinished(name=job.name, ok=False, error=str(exc), slug=getattr(handle, "slug", ""))
            )
        finally:
            with self._lock:
                self._active.pop(job.id, None)
                pending = len(self._pending)
            # Re-signal if work remains — covers a job that was blocked by THIS build's name and is now
            # runnable, and keeps a backlog draining as slots free up.
            if pending and not self._stopping:
                self._slots.release()
