"""SuggestionService — HELIX's ANTICIPATE surface: quiet, occasional, genuinely-useful nudges.

Deterministic producers only (no LLM, no network) so it's cheap and never chatty: it proposes ONE
candidate suggestion when asked, and the UI decides whether/when to actually show it (rate-limited,
dismissible, silent unless the user turned proactive speech on). The point is a calm ambient presence —
resurface a neglected build, remind about a drafted change — not a stream of pop-ups.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Suggestion:
    id: str                 # stable per underlying thing, so the UI can dedupe/never re-nag a dismissal
    text: str               # the one-line nudge shown on the chip
    open_slug: str = ""     # if set, an "Open" action opens this build


class SuggestionService:
    def __init__(self, recommend=None, builds=None, selfdev=None) -> None:
        self._recommend = recommend
        self._builds = builds
        self._selfdev = selfdev

    def candidate(self) -> Suggestion | None:
        """The single best suggestion to (maybe) surface right now, or None. Ordered by usefulness;
        the caller rate-limits and skips ones already dismissed this session."""
        # 1) A drafted self-change is waiting — the most actionable thing to nudge.
        if self._selfdev is not None:
            try:
                pending = self._selfdev.pending()
            except Exception:  # noqa: BLE001
                pending = []
            if pending:
                p = pending[0]
                label = (p.summary or p.id or "a change").strip().splitlines()[0][:60]
                return Suggestion(id=f"selfchange:{p.id}",
                                  text=f"A change is drafted — “{label}”. Say “apply it” or “discard it”.")
        # 2) A build the user used before but hasn't opened in a while — resurface it.
        if self._recommend is not None and self._builds is not None:
            try:
                apps = self._builds.list()
                for app, reason in self._recommend.suggestions(apps, limit=3):
                    if "not opened" in reason:
                        return Suggestion(id=f"neglected:{app.slug}",
                                          text=f"You haven’t opened “{app.name}” in a while ({reason}).",
                                          open_slug=app.slug)
            except Exception:  # noqa: BLE001
                pass
        return None
