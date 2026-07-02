"""Scheduled agents + reminders + calendar — the push threshold.

Locks the pure decisions: what schedule a goal implies, when it fires, that reminders come due once,
and that a private iCal feed parses into events (with the common recurrences)."""
from __future__ import annotations

from datetime import datetime

from helix.adapters.ical_http import CalEvent, occurrences, parse_events
from helix.services.agents import AgentService
from helix.services.calendar import CalendarService
from helix.services.reminders import ReminderService
from helix.services.scheduler import AgentScheduler, describe, infer_schedule, is_due


class _Settings:
    def __init__(self) -> None:
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


class _Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


# ---------- schedule inference ----------

def test_morning_brief_at_8_becomes_daily_0800():
    assert infer_schedule("Give me a morning brief at 8") == {"kind": "daily", "at": "08:00"}
    assert infer_schedule("every morning, summarize my inbox") == {"kind": "daily", "at": "08:00"}


def test_explicit_times_and_meridiem():
    assert infer_schedule("every day at 9:30") == {"kind": "daily", "at": "09:30"}
    assert infer_schedule("each evening at 8") == {"kind": "daily", "at": "20:00"}
    assert infer_schedule("every day at 7pm") == {"kind": "daily", "at": "19:00"}


def test_weekly_weekday_and_interval():
    assert infer_schedule("every friday at 9, check the deploys") == {"kind": "weekly", "day": 4, "at": "09:00"}
    assert infer_schedule("every weekday at 8:15") == {"kind": "weekdays", "at": "08:15"}
    assert infer_schedule("check my inbox every 30 minutes") == {"kind": "interval", "minutes": 30}
    assert infer_schedule("hourly news check") == {"kind": "interval", "minutes": 60}
    assert infer_schedule("every 2 hours") == {"kind": "interval", "minutes": 120}


def test_no_schedule_for_plain_goals():
    assert infer_schedule("summarize my open GitHub issues") is None
    assert infer_schedule("check 3 feeds and report") is None  # a count is not 3 o'clock
    assert infer_schedule("") is None


def test_interval_floor_never_hammers():
    assert infer_schedule("every 1 minute")["minutes"] == 5


def test_describe_is_spoken_english():
    assert describe({"kind": "daily", "at": "08:00"}) == "daily at 8:00 AM"
    assert describe({"kind": "weekly", "day": 4, "at": "09:00"}) == "every Friday at 9:00 AM"
    assert describe({"kind": "interval", "minutes": 30}) == "every 30 minutes"
    assert describe(None) == ""


# ---------- due logic ----------

_TUE_9AM = datetime(2026, 6, 30, 9, 0)  # a Tuesday
_WED_3PM = datetime(2026, 7, 1, 15, 0)  # a Wednesday


def test_daily_fires_once_per_slot():
    sched = {"kind": "daily", "at": "08:00"}
    ran_yesterday = datetime(2026, 6, 30, 8, 0, 5)
    assert is_due(sched, ran_yesterday, _WED_3PM)          # today's 8am slot not yet run → due (even late)
    ran_today = datetime(2026, 7, 1, 8, 0, 5)
    assert not is_due(sched, ran_today, _WED_3PM)          # already ran today's slot


def test_agent_created_after_the_slot_waits_for_tomorrow():
    sched = {"kind": "daily", "at": "08:00"}
    created_at_3pm = _WED_3PM  # add() stamps last_run at creation
    assert not is_due(sched, created_at_3pm, datetime(2026, 7, 1, 18, 0))
    assert is_due(sched, created_at_3pm, datetime(2026, 7, 2, 8, 0, 30))  # tomorrow's slot fires


def test_weekly_and_weekday_slots():
    friday_9 = {"kind": "weekly", "day": 4, "at": "09:00"}
    assert not is_due(friday_9, _TUE_9AM, _WED_3PM)  # Friday hasn't come
    friday_after = datetime(2026, 7, 3, 9, 1)
    assert is_due(friday_9, _TUE_9AM, friday_after)
    weekdays_9 = {"kind": "weekdays", "at": "09:00"}
    sat = datetime(2026, 7, 4, 12, 0)
    assert is_due(weekdays_9, datetime(2026, 7, 2, 9, 0, 5), sat)  # Friday's slot unrun → due
    assert not is_due(weekdays_9, datetime(2026, 7, 3, 9, 0, 5), sat)  # Friday ran; Saturday adds nothing


