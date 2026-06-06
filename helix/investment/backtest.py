from __future__ import annotations

"""Backtest harness (§29) — MEASURE BEFORE YOU OPTIMIZE, part (b).

Replays real historical daily bars through HELIX's **actual** deterministic engine
(`build_rebalance_plan`: conviction-weighted sizing, the cash buffer, drift-band rebalancing) with
the LLM ratings held **FIXED** — so the simulation is deterministic and costs no Claude tokens. It
answers a question the live equity curve can't isolate: *given a pick set, does the trading machinery
(sizing + rebalancing) actually produce good risk-adjusted returns, vs equal-weight and vs the S&P?*

Honest scope:
- It scores the **mechanics**, not pick skill. Replaying *today's* ratings over *past* prices carries
  look-ahead bias (the labels may "know" recent outcomes), so absolute returns are optimistic. The
  look-ahead-NEUTRAL signal is the **A/B**: conviction-weight vs equal-weight see the same basket and
  the same prices, so their *difference* is a clean read on whether the sizing scheme helps.
- Fills are at the bar's close, fractional shares, no spread/slippage/taxes — gross, idealized.
- The forward, no-look-ahead test of pick *skill* is the §28 prediction scorecard, not this.

Pure: all price history + the cached ratings are injected; the only I/O (the Alpaca bars fetch) lives
in `gather_backtest`, the edge orchestrator shared by the CLI.
"""

import bisect
import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from math import sqrt
from typing import Any, Callable

from helix.investment.autopilot import bars_to_dated_closes, build_rebalance_plan, composite_factor_scores
from helix.investment.market_data import factor_signals, volatility_signals


@dataclass(frozen=True)
class BacktestResult:
    label: str
    start_date: str
    end_date: str
    years: float
    start_equity: float
    end_equity: float
    total_return_pct: float
    annualized_return_pct: float
    annualized_vol_pct: float
    sharpe: float
    max_drawdown_pct: float
    up_period_pct: float
    benchmark_return_pct: float | None
    alpha_pct: float | None          # total_return - benchmark_return (excess over buy-and-hold SPY)
    n_names: int
    n_rebalances: int
    n_trades: int
    equity_curve: list               # [(date, equity)] — daily, oldest first

    def __bool__(self) -> bool:
        return len(self.equity_curve) >= 2


class _PriceBook:
    """Date-keyed close lookup with forward-fill (last close on/before a date). Built once per run."""

    def __init__(self, closes_by_symbol: dict[str, list[tuple[str, float]]]) -> None:
        self._dates: dict[str, list[str]] = {}
        self._closes: dict[str, list[float]] = {}
        for symbol, series in (closes_by_symbol or {}).items():
            ordered = sorted(series, key=lambda pair: pair[0])
            self._dates[symbol] = [day for day, _close in ordered]
            self._closes[symbol] = [close for _day, close in ordered]

    def price_on(self, symbol: str, day: str) -> float | None:
        dates = self._dates.get(symbol)
        if not dates:
            return None
        index = bisect.bisect_right(dates, day) - 1  # last date <= day (forward-fill)
        if index < 0:
            return None
        close = self._closes[symbol][index]
        return close if close > 0 else None


def _cached_research_fn(ratings: dict[str, Any]) -> Callable[[str], str]:
    """A research_fn that returns the FIXED ratings as JSON, ignoring the prompt — this is how the
    LLM is 'stubbed for replay' so `build_rebalance_plan` runs its real sizing path with no network."""
    payload = json.dumps(
        [
            {
                "symbol": symbol,
                "action": str(record.get("action", "watch")),
                "confidence": str(record.get("confidence", "low")),
                "rationale": str(record.get("rationale", "")),
            }
            for symbol, record in (ratings or {}).items()
        ]
    )
    return lambda _prompt: payload


def _years_between(start_day: str, end_day: str) -> float:
    try:
        d0 = datetime.strptime(start_day[:10], "%Y-%m-%d")
        d1 = datetime.strptime(end_day[:10], "%Y-%m-%d")
        days = (d1 - d0).days
        return days / 365.25 if days > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _max_drawdown_pct(points: list[float]) -> float:
    peak = points[0] if points else 0.0
    worst = 0.0
    for value in points:
        if value > peak:
            peak = value
        if peak > 0:
            drop = (peak - value) / peak
            if drop > worst:
                worst = drop
    return round(worst * 100.0, 2)


