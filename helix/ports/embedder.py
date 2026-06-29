"""Embedder port — turns text into vectors for optional SEMANTIC knowledge search.

Optional by design: when no embedder is configured (no key), the knowledge layer uses keyword retrieval
only. An embedder returns None on any failure, so retrieval always degrades gracefully to keyword.
"""
from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    model: str  # an id for the embedding space, so a cache can invalidate when the model changes

    def available(self) -> bool:
        """True when embedding is configured (e.g. a key is set)."""
        ...

    def embed(
        self, texts: list[str], *, input_type: str | None = None
    ) -> list[list[float]] | None:
        """One vector per input text, or None if embedding is unavailable or failed (caller falls back to
        keyword search). `input_type` is an optional hint: 'query' or 'document'."""
        ...
