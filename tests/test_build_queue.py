"""BuildQueue — background job runner: runs builds (now a small POOL, so distinct builds run in
parallel), serializes SAME-NAME builds, announces finish/failure, reorders the queue, and cancels. Uses a
gated fake forge so ordering is deterministic, not timed."""
from __future__ import annotations

import threading
import time

from helix.domain.errors import BuildCancelled
from helix.domain.events import BuildFinished, BuildProgress
from helix.domain.models import BuildKind
from helix.services.build_queue import BuildQueue


class _App:
    def __init__(self, name: str) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")


class _Bus:
    def __init__(self) -> None:
        self.events: list = []
        self._finishes = 0
        self._cond = threading.Condition()

    def publish(self, event) -> None:
        with self._cond:
            self.events.append(event)
            if isinstance(event, BuildFinished):
                self._finishes += 1
                self._cond.notify_all()

    def subscribe(self, *_a) -> None:  # BuildQueue only publishes
        pass

    def wait_finishes(self, n: int, timeout: float = 5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._finishes >= n, timeout=timeout)

    def finished(self) -> list:
        return [e for e in self.events if isinstance(e, BuildFinished)]


class _Forge:
    """A forge whose build() blocks on a per-name gate, so we can hold a build 'running' deterministically."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._calls_lock = threading.Lock()
        self.started: dict[str, threading.Event] = {}
        self.gate: dict[str, threading.Event] = {}
        self.fail: set[str] = set()

    def _ev(self, d, name):
        return d.setdefault(name, threading.Event())

    def build(self, name, request, *, prompt=None, kind=None, on_progress=None, cancel=None):
        with self._calls_lock:
            self.calls.append(name)
        self._ev(self.started, name).set()
        if on_progress:
            on_progress(f"working on {name}")
        self._ev(self.gate, name).wait(5)  # released by the test
        if cancel is not None and cancel.is_set():
            raise BuildCancelled(name.lower(), name, False)
        if name in self.fail:
            raise RuntimeError("boom")
        return _App(name)

    def release(self, name):
        self._ev(self.gate, name).set()

    def wait_started(self, name, timeout=5.0):
        return self._ev(self.started, name).wait(timeout)


def _alive(q: BuildQueue) -> bool:
    return any(t.is_alive() for t in q._threads)


def test_runs_a_build_and_announces_finished():
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus)
    assert q.enqueue("Tip Calc", "x", kind=BuildKind.APP) == 0  # nothing ahead → starts now
    assert forge.wait_started("Tip Calc")
    forge.release("Tip Calc")
    assert bus.wait_finishes(1)
    fin = bus.finished()[0]
    assert fin.ok and fin.name == "Tip Calc" and fin.slug == "tip-calc"
    assert any(isinstance(e, BuildProgress) for e in bus.events)  # progress was published


def test_two_distinct_builds_run_concurrently():
    # The headline of the parallel pool: two different builds run at the SAME time, not one-after-another.
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus, max_workers=2)
    q.enqueue("A", "x", kind=BuildKind.APP)
    q.enqueue("B", "x", kind=BuildKind.APP)
    assert forge.wait_started("A")
    assert forge.wait_started("B")  # BOTH running before either gate opens → genuinely parallel
    forge.release("A")
    forge.release("B")
    assert bus.wait_finishes(2)
    assert set(forge.calls) == {"A", "B"}


def test_same_name_builds_never_run_at_once():
    # Two edits of the SAME build must serialize, or two coders would clobber one workspace.
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus, max_workers=2)
    q.enqueue("X", "first", kind=BuildKind.APP)
    q.enqueue("X", "second", kind=BuildKind.APP)
    assert forge.wait_started("X")
    time.sleep(0.15)  # give a (wrongly) concurrent second run time to enter build()
    assert forge.calls == ["X"]  # only ONE X is inside build(); the second waits its turn
    forge.release("X")
    assert bus.wait_finishes(2)
    assert forge.calls == ["X", "X"]  # the second ran only after the first finished


def test_second_build_queues_behind_the_active_one_then_runs():
    # With a single worker, builds serialize: B waits for A. (max_workers=1 makes the queue order observable.)
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus, max_workers=1)
    q.enqueue("A", "x", kind=BuildKind.APP)
    assert forge.wait_started("A")              # A is running, gate still closed
    assert q.enqueue("B", "x", kind=BuildKind.APP) == 1  # one ahead of it (A)
    assert q.snapshot() == (["A"], ["B"])
    forge.release("A")
    assert forge.wait_started("B")              # B starts only after A finished
    forge.release("B")
    assert bus.wait_finishes(2)
    assert forge.calls == ["A", "B"]            # strictly serialized with one worker


def test_reorder_and_cancel_pending_while_one_runs():
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus, max_workers=1)
    q.enqueue("A", "x", kind=BuildKind.APP)
    assert forge.wait_started("A")
    q.enqueue("B", "x", kind=BuildKind.APP)
    q.enqueue("C", "x", kind=BuildKind.APP)
    assert q.snapshot() == (["A"], ["B", "C"])
    assert q.move_first("C") and q.snapshot() == (["A"], ["C", "B"])  # C jumps ahead of B
    assert q.cancel_queued("B") and q.snapshot() == (["A"], ["C"])    # B dropped
    assert not q.move_first("A")  # can't reorder the running one
    forge.release("A")
    forge.release("C")
    assert bus.wait_finishes(2)
    assert "B" not in forge.calls  # the cancelled-queued job never ran


def test_failed_build_announces_failure():
    bus, forge = _Bus(), _Forge()
    forge.fail.add("Bad")
    q = BuildQueue(forge, bus)
    q.enqueue("Bad", "x", kind=BuildKind.APP)
    assert forge.wait_started("Bad")
    forge.release("Bad")
    assert bus.wait_finishes(1)
    fin = bus.finished()[0]
    assert not fin.ok and fin.error and not fin.stopped


def test_cancel_active_stops_and_offers_cleanup():
    bus, forge = _Bus(), _Forge()
    q = BuildQueue(forge, bus)
    q.enqueue("A", "x", kind=BuildKind.APP)
    assert forge.wait_started("A")
    assert q.cancel_active() == ["A"]  # fire the active job's token (now a list of cancelled names)
    forge.release("A")                 # build wakes, sees cancel set, raises BuildCancelled
    assert bus.wait_finishes(1)
    fin = bus.finished()[0]
    assert not fin.ok and fin.stopped


class _CancelAwareForge:
    """A forge whose build() runs until its cancel token fires — mimics the coder the watcher kills."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.calls: list[str] = []

    def build(self, name, request, *, prompt=None, kind=None, on_progress=None, cancel=None):
        self.calls.append(name)
        self.started.set()
        while not (cancel is not None and cancel.is_set()):
            time.sleep(0.01)
        raise BuildCancelled(name.lower(), name, False)


def test_shutdown_reaps_the_active_build_and_exits_the_workers():
    # The owner's #1 case: closing mid-build must cancel the coder and stop the workers — never orphan one.
    bus, forge = _Bus(), _CancelAwareForge()
    q = BuildQueue(forge, bus)
    q.enqueue("A", "x", kind=BuildKind.APP)
    assert forge.started.wait(5)
    q.shutdown(timeout=3.0)
    assert not _alive(q)                    # the background workers actually exited
    assert q.snapshot() == ([], [])         # nothing left active or queued
    assert bus.finished() == []             # no cleanup announcement during shutdown (the UI is gone)


def test_shutdown_drops_pending_jobs_without_running_them():
    bus, forge = _Bus(), _CancelAwareForge()
    q = BuildQueue(forge, bus, max_workers=1)
    q.enqueue("A", "x", kind=BuildKind.APP)
    assert forge.started.wait(5)
    q.enqueue("B", "x", kind=BuildKind.APP)  # queued behind the running A
    q.shutdown(timeout=3.0)
    assert "B" not in forge.calls            # the queued job is dropped, never built, on shutdown
    assert not _alive(q)
