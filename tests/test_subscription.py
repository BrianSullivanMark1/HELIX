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


def test_preferred_chat_hands_a_pictures_images_to_the_subscription():
    """The hologram critic sends the rendered preview as an Image block in a plain (no-tool) call. On a
    subscription-only machine that call lands HERE, and _flatten keeps only the text — so the critic
    was about to judge a preview it could not see. The pictures must ride along, in order, as vision."""
    from helix.ports.llm import Image

    sub = _Sub()
    api = _ApiChat()
    chat = PreferredChat(sub, api)
    img = Image(media_type="image/png", data="iVBORw0KGgo=")
    reply = chat.chat([Turn(Role.USER, (img, Text("Design brief: a bracket")))], system="critic")
    assert reply.text == "hermetic reply"
    assert api.calls == 0, "an active subscription must carry the picture, not bounce to the meter"
    prompt, _names, kw = sub.hermetic_calls[0]
    assert "Design brief: a bracket" in prompt
    assert kw.get("images") == (img,), f"the Image block did not reach run_hermetic: {kw!r}"
    # A text-only distill stays exactly as it was: no images argument worth sending.
    chat.chat([Turn(Role.USER, (Text("distill this"),))])
    assert not sub.hermetic_calls[1][2].get("images")


def test_a_hermetic_run_with_images_sends_the_structured_vision_message(monkeypatch, tmp_path):
    """The same envelope the orb turn uses for an attachment — images first, then the text — handed to
    the SDK's one-shot query(). A plain string would silently drop the picture on the floor (the CLI
    would answer about a preview it never received); a text-only run must still be the plain string,
    because that is the form every CLI version understands."""
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, TextBlock

    from helix.adapters.agent_sdk_chat import SubscriptionBrain
    from helix.ports.llm import Image

    seen: list = []

    async def _fake_query(*, prompt, options):
        if isinstance(prompt, str):
            seen.append(prompt)
        else:
            seen.append([m async for m in prompt])
        yield AssistantMessage(content=[TextBlock("OK")], model="claude-sonnet-4-6")

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=str(tmp_path))
    try:
        img = Image(media_type="image/png", data="iVBORw0KGgo=")
        out = brain.run_hermetic("Design brief: a bracket", system="critic", images=[img])
        brain.run_hermetic("distill this")
    finally:
        brain.shutdown()
    assert out == "OK"
    messages = seen[0]
    assert isinstance(messages, list) and len(messages) == 1, (
        f"expected ONE structured user message, got {seen[0]!r}")
    msg = messages[0]
    assert msg["type"] == "user" and msg["message"]["role"] == "user"
    content = msg["message"]["content"]
    assert content == [
        {"type": "image",
         "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        {"type": "text", "text": "Design brief: a bracket"},
    ], f"the vision envelope drifted: {content!r}"
    assert seen[1] == "distill this", "a text-only run must stay a plain string prompt"


def _retry_brain(turn):
    """A real SubscriptionBrain with its one turn coroutine faked at the seam — no SDK, no CLI:
    only run_orb_turn's retry policy is under test."""
    from helix.adapters.agent_sdk_chat import SubscriptionBrain

    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir="C:/neutral")
    brain._orb_turn = turn
    return brain


def test_dead_session_still_gets_one_clean_retry():
    # The retry's genuine case: the turn blew up with NOTHING done yet (dead CLI, stale pipe).
    attempts = []

    async def _turn(prompt, names, sinks, history, images=None):
        attempts.append(prompt)
        if len(attempts) == 1:
            raise RuntimeError("stale pipe")
        return "second try"

    brain = _retry_brain(_turn)
    try:
        assert brain.run_orb_turn("what's up") == "second try"
        assert len(attempts) == 2
    finally:
        brain.shutdown()


def test_turn_is_never_retried_once_a_tool_has_run():
    # A build was enqueued before the failure — re-sending the turn would enqueue it TWICE.
    attempts = []
    seen = []

    async def _turn(prompt, names, sinks, history, images=None):
        attempts.append(prompt)
        sinks.on_tool("build_app", "started building", False)
        raise RuntimeError("pipe died after the build was enqueued")

    brain = _retry_brain(_turn)
    try:
        with pytest.raises(RuntimeError):
            brain.run_orb_turn("build me a timer", ("build_app",), on_tool=lambda *a: seen.append(a))
        assert len(attempts) == 1          # no replay of a turn with real side effects
        assert len(seen) == 1              # the caller's own on_tool still fired, once
    finally:
        brain.shutdown()


