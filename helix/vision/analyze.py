"""Turn a camera frame into a spoken answer via Claude vision (§vision).

Connects sight to the conversation: identify a tool and explain it, describe a person at the door for
awareness, or answer a free question about what's in view. Output is plain spoken text (the Xpert voice
reads it aloud). Person analysis is deliberately appearance-only — no identity/web lookup here; that
heavier "online profiling" is a separate, privacy-gated feature for later.
"""
from __future__ import annotations

import base64
from typing import Any

from helix.ai.claude import DEFAULT_CLAUDE_MODEL, ClaudeClient, ClaudeConfig, estimate_cost

TOOL_PROMPT = (
    "You are HELIX, looking through a camera at a tool or object the user is holding up. In a few plain "
    "spoken sentences (no markdown, symbols, or emoji — it will be read aloud): name the tool, say what "
    "it is used for, and give one practical tip for using it well or safely. If you genuinely can't tell "
    "what it is, say so briefly and describe what you do see."
)
PERSON_PROMPT = (
    "You are HELIX, looking through a door or security camera for a homeowner's awareness. Briefly and "
    "factually describe the person: apparent sex, rough age range, clothing, and anything notable they "
    "are holding or doing. Plain spoken sentences, no markdown or symbols. Describe APPEARANCE ONLY — do "
    "not guess their name or identity, do not state ethnicity, and do not make character judgments. If no "
    "person is clearly visible, say so."
)
GENERAL_PROMPT = (
    "You are HELIX, looking through a camera. Describe what you see in a few plain spoken sentences (no "
    "markdown or symbols). Be concise and concrete."
)
_PROMPTS = {"tool": TOOL_PROMPT, "person": PERSON_PROMPT, "general": GENERAL_PROMPT}


def prompt_for(focus: str, question: str = "") -> str:
    base = _PROMPTS.get((focus or "general").lower().strip(), GENERAL_PROMPT)
    question = (question or "").strip()
    return f"{base}\n\nThe user also asks: {question}" if question else base


def describe_image(
    image_jpeg: bytes,
    focus: str = "general",
    question: str = "",
    *,
    client: ClaudeClient | None = None,
    model: str = DEFAULT_CLAUDE_MODEL,
    memory: Any | None = None,
) -> str:
    """Describe a JPEG frame with Claude vision and return spoken text. Records AI usage if `memory`."""
    client = client or ClaudeClient(ClaudeConfig(model=model))
    image_b64 = base64.b64encode(image_jpeg).decode("ascii")
    text = client.vision(prompt_for(focus, question), image_b64, media_type="image/jpeg", model=model)
    usage = client.last_usage or {}
    if memory is not None:
        try:
            in_tok = int(usage.get("input_tokens", 0) or 0)
            out_tok = int(usage.get("output_tokens", 0) or 0)
            if in_tok or out_tok:
                memory.record_ai_usage(model, in_tok, out_tok, estimate_cost(model, in_tok, out_tok))
        except Exception:
            pass
    return text
