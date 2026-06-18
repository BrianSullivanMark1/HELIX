from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from helix.brokers.alpaca import AlpacaClient, AlpacaError
from helix.home.notify import is_configured as sms_is_configured, send_reminder, sms_config
from helix.home.tasks import HOME_TASKS_SETTING, due_tasks, normalize_task, task_status
from helix.investment.autopilot import (
    ROSTER_SETTING,
    SPECIAL_SETTING,
    build_roster_review,
    maybe_research_special,
    normalize_roster,
    portfolio_snapshot,
)
from helix.selfdev import coder

# --------------------------------------------------------------------------- #
# Tool schemas — the spoken commands HELIX can act on. Each maps to a real engine/memory call in
# ActionRouter. Anything that moves real money or sends something outward is gated behind an
# explicit spoken confirmation (mirroring the GUI's confirmation dialogs).
# --------------------------------------------------------------------------- #

XPERT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_portfolio",
        "description": (
            "Get the current investment account snapshot: equity/balance, cash, amount invested, "
            "open profit/loss, the top holdings, and whether auto-investing is running and in "
            "paper or live mode. Use whenever the user asks how their money, portfolio, balance, "
            "or positions are doing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recent_sells",
        "description": (
            "List the most recent positions HELIX sold or trimmed, with the reason and the "
            "realized result. Use when the user asks what was sold, what HELIX exited, or 'what "
            "did we sell'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "How many recent sells to list (1-20)."}
            },
        },
    },
    {
        "name": "get_track_record",
        "description": (
            "Get HELIX's realized track record: hit rate, average return, total realized "
            "profit/loss across closed positions, and how many trades on file. Use when the user "
            "asks how the strategy is doing overall or whether it's working."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_learning_status",
        "description": (
            "Get the Learning pillar status from the local usage log: how much HELIX has spent on "
            "Claude (the AI layer) — number of calls and estimated cost today, this month, and "
            "all-time. Use when the user asks about AI/Claude usage or cost, or how the Learning "
            "pillar is doing."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_auto_investing",
        "description": (
            "Start or stop automated investing (the START/STOP loop on the Investment tab). "
            "Starting in paper/practice mode is safe and immediate; starting in LIVE real-money "
            "mode requires explicit spoken confirmation. Stopping is always immediate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "stop"],
                    "description": "Whether to start or stop auto-investing.",
                }
            },
            "required": ["action"],
        },
    },
    {
        "name": "get_home_tasks",
        "description": (
            "Read the user's household task checklist and each task's status (Overdue, Due now, "
            "Due soon, On track). Use when the user asks what's due, what's overdue, or what's on "
            "their list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["all", "due"],
                    "description": "'due' returns only due/overdue tasks; 'all' returns everything.",
                }
            },
        },
    },
    {
        "name": "complete_home_task",
        "description": (
            "Mark a household task as done today (resets its due-date clock). Match the user's "
            "spoken description to one of their tasks. Use when the user says they did/finished "
            "something, e.g. 'mark the laundry done'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task the user did, e.g. 'laundry' or 'water the plants'.",
                }
            },
            "required": ["task"],
        },
    },
    {
        "name": "add_home_task",
        "description": "Add a new recurring task to the household checklist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "The verb, e.g. 'Clean' or 'Pay'."},
                "item": {"type": "string", "description": "The thing, e.g. 'gutters' or 'rent'."},
                "frequency": {
                    "type": "string",
                    "description": "Cadence such as Daily, Weekly, Monthly, Quarterly (default Weekly).",
                },
            },
            "required": ["item"],
        },
    },
    {
        "name": "text_my_tasks",
        "description": (
            "Send a text message to the user's phone listing their due/overdue household tasks. "
            "This sends something outward, so it ALWAYS requires explicit spoken confirmation."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "review_helix_100",
        "description": (
            "Run an on-demand review of the HELIX 100 stock universe: score the roster and "
            "candidates with the AI and report any proposed rotations (it reports the proposal; "
            "the roster auto-rotates on its own quarterly cadence). This makes an AI call and "
            "takes a few seconds. Use when the user asks to review or check the stock universe/roster."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "scout_special_stocks",
        "description": (
            "Run the high-risk 'Special Stocks' scout: ask the AI for speculative, asymmetric "
            "moonshot picks and report the current speculative sleeve. Makes an AI call and takes "
            "a few seconds. Use when the user asks to scout/find moonshots or speculative bets."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "improve_helix",
        "description": (
            "Change HELIX's OWN code with the Opus 4.8 coding agent — add a feature, fix a bug, or "
            "improve the app or yourself. Use whenever the user asks you to build, add, change, fix, or "
            "improve something about HELIX, this app, the program, or yourself (e.g. 'add a dark mode "
            "toggle', 'fix the Home screen', 'make yourself able to X'). The change is drafted on a "
            "separate review branch and is NOT applied to the running app, so it is safe — but it takes "
            "a couple of minutes and uses the Claude subscription. Pass the user's request as the task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "A clear description of the change to make, in the user's words plus any useful detail.",
                }
            },
            "required": ["task"],
        },
    },
]

