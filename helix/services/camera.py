"""The camera hand-off — how a tool call on a worker thread borrows the user's webcam.

The model calls view_camera; ToolRegistry.dispatch (worker thread) publishes a CameraRequested
event carrying ONE CameraRequest and parks on it. The UI thread claims the request, opens the live
preview window (ui/camera_view.py), the user presents the object, and the window fulfils the
request with encoded picture bytes — or fails it with a plain reason. The event stays a frozen
fact; every bit of mutability lives in this holder. Pure stdlib, so the whole contract is testable
without Qt or hardware.
"""
from __future__ import annotations

import threading

# How long dispatch waits for a UI to CLAIM the request. No claim means no camera window exists on
# the other side of the bus (headless, tests, torn-down UI) — give up fast, not after the full wait.
CLAIM_TIMEOUT_S = 2.0

# The hard ceiling on the worker's park, belt-and-braces — NOT a countdown the user races. The
# window waits for the user's word ("take the picture", or the button) with no time pressure; this
# only elapses when a window is left open and forgotten (HELIX then says no picture came), and it
# stays under the subscription turn budget (600s) so a forgotten window can never hang the brain.
CAPTURE_TIMEOUT_S = 300.0

_POLL_S = 0.1  # wait-loop granularity: how often the parked worker rechecks its cancel token


class CameraRequest:
    """One camera look: created by the tool, claimed and settled by the UI, awaited by the worker.

    Thread contract: fulfil/fail/claim run on the UI thread, wait() on the tool's worker thread,
    abandon() on whichever side gives up first. First settle wins; everything after is a no-op —
    a frame arriving just after a cancel is dropped, never resurrected.
    """

    def __init__(self, prompt: str = "") -> None:
        self.prompt = prompt  # one short line the window shows ("Hold the label up close")
        self.error = ""       # the plain-words reason when no picture came back
        self._data: bytes | None = None
        self._lock = threading.Lock()
        self._claimed = threading.Event()
        self._done = threading.Event()
        self._abandoned = False

    def claim(self) -> bool:
        """The UI announces it is handling this request. False = the worker already gave up
        (cancelled/timed out) and no window should open."""
        with self._lock:
            if self._abandoned:
                return False
            self._claimed.set()
            return True

    def fulfil(self, data: bytes) -> None:
        """Hand back the captured picture (encoded image bytes, e.g. PNG). First settle wins."""
        with self._lock:
            if self._abandoned or self._done.is_set():
                return
            self._data = data
            self._done.set()

    def fail(self, reason: str) -> None:
        """Settle without a picture — the reason is what the model relays ('window closed')."""
        with self._lock:
            if self._abandoned or self._done.is_set():
                return
            self.error = reason
            self._done.set()

    def abandon(self) -> None:
        """The waiting side gave up (cancel/timeout). Late fulfil/fail calls become no-ops, and the
        window notices via `abandoned` on its next tick and closes itself."""
        with self._lock:
            self._abandoned = True

    @property
    def abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    def wait(self, *, cancel=None, claim_timeout: float | None = None,
             timeout: float | None = None) -> bytes | None:
        """Park the worker until the UI settles the request. Returns the picture bytes, or None with
        `error` set. Breaks early — abandoning the request — when the turn's CancelToken fires, when
        no UI claims it, or when the capture window runs out."""
        claim_s = CLAIM_TIMEOUT_S if claim_timeout is None else claim_timeout
        total_s = CAPTURE_TIMEOUT_S if timeout is None else timeout

        def claimed_or_settled() -> bool:  # an unclaimed fail() (e.g. no Qt camera) still counts
            return self._claimed.is_set() or self._done.is_set()

        if not self._poll(claimed_or_settled, claim_s, cancel):
            self.abandon()
            if not self.error:
                self.error = "There's no camera window available right now."
            return None
        if not self._poll(self._done.is_set, total_s, cancel):
            self.abandon()
            if not self.error:
                self.error = "No picture was taken — the camera window was left open too long."
            return None
        return self._data

    def _poll(self, ready, limit_s: float, cancel) -> bool:
        """Wait until ready() up to limit_s, rechecking the cancel token; False = gave up. Paced on
        the done event so a settle wakes the worker instantly instead of on the next poll tick."""
        waited = 0.0
        while True:
            if ready():
                return True
            if cancel is not None and cancel.is_set():
                if not self.error:
                    self.error = "Stopped before a picture was taken."
                return False
            if waited >= limit_s:
                return False
            step = min(_POLL_S, limit_s - waited)
            self._done.wait(step)
            waited += step
