"""SQLite adapter — implements MemoryStore + ConversationStore over one file.

One connection guarded by a lock (used from worker threads), check_same_thread=False.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from helix.domain.models import Message, Role, Version

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  input_tokens  INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cost_usd      REAL    NOT NULL,
  at            TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  commit_sha TEXT PRIMARY KEY,
  summary    TEXT    NOT NULL,
  at         TEXT    NOT NULL,
  pinned     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  at   TEXT NOT NULL
);
"""


class SqliteStore:
    """Satisfies both the MemoryStore and ConversationStore ports."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    # ----- MemoryStore -----
    def record_usage(self, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        with self._lock:
            if self._closed:
                return  # a late worker write after close() drops cleanly instead of raising
            self._conn.execute(
                "INSERT INTO usage(input_tokens, output_tokens, cost_usd, at) VALUES (?,?,?,?)",
                (int(input_tokens), int(output_tokens), float(cost_usd), self._now()),
            )
            self._conn.commit()

    def add_version(self, version: Version) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO versions(commit_sha, summary, at, pinned) VALUES (?,?,?,?)",
                (version.commit, version.summary, version.at.isoformat(), int(version.pinned)),
            )
            self._conn.commit()

    def versions(self) -> list[Version]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM versions ORDER BY at DESC").fetchall()
        return [
            Version(
                commit=r["commit_sha"],
                summary=r["summary"],
                at=datetime.fromisoformat(r["at"]),
                pinned=bool(r["pinned"]),
            )
            for r in rows
        ]

    def set_pinned(self, commit: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE versions SET pinned = (commit_sha = ?)", (commit,))
            self._conn.commit()

    # ----- ConversationStore -----
    def append(self, message: Message) -> None:
        at = (message.at or datetime.now().astimezone()).isoformat()
        with self._lock:
            if self._closed:
                return  # a late agent/turn reply after close() drops cleanly instead of raising
            self._conn.execute(
                "INSERT INTO messages(role, text, at) VALUES (?,?,?)",
                (message.role.value, message.text, at),
            )
            self._conn.commit()

    def recent(self, limit: int = 100) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [
            Message(role=Role(r["role"]), text=r["text"], at=datetime.fromisoformat(r["at"]))
            for r in reversed(rows)
        ]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._conn.close()
