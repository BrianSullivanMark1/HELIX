"""The camera hand-off — how a tool call on a worker thread borrows the user's webcam.

The model calls view_camera; ToolRegistry.dispatch (worker thread) publishes a CameraRequested
event carrying ONE CameraRequest and parks on it. The UI thread claims the request, raises (or
reuses) the live camera panel, the user presents the object — or the panel simply grabs what it
sees — and the panel fulfils the request with encoded picture bytes (one frame, or a short clip's
worth), or fails it with a plain reason. The event stays a frozen fact; every bit of mutability
lives in this holder. Pure stdlib, so the whole contract is testable without a browser or hardware.

A second, smaller holder (CameraCommand) carries the AR commands the model can send to the live
panel — draw callouts over the view, project a hologram, open/close the panel — and brings back one
line saying what happened, so the model never has to guess whether a panel was there to hear it.
One command, "measure", is different: the panel PARKS it in Measure mode until the user sends a
measurement (or cancels), so its wait is long and cancel-aware like a look's.

Also here, pure and stdlib: the plain 'Measured: …' line the shell hands the model (measure_line)
and the reader for the layout file beside a baked mesh (read_layout) that the panel's component
ghosts are drawn from — READ_ME/MAKER_FLOW.md §5 and §6.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

# How long dispatch waits for a UI to CLAIM the request. No claim means no camera window exists on
# the other side of the bus (headless, tests, torn-down UI) — give up fast, not after the full wait.
CLAIM_TIMEOUT_S = 2.0

# The hard ceiling on the worker's park, belt-and-braces — NOT a countdown the user races. The
# window waits for the user's word ("take the picture", or the button) with no time pressure; this
# only elapses when a window is left open and forgotten (HELIX then says no picture came), and it
# stays under the subscription turn budget (600s) so a forgotten window can never hang the brain.
CAPTURE_TIMEOUT_S = 300.0

# A clip is a handful of frames sampled over a few seconds — bounded so a "record a clip" can never
# flood a turn (the image loader caps at 10 per message anyway; 8 leaves room for the grid legend).
MAX_FRAMES = 8
MAX_CLIP_SECONDS = 15.0

# How long an AR command waits for the panel's one-line answer. The shell settles it synchronously
# on the bus thread when a panel exists, so this only elapses when nothing is listening at all.
COMMAND_TIMEOUT_S = 2.0

# The one command the panel parks instead of answering at once: "measure" waits for the user to
# calibrate and drag the ruler across the part, then press Send. Same ceiling as a look — under the
# subscription turn budget (600 s), so a panel left open can never hang the brain.
MEASURE_TIMEOUT_S = 300.0

_POLL_S = 0.1  # wait-loop granularity: how often the parked worker rechecks its cancel token

# The ruler's calibration references the panel offers (mm). Kept here so the shell's line and the
# panel agree on the words; the numbers are the panel's, listed for the record (ISO/IEC 7810 ID-1
# for the card; the HELIX AR marker's outer square; US Mint; IEC 60086 AA length).
CALIBRATION_REFERENCES = {
    "card long edge": 85.60,
    "card short edge": 53.98,
    "HELIX marker": 80.0,
    "US quarter": 24.26,
    "AA cell length": 50.5,
}
_MAX_MEASURE_ITEMS = 40
_MAX_LABEL = 40
_MAX_REFERENCE = 60


def _finite(value, *, positive: bool = True) -> float | None:
    """A number the ruler could have produced, or None (NaN, inf, text, and non-positives drop)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if positive and f <= 0:
        return None
    return f


def measure_line(payload: dict) -> str:
    """The plain line the model reads for a batch of ruler measurements — MAKER_FLOW §5:
    `Measured: XIAO 21.1 × 17.6 mm; hole pitch 15.2 mm (0.19 mm/px, card long edge)`.
    Boxes read `label w × h mm`, distances `label d mm`, one decimal each; the scale reads to three
    significant figures so a close-up (0.0523 mm/px) is not rounded into nothing. Items the panel
    could not have produced (no numbers, unknown kinds) are dropped; nothing measurable → ''."""
    if not isinstance(payload, dict):
        return ""
    bits: list[str] = []
    for n, raw in enumerate(list(payload.get("items") or [])[:_MAX_MEASURE_ITEMS], start=1):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        label = " ".join(str(raw.get("label") or "").split())[:_MAX_LABEL] or f"part {n}"
        if kind == "box":
            w, h = _finite(raw.get("w_mm")), _finite(raw.get("h_mm"))
            if w is None or h is None:
                continue
            bits.append(f"{label} {w:.1f} × {h:.1f} mm")
        elif kind == "distance":
            d = _finite(raw.get("mm"))
            if d is None:
                continue
            bits.append(f"{label} {d:.1f} mm")
    if not bits:
        return ""
    scale = _finite(payload.get("mm_per_px"))
    reference = " ".join(str(payload.get("reference") or "").split())[:_MAX_REFERENCE]
    tail = []
    if scale is not None:
        tail.append(f"{scale:.3g} mm/px")
    if reference:
        tail.append(reference)
    line = "Measured: " + "; ".join(bits)
    return f"{line} ({', '.join(tail)})" if tail else line


