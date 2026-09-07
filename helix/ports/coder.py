"""CoderAgent port — writes/modifies code in a repo on a branch. The Forge's hands."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

ProgressFn = Callable[[str], None]


class Cancellable(Protocol):
    """Minimal stop signal the coder polls (CancelToken satisfies it)."""

    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class CoderResult:
    ok: bool
    summary: str
    changed_paths: tuple[str, ...] = ()
    error: str | None = None


class CoderAgent(Protocol):
    name: str

    def available(self) -> bool:
        """Can this agent run right now (CLI present / key set)?"""
        ...

    def run_task(
        self,
        repo_dir: Path,
        prompt: str,
        *,
        on_progress: ProgressFn | None = None,
        cancel: "Cancellable | None" = None,
        model: str | None = None,
    ) -> CoderResult:
        """Edit files under repo_dir to satisfy `prompt`, streaming progress lines. If `cancel` is set
        mid-run, stop as soon as possible and return a non-ok result. `model` optionally overrides the
        agent's configured model for THIS run (self-improvement sizes the model to the task — the
        Fable-5 proposal picks it); None keeps the configured default."""
        ...
