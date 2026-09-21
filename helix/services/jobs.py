"""CURRENT TASKS - the one register every long-running thing in HELIX reports into.

An upload, a fleet read, a deploy, a rollback, a secrets scan, a merge check, a bucket fetch: each
is a Job here, with a title in plain words, a state, a progress fraction when one is knowable, the
lines it printed, and - when it is safe - a way to cancel it. The page shows the register as the
CURRENT TASKS section and the dock at the bottom of the window; nothing runs in the dark.

Rules of the register:
  * Cancel is COOPERATIVE and declared per job. A job that cannot be cancelled says so (`cancel`
    is None); one that can carries a sentence saying what cancelling leaves behind, shown before
    the button is pressed. Cancelling never raises, and a cancelled job is never "done".
  * The register is kept on disk (jobs.json) so what finished yesterday is still there - as
    finished - after a restart, with a one-click "clear finished". A job that was RUNNING when
    HELIX stopped comes back as failed with the sentence "HELIX restarted while this was running",
    never as still running: nothing pretends to be alive.
  * Every change is pushed to the page (`{"t":"job", ...}` / `{"t":"job_line", ...}`); progress
    pushes are throttled so a fast upload does not flood the socket.
  * Lines are ASCII-cleaned (rule 7) and capped per job.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Push = Callable[[dict], None]

RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"
LOST = "HELIX restarted while this was running, so it did not finish."
NO_SUCH_JOB = "No task with that id."
CANNOT_CANCEL = "This task cannot be cancelled once it has started."
ALREADY_OVER = "That task is already over."
STILL_RUNNING = "That task is still running - cancel it first."

KEEP_JOBS = 200
KEEP_LINES = 600


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    id: str
    kind: str                    # upload | fetch | read | deploy | rollback | scan | merge | cache | create
    title: str                   # plain words: "Ship ECHO dev", "Upload Midnight Drive.mp4"
    app: str = ""
    env: str = ""
    by: str = ""
    state: str = RUNNING
    progress: float | None = None   # 0..1 when knowable, None = a wave (unknown length)
    note: str = ""                  # the current step, plain words
    started: str = field(default_factory=utcnow)
    finished: str | None = None
    rc: int | None = None
    cancel_note: str = ""           # what cancelling leaves behind; empty = not cancellable
    quiet: bool = False             # small automatic work (a prefetch): the dock shows it, the section does not
    lines: list[str] = field(default_factory=list)
    cancel_hook: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    cancel_asked: bool = field(default=False, compare=False)

    @property
    def over(self) -> bool:
        return self.state != RUNNING

    @property
    def can_cancel(self) -> bool:
        return self.state == RUNNING and bool(self.cancel_note) and not self.cancel_asked

    def as_dict(self, *, with_lines: bool = False) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title, "app": self.app, "env": self.env, "by": self.by,
            "state": self.state, "progress": self.progress, "note": self.note, "started": self.started,
            "finished": self.finished, "rc": self.rc, "cancel_note": self.cancel_note,
            "can_cancel": self.can_cancel, "cancel_asked": self.cancel_asked, "quiet": self.quiet,
            "lines_n": len(self.lines),
        }
        if with_lines:
            d["lines"] = list(self.lines)
        return d


def _ascii(text: str) -> str:
    return str(text).encode("ascii", "replace").decode("ascii")


class Jobs:
    """The register. Thread-safe; every mutation pushes to the page and (state changes) to disk."""

    def __init__(self, path: Path | None, push: Push | None = None, *, clock=None) -> None:
        self._path = path
        self._push = push or (lambda ev: None)
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._last_push: dict[str, float] = {}
        self._load()

    # ---------------------------------------------------------------- disk
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            rows = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(rows, list):
            return
        for r in rows[-KEEP_JOBS:]:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            job = Job(id=str(r["id"]), kind=str(r.get("kind") or "task"), title=str(r.get("title") or "?"),
                      app=str(r.get("app") or ""), env=str(r.get("env") or ""), by=str(r.get("by") or ""),
                      state=str(r.get("state") or FAILED), progress=r.get("progress"), note=str(r.get("note") or ""),
                      started=str(r.get("started") or utcnow()), finished=r.get("finished"), rc=r.get("rc"),
                      cancel_note="", quiet=bool(r.get("quiet")), lines=[str(x) for x in (r.get("lines") or [])][-KEEP_LINES:])
            if job.state == RUNNING:          # nothing pretends to be alive across a restart
                job.state, job.note, job.finished = FAILED, LOST, job.finished or utcnow()
                job.lines.append("[helix] " + LOST)
            self._jobs[job.id] = job
            self._order.append(job.id)

    def _save(self) -> None:
        if self._path is None:
            return
        with self._lock:
            rows = [self._jobs[i].as_dict(with_lines=True) for i in self._order[-KEEP_JOBS:] if i in self._jobs]
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    # ---------------------------------------------------------------- reads
    def list(self) -> list[dict]:
        with self._lock:
            return [self._jobs[i].as_dict() for i in reversed(self._order) if i in self._jobs]

    def get(self, jid: str) -> Job | None:
        with self._lock:
            return self._jobs.get(jid)

    def running(self, kind: str | None = None) -> list[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if j.state == RUNNING and (kind is None or j.kind == kind)]

    def log(self, jid: str) -> dict | None:
        job = self.get(jid)
        return job.as_dict(with_lines=True) if job else None

    # ---------------------------------------------------------------- the life of a job
    def start(self, kind: str, title: str, *, app: str = "", env: str = "", by: str = "",
              progress: float | None = None, note: str = "", cancel: Callable[[], None] | None = None,
              cancel_note: str = "", quiet: bool = False) -> Job:
        jid = f"{int(time.time() * 1000):x}"
        with self._lock:
            while jid in self._jobs:
                jid += "x"
            job = Job(id=jid, kind=kind, title=_ascii(title)[:120], app=app, env=env, by=by, progress=progress,
                      note=_ascii(note)[:160], cancel_hook=cancel, cancel_note=_ascii(cancel_note) if cancel else "",
                      quiet=quiet)
            self._jobs[jid] = job
            self._order.append(jid)
            if len(self._order) > KEEP_JOBS:
                for old in self._order[:-KEEP_JOBS]:
                    if self._jobs.get(old) and self._jobs[old].over:
                        self._jobs.pop(old, None)
                self._order = [i for i in self._order if i in self._jobs]
        self._emit(job, force=True)
        self._save()
        return job

    def line(self, job: Job, text: str) -> None:
        text = _ascii(text)
        with self._lock:
            job.lines.append(text)
            if len(job.lines) > KEEP_LINES:
                del job.lines[: len(job.lines) - KEEP_LINES]
        self._push({"t": "job_line", "id": job.id, "line": text})

    def progress(self, job: Job, frac: float | None, note: str | None = None) -> None:
        with self._lock:
            if job.over:
                return
            job.progress = None if frac is None else max(0.0, min(1.0, float(frac)))
            if note is not None:
                job.note = _ascii(note)[:160]
        self._emit(job)

    def finish(self, job: Job, ok: bool, *, rc: int | None = None, note: str | None = None) -> None:
        with self._lock:
            if job.over:
                return
            if job.cancel_asked and not ok:
                job.state = CANCELLED
            else:
                job.state = DONE if ok else FAILED
            job.rc = rc
            job.finished = utcnow()
            if note is not None:
                job.note = _ascii(note)[:160]
            if ok and job.progress is not None:
                job.progress = 1.0
            job.cancel_hook = None
        self._emit(job, force=True)
        self._save()

    def cancel(self, jid: str) -> str | None:
        """Ask a running job to stop. Returns a sentence when it cannot; None when the ask went out.
        The job itself decides when it is over (it finishes as cancelled when its runner notices)."""
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return NO_SUCH_JOB
            if job.over:
                return ALREADY_OVER
            if not job.cancel_note:
                return CANNOT_CANCEL
            if job.cancel_asked:
                return None
            job.cancel_asked = True
            job.note = "Stopping..."
            hook = job.cancel_hook
        self.line(job, "[helix] cancel asked by the person")
        self._emit(job, force=True)
        if hook is not None:
            try:
                hook()
            except Exception as e:  # noqa: BLE001 - a cancel must never raise into the route
                self.line(job, f"[helix] the stop hook failed: {e}")
        return None

    def cancelled(self, job: Job) -> bool:
        """For cooperative runners: has a cancel been asked for this job?"""
        return bool(job.cancel_asked)

    def dismiss(self, jid: str) -> str | None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return NO_SUCH_JOB
            if not job.over:
                return STILL_RUNNING
            self._jobs.pop(jid, None)
            self._order = [i for i in self._order if i != jid]
        self._push({"t": "job_gone", "id": jid})
        self._save()
        return None

    def clear_finished(self) -> int:
        with self._lock:
            gone = [i for i in self._order if i in self._jobs and self._jobs[i].over]
            for i in gone:
                self._jobs.pop(i, None)
            self._order = [i for i in self._order if i in self._jobs]
        for i in gone:
            self._push({"t": "job_gone", "id": i})
        self._save()
        return len(gone)

    # ---------------------------------------------------------------- helpers for runners
    def run(self, job: Job, fn: Callable[[Job], bool], *, name: str = "helix-job") -> threading.Thread:
        """Run fn(job) on a thread; True = done, False = failed; an exception = failed with its words."""
        def body():
            try:
                ok = bool(fn(job))
            except Exception as e:  # noqa: BLE001
                self.line(job, f"[helix] {type(e).__name__}: {e}")
                ok = False
            self.finish(job, ok)
        t = threading.Thread(target=body, daemon=True, name=name)
        t.start()
        return t

    # ---------------------------------------------------------------- push
    def _emit(self, job: Job, *, force: bool = False) -> None:
        now = self._clock()
        with self._lock:
            last = self._last_push.get(job.id, -1.0)
            if not force and now - last < 0.15:
                return
            self._last_push[job.id] = now
            payload = job.as_dict()
        self._push({"t": "job", "job": payload})
