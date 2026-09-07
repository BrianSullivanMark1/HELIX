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
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

from helix.adapters.claude_code_cli import cli_unavailable_reason, resolve_claude_cli
from helix.domain.errors import MissingApiKey
from helix.domain.vocabulary import friendly_tool_label
from helix.logging_setup import get_logger
from helix.ports.llm import Image, Reply, Text, ToolOutput, Turn, Usage

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
# NOTE: deep-think (think_harder) and the dream session pass their model EXPLICITLY per call now — the growth
# model resolved by GrowthModelResolver (Fable 5, auto-upscaling), not a constant here. There is
# deliberately no DEEP_MODEL constant; the growth tier is decided in the container/resolver.
_TURN_TIMEOUT_S = 600.0           # one orb turn (tools included) must land inside this
_MAX_TURNS = 8                    # agentic loop cap per query (mirrors MAX_STEPS + headroom)

TOKEN_SETTING = "claude_code_oauth_token"

# Windows caps an ENTIRE command line at 32,767 chars (CreateProcessW), and the Agent SDK passes the
# system prompt as a command-line argument (`--system-prompt <31 KB of text>`). HELIX's persona plus
# the flags and ~50 bridged tool names measured 33,434 — 667 over the ceiling — so every orb turn died
# in CreateProcess with WinError 206 ("The filename or extension is too long"), which the SDK reports
# as `CLINotFoundError: Claude Code not found at <path>`. A launchable CLI and a valid token, both
# blamed for a command line that was simply too long. Past this budget the prompt is staged to a FILE
# and passed as `--system-prompt-file <path>`, which costs the command line a path instead of 31 KB.
# Under it nothing changes: a short prompt cannot approach the ceiling, and the inline form is the
# form every CLI version understands. The margin is deliberately wide (a 16 KB prompt leaves ~16 KB
# of headroom) because the persona GROWS with every feature — that growth is what crossed the line.
SYSTEM_PROMPT_FILE_OVER = 16_000
_prompt_file_lock = threading.Lock()


def _system_prompt_arg(system: str) -> str | dict:
    """How the SDK should carry this system prompt: inline when short, a file when long enough to
    threaten the Windows command-line ceiling. Falls back to inline if the file can't be written —
    an unwritable temp dir must degrade to the old behaviour, never kill the turn."""
    if len(system) <= SYSTEM_PROMPT_FILE_OVER:
        return system
    path = _stage_system_prompt(system)
    return {"type": "file", "path": str(path)} if path is not None else system


def _stage_system_prompt(system: str) -> Path | None:
    """Write `system` to a temp file claude.exe can read, or None if the disk refuses.

    Named by content hash, so the same persona reuses ONE file across every turn and process (no
    per-turn litter), and a changed persona lands in its own file instead of racing a live session's
    read. The write is staged-then-renamed: a spawning claude.exe never sees a half-written prompt."""
    digest = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
    directory = Path(tempfile.gettempdir()) / "helix-system-prompts"
    path = directory / f"{digest}.txt"
    try:
        with _prompt_file_lock:
            if not path.exists():
                directory.mkdir(parents=True, exist_ok=True)
                staging = directory / f"{digest}.{os.getpid()}.partial"
                staging.write_text(system, encoding="utf-8")
                staging.replace(path)  # atomic on Windows for a same-directory rename
    except OSError:
        _LOG.warning("could not stage the system prompt to a file; sending it inline", exc_info=True)
        return None
    return path


def _tool_progress(name: str, args: dict | None = None) -> str:
    """The progress line for a tool the SDK just started running — never the raw identifier (spoken,
    "call_api" becomes "calawpee"), and always trailing off, because the tool has only STARTED. The
    API rail has said "Checking that service…" all along (conversation.py's _progress_label), so both
    rails now say the same words for the same action instead of one of them swallowing the ellipsis —
    they feed the SAME status line and voice. The guard is for the unmapped fallback, which already
    trails off on its own: "Working……" reads as a typo, in HELIX's own voice.

    `args` is the tool call's own input, forwarded so this rail can name the thing too ("Building Tip
    Calculator…"). Without it the subscription rail said "Building that…" while the API rail said the
    name for the very same build — one HELIX, two voices, depending on which rail happened to serve
    the turn."""
    label = friendly_tool_label(name, args)
    return label if label.endswith("…") else label + "…"


