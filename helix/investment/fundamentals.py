from __future__ import annotations

"""Fundamentals input (§32) — the biggest research data gap (§10/§25): until now the rating reasoned
from price/news + training memory, i.e. vibes + momentum. This adds REAL fundamentals — revenue growth,
margins, returns on equity, leverage — so the model can ask "do the numbers support the story?".

Source: **SEC EDGAR** — official, free, **keyless** (just a descriptive User-Agent), urllib-friendly,
fully in keeping with the stdlib-first / local-first ethos. We use the **XBRL frames API**
(`/api/xbrl/frames/us-gaap/{concept}/{unit}/CY{year}.json`), which returns one financial concept across
*all* filers in a single request — so the whole ~480-name universe is covered in ~15 bulk requests
(~15 MB) instead of a 3.6 MB-per-company `companyfacts` download (~700 MB for the universe).

Design: pure parsers/metric/line helpers (unit-tested with synthetic frames) + a thin fetch layer that
takes an injected `get_fn(url) -> bytes` (so tests never hit the network). Best-effort throughout — a
name SEC doesn't cover is simply omitted, exactly like the news path. Cached monthly (§ memory) so the
weekly re-rate reads locally and the SEC fetch is rare.
"""

import json
from datetime import datetime
from typing import Any, Callable

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_FRAME_URL = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{period}.json"
# SEC's fair-access policy requires a descriptive User-Agent in the plain "Name email" form they
# document — a fancier UA (extra punctuation / a URL) gets a 403 Forbidden.
SEC_USER_AGENT = "HELIX personal-research helix-user@example.com"

# Revenue is reported under several us-gaap tags depending on the filer/era; try in this order.
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)


# --------------------------------------------------------------------------- #
# Pure parsing + metrics (no I/O — unit-testable with synthetic frame JSON).
# --------------------------------------------------------------------------- #


def parse_cik_map(raw: Any) -> dict[str, int]:
    """SEC company_tickers.json -> {TICKER: cik}. Accepts bytes/str/parsed; first CIK per ticker wins."""
    data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    rows = data.values() if isinstance(data, dict) else (data or [])
    out: dict[str, int] = {}
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        ticker = str(entry.get("ticker", "")).strip().upper()
        try:
            cik = int(entry.get("cik_str"))
        except (TypeError, ValueError):
            continue
        if ticker and ticker not in out:
            out[ticker] = cik
    return out


