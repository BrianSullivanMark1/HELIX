from __future__ import annotations

import argparse
import sys
import time

from helix import __version__
from helix.ai.claude import (
    ClaudeClient,
    ClaudeConfig,
    ClaudeError,
    DEFAULT_RESEARCH_MODEL,
    estimate_cost,
)
from helix.ai.mock import generate_mock_portfolio_research, generate_mock_roster_review
from helix.brokers.alpaca import (
    ALPACA_ENV_PAPER,
    ALPACA_ENVIRONMENT_SETTING,
    AlpacaClient,
    AlpacaError,
)
from helix.core.config import load_config
from helix.core.daemon import run_core
from helix.core.memory import SQLiteMemory
from helix.core.settings import AppSettings
from helix.interfaces.api import HelixApiServer
from helix.investment.autopilot import (
    DEFAULT_RATING_MAX_AGE_DAYS,
    RESEARCH_TIMEOUT_SECONDS,
    ROSTER_SETTING,
    build_plan,
    research_max_tokens,
    build_rebalance_plan,
    build_roster_review,
    execute_plan,
    execute_rebalance,
    generate_rating_scorecard,
    merge_universe,
    normalize_roster,
    render_plan,
    render_rebalance_plan,
    render_roster_review,
)
from helix.investment.backtest import gather_backtest
from helix.investment.cli import add_investment_subparser
from helix.investment.planner import build_briefing, render_briefing
from helix.home.tasks import HOME_TASKS_SETTING, due_tasks
from helix.home.notify import NotifyError, is_configured, send_reminder


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    if argv is None and len(sys.argv) == 1:
        argv = ["ui"]
    args = parser.parse_args(argv)

    config = load_config()
    memory = SQLiteMemory(config.db_path)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    return handler(args, memory)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="HELIX",
        description="Home Enterprise Learning Investment Expert",
    )
    parser.add_argument("--version", action="version", version=f"HELIX {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    brief = subparsers.add_parser("brief", help="Show the current daily briefing")
    brief.set_defaults(handler=handle_brief)

    run = subparsers.add_parser("run", help="Run HELIX as an all-day local process")
    run.add_argument("--interval", type=int, default=3600, help="Seconds between briefings")
    run.add_argument("--once", action="store_true", help="Run one loop and exit")
    run.set_defaults(handler=handle_run)

    api = subparsers.add_parser("api", help="Run local HTTP API for other devices")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8765)
    api.set_defaults(handler=handle_api)

    ui = subparsers.add_parser("ui", help="Open the HELIX desktop UI")
    ui.set_defaults(handler=handle_ui)

    invest = subparsers.add_parser("invest", help="Build a paper invest plan from the watchlist")
    invest.add_argument("--cash", type=float, default=None, help="Cash to deploy (default: saved amount)")
    invest.add_argument("--preset", default="Balanced", choices=["Balanced", "Aggressive"])
    invest.add_argument("--ai", default="mock", choices=["mock", "claude"], help="Research source")
    invest.add_argument("--model", default=DEFAULT_RESEARCH_MODEL, help="Claude model when --ai claude")
    invest.add_argument("--execute", action="store_true", help="Submit buys as Alpaca PAPER orders")
    invest.set_defaults(handler=handle_invest)

    rebalance = subparsers.add_parser(
        "rebalance", help="Plan/execute a buy+sell rebalance from live Alpaca positions"
    )
    rebalance.add_argument("--preset", default="Aggressive", choices=["Balanced", "Aggressive"])
    rebalance.add_argument("--ai", default="mock", choices=["mock", "claude"], help="Research source")
    rebalance.add_argument("--model", default=DEFAULT_RESEARCH_MODEL, help="Claude model when --ai claude")
    rebalance.add_argument("--max-pos", type=float, default=20.0, help="Max %% of equity per stock")
    rebalance.add_argument("--cash-buffer", type=float, default=10.0, help="%% of equity kept in cash")
    rebalance.add_argument("--execute", action="store_true", help="Submit orders as Alpaca PAPER trades")
    rebalance.set_defaults(handler=handle_rebalance)

    autopilot = subparsers.add_parser(
        "autopilot", help="Run the rebalance loop automatically on an interval"
    )
    autopilot.add_argument("--interval", type=int, default=900, help="Seconds between cycles")
    autopilot.add_argument("--preset", default="Aggressive", choices=["Balanced", "Aggressive"])
    autopilot.add_argument("--ai", default="mock", choices=["mock", "claude"])
    autopilot.add_argument("--model", default=DEFAULT_RESEARCH_MODEL)
    autopilot.add_argument("--max-pos", type=float, default=20.0)
    autopilot.add_argument("--cash-buffer", type=float, default=10.0)
    autopilot.add_argument("--once", action="store_true", help="Run one cycle and exit")
    autopilot.add_argument("--ignore-hours", action="store_true", help="Trade even when the market is closed")
    autopilot.add_argument("--allow-live", action="store_true", help="Permit live (real money) automation")
    autopilot.set_defaults(handler=handle_autopilot)

    roster = subparsers.add_parser(
        "roster", help="Review/rotate the HELIX 100 universe (discover + rank + swap)"
    )
    roster.add_argument("--ai", default="mock", choices=["mock", "claude"], help="Research source")
    roster.add_argument("--model", default=DEFAULT_RESEARCH_MODEL, help="Claude model when --ai claude")
    roster.add_argument("--candidates", type=int, default=30, help="How many new names to consider")
    roster.add_argument("--max-swaps", type=int, default=10, help="Max rotations this review")
    roster.add_argument(
        "--min-margin", type=float, default=8.0, help="Score points a candidate must beat the dropped name by"
    )
    roster.add_argument("--apply", action="store_true", help="Write the new roster (default: dry run)")
    roster.set_defaults(handler=handle_roster)

    scorecard = subparsers.add_parser(
        "scorecard", help="Prediction scorecard: realized forward returns by rating confidence (§28)"
    )
    scorecard.add_argument("--days", type=int, default=365, help="Look-back window for rating snapshots")
    scorecard.set_defaults(handler=handle_scorecard)

    backtest = subparsers.add_parser(
        "backtest", help="Backtest the deterministic strategy on real history: conviction vs equal-weight vs S&P (§29)"
    )
    backtest.add_argument("--days", type=int, default=180, help="Look-back window in days")
    backtest.add_argument("--cadence-days", type=int, default=7, help="Rebalance every N days")
    backtest.add_argument("--cash-buffer", type=float, default=10.0, help="%% of equity kept in cash")
    backtest.add_argument(
        "--max-positions", type=int, default=0,
        help="Test a specific top-N concentration (0 = sweep 0/50/25/10 to find the best N)",
    )
    backtest.set_defaults(handler=handle_backtest)

    notify = subparsers.add_parser("notify", help="Text overdue Home tasks to your phone (email-to-SMS)")
    notify.add_argument("--always", action="store_true", help="Send even if nothing is due")
    notify.set_defaults(handler=handle_notify)

    add_investment_subparser(subparsers)
    return parser


