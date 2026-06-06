from __future__ import annotations

import statistics
from typing import Any

# Turns raw Alpaca bars + news into a compact, prompt-ready "live market context" digest so the AI
# research reasons from current reality (price action + headlines) instead of only training memory.
# Pure functions (no I/O, no Qt) so they're easy to unit-test.


def _closes(bars: Any) -> list[float]:
    out: list[float] = []
    for bar in bars or []:
        try:
            close = float(bar.get("c"))
        except (TypeError, ValueError, AttributeError):
            continue
        if close > 0:
            out.append(close)
    return out


def _pct(now: float, then: float) -> float | None:
    if then and then > 0:
        return (now / then - 1.0) * 100.0
    return None


def technical_line(bars: Any) -> str:
    """One-line technical read from OHLC bars (oldest-first): price, distance from the window
    high/low, ~1mo and ~3mo momentum, and trend vs a long moving average. '' if not enough data.

    Assumes weekly bars over ~1 year (the default fetch), so 4 bars ≈ 1 month, 13 ≈ 3 months,
    and a 40-bar average ≈ the 200-day trend line."""
    closes = _closes(bars)
    if len(closes) < 3:
        return ""
    last = closes[-1]
    high, low = max(closes), min(closes)
    parts = [f"${last:,.2f}"]
    from_high = _pct(last, high)
    from_low = _pct(last, low)
    if from_high is not None:
        parts.append(f"{from_high:+.0f}% vs 1y-high")
    if from_low is not None:
        parts.append(f"{from_low:+.0f}% vs 1y-low")
    for label, window in (("1mo", 4), ("3mo", 13)):
        if len(closes) > window:
            change = _pct(last, closes[-1 - window])
            if change is not None:
                parts.append(f"{label} {change:+.0f}%")
    window = min(len(closes), 40)
    moving_avg = sum(closes[-window:]) / window
    parts.append("above trend" if last >= moving_avg else "below trend")
    return ", ".join(parts)


def technicals_by_symbol(bars_by_symbol: dict) -> dict[str, str]:
    """{SYMBOL: technical_line} for every symbol that has enough bars."""
    out: dict[str, str] = {}
    for symbol, bars in (bars_by_symbol or {}).items():
        line = technical_line(bars)
        if line:
            out[str(symbol).upper()] = line
    return out


def news_by_symbol(articles: Any, universe: Any) -> dict[str, list[str]]:
    """Map recent headlines onto the universe symbols they tag: {SYMBOL: ["date: headline", …]}."""
    wanted = {str(s).upper() for s in (universe or [])}
    out: dict[str, list[str]] = {}
    for article in articles or []:
        headline = str(article.get("headline", "")).strip()
        if not headline:
            continue
        when = str(article.get("created_at", ""))[:10]
        for symbol in article.get("symbols") or []:
            upper = str(symbol).upper()
            if upper in wanted:
                out.setdefault(upper, []).append(f"{when}: {headline}")
    return out


