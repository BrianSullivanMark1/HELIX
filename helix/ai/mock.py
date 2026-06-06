from __future__ import annotations

import json
from typing import Any


def generate_mock_research(
    stream_name: str,
    focus: str,
    watchlist: list[dict[str, Any]],
) -> str:
    subject = focus.strip().upper() if focus.strip() else _default_subject(stream_name, watchlist)
    watchlist_context = _watchlist_context(watchlist)
    return "\n".join(
        [
            "[MOCK CLAUDE RESEARCH]",
            "",
            f"1. Subject: {subject}",
            f"2. What it is: Simulated research memo for the {stream_name} stream.",
            f"3. Why it matters: HELIX needs repeatable research output before it can automate portfolio decisions.",
            f"4. Bull case: {subject} may fit if it supports the user's long-term growth plan and does not overconcentrate risk.",
            f"5. Bear case: The idea may be too narrow, overpriced, poorly timed, or redundant with existing exposure.",
            "6. Key risks: market drawdown, concentration, bad data, overtrading, and acting on unverified assumptions.",
            f"7. Portfolio fit: Review against cash reserve, allocation limits, and current watchlist context. {watchlist_context}",
            "8. What data should be verified next: live price, expense ratio if ETF, revenue/earnings if company, recent news, and position sizing.",
            "9. Suggested action: research more",
            "10. Confidence: low",
            "",
            "Developer note: This is mock output for building HELIX workflows. It is not live AI research or financial advice.",
        ]
    )


def generate_mock_portfolio_research(
    watchlist: list[dict[str, Any]],
    preset: str,
) -> str:
    """Offline stand-in for the structured portfolio prompt.

    Returns the same JSON shape the real model is asked for, so the whole
    research -> parse -> size pipeline can be exercised with no API spend.
    """
    if not watchlist:
        return "[]"

    confidences = ["high", "medium", "low"]
    records: list[dict[str, Any]] = []
    for index, item in enumerate(watchlist):
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        confidence = confidences[index % len(confidences)]
        action = "buy" if confidence in ("high", "medium") else "watch"
        thesis = str(item.get("thesis", "")).strip() or "no thesis recorded"
        records.append(
            {
                "symbol": symbol,
                "action": action,
                "confidence": confidence,
                "rationale": f"[mock/{preset}] {thesis}",
            }
        )
    return json.dumps(records)


def generate_mock_roster_review(
    roster: list[str],
    holdings: Any = None,
    n_candidates: int = 30,
) -> str:
    """Offline stand-in for the roster-review prompt (HELIX 100).

    Returns the same {incumbents, candidates} JSON shape, with incumbent scores spread out and a
    pool of real (not-in-roster) candidate tickers scored high enough to trigger sample swaps.
    """
    roster_set = {str(s).strip().upper() for s in (roster or [])}
    incumbents: list[dict[str, Any]] = []
    for index, symbol in enumerate(roster or []):
        symbol = str(symbol).strip().upper()
        if not symbol:
            continue
        score = 35 + (index * 13) % 60  # spread 35-95 deterministically
        incumbents.append({"symbol": symbol, "score": score, "rationale": f"[mock] incumbent {symbol}"})

    pool = ["PANW", "SNOW", "UBER", "SHOP", "COIN", "ABNB", "DDOG", "NET", "CRWD", "MELI", "PLTR", "ANET"]
    candidates: list[dict[str, Any]] = []
    for index, symbol in enumerate([s for s in pool if s not in roster_set][: max(0, int(n_candidates))]):
        candidates.append({"symbol": symbol, "score": 88 - index, "rationale": f"[mock] candidate {symbol}"})

    return json.dumps({"incumbents": incumbents, "candidates": candidates})


def generate_mock_special_research(n_picks: int = 8) -> str:
    """Offline stand-in for the Special Stocks scout (§21) — real speculative tickers, no API spend."""
    pool = ["IONQ", "RKLB", "ASTS", "OKLO", "SOFI", "RIVN", "TEM", "RXRX", "SMR", "CELH", "DNA", "PATH"]
    convictions = ["high", "medium", "low"]
    out: list[dict[str, Any]] = []
    for index, symbol in enumerate(pool[: max(0, int(n_picks))]):
        out.append(
            {
                "symbol": symbol,
                "conviction": convictions[index % len(convictions)],
                "thesis": f"[mock] speculative asymmetric upside in {symbol}",
            }
        )
    return json.dumps(out)


def generate_mock_home_suggestions(tasks: list[str] | None = None) -> str:
    """Offline stand-in for the home time/money optimizer — no API spend."""
    items = [
        {"title": "Auto-deliver pantry & baby staples", "saves": "both", "effort": "low",
         "detail": "Set up Subscribe & Save / auto-ship for diapers, wipes, paper goods and pantry basics so they arrive on a schedule - removes a weekly errand and is usually 5-15% cheaper."},
        {"title": "Batch-cook + meal-kit hybrid", "saves": "time", "effort": "medium",
         "detail": "Cook 2-3 big-batch proteins on Sunday and use a meal kit only for variety nights; cuts daily cooking to assembly and trims takeout spend."},
        {"title": "Robot vacuum on a daily schedule", "saves": "time", "effort": "low",
         "detail": "Schedule a robot vac daily and reserve the monthly deep shampoo for carpets - removes most of the vacuuming chore."},
        {"title": "Everything on autopay + one review day", "saves": "both", "effort": "low",
         "detail": "Put home and business bills on autopay with a single monthly calendar reminder to review - avoids late fees and the manual paying."},
        {"title": "Standing grocery cart you reorder in one tap", "saves": "time", "effort": "low",
         "detail": "Build a saved cart of weekly staples at one store; each week just edit the few deltas instead of rebuilding the list."},
    ]
    return json.dumps(items)


def _default_subject(stream_name: str, watchlist: list[dict[str, Any]]) -> str:
    if watchlist and stream_name == "Stock Selection":
        return str(watchlist[0].get("symbol", "WATCHLIST")).upper()
    if stream_name == "World News":
        return "MACRO CONDITIONS"
    if stream_name == "Company Research":
        return "COMPANY UNDER REVIEW"
    return stream_name.upper()


def _watchlist_context(watchlist: list[dict[str, Any]]) -> str:
    if not watchlist:
        return "No watchlist tickers exist yet."

    tickers = ", ".join(str(item.get("symbol", "")).upper() for item in watchlist)
    return f"Current watchlist tickers: {tickers}."
