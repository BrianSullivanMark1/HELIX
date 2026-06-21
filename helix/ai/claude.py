from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from helix.core.settings import AppSettings

# Transient HTTP statuses worth retrying with backoff (Anthropic's own guidance): 429 rate-limit,
# 500/502/503/504 server errors, 529 overloaded. A blip on one of these should not fail research.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
MAX_RETRIES = 3


DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
# Cheaper default for high-frequency research so AI cost stays well below trading gains.
DEFAULT_RESEARCH_MODEL = "claude-sonnet-4-6"
CLAUDE_API_KEY_SETTING = "claude_api_key"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# Approximate USD per 1,000,000 tokens (input, output). Estimates only — update as
# Anthropic pricing changes. Used solely for the scorecard cost figure.
MODEL_PRICES_PER_MTOK = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.80, 4.0),
}


class ClaudeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaudeConfig:
    model: str = DEFAULT_CLAUDE_MODEL
    api_key_env: str = "ANTHROPIC_API_KEY"
    timeout_seconds: int = 90


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Rough USD estimate for one call. Defaults to Sonnet rates for unknown models."""
    name = (model or "").lower()
    key = "sonnet"
    for candidate in ("opus", "sonnet", "haiku"):
        if candidate in name:
            key = candidate
            break
    in_rate, out_rate = MODEL_PRICES_PER_MTOK[key]
    return round((input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate, 6)


class ClaudeClient:
    def __init__(self, config: ClaudeConfig | None = None, api_key: str | None = None) -> None:
        self.config = config or ClaudeConfig()
        self.api_key = (
            api_key
            or os.environ.get(self.config.api_key_env, "")
            or AppSettings().get(CLAUDE_API_KEY_SETTING, "")
        )
        # Token usage from the most recent successful call: {"input_tokens", "output_tokens"}.
        self.last_usage: dict = {}

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict) -> dict:
        """POST a Messages API payload, capture token usage, and return the parsed body.

        Shared by complete() (single-shot) and chat() (multi-turn / tool-use).
        """
        request = urllib.request.Request(
            ANTHROPIC_MESSAGES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
                "x-api-key": self.api_key,
            },
            method="POST",
        )
        # Retry transient failures (server 5xx / overloaded / rate-limit / connection) with exponential
        # backoff, so a passing blip — e.g. a 500 "internal server error" — doesn't fail the whole
        # research pass. Non-transient errors (400/401/etc.) fail immediately; the detail is truncated
        # so a long HTML error page never floods the UI/console.
        last_error: ClaudeError | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
                self.last_usage = body.get("usage", {}) or {}
                return body
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")[:300]
                last_error = ClaudeError(f"Claude API error {error.code}: {detail}")
                if error.code not in TRANSIENT_STATUSES or attempt == MAX_RETRIES:
                    raise last_error from error
            except urllib.error.URLError as error:
                last_error = ClaudeError(f"Claude API connection failed: {error.reason}")
                if attempt == MAX_RETRIES:
                    raise last_error from error
            time.sleep(min(2 ** attempt, 8))  # 1s, 2s, 4s between tries
        raise last_error or ClaudeError("Claude API call failed.")  # pragma: no cover

    def complete(self, prompt: str, max_tokens: int = 1800) -> str:
        if not self.api_key:
            raise ClaudeError(
                "Claude is not configured. Save a Claude API key in Settings or set "
                f"{self.config.api_key_env}."
            )

        body = self._post(
            {
                "model": self.config.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        content_blocks = body.get("content", [])
        text_parts = [
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text" and block.get("text")
        ]
        if not text_parts:
            raise ClaudeError("Claude returned no text content.")
        return "\n".join(text_parts).strip()

    def chat(
        self,
        messages: list,
        system: str | None = None,
        tools: list | None = None,
        max_tokens: int = 1024,
        model: str | None = None,
    ) -> dict:
        """One multi-turn Messages API exchange. Returns the raw response body so the caller can
        inspect `stop_reason`, text blocks, and `tool_use` blocks and drive a tool loop.

        Prompt caching: the system prompt and the tools block are marked `cache_control:
        ephemeral`, so the (static) JARVIS persona + HELIX context + tool schemas are cached and
        re-used across the back-and-forth of a turn's tool loop and across turns in a session,
        cutting both cost and latency. The growing `messages` are the dynamic, uncached tail.
        """
        if not self.api_key:
            raise ClaudeError(
                "Claude is not configured. Save a Claude API key in Settings or set "
                f"{self.config.api_key_env}."
            )

        payload: dict = {
            "model": model or self.config.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            payload["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        if tools:
            cached_tools = [dict(tool) for tool in tools]
            cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
            payload["tools"] = cached_tools

        return self._post(payload)