def factor_signals(closes_by_symbol: dict, *, min_bars: int = 8) -> dict[str, float]:
    """A deterministic momentum/trend **factor score** per symbol (higher = stronger), used to rank
    the top-N concentration cut (§30) — i.e. *which* same-conviction names make the cut when capital
    is concentrated. Pure, scale-free-ish: blends back-half momentum (last vs the series midpoint)
    with the distance above the window mean. Frequency-agnostic, so it works on weekly bars (the live
    path) or daily closes (the backtest), and accepts either Alpaca bar dicts ({"c": …}) or raw floats.

    Only the *ranking* matters, not the absolute value; symbols with too little history are omitted
    (the caller falls back to a deterministic tiebreak)."""
    out: dict[str, float] = {}
    for symbol, items in (closes_by_symbol or {}).items():
        series: list[float] = []
        for item in items or []:
            if isinstance(item, dict):
                try:
                    value = float(item.get("c"))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
            if value > 0:
                series.append(value)
        if len(series) < min_bars:
            continue
        last = series[-1]
        mid = series[len(series) // 2]
        momentum = (last / mid - 1.0) if mid > 0 else 0.0
        mean = sum(series) / len(series)
        trend = (last / mean - 1.0) if mean > 0 else 0.0
        out[str(symbol).upper()] = round(0.6 * momentum + 0.4 * trend, 6)
    return out


def volatility_signals(closes_by_symbol: dict, *, min_bars: int = 8) -> dict[str, float]:
    """Per-symbol volatility = standard deviation of bar-to-bar returns (§31), for volatility-adjusted
    sizing. Pure; same flexible input as `factor_signals` (Alpaca bar dicts or raw floats). Only the
    *relative* level matters (the engine tilts off the median), so the bar frequency need not be
    annualized. Symbols with too little history or a flat (zero-variance) series are omitted, so the
    engine falls back to no tilt for them."""
    out: dict[str, float] = {}
    for symbol, items in (closes_by_symbol or {}).items():
        series: list[float] = []
        for item in items or []:
            if isinstance(item, dict):
                try:
                    value = float(item.get("c"))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    value = float(item)
                except (TypeError, ValueError):
                    continue
            if value > 0:
                series.append(value)
        if len(series) < min_bars:
            continue
        returns = [series[i] / series[i - 1] - 1.0 for i in range(1, len(series)) if series[i - 1] > 0]
        if len(returns) < 2:
            continue
        vol = statistics.pstdev(returns)
        if vol > 0:
            out[str(symbol).upper()] = round(vol, 6)
    return out


def liquidity_metrics(bars: Any) -> tuple[float, float]:
    """(last_price, avg_daily_dollar_volume) from daily OHLCV bars, for the quality/liquidity screen
    (§37). Dollar volume = mean of close × volume across the bars. (0.0, 0.0) if no usable bars.
    Pure; accepts Alpaca bar dicts ({c, v})."""
    closes: list[float] = []
    dollar: list[float] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        try:
            close = float(bar.get("c"))
            volume = float(bar.get("v"))
        except (TypeError, ValueError):
            continue
        if close > 0 and volume >= 0:
            closes.append(close)
            dollar.append(close * volume)
    if not closes:
        return (0.0, 0.0)
    return (closes[-1], sum(dollar) / len(dollar))


def regime_risk_off(closes: Any, *, window: int = 40) -> bool:
    """Simple market-regime filter (§35): True when the index (SPY) is **risk-off** — its latest close
    is below a long moving average (a trend filter; ~40 weekly bars ≈ the 200-day line). Accepts Alpaca
    bar dicts or raw floats. Returns False (risk-ON) on thin data, so HELIX never goes defensive just
    because history is short. Pure."""
    series: list[float] = []
    for item in closes or []:
        if isinstance(item, dict):
            try:
                value = float(item.get("c"))
            except (TypeError, ValueError):
                continue
        else:
            try:
                value = float(item)
            except (TypeError, ValueError):
                continue
        if value > 0:
            series.append(value)
    if len(series) < max(5, window // 2):
        return False
    used = min(len(series), window)
    moving_avg = sum(series[-used:]) / used
    return series[-1] < moving_avg


def build_market_context(
    bars_by_symbol: dict,
    articles: list,
    universe: list,
    *,
    max_headlines: int = 18,
    fundamentals_text: str = "",
) -> str:
    """Assemble the live-market digest fed into the rating prompt: per-name price reads (with the
    latest relevant headline inline) + a short market-wide news list for macro context, plus an
    optional **fundamentals** section (§32: revenue growth, margins, ROE from SEC filings) so the
    model weighs the numbers, not just price/news. `fundamentals_text` is pre-rendered by the caller
    (keeps this module dependency-free)."""
    technicals = technicals_by_symbol(bars_by_symbol)
    news = news_by_symbol(articles, universe)
    lines: list[str] = []

    if fundamentals_text.strip():
        lines.append(fundamentals_text.strip())
        lines.append("")

    if technicals:
        lines.append("Per-name price action (recent) and any fresh headline:")
        for symbol in sorted(technicals):
            tail = f"  | news: {news[symbol][0]}" if news.get(symbol) else ""
            lines.append(f"- {symbol}: {technicals[symbol]}{tail}")

    headlines: list[str] = []
    for article in (articles or [])[:max_headlines]:
        headline = str(article.get("headline", "")).strip()
        if not headline:
            continue
        when = str(article.get("created_at", ""))[:10]
        tickers = ",".join(str(s).upper() for s in (article.get("symbols") or [])[:4])
        headlines.append(f"- {when} [{tickers}] {headline}")
    if headlines:
        lines.append("")
        lines.append(f"Latest market headlines ({len(headlines)}):")
        lines.extend(headlines)

    return "\n".join(lines).strip()
