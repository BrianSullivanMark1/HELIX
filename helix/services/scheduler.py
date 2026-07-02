"""Agent scheduling — agents that run themselves.

The schedule is INFERRED from the agent's goal at save time ("morning brief at 8" → daily 08:00) — no
knobs, no cron syntax, per the autonomy principle. A single UI heartbeat asks due_now() every tick;
`Agent.enabled` is the pause switch. Pure logic lives in infer_schedule/is_due so it's unit-testable;
the QTimer that drives it belongs to the shell (main_window), where every cadence lives.

Schedule shapes (plain dicts, stored with the agent):
  {"kind": "daily",    "at": "08:00"}
  {"kind": "weekdays", "at": "09:00"}
  {"kind": "weekly",   "day": 0-6 (Mon-Sun), "at": "09:00"}
  {"kind": "interval", "minutes": 30}
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from helix.logging_setup import get_logger

if TYPE_CHECKING:
    from helix.services.agents import Agent, AgentService

_LOG = get_logger("scheduler")

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_TIME_RE = re.compile(
    r"\b(?:(at|around|by)\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE
)
_INTERVAL_RE = re.compile(r"\bevery\s+(\d+)\s*(minutes?|mins?|hours?|hrs?)\b", re.IGNORECASE)
_WEEKLY_RE = re.compile(
    r"\b(?:every|each|on)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?\b",
    re.IGNORECASE,
)
_DAILY_RE = re.compile(r"\b(?:every|each)\s+(morning|evening|night|day|afternoon)\b|\bdaily\b|\bnightly\b",
                       re.IGNORECASE)
# "morning brief", "evening summary" — a schedule stated as a noun, the way people actually talk.
_NOUN_RE = re.compile(
    r"\b(morning|evening|nightly|afternoon)\s+(?:brief(?:ing)?|report|summary|digest|update|check(?:-?in)?)\b",
    re.IGNORECASE,
)
_DEFAULT_AT = {"morning": "08:00", "afternoon": "14:00", "evening": "20:00", "night": "20:00",
               "nightly": "20:00", "day": "09:00", "": "09:00"}


def _find_time(text: str, *, evening: bool = False) -> str | None:
    """The first plausible clock time in the text as 'HH:MM' 24h, or None. A time needs an anchor —
    'at 8', '8:30', or '8am' — so a bare count ('check 3 feeds') is never misread as 3 o'clock. A bare
    small hour leans on context: 'every evening at 8' means 20:00."""
    for m in _TIME_RE.finditer(text):
        prefix, minute_str, ampm = m.group(1), m.group(3), (m.group(4) or "").lower()
        if not (prefix or minute_str or ampm):
            continue  # a bare number — probably a count, not a time
        hour, minute = int(m.group(2)), int(minute_str or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        if ampm.startswith("p") and hour < 12:
            hour += 12
        elif ampm.startswith("a") and hour == 12:
            hour = 0
        elif not ampm and evening and hour < 12:
            hour += 12
        return f"{hour:02d}:{minute:02d}"
    return None


def infer_schedule(text: str) -> dict | None:
    """Read a schedule out of a plain-language goal. None = no schedule stated (a manual agent)."""
    t = (text or "").strip()
    if not t:
        return None
    m = _INTERVAL_RE.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        minutes = n * 60 if unit.startswith(("hour", "hr")) else n
        return {"kind": "interval", "minutes": max(5, minutes)}  # floor: never hammer the model
    if re.search(r"\b(?:every\s+hour|hourly)\b", t, re.IGNORECASE):
        return {"kind": "interval", "minutes": 60}
    m = _WEEKLY_RE.search(t)
    if m:
        day = _DAYS.index(m.group(1).lower())
        return {"kind": "weekly", "day": day, "at": _find_time(t) or "09:00"}
    if re.search(r"\b(?:every\s+weekday|weekdays|each\s+weekday)\b", t, re.IGNORECASE):
        return {"kind": "weekdays", "at": _find_time(t) or "09:00"}
    m = _DAILY_RE.search(t) or _NOUN_RE.search(t)
    if m:
        word = (m.group(1) or "").lower()
        evening = word in ("evening", "night", "nightly")
        at = _find_time(t, evening=evening) or _DEFAULT_AT.get(word, "09:00")
        return {"kind": "daily", "at": at}
    return None


def describe(schedule: dict | None) -> str:
    """A short spoken description of a schedule ('daily at 8:00 AM'), or '' for manual agents."""
    if not schedule:
        return ""
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = int(schedule.get("minutes", 60))
        return f"every {minutes // 60} hour{'s' if minutes >= 120 else ''}" if minutes % 60 == 0 \
            else f"every {minutes} minutes"
    at = _hhmm_spoken(schedule.get("at", "09:00"))
    if kind == "weekly":
        return f"every {_DAYS[int(schedule.get('day', 0)) % 7].capitalize()} at {at}"
    if kind == "weekdays":
        return f"weekdays at {at}"
    return f"daily at {at}"


def _hhmm_spoken(at: str) -> str:
    try:
        hour, minute = (int(p) for p in at.split(":"))
    except ValueError:
        return at
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def _parse_at(at: str) -> tuple[int, int]:
    try:
        hour, minute = (int(p) for p in (at or "09:00").split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except ValueError:
        pass
    return 9, 0


def _latest_occurrence(schedule: dict, now: datetime) -> datetime | None:
    """The most recent scheduled fire time <= now (None for interval schedules)."""
    kind = schedule.get("kind")
    hour, minute = _parse_at(schedule.get("at", "09:00"))
    if kind == "daily":
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return anchor if anchor <= now else anchor - timedelta(days=1)
    if kind == "weekdays":
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if anchor > now:
            anchor -= timedelta(days=1)
        while anchor.weekday() > 4:  # walk back off the weekend
            anchor -= timedelta(days=1)
        return anchor
    if kind == "weekly":
        day = int(schedule.get("day", 0)) % 7
        anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        anchor -= timedelta(days=(anchor.weekday() - day) % 7)
        if anchor > now:
            anchor -= timedelta(days=7)
        return anchor
    return None


def is_due(schedule: dict | None, last_run: datetime | None, now: datetime) -> bool:
    """Should this agent fire now? A missed slot (HELIX was closed at 8am, opened at 3pm) still fires
    once, late — friendlier than silently skipping the day."""
    if not schedule:
        return False
    if schedule.get("kind") == "interval":
        minutes = max(5, int(schedule.get("minutes", 60)))
        return last_run is None or (now - last_run) >= timedelta(minutes=minutes)
    anchor = _latest_occurrence(schedule, now)
    if anchor is None:
        return False
    return last_run is None or last_run < anchor


class AgentScheduler:
    """Decides which agents are due; the shell's heartbeat asks every tick and runs what it returns."""

    def __init__(self, agents: "AgentService", clock) -> None:
        self._agents = agents
        self._clock = clock

    def due_now(self) -> list["Agent"]:
        now = self._clock.now()
        due: list[Agent] = []
        for agent in self._agents.list():
            if not agent.enabled or not agent.schedule:
                continue
            last = None
            if agent.last_run:
                try:
                    last = datetime.fromisoformat(agent.last_run)
                except ValueError:
                    last = None
            try:
                if is_due(agent.schedule, last, now):
                    due.append(agent)
            except Exception:  # noqa: BLE001 — one malformed schedule must not stall the heartbeat
                _LOG.warning("bad schedule on agent %r: %r", agent.name, agent.schedule)
        return due

    def mark_ran(self, name: str) -> None:
        self._agents.mark_ran(name, self._clock.now())
