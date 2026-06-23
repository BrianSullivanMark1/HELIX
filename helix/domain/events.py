"""Domain events — facts the services announce and the UI reacts to.

Plain frozen dataclasses. The transport (an EventBus) is a port; these are the payloads.
"""
from __future__ import annotations

from dataclasses import dataclass

from helix.domain.models import App, Version


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
class VersionAdded(Event):
    version: Version


@dataclass(frozen=True)
class PendingChangeRaised(Event):
    summary: str


@dataclass(frozen=True)
class UsageRecorded(Event):
    total_cost_usd: float
