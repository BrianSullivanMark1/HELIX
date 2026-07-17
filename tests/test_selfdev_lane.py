"""SelfDevLane — drafts a self-change in the background, announces it, runs one at a time, cancels."""
from __future__ import annotations

import threading
import time

from helix.domain.events import SelfChangeFinished, SelfChangeProgress
from helix.domain.models import PendingChange
from helix.services.selfdev_lane import SelfDevLane


class _Bus:
    def __init__(self) -> None:
        self.events: list = []
        self._cond = threading.Condition()

    def publish(self, e) -> None:
        with self._cond:
            self.events.append(e)
            self._cond.notify_all()

    def subscribe(self, *a) -> None:
        pass

    def wait_for(self, pred, timeout=5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(pred, timeout=timeout)

    def finished(self):
        return [e for e in self.events if isinstance(e, SelfChangeFinished)]


class _Selfdev:
    def __init__(self, fn) -> None:
        self._fn = fn

    def propose(self, request, *, on_progress=None, cancel=None, model=None):
        self.model = model  # the lane threads the chosen coder model through to propose
        return self._fn(request, on_progress, cancel)


def test_lane_runs_propose_in_background_and_announces_ready():
    bus = _Bus()

    def fn(req, on_progress, cancel):
        if on_progress:
            on_progress("drafting it")
        return PendingChange(id="selfdev/x", branch="selfdev/x", summary="did x", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    assert lane.start("change it")
    assert bus.wait_for(lambda: bool(bus.finished()))
    fin = bus.finished()[0]
    assert fin.ok and fin.summary == "did x"
    assert any(isinstance(e, SelfChangeProgress) for e in bus.events)


def test_lane_runs_one_draft_at_a_time():
    bus = _Bus()
    started, release = threading.Event(), threading.Event()

    def fn(req, on_progress, cancel):
        started.set()
        release.wait(5)
        return PendingChange(id="b", branch="b", summary="", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    assert lane.start("a")
    assert started.wait(5) and lane.busy()
    assert lane.start("b") is False  # rejected while one is in flight
    release.set()
    assert bus.wait_for(lambda: bool(bus.finished()))


def test_lane_cancel_reports_stopped():
    bus = _Bus()

    def fn(req, on_progress, cancel):
        for _ in range(500):
            if cancel.is_set():
                raise RuntimeError("cancelled")
            time.sleep(0.01)
        return PendingChange(id="b", branch="b", summary="", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    lane.start("a")
    for _ in range(200):
        if lane.busy():
            break
        time.sleep(0.01)
    lane.cancel()
    assert bus.wait_for(lambda: bool(bus.finished()))
    fin = bus.finished()[0]
    assert not fin.ok and fin.stopped
