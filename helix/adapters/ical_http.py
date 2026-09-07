"""iCal adapter — fetch and parse a private .ics feed (read-only).

Speaks to a secret iCal URL (e.g. Google Calendar's "Secret address in iCal format"). The URL IS the
credential, so the opener refuses redirects — the same rule as call_api: a 3xx must never bounce a
secret to another host. Parsing is deliberately minimal: VEVENT start/end/summary/location plus the
common recurrences (FREQ=DAILY/WEEKLY with INTERVAL/BYDAY/UNTIL/COUNT), which covers real household
and work calendars without dragging in a full RFC 5545 engine.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from helix.logging_setup import get_logger

_LOG = get_logger("ical")

_MAX_BODY = 5_000_000  # a calendar feed beyond ~5MB is not a calendar
_MAX_EXPANSION = 500   # recurrence iterations per event — plenty for any real window


class CalendarError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The iCal URL is a secret; never re-send it to a host a redirect chose."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

_ICS_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


@dataclass
class CalEvent:
    start: datetime
    summary: str
    end: datetime | None = None
    location: str = ""
    all_day: bool = False
    rrule: dict = field(default_factory=dict)


def fetch_ics(url: str, timeout: float = 15.0) -> str:
    url = (url or "").strip()
    if not url.lower().startswith(("https://", "webcal://")):
        raise CalendarError("the calendar address must be an https:// (or webcal://) iCal URL")
    if url.lower().startswith("webcal://"):  # the common copy-paste form — same feed over https
        url = "https://" + url[len("webcal://"):]
    req = urllib.request.Request(url, headers={"User-Agent": "HELIX", "Accept": "text/calendar, */*"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read(_MAX_BODY).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CalendarError(f"the calendar feed returned HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 - offline, DNS, TLS…
        raise CalendarError(f"couldn't reach the calendar feed ({exc})") from exc


def _unfold(text: str) -> list[str]:
    """RFC 5545 line unfolding: a line starting with space/tab continues the previous one."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str, params: str) -> tuple[datetime | None, bool]:
    """An ICS DTSTART/DTEND -> (local datetime, all_day). 'Z' converts from UTC; a TZID is treated as
    the machine's local zone (the practical case: the user's calendar zone IS their machine zone)."""
    value = value.strip()
    try:
        if "VALUE=DATE" in params or (len(value) == 8 and value.isdigit()):
            return datetime.strptime(value, "%Y%m%d"), True
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.astimezone().replace(tzinfo=None), False
        return datetime.strptime(value, "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, False


def _parse_rrule(value: str) -> dict:
    out: dict = {}
    for part in value.strip().split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.upper()] = v
    return out


def parse_events(ics_text: str) -> list[CalEvent]:
    events: list[CalEvent] = []
    current: dict | None = None
    for line in _unfold(ics_text):
        if line.startswith("BEGIN:VEVENT"):
            current = {}
            continue
        if line.startswith("END:VEVENT"):
            if current is not None and current.get("start") is not None:
                events.append(
                    CalEvent(
                        start=current["start"], end=current.get("end"),
                        summary=current.get("summary", "(untitled)"),
                        location=current.get("location", ""),
                        all_day=current.get("all_day", False), rrule=current.get("rrule", {}),
                    )
                )
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        name = name.upper()
        if name == "DTSTART":
            current["start"], current["all_day"] = _parse_dt(value, params.upper())
        elif name == "DTEND":
            current["end"], _ = _parse_dt(value, params.upper())
        elif name == "SUMMARY":
            current["summary"] = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").strip()
        elif name == "LOCATION":
            current["location"] = value.replace("\\,", ",").strip()
        elif name == "RRULE":
            current["rrule"] = _parse_rrule(value)
    return events


def occurrences(event: CalEvent, window_start: datetime, window_end: datetime) -> list[CalEvent]:
    """This event's occurrences inside the window — itself if single, expanded if recurring."""
    duration = (event.end - event.start) if event.end else None
    if not event.rrule:
        return [event] if window_start <= event.start < window_end else []
    freq = event.rrule.get("FREQ", "").upper()
    if freq not in ("DAILY", "WEEKLY"):
        # An unsupported recurrence (monthly/yearly): surface the base event only when it lands in the
        # window, rather than silently inventing or dropping occurrences.
        return [event] if window_start <= event.start < window_end else []
    interval = max(1, int(event.rrule.get("INTERVAL", "1") or 1))
    until, _ = _parse_dt(event.rrule.get("UNTIL", ""), "") if event.rrule.get("UNTIL") else (None, False)
    count = int(event.rrule.get("COUNT", "0") or 0)
    step_days = interval if freq == "DAILY" else 7 * interval
    bydays = None
    if freq == "WEEKLY":
        raw = event.rrule.get("BYDAY", "")
        bydays = sorted({_ICS_DAYS[d] for d in raw.split(",") if d in _ICS_DAYS} or {event.start.weekday()})
    # Iterate by PERIOD: one cursor per day (DAILY) or per week-start/Monday (WEEKLY). Anchoring WEEKLY on
    # the week-start — not on DTSTART's weekday — is what lets us break safely when the whole week is past
    # the window: an earlier BYDAY (e.g. Monday) can't be missed by a cursor sitting on a later weekday.
    if freq == "DAILY":
        cursor = event.start
    else:
        cursor = event.start - timedelta(days=event.start.weekday())  # Monday of DTSTART's week, same time
    # Fast-forward the cursor to the window when there's no COUNT (a COUNT rule must be tallied from its
    # true start). Without this, a long-standing daily event whose DTSTART is >500 days before the query
    # exhausts _MAX_EXPANSION before ever reaching the window and silently vanishes from the calendar.
    if not count and cursor < window_start:
        skip = (window_start - cursor).days // step_days
        if skip > 0:
            cursor += timedelta(days=skip * step_days)
    out: list[CalEvent] = []
    made = 0
    for _ in range(_MAX_EXPANSION):
        if cursor >= window_end or (until and cursor > until):
            break
        if freq == "DAILY":
            hits = [cursor]
        else:  # each BYDAY within this week, at DTSTART's time, never before DTSTART itself
            hits = [
                (cursor + timedelta(days=d)).replace(hour=event.start.hour, minute=event.start.minute)
                for d in bydays
            ]
            hits = [h for h in hits if h >= event.start]
        for hit in hits:
            made += 1
            if count and made > count:
                break
            if until and hit > until:
                break
            if window_start <= hit < window_end:
                out.append(
                    CalEvent(start=hit, end=(hit + duration) if duration else None,
                             summary=event.summary, location=event.location, all_day=event.all_day)
                )
        if count and made > count:
            break
        cursor += timedelta(days=step_days)
    return out


def upcoming_events(url: str, days: int = 7, *, now: datetime | None = None) -> list[CalEvent]:
    """Fetch + parse + expand: every event in [today 00:00, today+days), soonest first."""
    now = now or datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=max(1, min(31, days)))
    found: list[CalEvent] = []
    for event in parse_events(fetch_ics(url)):
        try:
            found.extend(occurrences(event, start, end))
        except Exception:  # noqa: BLE001 — one weird event must not hide the calendar
            _LOG.warning("skipping unparseable event %r", event.summary)
    return sorted(found, key=lambda e: e.start)
