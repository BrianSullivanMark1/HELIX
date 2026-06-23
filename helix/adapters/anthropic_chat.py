"""ChatModel adapter — the Anthropic Messages API via the official SDK.

Every Anthropic specific lives here: the model id, prompt caching, tool-use translation, and the
usage→cost estimate. The rest of HELIX sees only the provider-neutral ChatModel port, so swapping the
model layer never reaches past this file.
"""
from __future__ import annotations

from typing import Any, Callable

import anthropic

from helix.domain.errors import MissingApiKey
from helix.domain.models import Role
from helix.logging_setup import get_logger
from helix.ports.llm import Reply, Text, ToolResult, ToolSpec, ToolUse, Turn, Usage

_LOG = get_logger("anthropic")

# Default model. Opus 4.8 uses adaptive thinking only; we omit `thinking` here for snappy
# conversational turns (no thinking) and keep replies short — the Forge's chat is routing + confirming,
# not long-form generation.
DEFAULT_MODEL = "claude-opus-4-8"

# Opus 4.8 list price, USD per token (input $5 / output $25 per 1M; cache write ~1.25x, read ~0.1x).
_PRICE_IN = 5.0 / 1_000_000
_PRICE_OUT = 25.0 / 1_000_000
_PRICE_CACHE_WRITE = 6.25 / 1_000_000
_PRICE_CACHE_READ = 0.5 / 1_000_000


def _estimate_cost(usage: Any) -> float:
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens * _PRICE_IN
        + usage.output_tokens * _PRICE_OUT
        + cache_write * _PRICE_CACHE_WRITE
        + cache_read * _PRICE_CACHE_READ
    )


class AnthropicChat:
    """Implements the ChatModel port. The API key is read lazily so it can be set/changed at runtime."""

    def __init__(
        self,
        api_key_provider: Callable[[], str | None],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
    ) -> None:
        self._key_provider = api_key_provider
        self._model = model
        self._max_tokens = max_tokens
        self._client: anthropic.Anthropic | None = None
        self._client_key: str | None = None

    def _client_for_current_key(self) -> anthropic.Anthropic:
        key = (self._key_provider() or "").strip()
        if not key:
            raise MissingApiKey("Add your Claude API key in Settings to start building.")
        if self._client is None or self._client_key != key:
            self._client = anthropic.Anthropic(api_key=key)
            self._client_key = key
        return self._client

    def chat(
        self,
        turns: list[Turn],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Reply:
        client = self._client_for_current_key()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [self._encode_turn(t) for t in turns],
        }
        if system:
            # Cache the stable system prefix (silently no-ops below the model's min cacheable size).
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            kwargs["tools"] = [self._encode_tool(t) for t in tools]

        resp = client.messages.create(**kwargs)
        return self._decode(resp)

    # ----- translation: port types <-> Anthropic wire format -----
    @staticmethod
    def _encode_tool(spec: ToolSpec) -> dict[str, Any]:
        return {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}

    @staticmethod
    def _encode_turn(turn: Turn) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for b in turn.blocks:
            if isinstance(b, Text):
                content.append({"type": "text", "text": b.text})
            elif isinstance(b, ToolUse):
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.args})
            elif isinstance(b, ToolResult):
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": b.content,
                }
                if b.is_error:
                    block["is_error"] = True
                content.append(block)
        role = "assistant" if turn.role == Role.ASSISTANT else "user"
        return {"role": role, "content": content}

    @staticmethod
    def _decode(resp: Any) -> Reply:
        blocks: list[Any] = []
        for b in resp.content:
            if b.type == "text":
                blocks.append(Text(b.text))
            elif b.type == "tool_use":
                blocks.append(ToolUse(id=b.id, name=b.name, args=dict(b.input)))
        if getattr(resp, "stop_reason", None) == "refusal" and not blocks:
            blocks.append(Text("I'm sorry — I can't help with that request."))
        u = resp.usage
        usage = Usage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cost_usd=_estimate_cost(u),
        )
        return Reply(blocks=tuple(blocks), usage=usage)
