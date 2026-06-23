"""ChatModel port — a provider-neutral conversation contract that supports tool use.

The transcript is a list of `Turn`s; each Turn carries content blocks (text, a tool call, or a tool
result). This is rich enough for the full model↔tools loop yet free of any Anthropic specifics, which
live entirely in adapters/anthropic_chat.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from helix.domain.models import Role


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


Block = Text | ToolUse | ToolResult


@dataclass(frozen=True)
class Turn:
    role: Role
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Reply:
    """One model turn's output: text and/or tool calls, plus token usage."""

    blocks: tuple[Block, ...]
    usage: Usage = field(default_factory=Usage)

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.blocks if isinstance(b, Text))

    @property
    def tool_uses(self) -> tuple[ToolUse, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUse))

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_uses)


class ChatModel(Protocol):
    def chat(
        self,
        turns: list[Turn],
        *,
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> Reply: ...