def test_tool_guard_holds_even_with_no_caller_on_tool():
    # The side effects happen whether or not the CALLER wanted digests — the guard can't depend on it.
    attempts = []

    async def _turn(prompt, names, sinks, history, images=None):
        attempts.append(prompt)
        sinks.on_tool("set_reminder", "reminder set", False)
        raise RuntimeError("boom")

    brain = _retry_brain(_turn)
    try:
        with pytest.raises(RuntimeError):
            brain.run_orb_turn("remind me at five", ("set_reminder",))
        assert len(attempts) == 1
    finally:
        brain.shutdown()


def test_cancelled_turn_is_never_retried():
    # The user pressed stop; the interrupt surfaces as an exception. Retrying would re-send the very
    # turn they just stopped.
    from helix.services.cancel import CancelToken

    tok = CancelToken()
    attempts = []

    async def _turn(prompt, names, sinks, history, images=None):
        attempts.append(prompt)
        tok.cancel()
        raise RuntimeError("interrupted")

    brain = _retry_brain(_turn)
    try:
        with pytest.raises(RuntimeError):
            brain.run_orb_turn("long story please", cancel=tok)
        assert len(attempts) == 1
    finally:
        brain.shutdown()


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


# ---------------------------------------------------------------------------
# THE WEB FENCE: an autonomous run gets no web tools of the model's own, on EITHER rail.
# ---------------------------------------------------------------------------


class _FakeApiClient:
    """Records the Messages API kwargs and answers with a plain, tool-free reply."""

    def __init__(self) -> None:
        self.kwargs: dict = {}

    @property
    def messages(self) -> "_FakeApiClient":
        return self

    def create(self, **kwargs):
        from types import SimpleNamespace

        self.kwargs = kwargs
        usage = SimpleNamespace(
            input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="all done")],
            stop_reason="end_turn", usage=usage,
        )


def test_an_autonomous_turn_on_the_api_rail_is_never_offered_the_web_tools(monkeypatch):
    """A watcher chews on untrusted content, so it must not get Anthropic's server-side
    web_search/web_fetch — that is an egress channel around call_api's allowlist and scrubbing. The
    subscription rail has always fenced it; the API rail served orb turns and watcher turns from ONE
    web-enabled chat, so a token-less user's every agent run was handed the web."""
    from helix.adapters.anthropic_chat import AnthropicChat

    fake = _FakeApiClient()
    monkeypatch.setattr(AnthropicChat, "_client_for_current_key", lambda self: fake)
    api = AnthropicChat(lambda: "sk-test", web_search=True)
    svc, _store, _api = _service(None, api=api)  # no subscription: every turn lands on the API rail

    svc.run_turn("summarize that Slack thread", allow_builds=False, persist=False)
    kinds = [t.get("type", "") for t in fake.kwargs["tools"]]
    assert not any(k.startswith("web_") for k in kinds), f"an autonomous run was handed {kinds}"
    assert "list_apps" in [t.get("name") for t in fake.kwargs["tools"]]  # its own tools still there

    svc.run_turn("what's the weather in Paris?")  # a human at the orb keeps the web
    assert fake.kwargs["tools"][0]["type"].startswith("web_search")


def test_an_autonomous_turn_tells_the_subscription_rail_to_withhold_the_web_too(monkeypatch):
    # Same rule, stated out loud on the other rail rather than left to run_hermetic's default — one
    # fence, both rails, so they cannot drift apart again.
    sub = _Sub()
    svc, _store, _api = _service(sub)
    svc.run_turn("summarize that Slack thread", allow_builds=False, persist=False)
    _prompt, _names, kw = sub.hermetic_calls[0]
    assert kw.get("web") is False


def test_a_hermetic_run_narrates_tool_milestones_not_the_models_half_formed_prose(monkeypatch,
                                                                                  tmp_path):
    """Agent runs and think_harder both stream their progress to the Console status line AND the
    voice. Pushing the model's first interim sentence there made HELIX read its own thinking aloud —
    the very thing the orb path was fixed to stop doing. Only tool milestones surface, in the
    friendly phrase both rails use."""
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, TextBlock, ToolUseBlock

    from helix.adapters.agent_sdk_chat import SubscriptionBrain

    async def _fake_query(*, prompt, options):
        yield AssistantMessage(
            content=[TextBlock("Hmm, let me work out whether the inbox even matters here."),
                     ToolUseBlock("t1", "mcp__helix__check_email", {})],
            model="claude-sonnet-4-6",
        )
        yield AssistantMessage(
            content=[TextBlock("Two new emails, both from Dave.")], model="claude-sonnet-4-6"
        )

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    lines: list[str] = []
    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=str(tmp_path))
    try:
        out = brain.run_hermetic("what's in my inbox?", on_progress=lines.append)
    finally:
        brain.shutdown()
    assert out == "Two new emails, both from Dave."
    assert lines == ["Checking your inbox…"], f"narrated {lines}"


