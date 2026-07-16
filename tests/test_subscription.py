"""The subscription brain — turns on the user's Claude plan instead of API billing.

No SDK, no CLI, no network: the brain is faked at the seam ConversationService uses. Invariants:
with an ACTIVE subscription the orb turn routes through it (persisting the user text, the reply,
and tool digests exactly like the API path — including the ephemeral context extras in the prompt);
agent runs route hermetic with BUILD_TOOLS already filtered; a subscription failure falls back to
the API loop instead of failing the turn; with no token nothing changes at all; and PreferredChat
sends no-tool calls to the subscription but tool-using calls always to the API chat.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from helix.adapters.agent_sdk_chat import PreferredChat
from helix.domain.models import Message, Role
from helix.ports.llm import Reply, Text, ToolSpec, Turn
from helix.services.conversation import BUILD_TOOLS, ConversationService


def _msg(role, text):
    return Message(role, text, datetime(2026, 7, 10, 9, 0, 0).astimezone())


class _FixedClock:
    def now(self):
        return datetime(2026, 7, 10, 9, 0, 0).astimezone()


class _Store:
    def __init__(self):
        self.messages = []

    def append(self, msg):
        self.messages.append(msg)

    def recent(self, limit):
        return list(self.messages)[-limit:]

    def record_usage(self, *a):
        pass


class _Tools:
    def __init__(self, names=("build_app", "list_apps")):
        self._specs = [ToolSpec(n, f"{n} tool", {"type": "object"}) for n in names]

    def specs(self):
        return list(self._specs)

    def dispatch(self, name, args, on_progress=None, cancel=None, user=""):
        return "dispatched"


class _ApiChat:
    def __init__(self):
        self.calls = 0

    def chat(self, turns, system=None, tools=None):
        self.calls += 1
        return Reply(blocks=(Text("api reply"),))


class _Sub:
    def __init__(self, active=True, fail=False, dispatch_before_fail=False):
        self._active = active
        self._fail = fail
        self._dispatch_before_fail = dispatch_before_fail
        self.orb_calls = []      # (prompt, names, history)
        self.hermetic_calls = []

    def active(self):
        return self._active

    def run_orb_turn(self, prompt, names, *, history="", on_progress=None, cancel=None, on_tool=None,
                     user="", images=None):
        if self._fail:
            if self._dispatch_before_fail and on_tool is not None:
                on_tool("build_app", "started building")  # a real side effect happened first
            raise RuntimeError("plan limit reached")
        self.orb_calls.append((prompt, tuple(names), history))
        self.last_images = images  # so a test can assert attached images reached the subscription
        if on_tool is not None:
            on_tool("list_apps", "two apps")
        return "subscription reply"

    def run_hermetic(self, prompt, names=(), **kw):
        self.hermetic_calls.append((prompt, tuple(names), kw))
        return "hermetic reply"


def _service(sub, tools=None, api=None):
    store = _Store()
    api = api or _ApiChat()
    svc = ConversationService(
        api, tools or _Tools(), store, store, _FixedClock(), "sys", subscription=sub,
    )
    return svc, store, api


def test_orb_turn_routes_through_active_subscription():
    sub = _Sub()
    svc, store, api = _service(sub)
    out = svc.run_turn("what's up")
    assert out == "subscription reply"
    assert api.calls == 0  # never touched the API meter
    prompt, names, _hist = sub.orb_calls[0]
    assert prompt.startswith("what's up")
    assert "Current date & time" in prompt  # the ephemeral extras ride in the prompt
    assert "build_app" in names             # orb turns keep the full tool surface
    roles = [m.role for m in store.messages]
    assert Role.USER in roles and Role.ASSISTANT in roles  # persisted like any turn
    assert any(m.role == Role.TOOL for m in store.messages)  # the tool digest was remembered


def test_attached_images_reach_the_subscription_turn():
    from helix.ports.llm import Image
    sub = _Sub()
    svc, _store, api = _service(sub)
    img = Image(media_type="image/jpeg", data="Zm9v")
    out = svc.run_turn("what's in this photo?", images=[img])
    assert out == "subscription reply"
    assert api.calls == 0
    assert sub.last_images == [img]  # the vision block rode into the subscription turn


def test_agent_run_routes_hermetic_with_builds_denied():
    sub = _Sub()
    svc, store, _api = _service(sub)
    out = svc.run_turn("watch things", allow_builds=False, persist=False)
    assert out == "hermetic reply"
    _prompt, names, _kw = sub.hermetic_calls[0]
    assert "build_app" not in names and not (set(names) & BUILD_TOOLS)
    assert "list_apps" in names
    assert store.messages == []  # hermetic: nothing persisted


def test_subscription_failure_falls_back_to_the_api_loop():
    sub = _Sub(fail=True)
    svc, store, api = _service(sub)
    out = svc.run_turn("hello")
    assert out == "api reply"
    assert api.calls == 1  # the safety net ran


def test_inactive_subscription_changes_nothing():
    sub = _Sub(active=False)
    svc, _store, api = _service(sub)
    out = svc.run_turn("hello")
    assert out == "api reply"
    assert sub.orb_calls == [] and sub.hermetic_calls == []
    assert api.calls == 1


def test_failure_after_tools_ran_does_not_rerun_on_the_api():
    # A build was already enqueued; a mid-turn failure must NOT re-run the whole turn (double build).
    sub = _Sub(fail=True, dispatch_before_fail=True)
    svc, _store, api = _service(sub)
    out = svc.run_turn("build me a timer")
    assert api.calls == 0  # never re-ran — the side effect already happened
    assert "snag" in out.lower() or "went through" in out.lower()


def test_pre_cancelled_turn_never_queries_the_subscription():
    from helix.services.cancel import CancelToken

    sub = _Sub()
    svc, _store, api = _service(sub)
    tok = CancelToken()
    tok.cancel()
    out = svc.run_turn("hello", cancel=tok)
    assert sub.orb_calls == [] and api.calls == 0  # neither brain was queried
    from helix.services.conversation import STOPPED_REPLY
    assert out == STOPPED_REPLY


def test_fresh_session_gets_recent_history_seed():
    sub = _Sub()
    store = _Store()
    # prior conversation already in the store
    store.append(_msg(Role.USER, "what's the capital of France"))
    store.append(_msg(Role.ASSISTANT, "Paris."))
    svc = ConversationService(_ApiChat(), _Tools(), store, store, _FixedClock(), "sys",
                              subscription=sub)
    svc.run_turn("and its population?")
    _prompt, _names, history = sub.orb_calls[0]
    assert "capital of France" in history and "Paris" in history
    assert "and its population" not in history  # THIS turn's line isn't echoed into its own history seed


def test_preferred_chat_prefers_subscription_for_plain_calls():
    sub = _Sub()
    api = _ApiChat()
    chat = PreferredChat(sub, api)
    reply = chat.chat([Turn(Role.USER, (Text("distill this"),))], system="distiller")
    assert reply.text == "hermetic reply"
    assert api.calls == 0
    prompt, _names, kw = sub.hermetic_calls[0]
    assert "distill this" in prompt
    assert kw.get("system") == "distiller"  # the distiller's own persona rides as the system prompt


def test_preferred_chat_sends_tool_calls_to_the_api():
    sub = _Sub()
    api = _ApiChat()
    chat = PreferredChat(sub, api)
    reply = chat.chat([Turn(Role.USER, (Text("hi"),))],
                      tools=[ToolSpec("t", "d", {"type": "object"})])
    assert reply.text == "api reply"
    assert sub.hermetic_calls == []  # the raw tool round-trip only exists on the API


def test_preferred_chat_falls_back_when_subscription_fails():
    sub = _Sub(fail=True)
    sub.run_hermetic = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    api = _ApiChat()
    chat = PreferredChat(sub, api)
    reply = chat.chat([Turn(Role.USER, (Text("hi"),))])
    assert reply.text == "api reply"


def test_child_windows_are_hidden_on_windows():
    # The blank claude.exe console popup fix: our wrapper must OR CREATE_NO_WINDOW into the
    # anyio.open_process call the SDK uses to spawn claude.exe.
    import sys
    if sys.platform != "win32":
        pytest.skip("window suppression is Windows-only")
    import anyio
    import helix.adapters.agent_sdk_chat as mod

    captured = {}

    async def _fake_open_process(command, **kwargs):
        captured.update(kwargs)
        return "proc"

    orig = anyio.open_process
    mod._windows_patched = False  # force a fresh patch over our fake
    anyio.open_process = _fake_open_process
    try:
        mod._hide_child_windows()
        import asyncio
        asyncio.new_event_loop().run_until_complete(anyio.open_process(["x"], env={}))
        flag = mod._NO_WINDOW
        assert captured.get("creationflags", 0) & flag == flag, "CREATE_NO_WINDOW not applied"
    finally:
        anyio.open_process = orig
        mod._windows_patched = False


def test_sdk_options_are_token_safe_and_sandboxed():
    # The ClaudeAgentOptions the brain builds must (a) NEVER pass --bare (it disables
    # subscription-token auth — a real regression we hit), (b) keep --no-session-persistence, (c)
    # run in a neutral cwd, (d) offer web tools only, (e) clear ANTHROPIC_API_KEY so an inherited
    # key can't silently bill the API, (f) load no filesystem settings.
    pytest.importorskip("claude_agent_sdk")
    from helix.adapters.agent_sdk_chat import _Sinks, SubscriptionBrain

    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir="C:/neutral")
    opts = brain._options((), "claude-sonnet-4-6", "low", _Sinks())
    assert "bare" not in (opts.extra_args or {}), "--bare breaks subscription-token auth"
    assert "no-session-persistence" in (opts.extra_args or {})
    assert str(opts.cwd) == "C:/neutral"
    assert opts.tools == ["WebSearch", "WebFetch"]
    for shell in ("Bash", "Edit", "Write", "Read"):
        assert shell in opts.disallowed_tools
    assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-fake"
    assert opts.env.get("ANTHROPIC_API_KEY") == ""  # inherited key must not reach claude.exe
    assert opts.setting_sources == []


def test_web_tools_denied_for_autonomous_runs():
    # Agents/watchers/distillers (web=False) get NO arbitrary web fetch — a WebFetch would be an egress
    # channel bypassing call_api's host allowlist. The orb/think_harder (web=True) still get web.
    pytest.importorskip("claude_agent_sdk")
    from helix.adapters.agent_sdk_chat import _Sinks, SubscriptionBrain

    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir="C:/neutral")
    no_web = brain._options((), "claude-sonnet-4-6", "low", _Sinks(), web=False)
    assert no_web.tools == []                                   # no built-in web tools available
    assert "WebFetch" in no_web.disallowed_tools and "WebSearch" in no_web.disallowed_tools
    assert not any("Web" in t for t in no_web.allowed_tools)

    web = brain._options((), "claude-sonnet-4-6", "low", _Sinks(), web=True)
    assert web.tools == ["WebSearch", "WebFetch"]               # user-driven runs keep web