TOOL_NAMES = {tool["name"] for tool in XPERT_TOOLS}


# --------------------------------------------------------------------------- #
# Affirmative / negative detection for the deterministic spoken-confirmation gate. This is the
# safety check: a money/outward action only fires when the user's actual transcribed words clearly
# say yes — never on the model's interpretation alone.
# --------------------------------------------------------------------------- #

_AFFIRM = (
    "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "do it",
    "go ahead", "go for it", "please do", "affirmative", "correct", "absolutely",
    "send it", "send them", "start it", "of course", "yes please",
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


def _match_task(tasks: list, target: str) -> int | None:
    """Best-effort match a spoken description to a task index (case-insensitive, token overlap)."""
    want = re.sub(r"[^a-z0-9 ]+", " ", (target or "").lower()).strip()
    if not want:
        return None
    want_tokens = set(want.split())
    best_index, best_score = None, 0.0
    for index, task in enumerate(tasks or []):
        action, item, _freq, _last = normalize_task(task)
        name = f"{action} {item}".lower().strip()
        if not name:
            continue
        if want == name or want == item.lower().strip() or want in name:
            return index  # strong, unambiguous-enough match
        name_tokens = set(re.sub(r"[^a-z0-9 ]+", " ", name).split())
        overlap = len(want_tokens & name_tokens)
        if overlap > best_score:
            best_index, best_score = index, overlap
    return best_index if best_score > 0 else None


# --------------------------------------------------------------------------- #
# The action context + router.
# --------------------------------------------------------------------------- #


@dataclass
class ActionContext:
    """Dependencies the router needs, kept free of any Qt types so the router is unit-testable.

    The UI (XpertTab) supplies these. The investment-state callables read values snapshotted on the
    main thread; start_auto/stop_auto are fire-and-forget (the UI marshals them to the main thread).
    """

    memory: Any
    settings: Any
    research_fn: Callable[[str], str]
    is_live: Callable[[], bool] = lambda: False
    auto_running: Callable[[], bool] = lambda: False
    keys_ready: Callable[[], bool] = lambda: False
    start_auto: Callable[[], None] = lambda: None
    stop_auto: Callable[[], None] = lambda: None
    refresh_home: Callable[[], None] = lambda: None


@dataclass
class ToolOutcome:
    text: str  # the tool_result fed back to the model
    requires_confirmation: bool = False
    pending: tuple | None = None  # (name, input) to execute once the user confirms out loud


class ActionRouter:
    """Maps tool calls to real HELIX engine/memory functions. Gated tools defer their side effect
    until ActionRouter.execute_confirmed is called after a spoken yes."""

    def __init__(self, ctx: ActionContext) -> None:
        self.ctx = ctx

    @property
    def tools(self) -> list:
        return XPERT_TOOLS

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
        """Perform a previously-gated side effect, now that the user has confirmed out loud."""
        tool_input = tool_input or {}
        if name == "text_my_tasks":
            tasks = self.ctx.settings.get(HOME_TASKS_SETTING) or []
            result = send_reminder(tasks, self.ctx.settings)
            return f"Sent, sir. {result}"
        if name == "set_auto_investing":
            self.ctx.start_auto()
            return "Confirmed — live auto-investing is starting now, sir."
        return "Done, sir."

    # -- read-only investment tools ----------------------------------------- #

    def _tool_get_portfolio(self, _input: dict) -> ToolOutcome:
        try:
            client = AlpacaClient.from_settings(self.ctx.settings)
            snapshot = portfolio_snapshot(client.get_account(), client.get_positions())
        except AlpacaError:
            return ToolOutcome(
                "Alpaca isn't connected, so there's no balance to read. The user needs to save "
                "their Alpaca keys on the Investment tab."
            )
        sign = "+" if snapshot.unrealized_pl >= 0 else ""
        parts = [
            f"Equity ${snapshot.equity:,.2f}, cash ${snapshot.cash:,.2f}, invested "
            f"${snapshot.market_value:,.2f}, open P/L {sign}${snapshot.unrealized_pl:,.2f} across "
            f"{len(snapshot.positions)} positions."
        ]
        top = snapshot.positions[:6]
        if top:
            holdings = ", ".join(
                f"{p.symbol} ${p.market_value:,.0f} ({p.unrealized_plpc:+.1f}%)" for p in top
            )
            parts.append(f"Top holdings: {holdings}.")
        running = "running" if self.ctx.auto_running() else "stopped"
        mode = "live (real money)" if self.ctx.is_live() else "paper (practice)"
        parts.append(f"Auto-investing is {running}; mode is {mode}.")
        return ToolOutcome(" ".join(parts))

    def _tool_get_recent_sells(self, tool_input: dict) -> ToolOutcome:
        try:
            limit = int(tool_input.get("limit", 6))
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(20, limit))
        sells = self.ctx.memory.list_sells(limit)
        if not sells:
            return ToolOutcome("No sells are on record yet, sir.")
        lines = []
        for sell in sells:
            symbol = sell.get("symbol", "?")
            reason = sell.get("reason", "")
            return_pct = sell.get("return_pct")
            outcome = f" ({return_pct:+.1f}%)" if isinstance(return_pct, (int, float)) else ""
            lines.append(f"{symbol}: {reason}{outcome}")
        return ToolOutcome("Recent sells — " + "; ".join(lines) + ".")

    def _tool_get_track_record(self, _input: dict) -> ToolOutcome:
        perf = self.ctx.memory.strategy_performance()
        digest = self.ctx.memory.investment_digest()
        if perf.get("closed", 0) <= 0:
            return ToolOutcome(
                "No closed trades yet, so there's no realized track record to judge from — "
                f"{digest.get('trades', 0)} trades on file since {digest.get('since') or 'recently'}."
            )
        return ToolOutcome(
            f"Hit rate {perf['hit_rate']}% over {perf['closed']} closed positions, average return "
            f"{perf['avg_return_pct']:+.1f}%, realized P/L ${perf['realized_pl']:+,.2f}. "
            f"{digest.get('trades', 0)} trades on file since {digest.get('since') or 'recently'}."
        )

    def _tool_get_learning_status(self, _input: dict) -> ToolOutcome:
        usage = self.ctx.memory.ai_usage_summary()
        return ToolOutcome(
            f"Learning (the AI layer): {usage.get('calls', 0)} Claude call(s) logged — about "
            f"${usage.get('today_cost', 0):.4f} today, ${usage.get('month_cost', 0):.4f} this month, "
            f"${usage.get('total_cost', 0):.4f} all-time (estimated). Enterprise is still a roadmap "
            "placeholder with no data yet."
        )

    # -- auto-investing control (gated for live) ---------------------------- #

    def _tool_set_auto_investing(self, tool_input: dict) -> ToolOutcome:
        action = str(tool_input.get("action", "")).strip().lower()
        if action == "stop":
            self.ctx.stop_auto()
            return ToolOutcome("Auto-investing stopped, sir.")
        if action == "start":
            if not self.ctx.keys_ready():
                return ToolOutcome(
                    "Auto-investing can't start without Alpaca keys — the user needs to save them "
                    "on the Investment tab first."
                )
            if self.ctx.auto_running():
                return ToolOutcome("Auto-investing is already running, sir.")
            if self.ctx.is_live():
                return ToolOutcome(
                    "CONFIRMATION REQUIRED: starting LIVE auto-investing will place REAL-money "
                    "orders automatically. Tell the user that plainly and ask them to confirm out "
                    "loud before it starts.",
                    requires_confirmation=True,
                    pending=("set_auto_investing", {"action": "start"}),
                )
            self.ctx.start_auto()
            return ToolOutcome("Starting auto-investing on paper money now, sir.")
        return ToolOutcome("Tell me whether to start or stop auto-investing.")

    # -- home tasks --------------------------------------------------------- #

    def _tool_get_home_tasks(self, tool_input: dict) -> ToolOutcome:
        tasks = self.ctx.settings.get(HOME_TASKS_SETTING) or []
        if str(tool_input.get("filter", "all")).lower() == "due":
            due = due_tasks(tasks)
            if not due:
                return ToolOutcome("Nothing's due — the user is all caught up.")
            items = ", ".join(
                f"{entry['action']} {entry['item']}".strip()
                + (" (overdue)" if entry["status"] == "Overdue" else " (due)")
                for entry in due
            )
            return ToolOutcome(f"Due now: {items}.")
        listed = []
        for task in tasks:
            action, item, freq, last = normalize_task(task)
            name = f"{action} {item}".strip()
            if name:
                listed.append(f"{name} — {task_status(freq, last)}")
        if not listed:
            return ToolOutcome("There are no household tasks on the list yet.")
        return ToolOutcome("Tasks: " + "; ".join(listed) + ".")

    def _tool_complete_home_task(self, tool_input: dict) -> ToolOutcome:
        target = str(tool_input.get("task", "")).strip()
        tasks = list(self.ctx.settings.get(HOME_TASKS_SETTING) or [])
        index = _match_task(tasks, target)
        if index is None:
            names = ", ".join(
                f"{normalize_task(t)[0]} {normalize_task(t)[1]}".strip() for t in tasks
            )
            return ToolOutcome(
                f"I couldn't find a task matching '{target}'. The list is: {names or 'empty'}."
            )
        row = normalize_task(tasks[index])
        row[3] = datetime.now().strftime("%Y-%m-%d")
        tasks[index] = row
        self.ctx.settings.set(HOME_TASKS_SETTING, tasks)
        self.ctx.refresh_home()
        name = f"{row[0]} {row[1]}".strip()
        return ToolOutcome(f"Marked '{name}' done today, sir.")

    def _tool_add_home_task(self, tool_input: dict) -> ToolOutcome:
        action = str(tool_input.get("action", "")).strip()
        item = str(tool_input.get("item", "")).strip()
        frequency = str(tool_input.get("frequency", "") or "Weekly").strip() or "Weekly"
        if not (action or item):
            return ToolOutcome("What task should I add, sir?")
        tasks = list(self.ctx.settings.get(HOME_TASKS_SETTING) or [])
        tasks.append([action, item, frequency, ""])
        self.ctx.settings.set(HOME_TASKS_SETTING, tasks)
        self.ctx.refresh_home()
        name = f"{action} {item}".strip()
        return ToolOutcome(f"Added '{name}' on a {frequency.lower()} cadence, sir.")

    def _tool_text_my_tasks(self, _input: dict) -> ToolOutcome:
        if not sms_is_configured(self.ctx.settings):
            return ToolOutcome(
                "Text reminders aren't set up — the user needs to add their phone, carrier, and "
                "Gmail app password on the Home tab first."
            )
        phone = sms_config(self.ctx.settings).get("phone", "") or "their phone"
        return ToolOutcome(
            f"CONFIRMATION REQUIRED: this will send a text to {phone} listing the due tasks. Tell "
            "the user that and ask them to confirm out loud before sending.",
            requires_confirmation=True,
            pending=("text_my_tasks", {}),
        )

    # -- universe / special scout (AI calls) -------------------------------- #

    def _tool_review_helix_100(self, _input: dict) -> ToolOutcome:
        roster = normalize_roster(self.ctx.settings.get(ROSTER_SETTING, ""))
        holdings: dict = {}
        try:
            client = AlpacaClient.from_settings(self.ctx.settings)
            holdings = {str(p.get("symbol", "")).upper(): p for p in client.get_positions()}
        except AlpacaError:
            holdings = {}
        review = build_roster_review(roster, holdings, self.ctx.research_fn, memory=self.ctx.memory)
        if not review.swaps:
            return ToolOutcome(
                f"Reviewed the HELIX 100 ({len(roster)} names) — no candidate beats the weakest "
                "holding by enough to rotate. The roster's holding up."
            )
        swaps = "; ".join(f"{s.drop_symbol} out for {s.add_symbol}" for s in review.swaps[:5])
        more = "" if len(review.swaps) <= 5 else f" and {len(review.swaps) - 5} more"
        return ToolOutcome(
            f"Reviewed the HELIX 100. {len(review.swaps)} rotation(s) proposed: {swaps}{more}. "
            "These apply automatically on the quarterly cadence."
        )

    def _tool_scout_special_stocks(self, _input: dict) -> ToolOutcome:
        # research_days=0 forces a fresh scout on demand (the normal cadence is nightly).
        symbols, researched = maybe_research_special(
            self.ctx.settings, self.ctx.memory, self.ctx.research_fn, research_days=0
        )
        if not symbols:
            return ToolOutcome("I scouted for moonshots but nothing convincing turned up, sir.")
        shown = ", ".join(symbols[:8]) + ("…" if len(symbols) > 8 else "")
        verb = "Scouted" if researched else "Current"
        return ToolOutcome(
            f"{verb} the speculative sleeve — {shown}. Each is sized small and bought only from "
            "house money above your protected principal."
        )

    # -- self-improvement (HELIX edits its own code) ------------------------- #

    def _tool_improve_helix(self, tool_input: dict) -> ToolOutcome:
        task = str(tool_input.get("task", "")).strip()
        if not task:
            return ToolOutcome("Tell me what to build, change, or fix in HELIX, sir.")
        result = coder.run_coding_task(task)
        if not result.ok:
            return ToolOutcome(f"I couldn't draft that change, sir: {result.error}")
        files = ", ".join(result.changed_files[:8]) + ("…" if len(result.changed_files) > 8 else "")
        cost = f" (about ${result.cost_usd:.2f})" if result.cost_usd else ""
        summary = (result.summary or "").strip()
        return ToolOutcome(
            f"Done, sir — I drafted that on review branch {result.branch}{cost}. {summary} "
            f"Files changed: {files or 'none'}. It's committed to a branch for your review and is NOT "
            "applied to the running app yet — approving and merging it is the next step."
        )


# --------------------------------------------------------------------------- #
# The chat-turn loop: drives ClaudeClient.chat through any tool-use rounds and returns the spoken
# reply plus any pending (confirmation-gated) action. Pure orchestration — testable with a fake
# client + fake router.
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
