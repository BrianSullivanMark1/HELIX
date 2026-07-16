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
class Image:
    """An image the model SEES (vision). `data` is base64-encoded image bytes; `media_type` is an
    Anthropic-supported type: image/png, image/jpeg, image/webp, or image/gif. Appears in a USER turn
    (an attachment) or inside a tool result (an image HELIX located on disk) — always data it looks
    at, never something it emits."""

    media_type: str
    data: str


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False
    images: tuple[Image, ...] = ()  # images the tool handed back for the model to SEE (e.g. a located
    #                                 photo) — carried into the tool_result content, never persisted


@dataclass(frozen=True)
class ToolOutput:
    """A tool's result that includes IMAGES for the model to see, not just text — for tools like
    find_images / view_image that hand back pixels. A plain tool just returns a str; `text` is the
    digest that's persisted/narrated, `images` are shown to the model this turn only."""

    text: str
    images: tuple[Image, ...] = ()


Block = Text | ToolUse | ToolResult | Image


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
