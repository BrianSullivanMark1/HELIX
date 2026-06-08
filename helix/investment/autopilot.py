from __future__ import annotations

import statistics
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from helix.ai.research import (
    build_adversarial_prompt,
    build_daytrade_research_prompt,
    build_portfolio_research_prompt,
    build_roster_discovery_prompt,
    build_roster_score_prompt,
    build_special_research_prompt,
    parse_adversarial_json,
    parse_research_json,
    parse_roster_review_json,
    parse_special_research_json,
)
from helix.brokers.alpaca import AlpacaError
from helix.investment.market_data import factor_signals, liquidity_metrics, volatility_signals

# Relative weighting used to size buys when the posture is "Aggressive". Deliberately steep (8:3:1,
# not 3:2:1) so capital concentrates in the highest-conviction names instead of spreading evenly —
# this is what keeps a wide universe (the HELIX 500) from becoming a 500-way equal-weight index.
CONFIDENCE_WEIGHT = {"high": 8.0, "medium": 3.0, "low": 1.0}
PRESETS = ("Balanced", "Aggressive")

# Ratings are reused for this many days before the loop re-rates with the model. This decouples
# re-rating (slow/expensive) from trading (frequent/cheap): re-rate ~weekly, rebalance often off
# the cache. See §14. 0 disables caching (always re-rate).
DEFAULT_RATING_MAX_AGE_DAYS = 7.0

# The HELIX 100 roster of record = the GUI "Stocks To Trade" basket, stored in settings.
ROSTER_SETTING = "invest_tickers"
# Self-curation cadence: how often the roster auto-rotates, and when it last did.
LAST_ROSTER_REVIEW_SETTING = "invest_last_roster_review"
DEFAULT_ROSTER_REVIEW_DAYS = 90  # quarterly — low turnover (§20)

# Special Stocks (§21): a high-risk satellite sleeve, separate from the HELIX 100 core.
SPECIAL_SETTING = "invest_special_stocks"
LAST_SPECIAL_RESEARCH_SETTING = "invest_last_special_research"
DEFAULT_SPECIAL_ALLOCATION_PCT = 0.20   # share of the account in the speculative sleeve (user-set)
DEFAULT_SPECIAL_MAX_POSITION_PCT = 0.05  # hard cap per speculative name (a moonshot can't sink you)
DEFAULT_SPECIAL_RESEARCH_DAYS = 1        # re-scout nightly (events move fast), during market-closed idle time
DEFAULT_SPECIAL_MAX_NAMES = 12           # buy-and-hold sleeve size cap (accumulate the best, hold them)

# Day-trade sleeve (§27): a third sleeve beside Core and Special — short-term momentum, FAST turnover,
# exited on a take-profit / stop-loss / rotate-off (NOT buy-and-hold like Special). High risk, small.
DAYTRADE_SETTING = "invest_daytrade_stocks"
LAST_DAYTRADE_RESEARCH_SETTING = "invest_last_daytrade_research"
DEFAULT_DAYTRADE_ALLOCATION_PCT = 0.10   # share of the account in the day-trade sleeve (user-set)
DEFAULT_DAYTRADE_MAX_POSITION_PCT = 0.05 # hard cap per day-trade name
DEFAULT_DAYTRADE_RESEARCH_DAYS = 1       # scout fresh momentum every day (setups move fast)
DEFAULT_DAYTRADE_MAX_NAMES = 8           # how many momentum names to hold at once
DAYTRADE_TAKE_PROFIT_PCT = 0.15          # exit a winner once it is up ~15%
DAYTRADE_STOP_LOSS_PCT = -0.08           # cut a loser once it is down ~8%

# Research token budget (§10/§25). The max_tokens ceiling on each Claude research call. Rating
# ~100 names with live-data rationales runs ~3k-4.5k output tokens — right at the old 4096 cap,
# which truncated the JSON and silently dropped every core rating (§10). 8192 clears that; the
# higher levels give headroom for a larger universe or longer rationales without truncating. It is
# a *ceiling*, not a target: the model only spends what it actually writes, so raising it mainly
# buys safety against truncation rather than forcing more tokens. Exposed as Settings -> "Research
# effort" and honored by every research call (GUI + CLI) via research_max_tokens().
INVEST_RESEARCH_TOKENS_SETTING = "invest_research_tokens"
# Default = High (16K). max_tokens is a *ceiling*, not a meter — you pay for tokens actually
# generated, so for the ~100-name core (one-line-per-name ratings ≈ 3k-4.5k output) 16K bills the
# same as 8K; it just removes truncation risk. Brian is fine spending good tokens on research.
DEFAULT_RESEARCH_TOKENS = 16384
# Named effort levels for the Settings picker: (label, max_tokens). Capped at 32000 so the value
# stays within every research-capable model's output limit (Opus 4.x = 32k; Sonnet allows more).
RESEARCH_EFFORT_LEVELS: tuple[tuple[str, int], ...] = (
    ("Standard - 8K tokens (cheapest)", 8192),
    ("High - 16K tokens (default, longer rationales)", 16384),
    ("Maximum - 32K tokens (biggest universe, deepest)", 32000),
)


# Research calls are big and slow now (≈100 names + a live-data digest + up to a 16K reply can take
# 1-3 min), so they need a far longer HTTP timeout than the 90s ClaudeConfig default — at 90s a real
# core re-rate times out, and the off-hours branch would swallow it silently. Used at every research
# client construction (GUI + CLI).
RESEARCH_TIMEOUT_SECONDS = 300

# A single Claude call reliably rates only so many names before the JSON grows long/slow (≈100 names
# ≈ 6.5k output tokens and a 1-3 min call). For a large universe (the "HELIX 500") we rate in batches
# of this size and merge, so no single call truncates or times out. See §10/§16.
RATING_CHUNK_SIZE = 50

# Adversarial pick-checking (§34): how many of the top buy candidates to stress-test (bull/bear/judge)
# per re-rate. One Claude call each, so this bounds the extra cost; the highest-conviction buys (which
# get the most capital) are checked first. Opt-in.
ADVERSARIAL_MAX_CHECKS = 12

# Risk-control defaults (§35) — the thresholds behind the five protective controls. Conservative
# catastrophe/prudence guards, not hair-triggers; tunable.
DEFAULT_SECTOR_CAP_PCT = 0.25             # no one sector above 25% of the book
DEFAULT_DRAWDOWN_BRAKE_PCT = 0.15         # raise cash once the account is down 15% from its peak
DEFAULT_DEFENSIVE_CASH_BUFFER_PCT = 0.40  # cash to hold while in drawdown or a risk-off regime
DEFAULT_CORE_STOP_LOSS_PCT = 0.25         # exit a core holding down 25%+ (deep single-name brake)
DEFAULT_MIN_POSITIONS = 20                # diversification floor: never concentrate below 20 names


