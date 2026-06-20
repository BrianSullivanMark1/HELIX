from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

# How many turns a single session may hold before it is summarized and rolled over, so a very long
# unbroken conversation doesn't balloon the table (or the startup-rebuilt prompt) without bound (§5).
MAX_SESSION_TURNS = 200
# A summary is just the last assistant line, clipped — enough to *scan* old context, not re-load it.
SUMMARY_MAX_CHARS = 200


class ConversationStore:
    """SQLite-backed persistence for the JARVIS conversation, so HELIX retains context across
    restarts. Deliberately self-contained: it owns its own tables and its own connections to the
    same `data/helix.db` file `SQLiteMemory` uses (no change to that class), in keeping with the
    "memory is the source of truth, I/O at the edges" convention (DESIGN.md §8).

    Lifecycle:
      * `__init__` creates the schema and resumes the most recent session (or starts one), so the
        next launch continues where the last left off.
      * `append_turn()` writes each user/assistant turn immediately — a crash loses at most the
        current in-flight turn.
      * `load_recent_messages()` rebuilds the in-memory Messages-API buffer on startup.
      * `new_session()` / `end_session()` close a session with a one-line summary (the "New chat"
        button and a >200-turn roll-over both use this), so old context stays scannable cheaply.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
        self._initialize()
        self.session_id = self._resume_or_create_session()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    role TEXT,
                    content TEXT,
                    session_id TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_session
                    ON conversation_history (session_id, id);

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_active TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    summary TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    # -- sessions ----------------------------------------------------------- #

    def _resume_or_create_session(self) -> str:
        """Resume the most recent session if one exists (so context survives a restart), else mint a
        fresh UUID session. Returns the active session_id."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions ORDER BY datetime(last_active) DESC, rowid DESC LIMIT 1"
            ).fetchone()
            if row and row["session_id"]:
                return str(row["session_id"])
        return self._create_session()

    def _create_session(self) -> str:
        session_id = uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sessions (session_id, created_at, last_active) "
                "VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (session_id,),
            )
        return session_id

    def new_session(self) -> str:
        """Summarize the current session, then start a fresh one (the "New chat" button). Returns the
        new session_id (also stored as `self.session_id`)."""
        self._summarize_session(self.session_id)
        self.session_id = self._create_session()
        return self.session_id

    # "End a session" is the same act as starting the next one minus the new mint — used when the app
    # closes. Kept as a thin alias so callers read clearly.
    def end_session(self) -> None:
        self._summarize_session(self.session_id)

    def _summarize_session(self, session_id: str) -> None:
        """Write a one-line summary (the last assistant message, clipped) so very old context is still
        scannable without re-loading it into the prompt. No-op for an empty session or a duplicate."""
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT content FROM conversation_history
                WHERE session_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            summary = (str(row["content"]) if row and row["content"] else "").strip()
            if not summary:
                return
            already = connection.execute(
                "SELECT 1 FROM session_summaries WHERE session_id = ? LIMIT 1", (session_id,)
            ).fetchone()
            if already:
                return
            connection.execute(
                "INSERT INTO session_summaries (session_id, summary, created_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP)",
                (session_id, summary[:SUMMARY_MAX_CHARS]),
            )

    # -- turns -------------------------------------------------------------- #

    def append_turn(self, role: str, content: str) -> None:
        """Persist one conversation turn immediately. Rolls the session over (with a summary) once it
        exceeds MAX_SESSION_TURNS so neither the table nor the rebuilt prompt grows without bound."""
        role = str(role or "").strip().lower()
        content = "" if content is None else str(content)
        if role not in ("user", "assistant") or not content.strip():
            return
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO conversation_history (role, content, session_id, timestamp) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (role, content, self.session_id),
            )
            connection.execute(
                "UPDATE sessions SET last_active = CURRENT_TIMESTAMP WHERE session_id = ?",
                (self.session_id,),
            )
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM conversation_history WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()
        if count and int(count["n"]) > MAX_SESSION_TURNS:
            self.new_session()

    def get_recent_history(self, n: int = 50, session_id: str | None = None) -> list[dict[str, Any]]:
        """The last `n` rows, ordered by id ascending, for rebuilding the Claude message list. Scoped
        to the current session by default; pass session_id=... (or "" for all sessions) to widen."""
        sid = self.session_id if session_id is None else session_id
        with self.connect() as connection:
            if sid:
                rows = connection.execute(
                    """
                    SELECT id, timestamp, role, content, session_id FROM (
                        SELECT * FROM conversation_history WHERE session_id = ?
                        ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (sid, int(n)),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT id, timestamp, role, content, session_id FROM (
                        SELECT * FROM conversation_history ORDER BY id DESC LIMIT ?
                    ) ORDER BY id ASC
                    """,
                    (int(n),),
                ).fetchall()
        return [dict(row) for row in rows]

    def load_recent_messages(self, n: int = 50) -> list[dict[str, str]]:
        """The recent history as plain Messages-API turns ({"role", "content"} strings) ready to seed
        the in-memory conversation buffer that gets sent to Claude on startup."""
        return [
            {"role": row["role"], "content": row["content"]}
            for row in self.get_recent_history(n)
            if row.get("role") in ("user", "assistant") and row.get("content")
        ]

    def list_session_summaries(self, limit: int = 20) -> list[dict[str, Any]]:
        """Recent session summaries, newest first — cheap scannable history of older conversations."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT session_id, summary, created_at FROM session_summaries "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]