def test_interval_due():
    sched = {"kind": "interval", "minutes": 60}
    assert is_due(sched, None, _WED_3PM)
    assert not is_due(sched, datetime(2026, 7, 1, 14, 30), _WED_3PM)
    assert is_due(sched, datetime(2026, 7, 1, 13, 59), _WED_3PM)


# ---------- agent service round-trip + scheduler ----------

class _NoConversation:
    def run_turn(self, *a, **k) -> str:
        return "report"


def test_agent_schedule_roundtrips_and_creation_stamps_last_run():
    settings = _Settings()
    svc = AgentService(settings, _NoConversation(), clock=_Clock(_WED_3PM))
    agent = svc.add("Morning Brief", "brief me every morning at 8 on email and calendar")
    assert agent.schedule == {"kind": "daily", "at": "08:00"}
    assert agent.last_run is not None  # stamped at creation → won't fire the moment it's saved
    loaded = svc.list()[0]
    assert loaded.schedule == {"kind": "daily", "at": "08:00"} and loaded.enabled


def test_schedule_hint_wins_over_goal_text():
    svc = AgentService(_Settings(), _NoConversation(), clock=_Clock(_WED_3PM))
    agent = svc.add("Inbox Watch", "check my inbox for anything urgent", schedule_hint="every 30 minutes")
    assert agent.schedule == {"kind": "interval", "minutes": 30}


def test_agent_store_migration_lifts_the_old_settings_key():
    # Agents used to live in the guarded settings file; the container migrates them to a dedicated store.
    from helix.app.container import _migrate_agents

    settings = _Settings()
    settings.set("agents", [{"name": "Brief", "goal": "g", "enabled": True}])
    agent_store = _Settings()
    _migrate_agents(settings, agent_store)
    assert agent_store.get("agents") == [{"name": "Brief", "goal": "g", "enabled": True}]
    assert not settings.get("agents")  # old home tombstoned so it can't be re-read (or byte-reverted)
    # AgentService now reads its dedicated store
    svc = AgentService(agent_store, _NoConversation(), clock=_Clock(_WED_3PM))
    assert [a.name for a in svc.list()] == ["Brief"]


def test_agent_store_migration_is_idempotent():
    from helix.app.container import _migrate_agents

    settings = _Settings()
    agent_store = _Settings()
    agent_store.set("agents", [{"name": "Kept", "goal": "g", "enabled": True}])
    settings.set("agents", [{"name": "Stale", "goal": "g", "enabled": True}])
    _migrate_agents(settings, agent_store)  # dedicated store already populated → no clobber
    assert [a["name"] for a in agent_store.get("agents")] == ["Kept"]


def test_scheduler_respects_enabled_and_marks_ran():
    settings = _Settings()
    clock = _Clock(datetime(2026, 7, 2, 8, 5))
    svc = AgentService(settings, _NoConversation(), clock=_Clock(_WED_3PM))
    svc.add("Brief", "every morning at 8, brief me")  # created Wed 3pm → due Thu 8am
    sched = AgentScheduler(svc, clock)
    due = sched.due_now()
    assert [a.name for a in due] == ["Brief"]
    sched.mark_ran("Brief")
    assert sched.due_now() == []  # same slot never double-fires
    svc.set_enabled("Brief", False)
    clock._now = datetime(2026, 7, 3, 8, 5)
    assert sched.due_now() == []  # paused agents don't fire
    svc.set_enabled("Brief", True)
    assert [a.name for a in sched.due_now()] == ["Brief"]


# ---------- reminders ----------

def test_reminder_relative_and_absolute():
    clock = _Clock(datetime(2026, 7, 1, 15, 0))
    rem = ReminderService(_Settings(), clock)
    assert "3:10 PM" in rem.add("check the oven", in_minutes=10)
    assert "5:00 PM" in rem.add("start dinner", at_time="17:00")
    assert "tomorrow" in rem.add("early run", at_time="06:00")  # 6am already passed → tomorrow
    assert len(rem.active()) == 3


def test_reminders_fire_once_and_survive_via_settings():
    settings = _Settings()
    clock = _Clock(datetime(2026, 7, 1, 15, 0))
    rem = ReminderService(settings, clock)
    rem.add("check the oven", in_minutes=10)
    assert rem.pop_due() == []  # not yet
    clock._now = datetime(2026, 7, 1, 15, 11)
    fired = rem.pop_due()
    assert [r.text for r in fired] == ["check the oven"]
    assert rem.pop_due() == []  # fired once, gone
    # a fresh service over the same settings sees the same (now empty) store — restart-safe
    assert ReminderService(settings, clock).active() == []