def run_backtest(
    closes_by_symbol: dict[str, list[tuple[str, float]]],
    ratings: dict[str, Any],
    *,
    label: str = "strategy",
    start_equity: float = 100000.0,
    cash_buffer_pct: float = 0.10,
    preset: str = "Aggressive",
    rebalance_every_days: int = 7,
    benchmark: str = "SPY",
    min_trade_usd: float = 1.0,
    max_positions: int = 0,
    vol_adjust: bool = False,
    factor_overlay: bool = False,
) -> BacktestResult:
    """Replay daily bars through `build_rebalance_plan`, marking to market daily and rebalancing on
    the cadence. Returns a `BacktestResult`. PURE — no network, no Claude (ratings are stubbed)."""
    book = _PriceBook(closes_by_symbol)
    all_dates = sorted({day for series in closes_by_symbol.values() for day, _c in series})
    empty = BacktestResult(
        label, all_dates[0] if all_dates else "", all_dates[-1] if all_dates else "", 0.0,
        start_equity, start_equity, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, None,
        len(ratings or {}), 0, 0, [],
    )
    if len(all_dates) < 2:
        return empty

    research_fn = _cached_research_fn(ratings)
    watchlist = [{"symbol": symbol, "thesis": "", "max_allocation_pct": None} for symbol in ratings]
    cash = float(start_equity)
    shares: dict[str, float] = {}
    curve: list[tuple[str, float]] = []
    last_rebalance: str | None = None
    n_rebalances = n_trades = 0

    for day in all_dates:
        holdings_value: dict[str, float] = {}
        for symbol, qty in shares.items():
            if qty <= 0:
                continue
            price = book.price_on(symbol, day)
            if price:
                holdings_value[symbol] = qty * price
        equity = cash + sum(holdings_value.values())
        curve.append((day, round(equity, 2)))

        due = last_rebalance is None or (
            datetime.strptime(day, "%Y-%m-%d") - datetime.strptime(last_rebalance, "%Y-%m-%d")
        ).days >= rebalance_every_days
        if not due:
            continue
        last_rebalance = day
        n_rebalances += 1

        # Point-in-time signals (§30 momentum, §31 vol, §33 composite) from trailing closes only — so
        # ranking, vol tilt and the factor overlay carry NO look-ahead, unlike the (biased) ratings.
        # (SEC quality isn't available in replay, so the backtest composite is momentum + low-vol only.)
        fscores = vols = None
        if max_positions or vol_adjust or factor_overlay:
            trailing = {sym: [c for d, c in series if d <= day] for sym, series in closes_by_symbol.items()}
            momentum = factor_signals(trailing) if (max_positions or factor_overlay) else None
            vols = volatility_signals(trailing) if (vol_adjust or factor_overlay) else None
            if factor_overlay:
                fscores = composite_factor_scores(momentum, None, vols)  # ranks + overlay use the composite
            elif max_positions:
                fscores = momentum
        plan = build_rebalance_plan(
            equity, cash, holdings_value, watchlist, research_fn,
            max_position_pct=1.0, max_positions=max_positions, factor_scores=fscores,
            factor_overlay=factor_overlay, volatilities=vols, vol_adjust=vol_adjust,
            cash_buffer_pct=cash_buffer_pct, preset=preset,
            memory=None, rating_max_age_days=0.0, min_trade_usd=min_trade_usd,
        )
        for action in plan.actions:
            price = book.price_on(action.symbol, day)
            if not price:
                continue
            if action.side == "sell":
                qty = min(shares.get(action.symbol, 0.0), action.amount_usd / price)
                if qty <= 0:
                    continue
                shares[action.symbol] = shares.get(action.symbol, 0.0) - qty
                cash += qty * price
                n_trades += 1
            else:  # buy — clamp to cash so rounding can never drive it negative
                spend = min(action.amount_usd, cash)
                if spend < min_trade_usd:
                    continue
                shares[action.symbol] = shares.get(action.symbol, 0.0) + spend / price
                cash -= spend
                n_trades += 1

    return _assemble(curve, book, benchmark, label, start_equity, len(ratings or {}), n_rebalances, n_trades)


