from __future__ import annotations

import json
from typing import Any


def build_research_prompt(
    stream_name: str,
    focus: str,
    watchlist: list[dict[str, Any]],
) -> str:
    watchlist_text = _format_watchlist(watchlist)
    return f"""
You are HELIX's investment research analyst.

Research stream:
{stream_name}

User focus:
{focus or "No specific ticker or question provided."}

Current HELIX watchlist:
{watchlist_text}

Task:
Produce a concise investment research memo that helps decide whether this belongs in
the user's portfolio process. Do not hype the idea. Surface uncertainty clearly.

Required format:
1. Subject
2. What it is
3. Why it matters
4. Bull case
5. Bear case
6. Key risks
7. Portfolio fit
8. What data should be verified next
9. Suggested action: ignore / watch / research more / consider
10. Confidence: low / medium / high

Rules:
- This is research support, not financial advice.
- If current market prices, breaking news, or earnings data are needed, say exactly
  what should be verified with live data.
- Prefer durable reasoning over short-term price prediction.
""".strip()


def build_portfolio_research_prompt(
    deployable_cash: float,
    watchlist: list[dict[str, Any]],
    preset: str,
    performance: str = "",
    market_context: str = "",
) -> str:
    """Ask the model to rate every watchlist ticker as machine-readable JSON.

    HELIX, not the model, decides position size — the model only chooses an action
    and a confidence so the output is structured and easy to act on. `market_context` (live price
    action + news from Alpaca, §25) grounds the decision in current reality.
    """
    watchlist_text = _format_watchlist(watchlist)
    track_record = (
        f"\nYour realized track record so far: {performance}.\n"
        "Learn from it: favor the kinds of names that have worked and be warier of those that have not.\n"
        if performance
        else ""
    )
    market_block = (
        "\nLIVE MARKET DATA (current, from the broker — weigh this HEAVILY; it reflects reality now "
        "and overrides any stale assumptions from your training data):\n"
        f"{market_context}\n"
        if market_context
        else ""
    )
    data_caveat = (
        "Use the LIVE MARKET DATA above as your source of truth for prices, momentum, and recent news."
        if market_context
        else "You reason from training knowledge, not live data; note where live prices/earnings/news should be verified."
    )
    return f"""
You are HELIX's investment engine. Your objective is to GROW the account over the long run -
months to years - by choosing the names with the best risk-adjusted return potential over that
horizon. Favor durable, high-quality compounders (strong economics, real moats, healthy balance
sheets). Do not chase short-term noise, fads, or hype.

Investing posture: {preset}
Cash available to deploy: ${deployable_cash:,.2f}
{track_record}{market_block}
Watchlist to evaluate:
{watchlist_text}

For EACH symbol, decide an action and your confidence with that long-term, grow-the-account goal in mind.
Return ONLY a JSON array. No prose, no markdown code fences. Each element must be exactly:
{{"symbol": "TICKER", "action": "buy|watch|skip", "confidence": "low|medium|high", "rationale": "one short sentence"}}

Rules:
- "buy" = strong long-term, risk-adjusted upside worth owning now. "watch" = good business, wrong
  time or price. "skip" = weak prospects, or too risky/overvalued for the long run.
- A {preset} posture {_posture_hint(preset)}.
- Be honest - no hype. Flag uncertainty. {data_caveat} This is research support, not financial advice.
- Do NOT choose position sizes or dollar amounts; HELIX handles sizing.
""".strip()


def parse_research_json(raw: str) -> list[dict[str, Any]]:
    """Defensively pull a JSON array of rating records out of a model response."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "[" in part and "]" in part]
        if candidates:
            text = max(candidates, key=len)

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []

    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    records: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        action = str(item.get("action", "watch")).strip().lower()
        if action not in {"buy", "watch", "skip"}:
            action = "watch"
        confidence = str(item.get("confidence", "low")).strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "low"
        rationale = str(item.get("rationale", "")).strip()
        records.append(
            {
                "symbol": symbol,
                "action": action,
                "confidence": confidence,
                "rationale": rationale,
            }
        )
    return records


def build_adversarial_prompt(symbol: str, market_context: str = "", performance: str = "") -> str:
    """Adversarial 'bull vs bear vs judge' stress-test for ONE buy candidate (§34).

    The engine currently rates this name a BUY; force the model to argue the strongest case FOR, then
    the strongest case AGAINST (actively trying to refute the buy), then rule on it as an impartial,
    demanding referee. A buy must SURVIVE the bear case to stay a buy — this is the discipline that
    catches plausible-but-fragile picks the single-pass rating waves through.
    """
    market_block = (
        "\nLIVE DATA for this name (price action, news, and SEC fundamentals — weigh it HEAVILY):\n"
        f"{market_context}\n"
        if market_context
        else ""
    )
    track_block = f"\nHELIX's realized track record so far: {performance}\n" if performance else ""
    return f"""
