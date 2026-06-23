"""VersionedRepo port — the git verbs the Forge needs. No raw git anywhere else."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Commit:
    sha: str
    summary: str
    at: datetime


class VersionedRepo(Protocol):
    def init(self, repo_dir: Path) -> None: ...

    def current_branch(self, repo_dir: Path) -> str: ...

    def create_branch(self, repo_dir: Path, name: str) -> None: ...

    def checkout(self, repo_dir: Path, ref: str) -> None: ...

    def commit_all(self, repo_dir: Path, message: str) -> Commit: ...

    def merge_no_ff(self, repo_dir: Path, branch: str, message: str) -> Commit:
        """Revertible merge — always a real merge commit, never fast-forward."""
        ...

    def revert(self, repo_dir: Path, sha: str) -> Commit: ...

    def restore_to(self, repo_dir: Path, sha: str) -> None:
        """Hard-restore the working tree to a past commit (the Archive lifeline)."""
        ...

    def log(self, repo_dir: Path, limit: int = 100) -> list[Commit]: ...

    def changed_paths(self, repo_dir: Path, ref_a: str, ref_b: str) -> list[str]: ...

    def deleted_paths(self, repo_dir: Path, ref_a: str, ref_b: str) -> list[str]: ...

    def add_worktree(self, repo_dir: Path, path: Path, ref: str) -> None: ...

    def remove_worktree(self, repo_dir: Path, path: Path) -> None: ...
