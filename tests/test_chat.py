"""AnthropicChat tests — web-search wiring, with the SDK client mocked (no network)."""
from __future__ import annotations

from types import SimpleNamespace

from helix.adapters.anthropic_chat import AnthropicChat, _estimate_cost
from helix.domain.models import Role
from helix.ports.llm import Text, Turn


class _FakeClient:
    """Records the create() kwargs and returns a canned response."""

    def __init__(self, response: object) -> None:
        self._response = response
        self.kwargs: dict = {}

    @property
    def messages(self) -> "_FakeClient":
        return self

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return self._response


def _response() -> object:
    usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    # A web-search turn: server-tool blocks (which HELIX must ignore) interleaved with the answer text.
    content = [
        SimpleNamespace(type="server_tool_use", id="srv1", name="web_search", input={"query": "q"}),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="srv1", content=[]),
        SimpleNamespace(type="text", text="It is sunny in Paris today."),
    ]
    return SimpleNamespace(content=content, stop_reason="end_turn", usage=usage)


def _chat(web_search: bool) -> tuple[AnthropicChat, _FakeClient]:
    chat = AnthropicChat(lambda: "sk-test", web_search=web_search)
    fake = _FakeClient(_response())
    chat._client_for_current_key = lambda: fake  # type: ignore[method-assign]
    return chat, fake


def test_web_search_tools_are_added_when_enabled():
    chat, fake = _chat(web_search=True)
    reply = chat.chat([Turn(Role.USER, (Text("weather in Paris?"),))], system="sys")
    tools = fake.kwargs["tools"]
    assert tools[0]["type"] == "web_search_20260209"
    assert tools[1]["type"] == "web_fetch_20260209"
    # Server-tool blocks are ignored; only the answer text comes through, and it's not a client tool call.
    assert reply.text == "It is sunny in Paris today."
    assert not reply.wants_tools


def test_no_web_tools_when_disabled():
    chat, fake = _chat(web_search=False)
    chat.chat([Turn(Role.USER, (Text("hello"),))])
    assert "tools" not in fake.kwargs  # no tools at all when none passed and web search off


def test_thinking_and_effort_pass_through():
    chat = AnthropicChat(
        lambda: "sk-test", model="claude-sonnet-4-6", thinking="disabled", effort="low"
    )
    fake = _FakeClient(_response())
    chat._client_for_current_key = lambda: fake  # type: ignore[method-assign]
    chat.chat([Turn(Role.USER, (Text("hi"),))])
    assert fake.kwargs["model"] == "claude-sonnet-4-6"
    assert fake.kwargs["thinking"] == {"type": "disabled"}
    assert fake.kwargs["output_config"] == {"effort": "low"}


def test_no_thinking_or_effort_by_default():
    chat, fake = _chat(web_search=False)
    chat.chat([Turn(Role.USER, (Text("hi"),))])
    assert "thinking" not in fake.kwargs
    assert "output_config" not in fake.kwargs


def test_cost_estimate_is_model_aware():
    u = SimpleNamespace(
        input_tokens=1_000_000, output_tokens=0,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    assert abs(_estimate_cost("claude-opus-4-8", u) - 5.0) < 1e-6
    assert abs(_estimate_cost("claude-sonnet-4-6", u) - 3.0) < 1e-6
    assert abs(_estimate_cost("claude-haiku-4-5", u) - 1.0) < 1e-6
    assert abs(_estimate_cost("unknown-model", u) - 5.0) < 1e-6  # falls back to Opus rate
