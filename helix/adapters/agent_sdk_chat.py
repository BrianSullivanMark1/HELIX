"""Subscription brain — HELIX's conversation on the user's Claude subscription, not the API meter.

Runs turns through the Claude Agent SDK (the Python wrapper around the same `claude.exe` the coder
already uses), authenticated with the user's Claude Code OAuth token (`claude setup-token`). On that
path every call draws on the Claude Pro/Max subscription — the same usage pool as Claude Desktop —
instead of pay-per-token Console billing. This is the sanctioned single-user pattern: the official
SDK/CLI, the user's own token, one person's assistant. The token never leaves this machine and is
only ever handed to the local `claude` subprocess.

Design:
  - The ORB keeps one persistent SDK session (fast follow-up turns, CLI-side caching + compaction);
    HELIX's tools ride into it as in-process MCP tools whose callbacks dispatch straight into
    ToolRegistry. Built-in Claude Code tools are OFF except WebSearch/WebFetch (and the file/shell
    tools are additionally disallowed) — any disk access the orb has goes through HELIX's OWN
    audited file tools on the MCP bridge (private-zone checks, the Settings write toggle, fenced
    output), never through the SDK's raw Read/Write/Bash, which would bypass every one of those
    guards. Isolation: `setting_sources=[]` (no user/project settings, MCP servers, or hooks), a
    NEUTRAL working dir with no CLAUDE.md (so no project instructions auto-load), and
    `--no-session-persistence` (the untrusted transcript is not written to ~/.claude/projects).
    NOTE: `--bare` would also block the CLI's own user-level auto-memory, but it DISABLES
    subscription-token auth outright ("Not logged in"), so it is NOT used — the user's own
    auto-memory may load, an accepted low-risk tradeoff on a single-user machine.
  - Agents/watchers and the distillers run hermetic one-shot `query()` calls (no session, no
    persistence). Each carries its OWN sinks in its closure — NO shared per-run state and NO global
    lock — so an orb turn, a background watcher, a distiller, and a mid-turn think_harber can all run
    at once without cross-wiring or deadlocking (a bridged tool that re-enters the brain is exactly
    this case and must never block on a lock the outer turn holds).
  - Everything is OPTIONAL and degrades: no token, no CLI, or no SDK → `active()` is False and the
    existing API-key path runs exactly as before.

Threading: the SDK is asyncio; HELIX is Qt threads. One daemon thread owns an event loop for the
brain's lifetime; public methods are synchronous and marshal onto it. Orb turns are serialized by
the Console (`_busy`), so the single persistent orb session sees one turn at a time.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable

from helix.adapters.claude_code_cli import resolve_claude_cli
from helix.domain.vocabulary import friendly_tool_label
from helix.logging_setup import get_logger
from helix.ports.llm import Reply, Text, ToolOutput, Turn, Usage

_LOG = get_logger("subscription")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_windows_patched = False


def _hide_child_windows() -> None:
    """The Agent SDK spawns claude.exe via anyio.open_process WITHOUT the no-window flag, so on
    Windows a blank console window flashes up on every turn (HELIX runs windowed, like the coder
    adapter which already passes CREATE_NO_WINDOW). Wrap anyio.open_process ONCE to OR in that flag —
    idempotent, no-op off Windows or if anyio is absent. Must run before the SDK's first spawn (which
    includes its own `claude --version` check), so callers invoke it before connect()/query()."""
    global _windows_patched
    if _windows_patched or sys.platform != "win32":
        _windows_patched = True
        return
    try:
        import anyio

        orig = anyio.open_process
        if getattr(orig, "_helix_no_window", False):
            _windows_patched = True
            return

        async def _no_window(command, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
            return await orig(command, **kwargs)

        _no_window._helix_no_window = True
        anyio.open_process = _no_window
        _windows_patched = True
    except Exception:  # noqa: BLE001 — a failed patch just means the window may flash; never fatal
        pass

ORB_MODEL = "claude-sonnet-4-6"   # fast conversational turns (mirrors the API path's tiering)
DEEP_MODEL = "claude-opus-4-8"    # think_harder escalation + hermetic heavy lifting
_TURN_TIMEOUT_S = 600.0           # one orb turn (tools included) must land inside this
_MAX_TURNS = 8                    # agentic loop cap per query (mirrors MAX_STEPS + headroom)

TOKEN_SETTING = "claude_code_oauth_token"


def sdk_importable() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except Exception:
        return False


def _image_message(prompt: str, images):
    """An async iterable yielding ONE user message whose content is the attached images followed by the
    text — the stream-json envelope the Agent SDK writes to the CLI (confirmed against the SDK source:
    string prompts wrap as {"type":"user","message":{"role":"user","content": ...}}). Images first, then
    the question, so the model reads the picture and answers about it. Base64 image blocks are the only
    image mechanism on the subscription/CLI path (no Files API)."""
    content = [
        {"type": "image",
         "source": {"type": "base64", "media_type": im.media_type, "data": im.data}}
        for im in images
    ]
    content.append({"type": "text", "text": prompt})

    async def _gen():
        yield {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    return _gen()


@dataclass
class _Sinks:
    """One run's callbacks — carried in the bridge closure, never shared between runs."""
    on_progress: Callable[[str], None] | None = None
    cancel: object | None = None
    on_tool: Callable[[str, str], None] | None = None
    user: str = ""  # the recognized speaker key, so a bridged write tool records to the right person