def _assemble(
    curve: list[tuple[str, float]], book: _PriceBook, benchmark: str, label: str,
    start_equity: float, n_names: int, n_rebalances: int, n_trades: int,
) -> BacktestResult:
    start_day, end_day = curve[0][0], curve[-1][0]
    equities = [eq for _d, eq in curve]
    start_eq, end_eq = equities[0], equities[-1]
    years = _years_between(start_day, end_day)
    total_return = (end_eq / start_eq - 1.0) * 100.0 if start_eq > 0 else 0.0

    rets = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    ppy = (len(rets) / years) if years > 0 else 252.0
    std_r = statistics.stdev(rets) if len(rets) > 1 else 0.0
    mean_r = statistics.fmean(rets) if rets else 0.0
    sharpe = (mean_r / std_r) * sqrt(ppy) if std_r > 0 else 0.0
    ann_vol = std_r * sqrt(ppy) * 100.0
    ann_return = ((end_eq / start_eq) ** (1.0 / years) - 1.0) * 100.0 if years > 0 and start_eq > 0 else total_return
    up_pct = (100.0 * sum(1 for r in rets if r > 0) / len(rets)) if rets else 0.0

    benchmark_return = alpha = None
    spy_start = book.price_on(benchmark, start_day)
    spy_end = book.price_on(benchmark, end_day)
    if spy_start and spy_end and spy_start > 0:
        benchmark_return = (spy_end / spy_start - 1.0) * 100.0
        alpha = total_return - benchmark_return

    return BacktestResult(
        label=label, start_date=start_day, end_date=end_day, years=round(years, 2),
        start_equity=round(start_eq, 2), end_equity=round(end_eq, 2),
        total_return_pct=round(total_return, 2), annualized_return_pct=round(ann_return, 2),
        annualized_vol_pct=round(ann_vol, 2), sharpe=round(sharpe, 2),
        max_drawdown_pct=_max_drawdown_pct(equities), up_period_pct=round(up_pct, 1),
        benchmark_return_pct=None if benchmark_return is None else round(benchmark_return, 2),
        alpha_pct=None if alpha is None else round(alpha, 2),
        n_names=n_names, n_rebalances=n_rebalances, n_trades=n_trades, equity_curve=curve,
    )


def render_backtest(results: list[BacktestResult], header: str = "") -> str:
    """Render one or more BacktestResults as a side-by-side comparison (§29)."""
    results = [r for r in results if r]
    lines = ["HELIX BACKTEST - deterministic strategy replayed on real history"]
    if header:
        lines.append(header)
    if not results:
        lines.append("")
        lines.append("Not enough price history to backtest (need at least two days of bars).")
        return "\n".join(lines)
    first = results[0]
    lines.append(f"Window: {first.start_date} -> {first.end_date} ({first.years:.2f}y)  |  "
                 f"start ${first.start_equity:,.0f}  |  {first.n_names} names")
    lines.append("")
    lines.append(f"  {'STRATEGY':<18}{'RETURN':>9}{'ANN':>8}{'VOL':>8}{'SHARPE':>8}{'MAXDD':>8}{'vs SPY':>9}")
    for r in results:
        spy = "n/a" if r.benchmark_return_pct is None else f"{r.benchmark_return_pct:+.1f}%"
        alpha = "" if r.alpha_pct is None else f"{r.alpha_pct:+.1f}%"
        lines.append(
            f"  {r.label:<18}{r.total_return_pct:>+8.1f}%{r.annualized_return_pct:>+7.1f}%"
            f"{r.annualized_vol_pct:>7.1f}%{r.sharpe:>8.2f}{r.max_drawdown_pct:>7.1f}%{alpha:>9}"
        )
    bench = next((r.benchmark_return_pct for r in results if r.benchmark_return_pct is not None), None)
    if bench is not None:
        lines.append(f"  {'S&P 500 (hold)':<18}{bench:>+8.1f}%")
    lines.append("")
    by = {r.label: r for r in results}
    all_conv = by.get("conviction (all)")
    # (1) Concentration: does capping to top-N help risk-adjusted return vs holding all? (§30)
    capped = [r for r in results if r.label.startswith("conviction (top")]
    if capped and all_conv:
        best_capped = max(capped, key=lambda r: r.sharpe)
        d_shp = best_capped.sharpe - all_conv.sharpe
        d_ret = best_capped.total_return_pct - all_conv.total_return_pct
        verb = "lifts" if d_shp > 0 else "lowers"
        lines.append(f"  -> concentration: {best_capped.label} {verb} risk-adjusted return vs holding "
                     f"all ({d_shp:+.2f} Sharpe, {d_ret:+.1f} pts return). Concentrating "
                     f"{'helped' if d_shp > 0 else 'did NOT help'} on this window.")
    # (2) Volatility-adjusted sizing vs flat conviction — look-ahead-neutral (same basket/prices). (§31)
    voladj = by.get("conviction + vol-adj")
    if voladj and all_conv:
        d_shp = voladj.sharpe - all_conv.sharpe
        d_ret = voladj.total_return_pct - all_conv.total_return_pct
        verb = "lifts" if d_shp > 0 else "lowers"
        lines.append(f"  -> vol-adjusted sizing {verb} Sharpe by {abs(d_shp):.2f} ({d_ret:+.1f} pts return) "
                     f"vs flat conviction, at {voladj.annualized_vol_pct:.1f}% vol vs {all_conv.annualized_vol_pct:.1f}%.")
    # (3) Factor overlay vs plain conviction — does tempering weak-factor buys help? (§33)
    overlay = by.get("conviction + factor-overlay")
    if overlay and all_conv:
        d_shp = overlay.sharpe - all_conv.sharpe
        d_ret = overlay.total_return_pct - all_conv.total_return_pct
        verb = "lifts" if d_shp > 0 else "lowers"
        lines.append(f"  -> factor overlay {verb} Sharpe by {abs(d_shp):.2f} ({d_ret:+.1f} pts return) vs plain "
                     f"conviction (tempering buys the numbers contradict; momentum+low-vol only in replay).")
    # (4) Sizing scheme: conviction-weighting vs equal-weight (look-ahead-neutral — same basket/prices).
    equal = by.get("equal-weight")
    if all_conv and equal:
        d_ret = all_conv.total_return_pct - equal.total_return_pct
        d_shp = all_conv.sharpe - equal.sharpe
        verb = "beats" if d_ret > 0 else "trails"
        lines.append(f"  -> sizing: conviction-weighting {verb} equal-weight by {abs(d_ret):.1f} pts "
                     f"({d_shp:+.2f} Sharpe). This A/B is look-ahead-neutral (same basket, same prices).")
    lines.append("")
    lines.append("Idealized & GROSS (close fills, no spread/slippage/taxes). Replaying current ratings")
    lines.append("over past prices is look-ahead-biased on absolute return - trust the A/B, not the level.")
    lines.append("Paper, simulated. Not financial advice.")
    return "\n".join(lines)


