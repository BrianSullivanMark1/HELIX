from __future__ import annotations

import argparse

from helix.core.memory import SQLiteMemory
from helix.investment.models import InvestmentProfile, RISK_LEVELS
from helix.investment.planner import build_briefing, render_briefing


def add_investment_subparser(subparsers: argparse._SubParsersAction) -> None:
    investment = subparsers.add_parser("investment", help="Investment planning tools")
    investment_subcommands = investment.add_subparsers(dest="investment_command")
    investment.set_defaults(handler=handle_default)

    profile = investment_subcommands.add_parser("profile", help="Create or update your investment profile")
    profile.set_defaults(handler=handle_profile)

    status = investment_subcommands.add_parser("status", help="Show investment status")
    status.set_defaults(handler=handle_status)

    watchlist = investment_subcommands.add_parser("watchlist", help="Manage investment watchlist")
    watchlist_subcommands = watchlist.add_subparsers(dest="watchlist_command")
    watchlist.set_defaults(handler=handle_default)

    watchlist_list = watchlist_subcommands.add_parser("list", help="List watchlist items")
    watchlist_list.set_defaults(handler=handle_watchlist_list)

    watchlist_add = watchlist_subcommands.add_parser("add", help="Add or update a watchlist item")
    watchlist_add.add_argument("symbol", help="Ticker symbol")
    watchlist_add.add_argument("--thesis", required=True, help="Short reason this belongs on the watchlist")
    watchlist_add.add_argument("--target-price", type=float, help="Price where you want HELIX to flag it")
    watchlist_add.add_argument(
        "--max-allocation-pct",
        type=float,
        help="Maximum portfolio allocation percentage for this idea",
    )
    watchlist_add.set_defaults(handler=handle_watchlist_add)

    watchlist_remove = watchlist_subcommands.add_parser("remove", help="Remove a watchlist item")
    watchlist_remove.add_argument("symbol", help="Ticker symbol")
    watchlist_remove.set_defaults(handler=handle_watchlist_remove)

    journal = investment_subcommands.add_parser("journal", help="Manage investment decision journal")
    journal_subcommands = journal.add_subparsers(dest="journal_command")
    journal.set_defaults(handler=handle_default)

    journal_add = journal_subcommands.add_parser("add", help="Add a decision journal entry")
    journal_add.add_argument("title", help="Entry title")
    journal_add.add_argument("--body", required=True, help="Reasoning, decision, or observation")
    journal_add.add_argument("--type", default="investment", help="Entry type")
    journal_add.set_defaults(handler=handle_journal_add)

    journal_list = journal_subcommands.add_parser("list", help="List recent journal entries")
    journal_list.add_argument("--limit", type=int, default=10)
    journal_list.set_defaults(handler=handle_journal_list)