def test_the_deep_reasoners_api_fallback_names_the_real_subscription_failure():
    """think_harder falls back to a BARE AnthropicChat, which can only say "check your token". When
    the subscription rail is structurally perfect and the turn still died, that sends the user off to
    re-issue a credential that had just worked — so the fallback must say what actually happened."""
    from helix.app.container import _deep_think_on_api
    from helix.domain.errors import MissingApiKey

    class _NoKeyChat:
        def chat(self, turns, system=None, tools=None):
            raise MissingApiKey(
                "Claude isn't reachable right now. Check your subscription token (run "
                "claude setup-token) or add a Claude API key in Settings."
            )

    class _StructurallyPerfectSub:
        def why_inactive(self):
            return None  # token saved, SDK present, a CLI that launches

        def last_failure(self):
            return "The filename or extension is too long"

    with pytest.raises(MissingApiKey) as caught:
        _deep_think_on_api(_NoKeyChat(), _StructurallyPerfectSub(), "think that through properly")
    said = str(caught.value)
    assert "The filename or extension is too long" in said
    assert "Check your subscription token" not in said, "still blaming a token that was never wrong"


def test_an_unmapped_tools_progress_line_trails_off_exactly_once():
    # The unmapped fallback already ends in an ellipsis, so appending another printed "Working……".
    assert ConversationService._progress_label("some_new_tool", {}) == "Working…"
    assert ConversationService._progress_label("check_email", {}) == "Checking your inbox…"


def test_the_production_chat_shape_fences_an_autonomous_turn(monkeypatch):
    """The shape the CONTAINER actually wires: ConversationService gets a PreferredChat, not a bare
    AnthropicChat. So the whole web fence rests on PreferredChat.without_web() forwarding the shed to
    its API leg — and until now nothing exercised that hop, which is exactly how the original hole
    (every watcher turn on the API key handed web_search/web_fetch) survived review."""
    from helix.adapters.anthropic_chat import AnthropicChat

    fake = _FakeApiClient()
    monkeypatch.setattr(AnthropicChat, "_client_for_current_key", lambda self: fake)
    # An INACTIVE subscription is the token-less user's everyday case: every turn lands on the API leg.
    preferred = PreferredChat(_Sub(active=False), AnthropicChat(lambda: "sk-test", web_search=True))
    svc, _store, _api = _service(_Sub(active=False), api=preferred)

    svc.run_turn("summarize that Slack thread", allow_builds=False, persist=False)
    kinds = [t.get("type", "") for t in fake.kwargs["tools"]]
    assert not any(k.startswith("web_") for k in kinds), f"an autonomous run was handed {kinds}"
    assert "list_apps" in [t.get("name") for t in fake.kwargs["tools"]]  # its own tools still there

    svc.run_turn("what's the weather in Paris?")  # a human at the orb still gets the web
    assert fake.kwargs["tools"][0]["type"].startswith("web_search"), "the fence shed too much"


