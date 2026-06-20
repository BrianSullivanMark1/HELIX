"""Runnable task "applications" surfaced by the Tasks button in the main navigation (§tasks).

Each Task is a small runnable action the user launches from the Tasks panel — the action counterpart to
the Menu's app shortcuts. The panel renders one card per registered task; clicking a card runs it and
shows the returned text. Tasks are pure callables returning a short status string (any real I/O lives at
the edges in the callable that gets registered), so this module stays import-safe and side-effect free.

Add a task either by appending a Task to BUILTIN_TASKS below, or programmatically at runtime via
`register(Task(...))` — both feed the same `all_tasks()` the panel reads, so new task apps need no UI
changes to appear.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Task:
    key: str
    label: str
    subtitle: str
    run: Callable[[], str]


# The Tasks launcher starts blank — no built-in default tasks. Add real task apps either by appending a
# Task here, or programmatically at runtime via `register(Task(...))`; both feed `all_tasks()`, so a new
# task app appears with no UI changes.
BUILTIN_TASKS: list[Task] = []


def register(task: Task) -> None:
    """Add (or replace, by key) a task at runtime so new task apps can be wired in programmatically."""
    BUILTIN_TASKS[:] = [t for t in BUILTIN_TASKS if t.key != task.key] + [task]


def all_tasks() -> list[Task]:
    return list(BUILTIN_TASKS)