def handle_profile(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    existing = memory.get_investment_profile()
    print("HELIX Investment Profile")
    print("Press Enter to keep the shown default.")

    profile = InvestmentProfile(
        monthly_income=_ask_float("Monthly after-tax income", existing, "monthly_income", 0.0),
        monthly_expenses=_ask_float("Monthly core expenses", existing, "monthly_expenses", 0.0),
        cash_savings=_ask_float("Current cash savings", existing, "cash_savings", 0.0),
        debt_total=_ask_float("Total debt", existing, "debt_total", 0.0),
        monthly_debt_payment=_ask_float("Monthly debt payment", existing, "monthly_debt_payment", 0.0),
        current_investments=_ask_float("Current investment balance", existing, "current_investments", 0.0),
        target_emergency_months=_ask_int("Emergency fund months", existing, "target_emergency_months", 6),
        risk_tolerance=_ask_choice("Risk tolerance", existing, "risk_tolerance", "balanced", RISK_LEVELS),
        primary_goal=_ask_str("Primary investment goal", existing, "primary_goal", "Build long-term wealth"),
        goal_amount=_ask_float("Goal amount", existing, "goal_amount", 100000.0),
        goal_years=_ask_int("Goal timeline in years", existing, "goal_years", 10),
        expected_annual_return=_ask_percentage(
            "Expected annual return percent",
            existing,
            "expected_annual_return",
            7.0,
        ),
    )

    memory.save_investment_profile(profile.to_record())
    print()
    print(render_briefing(build_briefing(profile.to_record())))
    return 0


def handle_status(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    print(render_briefing(build_briefing(memory.get_investment_profile())))
    return 0


def handle_watchlist_add(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    memory.upsert_watchlist_item(
        symbol=args.symbol,
        thesis=args.thesis,
        target_price=args.target_price,
        max_allocation_pct=args.max_allocation_pct,
    )
    print(f"Added {args.symbol.upper()} to watchlist.")
    return 0


def handle_watchlist_list(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    items = memory.list_watchlist()
    if not items:
        print("Watchlist is empty.")
        return 0

    for item in items:
        target = _optional_money(item["target_price"])
        allocation = _optional_percent(item["max_allocation_pct"])
        print(f"{item['symbol']}: {item['thesis']}")
        print(f"  target: {target} | max allocation: {allocation}")
    return 0


def handle_watchlist_remove(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    removed = memory.remove_watchlist_item(args.symbol)
    if removed:
        print(f"Removed {args.symbol.upper()} from watchlist.")
    else:
        print(f"{args.symbol.upper()} was not on the watchlist.")
    return 0


def handle_journal_add(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    memory.add_journal_entry(args.type, args.title, args.body)
    print("Journal entry added.")
    return 0


def handle_journal_list(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    entries = memory.list_journal_entries(limit=args.limit)
    if not entries:
        print("Journal is empty.")
        return 0

    for entry in entries:
        print(f"#{entry['id']} [{entry['entry_type']}] {entry['title']} ({entry['created_at']})")
        print(f"  {entry['body']}")
    return 0


def handle_default(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    print("Choose an investment command. Try: python main.py investment status")
    return 2


def _ask_float(
    prompt: str,
    existing: dict | None,
    key: str,
    default: float,
) -> float:
    value = _ask_raw(prompt, existing, key, default)
    try:
        return float(value)
    except ValueError:
        print("Invalid number; using 0.")
        return 0.0


def _ask_int(
    prompt: str,
    existing: dict | None,
    key: str,
    default: int,
) -> int:
    value = _ask_raw(prompt, existing, key, default)
    try:
        return int(value)
    except ValueError:
        print("Invalid integer; using default.")
        return default


def _ask_percentage(
    prompt: str,
    existing: dict | None,
    key: str,
    default_percent: float,
) -> float:
    if existing and key in existing:
        default_display = float(existing[key]) * 100
    else:
        default_display = default_percent

    value = input(f"{prompt} [{default_display}]: ").strip()
    if not value:
        value = str(default_display)
    try:
        return float(value) / 100
    except ValueError:
        print("Invalid percentage; using default.")
        return default_display / 100


def _ask_choice(
    prompt: str,
    existing: dict | None,
    key: str,
    default: str,
    choices: tuple[str, ...],
) -> str:
    current = str(existing.get(key, default)) if existing else default
    value = input(f"{prompt} {choices} [{current}]: ").strip().lower()
    if not value:
        value = current
    if value not in choices:
        print(f"Invalid choice; using {default}.")
        return default
    return value


def _ask_str(
    prompt: str,
    existing: dict | None,
    key: str,
    default: str,
) -> str:
    return str(_ask_raw(prompt, existing, key, default))


def _ask_raw(
    prompt: str,
    existing: dict | None,
    key: str,
    default: object,
) -> str:
    current = existing.get(key, default) if existing else default
    value = input(f"{prompt} [{current}]: ").strip()
    return value if value else str(current)


def _optional_money(value: float | None) -> str:
    if value is None:
        return "not set"
    return f"${float(value):,.2f}"


def _optional_percent(value: float | None) -> str:
    if value is None:
        return "not set"
    return f"{float(value):.2f}%"
