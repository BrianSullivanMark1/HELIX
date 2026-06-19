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


def _morning_briefing() -> str:
    return (
        "Morning Briefing — placeholder.\n"
        "This is where HELIX will assemble your overnight market moves, calendar, and home reminders."
    )


def _check_all_systems() -> str:
    return (
        "All Systems — placeholder.\n"
        "This is where HELIX will report broker, AI, and camera connectivity at a glance."
    )


def _review_portfolio() -> str:
    return (
        "Review Portfolio — placeholder.\n"
        "This is where HELIX will summarize positions, P&L, and any rebalancing it recommends."
    )


# Example built-ins demonstrating the pattern. Replace the placeholder callables with real work, or
# append/register new tasks alongside them.
BUILTIN_TASKS: list[Task] = [
    Task("morning_briefing", "Run Morning Briefing",
         "overnight markets · calendar · reminders", _morning_briefing),
    Task("check_systems", "Check All Systems",
         "broker · AI · cameras", _check_all_systems),
    Task("review_portfolio", "Review Portfolio",
         "positions · P&L · rebalance", _review_portfolio),
]


def register(task: Task) -> None:
    """Add (or replace, by key) a task at runtime so new task apps can be wired in programmatically."""
    BUILTIN_TASKS[:] = [t for t in BUILTIN_TASKS if t.key != task.key] + [task]


def all_tasks() -> list[Task]:
    return list(BUILTIN_TASKS)