def raise_no_rail(subscription, exc: MissingApiKey) -> NoReturn:
    """Re-raise a bare "no API key" as the whole truth about BOTH rails, then never return.

    The API adapter can only report its own half ("there's no key"): it knows nothing about the
    subscription. Only out here do we know why the SUBSCRIPTION rail didn't serve this turn — either
    structurally (why_inactive: no token, no SDK, no launchable CLI) or, when the structure is
    perfect and the turn STILL came here, what the last attempt actually died of (last_failure). Every
    caller that falls back from the subscription to a bare AnthropicChat must funnel through this one
    helper, or it repeats the failure this text was written to kill: a too-long command line reaching
    the user as "check your subscription token", sending them off to re-issue a credential that had
    never been wrong. With nothing to add, the original error stands unembellished."""
    probe = getattr(subscription, "why_inactive", None)
    reason = probe() if callable(probe) else None
    if not reason:
        recent = getattr(subscription, "last_failure", None)
        failure = recent() if callable(recent) else None
        if failure:
            # The sentence is CLOSED here. `failure` is a raw exception string ("The filename or
            # extension is too long") with no punctuation of its own, and the caller below glues
            # "There's no Claude API key set either…" straight onto this — unterminated, the two ran
            # together into one breathless sentence the voice read without a pause. Every why_inactive
            # reason already ends in a period, which is why that branch always read fine.
            failure = failure.rstrip()
            if failure and failure[-1] not in ".!?":
                failure += "."
            reason = ("Your subscription token is saved and the Claude Code CLI runs, but the "
                      f"turn on it failed: {failure}")
    if not reason:
        raise exc
    raise MissingApiKey(
        f"{reason} There's no Claude API key set either, so HELIX has no way to reach Claude "
        f"right now."
    ) from exc


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
    on_tool: Callable[..., None] | None = None  # (name, digest, saw_pixels: bool) — third arg says
    # whether the tool actually returned images (drives the visual-memory bookkeeping upstream)
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
        self._last_error: str | None = None  # why the last turn on this rail died — see last_failure()

    # ----- availability -----
    def token(self) -> str:
        return (self._oauth() or "").strip()

    def active(self, *, allow_probe: bool = True) -> bool:
        """True when HELIX should run conversation on the subscription: token + CLI + SDK present.

        Pass allow_probe=False from the Qt GUI thread — deciding whether a claude.exe will launch means
        spawning it, and no widget may block on that (see resolve_claude_cli)."""
        return (not self._closed and bool(self.token())
                and sdk_importable() and resolve_claude_cli(allow_probe=allow_probe) is not None)

    def why_inactive(self, *, allow_probe: bool = True) -> str | None:
        """None when the subscription rail is usable; otherwise the ACTUAL reason it isn't. Three very
        different problems (no token, no SDK, no launchable CLI) used to collapse into one message
        telling the user to check their token — so a working token got blamed for a broken CLI."""
        if self._closed:
            return "The subscription brain is shut down."
        if not self.token():
            return ("No Claude subscription token is saved. Run `claude setup-token` and paste the "
                    "token into Settings.")
        if not sdk_importable():
            return "The claude-agent-sdk package is not importable in this build."
        # token + SDK are fine; None here means the rail is usable
        return cli_unavailable_reason(allow_probe=allow_probe)

    def last_failure(self) -> str | None:
        """What the last subscription turn actually died of, or None if none has since it last worked.

        why_inactive() only sees STRUCTURE (token, SDK, a launchable CLI). All three can look perfect
        while every turn still fails — a command line over the Windows ceiling did exactly that, and
        the user was told to re-check a token that was never wrong. This is the other half of the
        answer: what happened when HELIX actually tried."""
        return self._last_error

    def _note_failure(self, exc: BaseException) -> None:
        text = (str(exc) or type(exc).__name__).strip()
        self._last_error = text[:300]

    def _note_success(self, out: str) -> str:
        self._last_error = None  # the rail demonstrably works; a stale error must not be blamed later
        return out

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
                            # Third arg: whether pixels ACTUALLY came back, so the sight bookkeeping
                            # (visual-memory distill) skips a look that failed or was cancelled.
                            saw_pixels = isinstance(out, ToolOutput) and bool(out.images)
                            sinks.on_tool(spec_name, digest, saw_pixels)
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
            # Long personas ride a FILE, not the command line — see SYSTEM_PROMPT_FILE_OVER.
            system_prompt=_system_prompt_arg(system or self._system),
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
                        # The block's own input rides along so a named build is narrated by NAME, the
                        # way the API rail has always done it (getattr: a future SDK block shape that
                        # renames the field must degrade to the generic phrase, never kill the turn).
                        on_progress(_tool_progress(block.name, getattr(block, "input", None)))
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
        a fresh session too) beyond the first attempt's own freshness. The retry exists for a DEAD
        session with nothing done yet: a turn that already ran a tool, or that the user stopped, is
        never re-sent."""
        _hide_child_windows()  # before any SDK spawn — no blank console window on every turn
        names = tuple(tool_names)
        with self._orb_lock:  # orb turns only — never held while a bridged tool re-enters the brain
            ran_tool = False

            def _note_tool(*a, **kw):
                # A dispatched tool means REAL side effects (a build enqueued, a reminder set, money
                # spent). From here the turn is no longer replayable, so the retry below must not
                # re-send it — this flag is set even when the caller passed no on_tool of its own.
                nonlocal ran_tool
                ran_tool = True
                if on_tool is not None:
                    on_tool(*a, **kw)

            self._orb_sinks.on_progress = on_progress
            self._orb_sinks.cancel = cancel
            self._orb_sinks.on_tool = _note_tool
            self._orb_sinks.user = user
            try:
                # The outer except records WHY a turn died, so a later "no rail" message can name the
                # real failure instead of sending the user back to a token that was never the problem.
                try:
                    try:
                        return self._note_success(
                            self._run(self._orb_turn(prompt, names, self._orb_sinks, history, images))
                        )
                    except Exception as first:  # one clean retry on a fresh session (dead CLI, stale pipe)
                        stopped = cancel is not None and cancel.is_set()
                        if ran_tool or stopped:
                            # Re-running would double the side effects, or re-send the very turn the
                            # user just stopped. Drop the session and let the caller decide what to say.
                            _LOG.warning("subscription turn failed (%s); NOT retrying (%s)", first,
                                         "tools already ran" if ran_tool else "user cancelled")
                            try:
                                self._run(self._drop_client(), timeout=30.0)
                            except Exception:  # noqa: BLE001
                                pass
                            raise
                        _LOG.warning("subscription turn failed (%s); retrying on a fresh session", first)
                        try:
                            self._run(self._drop_client(), timeout=30.0)
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            return self._note_success(
                                self._run(
                                    self._orb_turn(prompt, names, self._orb_sinks, history, images)
                                )
                            )
                        except Exception:
                            try:  # a twice-failed turn must not leave a live-but-broken session behind
                                self._run(self._drop_client(), timeout=30.0)
                            except Exception:  # noqa: BLE001
                                pass
                            raise
                except Exception as exc:
                    # A turn the USER stopped is not a broken rail. The interrupt surfaces here as an
                    # ordinary exception, and Settings now shows last_failure() as an amber warning —
                    # so recording it would tell someone who simply pressed Stop that their
                    # subscription is faulty, and keep telling them until a turn next succeeded.
                    if not (cancel is not None and cancel.is_set()):
                        self._note_failure(exc)
                    raise
            finally:
                self._orb_sinks.on_progress = None
                self._orb_sinks.cancel = None
                self._orb_sinks.on_tool = None

    # ----- hermetic one-shots (agents/watchers, distillers, think_harder) -----
    async def _hermetic(self, prompt: str, tool_names: tuple[str, ...], model: str, effort: str,
                        sinks: _Sinks, system: str | None, web: bool, images=None) -> str:
        from claude_agent_sdk import (
            AssistantMessage, ResultMessage, TextBlock, ToolUseBlock, query,
        )

        opts = self._options(tool_names, model, effort, sinks, system=system, web=web)
        # With attached images the one-shot is a structured user message (images + text), exactly as
        # _orb_turn sends them on the persistent session; otherwise a plain string, as before. The SDK's
        # query() takes the same async-iterable envelope and still ends stdin after the result, so a
        # picture-bearing hermetic run is as stateless and as fenced as a text-only one.
        query_input = _image_message(prompt, images) if images else prompt
        texts: list[str] = []
        result_text: str | None = None
        async for message in query(prompt=query_input, options=opts):
            if sinks.cancel is not None and sinks.cancel.is_set():
                break  # a stopped agent/deep-think stops draining (and stops billing) at once
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and (block.text or "").strip():
                        # Collect the interim prose as the running answer, but do NOT narrate it —
                        # exactly as the orb path does (_collect_response). Pushing the model's
                        # first half-formed sentence as a progress line made HELIX read its own
                        # thinking aloud, and this path speaks too: an agent run and think_harder
                        # both hand their on_progress straight to the Console status line and the
                        # voice. Only tool milestones surface, and only as a friendly phrase.
                        texts.append(block.text.strip())
                    elif isinstance(block, ToolUseBlock) and sinks.on_progress is not None:
                        # Same narration as the orb path, name and all — an agent run speaks too.
                        sinks.on_progress(_tool_progress(block.name, getattr(block, "input", None)))
            elif isinstance(message, ResultMessage):
                result_text = (message.result or "").strip() or None
        return result_text or (texts[-1] if texts else "")

    def run_hermetic(
        self, prompt: str, tool_names: tuple[str, ...] = (), *,
        model: str = ORB_MODEL, effort: str = "low", system: str | None = None,
        on_progress=None, cancel=None, on_tool=None, web: bool = False, images=None,
    ) -> str:
        """A stateless run (agents/watchers, deep thinking, distillation). Fresh session each time,
        nothing persisted anywhere by this layer; `system` overrides the orb persona when the caller
        is a distiller with its own. Its sinks live in its own closure — no lock, so it runs
        concurrently with the orb and can even be re-entered from an orb turn's bridged tool. Raises
        on failure. `web` defaults OFF: autonomous/distiller runs get no arbitrary web fetch — only a
        user-driven reasoner (think_harder) opts back in. `images` (ports.llm.Image blocks) ride in
        ahead of the prompt as vision, the way run_orb_turn carries an attachment — so a no-tool,
        unattended looker (the hologram critic judging a rendered preview) can run on the plan instead
        of being the one step that silently needs an API key."""
        _hide_child_windows()  # before any SDK spawn — no blank console window on every turn
        sinks = _Sinks(on_progress=on_progress, cancel=cancel, on_tool=on_tool)
        try:
            out = self._run(
                self._hermetic(prompt, tuple(tool_names), model, effort, sinks, system, web, images)
            )
        except Exception as exc:
            self._note_failure(exc)  # so the "no rail" message can name it — see last_failure()
            raise
        return self._note_success(out)

    def refresh_session(self) -> None:
        """Drop the persistent orb session so the NEXT turn reconnects fresh and reseeds from the
        caller's history digest. Used after a turn deliberately ran OUTSIDE the session — the
        auto-escalated deep turn runs hermetic on the growth model, and without this the live
        session's model-side history would be missing that whole exchange (the orb would not know
        its own last answer). Best-effort: a refresh hiccup costs continuity, never a turn."""
        try:
            if self._client is not None:
                self._run(self._drop_client(), timeout=30.0)
        except Exception:  # noqa: BLE001
            _LOG.warning("could not refresh the orb session", exc_info=True)

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
    the API chat: the raw tool round-trip only exists there. A no-tool call that carries Image blocks
    (the hologram critic handing over a rendered preview) stays on the subscription too: the pictures
    ride into run_hermetic as vision, so a subscription-only machine gets the look instead of the
    critic quietly abstaining on the one machine this rail exists for."""

    def __init__(self, subscription: SubscriptionBrain, api_chat, *, model: str = ORB_MODEL,
                 effort: str = "low") -> None:
        self._sub = subscription
        self._api = api_chat
        self._model = model
        self._effort = effort

    def without_web(self) -> "PreferredChat":
        """This rail-preferring chat with the API rail's server-side web tools shed, for AUTONOMOUS
        runs — see AnthropicChat.without_web for why an agent must not get them. The SUBSCRIPTION leg
        needs no change and is deliberately left alone: its calls go through run_hermetic, whose `web`
        has always defaulted OFF for exactly this reason. An API chat that can't shed them (a test
        double, a future adapter) leaves this chat as it is — the caller gets what it gave."""
        shed = getattr(self._api, "without_web", None)
        if not callable(shed):
            return self
        return PreferredChat(self._sub, shed(), model=self._model, effort=self._effort)

    @staticmethod
    def _flatten(turns) -> str:
        parts: list[str] = []
        for t in turns:
            for b in t.blocks:
                if isinstance(b, Text) and b.text.strip():
                    parts.append(b.text.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _images(turns) -> tuple:
        """Every Image block in the turns, in order. _flatten keeps only the text, which is right for a
        transcript distill — but it silently DROPPED a caller's picture, so a critic routed here would
        have judged a preview it never saw and invented a problem. The pictures travel separately."""
        return tuple(b for t in turns for b in t.blocks if isinstance(b, Image))

    def chat(self, turns: list[Turn], system: str | None = None, tools=None) -> Reply:
        if not tools and self._sub.active():
            try:
                text = self._sub.run_hermetic(
                    self._flatten(turns), (), model=self._model, effort=self._effort, system=system,
                    images=self._images(turns) or None,
                )
                return Reply(blocks=(Text(text),), usage=Usage())
            except Exception:  # noqa: BLE001 — the API chat is the safety net
                _LOG.warning("subscription chat failed; falling back to the API key", exc_info=True)
        try:
            return self._api.chat(turns, system=system, tools=tools)
        except MissingApiKey as exc:
            # Both rails are down. Naming WHICH one failed, and why, lives in one shared helper — the
            # deep reasoner's own API fallback (container._deep_think_on_api) falls back past this
            # class entirely, and the two must not drift into telling the user different stories.
            raise_no_rail(self._sub, exc)
