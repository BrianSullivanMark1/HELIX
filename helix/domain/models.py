"""Core domain models — plain data + tiny pure helpers. No I/O, no framework."""
from __future__ import annotations

import hashlib
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
    """How a built app runs (the run mechanism — NOT the taxonomy; see BuildKind)."""

    HTML = "html"  # open in a webview / browser
    PYTHON = "python"  # run in a console
    UNKNOWN = "unknown"


class BuildKind(str, Enum):
    """The taxonomy of a Build — WHAT the Forge made, independent of how it runs.

    Apps, tasks, and models are workspace builds (data/builds/<slug>/). Agents are a lighter
    substrate (a saved goal, no workspace — see AgentService) but share the taxonomy so every kind
    is conjurable by the orb and the menu can present them uniformly.
    """

    APP = "app"  # an interactive thing that opens a screen
    TASK = "task"  # a headless script that does a thing when run (console)
    AGENT = "agent"  # a saved goal HELIX runs on demand
    MODEL = "model"  # a 3D model/animation conjured to show the user (build_3d_model)


def slugify(name: str) -> str:
    """A filesystem- and git-safe slug for an app name.

    A name made entirely of non-ASCII characters (emoji, CJK, accents) would otherwise collapse to the
    same literal 'app' and silently overwrite earlier builds — so fall back to a stable content hash that
    stays unique per name. Cap the length so a very long name can't push the workspace path past the
    filesystem limit."""
    cleaned = name.strip()
    s = re.sub(r"[^a-z0-9]+", "-", cleaned.lower()).strip("-")
    if s:
        return s[:80].rstrip("-")
    if not cleaned:
        return "app"  # a truly empty / whitespace-only name (degenerate)
    # Non-empty but all non-ASCII (emoji, CJK, accents-only) → a stable per-name hash, so two such names
    # don't both collapse to one slug and silently overwrite each other.
    return "build-" + hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]


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
    kind: AppKind = AppKind.UNKNOWN  # run mechanism (HTML/PYTHON/UNKNOWN)
    build_kind: BuildKind = BuildKind.APP  # taxonomy (app/task/model) — the canonical kind
    entry_point: str | None = None
    created_at: datetime | None = None

    @property
    def is_model(self) -> bool:
        """Back-compat convenience: a 3D model build is shown in the Models tab, not Apps."""
        return self.build_kind == BuildKind.MODEL

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
