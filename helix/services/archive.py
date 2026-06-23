"""ArchiveService — HELIX's own version history: index git log, restore, pin, factory reset."""
from __future__ import annotations

from pathlib import Path

from helix.domain.models import Version
from helix.ports.repo import VersionedRepo
from helix.ports.stores import MemoryStore


class ArchiveService:
    """Operates on HELIX's own repo (self-versioning). Built apps version inside their own workspace."""

    def __init__(self, repo: VersionedRepo, memory: MemoryStore, root: Path) -> None:
        self._repo = repo
        self._memory = memory
        self._root = root

    def refresh(self) -> list[Version]:
        """Sync recent git history into the SQLite version index."""
        known = {v.commit for v in self._memory.versions()}
        for commit in self._repo.log(self._root, limit=200):
            if commit.sha not in known:
                self._memory.add_version(Version(commit=commit.sha, summary=commit.summary, at=commit.at))
        return self.versions()

    def versions(self) -> list[Version]:
        return self._memory.versions()

    def restore(self, commit: str) -> None:
        self._repo.restore_to(self._root, commit)

    def pin(self, commit: str) -> None:
        self._memory.set_pinned(commit)

    def factory_reset(self) -> None:
        """The lifeline: restore to the very first commit (root)."""
        commits = self._repo.log(self._root, limit=100_000)
        if commits:
            self._repo.restore_to(self._root, commits[-1].sha)