class SubscriptionBrain:
    """The orb's (and agents') turns over the Claude Agent SDK on the user's subscription token."""

    def __init__(
        self,
        oauth_provider: Callable[[], str | None],
        system: str,
        tools=None,  # ToolRegistry | None — specs() + dispatch(); bridged as in-process MCP tools
        workdir: str | None = None,  # a neutral cwd (no CLAUDE.md) so project files can't leak in
    ) -> None:
        self._oauth = oauth_provider
        self._system = system
        self._tools = tools
        self._workdir = workdir
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False
        self._client = None            # the persistent orb ClaudeSDKClient (loop thread only)
        self._client_tools: tuple = () # tool names the live client was built with
        self._orb_sinks = _Sinks()     # the orb session's bridge reads THIS (orb turns are serialized)
        self._orb_lock = threading.Lock()  # serialize orb turns only (the Console already does, belt)

    # ----- availability -----
    def token(self) -> str:
        return (self._oauth() or "").strip()

    def active(self) -> bool:
        """True when HELIX should run conversation on the subscription: token + CLI + SDK present."""
        return (not self._closed and bool(self.token())
                and sdk_importable() and resolve_claude_cli() is not None)

    # ----- the event loop thread -----
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._closed:
            raise RuntimeError("subscription brain is shut down")
        if self._loop is None or not (self._thread and self._thread.is_alive()):
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, daemon=True, name="helix-subscription"
            )
            self._thread.start()
        return self._loop

    def _run(self, coro, timeout: float = _TURN_TIMEOUT_S):
        """Run a coroutine on the brain's loop and block for the result. On timeout the coroutine is
        cancelled (not abandoned) so it can't keep a dead SDK consumer parked on the session."""
        future = asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()  # unpark the abandoned consumer instead of leaking it onto the session
            raise

    # ----- options / tool bridge -----
    def _bridged_tools(self, names: tuple[str, ...], sinks: _Sinks):
        """HELIX's ToolRegistry tools as in-process SDK MCP tools. The callback dispatches on a worker
        thread (dispatch is sync + may block) and feeds THIS run's sinks (captured in the closure —
        no shared state), so concurrent runs never cross-wire progress or digests."""
        from claude_agent_sdk import tool

        sdk_tools = []
        for spec in (self._tools.specs() if self._tools is not None else []):
            if spec.name not in names:
                continue

            def _make(spec_name: str):
                async def _call(args: dict):
                    def _dispatch():
                        out = self._tools.dispatch(
                            spec_name, args or {},
                            on_progress=sinks.on_progress, cancel=sinks.cancel, user=sinks.user,
                        )
                        # A tool may hand back IMAGES for the model to see (find_images / view_image) —
                        # those become MCP image content blocks the CLI forwards to the model as vision;
                        # the digest/narration uses the text part only.
                        if isinstance(out, ToolOutput):
                            content = [{"type": "text", "text": out.text}] if out.text else []
                            content += [
                                {"type": "image", "data": im.data, "mimeType": im.media_type}
                                for im in out.images
                            ]
                            digest = out.text
                        else:
                            digest = str(out)
                            content = [{"type": "text", "text": digest}]
                        if sinks.on_tool is not None:
                            sinks.on_tool(spec_name, digest)
                        return content

                    result = await asyncio.to_thread(_dispatch)
                    return {"content": result}

                return _call

            sdk_tools.append(
                tool(spec.name, spec.description, spec.input_schema)(_make(spec.name))
            )
        return sdk_tools

    def _options(self, tool_names: tuple[str, ...], model: str, effort: str, sinks: _Sinks,
                 system: str | None = None, web: bool = True):
        from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

        mcp = {}
        allowed = []
        if tool_names and self._tools is not None:
            mcp["helix"] = create_sdk_mcp_server(
                name="helix", version="1.0.0", tools=self._bridged_tools(tool_names, sinks)
            )
            allowed = [f"mcp__helix__{n}" for n in tool_names]
        # WebSearch/WebFetch are the ONLY built-in tools we ever allow, and only for USER-driven runs
        # (the orb, think_harder). AUTONOMOUS runs (agents/watchers, distillers) get web=False: they
        # act on untrusted content (Slack/GitHub/email/notes), and an arbitrary-URL fetch would be an
        # egress channel that bypasses call_api's host-allowlist + redirect-refusal + secret scrubbing.
        # Those runs reach services only through the audited HELIX tools on the MCP bridge.
        web_tools = ["WebSearch", "WebFetch"] if web else []
        # The token is the ONLY credential the child may see. An inherited ANTHROPIC_API_KEY would
        # otherwise reach claude.exe (the SDK merges os.environ) and the CLI PREFERS the key — silently
        # billing the API instead of the subscription — so clear it, exactly like the coder adapter.
        env = {"CLAUDE_CODE_OAUTH_TOKEN": self.token(), "ANTHROPIC_API_KEY": ""}
        return ClaudeAgentOptions(
            system_prompt=system or self._system,
            model=model,
            effort=effort,
            # Web only (and only when `web`) — never the file/shell tools. `tools` sets the AVAILABLE
            # built-in set (CLI --tools), so everything else is absent; disallowed_tools is belt-and-braces
            # and additionally denies the web tools outright when they aren't granted.
            tools=web_tools,
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Glob", "Grep"]
            + ([] if web else ["WebSearch", "WebFetch"]),
            mcp_servers=mcp,
            allowed_tools=allowed + web_tools,
            setting_sources=[],   # no user/project settings, MCP servers, or hooks
            # --no-session-persistence: the untrusted transcript is never written to ~/.claude/
            # projects. (NOT --bare — it breaks subscription-token auth; see the module docstring.)
            extra_args={"no-session-persistence": None},
            cwd=self._workdir,    # neutral dir: no project CLAUDE.md to auto-discover
            max_turns=_MAX_TURNS,
            env=env,
            cli_path=resolve_claude_cli(),
        )

    # ----- the orb session -----
    async def _ensure_client(self, tool_names: tuple[str, ...]) -> tuple[object, bool]:
        """Return (client, was_fresh). `was_fresh` is True when this call built a NEW session — the
        caller seeds it with recent history so a restart doesn't wipe the model's memory of the
        conversation (the store persists across restarts; the SDK session does not)."""
        from claude_agent_sdk import ClaudeSDKClient

        if self._client is not None and self._client_tools != tool_names:
            await self._drop_client()  # the tool surface changed — rebuild the session
        if self._client is None:
            # The orb bridge reads self._orb_sinks (mutated per orb turn; orb turns are serialized).
            client = ClaudeSDKClient(self._options(tool_names, ORB_MODEL, "low", self._orb_sinks))
            await client.connect()
            self._client = client
            self._client_tools = tool_names
            return client, True
        return self._client, False

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._client_tools = ()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _collect_response(self, client, on_progress) -> str:
        """Drain one turn's messages: narrate interim text/tool lines, return the final text."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        texts: list[str] = []
        result_text: str | None = None
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and (block.text or "").strip():
                        # Collect the model's interim prose as the running answer, but do NOT push it as
                        # a progress line: narrating half-formed thoughts made HELIX read its own thinking
                        # aloud. Only tool milestones surface as progress, and only in a friendly phrase.
                        texts.append(block.text.strip())
                    elif isinstance(block, ToolUseBlock) and on_progress is not None:
                        # Never emit the raw tool identifier — spoken, "call_api" becomes "calawpee".
                        on_progress(friendly_tool_label(block.name))
            elif isinstance(message, ResultMessage):
                result_text = (message.result or "").strip() or None
        return result_text or (texts[-1] if texts else "")

    async def _orb_turn(self, prompt: str, tool_names: tuple[str, ...], sinks: _Sinks,
                        history: str, images=None) -> str:
        client, fresh = await self._ensure_client(tool_names)
        if fresh and history:
            # Just (re)connected — the model has no memory of earlier turns. Prime it with a compact
            # recent transcript so "what did I just ask?" works right after a restart.
            prompt = f"[Earlier in this conversation:\n{history}\n]\n\n{prompt}"
        # With attached images the turn is a structured user message (images + text); otherwise a plain
        # string, exactly as before. Same persistent session, same response drain.
        query_input = _image_message(prompt, images) if images else prompt
        watcher = None
        if sinks.cancel is not None:
            async def _watch():
                while True:
                    await asyncio.sleep(0.3)
                    c = sinks.cancel
                    if c is not None and c.is_set():
                        try:
                            await client.interrupt()
                        except Exception:  # noqa: BLE001
                            pass
                        return

            watcher = asyncio.ensure_future(_watch())
        try:
            await client.query(query_input)
            return await self._collect_response(client, sinks.on_progress)
        finally:
            if watcher is not None:
                watcher.cancel()

    def run_orb_turn(
        self, prompt: str, tool_names: tuple[str, ...] = (), *,
        history: str = "", on_progress=None, cancel=None, on_tool=None, user: str = "", images=None,
    ) -> str:
        """One interactive orb turn on the persistent subscription session. Raises on failure —
        the caller decides whether to fall back to the API path or surface the error. A failed turn
        always drops the session first, so the next turn starts clean and no dead consumer lingers.
        `history` seeds a freshly-(re)connected session; it is NOT re-sent on the retry (which reuses
        a fresh session too) beyond the first attempt's own freshness."""
        _hide_child_windows()  # before any SDK spawn — no blank console window on every turn
        names = tuple(tool_names)
        with self._orb_lock:  # orb turns only — never held while a bridged tool re-enters the brain
            self._orb_sinks.on_progress = on_progress
            self._orb_sinks.cancel = cancel
            self._orb_sinks.on_tool = on_tool
            self._orb_sinks.user = user
            try:
                try:
                    return self._run(self._orb_turn(prompt, names, self._orb_sinks, history, images))
                except Exception as first:  # one clean retry on a fresh session (dead CLI, stale pipe)
                    _LOG.warning("subscription turn failed (%s); retrying on a fresh session", first)
                    try:
                        self._run(self._drop_client(), timeout=30.0)
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        return self._run(self._orb_turn(prompt, names, self._orb_sinks, history, images))
                    except Exception:
                        try:  # a twice-failed turn must not leave a live-but-broken session behind
                            self._run(self._drop_client(), timeout=30.0)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
            finally:
                self._orb_sinks.on_progress = None
                self._orb_sinks.cancel = None
                self._orb_sinks.on_tool = None

    # ----- hermetic one-shots (agents/watchers, distillers, think_harder) -----
    async def _hermetic(self, prompt: str, tool_names: tuple[str, ...], model: str, effort: str,
                        sinks: _Sinks, system: str | None, web: bool) -> str:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

        opts = self._options(tool_names, model, effort, sinks, system=system, web=web)
        texts: list[str] = []
        result_text: str | None = None
        async for message in query(prompt=prompt, options=opts):
            if sinks.cancel is not None and sinks.cancel.is_set():
                break  # a stopped agent/deep-think stops draining (and stops billing) at once
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and (block.text or "").strip():
                        texts.append(block.text.strip())
                        if sinks.on_progress is not None:
                            sinks.on_progress(block.text.strip().split("\n", 1)[0][:90])
            elif isinstance(message, ResultMessage):
                result_text = (message.result or "").strip() or None
        return result_text or (texts[-1] if texts else "")

    def run_hermetic(
        self, prompt: str, tool_names: tuple[str, ...] = (), *,
        model: str = ORB_MODEL, effort: str = "low", system: str | None = None,
        on_progress=None, cancel=None, on_tool=None, web: bool = False,
    ) -> str:
        """A stateless run (agents/watchers, deep thinking, distillation). Fresh session each time,
        nothing persisted anywhere by this layer; `system` overrides the orb persona when the caller
        is a distiller with its own. Its sinks live in its own closure — no lock, so it runs
        concurrently with the orb and can even be re-entered from an orb turn's bridged tool. Raises
        on failure. `web` defaults OFF: autonomous/distiller runs get no arbitrary web fetch — only a
        user-driven reasoner (think_harder) opts back in."""
        _hide_child_windows()  # before any SDK spawn — no blank console window on every turn
        sinks = _Sinks(on_progress=on_progress, cancel=cancel, on_tool=on_tool)
        return self._run(self._hermetic(prompt, tuple(tool_names), model, effort, sinks, system, web))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True  # active() now False + _ensure_loop refuses; new turns can't start
        try:
            if self._loop is not None and self._thread and self._thread.is_alive():
                if self._client is not None:
                    fut = asyncio.run_coroutine_threadsafe(self._drop_client(), self._loop)
                    try:
                        fut.result(timeout=10.0)
                    except Exception:  # noqa: BLE001
                        pass
                self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:  # noqa: BLE001
            pass


