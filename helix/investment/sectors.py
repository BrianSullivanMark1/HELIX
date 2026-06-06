from __future__ import annotations

"""Sector map for the sector-concentration cap (§35). Two layers:
  1. a curated ticker -> GICS-style sector table for common large/mid-caps (accurate, pure data), and
  2. **SEC enrichment** — for any name the curated map misses, pull its SIC code from SEC EDGAR
     submissions (free, keyless) and translate SIC -> sector, so the cap covers ~the whole universe
     automatically instead of just the hand-typed names. The curated map WINS where both exist (GICS is
     finer than SIC). Names still unresolved are exempt from the cap (best-effort, never mis-grouped).
"""

import json
from typing import Callable

from helix.investment.fundamentals import SEC_TICKERS_URL, parse_cik_map, sec_get

# sector -> tickers (inverted into SECTOR_MAP below). Confident large-cap classifications.
_SECTORS_BY_NAME: dict[str, tuple[str, ...]] = {
    "Information Technology": (
        "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "INTC", "CSCO", "ACN", "TXN",
        "QCOM", "IBM", "NOW", "INTU", "AMAT", "MU", "ADI", "LRCX", "KLAC", "SNPS", "CDNS", "ANET",
        "DELL", "HPQ", "HPE", "PANW", "CRWD", "FTNT",
    ),
    "Communication Services": (
        "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "T", "VZ", "TMUS", "CHTR", "EA", "TTWO",
        "OMC", "WBD",
    ),
    "Consumer Discretionary": (
        "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG", "TJX", "CMG", "ORLY", "MAR",
        "GM", "F", "ABNB", "HLT", "YUM", "ROST", "AZO", "LULU",
    ),
    "Consumer Staples": (
        "WMT", "PG", "KO", "PEP", "COST", "MDLZ", "PM", "MO", "CL", "KMB", "GIS", "KHC", "TGT",
        "SYY", "STZ", "KDP", "HSY", "K",
    ),
    "Health Care": (
        "UNH", "JNJ", "LLY", "ABBV", "MRK", "PFE", "TMO", "ABT", "DHR", "BMY", "AMGN", "CVS",
        "MDT", "ISRG", "GILD", "ELV", "CI", "VRTX", "REGN", "HUM", "A", "BDX", "BSX", "ZTS", "SYK",
    ),
    "Financials": (
        "BRK.B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "C", "SCHW", "BLK", "SPGI",
        "CB", "PGR", "USB", "PNC", "TFC", "COF", "ACGL", "MMC", "AON", "ICE", "CME", "MET", "AIG",
    ),
    "Industrials": (
        "GE", "CAT", "HON", "UNP", "BA", "RTX", "UPS", "LMT", "DE", "GD", "NOC", "ETN", "EMR",
        "CSX", "NSC", "FDX", "WM", "ITW", "MMM", "PH", "TDG", "GD",
    ),
    "Energy": (
        "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "OXY", "WMB", "KMI", "HES",
        "DVN", "BKR", "HAL",
    ),
    "Utilities": ("NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "XEL", "ED", "PEG", "WEC", "ES"),
    "Real Estate": ("PLD", "AMT", "EQIX", "CCI", "PSA", "O", "SPG", "WELL", "DLR", "VICI", "AVB"),
    "Materials": ("LIN", "APD", "SHW", "ECL", "FCX", "NEM", "DOW", "NUE", "CTVA", "VMC", "MLM"),
}

SECTORS: tuple[str, ...] = tuple(_SECTORS_BY_NAME)

# Inverted lookup: TICKER -> sector. First sector wins on the (rare) duplicate.
SECTOR_MAP: dict[str, str] = {}
for _sector, _tickers in _SECTORS_BY_NAME.items():
    for _ticker in _tickers:
        symbol = str(_ticker).strip().upper()
        if symbol and symbol not in SECTOR_MAP:
            SECTOR_MAP[symbol] = _sector


def sector_of(symbol: str) -> str | None:
    """The sector for a ticker, or None if not in the map (then it's exempt from the cap)."""
    return SECTOR_MAP.get(str(symbol).strip().upper())


def sectors_for(symbols: list) -> dict[str, str]:
    """{TICKER: sector} for the mapped names among `symbols` (unmapped omitted)."""
    out: dict[str, str] = {}
    for symbol in symbols or []:
        sector = sector_of(symbol)
        if sector:
            out[str(symbol).strip().upper()] = sector
    return out


# --------------------------------------------------------------------------- #
# SEC enrichment (§35): SIC code -> sector, fetched from SEC submissions for the
# names the curated map doesn't cover. SIC is coarser than GICS, so this is a
# best-effort coverage layer; the curated map takes precedence where both exist.
# --------------------------------------------------------------------------- #

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# Specific SIC codes whose plain range would mis-classify them (the big exceptions).
_SIC_OVERRIDES: dict[int, str] = {
    2834: "Health Care", 2835: "Health Care", 2836: "Health Care", 8731: "Health Care",  # pharma/biotech
    2844: "Consumer Staples",                                                              # cosmetics/toiletries
    2911: "Energy", 1311: "Energy",                                                        # refining / crude
    3571: "Information Technology", 3572: "Information Technology", 3576: "Information Technology",
    3661: "Information Technology", 3663: "Information Technology", 3669: "Information Technology",
    3674: "Information Technology",                                                         # semiconductors
    7370: "Information Technology", 7371: "Information Technology", 7372: "Information Technology",
    7373: "Information Technology", 7374: "Information Technology", 7389: "Information Technology",
    3711: "Consumer Discretionary", 3713: "Consumer Discretionary", 3714: "Consumer Discretionary",
    3861: "Consumer Discretionary",                                                        # photographic
    4812: "Communication Services", 4813: "Communication Services", 4899: "Communication Services",
    5912: "Consumer Staples",                                                              # drug stores
    6798: "Real Estate",                                                                   # REITs
}

# Broad SIC ranges -> sector (checked in order after the overrides).
_SIC_RANGES: tuple[tuple[int, int, str], ...] = (
    (100, 1199, "Materials"), (1200, 1399, "Energy"), (1400, 1499, "Materials"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"), (2200, 2599, "Consumer Discretionary"),
    (2600, 2699, "Materials"), (2700, 2799, "Communication Services"), (2800, 2999, "Materials"),
    (3000, 3399, "Materials"), (3400, 3599, "Industrials"),
    (3600, 3699, "Information Technology"), (3700, 3799, "Industrials"),
    (3800, 3899, "Health Care"), (3900, 3999, "Consumer Discretionary"),
    (4000, 4599, "Industrials"), (4600, 4699, "Energy"), (4700, 4799, "Industrials"),
    (4800, 4899, "Communication Services"), (4900, 4999, "Utilities"),
    (5000, 5399, "Consumer Discretionary"), (5400, 5499, "Consumer Staples"),
    (5500, 5999, "Consumer Discretionary"),
    (6000, 6499, "Financials"), (6500, 6599, "Real Estate"), (6600, 6999, "Financials"),
    (7000, 7299, "Consumer Discretionary"), (7300, 7799, "Industrials"),
    (7800, 7899, "Communication Services"), (7900, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"), (8100, 8999, "Industrials"),
)


def sic_to_sector(sic: object) -> str | None:
    """Translate an SEC SIC code to a GICS-style sector (§35). Coarse but sensible — SIC predates
    modern sectors, so this is a best-effort bucket for the concentration cap, not a precise label.
    Returns None for codes we don't classify (then the name is exempt). Pure."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return None
    if code in _SIC_OVERRIDES:
        return _SIC_OVERRIDES[code]
    for low, high, sector in _SIC_RANGES:
        if low <= code <= high:
            return sector
    return None


def fetch_sectors(
    symbols: list,
    get_fn: Callable[[str], bytes] = sec_get,
    *,
    cik_map: dict[str, int] | None = None,
) -> dict[str, str]:
    """Fetch SIC -> sector from SEC submissions for `symbols` (§35). One small request per name (SIC is
    on the submissions header), so the caller should pass only the UNMAPPED tail and cache the result
    (SIC is ~static). Best-effort — a name with no CIK / no classifiable SIC is simply skipped.
    `get_fn` is injectable for tests; `cik_map` can be supplied to skip the ticker-map fetch."""
    wanted = [str(s).strip().upper() for s in (symbols or []) if str(s).strip()]
    if not wanted:
        return {}

    def safe_get(url: str) -> bytes:
        try:
            return get_fn(url)
        except Exception:
            return b"{}"

    if cik_map is None:
        cik_map = parse_cik_map(safe_get(SEC_TICKERS_URL))
    out: dict[str, str] = {}
    for symbol in wanted:
        cik = cik_map.get(symbol)
        if not cik:
            continue
        try:
            data = json.loads(safe_get(SEC_SUBMISSIONS_URL.format(cik=int(cik))))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        sector = sic_to_sector(data.get("sic")) if isinstance(data, dict) else None
        if sector:
            out[symbol] = sector
    return out
