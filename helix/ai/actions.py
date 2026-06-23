"""The Forge tool router — the conversational hands of HELIX.

The Console conversation can BUILD apps for the user (`build_app`) and manage them, and improve HELIX
itself (`improve_helix` and the approval gate). Each tool maps to a real engine call in `ActionRouter`.
Anything that spends real resources (a build uses Claude; a self-change merges code) is gated behind an
explicit spoken/typed confirmation, mirroring the GUI's confirmation dialogs.

Qt-free so the router stays unit-testable; the UI (the Console) supplies dependencies via ActionContext.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from helix.agents import registry as agents_registry
from helix.selfdev import builds, coder, constitution, engine, mailer, triggers

# --------------------------------------------------------------------------- #
# Tool schemas — the commands the Console conversation can act on.
# --------------------------------------------------------------------------- #

XPERT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "build_app",
        "description": (
            "Build a brand-new app FOR THE USER from a plain-language description, using the Opus "
            "coding agent. Use whenever the user asks you to make/build/create/invent an app, tool, "
            "program, calculator, tracker, timer, game, or utility for them (e.g. 'build me a tip "
            "calculator', 'make a habit tracker', 'create a unit converter'). The app is written into "
            "its own isolated workspace and added to their menu. It takes a couple of minutes and uses "
            "Claude, so it is confirmed first. Give it a short name plus the user's full request."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A short, friendly name for the app, e.g. 'Tip Calculator'."},
                "request": {"type": "string", "description": "The full description of what the app should do, in the user's words plus any useful detail."},
            },
            "required": ["request"],
        },
    },
    {
        "name": "list_builds",
        "description": (
            "List the apps the user has built so far. Use when they ask what they've made, what's in "
            "their menu, or what apps they have."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "improve_helix",
        "description": (
            "Change HELIX's OWN code with the Opus 4.8 coding agent — add a feature, fix a bug, or "
            "improve the app itself. Use only when the user asks to change, fix, or improve HELIX, this "
            "app, or the program ITSELF (not when they ask to build their own app — use build_app for "
            "that). The change is drafted on a separate review branch and is NOT applied to the running "
            "app, so it is safe; it takes a couple of minutes and uses Claude. Pass the request as the task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A clear description of the change to make to HELIX itself."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "remove_feature",
        "description": (
            "Cleanly REMOVE a feature from HELIX's own code to keep it lean — the inverse of "
            "improve_helix. Use when the user asks to remove, delete, or get rid of a built-in HELIX "
            "feature. Drafted on a review branch; nothing is removed until approved, then HELIX restarts lean."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "feature": {"type": "string", "description": "Which feature to remove, in the user's words."},
            },
            "required": ["feature"],
        },
    },
    {
        "name": "audit_dead_code",
        "description": (
            "Audit HELIX's own code for dead / unused code and draft removals to keep it lean. Use when "
            "the user asks to clean up, prune, audit, or slim down the codebase."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "approve_change",
        "description": (
            "Approve and merge the most recent drafted HELIX code change — the 'ship it' command. "
            "Smoke-checks it, then merges it; a restart loads it. Use when the user says ship/approve/"
            "merge/apply the drafted change."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reject_change",
        "description": (
            "Discard the most recent drafted HELIX code change and delete its branch. Use when the user "
            "says reject/discard/scrap the change."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pending_changes",
        "description": (
            "List drafted HELIX code changes waiting for approval. Use when the user asks what's pending "
            "or waiting to ship."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fix_recent_crashes",
        "description": (
            "Check HELIX's error log for recent crashes and draft a fix for any new one (drafted on a "
            "review branch; nothing applied automatically). Use when the user asks to fix crashes or errors."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "show_screen",
        "description": (
            "Open one of HELIX's screens for the user. Use when they ask to see / show / open / pull up "
            "their apps (the menu), the run list (tasks), their version history (archive), or settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "screen": {
                    "type": "string",
                    "enum": ["menu", "tasks", "archive", "settings"],
                    "description": "Which screen: menu (the user's built apps), tasks (runnable actions), archive (version history + restore), or settings.",
                }
            },
            "required": ["screen"],
        },
    },
    {
        "name": "remove_app",
        "description": (
            "Delete one of the user's built APPS by name. Use when they ask to remove/delete/get rid of "
            "an app they made (e.g. 'delete the tip calculator'). Confirmed before it deletes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The app's name (or part of it)."}},
            "required": ["name"],
        },
    },
    {
        "name": "remove_task",
        "description": (
            "Delete one of the user's TASKS by name. Use when they ask to remove/delete a task they made. "
            "Confirmed before it deletes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The task's name (or part of it)."}},
            "required": ["name"],
        },
    },
    {
        "name": "remove_agent",
        "description": (
            "Delete one of the user's AGENTS by name. Use when they ask to remove/delete an agent. "
            "Confirmed before it deletes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The agent's name (or part of it)."}},
            "required": ["name"],
        },
    },
]

TOOL_NAMES = {tool["name"] for tool in XPERT_TOOLS}


# --------------------------------------------------------------------------- #
# Affirmative / negative detection for the deterministic spoken-confirmation gate. A build or a
# self-change only fires when the user's actual words clearly say yes — never on the model alone.
# --------------------------------------------------------------------------- #

_AFFIRM = (
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "do it",
    "go ahead", "go for it", "please do", "affirmative", "correct", "absolutely",
    "send it", "build it", "start it", "of course", "yes please",
)
_NEGATE = (
    "no", "nope", "nah", "cancel", "don't", "do not", "negative", "never mind",
    "nevermind", "abort", "stop", "forget it", "not now",
)


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip() + " "


def is_affirmative(text: str) -> bool:
    spaced = _norm(text)
    if any(f" {phrase} " in spaced for phrase in _NEGATE):  # an explicit no overrides
        return False
    return any(f" {phrase} " in spaced for phrase in _AFFIRM)


def is_negative(text: str) -> bool:
    spaced = _norm(text)
    return any(f" {phrase} " in spaced for phrase in _NEGATE)


# --------------------------------------------------------------------------- #
# The action context + router.
# --------------------------------------------------------------------------- #


@dataclass
class ActionContext:
    """Dependencies the router needs, kept free of any Qt types so the router is unit-testable.
    The Console UI supplies these."""

    memory: Any
    settings: Any
    research_fn: Callable[[str], str]
    on_progress: Callable[[str], None] = lambda _msg: None     # live "what HELIX is doing" step text
    show_screen: Callable[[str], None] = lambda _name: None    # open a screen (menu/tasks/archive/settings)
    on_build_created: Callable[[str], None] = lambda _name: None  # a new Build landed (refresh the menu)
    on_creations_changed: Callable[[], None] = lambda: None    # an app/task/agent was removed (refresh UI)


@dataclass
class ToolOutcome:
    text: str  # the tool_result fed back to the model
    requires_confirmation: bool = False
    pending: tuple | None = None  # (name, input) to execute once the user confirms out loud


class ActionRouter:
    """Maps tool calls to real engine functions. Gated tools defer their side effect until
    `execute_confirmed` is called after a spoken/typed yes."""

    def __init__(self, ctx: ActionContext, tool_names: tuple | set | None = None) -> None:
        self.ctx = ctx
        self._tool_names = set(tool_names) if tool_names is not None else None

    @property
    def tools(self) -> list:
        if self._tool_names is None:
            return XPERT_TOOLS
        return [tool for tool in XPERT_TOOLS if tool["name"] in self._tool_names]

    def run(self, name: str, tool_input: dict | None) -> ToolOutcome:
        tool_input = tool_input or {}
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return ToolOutcome(f"(unknown tool '{name}')")
        try:
            return handler(tool_input)
        except Exception as error:  # never let a tool crash the whole turn
            return ToolOutcome(f"That action ran into a problem: {error}")

    def execute_confirmed(self, name: str, tool_input: dict | None) -> str:
        """Perform a previously-gated side effect, now that the user has confirmed."""
        tool_input = tool_input or {}
        if name == "build_app":
            bname = str(tool_input.get("name", "")).strip() or "app"
            request = str(tool_input.get("request", "")).strip()
            ws, result = builds.build_app(bname, request, on_step=self.ctx.on_progress)
            if not result.ok:
                return f"I couldn't build that, sir: {result.error}"
            try:
                self.ctx.on_build_created(ws.name)  # the slug — so the UI can open the exact Build
            except Exception:
                pass
            summary = (result.summary or "").strip()
            return f"Built {bname}, sir. {summary} It's in your menu now — open it from there."
        if name in ("remove_app", "remove_task"):
            slug = str(tool_input.get("slug", ""))
            label = str(tool_input.get("label", slug))
            removed = builds.delete_build(slug)
            self._refresh_creations()
            return (f"Deleted {label}, sir." if removed
                    else f"I couldn't delete {label}, sir — it may be open; close it and try again.")
        if name == "remove_agent":
            key = str(tool_input.get("slug", ""))
            label = str(tool_input.get("label", key))
            removed = agents_registry.delete_agent(self.ctx.settings, key)
            self._refresh_creations()
            return f"Deleted the {label} agent, sir." if removed else f"I couldn't find the {label} agent, sir."
        return "Done, sir."

    def _refresh_creations(self) -> None:
        try:
            self.ctx.on_creations_changed()
        except Exception:
            pass

    @staticmethod
    def _match(items: list, target: str, name_key: str, key_key: str):
        """Find an item by (case-insensitive) name, key, or substring. Returns the item or None."""
        want = (target or "").strip().lower()
        if not want:
            return None
        for it in items:
            if str(it.get(name_key, "")).lower() == want or str(it.get(key_key, "")).lower() == want:
                return it
        for it in items:
            if want in str(it.get(name_key, "")).lower():
                return it
        return None

    # -- removing the user's creations (confirmed) -------------------------- #

    def _tool_remove_app(self, tool_input: dict) -> ToolOutcome:
        return self._remove_build_outcome(tool_input, "app", "remove_app")

    def _tool_remove_task(self, tool_input: dict) -> ToolOutcome:
        return self._remove_build_outcome(tool_input, "task", "remove_task")

    def _remove_build_outcome(self, tool_input: dict, kind: str, tool: str) -> ToolOutcome:
        noun = "task" if kind == "task" else "app"
        match = self._match(builds.list_builds(kind), str(tool_input.get("name", "")), "name", "slug")
        if not match:
            return ToolOutcome(f"I don't see a {noun} by that name, sir.")
        return ToolOutcome(
            f"CONFIRMATION REQUIRED: this will permanently delete the {noun} '{match['name']}'. Ask the "
            "user to confirm before deleting.",
            requires_confirmation=True,
            pending=(tool, {"slug": match["slug"], "label": match["name"]}),
        )

    def _tool_remove_agent(self, tool_input: dict) -> ToolOutcome:
        match = self._match(agents_registry.list_agents(self.ctx.settings),
                            str(tool_input.get("name", "")), "name", "key")
        if not match:
            return ToolOutcome("I don't see an agent by that name, sir.")
        return ToolOutcome(
            f"CONFIRMATION REQUIRED: this will delete the agent '{match['name']}'. Ask the user to confirm.",
            requires_confirmation=True,
            pending=("remove_agent", {"slug": match["key"], "label": match["name"]}),
        )

    # -- building apps for the user ----------------------------------------- #

    def _tool_build_app(self, tool_input: dict) -> ToolOutcome:
        request = str(tool_input.get("request", "")).strip()
        if not request:
            return ToolOutcome("Tell me what app you'd like me to build, sir.")
        name = str(tool_input.get("name", "")).strip() or request[:40]
        return ToolOutcome(
            f"CONFIRMATION REQUIRED: I'll build '{name}' now — it uses Claude and takes a couple of "
            "minutes. Tell the user what it will do and ask them to confirm before it starts.",
            requires_confirmation=True,
            pending=("build_app", {"name": name, "request": request}),
        )

    def _tool_list_builds(self, _input: dict) -> ToolOutcome:
        items = builds.list_builds("app")
        if not items:
            return ToolOutcome("You haven't built any apps yet, sir — tell me what to make.")
        lines = [b["name"] + (f" — {b['request'][:50]}" if b.get("request") else "") for b in items[:12]]
        return ToolOutcome("Your apps: " + "; ".join(lines) + ".")

    # -- self-improvement (HELIX edits its own code) ------------------------- #

    def _tool_improve_helix(self, tool_input: dict) -> ToolOutcome:
        task = str(tool_input.get("task", "")).strip()
        if not task:
            return ToolOutcome("Tell me what to build, change, or fix in HELIX, sir.")
        result = coder.run_coding_task(task, on_step=self.ctx.on_progress)
        if not result.ok:
            return ToolOutcome(f"I couldn't draft that change, sir: {result.error}")
        rec = engine.record_pending(self.ctx.settings, result)
        mailer.notify_drafted(self.ctx.settings, rec)  # best-effort email; no-op if not configured
        files = ", ".join(result.changed_files[:8]) + ("…" if len(result.changed_files) > 8 else "")
        cost = f" (about ${result.cost_usd:.2f})" if result.cost_usd else ""
        summary = (result.summary or "").strip()
        return ToolOutcome(
            f"Done, sir — drafted on review branch {result.branch}{cost}. {summary} "
            f"Files changed: {files or 'none'}. It's on a branch, not live yet — say 'ship it' to "
            "approve and merge it, or 'reject it' to discard."
        )

    def _tool_remove_feature(self, tool_input: dict) -> ToolOutcome:
        feature = str(tool_input.get("feature", "")).strip()
        if not feature:
            return ToolOutcome("Which feature should I remove, sir?")
        # The Forge's shell is immutable to text or voice (Commandments 8 & 12). Refuse up front rather
        # than spend Claude drafting a change the approval gate would only reject. The user's own
        # Apps/Tasks/Agents are data and are removed by name via remove_app/remove_task/remove_agent.
        if constitution.names_shell_component(feature):
            return ToolOutcome(
                "I can't remove that, sir — the navigation, the Archive, the voice toggle, Settings, and "
                "the Console itself are part of HELIX, not features I can strip out. I can remove an app, "
                "task, or agent you've built, or change a setting, if you'd like."
            )
        result = coder.run_coding_task(coder.build_removal_task(feature), on_step=self.ctx.on_progress)
        if not result.ok:
            return ToolOutcome(f"I couldn't draft that removal, sir: {result.error}")
        rec = engine.record_pending(self.ctx.settings, result)
        mailer.notify_drafted(self.ctx.settings, rec)
        files = ", ".join(result.changed_files[:8]) + ("…" if len(result.changed_files) > 8 else "")
        cost = f" (about ${result.cost_usd:.2f})" if result.cost_usd else ""
        summary = (result.summary or "").strip()
        return ToolOutcome(
            f"Drafted the removal of {feature} on review branch {result.branch}{cost}. {summary} "
            f"Files changed: {files or 'none'}. It's on a branch, not applied yet — say 'ship it' to "
            "approve, then I'll merge and restart lean."
        )

    def _tool_audit_dead_code(self, _input: dict) -> ToolOutcome:
        task = (
            "Audit HELIX for dead or unused code and remove what is clearly safe to remove, to keep it "
            "lean. Look for: modules never imported, functions/classes never referenced, tools with no "
            "handler (or handlers with no tool), launcher/registry entries with no backing code, and "
            "unused imports. Remove only clearly-dead code; when unsure, leave it and note it. Make sure "
            "the app still imports and boots. Summarize what you removed and what you flagged."
        )
        result = coder.run_coding_task(task, on_step=self.ctx.on_progress)
        if not result.ok:
            if "no file changes" in (result.error or "").lower():
                return ToolOutcome("Audited the codebase, sir — nothing clearly dead to prune. It's lean.")
            return ToolOutcome(f"I couldn't run the audit, sir: {result.error}")
        rec = engine.record_pending(self.ctx.settings, result)
        mailer.notify_drafted(self.ctx.settings, rec)
        files = ", ".join(result.changed_files[:8]) + ("…" if len(result.changed_files) > 8 else "")
        cost = f" (about ${result.cost_usd:.2f})" if result.cost_usd else ""
        summary = (result.summary or "").strip()
        return ToolOutcome(
            f"Drafted a cleanup on review branch {result.branch}{cost}. {summary} Files: {files}. "
            "Say 'ship it' to approve."
        )

    def _tool_approve_change(self, _input: dict) -> ToolOutcome:
        result = engine.approve(self.ctx.settings)
        return ToolOutcome(("Shipped, sir. " + result.message) if result.ok else result.message)

    def _tool_reject_change(self, _input: dict) -> ToolOutcome:
        return ToolOutcome(engine.reject(self.ctx.settings).message)

    def _tool_list_pending_changes(self, _input: dict) -> ToolOutcome:
        items = engine.list_pending(self.ctx.settings)
        if not items:
            return ToolOutcome("Nothing's waiting to ship, sir.")
        lines = [f"{(p.get('task') or '')[:60]} (branch {p.get('branch')})" for p in items]
        return ToolOutcome("Pending: " + "; ".join(lines) + ".")

    def _tool_fix_recent_crashes(self, _input: dict) -> ToolOutcome:
        drafted = triggers.maybe_fix_crashes(self.ctx.settings)
        if not drafted:
            return ToolOutcome("No new crashes to fix, sir — nothing in the log I haven't handled.")
        branches = ", ".join(d["branch"] for d in drafted)
        return ToolOutcome(
            f"Drafted {len(drafted)} crash fix(es) for review ({branches}). Say 'ship it' to approve, sir."
        )

    # -- navigation ---------------------------------------------------------- #

    def _tool_show_screen(self, tool_input: dict) -> ToolOutcome:
        raw = str(tool_input.get("screen", "")).strip().lower()
        alias = {"apps": "menu", "my apps": "menu", "builds": "menu", "launcher": "menu",
                 "shelf": "menu", "task": "tasks", "run": "tasks", "bench": "tasks",
                 "history": "archive", "versions": "archive", "settings": "settings"}
        key = alias.get(raw, raw)
        labels = {"menu": "your apps", "tasks": "the run list", "archive": "your version history",
                  "settings": "settings"}
        if key not in labels:
            return ToolOutcome("I can show your apps, the run list, your version history, or settings, sir. Which one?")
        self.ctx.show_screen(key)
        return ToolOutcome(f"Pulling up {labels[key]}, sir.")


# --------------------------------------------------------------------------- #
# The multi-turn chat loop (model <-> tools).
# --------------------------------------------------------------------------- #


@dataclass
class ChatTurnResult:
    reply: str
    messages: list  # the full updated Messages-API history (keep for the next turn)
    pending: tuple | None = None  # (name, input) awaiting spoken confirmation
    usages: list = field(default_factory=list)


def run_chat_turn(
    client: Any,
    model: str,
    system: str,
    messages: list,
    router: ActionRouter,
    *,
    max_iters: int = 6,
    max_tokens: int = 1024,
    on_step: Callable[[str], None] | None = None,
) -> ChatTurnResult:
    messages = list(messages)
    pending: tuple | None = None
    reply = ""
    usages: list = []

    for _ in range(max_iters):
        body = client.chat(messages, system=system, tools=router.tools, max_tokens=max_tokens, model=model)
        usages.append(body.get("usage", {}) or {})
        blocks = body.get("content", []) or []
        text = " ".join(
            block.get("text", "")
            for block in blocks
            if block.get("type") == "text" and block.get("text")
        ).strip()
        if text:
            reply = text
        tool_uses = [block for block in blocks if block.get("type") == "tool_use"]

        if body.get("stop_reason") == "tool_use" and tool_uses:
            messages.append({"role": "assistant", "content": blocks})
            results = []
            for use in tool_uses:
                name = use.get("name", "")
                if on_step:
                    on_step(name)
                outcome = router.run(name, use.get("input") or {})
                if outcome.requires_confirmation and outcome.pending:
                    pending = outcome.pending
                results.append(
                    {"type": "tool_result", "tool_use_id": use.get("id", ""), "content": outcome.text}
                )
            messages.append({"role": "user", "content": results})
            continue

        messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": reply}]})
        return ChatTurnResult(reply=reply or "…", messages=messages, pending=pending, usages=usages)

    return ChatTurnResult(
        reply=reply or "I'll need another moment on that, sir.",
        messages=messages,
        pending=pending,
        usages=usages,
    )
