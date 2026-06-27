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
class BuildDeleted(Event):
    slug: str


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
