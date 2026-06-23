"""Core domain models — plain data + tiny pure helpers. No I/O, no framework."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Role(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class AppKind(str, Enum):
    """How a built app runs."""

    HTML = "html"  # open in a webview / browser
    PYTHON = "python"  # run in a console
    UNKNOWN = "unknown"


def slugify(name: str) -> str:
    """A filesystem- and git-safe slug for an app name."""
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "app"


@dataclass(frozen=True)
class Message:
    """One line of the human-facing conversation transcript."""

    role: Role
    text: str
    at: datetime | None = None


@dataclass
class App:
    """A built app (internally a 'Build'). Lives at data/builds/<slug>/."""

    slug: str
    name: str
    request: str  # the originating plain-language description
    kind: AppKind = AppKind.UNKNOWN
    entry_point: str | None = None
    created_at: datetime | None = None

    @classmethod
    def from_request(cls, name: str, request: str) -> "App":
        return cls(slug=slugify(name), name=name.strip(), request=request.strip())


@dataclass
class Version:
    """One entry in the Archive — a git commit indexed for restore."""

    commit: str
    summary: str
    at: datetime
    pinned: bool = False


@dataclass
class PendingChange:
    """A self-modification awaiting human approval."""

    id: str
    branch: str
    summary: str
    request: str
    created_at: datetime | None = None
