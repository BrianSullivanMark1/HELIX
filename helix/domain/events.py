"""Domain events — facts the services announce and the UI reacts to.

Plain frozen dataclasses. The transport (an EventBus) is a port; these are the payloads.
"""
from __future__ import annotations

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
    """A live step while HELIX drafts a change to its OWN code in the background."""

    line: str


@dataclass(frozen=True)
class SelfChangeFinished(Event):
    """A background self-change draft ended — announced so the user can apply or discard it."""

    ok: bool
    summary: str = ""
    branch: str = ""
    error: str | None = None
    stopped: bool = False


@dataclass(frozen=True)
class BuildDeleteRequested(Event):
    """The model asked to delete something. Deletion is NEVER performed from the model loop — this event
    asks the UI to get one real human confirmation first, so injected text can't trigger a silent rmtree.

    name: the user-facing name the model named."""

    name: str


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