def research_max_tokens(settings: Any) -> int:
    """The user-chosen max_tokens budget for a Claude research call (Settings -> Research effort).
    Falls back to DEFAULT_RESEARCH_TOKENS for a missing/non-numeric value, and clamps the result to
    [4096, 32000] so a hand-edited setting can never send 0 or an over-limit cap the API rejects."""
    raw = settings.get(INVEST_RESEARCH_TOKENS_SETTING, DEFAULT_RESEARCH_TOKENS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_RESEARCH_TOKENS
    return max(4096, min(value, 32000))


# Real-market screener (§36): the exchanges HELIX treats as "the market" (OTC excluded).
MAJOR_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"})


# Quality/liquidity screen thresholds (§37), per sleeve. Core wants big/liquid/quality (S&P-caliber);
# Special (moonshots) and Day-trade (momentum) need liquidity to trade but NOT the size/quality gates
# that would defeat their purpose. All baked (no UI knobs) per the "think under the hood" preference.
#
# IMPORTANT — these dollar-volume floors are on the FREE IEX feed, which reports only IEX-exchange
# volume (~2-5% of consolidated). So ~$1M/day IEX ≈ ~$25-50M/day true volume — a real large/mid-cap.
# Calibrated so genuine S&P names (e.g. Rollins, Deckers) pass while penny/near-zero-volume junk fails;
# not an exact consolidated-volume figure (would need the paid SIP feed).
SCREEN_PROFILES: dict[str, dict[str, float]] = {
    # min_price, min_dollar_volume (avg daily $, IEX feed), max_debt_equity, min_net_margin, check_quality
    "core":     {"min_price": 5.0, "min_dollar_volume": 1_000_000.0, "max_debt_equity": 6.0, "min_net_margin": -0.10, "check_quality": 1.0},
    "special":  {"min_price": 2.0, "min_dollar_volume": 200_000.0,   "max_debt_equity": 0.0, "min_net_margin": 0.0,   "check_quality": 0.0},
    "daytrade": {"min_price": 3.0, "min_dollar_volume": 1_000_000.0, "max_debt_equity": 0.0, "min_net_margin": 0.0,   "check_quality": 0.0},
}


def screen_candidates(
    symbols: list,
    bars_by_symbol: dict,
    fundamentals_by_symbol: dict | None = None,
    *,
    min_price: float = 5.0,
    min_dollar_volume: float = 20_000_000.0,
    max_debt_equity: float = 6.0,
    min_net_margin: float = -0.10,
    check_quality: bool = True,
) -> set[str]:
    """Quality/liquidity screen (§37): the subset of `symbols` worth holding. **Liquidity is required**
    — last price ≥ `min_price` and avg daily dollar volume ≥ `min_dollar_volume` (from `bars_by_symbol`);
    a name with no bars fails (can't confirm it's tradeable/liquid). **Quality is lenient** and only
    applied when `check_quality` (core sleeve): drop a name only if cached SEC fundamentals show it's
    deeply unprofitable (net margin < `min_net_margin`) or over-levered (debt/equity > `max_debt_equity`);
    missing fundamentals pass. Pure — bars + fundamentals injected."""
    fundamentals_by_symbol = fundamentals_by_symbol or {}
    out: set[str] = set()
    for symbol in symbols or []:
        symbol = str(symbol).strip().upper()
        price, dollar_volume = liquidity_metrics(bars_by_symbol.get(symbol))
        if price < min_price or dollar_volume < min_dollar_volume:
            continue  # illiquid / penny / unverifiable -> reject (liquidity is required)
        if check_quality:
            metrics = fundamentals_by_symbol.get(symbol) or {}
            net_margin = metrics.get("net_margin")
            debt_equity = metrics.get("debt_to_equity")
            if net_margin is not None and net_margin < min_net_margin:
                continue
            if debt_equity is not None and debt_equity > max_debt_equity:
                continue
        out.add(symbol)
    return out


def tradable_symbols(assets: list, *, require_fractionable: bool = True) -> set[str]:
    """The set of real, tradeable tickers from Alpaca's asset list (§36): active, tradable, on a major
    exchange (no OTC), and — by default — **fractionable**, because HELIX places NOTIONAL dollar orders
    which only execute on fractionable assets. This is the live market universe that discovered names
    (core rotation candidates, Special + Day-trade picks) are validated against, so no hallucinated,
    delisted, or un-buyable ticker ever enters a sleeve. Pure."""
    out: set[str] = set()
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if not asset.get("tradable") or str(asset.get("status", "")).lower() != "active":
            continue
        if asset.get("exchange") not in MAJOR_EXCHANGES:
            continue
        if require_fractionable and not asset.get("fractionable"):
            continue
        symbol = str(asset.get("symbol", "")).strip().upper()
        if symbol:
            out.add(symbol)
    return out


def normalize_roster(raw: Any) -> list[str]:
    """Roster (list or comma/semicolon string) -> ordered, de-duped, uppercased symbols."""
    tokens = raw if isinstance(raw, (list, tuple)) else str(raw or "").replace(";", ",").split(",")
    out: list[str] = []
    for token in tokens:
        symbol = str(token).strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


@dataclass(frozen=True)
class TradeProposal:
    symbol: str
    action: str  # buy | watch | skip
    confidence: str  # low | medium | high
    rationale: str
    size_usd: float = 0.0


@dataclass(frozen=True)
class InvestPlan:
    deployable_cash: float
    preset: str
    proposals: list
    raw_response: str = ""

    @property
    def buys(self) -> list:
        return [p for p in self.proposals if p.action == "buy" and p.size_usd > 0]

    @property
    def allocated(self) -> float:
        return round(sum(p.size_usd for p in self.buys), 2)

    @property
    def leftover_cash(self) -> float:
        return round(max(0.0, self.deployable_cash - self.allocated), 2)


def build_plan(
    deployable_cash: float,
    watchlist: list[dict[str, Any]],
    research_fn: Callable[[str], str],
    preset: str = "Balanced",
) -> InvestPlan:
    """Ask the AI to rate each watchlist ticker, then size the buys deterministically."""
    deployable_cash = max(0.0, float(deployable_cash))
    prompt = build_portfolio_research_prompt(deployable_cash, watchlist, preset)
    raw = research_fn(prompt) or ""
    records = {record["symbol"]: record for record in parse_research_json(raw)}
    caps = _allocation_caps(watchlist)

    proposals: list[TradeProposal] = []
    for item in watchlist:
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        record = records.get(
            symbol,
            {"action": "watch", "confidence": "low", "rationale": "No AI signal returned."},
        )
        proposals.append(
            TradeProposal(
                symbol=symbol,
                action=record["action"],
                confidence=record["confidence"],
                rationale=record["rationale"],
            )
        )

    proposals = _apply_sizing(proposals, deployable_cash, caps, preset)
    return InvestPlan(
        deployable_cash=deployable_cash,
        preset=preset,
        proposals=proposals,
        raw_response=raw,
    )


def execute_plan(
    proposals: list[TradeProposal],
    alpaca_client: Any,
    memory: Any,
    mode_label: str = "paper",
) -> list[tuple[TradeProposal, str]]:
    """Submit each buy as a market/day notional order and log it. One failure never aborts the batch."""
    entry_type = "live_trade" if mode_label == "live" else "paper_trade"
    results: list[tuple[TradeProposal, str]] = []
    for proposal in proposals:
        if proposal.action != "buy" or proposal.size_usd <= 0:
            continue
        try:
            order = alpaca_client.submit_order(
                symbol=proposal.symbol,
                side="buy",
                notional=proposal.size_usd,
            )
            status = order.get("status", "submitted")
            order_id = order.get("id", "unknown")
            outcome = f"{mode_label} buy ${proposal.size_usd:,.2f} -> {status}"
            memory.add_journal_entry(
                entry_type=entry_type,
                title=f"{mode_label.title()} buy {proposal.symbol}",
                body="\n".join(
                    [
                        f"Order ID: {order_id}",
                        f"Status: {status}",
                        f"Symbol: {proposal.symbol}",
                        f"Notional: {proposal.size_usd:.2f}",
                        f"Confidence: {proposal.confidence}",
                        f"Rationale: {proposal.rationale}",
                        "Order type: market / day",
                    ]
                ),
            )
        except AlpacaError as error:
            outcome = f"FAILED: {error}"
        results.append((proposal, outcome))
    return results


def render_plan(plan: InvestPlan) -> str:
    lines = [
        "HELIX Invest Plan (proposal)",
        f"Posture: {plan.preset}",
        f"Cash to deploy: ${plan.deployable_cash:,.2f}",
        "",
    ]
    if not plan.proposals:
        lines.append("No watchlist tickers to evaluate.")
        return "\n".join(lines)

    lines.append(f"{'SYMBOL':<8} {'ACTION':<6} {'CONF':<7} {'SIZE':>12}  RATIONALE")
    for proposal in plan.proposals:
        size = f"${proposal.size_usd:,.2f}" if proposal.action == "buy" else "-"
        lines.append(
            f"{proposal.symbol:<8} {proposal.action:<6} {proposal.confidence:<7} {size:>12}  {proposal.rationale}"
        )
    lines.append("")
    lines.append(
        f"Buys: {len(plan.buys)} | Allocated: ${plan.allocated:,.2f} | Leftover cash: ${plan.leftover_cash:,.2f}"
    )
    lines.append("Note: proposal only. Practice/paper unless you switch to real money. Not financial advice.")
    return "\n".join(lines)


def _allocation_caps(watchlist: list[dict[str, Any]]) -> dict[str, float]:
    caps: dict[str, float] = {}
    for item in watchlist:
        symbol = str(item.get("symbol", "")).strip().upper()
        pct = item.get("max_allocation_pct")
        if not symbol or pct is None:
            continue
        try:
            caps[symbol] = float(pct) / 100.0
        except (TypeError, ValueError):
            continue
    return caps


def _apply_sizing(
    proposals: list[TradeProposal],
    deployable_cash: float,
    caps: dict[str, float],
    preset: str,
) -> list[TradeProposal]:
    buys = [p for p in proposals if p.action == "buy"]
    if not buys or deployable_cash <= 0:
        return proposals

    if preset == "Aggressive":
        weights = {p.symbol: CONFIDENCE_WEIGHT.get(p.confidence, 1.0) for p in buys}
    else:
        weights = {p.symbol: 1.0 for p in buys}
    total_weight = sum(weights.values()) or 1.0

    sized: list[TradeProposal] = []
    for proposal in proposals:
        if proposal.action != "buy":
            sized.append(proposal)
            continue
        base = deployable_cash * (weights[proposal.symbol] / total_weight)
        cap = caps.get(proposal.symbol)
        size = min(base, deployable_cash * cap) if cap is not None else base
        sized.append(
            TradeProposal(
                symbol=proposal.symbol,
                action=proposal.action,
                confidence=proposal.confidence,
                rationale=proposal.rationale,
                size_usd=round(max(0.0, size), 2),
            )
        )
    return sized


# --------------------------------------------------------------------------- #
# Active rebalance engine (buy + sell + trim) — the v2 strategy.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RebalanceAction:
    symbol: str
    side: str  # buy | sell
    amount_usd: float
    reason: str
    current_usd: float = 0.0
    target_usd: float = 0.0
    confidence: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class RebalancePlan:
    total_equity: float
    cash: float
    investable: float
    cash_buffer_pct: float
    max_position_pct: float
    preset: str
    targets: dict
    actions: list
    ratings: dict
    raw_response: str = ""

    @property
    def sells(self) -> list:
        return [a for a in self.actions if a.side == "sell"]

    @property
    def buys(self) -> list:
        return [a for a in self.actions if a.side == "buy"]

    @property
    def total_sell_usd(self) -> float:
        return round(sum(a.amount_usd for a in self.sells), 2)

    @property
    def total_buy_usd(self) -> float:
        return round(sum(a.amount_usd for a in self.buys), 2)


def merge_universe(
    watchlist: list[dict[str, Any]],
    holdings: dict[str, float] | None,
) -> list[dict[str, Any]]:
    """Watchlist plus any currently-held names not on it, so exits can be rated too."""
    merged = list(watchlist or [])
    symbols = {str(item.get("symbol", "")).strip().upper() for item in merged}
    for held in (holdings or {}):
        held_symbol = str(held).strip().upper()
        if held_symbol and held_symbol not in symbols:
            merged.append({"symbol": held_symbol, "thesis": "currently held", "max_allocation_pct": None})
    return merged


def _research_issue(label: str, raw: str) -> str:
    """Format a human-readable diagnostic for a research call that produced no usable picks — the
    silent-failure case from §10 (truncated/garbled JSON parses to []). Includes the raw length and
    a head+tail snippet so the cause (truncation vs. a refusal vs. an empty reply) is obvious."""
    raw = (raw or "").strip()
    if not raw:
        return (f"{label}: the model returned no text, so research produced nothing "
                "(check the Claude API key and network).")
    size = len(raw)
    snippet = raw if size <= 500 else f"{raw[:250]} ... {raw[-250:]}"
    return (f"{label}: the response did not parse into any usable picks - most likely the JSON was "
            f"truncated past the token budget ({size} chars returned). Raise Settings > Research "
            f"effort, or reduce the universe size. Raw response: {snippet}")


def _position_plpc(holdings_pl: dict | None, symbol: str) -> float | None:
    """A held position's unrealized P&L as a fraction (e.g. 0.12 = +12%), or None if unknown. Used by
    the day-trade sleeve's take-profit / stop-loss exits (§27)."""
    info = (holdings_pl or {}).get(symbol)
    if not isinstance(info, dict):
        return None
    try:
        return float(info.get("unrealized_plpc"))
    except (TypeError, ValueError):
        return None


def rate_universe(
    equity: float,
    watchlist: list[dict[str, Any]],
    research_fn: Callable[[str], str],
    preset: str,
    *,
    performance: str = "",
    market_context_fn: Callable[[list], str] | None = None,
    on_issue: Callable[[str], None] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    chunk_size: int = RATING_CHUNK_SIZE,
) -> dict:
    """Rate every name in `watchlist` buy/watch/skip, **in batches** of `chunk_size`, and merge — so a
    large universe (the HELIX 500) never truncates or times out in a single call (§10/§16). Each batch
    fetches its own per-chunk live market context (`market_context_fn(symbols)`), so prompts stay small.
    Returns {SYMBOL: rating record}. A batch that parses to nothing reports via `on_issue` but does not
    abort the others. With one batch this is just a single rating call (the small-universe path)."""
    ratings: dict = {}
    items = [item for item in (watchlist or []) if item.get("symbol")]
    chunks = [items[i:i + max(1, chunk_size)] for i in range(0, len(items), max(1, chunk_size))]
    for index, chunk in enumerate(chunks):
        if progress_fn is not None and len(chunks) > 1:
            first = index * chunk_size + 1
            progress_fn(f"Rating stocks {first}-{first + len(chunk) - 1} of {len(items)}...")
        symbols = [str(item.get("symbol", "")).strip().upper() for item in chunk if item.get("symbol")]
        try:  # a failed batch (e.g. a transient API error after retries) must not lose the others
            context = market_context_fn(symbols) if market_context_fn is not None else ""
            prompt = build_portfolio_research_prompt(equity, chunk, preset, performance=performance, market_context=context)
            raw = research_fn(prompt) or ""
        except Exception as exc:  # noqa: BLE001 — keep the successful batches, surface the failure
            if on_issue is not None:
                on_issue(f"Core ratings batch {index + 1}/{len(chunks)} failed (kept the rest): {exc}")
            continue
        records = parse_research_json(raw)
        if not records and on_issue is not None:
            on_issue(_research_issue(f"Core ratings (batch {index + 1}/{len(chunks)}, {len(chunk)} names)", raw))
        for record in records:
            ratings[record["symbol"]] = record
    return ratings


def apply_adversarial_review(
    ratings: dict,
    research_fn: Callable[[str], str],
    *,
    market_context_fn: Callable[[list], str] | None = None,
    performance: str = "",
    max_checks: int = ADVERSARIAL_MAX_CHECKS,
    progress_fn: Callable[[str], None] | None = None,
) -> dict:
    """Adversarial 'bull vs bear vs judge' check (§34) on the top buy candidates: re-examine each with
    a forced bull case + bear case + an impartial, demanding verdict, and **override the rating with
    the judge's call** — so a buy that doesn't survive its own bear case is downgraded to watch/skip.

    Bounded to `max_checks` of the highest-conviction buys (they get the most capital), one Claude call
    each. Returns a NEW ratings dict (originals untouched). PURE w.r.t. I/O — `research_fn` and the
    per-name `market_context_fn` are injected, so it's stubbable in tests.
    """
    order = {"high": 0, "medium": 1, "low": 2}
    candidates = sorted(
        [(symbol, rec) for symbol, rec in (ratings or {}).items() if rec.get("action") == "buy"],
        key=lambda pair: (order.get(pair[1].get("confidence", "low"), 3), pair[0]),
    )[: max(0, max_checks)]
    if not candidates:
        return ratings
    out = dict(ratings)
    for index, (symbol, record) in enumerate(candidates):
        if progress_fn is not None:
            progress_fn(f"Stress-testing buy {index + 1}/{len(candidates)}: {symbol}...")
        context = market_context_fn([symbol]) if market_context_fn is not None else ""
        raw = research_fn(build_adversarial_prompt(symbol, market_context=context, performance=performance)) or ""
        verdict = parse_adversarial_json(raw)
        if not verdict:
            continue  # unparseable — keep the original buy rather than dropping it
        note = f" [bull/bear check -> {verdict['verdict']}: {verdict.get('rationale', '')}]".rstrip()
        out[symbol] = {
            "action": verdict["verdict"],
            "confidence": verdict.get("confidence", record.get("confidence", "low")),
            "rationale": (verdict.get("rationale") or record.get("rationale", "")) + note,
        }
    return out


# --------------------------------------------------------------------------- #
# Factor backbone + LLM overlay (§33) — blend a DETERMINISTIC composite factor
# (momentum + quality + low-vol) with the LLM's buy/watch/skip, using the model
# as a qualitative overlay/check on the numbers rather than the sole decider.
# Pure: the per-factor inputs are computed at the edge and injected.
# --------------------------------------------------------------------------- #

# Composite weights across the factors HELIX can compute today (renormalized over whichever are
# present for a name): momentum (§30 price trend), quality (§32 SEC fundamentals), low-vol (§31).
FACTOR_WEIGHTS: dict[str, float] = {"momentum": 0.4, "quality": 0.4, "low_vol": 0.2}
# Overlay thresholds on the 0-1 composite: a buy in the weak tail is tempered to a watch; a buy in
# the strong head has its confidence bumped. Conservative — factors check/confirm buys, never invent them.
FACTOR_VETO_BELOW = 0.20
FACTOR_BOOST_ABOVE = 0.80


def _percentile_ranks(values: dict[str, float], *, invert: bool = False) -> dict[str, float]:
    """{symbol: value} -> {symbol: percentile in [0,1]}, 1 = best. `invert` ranks LOW values best
    (for volatility). Robust to outliers (rank-based, not level-based). Ties resolve by order."""
    items = [(symbol, value) for symbol, value in (values or {}).items() if value is not None]
    if not items:
        return {}
    ordered = sorted(items, key=lambda kv: kv[1], reverse=not invert)  # best first
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    return {symbol: round(1.0 - index / (n - 1), 4) for index, (symbol, _v) in enumerate(ordered)}


def composite_factor_scores(
    momentum: dict[str, float] | None = None,
    quality: dict[str, float] | None = None,
    volatility: dict[str, float] | None = None,
    *,
    weights: dict[str, float] = FACTOR_WEIGHTS,
) -> dict[str, float]:
    """Blend momentum + quality + low-vol into a single 0-1 composite per symbol (§33), via
    percentile ranks so the factors are comparable and outlier-robust. Each name is scored on
    whichever factors it has, with the weights renormalized over those present. PURE."""
    ranked = {
        "momentum": _percentile_ranks(momentum or {}),
        "quality": _percentile_ranks(quality or {}),
        "low_vol": _percentile_ranks(volatility or {}, invert=True),  # lower vol ranks better
    }
    symbols: set[str] = set()
    for table in ranked.values():
        symbols |= set(table)
    out: dict[str, float] = {}
    for symbol in symbols:
        num = den = 0.0
        for factor, table in ranked.items():
            if symbol in table:
                weight = weights.get(factor, 0.0)
                num += weight * table[symbol]
                den += weight
        if den > 0:
            out[symbol] = round(num / den, 4)
    return out


def apply_factor_overlay(
    ratings: dict,
    composite: dict[str, float],
    *,
    veto_below: float = FACTOR_VETO_BELOW,
    boost_above: float = FACTOR_BOOST_ABOVE,
) -> dict:
    """LLM-as-overlay blend (§33): temper the model's buy/watch/skip with the deterministic composite
    factor. A 'buy' whose composite sits in the weak tail (< veto_below) is **downgraded to 'watch'**
    — the numbers contradict the story; a strong-factor 'buy' (>= boost_above) gets a **confidence
    bump**. Conservative and PURE: it checks/confirms the model's buys, never invents new ones, and
    returns a NEW ratings dict (originals untouched). No-op if there are no composite scores."""
    if not composite:
        return ratings
    bump = {"low": "medium", "medium": "high", "high": "high"}
    out: dict = {}
    for symbol, record in (ratings or {}).items():
        action = record.get("action")
        confidence = record.get("confidence", "low")
        score = composite.get(symbol)
        new = dict(record)
        if action == "buy" and score is not None:
            if score < veto_below:
                new["action"] = "watch"
                new["rationale"] = (record.get("rationale", "") + f" [factor overlay: numbers weak ({score:.2f}), downgraded]").strip()
            elif score >= boost_above:
                new["confidence"] = bump.get(confidence, confidence)
                new["rationale"] = (record.get("rationale", "") + f" [factor overlay: numbers strong ({score:.2f})]").strip()
        out[symbol] = new
    return out


# --------------------------------------------------------------------------- #
# Risk controls (§35) — protective rules that keep one sector, one crash, or one
# blown-up name from sinking the account. Bundled into RiskControls so the engine
# signature stays readable; all default to off/neutral. Pure (data injected).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RiskControls:
    sectors: dict | None = None              # {SYMBOL: sector} for the sector cap (unmapped = exempt)
    sector_cap_pct: float = 0.0              # max share of the book in any one sector (0 = off)
    equity_peak: float = 0.0                 # high-water equity, for the drawdown brake
    drawdown_brake_pct: float = 0.0          # raise cash once down this far from the peak (0 = off)
    defensive_cash_buffer_pct: float = 0.40  # cash buffer to hold while in drawdown or risk-off
    risk_off: bool = False                   # market regime is risk-off (SPY below its long trend)
    core_stop_loss_pct: float = 0.0          # exit a core holding down at least this much (0 = off)
    min_positions: int = 0                   # diversification floor: never concentrate below N names


def compute_drawdown(equity: float, peak: float) -> float:
    """Current drawdown as a fraction of the peak (0 if at/above peak or no peak). Pure."""
    if not peak or peak <= 0:
        return 0.0
    return max(0.0, (peak - equity) / peak)


def apply_sector_cap(targets: dict, sectors: dict | None, cap_usd: float) -> dict:
    """Scale down any sector whose summed target $ exceeds `cap_usd` (§35), pro-rata across its names.
    Unmapped names are exempt (never grouped). Freed budget becomes cash — conservative, not
    redistributed (redistribution could re-breach another cap). Pure; returns a NEW targets dict."""
    if not sectors or cap_usd <= 0:
        return targets
    by_sector: dict[str, list[str]] = {}
    for symbol, target in targets.items():
        sector = sectors.get(symbol)
        if sector and target > 0:
            by_sector.setdefault(sector, []).append(symbol)
    out = dict(targets)
    for names in by_sector.values():
        total = sum(out[s] for s in names)
        if total > cap_usd and total > 0:
            scale = cap_usd / total
            for s in names:
                out[s] = round(out[s] * scale, 2)
    return out


def screen_market_candidates(
    bars_by_symbol: dict[str, Any],
    *,
    exclude: set | None = None,
    top_n: int = 30,
    min_price: float = 5.0,
    min_dollar_volume: float = 5_000_000.0,
    quality: dict[str, float] | None = None,
    min_bars: int = 30,
) -> list[str]:
    """Data-driven candidate generator (#3 / §40): rank a broad set of tickers by a market-data
    composite (momentum + low-vol + optional SEC quality) and return the top_n LIQUID names not in
    `exclude`. A liquidity gate (last price + average daily dollar volume) drops penny / illiquid names
    BEFORE ranking so the composite isn't spent on untradeable noise. PURE — daily bars are injected —
    so it finds names by the market's data, not the model's memory, and is testable without a network."""
    skip = {str(s).strip().upper() for s in (exclude or set())} | {"SPY"}
    closes_by_symbol: dict[str, list[float]] = {}
    for symbol, bars in (bars_by_symbol or {}).items():
        sym = str(symbol).strip().upper()
        if sym in skip:
            continue
        price, dollar_volume = liquidity_metrics(bars)
        if price < min_price or dollar_volume < min_dollar_volume:
            continue  # §37-style liquidity floor: real, tradeable size only
        closes = [close for _day, close in bars_to_dated_closes(bars)]
        if len(closes) >= min_bars:
            closes_by_symbol[sym] = closes
    if not closes_by_symbol:
        return []
    momentum = factor_signals(closes_by_symbol, min_bars=min_bars)
    vols = volatility_signals(closes_by_symbol, min_bars=min_bars)
    qual = {s: quality[s] for s in closes_by_symbol if s in quality} if quality else None
    composite = composite_factor_scores(momentum, qual, vols)  # 0-1 percentile blend (§33)
    ranked = sorted(composite.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    return [sym for sym, _score in ranked[: max(0, top_n)]]


def discover_market_candidates(
    client: Any,
    pool: list,
    *,
    exclude: set | None = None,
    top_n: int = 30,
    batch_size: int = 200,
    start: str | None = None,
    quality: dict[str, float] | None = None,
) -> list[str]:
    """Edge for data-breadth discovery (#3 / §40): fetch daily bars for `pool` (a bounded slice of the
    real tradeable market) in batches, then return the top_n data-ranked liquid candidates via
    `screen_market_candidates`. Best-effort and bounded — the caller passes a capped, rotating slice to
    respect rate limits + the free feed; a failed batch is skipped, not fatal. Only the bars fetch
    touches the network. Returns [] on no data."""
    names = [str(s).strip().upper() for s in (pool or []) if str(s).strip()]
    if not names:
        return []
    start = start or (datetime.now() - timedelta(days=160)).strftime("%Y-%m-%d")  # ~5mo of daily bars
    bars_by_symbol: dict[str, Any] = {}
    for i in range(0, len(names), max(1, batch_size)):
        batch = names[i:i + max(1, batch_size)]
        try:
            bars = client.get_bars_multi(batch, timeframe="1Day", start=start)
        except Exception:  # noqa: BLE001 — skip a failed batch, keep the rest
            continue
        if bars:
            bars_by_symbol.update(bars)
    return screen_market_candidates(bars_by_symbol, exclude=exclude, top_n=top_n, quality=quality)


def build_rebalance_plan(
    total_equity: float,
    cash: float,
    holdings: dict[str, float],
    watchlist: list[dict[str, Any]],
    research_fn: Callable[[str], str],
    *,
    max_position_pct: float = 0.20,
    max_positions: int = 0,
    factor_scores: dict[str, float] | None = None,
    factor_overlay: bool = False,
    volatilities: dict[str, float] | None = None,
    vol_adjust: bool = False,
    adversarial: bool = False,
    adversarial_max_checks: int = ADVERSARIAL_MAX_CHECKS,
    risk: "RiskControls | None" = None,
    cash_buffer_pct: float = 0.10,
    preset: str = "Aggressive",
    min_trade_usd: float = 1.0,
    deploy_budget: float = 0.0,
    memory: Any = None,
    rating_max_age_days: float = 0.0,
    special_symbols: list | None = None,
    special_allocation_pct: float = 0.0,
    special_max_position_pct: float = DEFAULT_SPECIAL_MAX_POSITION_PCT,
    special_principal: float = 0.0,
    daytrade_symbols: list | None = None,
    daytrade_allocation_pct: float = 0.0,
    daytrade_max_position_pct: float = DEFAULT_DAYTRADE_MAX_POSITION_PCT,
    holdings_pl: dict | None = None,
    market_context_fn: Callable[[list], str] | None = None,
    on_issue: Callable[[str], None] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    performance_override: str = "",
) -> RebalancePlan:
    """Move a live portfolio toward AI-rated target weights: buy underweight, sell/trim overweight, exit downgrades.

    Three sleeves share the investable budget: the **Core** (roster, conviction-weighted), **Special**
    (buy-and-hold moonshots, §21), and **Day-trade** (short-term momentum with take-profit/stop-loss
    exits, §27). Special and Day-trade are each carved off the top by their own % before the Core sizes."""
    risk = risk or RiskControls()
    total_equity = max(0.0, float(total_equity))
    cash = max(0.0, float(cash))
    holdings = {str(symbol).upper(): max(0.0, float(value)) for symbol, value in (holdings or {}).items()}

    merged_watchlist = merge_universe(watchlist, holdings)
    universe_symbols = [
        str(item.get("symbol", "")).strip().upper() for item in merged_watchlist if item.get("symbol")
    ]

    # Reuse recent ratings if they're fresh enough (cadence, §14); otherwise re-rate the universe.
    cached = None
    if memory is not None and rating_max_age_days > 0:
        try:
            cached = memory.cached_ratings(universe_symbols, rating_max_age_days)
        except Exception:
            cached = None

    raw = ""  # vestigial RebalancePlan.raw_response (unused); chunked rating keeps no single raw
    if cached is not None:
        ratings = cached  # no model call, and no re-save so the freshness clock keeps ticking
    else:
        # Feedback loop (§17): realized record + how current picks are doing + vs the S&P.
        performance = performance_override or performance_digest(memory, holdings_pl=holdings_pl)
        # Rate the whole universe in batches (HELIX 500-safe) and merge — fetched only on a re-rate.
        ratings = rate_universe(
            total_equity, merged_watchlist, research_fn, preset,
            performance=performance, market_context_fn=market_context_fn,
            on_issue=on_issue, progress_fn=progress_fn,
        )
        # Adversarial bull/bear/judge stress-test on the top buys (§34) — overrides the rating with the
        # judge's verdict before it's persisted, so the scorecard records the considered decision.
        if adversarial and ratings:
            ratings = apply_adversarial_review(
                ratings, research_fn, market_context_fn=market_context_fn,
                performance=performance, max_checks=adversarial_max_checks, progress_fn=progress_fn,
            )
        if memory is not None and ratings:
            try:
                memory.save_stock_rationales(ratings)
                memory.record_rating_snapshots(ratings)  # §28: append-only history for the scorecard
            except Exception:
                pass

    # Factor backbone + LLM overlay (§33): temper the model's buys with the deterministic composite
    # factor (numbers as a check on the story). Applied AFTER the snapshot above, so the scorecard
    # records the model's own call while trading acts on the blended decision. `factor_scores` is the
    # composite (momentum + quality + low-vol) supplied by the caller. Off by default.
    if factor_overlay and factor_scores:
        ratings = apply_factor_overlay(ratings, factor_scores)

    base = deploy_budget if deploy_budget and deploy_budget > 0 else total_equity
    # Defensive cash buffer (§35): in an account drawdown past the brake, OR a risk-off market regime
    # (SPY below its long trend), hold more cash and deploy less — survive bad stretches. Either
    # trigger raises the buffer to the defensive level; otherwise the normal buffer applies.
    effective_cash_buffer = cash_buffer_pct
    in_drawdown = risk.drawdown_brake_pct > 0 and compute_drawdown(total_equity, risk.equity_peak) >= risk.drawdown_brake_pct
    if in_drawdown or risk.risk_off:
        effective_cash_buffer = max(cash_buffer_pct, risk.defensive_cash_buffer_pct)
    investable = base * (1.0 - effective_cash_buffer)
    caps = _allocation_caps(merged_watchlist)
    hard_cap = base * max_position_pct

    # Roster discipline (HELIX 100): only names on the roster (watchlist) are buy-eligible, so a
    # held name rotated off the roster gets no target and is exited. When no roster is defined
    # (e.g. a CLI rebalance from holdings only), fall back to rating-driven buys for any name.
    roster_symbols = {str(item.get("symbol", "")).strip().upper() for item in (watchlist or []) if item.get("symbol")}

    # Special Stocks sleeve (§21): carve a capped % off the top for high-risk satellite bets, sized
    # separately from the core. Special names are kept distinct from the roster core.
    special_set = set(normalize_roster(special_symbols)) - roster_symbols
    special_cap_budget = base * max(0.0, special_allocation_pct) if special_set else 0.0
    if special_principal and special_principal > 0:
        # House-money (conservative): fund specials ONLY from gains above protected principal,
        # capped at the sleeve %. Principal is never routed to the speculative sleeve.
        gains = max(0.0, total_equity - special_principal)
        special_budget = min(gains, special_cap_budget)
    else:
        special_budget = special_cap_budget

    # Day-trade sleeve (§27): a third capped sleeve, distinct from the roster core AND the special set.
    daytrade_set = set(normalize_roster(daytrade_symbols)) - roster_symbols - special_set
    daytrade_budget = base * max(0.0, daytrade_allocation_pct) if daytrade_set else 0.0

    core_investable = max(0.0, investable - special_budget - daytrade_budget)

    buy_symbols = [
        symbol
        for symbol, record in ratings.items()
        if record["action"] == "buy" and symbol not in special_set and (not roster_symbols or symbol in roster_symbols)
    ]
    # Concentration (§30): cap the core at the top-N buy names so capital concentrates in the best
    # ideas instead of spreading into a closet index across the whole universe. Rank by conviction
    # tier first, then the data-driven factor score (momentum/trend), then symbol for determinism;
    # keep the top N. 0 = uncapped (the default — no behavior change). Names that miss the cut get no
    # target and are exited like any other non-buy, so turning this on deliberately concentrates.
    keep_n = max_positions
    if max_positions and max_positions > 0 and risk.min_positions > 0:
        keep_n = max(max_positions, risk.min_positions)  # §35 diversification floor: never below N names
    if keep_n and keep_n > 0 and len(buy_symbols) > keep_n:
        scores = factor_scores or {}
        buy_symbols = sorted(
            buy_symbols,
            key=lambda s: (CONFIDENCE_WEIGHT.get(ratings[s]["confidence"], 1.0), scores.get(s, 0.0), s),
            reverse=True,
        )[:keep_n]
    if preset == "Aggressive":
        weights = {symbol: CONFIDENCE_WEIGHT.get(ratings[symbol]["confidence"], 1.0) for symbol in buy_symbols}
    else:
        weights = {symbol: 1.0 for symbol in buy_symbols}
    # Volatility-adjusted sizing (§31): tilt each buy weight by a BOUNDED inverse-vol multiplier
    # (median_vol / vol, clamped to [0.25x, 4x]) so steadier names get more and jumpy names less —
    # targeting more equal RISK per position, which tends to lift Sharpe / cut drawdown. Conviction
    # stays the primary driver (it's a tilt, not a replacement); names without a vol estimate are
    # left at 1x. Off by default; the median anchor keeps the book's gross exposure ~unchanged.
    if vol_adjust and volatilities:
        observed = [volatilities[s] for s in buy_symbols if volatilities.get(s, 0.0) > 0]
        median_vol = statistics.median(observed) if observed else 0.0
        if median_vol > 0:
            tilted: dict[str, float] = {}
            for symbol in buy_symbols:
                vol = volatilities.get(symbol, 0.0)
                multiplier = max(0.25, min(4.0, median_vol / max(vol, 0.25 * median_vol))) if vol > 0 else 1.0
                tilted[symbol] = weights[symbol] * multiplier
            weights = tilted
    total_weight = sum(weights.values()) or 1.0

    targets: dict[str, float] = {}
    for symbol in buy_symbols:
        alloc = core_investable * (weights[symbol] / total_weight)
        cap = hard_cap
        watchlist_cap = caps.get(symbol)
        if watchlist_cap is not None:
            cap = min(cap, alloc * watchlist_cap)
        targets[symbol] = round(min(alloc, cap), 2)

    # Sector cap (§35): trim any one sector back to its share of the book so the core isn't secretly
    # one big macro bet. Applies to the core sleeve; unmapped names are exempt; freed budget -> cash.
    if risk.sectors and risk.sector_cap_pct > 0:
        targets = apply_sector_cap(targets, risk.sectors, base * risk.sector_cap_pct)

    # Special sleeve (§21) — BUY-AND-HOLD: hold what we already own (no trim, no exit) so a winner can
    # run like NVIDIA, and use whatever budget is left to open small new positions in fresh picks.
    if special_set and special_budget > 0:
        special_cap = base * max(0.0, special_max_position_pct)
        held_value = sum(holdings.get(symbol, 0.0) for symbol in special_set if holdings.get(symbol, 0.0) > 0)
        new_names = [symbol for symbol in special_set if holdings.get(symbol, 0.0) <= 0]
        for symbol in special_set:  # hold existing positions exactly (target = current -> no trade)
            current_val = holdings.get(symbol, 0.0)
            if current_val > 0:
                targets[symbol] = round(current_val, 2)
        available = max(0.0, special_budget - held_value)
        if new_names and available > 0:  # open new positions from the remaining budget, capped small
            entry = available / len(new_names)
            entry = min(entry, special_cap) if special_cap > 0 else entry
            if entry >= min_trade_usd:
                for symbol in new_names:
                    targets[symbol] = round(entry, 2)

    # Day-trade sleeve (§27): take-profit / stop-loss exits on held momentum names, then deploy the
    # remaining sleeve budget into fresh picks. A held name that rotated OFF the pick list (no longer in
    # daytrade_set, and not roster/special) simply gets no target here and is exited by the generic loop.
    exit_reasons: dict[str, str] = {}
    if daytrade_set:
        dt_cap = base * max(0.0, daytrade_max_position_pct)
        held_value = 0.0
        for symbol in daytrade_set:
            current_val = holdings.get(symbol, 0.0)
            if current_val <= 0:
                continue
            plpc = _position_plpc(holdings_pl, symbol)
            if plpc is not None and plpc >= DAYTRADE_TAKE_PROFIT_PCT:
                targets[symbol] = 0.0
                exit_reasons[symbol] = "day-trade: take profit"
            elif plpc is not None and plpc <= DAYTRADE_STOP_LOSS_PCT:
                targets[symbol] = 0.0
                exit_reasons[symbol] = "day-trade: stop loss"
            else:
                targets[symbol] = round(current_val, 2)  # ride it toward the target or the stop
                held_value += current_val
        new_names = [symbol for symbol in daytrade_set if holdings.get(symbol, 0.0) <= 0]
        available = max(0.0, daytrade_budget - held_value)
        if new_names and available > 0:
            entry = available / len(new_names)
            entry = min(entry, dt_cap) if dt_cap > 0 else entry
            if entry >= min_trade_usd:
                for symbol in new_names:
                    targets[symbol] = round(entry, 2)

    # Per-stock stop-loss (§35): exit a CORE holding that has fallen past the stop — a deep catastrophe
    # brake to cap single-name blow-ups (NOT the special sleeve, which is buy-and-hold, nor day-trade,
    # which has its own ±TP/SL). Overrides any buy/hold target for that name.
    if risk.core_stop_loss_pct > 0 and holdings_pl:
        stop = -abs(risk.core_stop_loss_pct)
        for symbol, current in holdings.items():
            if current <= 0 or symbol in special_set or symbol in daytrade_set:
                continue
            plpc = _position_plpc(holdings_pl, symbol)
            if plpc is not None and plpc <= stop:
                targets[symbol] = 0.0
                exit_reasons[symbol] = "stop loss (core)"

    raw_actions: list[RebalanceAction] = []
    for symbol in sorted(set(holdings) | set(targets)):
        current = holdings.get(symbol, 0.0)
        target = targets.get(symbol, 0.0)
        rating = ratings.get(symbol, {"confidence": "", "rationale": ""})
        diff = round(target - current, 2)
        if abs(diff) < min_trade_usd:
            continue
        if diff < 0:
            reason = exit_reasons.get(symbol) or ("exit: not buy-rated" if target == 0 else "trim to target/cap")
            side = "sell"
            amount = round(-diff, 2)
        else:
            reason = "new buy" if current == 0 else "add to target"
            side = "buy"
            amount = round(diff, 2)
        raw_actions.append(
            RebalanceAction(
                symbol=symbol,
                side=side,
                amount_usd=amount,
                reason=reason,
                current_usd=round(current, 2),
                target_usd=round(target, 2),
                confidence=rating.get("confidence", ""),
                rationale=rating.get("rationale", ""),
            )
        )

    # Fund buys from cash plus sell proceeds; scale buys down if there isn't enough.
    sells = [action for action in raw_actions if action.side == "sell"]
    buys = [action for action in raw_actions if action.side == "buy"]
    available_for_buys = cash + sum(action.amount_usd for action in sells)
    total_buy = sum(action.amount_usd for action in buys)
    if total_buy > available_for_buys and total_buy > 0:
        scale = available_for_buys / total_buy
        buys = [replace(action, amount_usd=round(action.amount_usd * scale, 2)) for action in buys]
        buys = [action for action in buys if action.amount_usd >= min_trade_usd]

    return RebalancePlan(
        total_equity=total_equity,
        cash=cash,
        investable=round(investable, 2),
        cash_buffer_pct=cash_buffer_pct,
        max_position_pct=max_position_pct,
        preset=preset,
        targets=targets,
        actions=sells + buys,  # sells first so proceeds fund the buys
        ratings=ratings,
        raw_response=raw,
    )


def execute_rebalance(
    actions: list[RebalanceAction],
    alpaca_client: Any,
    memory: Any,
    mode_label: str = "paper",
    holdings_pl: dict | None = None,
) -> list[tuple[RebalanceAction, str]]:
    """Submit each action as a market/day notional order and log it. One failure never aborts the batch."""
    entry_type = "live_trade" if mode_label == "live" else "paper_trade"
    results: list[tuple[RebalanceAction, str]] = []
    for action in actions:
        if action.amount_usd <= 0:
            continue
        try:
            order = alpaca_client.submit_order(
                symbol=action.symbol,
                side=action.side,
                notional=action.amount_usd,
            )
            status = order.get("status", "submitted")
            order_id = order.get("id", "unknown")
            outcome = f"{action.side} ${action.amount_usd:,.2f} -> {status}"
            memory.add_journal_entry(
                entry_type=entry_type,
                title=f"{mode_label.title()} {action.side} {action.symbol}",
                body="\n".join(
                    [
                        f"Order ID: {order_id}",
                        f"Status: {status}",
                        f"Symbol: {action.symbol}",
                        f"Side: {action.side}",
                        f"Notional: {action.amount_usd:.2f}",
                        f"Reason: {action.reason}",
                        f"Confidence: {action.confidence}",
                        f"Rationale: {action.rationale}",
                        "Order type: market / day",
                    ]
                ),
            )
            if action.side == "sell":
                pl = (holdings_pl or {}).get(action.symbol)
                return_pct = realized_pl = None
                if pl:
                    market_value = float(pl.get("market_value", 0.0) or 0.0)
                    return_pct = float(pl.get("unrealized_plpc", 0.0) or 0.0) * 100.0
                    if market_value:
                        realized_pl = float(pl.get("unrealized_pl", 0.0) or 0.0) * (action.amount_usd / market_value)
                try:
                    memory.record_sell(
                        action.symbol, action.reason, action.rationale, action.amount_usd, return_pct, realized_pl
                    )
                except Exception:
                    pass
        except AlpacaError as error:
            outcome = f"FAILED: {error}"
        results.append((action, outcome))
    return results


def render_rebalance_plan(plan: RebalancePlan) -> str:
    lines = [
        "HELIX Rebalance Plan (proposal)",
        f"Posture: {plan.preset} | Max/stock: {plan.max_position_pct * 100:.0f}% | Cash buffer: {plan.cash_buffer_pct * 100:.0f}%",
        f"Equity: ${plan.total_equity:,.2f} | Cash: ${plan.cash:,.2f} | Investable: ${plan.investable:,.2f}",
        "",
    ]
    if not plan.actions:
        lines.append("No trades needed - portfolio already on target.")
        return "\n".join(lines)

    lines.append(f"{'SIDE':<5} {'SYMBOL':<8} {'AMOUNT':>12}  {'CUR -> TGT':>22}  REASON")
    for action in plan.actions:
        move = f"${action.current_usd:,.0f} -> ${action.target_usd:,.0f}"
        lines.append(
            f"{action.side.upper():<5} {action.symbol:<8} ${action.amount_usd:>10,.2f}  {move:>22}  {action.reason}"
        )
    lines.append("")
    lines.append(
        f"Sells: {len(plan.sells)} (${plan.total_sell_usd:,.2f})  |  Buys: {len(plan.buys)} (${plan.total_buy_usd:,.2f})"
    )
    lines.append("Note: proposal only. Paper unless you switch to real money. Not financial advice.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Portfolio snapshot — "what do I own / what is it worth" for the dashboard.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    qty: float
    market_value: float
    avg_entry: float
    unrealized_pl: float
    unrealized_plpc: float


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: float
    cash: float
    market_value: float
    unrealized_pl: float
    positions: list

    @property
    def invested_pct(self) -> float:
        if self.equity <= 0:
            return 0.0
        return round(self.market_value / self.equity * 100.0, 1)


def portfolio_snapshot(account: dict, positions: list) -> PortfolioSnapshot:
    def to_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    rows: list[PositionRow] = []
    total_market_value = 0.0
    total_unrealized = 0.0
    for position in positions or []:
        market_value = to_float(position.get("market_value"))
        unrealized = to_float(position.get("unrealized_pl"))
        total_market_value += market_value
        total_unrealized += unrealized
        rows.append(
            PositionRow(
                symbol=str(position.get("symbol", "")),
                qty=to_float(position.get("qty")),
                market_value=market_value,
                avg_entry=to_float(position.get("avg_entry_price")),
                unrealized_pl=unrealized,
                unrealized_plpc=to_float(position.get("unrealized_plpc")) * 100.0,
            )
        )

    rows.sort(key=lambda row: row.market_value, reverse=True)
    return PortfolioSnapshot(
        equity=to_float(account.get("equity") or account.get("portfolio_value")),
        cash=to_float(account.get("cash")),
        market_value=round(total_market_value, 2),
        unrealized_pl=round(total_unrealized, 2),
        positions=rows,
    )


# --------------------------------------------------------------------------- #
# Equity curve — "is it actually working" over time. The chart widget consumes
# an EquitySeries regardless of whether it came from Alpaca or HELIX's own DB.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EquitySeries:
    points: list  # list[float], oldest first
    start_label: str = ""
    end_label: str = ""

    def __bool__(self) -> bool:
        return len(self.points) >= 2

    @property
    def start(self) -> float:
        return self.points[0] if self.points else 0.0

    @property
    def end(self) -> float:
        return self.points[-1] if self.points else 0.0

    @property
    def low(self) -> float:
        return min(self.points) if self.points else 0.0

    @property
    def high(self) -> float:
        return max(self.points) if self.points else 0.0

    @property
    def change_usd(self) -> float:
        return round(self.end - self.start, 2)

    @property
    def change_pct(self) -> float:
        if not self.points or self.start == 0:
            return 0.0
        return round((self.end - self.start) / self.start * 100.0, 2)


def _epoch_label(epoch: Any) -> str:
    try:
        return datetime.fromtimestamp(int(epoch)).strftime("%b %d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _iso_label(iso: Any) -> str:
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%b %d")
    except (TypeError, ValueError):
        return str(iso)[:10]


def parse_portfolio_history(history: dict) -> EquitySeries:
    """Alpaca /v2/account/portfolio/history -> EquitySeries, dropping null/invalid equity points."""
    timestamps = (history or {}).get("timestamp") or []
    equities = (history or {}).get("equity") or []
    points: list[float] = []
    times: list[Any] = []
    for stamp, value in zip(timestamps, equities):
        if value is None:
            continue
        try:
            equity = float(value)
        except (TypeError, ValueError):
            continue
        if equity <= 0:
            continue
        points.append(round(equity, 2))
        times.append(stamp)
    return EquitySeries(
        points=points,
        start_label=_epoch_label(times[0]) if times else "",
        end_label=_epoch_label(times[-1]) if times else "",
    )


def equity_series_from_rows(rows: list[dict]) -> EquitySeries:
    """HELIX's own equity_history rows (memory.list_equity_history) -> EquitySeries."""
    points: list[float] = []
    labels: list[str] = []
    for row in rows or []:
        try:
            equity = float(row.get("equity"))
        except (TypeError, ValueError):
            continue
        if equity <= 0:
            continue
        points.append(round(equity, 2))
        labels.append(str(row.get("created_at", "")))
    return EquitySeries(
        points=points,
        start_label=_iso_label(labels[0]) if labels else "",
        end_label=_iso_label(labels[-1]) if labels else "",
    )


def parse_stock_bars(payload: dict, symbol: str = "") -> list[float]:
    """Alpaca market-data bars -> chronological list of closes. Handles multi-symbol + flat shapes."""
    bars = (payload or {}).get("bars")
    if isinstance(bars, dict):
        symbol = (symbol or "").strip().upper()
        series = bars.get(symbol) if symbol else next(iter(bars.values()), [])
    elif isinstance(bars, list):
        series = bars
    else:
        series = []
    closes: list[float] = []
    for bar in series or []:
        if not isinstance(bar, dict):
            continue
        try:
            close = float(bar.get("c"))
        except (TypeError, ValueError):
            continue
        if close > 0:
            closes.append(close)
    return closes


def benchmark_series(
    start_value: float, closes: list[float], start_label: str = "", end_label: str = ""
) -> EquitySeries:
    """Normalize index closes to begin at `start_value` so the benchmark shares the account's dollar
    scale and the two curves diverge visibly. Empty series if there's nothing to draw (§19)."""
    try:
        start_value = float(start_value)
    except (TypeError, ValueError):
        return EquitySeries([])
    closes = [close for close in (closes or []) if close > 0]
    if start_value <= 0 or len(closes) < 2:
        return EquitySeries([])
    base = closes[0]
    points = [round(start_value * (close / base), 2) for close in closes]
    return EquitySeries(points=points, start_label=start_label, end_label=end_label)


# --------------------------------------------------------------------------- #
# Prediction scorecard (§28) — MEASURE BEFORE YOU OPTIMIZE. Score each past
# rating's realized forward return at 1w / 1m / 3m, bucketed by confidence, vs
# the S&P 500. The core test: do high-conviction buys actually beat low-conviction
# (and the index)? Pure: all price data is injected; the I/O lives at the edges.
# --------------------------------------------------------------------------- #

# Forward-return horizons: (label, calendar days). The window over which a rating is judged.
RATING_HORIZONS: tuple[tuple[str, int], ...] = (("1w", 7), ("1m", 30), ("3m", 90))

# Display/iteration order for the (action, confidence) buckets in the scorecard.
_BUCKET_ORDER: dict[tuple[str, str], int] = {
    ("buy", "high"): 0, ("buy", "medium"): 1, ("buy", "low"): 2,
    ("watch", "high"): 3, ("watch", "medium"): 4, ("watch", "low"): 5,
    ("skip", "high"): 6, ("skip", "medium"): 7, ("skip", "low"): 8,
    ("special", "high"): 9, ("special", "medium"): 10, ("special", "low"): 11,
    ("daytrade", "high"): 12, ("daytrade", "medium"): 13, ("daytrade", "low"): 14,
}


@dataclass(frozen=True)
class RatingOutcome:
    """One rating scored over one forward horizon (§28)."""
    symbol: str
    action: str
    confidence: str
    rated_at: str           # YYYY-MM-DD (the snapshot date)
    horizon: str            # "1w" | "1m" | "3m"
    entry_price: float
    exit_price: float
    return_pct: float       # the name's forward return over the horizon
    benchmark_pct: float | None  # SPY's return over the same dates, or None if unavailable
    excess_pct: float | None     # return_pct - benchmark_pct (the edge vs just owning the index)
    matured: bool           # True once the full horizon has elapsed as of `asof`


def _parse_day(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def bars_to_dated_closes(bars: Any) -> list[tuple[str, float]]:
    """Alpaca daily bars (list of {t, c, …}) -> [(YYYY-MM-DD, close)] sorted oldest-first.

    Pure and tolerant of missing/garbled bars. This is the structured form the scorer needs
    (the equity-curve `parse_stock_bars` drops the dates; the scorer matches returns BY date)."""
    out: list[tuple[str, float]] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        day = str(bar.get("t", ""))[:10]
        try:
            close = float(bar.get("c"))
        except (TypeError, ValueError):
            continue
        if len(day) == 10 and close > 0:
            out.append((day, close))
    out.sort(key=lambda pair: pair[0])
    return out


def _close_on_or_after(closes: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    """First (date, close) at or after `day`; `closes` must be sorted oldest-first."""
    for date_str, close in closes:
        if date_str >= day:
            return (date_str, close)
    return None


def score_rating_snapshots(
    snapshots: list[dict[str, Any]],
    closes_by_symbol: dict[str, list[tuple[str, float]]],
    spy_closes: list[tuple[str, float]] | None = None,
    *,
    asof: str | None = None,
    horizons: tuple[tuple[str, int], ...] = RATING_HORIZONS,
) -> list[RatingOutcome]:
    """Score each rating snapshot's realized forward return at each horizon, vs SPY (§28).

    PURE — all price history is injected. For each (snapshot, horizon): the **entry** is the first
    close on/after the rating date; the **exit** is the first close on/after rating_date + horizon
    days. An outcome is `matured` once that full horizon has elapsed as of `asof` (default: today).
    Immature outcomes are still returned (exit = latest available close) and flagged, so a caller can
    show in-flight progress; the summary keys on matured only, so headline stats are never polluted
    by half-elapsed horizons. Snapshots whose entry has no forward data yet are skipped entirely.
    """
    asof_dt = _parse_day(asof) or datetime.now()
    spy = spy_closes or []
    outcomes: list[RatingOutcome] = []
    for snap in snapshots or []:
        symbol = str(snap.get("symbol", "")).strip().upper()
        rated_dt = _parse_day(snap.get("created_at") or snap.get("rated_at"))
        if not symbol or rated_dt is None:
            continue
        closes = closes_by_symbol.get(symbol) or []
        if len(closes) < 2:
            continue
        entry = _close_on_or_after(closes, rated_dt.strftime("%Y-%m-%d"))
        if entry is None:
            continue
        entry_date, entry_close = entry
        if entry_close <= 0:
            continue
        action = str(snap.get("action", "")).strip().lower()
        confidence = str(snap.get("confidence", "")).strip().lower()
        for label, days in horizons:
            target_dt = rated_dt + timedelta(days=days)
            matured = asof_dt >= target_dt
            exit_point = _close_on_or_after(closes, target_dt.strftime("%Y-%m-%d"))
            if exit_point is None:  # horizon not reached in the data yet — show progress so far
                exit_point = closes[-1]
                matured = False
            exit_date, exit_close = exit_point
            if exit_close <= 0 or exit_date <= entry_date:
                continue  # no forward bar after entry yet — nothing to measure
            ret = (exit_close / entry_close - 1.0) * 100.0
            benchmark = excess = None
            if spy:
                spy_entry = _close_on_or_after(spy, entry_date)
                spy_exit = _close_on_or_after(spy, exit_date)
                if spy_entry and spy_exit and spy_entry[1] > 0:
                    benchmark = (spy_exit[1] / spy_entry[1] - 1.0) * 100.0
                    excess = ret - benchmark
            outcomes.append(
                RatingOutcome(
                    symbol=symbol,
                    action=action,
                    confidence=confidence,
                    rated_at=entry_date,
                    horizon=label,
                    entry_price=round(entry_close, 4),
                    exit_price=round(exit_close, 4),
                    return_pct=round(ret, 2),
                    benchmark_pct=None if benchmark is None else round(benchmark, 2),
                    excess_pct=None if excess is None else round(excess, 2),
                    matured=matured,
                )
            )
    return outcomes


def summarize_rating_outcomes(outcomes: list[RatingOutcome]) -> dict[str, Any]:
    """Bucket scored outcomes by (action, confidence) within each horizon (§28).

    Per bucket: the **matured** count, average forward return, **hit rate** (% positive), and average
    **excess vs SPY** — the numbers that answer "do high-conviction buys beat low-conviction and the
    index?". Immature outcomes are counted as `pending` (kept out of the stats) so an early scorecard
    reports honestly instead of showing returns from half-elapsed horizons.
    """
    horizons: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for outcome in outcomes or []:
        buckets = horizons.setdefault(outcome.horizon, {})
        bucket = buckets.setdefault(
            (outcome.action, outcome.confidence), {"returns": [], "excess": [], "pending": 0}
        )
        if outcome.matured:
            bucket["returns"].append(outcome.return_pct)
            if outcome.excess_pct is not None:
                bucket["excess"].append(outcome.excess_pct)
        else:
            bucket["pending"] += 1

    summary: dict[str, Any] = {}
    for horizon, buckets in horizons.items():
        rows: dict[tuple[str, str], dict[str, Any]] = {}
        for key, bucket in buckets.items():
            returns = bucket["returns"]
            n = len(returns)
            excess = bucket["excess"]
            rows[key] = {
                "n": n,
                "pending": bucket["pending"],
                "avg_return": round(sum(returns) / n, 2) if n else None,
                "hit_rate": round(100.0 * sum(1 for r in returns if r > 0) / n, 1) if n else None,
                "avg_excess": round(sum(excess) / len(excess), 2) if excess else None,
            }
        summary[horizon] = rows
    return summary


def _conviction_verdict(rows: dict[tuple[str, str], dict[str, Any]]) -> str:
    """One-line read on whether conviction is paying off in a horizon's buy buckets (§28)."""
    high = rows.get(("buy", "high"))
    low = rows.get(("buy", "low"))
    medium = rows.get(("buy", "medium"))
    if high and high["n"] and high["avg_excess"] is not None:
        beat = "beats" if high["avg_excess"] > 0 else "trails"
        edge = f"buy/high {beat} the S&P by {abs(high['avg_excess']):.1f} pts"
        ref = low if (low and low["n"]) else (medium if (medium and medium["n"]) else None)
        if ref is not None and ref["avg_return"] is not None and high["avg_return"] is not None:
            delta = high["avg_return"] - ref["avg_return"]
            sign = "+" if delta >= 0 else ""
            label = "buy/low" if ref is low else "buy/medium"
            edge += f"; vs {label} {sign}{delta:.1f} pts (conviction {'is' if delta > 0 else 'is NOT'} paying)"
        return edge
    return ""


def render_rating_scorecard(summary: dict[str, Any], header: str = "") -> str:
    """Render the prediction scorecard (§28) as a fixed-width text report."""
    lines = ["HELIX PREDICTION SCORECARD - realized forward return by rating"]
    if header:
        lines.append(header)
    lines.append("Core test: do high-conviction buys beat low-conviction and the S&P 500?")
    lines.append("")
    any_matured = False
    for label, _days in RATING_HORIZONS:
        buckets = summary.get(label, {})
        lines.append(f"== {label} forward ==")
        lines.append(f"  {'RATING':<14}{'N':>5}{'AVG RET':>10}{'HIT':>7}{'vs SPY':>9}{'PENDING':>9}")
        rows = sorted(buckets.items(), key=lambda kv: _BUCKET_ORDER.get(kv[0], 99))
        if not rows:
            lines.append("  (no snapshots in this horizon yet)")
        for (action, confidence), stats in rows:
            name = f"{action}/{confidence}"
            n = stats["n"]
            if n:
                any_matured = True
                avg = f"{stats['avg_return']:+.1f}%"
                hit = f"{stats['hit_rate']:.0f}%"
                excess = "n/a" if stats["avg_excess"] is None else f"{stats['avg_excess']:+.1f}%"
            else:
                avg = hit = excess = "-"
            lines.append(f"  {name:<14}{n:>5}{avg:>10}{hit:>7}{excess:>9}{stats['pending']:>9}")
        verdict = _conviction_verdict(buckets)
        if verdict:
            lines.append(f"  -> {verdict}")
        lines.append("")
    if not any_matured:
        lines.append("No outcomes have matured yet - the scorecard fills in as ratings age past each")
        lines.append("horizon (1 week / 1 month / 3 months). Snapshots accrue on every re-rate.")
        lines.append("")
    lines.append("Paper, simulated. Forward returns are gross of costs/taxes. Not financial advice.")
    return "\n".join(lines)


def build_rating_scorecard(
    snapshots: list[dict[str, Any]],
    closes_by_symbol: dict[str, list[tuple[str, float]]],
    spy_closes: list[tuple[str, float]] | None = None,
    *,
    asof: str | None = None,
    header: str = "",
) -> tuple[str, dict[str, Any]]:
    """Convenience: score -> summarize -> render in one call (§28). Returns (report_text, summary).
    Pure; the caller fetches the daily bars (the I/O edge) and injects them."""
    outcomes = score_rating_snapshots(snapshots, closes_by_symbol, spy_closes, asof=asof)
    summary = summarize_rating_outcomes(outcomes)
    return render_rating_scorecard(summary, header=header), summary


def fetch_scorecard_prices(
    client: Any, symbols: list, start: str
) -> tuple[dict[str, list[tuple[str, float]]], list[tuple[str, float]]]:
    """Edge I/O for the scorecard (§28): pull daily closes for `symbols` + SPY from Alpaca in one
    paginated call, as {SYMBOL: [(date, close)…]} + the SPY series. Best-effort — a symbol with no
    bars (e.g. delisted, or no IEX history) is simply absent, and the scorer skips it. `client` is
    duck-typed (needs `get_bars_multi`), so this is trivially stubbable in tests."""
    wanted = list(dict.fromkeys([str(s).strip().upper() for s in (symbols or []) if str(s).strip()] + ["SPY"]))
    try:
        bars = client.get_bars_multi(wanted, timeframe="1Day", start=start)
    except Exception:
        bars = {}
    closes_by_symbol: dict[str, list[tuple[str, float]]] = {}
    for symbol, sym_bars in (bars or {}).items():
        dated = bars_to_dated_closes(sym_bars)
        if dated:
            closes_by_symbol[str(symbol).upper()] = dated
    return closes_by_symbol, closes_by_symbol.get("SPY", [])


def generate_rating_scorecard(
    memory: Any, client: Any, *, days: int = 365, asof: str | None = None
) -> tuple[str, dict[str, Any]]:
    """End-to-end prediction scorecard (§28): read rating snapshots, fetch their daily bars + SPY,
    score and render. The edge orchestrator shared by the CLI and the desktop UI. Returns
    (report_text, summary). Pure scoring underneath; only the bars fetch touches the network."""
    snapshots = memory.list_rating_snapshots(days=days)
    if not snapshots:
        return (
            render_rating_scorecard({}, header="No ratings have been recorded yet - run a cycle first."),
            {},
        )
    symbols = sorted({str(s.get("symbol", "")).strip().upper() for s in snapshots if s.get("symbol")})
    dates = [str(s.get("created_at", ""))[:10] for s in snapshots if s.get("created_at")]
    start = min((d for d in dates if d), default="") or (
        datetime.now() - timedelta(days=days)
    ).strftime("%Y-%m-%d")
    closes_by_symbol, spy_closes = fetch_scorecard_prices(client, symbols, start)
    info = memory.rating_snapshot_summary()
    header = (
        f"{info['snapshots']} rating snapshots on {info['symbols']} names "
        f"since {info['since']} (latest {info['latest']})."
    )
    return build_rating_scorecard(snapshots, closes_by_symbol, spy_closes, asof=asof, header=header)


# Close-the-loop (§38): minimum MATURED outcomes a (action, confidence) bucket needs before its
# forward record is trusted enough to feed back into the rating prompt. Below this we stay silent
# rather than calibrate on noise — the whole point of the §28 scorecard. Baked default, not a UI knob.
SCORECARD_FEEDBACK_MIN_N = 8
# Daily cache for the distilled calibration line (the scorecard fetch is a full-universe bar pull).
INVEST_SCORECARD_FEEDBACK_SETTING = "invest_scorecard_feedback"
INVEST_SCORECARD_FEEDBACK_DATE_SETTING = "invest_scorecard_feedback_date"


def scorecard_feedback(summary: dict[str, Any], *, min_n: int = SCORECARD_FEEDBACK_MIN_N) -> str:
    """Distill the §28 scorecard summary into one calibration line for the research prompts (§38).

    Closes the loop: tells the model how its OWN buy-conviction has actually performed, net of the
    S&P 500, so it can calibrate confidence. Only buy buckets with at least `min_n` MATURED outcomes
    are trusted (below that we stay silent rather than learn from noise). Reports the LONGEST
    qualifying horizon first (3m > 1m > 1w — a months-to-years strategy shouldn't calibrate on a
    one-week blip), up to two, and names the horizon so even an early 1w-only signal reads honestly.
    Reuses `_conviction_verdict` so the prompt and the rendered scorecard never tell different
    stories. Returns '' when nothing has matured enough yet (today's empty state injects nothing)."""
    lines: list[str] = []
    for label, _days in reversed(RATING_HORIZONS):  # longest horizon first
        rows = summary.get(label, {})
        gated = {
            key: stats
            for key, stats in rows.items()
            if key[0] == "buy" and stats.get("n", 0) >= min_n
        }
        verdict = _conviction_verdict(gated)
        if not verdict:
            continue
        high = gated[("buy", "high")]  # present whenever the verdict is non-empty
        lines.append(f"at {label}, {verdict} (buy/high n={high['n']}, hit {high['hit_rate']:.0f}%)")
        if len(lines) >= 2:
            break
    if not lines:
        return ""
    return (
        "Calibration (your own forward returns by confidence, net of the S&P 500): "
        + "; ".join(lines)
        + ". Use this to calibrate confidence — reserve 'high' for setups like the ones that have "
        "actually beaten the index."
    )


def refresh_scorecard_feedback(memory: Any, client: Any, settings: Any, *, today: str | None = None) -> str:
    """The daily-cached scorecard calibration line for the feedback loop (§38). Best-effort.

    The scorecard scores against a full-universe daily-bar pull, so we distill it at most once per
    day and cache the text in settings (keyed by date STRING — comparing `(now - last).days` is an
    off-by-a-day trap). The `performance` string is only consumed by research calls, so once/day is
    ample freshness. Any failure (keys missing, network, market closed) returns the cached line — or
    '' — and never raises into the trading cycle. `today` is injectable for tests."""
    today_str = today or datetime.now().strftime("%Y-%m-%d")
    cached = settings.get(INVEST_SCORECARD_FEEDBACK_SETTING, "") or ""
    if settings.get(INVEST_SCORECARD_FEEDBACK_DATE_SETTING, "") == today_str:
        return cached  # already computed today — no fetch
    try:
        _report, summary = generate_rating_scorecard(memory, client)
        line = scorecard_feedback(summary)
        settings.set(INVEST_SCORECARD_FEEDBACK_SETTING, line)
        settings.set(INVEST_SCORECARD_FEEDBACK_DATE_SETTING, today_str)
        return line
    except Exception:  # noqa: BLE001 — feedback must never sink a trading cycle
        return cached


# --------------------------------------------------------------------------- #
# The "HELIX 100" — a self-curating universe. Discover + score candidates,
# rank them against incumbents, and rotate laggards out for stronger names.
# Pure: the model call is injected; the caller applies the new roster (§20).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RosterSwap:
    drop_symbol: str
    drop_score: float
    add_symbol: str
    add_score: float
    rationale: str


@dataclass(frozen=True)
class RosterReview:
    current_roster: list  # symbols, ordered
    incumbents: list  # [{symbol, score, rationale}] sorted best-first
    candidates: list  # [{symbol, score, rationale}] sorted best-first
    swaps: list  # [RosterSwap]
    new_roster: list  # symbols after applying the swaps
    held: set  # currently-held symbols (dropping one == a forced sell)
    max_swaps: int
    min_margin: float
    raw_response: str = ""

    @property
    def drops(self) -> list:
        return [swap.drop_symbol for swap in self.swaps]

    @property
    def adds(self) -> list:
        return [swap.add_symbol for swap in self.swaps]


def _track_record(memory: Any) -> str:
    if memory is None:
        return ""
    try:
        perf = memory.strategy_performance()
        if perf["closed"] > 0:
            return (
                f"hit rate {perf['hit_rate']}% over {perf['closed']} closed positions, "
                f"average return {perf['avg_return_pct']:+.1f}%, realized P/L ${perf['realized_pl']:+,.0f}"
            )
    except Exception:
        return ""
    return ""


def performance_digest(
    memory: Any, holdings_pl: dict | None = None, equity_review: str = "", scorecard: str = ""
) -> str:
    """The §17 feedback-loop summary the research prompts learn from, combining four signals (each
    optional). Ordered best-signal-first: the **scorecard calibration** line (§38 — the model's own
    forward returns by confidence, net of the S&P; the highest-quality signal, so it LEADS), then the
    **realized** closed-trade record (hit rate / avg return / P&L — noisier, ~95% rebalance-trim per
    §28), how **current picks** are doing (forward returns straight from live position P&L), and how
    the account is doing **vs the S&P** (`equity_review`). Returns '' if there's nothing to say."""
    parts: list[str] = []
    if scorecard:
        parts.append(scorecard.strip())
    realized = _track_record(memory)
    if realized:
        parts.append(f"Realized (closed trades): {realized}.")
    plpcs: list[tuple[str, float]] = []
    for symbol, info in (holdings_pl or {}).items():
        if not isinstance(info, dict):
            continue
        try:
            plpcs.append((str(symbol).strip().upper(), float(info.get("unrealized_plpc"))))
        except (TypeError, ValueError):
            continue
    if plpcs:
        up = sum(1 for _s, value in plpcs if value > 0)
        down = sum(1 for _s, value in plpcs if value < 0)
        best = sorted(plpcs, key=lambda pair: pair[1], reverse=True)[:5]
        worst = sorted(plpcs, key=lambda pair: pair[1])[:5]
        fmt = lambda rows: ", ".join(f"{sym} {value * 100:+.0f}%" for sym, value in rows)
        parts.append(
            f"Current holdings ({len(plpcs)}): {up} up, {down} down. "
            f"Biggest winners: {fmt(best)}. Biggest laggards: {fmt(worst)}."
        )
    if equity_review:
        parts.append(equity_review.strip())
    return " ".join(parts)


def build_roster_review(
    roster: Any,
    holdings: dict | None,
    research_fn: Callable[[str], str],
    *,
    max_swaps: int = 10,
    min_margin: float = 8.0,
    n_candidates: int = 30,
    memory: Any = None,
    market_context_fn: Callable[[list], str] | None = None,
    chunk_size: int = RATING_CHUNK_SIZE,
    on_issue: Callable[[str], None] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    tradable: set | None = None,
    screen_fn: Callable[[list], set] | None = None,
    seed_candidates: list | None = None,
) -> RosterReview:
    """Score the roster + discovered candidates, then propose margin-gated 1-for-1 rotations.

    **Chunked (§20):** incumbents are scored in batches of `chunk_size` (so the full HELIX 500 scores
    without truncating), then a single discovery call proposes new candidates anchored on the weakest
    incumbents (same 0-100 scale, head-to-head). Greedy: the best candidate replaces the worst
    incumbent only when it beats it by `min_margin`, up to `max_swaps`. Size-preserving.
    """
    roster = normalize_roster(roster)
    held = {str(symbol).strip().upper() for symbol in (holdings or {})}
    performance = _track_record(memory)

    # 1. Score every incumbent, in chunks (HELIX-500-safe). Each chunk fetches its own live context.
    scored: dict[str, dict[str, Any]] = {}
    chunks = [roster[i:i + max(1, chunk_size)] for i in range(0, len(roster), max(1, chunk_size))]
    for index, chunk in enumerate(chunks):
        if progress_fn is not None and len(chunks) > 1:
            first = index * chunk_size + 1
            progress_fn(f"Scoring the universe {first}-{first + len(chunk) - 1} of {len(roster)}...")
        try:  # a failed batch must not abort the whole roster review
            context = market_context_fn(chunk) if market_context_fn is not None else ""
            raw = research_fn(build_roster_score_prompt(chunk, performance, context)) or ""
        except Exception as exc:  # noqa: BLE001
            if on_issue is not None:
                on_issue(f"Roster scoring batch {index + 1}/{len(chunks)} failed (kept the rest): {exc}")
            continue
        records = parse_roster_review_json(raw).get("incumbents", [])
        if not records and on_issue is not None:
            on_issue(_research_issue(f"Roster scoring (batch {index + 1}/{len(chunks)})", raw))
        for record in records:
            scored[record["symbol"]] = record
    incumbents = [
        {
            "symbol": symbol,
            "score": float(scored.get(symbol, {}).get("score", 50.0)),
            "rationale": scored.get(symbol, {}).get("rationale", ""),
        }
        for symbol in roster
    ]
    worst_first = sorted(incumbents, key=lambda record: record["score"])

    # 2. Discover new candidates in one call, anchored on the weakest incumbents (the rotation targets).
    weak_anchor = [(record["symbol"], record["score"]) for record in worst_first[: max(10, max_swaps * 2)]]
    if progress_fn is not None:
        progress_fn("Discovering stronger candidates for the universe...")
    disc_context = market_context_fn([s for s, _score in weak_anchor]) if market_context_fn is not None else ""
    raw = research_fn(
        build_roster_discovery_prompt(weak_anchor, n_candidates, performance, disc_context,
                                      seed_candidates=seed_candidates)
    ) or ""
    parsed = parse_roster_review_json(raw)
    if not parsed.get("candidates") and on_issue is not None:
        on_issue(_research_issue("Roster candidate discovery", raw))

    roster_set = set(roster)
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for record in parsed.get("candidates", []):
        symbol = record["symbol"]
        if symbol in roster_set or symbol in seen:
            continue
        if tradable and symbol not in tradable:
            continue  # §36: only real, tradeable market tickers — drop anything not in the live universe
        seen.add(symbol)
        candidates.append({"symbol": symbol, "score": float(record.get("score", 0.0)), "rationale": record.get("rationale", "")})

    if screen_fn and candidates:  # §37: keep only liquid, quality candidates (S&P-caliber for the core)
        accepted = screen_fn([c["symbol"] for c in candidates])
        candidates = [c for c in candidates if c["symbol"] in accepted]

    best_first = sorted(candidates, key=lambda record: record["score"], reverse=True)

    swaps: list[RosterSwap] = []
    dropped: set[str] = set()
    added: set[str] = set()
    index = 0
    for candidate in best_first:
        if len(swaps) >= max_swaps:
            break
        while index < len(worst_first) and worst_first[index]["symbol"] in dropped:
            index += 1
        if index >= len(worst_first):
            break
        incumbent = worst_first[index]
        # Candidates are best-first and incumbents worst-first, so once the best remaining
        # candidate can't clear the margin, none of the rest can either.
        if candidate["score"] - incumbent["score"] < min_margin:
            break
        swaps.append(
            RosterSwap(
                drop_symbol=incumbent["symbol"],
                drop_score=round(incumbent["score"], 1),
                add_symbol=candidate["symbol"],
                add_score=round(candidate["score"], 1),
                rationale=candidate["rationale"] or f"scores {candidate['score']:.0f} vs {incumbent['score']:.0f}",
            )
        )
        dropped.add(incumbent["symbol"])
        added.add(candidate["symbol"])
        index += 1

    new_roster = [symbol for symbol in roster if symbol not in dropped]
    new_roster += [candidate["symbol"] for candidate in best_first if candidate["symbol"] in added]

    return RosterReview(
        current_roster=roster,
        incumbents=sorted(incumbents, key=lambda record: record["score"], reverse=True),
        candidates=best_first,
        swaps=swaps,
        new_roster=new_roster,
        held=held,
        max_swaps=max_swaps,
        min_margin=min_margin,
        raw_response=raw,
    )


def render_roster_review(review: RosterReview) -> str:
    lines = [
        "HELIX 100 - Roster Review (proposal)",
        f"Roster: {len(review.current_roster)} names  |  candidates considered: {len(review.candidates)}  |  "
        f"max swaps: {review.max_swaps}  |  min margin: {review.min_margin:.0f} pts",
        "",
    ]
    if not review.swaps:
        lines.append("No rotations: no candidate beats the weakest roster name by the required margin.")
    else:
        lines.append(f"{'DROP':<8} {'SCORE':>6}      {'ADD':<8} {'SCORE':>6}   WHY")
        for swap in review.swaps:
            held_flag = "*" if swap.drop_symbol in review.held else " "
            lines.append(
                f"{swap.drop_symbol:<8}{held_flag}{swap.drop_score:>5.0f}  ->  "
                f"{swap.add_symbol:<8} {swap.add_score:>6.0f}   {swap.rationale}"
            )
        lines.append("")
        lines.append(
            f"{len(review.swaps)} swap(s). '*' = currently held -> will be SOLD on the next rebalance/cycle."
        )
    lines.append("Note: proposal only. Paper unless you switch to real money. Not financial advice.")
    return "\n".join(lines)


def maybe_rotate_roster(
    settings: Any,
    memory: Any,
    symbols: list,
    holdings: dict | None,
    research_fn: Callable[[str], str],
    *,
    review_days: int = DEFAULT_ROSTER_REVIEW_DAYS,
    max_swaps: int = 10,
    min_margin: float = 8.0,
    n_candidates: int = 30,
    market_context_fn: Callable[[list], str] | None = None,
    chunk_size: int = RATING_CHUNK_SIZE,
    on_issue: Callable[[str], None] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    tradable: set | None = None,
    screen_fn: Callable[[list], set] | None = None,
    seed_fn: Callable[[], list] | None = None,
    force: bool = False,
) -> tuple:
    """Self-curating universe: run a HELIX 100 roster review if one is due, persist the result.

    `seed_fn` (data-breadth discovery, §40) is invoked ONLY when a review actually runs — it returns
    market-screener candidates for the model to judge — so the bounded bars scan never fires on a
    cadence-skipped cycle.

    Returns (symbols, reviewed, swaps). Cadence is calendar-based via a persisted timestamp, so it
    works even when the app runs only intermittently. The first call just stamps a baseline (no
    immediate rotation), so rotation begins one `review_days` window later. The review is **chunked**
    (§20), so the full HELIX 500 rotates without truncating — no size cap anymore.
    """
    symbols = normalize_roster(symbols)
    now = datetime.now()
    last = settings.get(LAST_ROSTER_REVIEW_SETTING, "")
    last_dt = None
    if last:
        try:
            last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d")
        except ValueError:
            last_dt = None
    if not force and last_dt is None:  # first run: stamp baseline, rotate next window
        settings.set(LAST_ROSTER_REVIEW_SETTING, now.strftime("%Y-%m-%d"))
        return symbols, False, 0
    if not force and last_dt is not None and (now - last_dt).days < review_days:
        return symbols, False, 0

    seeds = None
    if seed_fn is not None:
        try:
            seeds = seed_fn()  # data-breadth discovery (§40) — only fetched when a review actually runs
        except Exception:  # noqa: BLE001 — discovery is best-effort; fall back to model brainstorming
            seeds = None
    review = build_roster_review(
        symbols, holdings, research_fn,
        max_swaps=max_swaps, min_margin=min_margin, n_candidates=n_candidates, memory=memory,
        market_context_fn=market_context_fn, chunk_size=chunk_size, on_issue=on_issue, progress_fn=progress_fn,
        tradable=tradable, screen_fn=screen_fn, seed_candidates=seeds,
    )
    settings.set(LAST_ROSTER_REVIEW_SETTING, now.strftime("%Y-%m-%d"))
    if review.swaps:
        settings.set(ROSTER_SETTING, ", ".join(review.new_roster))
        return review.new_roster, True, len(review.swaps)
    return symbols, True, 0


def maybe_refresh_core_ratings(
    memory: Any,
    watchlist: list[dict[str, Any]],
    holdings: dict | None,
    research_fn: Callable[[str], str],
    *,
    total_equity: float = 0.0,
    preset: str = "Aggressive",
    rating_max_age_days: float = DEFAULT_RATING_MAX_AGE_DAYS,
    market_context_fn: Callable[[list], str] | None = None,
    on_issue: Callable[[str], None] | None = None,
    progress_fn: Callable[[str], None] | None = None,
    performance_override: str = "",
    adversarial: bool = False,
    adversarial_max_checks: int = ADVERSARIAL_MAX_CHECKS,
    force: bool = False,
) -> bool:
    """Warm the core (HELIX 500) ratings cache during idle/market-closed time, so opening-day trading
    runs off fresh cached ratings with no model call. Re-rates only if the cache is stale (same gating
    as build_rebalance_plan). Returns True if it researched. Persists to `stock_rationale` (§14/§17)."""
    if memory is None or (rating_max_age_days <= 0 and not force):
        return False
    merged = merge_universe(watchlist, holdings)
    universe = [str(item.get("symbol", "")).strip().upper() for item in merged if item.get("symbol")]
    if not universe:
        return False
    if not force:  # force (the manual "Refresh research now" button) re-rates regardless of cache age
        try:
            if memory.cached_ratings(universe, rating_max_age_days) is not None:
                return False  # still fresh — nothing to do
        except Exception:
            pass
    performance = performance_override or _track_record(memory)  # feedback loop (§17)
    ratings = rate_universe(  # batched (HELIX 500-safe); on_issue fires per empty batch
        total_equity, merged, research_fn, preset,
        performance=performance, market_context_fn=market_context_fn,
        on_issue=on_issue, progress_fn=progress_fn,
    )
    if adversarial and ratings:  # bull/bear/judge stress-test of the top buys (§34)
        ratings = apply_adversarial_review(
            ratings, research_fn, market_context_fn=market_context_fn,
            performance=performance, max_checks=adversarial_max_checks, progress_fn=progress_fn,
        )
    if not ratings:
        return False
    try:
        memory.save_stock_rationales(ratings)
        memory.record_rating_snapshots(ratings)  # §28: append-only history for the scorecard
    except Exception:
        pass
    return True


def maybe_research_special(
    settings: Any,
    memory: Any,
    research_fn: Callable[[str], str],
    *,
    n_picks: int = 8,
    research_days: int = DEFAULT_SPECIAL_RESEARCH_DAYS,
    max_names: int = DEFAULT_SPECIAL_MAX_NAMES,
    holdings_pl: dict | None = None,
    max_rotations: int = 3,
    market_context_fn: Callable[[], str] | None = None,
    on_issue: Callable[[str], None] | None = None,
    performance: str = "",
    tradable: set | None = None,
    screen_fn: Callable[[list], set] | None = None,
    force: bool = False,
) -> tuple:
    """Scout high-risk Special Stocks (§21) during idle/market-closed time, cadence-gated.

    Returns (special_symbols, researched). Calls the model only when due; persists picks to settings
    and theses to stock_rationale (action 'special'). Accumulates up to `max_names`; when full, a
    fresh **high-conviction** pick evicts the **weakest non-winner** (an unproven, not-yet-held name
    first, else a held laggard) — but **never a winner** (a held position that's up). `holdings_pl`
    (Alpaca position P&L) identifies winners; without it, no eviction (just accumulate).
    """
    current = normalize_roster(settings.get(SPECIAL_SETTING, ""))
    now = datetime.now()
    last = settings.get(LAST_SPECIAL_RESEARCH_SETTING, "")
    last_dt = None
    if last:
        try:
            last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d")
        except ValueError:
            last_dt = None
    if not force and last_dt is not None and (now - last_dt).days < research_days:
        return current, False

    market_context = market_context_fn() if market_context_fn else ""  # live news (§25), fetched only on a scout
    raw = research_fn(build_special_research_prompt(n_picks, performance=performance, market_context=market_context)) or ""
    picks = parse_special_research_json(raw)
    if tradable:  # §36: keep only real, tradeable market tickers
        picks = [pick for pick in picks if pick.get("symbol") in tradable]
    if screen_fn and picks:  # §37: liquidity screen (must be tradeable enough to enter/exit)
        accepted = screen_fn([pick["symbol"] for pick in picks])
        picks = [pick for pick in picks if pick.get("symbol") in accepted]
    settings.set(LAST_SPECIAL_RESEARCH_SETTING, now.strftime("%Y-%m-%d"))
    if not picks:
        if on_issue is not None:
            on_issue(_research_issue("Special scout", raw))  # surface the silent failure (§10)
        return current, False

    # Buy-and-hold: ACCUMULATE the best new picks (highest conviction first) onto the existing list,
    # capped at `max_names`. Never auto-remove — once HELIX holds a moonshot it keeps holding it so a
    # winner can run (NVIDIA-style). Theses for all scouted names are stored for display.
    order = {"high": 0, "medium": 1, "low": 2}
    picks = sorted(picks, key=lambda pick: order.get(pick.get("conviction", "low"), 3))

    held_pl: dict[str, float] = {}
    for symbol, info in (holdings_pl or {}).items():
        try:
            held_pl[str(symbol).strip().upper()] = float((info or {}).get("unrealized_pl", 0.0) or 0.0)
        except (TypeError, ValueError):
            held_pl[str(symbol).strip().upper()] = 0.0

    def weakness(symbol: str):
        # Rank evictables weakest-first: not-yet-held (unproven) before held, then by worst P&L.
        held = symbol in held_pl
        return (1 if held else 0, held_pl.get(symbol, 0.0))

    can_rotate = holdings_pl is not None  # need P&L data to tell winners from laggards; else don't evict
    merged = list(current)
    ratings: dict[str, Any] = {}
    rotations = 0
    for pick in picks:
        symbol = pick["symbol"]
        if not symbol:
            continue
        ratings[symbol] = {
            "action": "special",
            "confidence": pick.get("conviction", ""),
            "rationale": pick.get("rationale", ""),
        }
        if symbol in merged:
            continue
        if len(merged) < max_names:
            merged.append(symbol)
        elif can_rotate and pick.get("conviction") == "high" and rotations < max_rotations:
            # Full sleeve: evict the weakest NON-WINNER (a held position that's up is never touched).
            evictable = sorted((s for s in merged if held_pl.get(s, 0.0) <= 0), key=weakness)
            if evictable:
                merged[merged.index(evictable[0])] = symbol  # laggard out, fresh high-conviction in
                rotations += 1
    settings.set(SPECIAL_SETTING, ", ".join(merged))
    if memory is not None and ratings:
        try:
            memory.save_stock_rationales(ratings)
            memory.record_rating_snapshots(ratings)  # §28: append-only history for the scorecard
        except Exception:
            pass
    return merged, True


def maybe_research_daytrade(
    settings: Any,
    memory: Any,
    research_fn: Callable[[str], str],
    *,
    n_picks: int = 8,
    research_days: int = DEFAULT_DAYTRADE_RESEARCH_DAYS,
    max_names: int = DEFAULT_DAYTRADE_MAX_NAMES,
    market_context_fn: Callable[[], str] | None = None,
    on_issue: Callable[[str], None] | None = None,
    performance: str = "",
    tradable: set | None = None,
    screen_fn: Callable[[list], set] | None = None,
    force: bool = False,
) -> tuple:
    """Scout short-term momentum / day-trade names (§27), cadence-gated. Unlike Special (buy-and-hold +
    accumulate), this REPLACES the pick list with the freshest momentum names each scout — a held name
    that is no longer a pick rotates off and is exited on the next rebalance (the take-profit/stop-loss
    exits live in `build_rebalance_plan`). Returns (daytrade_symbols, researched); persists picks to
    settings and theses to `stock_rationale` (action 'daytrade')."""
    current = normalize_roster(settings.get(DAYTRADE_SETTING, ""))
    now = datetime.now()
    last = settings.get(LAST_DAYTRADE_RESEARCH_SETTING, "")
    last_dt = None
    if last:
        try:
            last_dt = datetime.strptime(str(last)[:10], "%Y-%m-%d")
        except ValueError:
            last_dt = None
    if not force and last_dt is not None and (now - last_dt).days < research_days:
        return current, False

    market_context = market_context_fn() if market_context_fn else ""  # live news/price action (§25)
    raw = research_fn(build_daytrade_research_prompt(n_picks, performance=performance, market_context=market_context)) or ""
    picks = parse_special_research_json(raw)  # same {symbol, conviction, thesis} shape as the special scout
    if tradable:  # §36: keep only real, tradeable market tickers
        picks = [pick for pick in picks if pick.get("symbol") in tradable]
    if screen_fn and picks:  # §37: liquidity screen (momentum trades need to be easy to get in/out of)
        accepted = screen_fn([pick["symbol"] for pick in picks])
        picks = [pick for pick in picks if pick.get("symbol") in accepted]
    settings.set(LAST_DAYTRADE_RESEARCH_SETTING, now.strftime("%Y-%m-%d"))
    if not picks:
        if on_issue is not None:
            on_issue(_research_issue("Day-trade scout", raw))
        return current, False

    order = {"high": 0, "medium": 1, "low": 2}
    picks = sorted(picks, key=lambda pick: order.get(pick.get("conviction", "low"), 3))
    fresh: list[str] = []
    ratings: dict[str, Any] = {}
    for pick in picks:
        symbol = pick["symbol"]
        if not symbol:
            continue
        ratings[symbol] = {
            "action": "daytrade",
            "confidence": pick.get("conviction", ""),
            "rationale": pick.get("rationale", ""),
        }
        if symbol not in fresh and len(fresh) < max_names:
            fresh.append(symbol)
    settings.set(DAYTRADE_SETTING, ", ".join(fresh))
    if memory is not None and ratings:
        try:
            memory.save_stock_rationales(ratings)
            memory.record_rating_snapshots(ratings)  # §28: append-only history for the scorecard
        except Exception:
            pass
    return fresh, True