@pytest.fixture(scope="module")
def _qt_app():
    """One QApplication for this module — constructing the real Container touches Qt, and a second
    QApplication in the same process aborts the interpreter."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # must be set BEFORE PyQt6 is imported
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def container(_qt_app, tmp_path, monkeypatch):
    """The real composition root, pointed at a throwaway data dir so the user's own data is never
    touched. It has to be the real thing: the wiring IS what's under test, and a hand-built stand-in
    would happily reproduce whatever the container actually does wrong."""
    import helix.config as config

    root = config.AppPaths.resolve().root  # the repo itself — read-only for our purposes
    monkeypatch.setattr(
        config.AppPaths, "resolve",
        staticmethod(lambda: config.AppPaths(root=root, data=tmp_path)),
    )
    from helix.app.container import Container

    return Container()


def test_the_unattended_services_are_wired_to_a_web_less_chat(container):
    """The distillers and Evolve think with NOBODY watching, over text HELIX didn't write (a Slack
    thread pasted into the transcript, an email body, the log tail). ConversationService sheds the web
    per turn for its agent runs; these have no human turn to distinguish, so the container must hand
    them the shed twin at wiring time. It handed them the web-enabled `self.chat` instead."""
    unattended = {
        "profile": container.profile, "lessons": container.lessons,
        "user_memory": container.user_memory, "voice_id": container.voice_id,
        "evolve": container.evolve,
    }
    open_fence = [
        name for name, svc in unattended.items()
        if getattr(getattr(svc, "_chat", None), "_api", None) is None
        or svc._chat._api._web_search
    ]
    assert not open_fence, f"unattended services still hold a web-enabled chat: {open_fence}"
    # ...and the orb's own chat KEEPS it: a human asking "what's the weather?" must still be able to
    # search. Without this the fix could be "shed everything" and nobody would notice.
    assert container.chat._api._web_search is True


def test_a_stopped_turn_is_not_recorded_as_a_rail_failure():
    """Pressing Stop surfaces as an ordinary exception from the interrupted turn. Settings shows
    last_failure() to the user as an amber "your subscription is having trouble" warning, so
    recording a cancellation would accuse a perfectly healthy rail — and keep accusing it until some
    later turn happened to succeed."""
    from helix.services.cancel import CancelToken

    tok = CancelToken()

    async def _turn(prompt, names, sinks, history, images=None):
        tok.cancel()
        raise RuntimeError("interrupted")

    brain = _retry_brain(_turn)
    try:
        with pytest.raises(RuntimeError):
            brain.run_orb_turn("long story please", cancel=tok)
        assert brain.last_failure() is None, f"a user cancel was blamed on the rail: {brain.last_failure()}"
    finally:
        brain.shutdown()


def test_a_genuine_failure_is_still_recorded():
    # The other half of the guard above: an uncancelled turn that dies must STILL name its cause, or
    # the "no rail" message goes back to blaming the token.
    async def _turn(prompt, names, sinks, history, images=None):
        raise RuntimeError("The filename or extension is too long")

    brain = _retry_brain(_turn)
    try:
        with pytest.raises(RuntimeError):
            brain.run_orb_turn("hello")
        assert brain.last_failure() == "The filename or extension is too long"
    finally:
        brain.shutdown()


def test_the_no_rail_message_closes_the_failure_sentence():
    """raise_no_rail glues "There's no Claude API key set either…" onto the reason. A raw exception
    string carries no punctuation of its own, so the two ran together into one breathless run-on
    sentence — which the voice reads without a pause."""
    from helix.adapters.agent_sdk_chat import raise_no_rail
    from helix.domain.errors import MissingApiKey

    class _StructurallyPerfectSub:
        def why_inactive(self):
            return None

        def last_failure(self):
            return "The filename or extension is too long"

    with pytest.raises(MissingApiKey) as caught:
        raise_no_rail(_StructurallyPerfectSub(), MissingApiKey("no key"))
    said = str(caught.value)
    assert "too long. There's no Claude API key" in said, said
    assert "too long There's" not in said


def test_a_failure_that_already_ends_in_punctuation_is_not_double_stopped():
    from helix.adapters.agent_sdk_chat import raise_no_rail
    from helix.domain.errors import MissingApiKey

    class _Sub2:
        def why_inactive(self):
            return None

        def last_failure(self):
            return "The plan's usage limit was reached."

    with pytest.raises(MissingApiKey) as caught:
        raise_no_rail(_Sub2(), MissingApiKey("no key"))
    assert "reached.." not in str(caught.value)


def test_the_subscription_rail_narrates_a_build_by_name(monkeypatch, tmp_path):
    """Both rails, one voice. The API rail has said "Building Tip Calculator…" all along; this rail
    said "Building that…" for the same build, because the personalization keyed off the raw tool
    name and this rail's names arrive MCP-prefixed (`mcp__helix__build_app`)."""
    pytest.importorskip("claude_agent_sdk")
    import claude_agent_sdk
    from claude_agent_sdk import AssistantMessage, ToolUseBlock

    from helix.adapters.agent_sdk_chat import SubscriptionBrain

    async def _fake_query(*, prompt, options):
        yield AssistantMessage(
            content=[ToolUseBlock("t1", "mcp__helix__build_app", {"name": "Tip Calculator"})],
            model="claude-sonnet-4-6",
        )

    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query)
    lines: list[str] = []
    brain = SubscriptionBrain(lambda: "sk-ant-oat01-fake", "sys", workdir=str(tmp_path))
    try:
        brain.run_hermetic("build me a tip calculator", on_progress=lines.append)
    finally:
        brain.shutdown()
    assert lines == ["Building Tip Calculator…"], f"narrated {lines}"
