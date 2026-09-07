"""Back-compat shim — the vocabulary (V3 words + speakable tool labels) lives in vocabulary.py."""
from __future__ import annotations

from helix.domain.vocabulary import friendly_tool_label

__all__ = ["friendly_tool_label"]