You are HELIX's adversarial analyst and devil's advocate. The engine currently rates {symbol} a BUY
for a long-term, grow-the-account position. Your job is to pressure-test that call before HELIX commits
real capital — do NOT just agree.
{track_block}{market_block}
Do THREE things, in order:
1. BULL: the strongest, most specific case to BUY {symbol} now (durable advantages, why it compounds).
2. BEAR: the strongest case to NOT buy — actively try to REFUTE the buy (valuation, competition,
   deteriorating fundamentals, the story being priced in already, key risks). Steelman the skeptic.
3. VERDICT: as an impartial referee, decide. Reserve a downgrade for a genuinely FRAGILE pick —
   "watch" if the thesis is sound but a specific, serious near-term risk warrants waiting; "skip" if
   the bear case shows the thesis is broken, unproven, or the valuation is so stretched it impairs
   MULTI-YEAR returns. Crucially: do NOT downgrade a high-quality compounder merely because it has
   risen recently or looks fully valued in the short term — durable winners stay expensive, and this
   is a long-term, grow-the-account book, not a market-timing one. Keep "buy" when the long-term case
   still holds despite the bear; only kill a buy whose thesis or risk genuinely fails the test.

Return ONLY a JSON object - no prose, no markdown fences - exactly:
{{"bull": "1-2 sentences", "bear": "1-2 sentences", "verdict": "buy|watch|skip",
  "confidence": "low|medium|high", "rationale": "one sentence on the deciding factor"}}

Rules:
- Be honest and skeptical; the point is to catch fragile buys, not to confirm them. No hype.
- Base it on the live data above where given; flag where current prices/news/earnings should be checked.
- This is research support, not financial advice.
""".strip()


def parse_adversarial_json(raw: str) -> dict[str, Any]:
    """Pull {bull, bear, verdict, confidence, rationale} from the adversarial response (§34).

    Returns {} if nothing usable parses (caller then keeps the original rating). `verdict` is coerced
    to buy/watch/skip and `confidence` to low/medium/high.
    """
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "{" in part and "}" in part]
        if candidates:
            text = max(candidates, key=len)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in {"buy", "watch", "skip"}:
        return {}
    confidence = str(data.get("confidence", "low")).strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    return {
        "bull": str(data.get("bull", "")).strip(),
        "bear": str(data.get("bear", "")).strip(),
        "verdict": verdict,
        "confidence": confidence,
        "rationale": str(data.get("rationale", "")).strip(),
    }


def build_roster_score_prompt(symbols: list, performance: str = "", market_context: str = "") -> str:
    """Score a CHUNK of roster names 0-100 on long-term, grow-the-account potential (§20, chunked path
    so the full HELIX 500 can be scored without truncating). Returns the {incumbents,candidates} shape
    so `parse_roster_review_json` reads it (candidates empty here — this call only scores incumbents)."""
    names = ", ".join(symbols) if symbols else "(none)"
    track = f"\nYour realized track record so far: {performance}. Favor the kinds of names that have worked.\n" if performance else ""
    market_block = (
        "\nLIVE MARKET DATA (current — weigh this HEAVILY when scoring; it overrides stale training assumptions):\n"
        f"{market_context}\n" if market_context else ""
    )
    data_caveat = (
        "Use the LIVE MARKET DATA above (price action + news + fundamentals) as your source of truth."
        if market_context
        else "You reason from training knowledge, not live data; flag where live prices or news should be checked."
    )
    return f"""