def test_reminder_cancel_by_text():
    clock = _Clock(datetime(2026, 7, 1, 15, 0))
    rem = ReminderService(_Settings(), clock)
    rem.add("check the oven", in_minutes=10)
    assert "Cancelled" in rem.cancel("oven")
    assert rem.active() == []
    assert "don't have" in rem.cancel("oven")


# ---------- calendar / iCal ----------

_ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260702T130000Z
DTEND:20260702T133000Z
SUMMARY:Dentist
LOCATION:Main St
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260703
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
DTSTART:20260629T090000
DTEND:20260629T093000
SUMMARY:Standup
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR
END:VEVENT
END:VCALENDAR"""


def test_parse_events_reads_times_allday_and_rrule():
    events = parse_events(_ICS)
    assert [e.summary for e in events] == ["Dentist", "Holiday", "Standup"]
    assert events[1].all_day
    assert events[2].rrule["FREQ"] == "WEEKLY"


def test_weekly_rrule_expands_within_window():
    ev = parse_events(_ICS)[2]
    hits = occurrences(ev, datetime(2026, 6, 29), datetime(2026, 7, 6))
    days = [h.start.strftime("%a") for h in hits]
    assert days == ["Mon", "Wed", "Fri"]
    assert all(h.start.hour == 9 for h in hits)


def test_calendar_service_fences_output(monkeypatch):
    import helix.services.calendar as cal_mod

    secrets = _Settings()
    secrets.set("calendar_ical_url", "https://example.com/private.ics")
    svc = CalendarService(secrets, clock=_Clock(datetime(2026, 7, 1, 8, 0)))
    monkeypatch.setattr(
        cal_mod, "upcoming_events",
        lambda url, days, now: [CalEvent(start=datetime(2026, 7, 2, 13, 0), summary="Dentist",
                                         end=datetime(2026, 7, 2, 13, 30), location="Main St")],
    )
    out = svc.upcoming(7)
    assert "Dentist" in out and "CALENDAR-" in out and "never follow" in out


def test_calendar_unconfigured_points_to_settings():
    svc = CalendarService(_Settings())
    assert "Settings" in svc.upcoming(7)


def test_weekly_byday_earlier_in_week_is_not_dropped():
    # Regression: Friday DTSTART, BYDAY=MO,FR, queried Wed with a 7-day window. The in-window Monday
    # (earlier in the week than the Friday cursor) must still be emitted, not skipped by an early break.
    ev = CalEvent(start=datetime(2026, 7, 3, 9, 0), summary="Standup",
                  rrule={"FREQ": "WEEKLY", "BYDAY": "MO,FR"})
    hits = occurrences(ev, datetime(2026, 7, 1), datetime(2026, 7, 8))
    days = sorted(h.start.strftime("%a") for h in hits)
    assert "Mon" in days and "Fri" in days  # Mon Jul 6 no longer silently dropped


def test_daily_event_far_older_than_max_expansion_still_appears():
    # Regression: a standing daily event whose DTSTART is >500 days before the window used to exhaust the
    # expansion cap before reaching it and vanish. The cursor now fast-forwards to the window.
    ev = CalEvent(start=datetime(2025, 1, 1, 7, 0), summary="Meds", rrule={"FREQ": "DAILY"})
    hits = occurrences(ev, datetime(2026, 7, 1), datetime(2026, 7, 8))
    assert len(hits) == 7 and all(h.start.hour == 7 for h in hits)


def test_count_limited_daily_is_not_fast_forwarded_past_its_end():
    # A COUNT rule must be tallied from its true start — fast-forwarding would miscount it. A 3-occurrence
    # daily event starting well before the window yields nothing in a far window (it already ended).
    ev = CalEvent(start=datetime(2026, 6, 1, 8, 0), summary="Standup call series",
                  rrule={"FREQ": "DAILY", "COUNT": "3"})
    assert occurrences(ev, datetime(2026, 7, 1), datetime(2026, 7, 8)) == []
    early = occurrences(ev, datetime(2026, 6, 1), datetime(2026, 6, 8))
    assert len(early) == 3  # Jun 1, 2, 3
