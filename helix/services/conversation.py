"""ConversationService — the model↔tools loop. The brain behind the orb.

Confirmation is conversational: the system prompt tells the model to restate and ask before calling
build_app, so the human always approves a spend in plain language before it happens.
"""
from __future__ import annotations

import threading

from helix.domain.errors import BuildCancelled
from helix.domain.models import Message, Role
from helix.ports.clock import Clock
from helix.ports.coder import ProgressFn
from helix.ports.llm import ChatModel, Text, ToolResult, Turn
from helix.ports.stores import ConversationStore, MemoryStore
from helix.services.cancel import CancelToken
from helix.services.tools import ToolRegistry

STOPPED_REPLY = "Okay, I stopped."  # shown (not spoken) when the user halts a turn; UI may offer cleanup

MAX_STEPS = 6  # guard against a runaway tool loop

# Tools that build, spend, self-modify, or delete the user's stuff. An AGENT run is autonomous (no human
# in the loop), so it is denied these — it can read, think, search, and report, but never build or change.
BUILD_TOOLS = frozenset(
    {"build_app", "build_task", "build_3d_model", "create_agent", "delete_build", "improve_helix"}
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
    ) -> None:
        self._chat = chat
        self._tools = tools
        self._store = store
        self._memory = memory
        self._clock = clock
        self._system = system
        # A turn is a read-modify-write over the shared history. The Console and an Agent run on
        # separate worker threads against this one service, so serialize whole turns — otherwise their
        # appends interleave and the API gets a malformed (e.g. two-user-in-a-row) turn list.
        self._lock = threading.Lock()

    def run_turn(
        self, user_text: str, *, on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None, allow_builds: bool = True,
    ) -> str:
        with self._lock:
            return self._run_turn_locked(
                user_text, on_progress=on_progress, cancel=cancel, allow_builds=allow_builds
            )

    def _run_turn_locked(
        self, user_text: str, *, on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None, allow_builds: bool = True,
    ) -> str:
        self._store.append(Message(Role.USER, user_text, self._clock.now()))
        turns = self._history_turns()
        specs = self._tools.specs()
        if not allow_builds:  # an agent run is autonomous — deny build/spend/self-mod/delete tools
            specs = [s for s in specs if s.name not in BUILD_TOOLS]

        reply = None
        for _ in range(MAX_STEPS):
            if cancel is not None and cancel.is_set():
                return self._remember(STOPPED_REPLY)
            reply = self._chat.chat(turns, system=self._system, tools=specs)
            u = reply.usage
            self._memory.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)

            if not reply.wants_tools:
                return self._remember(reply.text)

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
                except BuildCancelled:  # the user stopped mid-build — end the turn (don't loop the model)
                    return self._remember(STOPPED_REPLY)
                except Exception as exc:  # surface to the model so it can recover gracefully
                    results.append(ToolResult(call.id, f"Error: {exc}", is_error=True))
            if cancel is not None and cancel.is_set():
                return self._remember(STOPPED_REPLY)
            turns.append(Turn(Role.USER, tuple(results)))

        return self._remember((reply.text if reply else "") or "I got stuck — could you rephrase?")

    # ----- helpers -----
    def _remember(self, text: str) -> str:
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
        return turns

    @staticmethod
    def _progress_label(tool: str, args: dict) -> str:
        if tool == "build_app":
            return f"Building {args.get('name', 'your app')}…"
        if tool == "build_task":
            return f"Building the {args.get('name', 'task')} task…"
        if tool == "build_3d_model":
            return f"Modeling {args.get('name', 'it')}…"
        if tool == "create_agent":
            return f"Saving the {args.get('name', 'agent')} agent…"
        if tool == "delete_build":
            return f"Removing {args.get('name', 'it')}…"
        if tool == "think_harder":
            return "Thinking it through…"
        return "Working…"
