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

# Anthropic server-side tools: Claude runs the searches/fetches itself and answers with the results
# folded in (no search-API key, no client executor). The result blocks come back as server-tool types
# that _decode ignores — HELIX only reads the answer text. Supported on Opus 4.8 (dynamic filtering).
_WEB_TOOLS = (
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
)

# Per-model list price, USD per 1M tokens: (input, output). Cache write ~1.25x input, read ~0.1x input.
# Tiering uses Sonnet for the conversation and Opus for builds / deep reasoning, so cost must be
# model-aware — a flat Opus rate would over-report the cheap conversational turns.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICING = (5.0, 25.0)


def _estimate_cost(model: str, usage: Any) -> float:
    price_in, price_out = _PRICING.get(model, _DEFAULT_PRICING)
    price_in /= 1_000_000
    price_out /= 1_000_000
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens * price_in
        + usage.output_tokens * price_out
        + cache_write * price_in * 1.25
        + cache_read * price_in * 0.1
    )


class AnthropicChat:
    """Implements the ChatModel port. The API key is read lazily so it can be set/changed at runtime."""

    def __init__(
        self,
        api_key_provider: Callable[[], str | None],
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        web_search: bool = False,
        thinking: str | None = None,   # "adaptive" (deep) | "disabled" (fast) | None (model default)
        effort: str | None = None,     # "low" | "medium" | "high" | "max"; None leaves it at the default
    ) -> None:
        self._key_provider = api_key_provider
        self._model = model
        self._max_tokens = max_tokens
        self._web_search = web_search  # let Claude search/fetch the web (the conversation; not the coder)
        self._thinking = thinking      # the conversation runs "disabled" for speed; the deep tier "adaptive"
        self._effort = effort
        self._client: anthropic.Anthropic | None = None
        self._client_key: str | None = None

    def _client_for_current_key(self) -> anthropic.Anthropic:
        key = (self._key_provider() or "").strip()
        if not key:
            # Reached only when the subscription path is unavailable (no token, no CLI, or it failed)
            # AND no API key is set — name both so a token-only user knows their options.
            raise MissingApiKey(
                "Claude isn't reachable right now. Check your subscription token (run "
                "claude setup-token) or add a Claude API key in Settings."
            )
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
        # Thinking + effort: "disabled"/low keeps the conversation snappy; "adaptive"/high lets the deep
        # tier reason. Both are GA on Opus 4.8 and Sonnet 4.6 (no beta header).
        if self._thinking in ("adaptive", "disabled"):
            kwargs["thinking"] = {"type": self._thinking}
        if self._effort:
            kwargs["output_config"] = {"effort": self._effort}
        # Web search/fetch (server tools) sit first; the app's own tools follow. Claude runs the web
        # tools server-side and returns the answer text, so HELIX has nothing to execute for them.
        encoded = list(_WEB_TOOLS) if self._web_search else []
        if tools:
            encoded += [self._encode_tool(t) for t in tools]
        if encoded:
            kwargs["tools"] = encoded

        resp = client.messages.create(**kwargs)
        return self._decode(resp, self._model)

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
    def _decode(resp: Any, model: str) -> Reply:
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
            cost_usd=_estimate_cost(model, u),
        )
        return Reply(blocks=tuple(blocks), usage=usage)
