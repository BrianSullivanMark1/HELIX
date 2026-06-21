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
                    symbol TEXT PRIMARY KEY,
                    fractionable INTEGER NOT NULL DEFAULT 1
                );

                -- §selfdev Archive: every self-improvement is a revertible commit on main; this is the
                -- human-friendly index of those versions (whole-app snapshots) you can restore to. git
                -- is the version STORE; this table is the INDEX (prompts, labels, default/root pointers).
                CREATE TABLE IF NOT EXISTS interface_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_sha TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    is_root INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                -- §selfdev: the construction prompt(s) behind each menu button, so each self-built
                -- feature carries its provenance. Cleaned up when the feature is removed (its ✕).
                CREATE TABLE IF NOT EXISTS feature_provenance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_key TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    branch TEXT NOT NULL DEFAULT '',
                    commit_sha TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'build',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(feature_key, commit_sha)
                );
                """
            )

    def _migrate(self) -> None:
        # Add columns to databases created before they existed; ignore if already present.
        additions = [
            ("sell_log", "return_pct", "REAL"),
            ("sell_log", "realized_pl", "REAL"),
            ("market_assets", "fractionable", "INTEGER NOT NULL DEFAULT 1"),  # §42 whole-share support
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





















    # -- self-improvement Archive: versions & provenance (§selfdev) ------------ #

    def upsert_interface_version(
        self, commit_sha: str, label: str, prompt: str, branch: str = "", created_at: str | None = None
    ) -> None:
        """Record (or refresh) one app version — a point on main's timeline, keyed by commit so a
        re-sync is idempotent. Never touches the is_default / is_root pointers."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO interface_versions (commit_sha, label, prompt, branch, created_at)
                VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
                ON CONFLICT(commit_sha) DO UPDATE SET
                    label = CASE WHEN interface_versions.is_root = 1 THEN interface_versions.label ELSE excluded.label END,
                    prompt = CASE WHEN interface_versions.is_root = 1 THEN interface_versions.prompt ELSE excluded.prompt END,
                    branch = excluded.branch
                """,
                (commit_sha, label, prompt, branch, created_at),
            )

    def list_interface_versions(self) -> list[dict[str, Any]]:
        """All recorded app versions, newest first (the Archive list)."""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, commit_sha, label, prompt, branch, is_default, is_root, created_at
                FROM interface_versions
                ORDER BY datetime(created_at) DESC, id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_interface_version(self, version_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM interface_versions WHERE id = ?", (version_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_interface_version(self, version_id: int) -> bool:
        """Purge a version from the Archive. Refuses to delete the master default or the root baseline
        (their protection lives in the WHERE clause, so it can't be bypassed by a stray call)."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM interface_versions WHERE id = ? AND is_default = 0 AND is_root = 0",
                (version_id,),
            )
        return cursor.rowcount > 0

    def set_default_version(self, version_id: int) -> bool:
        """Pin one version as the master default (clears any previous). Returns True if it existed."""
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM interface_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if not exists:
                return False
            connection.execute("UPDATE interface_versions SET is_default = 0")
            connection.execute(
                "UPDATE interface_versions SET is_default = 1 WHERE id = ?", (version_id,)
            )
        return True


    def get_root_version(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM interface_versions WHERE is_root = 1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def ensure_root_version(self, commit_sha: str, label: str, prompt: str) -> bool:
        """Pin the immutable ROOT baseline once (the blank-menu factory-reset target). No-op if set."""
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM interface_versions WHERE is_root = 1 LIMIT 1"
            ).fetchone()
            if existing:
                return False
            connection.execute(
                """
                INSERT INTO interface_versions (commit_sha, label, prompt, branch, is_root, created_at)
                VALUES (?, ?, ?, '', 1, CURRENT_TIMESTAMP)
                ON CONFLICT(commit_sha) DO UPDATE SET is_root = 1, label = excluded.label
                """,
                (commit_sha, label, prompt),
            )
        return True

    def upsert_feature_provenance(
        self, feature_key: str, label: str, prompt: str, branch: str = "",
        commit_sha: str = "", kind: str = "build",
    ) -> None:
        """Save the construction prompt behind a menu feature (idempotent per feature+commit)."""
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO feature_provenance (feature_key, label, prompt, branch, commit_sha, kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(feature_key, commit_sha) DO UPDATE SET
                    label = excluded.label, prompt = excluded.prompt,
                    branch = excluded.branch, kind = excluded.kind
                """,
                (feature_key, label, prompt, branch, commit_sha, kind),
            )



    def prune_feature_provenance(self, keep_keys) -> int:
        """Delete provenance for any feature no longer in the menu (self-healing cleanup on removal).
        Passing an empty set clears all provenance (a blank menu has no features)."""
        keep = [str(k) for k in (keep_keys or []) if str(k)]
        with self.connect() as connection:
            if keep:
                placeholders = ",".join("?" * len(keep))
                cursor = connection.execute(
                    f"DELETE FROM feature_provenance WHERE feature_key NOT IN ({placeholders})", keep
                )
            else:
                cursor = connection.execute("DELETE FROM feature_provenance")
        return cursor.rowcount

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
