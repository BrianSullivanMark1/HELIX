"""Clock adapter — the real wall clock (local, timezone-aware)."""
from __future__ import annotations

from datetime import datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()