class PreferredChat:
    """ChatModel that answers simple (no-tool) calls on the subscription when it's active, and falls
    back to the API-key chat otherwise — the distillers (profile, voice notes) and any other plain
    chat consumer route here so 'use my subscription' covers them too. Tool-using calls always go to
    the API chat: the raw tool round-trip only exists there."""

    def __init__(self, subscription: SubscriptionBrain, api_chat, *, model: str = ORB_MODEL,
                 effort: str = "low") -> None:
        self._sub = subscription
        self._api = api_chat
        self._model = model
        self._effort = effort

    @staticmethod
    def _flatten(turns) -> str:
        parts: list[str] = []
        for t in turns:
            for b in t.blocks:
                if isinstance(b, Text) and b.text.strip():
                    parts.append(b.text.strip())
        return "\n\n".join(parts)

    def chat(self, turns: list[Turn], system: str | None = None, tools=None) -> Reply:
        if not tools and self._sub.active():
            try:
                text = self._sub.run_hermetic(
                    self._flatten(turns), (), model=self._model, effort=self._effort, system=system,
                )
                return Reply(blocks=(Text(text),), usage=Usage())
            except Exception:  # noqa: BLE001 — the API chat is the safety net
                _LOG.warning("subscription chat failed; falling back to the API key", exc_info=True)
        return self._api.chat(turns, system=system, tools=tools)
