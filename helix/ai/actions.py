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
from helix.selfdev import coder, engine, mailer, triggers
from helix.vision import analyze as vision_analyze, camera as vision_camera
from helix.home import groceries, kroger

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
    {
        "name": "remove_feature",
        "description": (
            "Cleanly REMOVE a feature from HELIX's own code to keep the program lean — the inverse of "
            "improve_helix. Use when the user asks to remove, delete, strip out, or get rid of a feature "
            "(e.g. 'remove the dark mode toggle', 'get rid of the weather panel'). Opus deletes the "
            "feature's module, its tool + handler, any menu/UI entry, and now-dead imports, on a review "
            "branch — nothing is removed until you approve, then HELIX restarts lean."
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
        "name": "approve_change",
        "description": (
            "Approve and merge the most recent drafted code change into HELIX — the 'ship it' command. "
            "Smoke-checks it first (its code must import), then merges it into the app; a restart loads "
            "it. Use when the user says to ship/approve/merge/apply the drafted change, e.g. 'ship it', "
            "'approve that', 'merge it', 'apply it'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reject_change",
        "description": (
            "Discard the most recent drafted code change and delete its branch. Use when the user says "
            "to reject/discard/scrap the change, or 'don't apply that'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pending_changes",
        "description": (
            "List drafted code changes waiting for approval. Use when the user asks what's pending, "
            "waiting to ship, or drafted."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fix_recent_crashes",
        "description": (
            "Check HELIX's error log for recent crashes and draft a fix for any new one (Opus drafts "
            "it on a review branch; nothing is applied automatically). Use when the user asks to fix "
            "crashes, bugs, or errors, or says 'fix what broke'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "look",
        "description": (
            "Look through a camera (one of HELIX's eyes) and answer a question about what you see. Use "
            "for anything visual: 'what is this tool', 'who's at the door', 'what's in the fridge', 'is "
            "the garage light on'. Captures one frame and analyzes it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "camera": {"type": "string", "description": "Which camera/area to look through, e.g. fridge. Omit for the default camera."},
                "question": {"type": "string", "description": "What to find out about the view. Omit for a general description."},
            },
        },
    },
    {
        "name": "look_around",
        "description": (
            "Look through ALL of HELIX's cameras and answer a question across the whole house — e.g. "
            "'check the house', 'is anyone home', 'where did I leave my keys', 'what's running low'. "
            "Captures every registered camera and reports per area."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What to look for across every camera. Omit for a general look."},
            },
        },
    },
    {
        "name": "list_cameras",
        "description": "List HELIX's cameras (its eyes around the house). Use when asked what cameras exist or which areas HELIX can see.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_camera",
        "description": (
            "Register a camera by name and source so HELIX gains an eye there. Source is a USB index "
            "(like '0') or a stream URL (rtsp://… or http://… from a phone IP-cam or a network camera). "
            "Use when the user wants to add or set up a camera, e.g. 'add a fridge camera at rtsp://…'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A short location name, e.g. fridge, laundry, garage, door."},
                "source": {"type": "string", "description": "USB index (e.g. 0) or a stream URL (rtsp/http)."},
            },
            "required": ["name", "source"],
        },
    },
    {
        "name": "add_to_shopping_list",
        "description": (
            "Add an item to the household shopping list. Use when the user says they need / are out of / "
            "are low on something, or 'add X to the list'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item": {"type": "string", "description": "The grocery item, e.g. milk, eggs, dish soap."},
                "qty": {"type": "integer", "description": "How many (default 1)."},
            },
            "required": ["item"],
        },
    },
    {
        "name": "remove_from_shopping_list",
        "description": "Remove an item from the shopping list. Use when the user got it or changed their mind.",
        "input_schema": {
            "type": "object",
            "properties": {"item": {"type": "string", "description": "The item to remove."}},
            "required": ["item"],
        },
    },
    {
        "name": "show_shopping_list",
        "description": "Read back the current shopping list. Use when the user asks what's on the list or what they need.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "order_groceries",
        "description": (
            "Add everything on the shopping list to the user's Fry's (Kroger) cart so they can check out. "
            "This reaches outward toward a purchase, so it ALWAYS requires explicit spoken confirmation. "
            "Use when the user says to order groceries, place the order, or send it to Fry's."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "show_screen",
        "description": (
            "Pop a panel up on the screen for the user to see — the investments table, home, work, "
            "learning, or the live house cameras. Use whenever the user asks to see / show / open / "
            "pull up one of those (e.g. 'show my investments', 'pull up the portfolio', 'open home', "
            "'show cameras' / 'show me the cameras'). No confirmation needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "screen": {
                    "type": "string",
                    "enum": ["investment", "home", "enterprise", "learning", "cameras", "settings"],
                    "description": "Which panel: investment (money/portfolio/trading), home, enterprise (work), learning, cameras (live house cameras), or settings (voice speed + devices).",
                }
            },
            "required": ["screen"],
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
    on_progress: Callable[[str], None] = lambda _msg: None  # live "what HELIX is doing" step text
    show_screen: Callable[[str], None] = lambda _name: None  # pop a panel up (investment/home/enterprise/learning)


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
        if name == "order_groceries":
            items = groceries.list_items(self.ctx.settings)
            result = kroger.order_list(self.ctx.settings, items)
            added, missing = result.get("added", []), result.get("missing", [])
            if added:
                groceries.clear(self.ctx.settings)
            message = f"Added {len(added)} item(s) to your Fry's cart, sir — open the app to check out."
            if missing:
                message += f" I couldn't find: {', '.join(missing)}."
            return message
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
        task = (
            f"Completely and cleanly REMOVE the '{feature}' feature from HELIX, to keep the codebase lean. "
            "Find everything that belongs ONLY to this feature and delete it: its module/files, its entry "
            "in XPERT_TOOLS plus the matching _tool_ handler in helix/ai/actions.py, any launcher/menu "
            "entry and UI panel, settings keys it owns, and any imports that become unused as a result. "
            "Do NOT remove anything shared with other features. Afterward make sure the app still imports "
            "and boots with no dangling references. Summarize exactly what you deleted."
        )
        result = coder.run_coding_task(task, on_step=self.ctx.on_progress)
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

    # -- vision (HELIX's eyes) ----------------------------------------------- #

    def _tool_look(self, tool_input: dict) -> ToolOutcome:
        if not vision_camera.is_available():
            return ToolOutcome("I don't have a camera available, sir — install opencv-python and connect a camera.")
        camera = str(tool_input.get("camera", "")).strip()
        question = str(tool_input.get("question", "")).strip()
        try:
            if camera and vision_camera.get_camera(self.ctx.settings, camera) is not None:
                frame = vision_camera.capture_named(self.ctx.settings, camera)
            else:
                cams = vision_camera.list_cameras(self.ctx.settings)  # default eye: first camera, else the webcam
                source = cams[0]["source"] if cams else vision_camera.DEFAULT_CAMERA_INDEX
                frame = vision_camera.capture_jpeg(source)
        except vision_camera.CameraError as error:
            return ToolOutcome(f"I couldn't get a camera image, sir: {error}")
        try:
            answer = vision_analyze.describe_image(frame, question=question, memory=self.ctx.memory)
        except Exception as error:
            return ToolOutcome(f"I couldn't analyze the image, sir: {error}")
        return ToolOutcome((f"{camera.capitalize()}: " if camera else "") + answer)

    def _tool_look_around(self, tool_input: dict) -> ToolOutcome:
        if not vision_camera.is_available():
            return ToolOutcome("I don't have a camera available, sir — install opencv-python.")
        question = str(tool_input.get("question", "")).strip()
        cams = vision_camera.list_cameras(self.ctx.settings)
        if not cams:
            return ToolOutcome("No cameras are set up yet, sir — add some eyes around the house first.")
        lines = []
        for cam in cams[:8]:  # cap so a big install doesn't run away on cost/time
            name = cam.get("name", "camera")
            try:
                frame = vision_camera.capture_jpeg(cam.get("source", vision_camera.DEFAULT_CAMERA_INDEX))
                answer = vision_analyze.describe_image(frame, question=question, memory=self.ctx.memory, max_tokens=300)
            except Exception as error:
                answer = f"(couldn't reach this camera: {error})"
            lines.append(f"{name}: {answer}")
        return ToolOutcome("\n".join(lines))

    def _tool_list_cameras(self, _input: dict) -> ToolOutcome:
        cams = vision_camera.list_cameras(self.ctx.settings)
        if not cams:
            return ToolOutcome("No cameras are set up yet, sir. Add one with a name and a source.")
        return ToolOutcome("Cameras: " + ", ".join(c["name"] for c in cams) + ".")

    def _tool_add_camera(self, tool_input: dict) -> ToolOutcome:
        name = str(tool_input.get("name", "")).strip()
        source = str(tool_input.get("source", "")).strip()
        if not name or not source:
            return ToolOutcome("I need a name and a source — a USB number like 0, or a stream URL, sir.")
        vision_camera.add_camera(self.ctx.settings, name, source)
        return ToolOutcome(f"Added the {name} camera, sir.")

    def _tool_show_screen(self, tool_input: dict) -> ToolOutcome:
        raw = str(tool_input.get("screen", "")).strip().lower()
        alias = {"investments": "investment", "portfolio": "investment", "money": "investment",
                 "stocks": "investment", "work": "enterprise", "camera": "cameras"}
        key = alias.get(raw, raw)
        labels = {"investment": "the investments", "home": "home", "enterprise": "work",
                  "learning": "learning", "cameras": "the cameras", "settings": "settings"}
        if key not in labels:
            return ToolOutcome("I can show investments, home, work, learning, or the cameras, sir. Which one?")
        self.ctx.show_screen(key)
        return ToolOutcome(f"Pulling up {labels[key]}, sir.")

    # -- groceries / shopping list ------------------------------------------ #

    def _list_tail(self) -> str:
        return f"{len(groceries.list_items(self.ctx.settings))} item(s) on the list."

    def _tool_add_to_shopping_list(self, tool_input: dict) -> ToolOutcome:
        item = str(tool_input.get("item", "")).strip()
        if not item:
            return ToolOutcome("What should I add to the list, sir?")
        try:
            qty = int(tool_input.get("qty", 1) or 1)
        except (TypeError, ValueError):
            qty = 1
        groceries.add_item(self.ctx.settings, item, qty)
        return ToolOutcome(f"Added {item} to the shopping list, sir. " + self._list_tail())

    def _tool_remove_from_shopping_list(self, tool_input: dict) -> ToolOutcome:
        item = str(tool_input.get("item", "")).strip()
        if groceries.remove_item(self.ctx.settings, item):
            return ToolOutcome(f"Removed {item}, sir. " + self._list_tail())
        return ToolOutcome(f"{item} wasn't on the list, sir.")

    def _tool_show_shopping_list(self, _input: dict) -> ToolOutcome:
        return ToolOutcome(groceries.summary(self.ctx.settings))

    def _tool_order_groceries(self, _input: dict) -> ToolOutcome:
        items = groceries.list_items(self.ctx.settings)
        if not items:
            return ToolOutcome("The shopping list is empty, sir — nothing to order.")
        if not kroger.is_configured(self.ctx.settings):
            return ToolOutcome(
                "Fry's isn't connected yet, sir. Here's the list so you can order it: "
                + groceries.summary(self.ctx.settings)
                + ". To let me fill the cart, set up the Kroger account in settings."
            )
        return ToolOutcome(
            f"CONFIRMATION REQUIRED: this will add {len(items)} item(s) to your Fry's cart — "
            f"{groceries.summary(self.ctx.settings)}. Tell the user that and ask them to confirm out loud "
            "before it goes to the cart. They still check out themselves in the Fry's app.",
            requires_confirmation=True,
            pending=("order_groceries", {}),
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
