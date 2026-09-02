"""Domain events — facts the services announce and the UI reacts to.

Plain frozen dataclasses. The transport (an EventBus) is a port; these are the payloads.

A couple of events carry a RESULT HOLDER: the event itself stays a frozen fact, and every bit of
mutability (claim / settle / wait) lives inside a small stdlib-only object the publisher parks on
until the UI answers. That is how a tool call on a worker thread can ask the GUI thread a question
and hear the real answer back, instead of assuming it.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from helix.domain.models import App


@dataclass(frozen=True)
class Event:
    """Base class for domain events."""


@dataclass(frozen=True)
class BuildCreated(Event):
    app: App


@dataclass(frozen=True)
class BuildIterated(Event):
    app: App


@dataclass(frozen=True)
class BuildRenamed(Event):
    """A build was given a new display name (and possibly a new slug) — the menu refreshes. old_slug lets
    an open viewer re-point itself when the rename moved the workspace to a new slug."""

    app: App
    old_slug: str | None = None


@dataclass(frozen=True)
class BuildDeleted(Event):
    slug: str


@dataclass(frozen=True)
class AgentsChanged(Event):
    """An agent was created, renamed, or removed — the menu's Agents tab should refresh."""


@dataclass(frozen=True)
class SelfChangeProgress(Event):
    """A live step while HELIX drafts a change to its OWN code in the background.

    `unattended` means nobody asked for this draft and nobody is in the room — the overnight Evolve
    pass reuses this very lane between 3 and 6 AM. Growth narration is deliberately spoken even when
    the mic is asleep (a user who asked HELIX to improve itself WANTS to hear it describe what it is
    becoming), so without this flag riding along with the line, the 3 AM pass reads every coder step
    aloud into a dark house. It is trailing and defaulted so the attended publisher — improve_helix,
    a change the user asked for and is sitting through — keeps narrating exactly as it always has."""

    line: str
    unattended: bool = False


@dataclass(frozen=True)
class SelfChangeFinished(Event):
    """A background self-change draft ended — announced so the user can apply or discard it.

    `unattended` carries the same fact as on SelfChangeProgress, and matters most here: the FINISHED
    announcement is the one the sleeping house would hear ("Couldn't draft that change") hours after
    everyone went to bed. Unattended lands as a bubble and a status line only — the quiet suggestion
    waiting to be read in the morning. Trailing + defaulted keeps every existing publish attended."""

    ok: bool
    summary: str = ""
    branch: str = ""
    error: str | None = None
    stopped: bool = False
    unattended: bool = False


@dataclass(frozen=True)
class BuildDeleteRequested(Event):
    """The model asked to delete something. Deletion is NEVER performed from the model loop — this event
    asks the UI to get one real human confirmation first, so injected text can't trigger a silent rmtree.

    name: the user-facing name the model named."""

    name: str


@dataclass(frozen=True)
class BuildOpenRequested(Event):
    """The model asked to OPEN a build ("open it", "show me the tip calculator") — the UI resolves the
    slug and opens it exactly as a menu click would. Read-only from the model's side.

    slug: the resolved workspace slug; name: the user-facing name (for the announcement)."""

    slug: str
    name: str


@dataclass(frozen=True)
class ConnectRequested(Event):
    """The model asked to CONNECT an outside service just in time (a key is needed for a watcher, a
    build, a hologram engine, or call_api). The UI opens a small masked key panel naming the service
    and why; the user pastes the value into IT — never into chat, never spoken — and it lands in the
    same secrets/settings store as before. The model never sees a key's value.

    service_id: a JIT-connectable service id (see services/connections.py CONNECTABLE);
    reason: one plain-words line for the panel ("the Slack watcher needs a token")."""

    service_id: str
    reason: str = ""


@dataclass(frozen=True)
class CameraRequested(Event):
    """The model asked to LOOK THROUGH THE CAMERA because the user wants to show it a physical
    thing ("what is this part I'm holding?"). The UI opens a small live-preview window on the GUI
    thread; the user presents the object; the window hands ONE captured frame back through the
    request holder to the tool's parked worker thread, and the model sees it as an ephemeral image
    — never saved. The event stays a frozen fact; the mutability (claim/fulfil/fail/wait) lives
    inside the holder.

    request: a helix.services.camera.CameraRequest (typed loosely so the domain layer stays free of
    service imports)."""

    request: object


# How long the parked tool waits for a UI to CLAIM a sleep request. No claim means there is nothing
# on the other side of the bus that could have rested any ears (headless, tests, a torn-down shell) —
# give up fast and say so, rather than hanging the turn on a listener that does not exist.
SLEEP_CLAIM_TIMEOUT_S = 2.0
# The ceiling once a UI HAS claimed it. Resting the mic is a handful of instructions on the GUI
# thread, so this only ever elapses if that thread is wedged; it stays short so a goodnight is never
# the slowest thing HELIX does.
SLEEP_TIMEOUT_S = 5.0

_SLEEP_POLL_S = 0.05  # wait-loop granularity: how often the parked worker rechecks its cancel token