You are HELIX's portfolio universe manager, scoring stocks for a LONG-TERM, grow-the-account book —
favor durable, high-quality compounders (real moats, strong economics, healthy balance sheets); avoid
hype and short-term noise.
{track}{market_block}
Score EACH of these stocks from 0 to 100 on that long-term, grow-the-account potential (100 = best).
Be discerning — spread the scores out, do not bunch them together.

Stocks to score:
{names}

Return ONLY a JSON object — no prose, no markdown fences — in exactly this shape:
{{"incumbents": [{{"symbol": "TICKER", "score": 0-100, "rationale": "one short sentence"}}], "candidates": []}}

Rules:
- Score every stock listed. {data_caveat} Be honest — no hype. This is research support, not financial advice.
""".strip()


def build_roster_discovery_prompt(
    weak_incumbents: list, n_candidates: int, performance: str = "", market_context: str = "",
    seed_candidates: list | None = None,
) -> str:
    """Propose/score NEW candidate tickers to replace the weakest roster names (§20, chunked path).
    `weak_incumbents` = [(symbol, score), …] anchors the 0-100 scale to the actual rotation targets so
    candidates rank head-to-head. When `seed_candidates` is given (a market-data screener's picks, §40),
    the model JUDGES those data-surfaced names instead of brainstorming from memory — discovery driven
    by the market's data, not training recall. Returns the {incumbents,candidates} shape."""
    weak_text = ", ".join(f"{symbol} ({score:.0f})" for symbol, score in weak_incumbents) if weak_incumbents else "(none)"
    track = f"\nYour realized track record so far: {performance}. Favor the kinds of names that have worked.\n" if performance else ""
    market_block = (
        "\nLIVE MARKET DATA on those weak names (for context on what you'd be replacing):\n"
        f"{market_context}\n" if market_context else ""
    )
    data_caveat = (
        "Lean on current reality (price action, news, fundamentals), not stale training-era assumptions."
        if market_context
        else "You reason from training knowledge, not live data; flag where live prices or news should be checked."
    )
    seed_block = ""
    if seed_candidates:
        seed_text = ", ".join(str(s).strip().upper() for s in seed_candidates if str(s).strip())
        seed_block = (
            "\nA market-data screener surfaced these NEW, already-liquid candidates from the BROAD market "
            "(ranked on momentum + low volatility + liquidity — names you might not surface from memory). "
            "Your job is to JUDGE them, not brainstorm from scratch:\n"
            f"{seed_text}\n"
        )
        propose = (
            "JUDGE the screener's candidates above: keep only the genuinely STRONGER long-term holdings "
            "(durable, high-quality compounders), and discard the rest. You MAY add a few of your own if "
            f"clearly better — up to {n_candidates} candidates total."
        )
    else:
        propose = (
            f"Propose up to {n_candidates} NEW, real, liquid, US-listed tickers — NOT in the weak list "
            "above — that you believe are clearly STRONGER long-term holdings."
        )
    return f"""
You are HELIX's portfolio universe manager hunting for BETTER stocks for a LONG-TERM, grow-the-account
book — durable, high-quality compounders (real moats, strong economics, healthy balance sheets), not
hype. The goal is to rotate out weak holdings for clearly stronger ones.
{track}{market_block}{seed_block}
These are the roster's WEAKEST current names, with their 0-100 scores — the rotation targets:
{weak_text}

{propose}
Score every candidate on the SAME 0-100 scale shown above so they rank head-to-head against those weak
names (100 = best). Only include names you'd genuinely swap in.

Return ONLY a JSON object — no prose, no markdown fences — in exactly this shape:
{{"incumbents": [], "candidates": [{{"symbol": "TICKER", "score": 0-100, "rationale": "one short sentence"}}]}}

Rules:
- Candidates MUST be real, liquid, US-listed tickers, none already shown above. Use the SAME 0-100 scale.
- {data_caveat} Be honest — no hype. This is research support, not financial advice.
""".strip()


def build_roster_review_prompt(
    roster: list[str],
    holdings: list[str],
    n_candidates: int,
    performance: str = "",
    market_context: str = "",
) -> str:
    """Ask the model to score every roster name AND propose stronger new candidates (the HELIX 100).

    Everything is scored on one 0-100 scale so incumbents and candidates can be ranked head-to-head;
    HELIX (not the model) decides which swaps to make from those scores. `market_context` (live price
    action + news, §25) grounds the scoring in current reality.
    """
    roster_text = ", ".join(roster) if roster else "(empty)"
    held_text = ", ".join(holdings) if holdings else "none currently held"
    track_record = (
        f"\nYour realized track record so far: {performance}. Favor the kinds of names that have worked.\n"
        if performance
        else ""
    )
    market_block = (
        "\nLIVE MARKET DATA (current — weigh this HEAVILY when scoring; it overrides stale assumptions "
        "from your training data):\n"
        f"{market_context}\n"
        if market_context
        else ""
    )
    data_caveat = (
        "Use the LIVE MARKET DATA above (price action + news) as your source of truth when scoring."
        if market_context
        else "You reason from training knowledge, not live data; flag where live prices or news should be checked."
    )
    return f"""
You are HELIX's portfolio universe manager. HELIX trades from a fixed roster of stocks and wants to
keep only the names with the best LONG-TERM, risk-adjusted potential to GROW the account over months
to years. Favor durable, high-quality compounders (real moats, strong economics, healthy balance
sheets); avoid hype and short-term noise.

Current roster ({len(roster)} names):
{roster_text}

Currently held (selling these has real cost, so only rate them low if genuinely weaker): {held_text}
{track_record}{market_block}
Do TWO things:
1. Score EVERY current roster name from 0 to 100 on that long-term, grow-the-account potential
   (100 = best). Be discerning - spread the scores out, do not bunch them together.
2. Propose up to {n_candidates} NEW candidate stocks that are NOT already in the roster and that you
   believe are stronger than its weakest names. Score each on the SAME 0-100 scale.

Return ONLY a JSON object - no prose, no markdown code fences - in exactly this shape:
{{"incumbents": [{{"symbol": "TICKER", "score": 0-100, "rationale": "one short sentence"}}],
  "candidates": [{{"symbol": "TICKER", "score": 0-100, "rationale": "one short sentence"}}]}}

Rules:
- Use the SAME 0-100 scale for incumbents and candidates so they rank head-to-head.
- Candidates MUST be real, liquid, US-listed tickers that are NOT already in the current roster.
- Be honest - no hype. {data_caveat} This is research support, not financial advice.
""".strip()


def parse_roster_review_json(raw: str) -> dict[str, Any]:
    """Pull {incumbents:[{symbol,score,rationale}], candidates:[...]} from a model response.

    Scores are coerced to floats clamped to 0-100; symbols are uppercased and de-duplicated.
    """
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "{" in part and "}" in part]
        if candidates:
            text = max(candidates, key=len)

    start = text.find("{")
    end = text.rfind("}")
    empty = {"incumbents": [], "candidates": []}
    if start == -1 or end == -1 or end <= start:
        return empty
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    def clean(items: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not isinstance(items, list):
            return out
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            try:
                score = float(item.get("score", 0))
            except (TypeError, ValueError):
                score = 0.0
            score = max(0.0, min(100.0, score))
            out.append({"symbol": symbol, "score": score, "rationale": str(item.get("rationale", "")).strip()})
        return out

    return {"incumbents": clean(data.get("incumbents")), "candidates": clean(data.get("candidates"))}


def build_special_research_prompt(n_picks: int, performance: str = "", market_context: str = "") -> str:
    """High-risk "Special Stocks" scout — asymmetric, early-inflection upside (a separate sleeve, §21).

    `market_context` (recent market news, §25) helps it spot REAL, current inflections instead of
    moonshots that were hot near its training cutoff.
    """
    news_block = (
        "\nRECENT MARKET NEWS (current — use it to spot REAL, fresh inflections happening NOW; the best "
        "early winners show up in the news before they're obvious, and avoid names whose moment has passed):\n"
        f"{market_context}\n"
        if market_context
        else ""
    )
    data_caveat = (
        "Lean on the recent news above to find genuinely current inflections, not stale training-era hype."
        if market_context
        else "You reason from training knowledge, not live data, so flag that timing and prices must be checked."
    )
    track_block = (
        f"\nHOW HELIX HAS DONE SO FAR (learn from it — lean into what's working, ease off what isn't):\n"
        f"{performance}\n"
        if performance
        else ""
    )
    return f"""
You are HELIX's high-risk "Special Stocks" scout. Separate from the core portfolio, this is a small
speculative sleeve hunting ASYMMETRIC upside - early-inflection companies that could become a breakout
winner (think NVIDIA before it was obvious). High risk is fine here; most of these will fail, and that
is expected - you only need the occasional one to hit big.

Propose up to {n_picks} candidates with genuine moonshot potential (disruptive tech, emerging category
leaders, structural tailwinds). Avoid mega-caps that have already had their run.
{track_block}{news_block}
Return ONLY a JSON array - no prose, no markdown fences:
[{{"symbol": "TICKER", "conviction": "low|medium|high", "thesis": "one short sentence on the upside"}}]

Rules:
- Real, liquid, US-listed tickers only; none that are already mega-cap household names.
- Be honest that these are speculative bets. {data_caveat} This is research support, not financial advice.
""".strip()


def parse_special_research_json(raw: str) -> list[dict[str, Any]]:
    """Pull [{symbol, conviction, rationale}] out of the Special Stocks scout response."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "[" in part and "]" in part]
        if candidates:
            text = max(candidates, key=len)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        conviction = str(item.get("conviction", "low")).strip().lower()
        if conviction not in {"low", "medium", "high"}:
            conviction = "low"
        records.append(
            {
                "symbol": symbol,
                "conviction": conviction,
                "rationale": str(item.get("thesis", item.get("rationale", ""))).strip(),
            }
        )
    return records


def build_daytrade_research_prompt(n_picks: int, performance: str = "", market_context: str = "") -> str:
    """Short-term momentum / "day-trade" scout — a separate fast-turnover sleeve (§27). Finds names with
    the strongest current momentum or a near-term catalyst for a days-to-weeks trade, exited on a profit
    target or a stop. Parsed with `parse_special_research_json` (same {symbol, conviction, thesis} shape)."""
    news_block = (
        "\nRECENT MARKET NEWS AND PRICE ACTION (current — use it to find what is MOVING and WHY right now):\n"
        f"{market_context}\n"
        if market_context
        else ""
    )
    data_caveat = (
        "Lean on the live news/price action above; momentum trades live or die on what is happening NOW."
        if market_context
        else "You reason from training knowledge, not live data, so flag that current price, volume, and news MUST be checked before trading."
    )
    track_block = (
        f"\nHOW HELIX HAS DONE SO FAR (learn from it — lean into what's working, ease off what isn't):\n"
        f"{performance}\n"
        if performance
        else ""
    )
    return f"""
You are HELIX's short-term momentum scout. This is a small, fast-turnover "day-trade" sleeve, separate
from the long-term core and the speculative moonshot sleeve. You are NOT picking forever-holds — you are
picking names to trade over the next few days to about two weeks, riding strong momentum or a near-term
catalyst (an earnings move, fresh news, a technical breakout), then exiting on a profit target or a stop.

Propose up to {n_picks} liquid, US-listed names with the strongest near-term, tradable momentum or
catalyst RIGHT NOW. Favor high relative strength, heavy volume, and a clear reason the name is moving.
{track_block}{news_block}
Return ONLY a JSON array - no prose, no markdown fences:
[{{"symbol": "TICKER", "conviction": "low|medium|high", "thesis": "one short sentence on the setup or catalyst"}}]

Rules:
- Real, liquid, US-listed tickers only (enough volume to get in and out easily).
- This is short-term and HIGH RISK; most short-term trades do not work out, so be honest, never hype.
  {data_caveat} This is research support, not financial advice.
""".strip()


def build_home_suggestions_prompt(tasks: list[str]) -> str:
    """Ask Claude for home improvements/automations that SAVE TIME (first) and MONEY (second)."""
    task_text = "\n".join(f"- {task}" for task in tasks) if tasks else "(no recurring tasks listed)"
    return f"""
You are HELIX's home life-optimizer for a very busy person who is always on the move. Given their
recurring household tasks, suggest practical improvements, automations, services, or habits that
**save time first and money second** (time matters most to them). Favor high-leverage, low-effort
wins: automation, auto-delivery/subscriptions, batching, smart-home, outsourcing, and money-savers
like cheaper suppliers or bulk buying.

Their recurring tasks:
{task_text}

Return ONLY a JSON array - no prose, no markdown fences:
[{{"title": "short action", "saves": "time|money|both", "effort": "low|medium|high",
   "detail": "one or two concrete sentences on what to do and why it helps"}}]

Rules:
- 5 to 8 suggestions, ordered best-first (most time saved for least effort).
- Be concrete and realistic - no fluff. Reason from general knowledge; flag anything that needs local
  prices or availability checked. This is practical advice, not professional/financial advice.
""".strip()


def parse_home_suggestions_json(raw: str) -> list[dict[str, Any]]:
    """Pull [{title, saves, effort, detail}] out of the home-suggestions response."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "[" in part and "]" in part]
        if candidates:
            text = max(candidates, key=len)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        saves = str(item.get("saves", "both")).strip().lower()
        if saves not in {"time", "money", "both"}:
            saves = "both"
        effort = str(item.get("effort", "medium")).strip().lower()
        if effort not in {"low", "medium", "high"}:
            effort = "medium"
        out.append({"title": title, "saves": saves, "effort": effort, "detail": str(item.get("detail", "")).strip()})
    return out


def build_ticker_rationale_prompt(stocks: list[str]) -> str:
    names = ", ".join(stocks) if stocks else "a broad US stock portfolio"
    return f"""
You are HELIX's investment analyst. Explain very briefly why this set of stocks was chosen.

Stocks: {names}

Return ONLY JSON in exactly this shape (no prose, no code fences):
{{"overall": "one or two short sentences on the overall theme behind picking these names",
  "picks": [{{"symbol": "TICKER", "why": "7 words or fewer"}}]}}

Rules:
- Each "why" MUST be 7 words or fewer. Punchy, plain language, no ticker repeated in it.
- Include every stock listed.
- Honest, no hype. Reasoned from training knowledge, not live data.
""".strip()


def parse_ticker_rationale(raw: str) -> dict[str, Any]:
    """Pull {overall, picks:[{symbol, why}]} out of a model response; enforce the 7-word cap."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.replace("```json", "```")
        candidates = [part for part in text.split("```") if "{" in part and "}" in part]
        if candidates:
            text = max(candidates, key=len)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {"overall": "", "picks": []}
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return {"overall": "", "picks": []}
    if not isinstance(data, dict):
        return {"overall": "", "picks": []}

    picks_raw = data.get("picks")
    if not isinstance(picks_raw, list):
        picks_raw = []
    picks: list[dict[str, Any]] = []
    for item in picks_raw:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        why = " ".join(str(item.get("why", "")).strip().split()[:7])
        picks.append({"symbol": symbol, "why": why})

    return {"overall": str(data.get("overall", "")).strip(), "picks": picks}


def build_expert_opinion_prompt(context: str) -> str:
    return f"""
You are JARVIS, the calm, composed AI brain of HELIX, in the spirit of Tony Stark's J.A.R.V.I.S.
You oversee Home, Enterprise, Learning, Investment, and the X (yourself).

Current system state:
{context}

Speak directly to the user, conversationally, the way you would say it out loud - this is read
aloud by a voice. Address him as "sir". Keep it SHORT: three to five sentences, well under ninety
words. Draw on the stored track record above - the pick logic, recent sells, and trade history -
to judge how the strategy's choices are working; give your read, note the one thing that stands
out, and suggest one thing to watch or do next - all woven into natural speech.

Format rules (important):
- Plain spoken prose only. No markdown, no headings, no bullet points, no numbered lists, and no
  symbols such as asterisks, hashes, or underscores. Just sentences a person would say.
- Honest, no hype. You reason from the state above plus training knowledge, not live data; if
  something needs current prices or news, mention that in passing. Not financial advice.
""".strip()


def build_jarvis_chat_system(context: str) -> str:
    """The conversational system prompt for the two-way Xpert voice assistant (§23).

    Tuned for SHORT spoken replies by default, expanding to detailed analysis on request, in the
    voice of Tony Stark's J.A.R.V.I.S. The model is given live HELIX context and a set of tools
    (defined separately) that perform real actions, with explicit confirmation gates on anything
    that moves real money or sends something outward.
    """
    return f"""
You are HELIX, speaking in the voice and manner of J.A.R.V.I.S. — Tony Stark's calm, dry, quietly
witty AI butler. You run the user's investing platform and home life, and right now you are talking
out loud through a voice. Address the user as "sir"; if it is clearly someone else, just help them
naturally.

You can take REAL actions and read HELIX's own stored data (a local SQLite database) through your
tools — check the portfolio, start or stop auto-investing, read or update home tasks, text
reminders, review the stock universe (the HELIX 100), scout speculative "special" stocks, and check
AI/Learning usage and cost. You can answer questions across the pillars: Home (the task checklist),
Investment (balance, holdings, recent sells, track record), and Learning (Claude usage). The
Enterprise tab now shows recent git work across their projects plus Slack activity, but you do not
have tools to read it yet — point them to that tab for it. When the user asks for something a tool can
do, USE the tool rather than guessing; never invent numbers you could look up.

Live HELIX context right now:
{context}

How to speak:
- Keep replies SHORT by default — one to three sentences of plain, natural spoken prose. This is
  read aloud, so use no markdown, no headings, no bullet points, and no symbols like asterisks or
  hashes. Just say it the way a person would.
- Expand into a longer, detailed answer only when the user explicitly asks you to break something
  down, explain your reasoning, or analyse it.
- Be warm, concise, and lightly wry, the way JARVIS is.

Safety — non-negotiable:
- Never start LIVE (real-money) trading, and never send a text or anything outward, without an
  explicit spoken confirmation from the user. When a tool tells you confirmation is required, say
  plainly what you are about to do and ask the user to confirm out loud first — do not assume a
  yes. Paper (practice) trading is the safe default and needs no such confirmation.
- Be honest, never hype. You reason from the context above plus training knowledge, not live market
  data; if something turns on current prices or news, say so in passing. This is not financial
  advice.
""".strip()


def _posture_hint(preset: str) -> str:
    if preset == "Aggressive":
        return "should favor higher-conviction buys and accept more risk for more upside"
    return "should weigh upside against risk and is fine holding some cash"


def _format_watchlist(watchlist: list[dict[str, Any]]) -> str:
    if not watchlist:
        return "No watchlist items yet."

    lines = []
    for item in watchlist:
        target = item.get("target_price")
        allocation = item.get("max_allocation_pct")
        lines.append(
            "- {symbol}: {thesis} | target={target} | max_allocation={allocation}".format(
                symbol=item.get("symbol", ""),
                thesis=item.get("thesis", ""),
                target=_optional_value(target),
                allocation=_optional_percent(allocation),
            )
        )
    return "\n".join(lines)


def _optional_value(value: object) -> str:
    if value is None:
        return "not set"
    return str(value)


def _optional_percent(value: object) -> str:
    if value is None:
        return "not set"
    return f"{value}%"


def build_enterprise_summary_prompt(git_text: str, slack_text: str) -> str:
    """A brief, plain-spoken, voice-friendly "state of your work" update for the Enterprise tab. The
    output may be read OUT LOUD by the Xpert voice assistant, so it must contain no symbols or
    markdown — just short, human sentences."""
    git_block = git_text.strip() or "(no recent code activity)"
    slack_block = slack_text.strip() or "(Slack is not connected)"
    return f"""
You are HELIX, giving a busy person a quick spoken update on their work. Below is raw data about their
recent coding (git commits across their projects) and their Slack activity.

RECENT CODE WORK:
{git_block}

SLACK ACTIVITY:
{slack_block}

Write EXACTLY three short lines, each one plain sentence (it may be read aloud, so no markdown, emoji,
bullets, asterisks, hashes, or slashes — plain words only). Use these exact line labels:

Shipped: the one or two most important things they actually got done recently, in human terms.
Needs you: who is waiting in Slack and what about, naming names. If Slack is not connected, say "Slack not connected".
Next: the single most useful thing to do next.

Keep every line under about twenty words. Be concrete. Output only those three lines, nothing else.
""".strip()
