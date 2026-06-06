from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class SQLiteMemory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(exist_ok=True)
        self._initialize()
        self._migrate()
        self._seed_rating_outcomes()
        self.prune_old_data()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS investment_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    monthly_income REAL NOT NULL,
                    monthly_expenses REAL NOT NULL,
                    cash_savings REAL NOT NULL,
                    debt_total REAL NOT NULL,
                    monthly_debt_payment REAL NOT NULL,
                    current_investments REAL NOT NULL,
                    target_emergency_months INTEGER NOT NULL,
                    risk_tolerance TEXT NOT NULL,
                    primary_goal TEXT NOT NULL,
                    goal_amount REAL NOT NULL,
                    goal_years INTEGER NOT NULL,
                    expected_annual_return REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY,
                    thesis TEXT NOT NULL,
                    target_price REAL,
                    max_allocation_pct REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    est_cost REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS stock_rationale (
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol, action)
                );

                CREATE TABLE IF NOT EXISTS sell_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    return_pct REAL,
                    realized_pl REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    equity REAL NOT NULL,
                    cash REAL NOT NULL DEFAULT 0,
                    market_value REAL NOT NULL DEFAULT 0,
                    unrealized_pl REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS rating_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_rating_outcomes_symbol
                    ON rating_outcomes (symbol);

                CREATE TABLE IF NOT EXISTS fundamentals (
                    symbol TEXT PRIMARY KEY,
                    metrics TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sectors (
                    symbol TEXT PRIMARY KEY,
                    sector TEXT NOT NULL,
                    fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS market_assets (
                    symbol TEXT PRIMARY KEY
                );
                """
            )

    def _migrate(self) -> None:
        # Add columns to databases created before they existed; ignore if already present.
        additions = [
            ("sell_log", "return_pct", "REAL"),
            ("sell_log", "realized_pl", "REAL"),
        ]
        with self.connect() as connection:
            for table, column, col_type in additions:
                try:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except sqlite3.OperationalError:
                    pass
            # Migrate stock_rationale from PK(symbol) to PK(symbol, action) so each sleeve's rationale
            # survives — previously the core re-rate overwrote a symbol's day-trade/special row (they
            # share the table, keyed by symbol), wiping the Day-trade research log. Idempotent: the
            # check matches only the old inline-PK schema. Rows are preserved.
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='stock_rationale'"
            ).fetchone()
            if row and row[0] and "symbol TEXT PRIMARY KEY" in row[0]:
                connection.executescript(
                    """
                    CREATE TABLE stock_rationale_new (
                        symbol TEXT NOT NULL,
                        action TEXT NOT NULL,
                        confidence TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (symbol, action)
                    );
                    INSERT OR IGNORE INTO stock_rationale_new (symbol, action, confidence, rationale, updated_at)
                        SELECT symbol, action, confidence, rationale, updated_at FROM stock_rationale;
                    DROP TABLE stock_rationale;
                    ALTER TABLE stock_rationale_new RENAME TO stock_rationale;
                    """
                )

    def _seed_rating_outcomes(self) -> None:
        """One-time backfill (§28): if the append-only `rating_outcomes` log is empty but current
        ratings already exist, seed it from `stock_rationale` using each row's real `updated_at`.

        This gives an existing account an immediate baseline so forward-return buckets begin maturing
        from the ratings it already has, rather than from the next re-rate a week out. A no-op once
        seeded, and on a fresh DB (nothing to copy). New ratings append via `record_rating_snapshots`.
        """
        with self.connect() as connection:
            already = connection.execute("SELECT 1 FROM rating_outcomes LIMIT 1").fetchone()
            if already:
                return
            connection.execute(
                """
                INSERT INTO rating_outcomes (symbol, action, confidence, rationale, created_at)
                SELECT symbol, action, confidence, rationale, updated_at FROM stock_rationale
                """
            )

    def get_investment_profile(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM investment_profile WHERE id = 1"
            ).fetchone()
        return dict(row) if row else None

    def save_investment_profile(self, profile: dict[str, Any]) -> None:
        fields = [
            "monthly_income",
            "monthly_expenses",
            "cash_savings",
            "debt_total",
            "monthly_debt_payment",
            "current_investments",
            "target_emergency_months",
            "risk_tolerance",
            "primary_goal",
            "goal_amount",
            "goal_years",
            "expected_annual_return",
        ]
        values = {field: profile[field] for field in fields}
        with self.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO investment_profile (
                    id, {", ".join(fields)}, created_at, updated_at
                )
                VALUES (
                    1, {", ".join(":" + field for field in fields)},
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT(id) DO UPDATE SET
                    {", ".join(field + " = excluded." + field for field in fields)},
                    updated_at = CURRENT_TIMESTAMP
                """,
                values,
            )

    def list_watchlist(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, thesis, target_price, max_allocation_pct, created_at, updated_at
                FROM watchlist
                ORDER BY symbol
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_watchlist_item(
        self,
        symbol: str,
        thesis: str,
        target_price: float | None,
        max_allocation_pct: float | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO watchlist (
                    symbol, thesis, target_price, max_allocation_pct, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET
                    thesis = excluded.thesis,
                    target_price = excluded.target_price,
                    max_allocation_pct = excluded.max_allocation_pct,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (symbol.upper(), thesis, target_price, max_allocation_pct),
            )

    def remove_watchlist_item(self, symbol: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM watchlist WHERE symbol = ?",
                (symbol.upper(),),
            )
        return cursor.rowcount > 0

    def add_journal_entry(self, entry_type: str, title: str, body: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO journal (entry_type, title, body, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (entry_type, title, body),
            )

    def list_journal_entries(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, entry_type, title, body, created_at
                FROM journal
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_symbol_trades(self, symbol: str, limit: int = 30) -> list[dict[str, Any]]:
        """HELIX's own buy/sell journal records for one symbol, newest first (its order log).

        Trade titles are written as '<Mode> <side> <SYMBOL>' (e.g. 'Paper buy AAPL'), so matching
        the title's trailing ' SYMBOL' isolates one ticker without false-matching longer ones.
        """
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT created_at, title, body
                FROM journal
                WHERE entry_type IN ('paper_trade', 'live_trade') AND title LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"% {symbol}", limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_ai_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        est_cost: float,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_usage (model, input_tokens, output_tokens, est_cost, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (model, int(input_tokens), int(output_tokens), float(est_cost)),
            )

    def monthly_ai_spend(self) -> float:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(est_cost), 0.0) AS total
                FROM ai_usage
                WHERE created_at >= date('now', 'start of month')
                """
            ).fetchone()
        return float(row["total"]) if row else 0.0

    def ai_usage_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS calls,
                    COALESCE(SUM(est_cost), 0.0) AS total_cost,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(CASE WHEN created_at >= date('now', 'start of month') THEN est_cost ELSE 0 END), 0.0) AS month_cost,
                    COALESCE(SUM(CASE WHEN created_at >= date('now', 'start of day') THEN est_cost ELSE 0 END), 0.0) AS today_cost
                FROM ai_usage
                """
            ).fetchone()
        return {
            "calls": int(row["calls"]),
            "total_cost": float(row["total_cost"]),
            "month_cost": float(row["month_cost"]),
            "today_cost": float(row["today_cost"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
        }

    def save_stock_rationales(self, ratings: dict) -> None:
        """Upsert per-stock pick logic: {symbol: {action, confidence, rationale}}."""
        if not ratings:
            return
        with self.connect() as connection:
            for symbol, record in ratings.items():
                connection.execute(
                    """
                    INSERT INTO stock_rationale (symbol, action, confidence, rationale, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(symbol, action) DO UPDATE SET
                        confidence = excluded.confidence,
                        rationale = excluded.rationale,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        str(symbol).upper(),
                        str(record.get("action", "")),
                        str(record.get("confidence", "")),
                        str(record.get("rationale", "")),
                    ),
                )

    def list_stock_rationale(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, action, confidence, rationale, updated_at
                FROM stock_rationale
                ORDER BY CASE action WHEN 'buy' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END, symbol
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def cached_ratings(self, symbols: list[str], max_age_days: float) -> dict[str, Any] | None:
        """Reuse recent ratings instead of re-calling the model every cycle (cadence, §14).

        Returns {symbol: {action, confidence, rationale}} ONLY if every requested symbol has a
        rating refreshed within `max_age_days`; otherwise None, so the caller re-rates the whole
        universe (a newly-added or stale ticker forces a fresh pass that keeps ratings coherent).
        """
        unique = sorted({str(s).strip().upper() for s in (symbols or []) if str(s).strip()})
        if not unique or max_age_days <= 0:
            return None
        placeholders = ",".join("?" * len(unique))
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, action, confidence, rationale
                FROM stock_rationale
                WHERE symbol IN ({placeholders}) AND action IN ('buy', 'watch', 'skip')
                  AND updated_at >= datetime('now', ?)
                """,
                (*unique, f"-{float(max_age_days)} days"),
            ).fetchall()
        fresh = {row["symbol"]: dict(row) for row in rows}
        if any(symbol not in fresh for symbol in unique):
            return None
        return {
            symbol: {
                "action": fresh[symbol]["action"],
                "confidence": fresh[symbol]["confidence"],
                "rationale": fresh[symbol]["rationale"],
            }
            for symbol in unique
        }

    def record_rating_snapshots(self, ratings: dict) -> int:
        """Append a point-in-time snapshot of each rating to the append-only `rating_outcomes` log,
        so per-pick forward returns can later be measured and bucketed by confidence (§28).

        This is the MEASUREMENT counterpart to `save_stock_rationales`: that table is current-state
        (one row per symbol, overwritten each re-rate), so prior ratings are lost; this one is never
        updated, so it preserves "on date D, HELIX rated SYMBOL action/confidence" — the history the
        prediction scorecard scores against realized forward prices. Returns rows written.
        """
        rows: list[tuple[str, str, str, str]] = []
        for symbol, record in (ratings or {}).items():
            sym = str(symbol).strip().upper()
            if not sym:
                continue
            rows.append(
                (
                    sym,
                    str(record.get("action", "")),
                    str(record.get("confidence", "")),
                    str(record.get("rationale", "")),
                )
            )
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO rating_outcomes (symbol, action, confidence, rationale, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                rows,
            )
        return len(rows)

    def list_rating_snapshots(
        self, days: int = 365, actions: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """Rating snapshots within the last `days`, oldest first, for the prediction scorecard (§28).

        Optionally filter to specific actions (e.g. ('buy','watch','skip') for the core sleeve, or
        ('special',) / ('daytrade',) for a per-sleeve track record).
        """
        clauses = ["created_at >= datetime('now', ?)"]
        params: list[Any] = [f"-{int(days)} days"]
        if actions:
            clauses.append("action IN (%s)" % ",".join("?" * len(actions)))
            params.extend(actions)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, action, confidence, rationale, created_at
                FROM rating_outcomes
                WHERE {" AND ".join(clauses)}
                ORDER BY id ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def rating_snapshot_summary(self) -> dict[str, Any]:
        """Cheap counts/span for the scorecard header (how much history has accrued)."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n, COUNT(DISTINCT symbol) AS symbols,
                       MIN(created_at) AS since, MAX(created_at) AS latest
                FROM rating_outcomes
                """
            ).fetchone()
        return {
            "snapshots": int(row["n"]) if row and row["n"] else 0,
            "symbols": int(row["symbols"]) if row and row["symbols"] else 0,
            "since": str(row["since"])[:10] if row and row["since"] else "",
            "latest": str(row["latest"])[:10] if row and row["latest"] else "",
        }

    def upsert_fundamentals(self, metrics_by_symbol: dict[str, Any]) -> int:
        """Cache per-symbol fundamentals (§32): {SYMBOL: {revenue, net_margin, roe, …}} as JSON,
        upserted (current-state, like `stock_rationale`). Returns rows written. The monthly SEC
        refresh writes here; the weekly re-rate reads locally via `get_fundamentals`."""
        rows = [(str(s).strip().upper(), json.dumps(m)) for s, m in (metrics_by_symbol or {}).items() if str(s).strip()]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO fundamentals (symbol, metrics, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET metrics = excluded.metrics, fetched_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def get_fundamentals(self, symbols: list[str] | None = None) -> dict[str, Any]:
        """Cached fundamentals {SYMBOL: metrics}, optionally limited to `symbols` (the chunk being rated)."""
        with self.connect() as connection:
            if symbols:
                unique = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
                if not unique:
                    return {}
                placeholders = ",".join("?" * len(unique))
                rows = connection.execute(
                    f"SELECT symbol, metrics FROM fundamentals WHERE symbol IN ({placeholders})", unique
                ).fetchall()
            else:
                rows = connection.execute("SELECT symbol, metrics FROM fundamentals").fetchall()
        out: dict[str, Any] = {}
        for row in rows:
            try:
                out[row["symbol"]] = json.loads(row["metrics"])
            except (TypeError, ValueError):
                continue
        return out

    def fundamentals_summary(self) -> dict[str, Any]:
        """Coverage + freshness for the fundamentals cache (how many names, last SEC refresh)."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n, MAX(fetched_at) AS latest FROM fundamentals"
            ).fetchone()
        return {
            "names": int(row["n"]) if row and row["n"] else 0,
            "latest": str(row["latest"])[:19] if row and row["latest"] else "",
        }

    def upsert_sectors(self, by_symbol: dict[str, str]) -> int:
        """Cache SEC-derived sectors (§35): {SYMBOL: sector}, upserted (current-state; SIC is ~static)."""
        rows = [(str(s).strip().upper(), str(sec)) for s, sec in (by_symbol or {}).items() if str(s).strip() and sec]
        if not rows:
            return 0
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO sectors (symbol, sector, fetched_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(symbol) DO UPDATE SET sector = excluded.sector, fetched_at = CURRENT_TIMESTAMP
                """,
                rows,
            )
        return len(rows)

    def replace_market_assets(self, symbols) -> int:
        """Replace the cached real-market universe (§36) with the current tradeable Alpaca asset set —
        a full snapshot (clear + insert), refreshed on a weekly cadence. Returns the count stored."""
        unique = sorted({str(s).strip().upper() for s in (symbols or []) if str(s).strip()})
        with self.connect() as connection:
            connection.execute("DELETE FROM market_assets")
            connection.executemany("INSERT OR IGNORE INTO market_assets (symbol) VALUES (?)", [(s,) for s in unique])
        return len(unique)

    def get_tradable_universe(self) -> set[str]:
        """The cached set of real, tradeable tickers (§36) — discovered names are validated against it."""
        with self.connect() as connection:
            rows = connection.execute("SELECT symbol FROM market_assets").fetchall()
        return {row["symbol"] for row in rows}

    def get_sectors(self, symbols: list[str] | None = None) -> dict[str, str]:
        """Cached SEC sectors {SYMBOL: sector}, optionally limited to `symbols`."""
        with self.connect() as connection:
            if symbols:
                unique = sorted({str(s).strip().upper() for s in symbols if str(s).strip()})
                if not unique:
                    return {}
                placeholders = ",".join("?" * len(unique))
                rows = connection.execute(
                    f"SELECT symbol, sector FROM sectors WHERE symbol IN ({placeholders})", unique
                ).fetchall()
            else:
                rows = connection.execute("SELECT symbol, sector FROM sectors").fetchall()
        return {row["symbol"]: row["sector"] for row in rows}

    def record_sell(
        self,
        symbol: str,
        reason: str,
        rationale: str,
        amount_usd: float,
        return_pct: float | None = None,
        realized_pl: float | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sell_log (symbol, reason, rationale, amount_usd, return_pct, realized_pl, created_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(symbol).upper(),
                    str(reason),
                    str(rationale),
                    float(amount_usd),
                    None if return_pct is None else float(return_pct),
                    None if realized_pl is None else float(realized_pl),
                ),
            )

    def list_sells(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, reason, rationale, amount_usd, return_pct, realized_pl, created_at
                FROM sell_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def strategy_performance(self) -> dict[str, Any]:
        """Realized track record from closed sells: hit rate, avg return, realized P/L."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS closed,
                    COALESCE(SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END), 0) AS wins,
                    AVG(return_pct) AS avg_return,
                    COALESCE(SUM(realized_pl), 0.0) AS realized
                FROM sell_log
                WHERE return_pct IS NOT NULL
                """
            ).fetchone()
        closed = int(row["closed"]) if row and row["closed"] else 0
        wins = int(row["wins"]) if row and row["wins"] else 0
        avg_return = float(row["avg_return"]) if row and row["avg_return"] is not None else 0.0
        realized = float(row["realized"]) if row and row["realized"] is not None else 0.0
        return {
            "closed": closed,
            "wins": wins,
            "hit_rate": round(100.0 * wins / closed, 1) if closed else 0.0,
            "avg_return_pct": round(avg_return, 2),
            "realized_pl": round(realized, 2),
        }

    def record_equity(
        self,
        equity: float,
        cash: float = 0.0,
        market_value: float = 0.0,
        unrealized_pl: float = 0.0,
        min_gap_seconds: int = 600,
    ) -> None:
        """Append an account-equity sample (HELIX's own durable curve, fed back to the AI layer).

        Skips the insert if another sample was recorded within the last `min_gap_seconds`, so
        frequent portfolio refreshes don't flood the table. Non-positive equity is ignored.
        """
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            return
        if equity <= 0:
            return
        with self.connect() as connection:
            if min_gap_seconds > 0:
                recent = connection.execute(
                    "SELECT 1 FROM equity_history WHERE created_at >= datetime('now', ?) LIMIT 1",
                    (f"-{int(min_gap_seconds)} seconds",),
                ).fetchone()
                if recent:
                    return
            connection.execute(
                """
                INSERT INTO equity_history (equity, cash, market_value, unrealized_pl, created_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (equity, float(cash), float(market_value), float(unrealized_pl)),
            )

    def list_equity_history(self, days: int = 365) -> list[dict[str, Any]]:
        """Equity samples within the last `days`, oldest first (ready for plotting)."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT equity, cash, market_value, unrealized_pl, created_at
                FROM equity_history
                WHERE created_at >= datetime('now', ?)
                ORDER BY id ASC
                """,
                (f"-{int(days)} days",),
            ).fetchall()
        return [dict(row) for row in rows]

    def investment_digest(self) -> dict[str, Any]:
        """Compact track-record summary for the Xpert brain (trade/sell counts + data span)."""
        with self.connect() as connection:
            trades = connection.execute(
                "SELECT COUNT(*) AS n FROM journal WHERE entry_type IN ('paper_trade', 'live_trade')"
            ).fetchone()
            sells = connection.execute("SELECT COUNT(*) AS n FROM sell_log").fetchone()
            since_row = connection.execute(
                """
                SELECT MIN(created_at) AS since FROM (
                    SELECT created_at FROM journal
                    UNION ALL SELECT created_at FROM sell_log
                    UNION ALL SELECT created_at FROM ai_usage
                )
                """
            ).fetchone()
        since = since_row["since"] if since_row and since_row["since"] else ""
        return {
            "trades": int(trades["n"]) if trades else 0,
            "sells": int(sells["n"]) if sells else 0,
            "since": str(since)[:10],
        }

    def prune_old_data(self, days: int = 365) -> None:
        """Rolling-window retention: drop time-series rows older than `days` (default 1 year).

        `stock_rationale` is current-state (one upserted row per symbol) so it is not time-pruned.
        """
        cutoff = f"-{int(days)} days"
        with self.connect() as connection:
            for table in ("journal", "sell_log", "ai_usage", "equity_history", "rating_outcomes"):
                connection.execute(
                    f"DELETE FROM {table} WHERE created_at < datetime('now', ?)",
                    (cutoff,),
                )