def gather_backtest(
    memory: Any, client: Any, *, days: int = 180, rebalance_every_days: int = 7,
    start_equity: float = 100000.0, cash_buffer_pct: float = 0.10,
    max_positions_sweep: tuple[int, ...] = (0,),
) -> tuple[str, list[BacktestResult]]:
    """End-to-end backtest (§29/§30): take the current core BUY ratings, fetch their daily bars + SPY,
    and replay the deterministic engine across a **concentration sweep** — conviction-weighted at each
    top-N in `max_positions_sweep` (0 = uncapped) plus an equal-weight baseline. The edge orchestrator
    shared by the CLI; only the bars fetch touches the network. Returns (report, results)."""
    rows = memory.list_stock_rationale()
    ratings = {
        str(r["symbol"]).upper(): {"action": r["action"], "confidence": r.get("confidence", "low"),
                                   "rationale": r.get("rationale", "")}
        for r in rows
        if r.get("action") == "buy" and r.get("symbol")
    }
    if not ratings:
        return ("HELIX BACKTEST\n\nNo buy-rated names to backtest yet - run a cycle so HELIX rates the "
                "universe first.", [])
    symbols = sorted(ratings) + ["SPY"]
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")  # pad for warm-up bars
    try:
        bars = client.get_bars_multi(symbols, timeframe="1Day", start=start)
    except Exception as exc:  # noqa: BLE001 — best-effort; report cleanly
        return (f"HELIX BACKTEST\n\nCould not fetch price history: {exc}", [])
    closes_by_symbol = {}
    for symbol, sym_bars in (bars or {}).items():
        dated = bars_to_dated_closes(sym_bars)
        if dated:
            closes_by_symbol[str(symbol).upper()] = dated

    common = dict(
        closes_by_symbol=closes_by_symbol, ratings=ratings, start_equity=start_equity,
        cash_buffer_pct=cash_buffer_pct, rebalance_every_days=rebalance_every_days,
    )
    sweep = list(dict.fromkeys(max_positions_sweep)) or [0]
    results = [run_backtest(label="conviction (all)", preset="Aggressive", max_positions=0, **common)]
    # Volatility-adjusted sizing A/B (§31): same basket/conviction, with the bounded inverse-vol tilt.
    results.append(run_backtest(label="conviction + vol-adj", preset="Aggressive", max_positions=0,
                                vol_adjust=True, **common))
    # Factor-overlay A/B (§33): temper buys whose composite factor (momentum + low-vol here) is weak.
    results.append(run_backtest(label="conviction + factor-overlay", preset="Aggressive", max_positions=0,
                                factor_overlay=True, **common))
    for n in sweep:  # concentration sweep (§30); uncapped is already the "all" leg above
        if n:
            results.append(run_backtest(label=f"conviction (top {n})", preset="Aggressive",
                                        max_positions=n, **common))
    results.append(run_backtest(label="equal-weight", preset="Balanced", max_positions=0, **common))
    header = (f"{len(ratings)} buy-rated names, rebalanced every {rebalance_every_days}d, "
              f"{cash_buffer_pct * 100:.0f}% cash buffer.")
    return render_backtest(results, header=header), results