def handle_brief(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    print(render_briefing(build_briefing(memory.get_investment_profile())))
    return 0


def handle_run(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    interval = max(10, args.interval)
    return run_core(memory=memory, interval_seconds=interval, once=args.once)


def handle_api(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    HelixApiServer(memory=memory, host=args.host, port=args.port).serve_forever()
    return 0


def handle_ui(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    try:
        from helix.interfaces.qt_app import run_qt_app
    except ModuleNotFoundError as error:
        if error.name == "PyQt6":
            print("PyQt6 is required for the desktop UI.")
            print("Install it with: python -m pip install PyQt6")
            return 1
        raise

    return run_qt_app(memory)


def handle_invest(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    settings = AppSettings()
    cash = args.cash if args.cash is not None else float(settings.get("investment_amount", 100.0))
    watchlist = memory.list_watchlist()
    if not watchlist:
        print("Watchlist is empty. Add tickers first:")
        print('  python main.py investment watchlist add VOO --thesis "broad market core"')
        return 1

    if args.ai == "claude":
        client = ClaudeClient(ClaudeConfig(model=args.model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))

        def research_fn(prompt: str) -> str:
            text = client.complete(prompt, max_tokens=research_max_tokens(settings))
            usage = client.last_usage or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            memory.record_ai_usage(
                args.model,
                input_tokens,
                output_tokens,
                estimate_cost(args.model, input_tokens, output_tokens),
            )
            return text

        try:
            plan = build_plan(cash, watchlist, research_fn, args.preset)
        except ClaudeError as error:
            print(f"Claude research failed: {error}")
            return 1
    else:
        plan = build_plan(
            cash,
            watchlist,
            lambda _prompt: generate_mock_portfolio_research(watchlist, args.preset),
            args.preset,
        )

    print(render_plan(plan))

    if not args.execute:
        print("\nDry run. Re-run with --execute to submit PAPER orders.")
        return 0

    environment = settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
    if environment != ALPACA_ENV_PAPER:
        print("Refusing to execute: Alpaca environment is not Paper. The CLI submits paper orders only.")
        return 1

    try:
        client = AlpacaClient.from_settings(settings)
    except AlpacaError as error:
        print(f"Alpaca not ready: {error}")
        return 1

    print()
    for proposal, outcome in execute_plan(plan.buys, client, memory, mode_label="paper"):
        print(f"{proposal.symbol}: {outcome}")
    return 0


def handle_rebalance(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    settings = AppSettings()
    try:
        client = AlpacaClient.from_settings(settings)
        account = client.get_account()
        positions = client.get_positions()
    except AlpacaError as error:
        print(f"Alpaca not ready: {error}")
        return 1

    def _f(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    total_equity = _f(account.get("equity") or account.get("portfolio_value"))
    cash = _f(account.get("cash"))
    holdings = {p.get("symbol", ""): _f(p.get("market_value")) for p in positions if p.get("symbol")}
    holdings_pl = {
        p.get("symbol", ""): {
            "market_value": _f(p.get("market_value")),
            "unrealized_pl": _f(p.get("unrealized_pl")),
            "unrealized_plpc": _f(p.get("unrealized_plpc")),
        }
        for p in positions
        if p.get("symbol")
    }
    watchlist = memory.list_watchlist()
    if not watchlist and not holdings:
        print("Nothing to do: empty watchlist and no positions. Add tickers first.")
        return 1

    max_position_pct = args.max_pos / 100.0
    cash_buffer_pct = args.cash_buffer / 100.0

    if args.ai == "claude":
        ai_client = ClaudeClient(ClaudeConfig(model=args.model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))

        def research_fn(prompt: str) -> str:
            text = ai_client.complete(prompt, max_tokens=research_max_tokens(settings))
            usage = ai_client.last_usage or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            memory.record_ai_usage(
                args.model, input_tokens, output_tokens, estimate_cost(args.model, input_tokens, output_tokens)
            )
            return text

        try:
            plan = build_rebalance_plan(
                total_equity, cash, holdings, watchlist, research_fn,
                max_position_pct=max_position_pct, cash_buffer_pct=cash_buffer_pct, preset=args.preset,
                memory=memory,
                rating_max_age_days=DEFAULT_RATING_MAX_AGE_DAYS,
                on_issue=lambda message: print(f"[research] {message}"),
            )
        except ClaudeError as error:
            print(f"Claude research failed: {error}")
            return 1
    else:
        universe = merge_universe(watchlist, holdings)
        plan = build_rebalance_plan(
            total_equity, cash, holdings, watchlist,
            lambda _prompt: generate_mock_portfolio_research(universe, args.preset),
            max_position_pct=max_position_pct, cash_buffer_pct=cash_buffer_pct, preset=args.preset,
            memory=memory,
            rating_max_age_days=DEFAULT_RATING_MAX_AGE_DAYS,
        )

    print(render_rebalance_plan(plan))

    if not args.execute:
        print("\nDry run. Re-run with --execute to submit PAPER orders.")
        return 0

    environment = settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
    if environment != ALPACA_ENV_PAPER:
        print("Refusing to execute: Alpaca environment is not Paper. The CLI submits paper orders only.")
        return 1

    print()
    for action, outcome in execute_rebalance(plan.actions, client, memory, mode_label="paper", holdings_pl=holdings_pl):
        print(f"{action.side.upper()} {action.symbol}: {outcome}")
    return 0


def handle_roster(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    settings = AppSettings()
    roster = normalize_roster(settings.get(ROSTER_SETTING, ""))
    if not roster:
        print("No roster yet. Open the Investment tab and Load the 100-Stock Basket, or add tickers.")
        return 1

    def _f(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    holdings: dict[str, float] = {}
    try:
        client = AlpacaClient.from_settings(settings)
        holdings = {p.get("symbol", ""): _f(p.get("market_value")) for p in client.get_positions() if p.get("symbol")}
    except AlpacaError:
        holdings = {}  # the review works without live holdings; they only flag which drops are forced sells

    if args.ai == "claude":
        ai_client = ClaudeClient(ClaudeConfig(model=args.model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))

        def research_fn(prompt: str) -> str:
            text = ai_client.complete(prompt, max_tokens=research_max_tokens(settings))
            usage = ai_client.last_usage or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            memory.record_ai_usage(
                args.model, input_tokens, output_tokens, estimate_cost(args.model, input_tokens, output_tokens)
            )
            return text

        try:
            review = build_roster_review(
                roster, holdings, research_fn,
                max_swaps=args.max_swaps, min_margin=args.min_margin, n_candidates=args.candidates,
                memory=memory,
            )
        except ClaudeError as error:
            print(f"Claude research failed: {error}")
            return 1
    else:
        review = build_roster_review(
            roster, holdings,
            lambda _prompt: generate_mock_roster_review(roster, holdings, args.candidates),
            max_swaps=args.max_swaps, min_margin=args.min_margin, n_candidates=args.candidates,
            memory=memory,
        )

    print(render_roster_review(review))

    if not args.apply:
        print("\nDry run. Re-run with --apply to update the roster (trades happen on the next rebalance).")
        return 0

    settings.set(ROSTER_SETTING, ", ".join(review.new_roster))
    print(f"\nRoster updated: {len(review.swaps)} swap(s) applied; {len(review.new_roster)} names now.")
    if review.swaps:
        print("Rotated-out names still held will be sold on the next rebalance/cycle.")
    return 0


def handle_scorecard(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    """Print the prediction scorecard (§28): do high-conviction buys actually beat low-conviction
    and the S&P? Deterministic — reads the rating_outcomes log and fetches daily bars from Alpaca
    (no Claude call, no trading)."""
    settings = AppSettings()
    try:
        client = AlpacaClient.from_settings(settings)
    except AlpacaError as error:
        print(f"Alpaca not ready (price history is needed to score outcomes): {error}")
        return 1
    report, _summary = generate_rating_scorecard(memory, client, days=args.days)
    print(report)
    return 0


def handle_backtest(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    """Backtest the deterministic strategy (§29): replay real daily bars through `build_rebalance_plan`
    with the current buy ratings held fixed, conviction-weighted vs equal-weight vs S&P. No Claude
    call (ratings stubbed for replay); only Alpaca price reads."""
    settings = AppSettings()
    try:
        client = AlpacaClient.from_settings(settings)
    except AlpacaError as error:
        print(f"Alpaca not ready (price history is needed to backtest): {error}")
        return 1
    sweep = (0, args.max_positions) if args.max_positions > 0 else (0, 50, 25, 10)
    report, _results = gather_backtest(
        memory, client, days=args.days, rebalance_every_days=args.cadence_days,
        cash_buffer_pct=args.cash_buffer / 100.0, max_positions_sweep=sweep,
    )
    print(report)
    return 0


def handle_notify(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    settings = AppSettings()
    tasks = settings.get(HOME_TASKS_SETTING) or []
    if not is_configured(settings):
        print("SMS not configured. Set your phone, carrier, and Gmail app password in the Home tab.")
        return 1
    due = due_tasks(tasks)
    if not due and not args.always:
        print("Nothing due - no text sent. (Use --always to send the all-clear anyway.)")
        return 0
    try:
        result = send_reminder(tasks, settings)
    except NotifyError as error:
        print(str(error))
        return 1
    print(result)
    return 0


def handle_autopilot(args: argparse.Namespace, memory: SQLiteMemory) -> int:
    settings = AppSettings()
    interval = max(30, args.interval)
    environment = settings.get(ALPACA_ENVIRONMENT_SETTING, ALPACA_ENV_PAPER)
    mode_label = "paper"
    if environment != ALPACA_ENV_PAPER:
        if not args.allow_live:
            print("autopilot is paper-only unless --allow-live is passed (Alpaca env is not Paper).")
            return 1
        mode_label = "live"

    print(f"HELIX autopilot online ({environment}). Interval {interval}s. AI={args.ai}. Ctrl+C to stop.")
    try:
        while True:
            _autopilot_cycle(args, memory, settings, mode_label)
            if args.once:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        print("autopilot stopped.")
        return 0


def _autopilot_cycle(args: argparse.Namespace, memory: SQLiteMemory, settings: AppSettings, mode_label: str) -> None:
    try:
        client = AlpacaClient.from_settings(settings)
        if not args.ignore_hours:
            clock = client.get_clock()
            if not clock.get("is_open", False):
                print("Market closed; skipping cycle.")
                return
        account = client.get_account()
        positions = client.get_positions()
    except AlpacaError as error:
        print(f"Alpaca error: {error}")
        return

    def _f(value) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    total_equity = _f(account.get("equity") or account.get("portfolio_value"))
    cash = _f(account.get("cash"))
    holdings = {p.get("symbol", ""): _f(p.get("market_value")) for p in positions if p.get("symbol")}
    holdings_pl = {
        p.get("symbol", ""): {
            "market_value": _f(p.get("market_value")),
            "unrealized_pl": _f(p.get("unrealized_pl")),
            "unrealized_plpc": _f(p.get("unrealized_plpc")),
        }
        for p in positions
        if p.get("symbol")
    }
    watchlist = memory.list_watchlist()
    if not watchlist and not holdings:
        print("Nothing to trade (empty watchlist, no positions).")
        return

    max_position_pct = args.max_pos / 100.0
    cash_buffer_pct = args.cash_buffer / 100.0

    if args.ai == "claude":
        ai_client = ClaudeClient(ClaudeConfig(model=args.model, timeout_seconds=RESEARCH_TIMEOUT_SECONDS))

        def research_fn(prompt: str) -> str:
            text = ai_client.complete(prompt, max_tokens=research_max_tokens(settings))
            usage = ai_client.last_usage or {}
            input_tokens = int(usage.get("input_tokens", 0) or 0)
            output_tokens = int(usage.get("output_tokens", 0) or 0)
            memory.record_ai_usage(
                args.model, input_tokens, output_tokens, estimate_cost(args.model, input_tokens, output_tokens)
            )
            return text

        try:
            plan = build_rebalance_plan(
                total_equity, cash, holdings, watchlist, research_fn,
                max_position_pct=max_position_pct, cash_buffer_pct=cash_buffer_pct, preset=args.preset,
                memory=memory,
                rating_max_age_days=DEFAULT_RATING_MAX_AGE_DAYS,
                on_issue=lambda message: print(f"[research] {message}"),
            )
        except ClaudeError as error:
            print(f"Claude error: {error}")
            return
    else:
        universe = merge_universe(watchlist, holdings)
        plan = build_rebalance_plan(
            total_equity, cash, holdings, watchlist,
            lambda _prompt: generate_mock_portfolio_research(universe, args.preset),
            max_position_pct=max_position_pct, cash_buffer_pct=cash_buffer_pct, preset=args.preset,
            memory=memory,
            rating_max_age_days=DEFAULT_RATING_MAX_AGE_DAYS,
        )

    if not plan.actions:
        print(f"Equity ${total_equity:,.0f}: on target, no trades.")
        return

    results = execute_rebalance(plan.actions, client, memory, mode_label=mode_label, holdings_pl=holdings_pl)
    placed = sum(1 for _action, outcome in results if not outcome.startswith("FAILED"))
    print(f"Equity ${total_equity:,.0f}: {placed}/{len(results)} order(s) placed.")
    for action, outcome in results:
        print(f"  {action.side.upper()} {action.symbol}: {outcome}")