def parse_frame(raw: Any) -> dict[int, float]:
    """SEC XBRL frame JSON -> {cik: value}. One value per company (the frame is already one period)."""
    data = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    if not isinstance(data, dict):
        return {}
    out: dict[int, float] = {}
    for row in data.get("data") or []:
        if not isinstance(row, dict):
            continue
        try:
            out[int(row["cik"])] = float(row["val"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def default_years(now: datetime | None = None) -> list[int]:
    """The three most recent completed calendar years to pull frames for (latest + two priors, so
    per-company latest-available + YoY growth both work). E.g. mid-2026 -> [2025, 2024, 2023]."""
    year = (now or datetime.now()).year
    return [year - 1, year - 2, year - 3]


def extract_metrics(cik: int, frames: dict[tuple[str, str], dict[int, float]], years: list[int]) -> dict[str, Any] | None:
    """Compute one company's fundamentals from the pre-parsed frames (§32). PURE.

    `frames` is {(concept, "CY<year>"|"CY<year>Q4I"): {cik: val}}. Picks the company's latest year
    with a revenue figure, then derives YoY growth, net/gross margins, ROE and a debt ratio from the
    same-period concepts. Returns None if not even revenue or net income is available.
    """
    desc_years = sorted(years, reverse=True)

    def flow(concept: str, year: int) -> float | None:
        return frames.get((concept, f"CY{year}"), {}).get(cik)

    def revenue_for(year: int) -> tuple[float, str] | None:
        for concept in REVENUE_CONCEPTS:
            value = flow(concept, year)
            if value is not None:
                return value, concept
        return None

    revenue = revenue_year = revenue_concept = None
    for year in desc_years:
        found = revenue_for(year)
        if found:
            revenue, revenue_concept = found
            revenue_year = year
            break

    net_income = None
    income_year = revenue_year
    if income_year is None:  # no revenue tag — still try net income on the latest year
        for year in desc_years:
            if flow("NetIncomeLoss", year) is not None:
                income_year = year
                break
    if income_year is not None:
        net_income = flow("NetIncomeLoss", income_year)
    if revenue is None and net_income is None:
        return None

    metrics: dict[str, Any] = {"fiscal_year": revenue_year or income_year}
    if revenue is not None:
        metrics["revenue"] = revenue
        prior = revenue_for(revenue_year - 1) if revenue_year else None
        if prior and prior[1] == revenue_concept and prior[0] > 0:
            metrics["revenue_growth"] = revenue / prior[0] - 1.0
    if net_income is not None:
        metrics["net_income"] = net_income
        if revenue and revenue > 0:
            metrics["net_margin"] = net_income / revenue
    if revenue_year is not None:
        gross = flow("GrossProfit", revenue_year)
        if gross is not None and revenue and revenue > 0:
            metrics["gross_margin"] = gross / revenue
        equity = frames.get(("StockholdersEquity", f"CY{revenue_year}Q4I"), {}).get(cik)
        if equity and equity > 0:
            metrics["equity"] = equity
            if net_income is not None:
                metrics["roe"] = net_income / equity
            liabilities = frames.get(("Liabilities", f"CY{revenue_year}Q4I"), {}).get(cik)
            if liabilities is not None:
                metrics["debt_to_equity"] = liabilities / equity
    return metrics


def _abbrev_usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    n = abs(value)
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
        if n >= scale:
            return f"{sign}${n / scale:.1f}{unit}"
    return f"{sign}${n:,.0f}"


def fundamentals_line(metrics: dict[str, Any]) -> str:
    """A compact one-line fundamentals read for the rating prompt, e.g.
    'FY2025 rev $416.2B (+6% YoY), net margin 27%, gross margin 47%, ROE 152%, D/E 3.9'."""
    if not metrics:
        return ""
    parts: list[str] = []
    fy = metrics.get("fiscal_year")
    if metrics.get("revenue") is not None:
        head = f"FY{fy} rev {_abbrev_usd(metrics['revenue'])}" if fy else f"rev {_abbrev_usd(metrics['revenue'])}"
        if metrics.get("revenue_growth") is not None:
            head += f" ({metrics['revenue_growth'] * 100:+.0f}% YoY)"
        parts.append(head)
    if metrics.get("net_margin") is not None:
        parts.append(f"net margin {metrics['net_margin'] * 100:.0f}%")
    if metrics.get("gross_margin") is not None:
        parts.append(f"gross margin {metrics['gross_margin'] * 100:.0f}%")
    if metrics.get("roe") is not None:
        parts.append(f"ROE {metrics['roe'] * 100:.0f}%")
    if metrics.get("debt_to_equity") is not None:
        parts.append(f"D/E {metrics['debt_to_equity']:.1f}")
    return ", ".join(parts)


def fundamental_score(metrics: dict[str, Any]) -> float | None:
    """A deterministic 0-1 quality/growth composite from the fundamentals (§32). Rewards revenue
    growth, net margin and ROE; lightly penalizes heavy leverage. Returns None if too little data.
    Squashed so outliers (e.g. ROE 150%) don't dominate. A coarse signal for ranking/overlay use."""
    growth = metrics.get("revenue_growth")
    margin = metrics.get("net_margin")
    roe = metrics.get("roe")
    leverage = metrics.get("debt_to_equity")
    if growth is None and margin is None and roe is None:
        return None

    def clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    score = parts = 0.0
    if growth is not None:
        score += clamp01((growth + 0.10) / 0.40)  # -10%..+30% growth -> 0..1
        parts += 1
    if margin is not None:
        score += clamp01(margin / 0.30)  # 0..30% net margin -> 0..1
        parts += 1
    if roe is not None:
        score += clamp01(roe / 0.40)  # 0..40% ROE -> 0..1 (caps the AAPL-style outliers)
        parts += 1
    if parts == 0:
        return None
    composite = score / parts
    if leverage is not None and leverage > 3.0:  # mild penalty for very heavy leverage
        composite *= 0.9
    return round(composite, 4)


def fundamentals_block(fundamentals: dict[str, dict], symbols: list | None = None) -> str:
    """Render the per-name fundamentals section for the prompt digest (§32). Limits to `symbols` when
    given (the chunk being rated). '' if nothing to show."""
    wanted = {str(s).strip().upper() for s in symbols} if symbols else None
    lines: list[str] = []
    for symbol in sorted(fundamentals):
        if wanted is not None and symbol not in wanted:
            continue
        line = fundamentals_line(fundamentals[symbol])
        if line:
            lines.append(f"- {symbol}: {line}")
    if not lines:
        return ""
    return "Fundamentals (latest annual SEC filings):\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fetch layer (the I/O edge) — `get_fn(url) -> bytes` is injected so tests stub it.
# --------------------------------------------------------------------------- #


def sec_get(url: str, timeout: int = 30) -> bytes:
    """Fetch a SEC URL with the required User-Agent, transparently handling gzip/deflate. stdlib-only."""
    import gzip
    import urllib.request
    import zlib

    request = urllib.request.Request(
        url, headers={"User-Agent": SEC_USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        encoding = response.headers.get("Content-Encoding", "")
    if encoding == "gzip":
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    return raw


def fetch_fundamentals(
    symbols: list,
    get_fn: Callable[[str], bytes] = sec_get,
    *,
    years: list[int] | None = None,
    cik_map: dict[str, int] | None = None,
) -> dict[str, dict]:
    """Fetch fundamentals for `symbols` from SEC frames (§32). Bulk: one request per (concept, year)
    covers the whole universe. Best-effort — a failed frame or an uncovered name is simply skipped.
    `get_fn` is injectable for tests; `cik_map` can be supplied to skip the ticker-map fetch.
    """
    wanted_tickers = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not wanted_tickers:
        return {}
    years = years or default_years()

    def safe_get(url: str) -> bytes:
        try:
            return get_fn(url)
        except Exception:
            return b"{}"

    if cik_map is None:
        cik_map = parse_cik_map(safe_get(SEC_TICKERS_URL))
    wanted = {ticker: cik_map[ticker] for ticker in wanted_tickers if ticker in cik_map}
    if not wanted:
        return {}
    target_ciks = set(wanted.values())

    frames: dict[tuple[str, str], dict[int, float]] = {}

    def load(concept: str, period: str) -> None:
        url = SEC_FRAME_URL.format(concept=concept, unit="USD", period=period)
        parsed = parse_frame(safe_get(url))
        # keep only the universe's CIKs to bound memory (frames carry thousands of filers)
        frames[(concept, period)] = {cik: value for cik, value in parsed.items() if cik in target_ciks}

    for year in years:
        for concept in REVENUE_CONCEPTS:
            load(concept, f"CY{year}")
        load("NetIncomeLoss", f"CY{year}")
        load("GrossProfit", f"CY{year}")
        load("StockholdersEquity", f"CY{year}Q4I")
        load("Liabilities", f"CY{year}Q4I")

    out: dict[str, dict] = {}
    for ticker, cik in wanted.items():
        metrics = extract_metrics(cik, frames, years)
        if metrics:
            out[ticker] = metrics
    return out
