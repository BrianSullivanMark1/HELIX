"""ReminderService — timers and reminders the orb actually keeps.

"Set a 10-minute timer" / "remind me at 5 to start the oven" used to get an app-build offer; now it's a
first-class faculty. Settings-backed (survives a restart — a reminder that came due while HELIX was
closed fires at the next launch, marked late). The shell's heartbeat calls pop_due() and speaks them.
"""
from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.stores import SettingsStore

_LOG = get_logger("reminders")

_KEY = "reminders"
_MAX_ACTIVE = 50  # a runaway model can't flood the store


@dataclass(frozen=True)
class Reminder:
    id: str
    text: str
    due: datetime


def _spoken_time(dt: datetime) -> str:
    suffix = "AM" if dt.hour < 12 else "PM"
    h12 = dt.hour % 12 or 12
    return f"{h12}:{dt.minute:02d} {suffix}"


class ReminderService:
    def __init__(self, settings: SettingsStore, clock: Clock) -> None:
        self._settings = settings
        self._clock = clock

    # ----- reads -----
    def active(self) -> list[Reminder]:
        out: list[Reminder] = []
        for r in self._settings.get(_KEY) or []:
            try:
                out.append(Reminder(r["id"], r["text"], datetime.fromisoformat(r["due"])))
            except (KeyError, TypeError, ValueError):
                continue  # a malformed row is dropped on the next save
        return sorted(out, key=lambda r: r.due)

    def list_line(self) -> str:
        items = self.active()
        if not items:
            return "No reminders set."
        return "Reminders:\n" + "\n".join(
            f"- {r.text} — {_spoken_time(r.due)} ({r.due.strftime('%b %d')})" for r in items
        )

    # ----- writes (orb tools) -----
    def add(self, text: str, *, in_minutes: float | None = None, at_time: str | None = None) -> str:
        """Set one reminder. `in_minutes` for relative ('in 10 minutes'), `at_time` 'HH:MM' 24h for
        absolute (today, or tomorrow if that time already passed). Returns the spoken acknowledgement."""
        text = (text or "").strip() or "Time!"
        now = self._clock.now()
        if in_minutes is not None and in_minutes > 0:
            due = now + timedelta(minutes=float(in_minutes))
        elif at_time:
            try:
                hour, minute = (int(p) for p in str(at_time).split(":"))
                due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            except (TypeError, ValueError):
                return "I couldn't read that time — give me a clock time like 17:30."
            if due <= now:
                due += timedelta(days=1)
        else:
            return "When should I remind you — in how many minutes, or at what time?"
        items = self.active()
        if len(items) >= _MAX_ACTIVE:
            return "That's a lot of reminders — clear some before adding more."
        rem = Reminder(_secrets.token_hex(3), text, due)
        self._save(items + [rem])
        when = _spoken_time(due) if due.date() == now.date() else f"{_spoken_time(due)} tomorrow"
        return f"Reminder set for {when} — I'll speak up."

    def cancel(self, which: str) -> str:
        """Cancel by id or by a fragment of the reminder text."""
        needle = (which or "").strip().lower()
        if not needle:
            return "Which reminder should I cancel?"
        items = self.active()
        hits = [r for r in items if r.id == needle or needle in r.text.lower()]
        if not hits:
            return f"I don't have a reminder matching '{which}'."
        if len(hits) > 1:
            return "A few match — which one? " + "; ".join(f"{r.text} at {_spoken_time(r.due)}" for r in hits)
        self._save([r for r in items if r.id != hits[0].id])
        return f"Cancelled the '{hits[0].text}' reminder."

    # ----- the heartbeat -----
    def pop_due(self) -> list[Reminder]:
        """Return every reminder that has come due and remove it from the store — each fires once."""
        now = self._clock.now()
        items = self.active()
        due = [r for r in items if r.due <= now]
        if due:
            self._save([r for r in items if r.due > now])
        return due

    def _save(self, items: list[Reminder]) -> None:
        self._settings.set(
            _KEY, [{"id": r.id, "text": r.text, "due": r.due.isoformat()} for r in items]
        )
