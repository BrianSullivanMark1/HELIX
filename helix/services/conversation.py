"""ConversationService — the model↔tools loop. The brain behind the orb.

Confirmation is conversational: the system prompt tells the model to restate and ask before calling
build_app, so the human always approves a spend in plain language before it happens.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from helix.domain.errors import BuildCancelled
from helix.domain.models import Message, Role
from helix.ports.clock import Clock
from helix.ports.coder import ProgressFn
from helix.ports.llm import ChatModel, Text, ToolResult, Turn
from helix.ports.stores import ConversationStore, MemoryStore
from helix.services.cancel import CancelToken
from helix.services.tools import ToolRegistry

if TYPE_CHECKING:
    from helix.services.knowledge import KnowledgeService

STOPPED_REPLY = "Okay, I stopped."  # shown (not spoken) when the user halts a turn; UI may offer cleanup

MAX_STEPS = 6  # guard against a runaway tool loop

# Tools that build, spend, self-modify, delete, rename, or launch the user's stuff. An AGENT run is
# autonomous (no human in the loop), so it is denied all of these — it can read, think, search, and
# report, but never build, change, remove, rename, or run anything on its own.
BUILD_TOOLS = frozenset(
    {
        "build_app", "build_task", "build_3d_model", "create_agent", "delete_build",
        "improve_helix", "rename_build", "run_task", "run_agent",
        "approve_self_change", "reject_self_change",
        # Knowledge WRITES are human-driven only — an autonomous agent may search the user's knowledge
        # (search_knowledge is deliberately NOT here) but never create a base or save a note on its own.
        "create_knowledge", "remember",
    }
)


class ConversationService:
    def __init__(
        self,
        chat: ChatModel,
        tools: ToolRegistry,
        store: ConversationStore,
        memory: MemoryStore,
        clock: Clock,
        system: str,
        knowledge: "KnowledgeService | None" = None,
    ) -> None:
        self._chat = chat
        self._tools = tools
        self._store = store
        self._memory = memory
        self._clock = clock
        self._system = system
        self._knowledge = knowledge  # ambient auto-recall of the user's saved knowledge (orb turns only)
        # A turn is a read-modify-write over the shared history. The Console and an Agent run on
        # separate worker threads against this one service, so serialize whole turns — otherwise their
        # appends interleave and the API gets a malformed (e.g. two-user-in-a-row) turn list.
        self._lock = threading.Lock()

    def run_turn(
        self, user_text: str, *, attachments_text: str | None = None,
        on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None, allow_builds: bool = True, persist: bool = True,
        knowledge_sources: list[tuple[str, str]] | None = None,
    ) -> str:
        # Only the brief history read-modify-writes are locked — NOT the model/tool loop. Builds run in
        # the background (the build tools just enqueue), so a turn is now milliseconds plus model latency;
        # narrowing the lock lets a Console turn and an Agent turn (or a quick follow-up) interleave
        # instead of one freezing the other.
        if persist:
            with self._lock:
                self._store.append(Message(Role.USER, user_text, self._clock.now()))
                turns = self._history_turns()
        else:
            # An agent run is hermetic: its goal and report never touch the shared Console transcript, so
            # it can't evict real turns from the window or be 'remembered' as if the user typed it.
            turns = [Turn(Role.USER, (Text(user_text),))]
        # Anchor the model to the REAL current time every turn. Without this it has no idea what "today"
        # is or the user's timezone, so it guesses dates — and it mis-converts the Unix-epoch timestamps
        # that Slack/GitHub/email return (e.g. Slack's "ts"). This is the fix for wrong message dates.
        if turns:
            last = turns[-1]
            turns[-1] = Turn(last.role, last.blocks + (Text(self._now_context()),))
        if attachments_text:
            # Attached files/folders ride along as EPHEMERAL context on this turn only — appended to the
            # current (last) user turn, never written to history, so a big attachment isn't replayed on
            # every later turn (and doesn't bloat the saved transcript). The text is already fenced and
            # marked untrusted by the attachments service.
            if turns:
                last = turns[-1]
                turns[-1] = Turn(last.role, last.blocks + (Text(attachments_text),))
            else:
                turns = [Turn(Role.USER, (Text(attachments_text),))]
        # Ambient knowledge: when the message clearly matches something the user saved, surface it as
        # EPHEMERAL context (appended to this turn only, never persisted — exactly like attachments) so the
        # orb answers from their own material without being told to search. Interactive orb only (persist);
        # an agent retrieves explicitly. auto_context is high-precision, so most turns inject nothing.
        if persist and self._knowledge is not None and turns:
            knowledge_text, ksources = self._knowledge.auto_context_with_sources(user_text)
            if knowledge_text:
                last = turns[-1]
                turns[-1] = Turn(last.role, last.blocks + (Text(knowledge_text),))
                if knowledge_sources is not None:
                    knowledge_sources.extend(ksources)  # surfaced to the UI as a citation chip
        specs = self._tools.specs()
        if not allow_builds:  # an agent run is autonomous — deny build/spend/self-mod/delete/run tools
            specs = [s for s in specs if s.name not in BUILD_TOOLS]

        def finish(text: str) -> str:
            return self._remember(text) if persist else text

        reply = None
        try:
            for _ in range(MAX_STEPS):
                if cancel is not None and cancel.is_set():
                    return finish(STOPPED_REPLY)
                reply = self._chat.chat(turns, system=self._system, tools=specs)
                u = reply.usage
                self._memory.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)

                if not reply.wants_tools:
                    return finish(reply.text)

                turns.append(Turn(Role.ASSISTANT, reply.blocks))
                results = []
                for call in reply.tool_uses:
                    if on_progress:
                        on_progress(self._progress_label(call.name, call.args))
                    try:
                        out = self._tools.dispatch(
                            call.name, call.args, on_progress=on_progress, cancel=cancel
                        )
                        results.append(ToolResult(call.id, out))
                    except BuildCancelled:  # user stopped mid-build — end the turn (don't loop the model)
                        return finish(STOPPED_REPLY)
                    except Exception as exc:  # surface to the model so it can recover gracefully
                        results.append(ToolResult(call.id, f"Error: {exc}", is_error=True))
                if cancel is not None and cancel.is_set():
                    return finish(STOPPED_REPLY)
                turns.append(Turn(Role.USER, tuple(results)))

            return finish((reply.text if reply else "") or "I got stuck — could you rephrase?")
        except Exception:
            # Record a balanced assistant reply even on failure, so a crashed turn never leaves a dangling
            # USER row that would malform the NEXT request. The worker still surfaces the real error.
            finish("Something went wrong on that one — try me again?")
            raise

    def _now_context(self) -> str:
        """A one-line current-time anchor injected each turn, so date reasoning is grounded and API
        epoch timestamps (Slack 'ts', GitHub, email) convert correctly instead of being guessed."""
        now = self._clock.now()
        offset = now.strftime("%z")
        tz = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
        human = now.strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        try:
            epoch = int(now.timestamp())
        except (OverflowError, OSError, ValueError):
            epoch = 0
        return (
            f"[Current date & time: {human} ({tz}); Unix epoch {epoch}. Use THIS as \"now\" for every "
            f"date question. When a tool result contains a Unix-epoch timestamp (e.g. Slack's \"ts\", or "
            f"GitHub/email times), convert it to the user's local timezone by comparing it to the current "
            f"epoch above, and answer with the absolute date (e.g. \"July 1\"). Never guess a date or "
            f"infer it from earlier in the conversation; if a timestamp is ambiguous, say so.]"
        )

    def recent_messages(self, limit: int = 50) -> list[Message]:
        """The recent human-facing transcript (USER/ASSISTANT only), oldest-first — so the Console can show
        the last messages on load and the conversation persists across launches."""
        return [m for m in self._store.recent(limit) if m.role in (Role.USER, Role.ASSISTANT)]

    # ----- helpers -----
    def _remember(self, text: str) -> str:
        with self._lock:
            self._store.append(Message(Role.ASSISTANT, text, self._clock.now()))
        return text

    def _history_turns(self) -> list[Turn]:
        turns = [
            Turn(m.role, (Text(m.text),))
            for m in self._store.recent(40)
            if m.role in (Role.USER, Role.ASSISTANT)
        ]
        while turns and turns[0].role != Role.USER:  # the API requires the first turn to be 'user'
            turns.pop(0)
        return self._coalesce(turns)

    @staticmethod
    def _coalesce(turns: list[Turn]) -> list[Turn]:
        """Merge consecutive same-role turns so the transcript always strictly alternates user/assistant —
        even when concurrent Console + agent runs interleave, or a failed turn left two users in a row.
        The Anthropic API rejects a malformed (e.g. user-then-user) turn list, so this keeps replay safe."""
        merged: list[Turn] = []
        for t in turns:
            if merged and merged[-1].role == t.role:
                merged[-1] = Turn(t.role, merged[-1].blocks + t.blocks)
            else:
                merged.append(t)
        return merged

    @staticmethod
    def _progress_label(tool: str, args: dict) -> str:
        if tool == "build_app":
            return f"Building {args.get('name', 'your app')}…"
        if tool == "build_task":
            return f"Building the {args.get('name', 'flow')} flow…"
        if tool == "build_3d_model":
            return f"Modeling {args.get('name', 'it')}…"
        if tool == "create_agent":
            return f"Saving the {args.get('name', 'agent')} agent…"
        if tool == "delete_build":
            return f"Removing {args.get('name', 'it')}…"
        if tool == "think_harder":
            return "Thinking it through…"
        if tool == "search_knowledge":
            return "Checking your knowledge…"
        if tool == "check_email":
            return "Checking your inbox…"
        if tool == "remember":
            return "Saving that…"
        if tool == "create_knowledge":
            return f"Starting the {args.get('name', 'knowledge')} base…"
        return "Working…"
