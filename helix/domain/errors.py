"""Domain exceptions — raised by services, handled by the UI."""
from __future__ import annotations

from typing import Any


class HelixError(Exception):
    """Base for all HELIX domain errors."""


class ConfirmationRequired(HelixError):
    """A spend/self-change action needs explicit user confirmation first.

    Carries the pending action so the UI can ask, then re-dispatch the same call on "yes".
    """

    def __init__(self, prompt: str, action: str, args: dict[str, Any] | None = None):
        super().__init__(prompt)
        self.prompt = prompt
        self.action = action
        self.args = args or {}


class ConstitutionViolation(HelixError):
    """A self-change touched a protected path, a locked setting, or the immutable shell."""


class MissingApiKey(HelixError):
    """No Claude API key is configured yet."""


class BuildError(HelixError):
    """A build (or coder run) failed."""


class BuildCancelled(HelixError):
    """A build was stopped by the user mid-run. Carries what was being built so the UI can offer to
    remove (a new build) or roll back (an iteration) the half-finished work."""

    def __init__(self, slug: str, name: str, iterating: bool, is_model: bool = False):
        super().__init__(f"build of {name!r} was stopped")
        self.slug = slug
        self.name = name
        self.iterating = iterating
        self.is_model = is_model