class SleepRequest:
    """One sleep request: created by the tool's worker thread, claimed and settled by the UI, awaited
    by the worker. Modelled on services/camera.py's CameraRequest, for the same reason — the answer
    lives on the GUI thread and the question was asked from a worker.

    It exists because the tool used to ASSUME the answer. go_to_sleep told the model "the ears are
    resting" unconditionally, so when nothing was listening (silent mode, no microphone, the mic
    already asleep) the console wrote "there's nothing to put to sleep" on screen while the model,
    told the tool had succeeded, spoke a goodnight for ears that never closed — a plain
    self-contradiction sitting in the transcript. With a holder the UI reports what actually
    happened and the model's reply matches the room.

    Thread contract: claim/fulfil/fail run on the UI thread, wait() on the tool's worker thread,
    abandon() on whichever side gives up first. First settle wins; everything after is a no-op.
    """

    def __init__(self) -> None:
        self.error = ""       # the plain-words reason when the ears did NOT rest
        self._slept = False
        self._lock = threading.Lock()
        self._claimed = threading.Event()
        self._done = threading.Event()
        self._abandoned = False

    def claim(self) -> bool:
        """The UI announces it is handling this request. False = the worker already gave up
        (cancelled/timed out), so nothing should be slept on its behalf."""
        with self._lock:
            if self._abandoned:
                return False
            self._claimed.set()
            return True

    def fulfil(self) -> None:
        """The ears really did close. First settle wins."""
        with self._lock:
            if self._abandoned or self._done.is_set():
                return
            self._slept = True
            self._done.set()

    def fail(self, reason: str) -> None:
        """Settle without resting anything — the reason is what the model relays instead of a
        goodnight ("nothing was listening")."""
        with self._lock:
            if self._abandoned or self._done.is_set():
                return
            self.error = reason
            self._done.set()

    def abandon(self) -> None:
        """The waiting side gave up (cancel/timeout). Late fulfil/fail calls become no-ops, so a UI
        answering after the turn moved on can never rewrite an outcome the model already reported."""
        with self._lock:
            self._abandoned = True

    @property
    def abandoned(self) -> bool:
        with self._lock:
            return self._abandoned

    def wait(self, *, cancel=None, claim_timeout: float | None = None,
             timeout: float | None = None) -> bool:
        """Park the worker until the UI settles the request. True = the ears are resting; False with
        `error` set = they are not, and the model must say so instead of saying goodnight."""
        claim_s = SLEEP_CLAIM_TIMEOUT_S if claim_timeout is None else claim_timeout
        total_s = SLEEP_TIMEOUT_S if timeout is None else timeout

        def claimed_or_settled() -> bool:  # an unclaimed fail() still counts as a real answer
            return self._claimed.is_set() or self._done.is_set()

        if not self._poll(claimed_or_settled, claim_s, cancel):
            self.abandon()
            if not self.error:
                self.error = "Nothing was listening just now, so there was nothing to rest."
            return False
        if not self._poll(self._done.is_set, total_s, cancel):
            self.abandon()
            if not self.error:
                self.error = "The ears didn't settle just now."
            return False
        return self._slept

    def _poll(self, ready, limit_s: float, cancel) -> bool:
        """Wait until ready() up to limit_s, rechecking the cancel token; False = gave up. Paced on
        the done event so a settle wakes the worker instantly instead of on the next poll tick."""
        waited = 0.0
        while True:
            if ready():
                return True
            if cancel is not None and cancel.is_set():
                if not self.error:
                    self.error = "Stopped before the ears could rest."
                return False
            if waited >= limit_s:
                return False
            step = min(_SLEEP_POLL_S, limit_s - waited)
            self._done.wait(step)
            waited += step


@dataclass(frozen=True)
class SleepRequested(Event):
    """The model judged that the user genuinely asked HELIX to rest its ears — a sleep request
    embedded in natural speech ("go take a nap while we talk") rather than a crisp voice command
    (those are handled deterministically in the voice layer). The UI puts the mic to sleep WITHOUT
    the canned confirmation; the model's own reply is the goodnight. Merely MENTIONING the sleep
    command (explaining HELIX to someone) must never publish this.

    `request` is the result holder the UI settles with what actually happened, so the tool can tell
    the model the truth rather than assuming the mic obeyed (see SleepRequest above). Defaulted to
    None so a publisher that does not care about the outcome — and the event's own identity, which
    several tests assert on — is unchanged."""

    request: SleepRequest | None = None


@dataclass(frozen=True)
class BuildStarted(Event):
    """A background build just began — the tile turns yellow, it joins the Console legend, and the orb
    goes to its working hue. Fired by the Forge the moment the workspace is marked building, so the UI
    reflects in-progress work immediately (not only when it finishes)."""

    name: str
    slug: str
    iterating: bool = False


@dataclass(frozen=True)
class BuildProgress(Event):
    """A live step from a background build, for the status line / spoken narration."""

    name: str
    line: str


@dataclass(frozen=True)
class BuildFinished(Event):
    """A background build ended — for the spoken announcement (separate from BuildCreated, which the
    menu listens to). `handle` is the BuildHandle of a stopped/half-built job, for the cleanup offer
    (typed loosely as object so the domain doesn't import the services layer)."""

    name: str
    ok: bool
    error: str | None = None
    stopped: bool = False
    handle: object | None = None
    iterating: bool = False  # True = an existing build was updated in place (announce "Updated", not "Done")
    slug: str = ""  # the build's slug, so the menu tile / status board can be keyed without re-resolving
