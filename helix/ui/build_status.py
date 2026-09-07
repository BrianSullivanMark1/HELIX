"""BuildStatusBoard — the shared, UI-thread source of truth for each build's live status.

It drives two surfaces that must always agree: the menu tiles' coloured borders and the Console legend.
(The orb's hue is driven separately by the Console, which adds a green "just finished" flash and an
error hold the persistent tile/legend status shouldn't have.) Pure data, no Qt — updated only from the
UI thread (the main window's bus-bridged slots), so it needs no locking. A build is keyed by its slug.

Status meanings (mirrored in the theme palette):
    BUILDING → yellow: a build is in progress.
    DONE     → green: it finished successfully and hasn't been reopened since.
    ERROR    → red: its last build errored.
A build with no entry here is blue (the default): never built this session, or done-and-since-reopened.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BuildStatus(str, Enum):
    BUILDING = "building"  # yellow
    DONE = "done"          # green
    ERROR = "error"        # red


@dataclass(frozen=True)
class LegendEntry:
    slug: str
    name: str
    status: BuildStatus


# Legend ordering: what's happening now first, then fresh results, then problems.
_ORDER = {BuildStatus.BUILDING: 0, BuildStatus.DONE: 1, BuildStatus.ERROR: 2}


class BuildStatusBoard:
    def __init__(self) -> None:
        self._status: dict[str, BuildStatus] = {}
        self._names: dict[str, str] = {}

    def mark_building(self, slug: str, name: str) -> None:
        if not slug:  # a keyless build can never be cleared (no tile/chip to open) — never record it
            return
        self._status[slug] = BuildStatus.BUILDING
        self._names[slug] = name

    def mark_done(self, slug: str, name: str | None = None) -> None:
        if not slug:
            return
        self._status[slug] = BuildStatus.DONE
        if name:
            self._names[slug] = name

    def mark_error(self, slug: str, name: str | None = None) -> None:
        if not slug:
            return
        self._status[slug] = BuildStatus.ERROR
        if name:
            self._names[slug] = name

    def mark_seen(self, slug: str) -> bool:
        """Opening or navigating to a build acknowledges a finished result: a DONE/ERROR entry clears
        back to blue and drops off the legend. A BUILDING entry is left alone (it's still running).
        Returns True if anything changed (so the caller can refresh)."""
        if self._status.get(slug) in (BuildStatus.DONE, BuildStatus.ERROR):
            self._status.pop(slug, None)
            self._names.pop(slug, None)
            return True
        return False

    def remove(self, slug: str) -> None:
        """A build was deleted — drop it entirely."""
        self._status.pop(slug, None)
        self._names.pop(slug, None)

    def status_of(self, slug: str) -> BuildStatus | None:
        return self._status.get(slug)

    def legend(self) -> list[LegendEntry]:
        """Builds that want the user's attention — in-progress, freshly done, or errored — ordered for
        the legend. Self-clearing: entries leave as builds finish-and-are-seen or are deleted."""
        entries = [
            LegendEntry(slug, self._names.get(slug, slug), status)
            for slug, status in self._status.items()
        ]
        entries.sort(key=lambda e: (_ORDER.get(e.status, 9), e.name.lower()))
        return entries
