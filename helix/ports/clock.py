"""Clock port — so nothing scatters datetime.now() and tests stay deterministic."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...
