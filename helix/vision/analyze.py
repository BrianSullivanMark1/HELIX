"""HELIX's eyes: answer any question about what a camera sees, via Claude vision (§vision).

Generic by design — a camera is just an eye. Ask anything ("what is this tool?", "who's at the door?",
"what's in the fridge?", "is the garage light on?") and HELIX captures a frame and answers. Output is
plain spoken text. People are described by appearance only (no identity/web lookup); deeper profiling is
a separate, privacy-gated feature for later.
"""
from __future__ import annotations

import base64
from typing import Any

from helix.ai.claude import DEFAULT_CLAUDE_MODEL, ClaudeClient, ClaudeConfig, estimate_cost

LOOK_PROMPT = (
    "You are HELIX, looking through one of your cameras (your eyes). Answer the user's question about "
    "what you see, in a few plain spoken sentences — no markdown, symbols, or emoji (it may be read aloud).\n"
    "- If they ask about a tool or object, name it and say what it is for, with a quick use or safety tip.\n"
    "- If a person is in view, describe appearance only (apparent sex, rough age range, clothing, what they "
    "are doing) — never their name, identity, ethnicity, or character judgments.\n"
    "- Otherwise answer concretely about what is visible. If you cannot tell, say so briefly.\n\n"
    "The user's question: {question}"
)


def look_prompt(question: str = "") -> str:
    return LOOK_PROMPT.format(question=(question or "").strip() or "What do you see?")


def describe_image(
    image_jpeg: bytes,
    question: str = "",
    *,
    client: ClaudeClient | None = None,
    model: str = DEFAULT_CLAUDE_MODEL,
    memory: Any | None = None,
    max_tokens: int = 700,
) -> str:
    """Answer `question` about a JPEG frame with Claude vision; return spoken text. Records AI usage."""
    client = client or ClaudeClient(ClaudeConfig(model=model))
    image_b64 = base64.b64encode(image_jpeg).decode("ascii")
    text = client.vision(
        look_prompt(question), image_b64, media_type="image/jpeg", max_tokens=max_tokens, model=model
    )
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