def read_layout(workspace) -> dict | None:
    """The component layout beside a baked mesh — `<workspace>/assets/layout.json` (MAKER_FLOW §6),
    written by design_enclosure. None when there is no file, it isn't JSON, or it isn't the
    dict-with-a-components-list shape the panel's ghosts are drawn from; never raises."""
    try:
        path = Path(workspace) / "assets" / "layout.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        return None
    return data


def clamp_frames(n) -> int:
    """1..MAX_FRAMES — a still is one frame; anything the model over-asks for is capped, never refused."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_FRAMES, n))


def clamp_seconds(s, frames: int) -> float:
    """The clip's span. A still has no span; a clip defaults to half a second a frame, capped."""
    if frames <= 1:
        return 0.0
    try:
        s = float(s)
    except (TypeError, ValueError):
        s = 0.0
    if s <= 0:
        s = frames * 0.5
    return max(0.5, min(MAX_CLIP_SECONDS, s))


class CameraRequest:
    """One camera look: created by the tool, claimed and settled by the UI, awaited by the worker.

    `hold` = wait for the user's word (they were asked to show/turn/hold something); otherwise a
    live panel grabs what it sees at once. `frames` > 1 asks for a clip: that many frames sampled
    evenly over `seconds`. `grid` asks for a labelled reference grid on the picture(s) the model
    receives, so it can place AR callouts precisely.

    Thread contract: fulfil/fail/claim run on the UI thread, wait() on the tool's worker thread,
    abandon() on whichever side gives up first. First settle wins; everything after is a no-op —
    a frame arriving just after a cancel is dropped, never resurrected.
    """

    def __init__(self, prompt: str = "", *, hold: bool | None = None, frames: int = 1,
                 seconds: float = 0.0, grid: bool = False) -> None:
        self.prompt = prompt  # one short line the panel shows ("Hold the label up close")
        self.frames = clamp_frames(frames)
        self.seconds = clamp_seconds(seconds, self.frames)
        self.grid = bool(grid)
        # Default: a prompt means the user was asked to present something → wait for their word.
        self.hold = bool(prompt) if hold is None else bool(hold)
        self.error = ""       # the plain-words reason when no picture came back
        self._data: list[bytes] = []
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
        self.fulfil_frames([data])

    def fulfil_frames(self, frames) -> None:
        """Hand back a clip: encoded frames in time order (a single-frame list is a still)."""
        frames = [f for f in (frames or []) if f]
        with self._lock:
            if self._abandoned or self._done.is_set():
                return
            if not frames:
                self.error = "The camera handed back no frames."
            self._data = frames[:MAX_FRAMES]
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

    @property
    def settled(self) -> bool:
        return self._done.is_set()

    @property
    def frames_data(self) -> tuple[bytes, ...]:
        """Every frame that came back, in order (empty until fulfilled)."""
        with self._lock:
            return tuple(self._data)

    def wait(self, *, cancel=None, claim_timeout: float | None = None,
             timeout: float | None = None) -> bytes | None:
        """Park the worker until the UI settles the request. Returns the (first) picture's bytes,
        or None with `error` set; a clip's remaining frames are on `frames_data`. Breaks early —
        abandoning the request — when the turn's CancelToken fires, when no UI claims it, or when
        the capture window runs out."""
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
        with self._lock:
            return self._data[0] if self._data else None

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


class CameraCommand:
    """An AR command to the live camera panel (draw callouts, project a hologram, open/close),
    answered with ONE plain line. The shell settles it on the bus thread; the tool's worker waits
    briefly and relays the line — or says nothing was listening.

    A "measure" command is parked on the panel instead: the worker waits up to MEASURE_TIMEOUT_S
    (cancel-aware) for the user's ruler. When the worker gives up first it ABANDONS the command, so
    a measurement sent afterwards is not settled into a void — the shell sees `abandoned` and hands
    the line to HELIX as a turn instead."""

    def __init__(self, command: str, payload: dict | None = None) -> None:
        self.command = command
        self.payload = dict(payload or {})
        self.reply = ""
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._abandoned = False

    def settle(self, reply: str) -> None:
        """First settle wins."""
        with self._lock:
            if self._done.is_set():
                return
            self.reply = reply
            self._done.set()

    def abandon(self) -> None:
        """The waiting side gave up (timeout, the turn's stop). A later settle still records the
        line (nothing reads it), but `abandoned` tells the shell nobody is waiting any more."""
        with self._lock:
            self._abandoned = True

    @property
    def abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    @property
    def settled(self) -> bool:
        return self._done.is_set()

    @property
    def waiting(self) -> bool:
        """True while a worker could still receive the reply — parked, not settled, not given up."""
        return not self.settled and not self.abandoned

    def wait(self, timeout: float | None = None, *, cancel=None) -> str | None:
        """The panel's one-line answer, or None when nothing answered in time (or the turn's
        CancelToken fired first) — either way the command is abandoned on the way out. The default
        ceiling is the instant commands' COMMAND_TIMEOUT_S; a parked "measure" waits
        MEASURE_TIMEOUT_S."""
        if timeout is None:
            timeout = MEASURE_TIMEOUT_S if self.command == "measure" else COMMAND_TIMEOUT_S
        waited = 0.0
        while True:
            if self._done.is_set():
                return self.reply
            if cancel is not None and cancel.is_set():
                self.abandon()
                return None
            if waited >= timeout:
                self.abandon()
                return None
            step = min(_POLL_S, timeout - waited)
            self._done.wait(step)
            waited += step
