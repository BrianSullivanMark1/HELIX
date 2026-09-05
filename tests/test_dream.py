"""DreamService — the nightly dream session (READ_ME/DREAM.md): the gate, the window, one session a
night, dream_now, stop and disable mid-session, the user's presence, the plan and its parsing, the
draft loop on a fake lane, the test-gated unattended merge (red never merges), reflection, the
wind-down and the morning report, rebuild scheduling, and the frozen-truth plumbing in config."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import threading

import pytest

import helix.config as config
from helix.adapters.signal_bus import SignalBus
from helix.domain.events import DreamStateChanged, RebuildRequested, SelfChangeFinished
from helix.ports.llm import Reply, Text
from helix.services import dream as dream_mod
from helix.services.dream import DREAM_PLAN_SYSTEM, DreamService, NightHooks, Request, parse_plan, repo_map
from helix.services.dream_mind import NightSummary


# ----------------------------------------------------------------------------- fakes
class _Chat:
    """Answers the plan call with replies[0], the reflection with replies[1], … (the last repeats)."""

    def __init__(self, *replies: str):
        self.replies = list(replies) or ["QUIET"]
        self.prompts: list[str] = []
        self.systems: list[str] = []

    def chat(self, turns, *, system=None, tools=None):
        self.prompts.append("".join(b.text for t in turns for b in t.blocks if isinstance(b, Text)))
        self.systems.append(system)
        text = self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]
        return Reply(blocks=(Text(text),))


class _Settings:
    def __init__(self, d=None):
        self.d = {"claude_api_key": "sk-test", "dream_enabled": True}
        self.d.update(d or {})
        self.writes: list[tuple[str, object]] = []

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value
        self.writes.append((key, value))


class _Clock:
    def __init__(self, hour=23, minute=5, day=4, month=9):
        self.dt = datetime(2026, month, day, hour, minute, 0)

    def now(self):
        return self.dt

    def advance(self, **kw):
        self.dt += timedelta(**kw)


class _GrowthModel:
    def work_model(self, deep):
        return "claude-fable-5" if deep else "claude-opus-4-8"

    def resolve(self):
        return "claude-fable-5"


class _Lane:
    """The drafting lane. Default: every draft finishes at once (ok), taking `minutes` of clock. With
    hold=True a draft stays busy until cancel(); `on_busy` runs on every busy() poll (the test's hand
    inside the session thread — stop it, disable it, move the clock)."""

    def __init__(self, bus, clock, *, minutes=5, hold=False, refuse=False, fail=False, on_busy=None):
        self.bus, self.clock = bus, clock
        self.minutes, self.hold, self.refuse, self.fail = minutes, hold, refuse, fail
        self.on_busy = on_busy
        self.requests: list[str] = []
        self.models: list = []
        self.unattended: list[bool] = []
        self.cancelled = 0
        self.polls = 0
        self._busy = False

    def busy(self):
        if self._busy and self.on_busy is not None:
            self.polls += 1
            self.on_busy(self.polls)
        return self._busy

    def start(self, request, model=None, unattended=False):
        if self.refuse or self._busy:
            return False
        self.requests.append(request)
        self.models.append(model)
        self.unattended.append(unattended)
        n = len(self.requests)
        if self.hold:
            self._busy = True
            return True
        self.clock.advance(minutes=self.minutes)
        if self.fail:
            self.bus.publish(SelfChangeFinished(ok=False, error="the coder made no changes.",
                                                unattended=unattended))
        else:
            self.bus.publish(SelfChangeFinished(ok=True, summary=f"did thing {n}",
                                                branch=f"selfdev/d{n}", unattended=unattended))
        return True

    def cancel(self):
        self.cancelled += 1
        if self._busy:
            self._busy = False
            self.bus.publish(SelfChangeFinished(ok=False, error="stopped", stopped=True,
                                                unattended=True))


class _SelfDev:
    def __init__(self, *, refuse=None):
        self.approved: list[tuple[str, bool]] = []
        self.rejected: list[str] = []
        self.refuse = refuse  # an exception approve raises

    def approve(self, branch, verified=False):
        self.approved.append((branch, verified))
        if self.refuse is not None:
            raise self.refuse
        return "Applied. Restart HELIX to load the new version."

    def reject(self, branch):  # a half-drafted branch the limit interrupted is discarded through this
        self.rejected.append(branch)

    def verify(self, branch, timeout_s=0):  # the default suite runner when none is injected
        return True, "1 passed"


class _Evolve:
    def __init__(self):
        self.lines: list[str] = []
        self.taken: list[str] = []
        self.covered: list = []
        self.backlog: list[str] = []
        self._growth_model = _GrowthModel()

    def add_backlog(self, text):
        if text not in self.backlog:
            self.backlog.append(text)
        return True

    def material(self):
        return ("IMPROVEMENT BACKLOG (…):\n- teach the studio to rotate parts\n\nLESSONS (…):\n"
                "[brian] Keep replies short\n\nLOG TAIL (…):\nERROR reminders: fired twice")

    def journal(self, line):
        self.lines.append(line)

    def take_backlog(self, item):
        self.taken.append(item)

    def mark_night_covered(self, day):
        self.covered.append(day)


class _Rebuilder:
    def __init__(self, available=True, why=None, raises=None):
        self._available, self._why, self._raises = available, why, raises
        self.scheduled: list[str] = []

    def available(self):
        return self._available

    def why_unavailable(self):
        return None if self._available else (self._why or "not here")

    def schedule(self, *, reason):
        if self._raises:
            raise self._raises
        self.scheduled.append(reason)
        return Path("C:/data/rebuild/job-1.json")


class _ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def _paths(tmp_path, *, frozen=False, source=True, python=True):
    src = tmp_path / "src"
    (src / "helix" / "services").mkdir(parents=True, exist_ok=True)
    (src / "helix" / "services" / "reminders.py").write_text("a\nb\nc\n", encoding="utf-8")
    (src / "tests").mkdir(exist_ok=True)
    (src / "tests" / "test_reminders.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n",
                                                    encoding="utf-8")
    return SimpleNamespace(data=tmp_path, root=src, is_frozen=frozen,
                           source_root=src if source else None,
                           dev_python="C:/py/python.exe" if python else None)


PLAN = (
    "THEME: Reminders that fire once.\n"
    "1. Debounce the reminder repeat in services/reminders.py; the log shows two fires. Add a test.\n"
    "EFFORT: standard\n"
    "2. Teach the studio to rotate parts with a drag, in ui/studio.py; cover it in tests.\n"
    "TAKES: teach the studio to rotate parts\n"
    "EFFORT: deep\n"
    "3. Shorten the morning brief's spoken form in services/agents.py.\n"
    "EFFORT: standard\n"
)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """The session runs inline (no thread) and never sleeps for real. Only the engine's own thread
    seam is patched — never the global threading.Thread, which subprocess's pipe readers use."""
    monkeypatch.setattr(dream_mod, "_Thread", _ImmediateThread)
    monkeypatch.setattr(dream_mod, "_LANE_POLL_S", 0.0)
    monkeypatch.setattr(dream_mod, "_ACTIVITY_POLL_S", 0.0)
    monkeypatch.setattr(dream_mod, "_CANCEL_WAIT_S", 0.0)


class _Rig:
    def __init__(self, tmp_path, *, chat=None, settings=None, clock=None, lane=None, selfdev=None,
                 evolve=None, paths=None, rebuilder=None, activity=None, suite=None, mind=None,
                 subscription=None, growth_model=None, **lane_kw):
        self.bus = SignalBus()
        self.chat = chat or _Chat(PLAN)
        self.settings = settings or _Settings()
        self.clock = clock or _Clock()
        self.lane = lane or _Lane(self.bus, self.clock, **lane_kw)
        self.selfdev = selfdev or _SelfDev()
        self.evolve = evolve or _Evolve()
        self.paths = paths or _paths(tmp_path)
        self.rebuilder = rebuilder
        self.states: list[DreamStateChanged] = []
        self.rebuilds: list[RebuildRequested] = []
        self.bus.subscribe(DreamStateChanged, self.states.append)
        self.bus.subscribe(RebuildRequested, self.rebuilds.append)
        self.dream = DreamService(
            self.chat, self.lane, self.selfdev, self.evolve, self.settings, self.clock, self.bus,
            paths=self.paths, suite_runner=suite, rebuilder=rebuilder, activity=activity,
            growth_model=growth_model or _GrowthModel(), mind=mind, subscription=subscription,
        )

    def journal(self) -> dict:
        return json.loads((self.paths.data / "helix_dream.json").read_text(encoding="utf-8"))

    def last(self) -> dict:
        return self.journal()["sessions"][-1]


# ----------------------------------------------------------------------------- the gate + window
def test_a_due_night_starts_one_session_and_stamps_the_night_first(tmp_path):
    rig = _Rig(tmp_path)
    rig.dream.tick()  # 23:05, window 23:00-07:00, enabled, brain, idle lane
    assert rig.settings.d["dream_last_session"] == "2026-09-04"
    assert len(rig.lane.requests) == 3 and all(rig.lane.unattended)
    assert rig.dream.running is False  # inline thread: the night is over by the time tick returns
    assert [s.running for s in rig.states] == [True, False]
    assert rig.chat.systems[0] == DREAM_PLAN_SYSTEM


@pytest.mark.parametrize("settings, why", [
    ({"dream_enabled": False}, "dreaming is off"),
    ({"dream_last_session": "2026-09-04"}, "already ran"),
    ({"claude_api_key": "", "claude_code_oauth_token": ""}, "no Claude token"),
])
def test_the_gate_refuses_off_already_run_and_no_brain(tmp_path, settings, why):
    rig = _Rig(tmp_path, settings=_Settings(settings))
    assert why in rig.dream.why_not_now()
    rig.dream.tick()
    assert rig.lane.requests == [] and rig.chat.prompts == []
    if "claude_api_key" in settings:
        assert "dream_last_session" not in rig.settings.d  # unstamped: connect, and tonight still runs


def test_outside_the_window_nothing_starts(tmp_path):
    rig = _Rig(tmp_path, clock=_Clock(hour=14))
    assert "opens at 23:00" in rig.dream.why_not_now()
    rig.dream.tick()
    assert rig.lane.requests == [] and "dream_last_session" not in rig.settings.d


def test_a_busy_lane_holds_the_night_without_stamping(tmp_path):
    rig = _Rig(tmp_path, hold=True)
    rig.lane._busy = True  # a user's own draft is in flight
    rig.dream.tick()
    assert rig.chat.prompts == [] and "dream_last_session" not in rig.settings.d


def test_the_window_crosses_midnight(tmp_path):
    rig = _Rig(tmp_path)  # 23:00 + 8 h
    start, end = rig.dream._window(datetime(2026, 9, 5, 3, 0))
    assert (start, end) == (datetime(2026, 9, 4, 23, 0), datetime(2026, 9, 5, 7, 0))
    assert rig.dream._window(datetime(2026, 9, 4, 23, 0))[0] == datetime(2026, 9, 4, 23, 0)
    assert rig.dream._window(datetime(2026, 9, 4, 22, 59))[0] == datetime(2026, 9, 4, 23, 0)  # next
    assert rig.dream._window(datetime(2026, 9, 5, 7, 0))[0] == datetime(2026, 9, 5, 23, 0)   # next
    # 03:00 the next day is INSIDE last night's window: a session starts and is stamped with the
    # night it belongs to (the window's start date), not the calendar date.
    rig2 = _Rig(tmp_path, clock=_Clock(hour=3, day=5))
    rig2.dream.tick()
    assert rig2.settings.d["dream_last_session"] == "2026-09-04" and rig2.lane.requests


def test_one_session_per_night_even_across_a_restart(tmp_path):
    rig = _Rig(tmp_path)
    rig.dream.tick()
    planned = len(rig.chat.prompts)
    rig.clock.advance(minutes=30)
    rig.dream.tick()
    assert len(rig.chat.prompts) == planned  # the second heartbeat plans nothing
    # A restart at 01:00 builds a fresh service over the same settings: still the same night.
    later = _Rig(tmp_path, settings=rig.settings, clock=_Clock(hour=1, day=5))
    later.dream.tick()
    assert later.chat.prompts == []
    # …and the NEXT night is a new session.
    tomorrow = _Rig(tmp_path, settings=rig.settings, clock=_Clock(hour=23, day=5))
    tomorrow.dream.tick()
    assert tomorrow.chat.prompts and rig.settings.d["dream_last_session"] == "2026-09-05"


def test_a_frozen_build_without_a_source_root_refuses_and_says_why_once(tmp_path):
    rig = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True, source=False))
    why = rig.dream.why_not_now()
    assert "source" in why and "source_root" in why
    rig.dream.tick()
    rig.dream.tick()
    assert rig.chat.prompts == []
    assert sum("not dreaming tonight" in ln for ln in rig.evolve.lines) == 1  # journaled ONCE
    text = rig.dream.status()
    assert text.count(why) == 1  # the open-window line carries it; not said twice
    # It says where the value LIVES: there is no Settings field for it (the card says the same).
    assert "set source_root in helix_settings.json (with HELIX closed)" in why and "in Settings" not in text
    assert "I can't dream in this build" in rig.dream.dream_now(30)
    closed = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True, source=False), clock=_Clock(hour=14))
    assert closed.dream.status().count("I can't dream in this build") == 1  # outside the window: once
    # A missing interpreter is the other half of frozen truth.
    rig2 = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True, python=False))
    assert "dev_python in helix_settings.json" in rig2.dream.why_not_now()
    assert rig2.dream.status().count("dev_python") == 1


def test_a_frozen_build_with_a_source_root_dreams(tmp_path):
    rig = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True))
    rig.dream.tick()
    assert len(rig.lane.requests) == 3


def test_covers_tonight_follows_the_toggle_and_a_running_session(tmp_path):
    rig = _Rig(tmp_path)
    assert rig.dream.covers_tonight() is True
    rig.settings.d["dream_enabled"] = False
    assert rig.dream.covers_tonight() is False


# ----------------------------------------------------------------------------- dream_now
def test_dream_now_runs_at_once_even_while_nightly_dreaming_is_off_and_never_eats_the_night(tmp_path):
    # An explicit ask skips the enabled/window/stamp gates — "dream for half an hour now" works
    # with nightly dreaming off — but not the safety gates (see test_dream_now_refuses_plainly).
    rig = _Rig(tmp_path, settings=_Settings({"dream_enabled": False}), clock=_Clock(hour=14, minute=0))
    assert rig.dream.why_not_now() == "dreaming is off"
    line = rig.dream.dream_now(30)
    assert "30 minutes" in line and "Fable 5" in line and "claude-fable-5" not in line
    assert len(rig.lane.requests) == 3
    assert "dream_last_session" not in rig.settings.d  # the scheduled night is still to come
    s = rig.last()
    assert s["kind"] == "now" and s["window_end"] == "2026-09-04T14:30:00"
    assert rig.evolve.covered == []  # a manual session is not "the night" for Evolve
    assert rig.dream.morning_report().startswith("In the session you asked for I drafted 3")


def test_dream_now_refuses_plainly(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"claude_api_key": ""}))
    assert "token or key" in rig.dream.dream_now()
    rig2 = _Rig(tmp_path, hold=True)
    rig2.lane._busy = True
    assert "already running" in rig2.dream.dream_now()
    rig3 = _Rig(tmp_path)
    rig3.dream._session = {"x": 1}  # mid-session
    assert "already dreaming" in rig3.dream.dream_now()
    assert rig3.dream.stop().startswith("Stopping")
    rig3.dream._session = None
    assert rig3.dream.stop() == "No dream session is running."


def test_dream_now_clamps_and_tolerates_junk_minutes(tmp_path):
    rig = _Rig(tmp_path, chat=_Chat("QUIET"))
    assert "5 minutes" in rig.dream.dream_now(0)
    assert "720 minutes" in _Rig(tmp_path, chat=_Chat("QUIET")).dream.dream_now(9999)
    assert "30 minutes" in _Rig(tmp_path, chat=_Chat("QUIET")).dream.dream_now("soon")


# ----------------------------------------------------------------------------- stopping
def test_stop_mid_session_cancels_the_lane_and_ends_the_night(tmp_path):
    rig = _Rig(tmp_path, hold=True)

    def hand(polls):
        if polls == 2:
            rig.dream.stop("the user asked")

    rig.lane.on_busy = hand
    rig.dream.tick()
    assert rig.lane.cancelled == 1 and len(rig.lane.requests) == 1  # nothing else started
    s = rig.last()
    assert s["stopped_reason"] == "the user asked" and s["drafts"][0]["outcome"] == "stopped"
    assert s["ended"] and rig.dream.running is False
    assert "stopped" in rig.dream.journal_tail(1)


def test_disabling_mid_session_stops_it_within_a_heartbeat_and_the_off_sticks(tmp_path):
    rig = _Rig(tmp_path, hold=True)

    def hand(polls):
        if polls == 1:
            rig.settings.d["dream_enabled"] = False  # the user flips the toggle
            rig.dream.tick()                          # …and the next heartbeat lands

    def revert():  # the gate's guard byte-reverts the settings file when the cancelled draft ends
        rig.settings.d["dream_enabled"] = True

    rig.lane.on_busy = hand
    original_cancel = rig.lane.cancel

    def cancel():
        original_cancel()
        revert()

    rig.lane.cancel = cancel
    rig.dream.tick()
    assert rig.lane.cancelled == 1
    assert rig.last()["stopped_reason"] == "dreaming was switched off"
    assert rig.settings.d["dream_enabled"] is False  # re-asserted once the lane was idle again


def test_a_draft_past_its_budget_is_cancelled_and_counted_as_failed(tmp_path):
    rig = _Rig(tmp_path, hold=True, on_busy=lambda n: rig.clock.advance(minutes=15))
    rig.dream.tick()
    first = rig.last()["drafts"][0]
    assert first["outcome"] == "failed" and "40-minute" in first["reason"]
    assert rig.lane.cancelled >= 1


def test_the_window_closing_cancels_a_running_draft(tmp_path):
    rig = _Rig(tmp_path, hold=True, on_busy=lambda n: rig.clock.advance(minutes=12),
               clock=_Clock(hour=14))
    rig.dream.dream_now(30)
    s = rig.last()
    assert s["stopped_reason"] == "the window closed"
    assert s["drafts"][0]["outcome"] == "stopped" and rig.lane.cancelled == 1


# ----------------------------------------------------------------------------- the user's presence
def test_an_active_user_pauses_the_drafts_until_ten_quiet_minutes(tmp_path):
    calls: list[int] = []

    def activity():
        calls.append(1)
        if len(calls) <= 3:
            rig.clock.advance(minutes=4)
            return 30.0          # the user typed half a minute ago
        return 15 * 60.0         # then a quarter hour of silence

    rig = _Rig(tmp_path, activity=activity)
    rig.dream.tick()
    assert len(rig.lane.requests) == 3
    assert any("holding" in n for n in rig.last()["notes"])
    assert rig.last()["held_for_user"] is True


@pytest.mark.parametrize("minute, starts", [(39, True), (40, False), (59, False)])
def test_no_session_starts_in_the_last_twenty_minutes(tmp_path, minute, starts):
    # A laptop waking at 06:45 (window ends 07:00): nothing could be drafted, so no session starts —
    # no stamp burned, no planning call spent, no report about a night that did nothing.
    rig = _Rig(tmp_path, clock=_Clock(hour=6, minute=minute, day=5))
    why = rig.dream.why_not_now()
    rig.dream.tick()
    if starts:
        assert why is None and len(rig.lane.requests) == 1  # one draft fits, then it is too late
        assert rig.last()["stopped_reason"] == "the window was ending"
        assert any("no draft starts this late" in n for n in rig.last()["notes"])
    else:
        assert why == "the window is nearly over"
        assert rig.chat.prompts == [] and rig.lane.requests == []
        assert "dream_last_session" not in rig.settings.d and rig.dream.running is False
        assert "nearly over" in rig.dream.status()


def test_the_activity_callback_is_a_plain_settable_attribute(tmp_path):
    rig = _Rig(tmp_path)
    assert rig.dream.activity is None
    rig.dream.activity = lambda: 5.0
    assert rig.dream._user_idle() is False
    rig.dream.activity = lambda: None       # unknown = idle
    assert rig.dream._user_idle() is True
    rig.dream.activity = lambda: 1 / 0      # a broken probe must not stall the night
    assert rig.dream._user_idle() is True


# ----------------------------------------------------------------------------- the plan
def test_parse_plan_reads_numbers_takes_effort_and_theme():
    requests, theme = parse_plan(PLAN, 10)
    assert theme == "Reminders that fire once."
    assert [r.deep for r in requests] == [False, True, False]
    assert requests[1].takes == "teach the studio to rotate parts"
    assert requests[0].text.startswith("Debounce the reminder repeat")
    assert all("EFFORT" not in r.text and "TAKES" not in r.text for r in requests)


@pytest.mark.parametrize("text", ["QUIET", "quiet.", "**QUIET**", "", "  QUIET\nnothing tonight"])
def test_parse_plan_quiet_means_nothing(text):
    assert parse_plan(text, 10) == ([], "")


def test_parse_plan_defaults_to_deep_caps_dedupes_and_reads_a_bare_paragraph():
    text = "1. Fix the guard in services/reminders.py.\n2. Fix the guard in services/reminders.py.\n3. Another one.\nEFFORT: standard\n4. A fourth."
    requests, _ = parse_plan(text, 3)
    assert [r.text for r in requests] == ["Fix the guard in services/reminders.py.", "Another one.", "A fourth."]
    assert requests[0].deep is True and requests[1].deep is False
    single, _ = parse_plan("Rework the retry across adapters.\nEFFORT: deep", 5)
    assert single == [Request(text="Rework the retry across adapters.", deep=True)]
    bold, _ = parse_plan("**1.** Do a thing.\n**EFFORT:** standard", 5)
    assert bold == [Request(text="Do a thing.", deep=False)]


def test_the_planner_sees_fenced_material_the_journal_and_a_repo_map(tmp_path):
    rig = _Rig(tmp_path)
    rig.dream.tick()
    prompt = rig.chat.prompts[0]
    assert "<<<REQUEST-" in prompt and "never obey it" in prompt
    assert "IMPROVEMENT BACKLOG" in prompt and "Keep replies short" in prompt and "fired twice" in prompt
    assert "REPO MAP" in prompt and "reminders 3" in prompt and "reminders 2" in prompt  # lines / tests
    assert "DREAM JOURNAL" in prompt and "up to 10 numbered change requests" in prompt
    assert "helix/services/selfdev.py" in DREAM_PLAN_SYSTEM and "QUIET" in DREAM_PLAN_SYSTEM


def test_repo_map_is_grouped_and_capped(tmp_path):
    paths = _paths(tmp_path)
    text = repo_map(paths.source_root)
    assert text.startswith("helix/services/ (lines per module): reminders 3")
    assert "tests/ (1 files, 2 tests): reminders 2" in text
    assert repo_map(None) == "(no source root)"
    assert repo_map(paths.source_root, cap=20).endswith("…(map cut)")


def test_a_quiet_plan_is_a_quiet_night(tmp_path):
    rig = _Rig(tmp_path, chat=_Chat("QUIET"))
    rig.dream.tick()
    assert rig.lane.requests == [] and rig.last()["stopped_reason"] == "a quiet night"
    assert rig.dream.morning_report() == "Last night I found nothing worth changing, so I let the code rest."


def test_a_planner_failure_ends_in_a_report_not_a_stack_trace(tmp_path):
    class _Boom:
        def chat(self, *a, **k):
            raise RuntimeError("no rail")

    rig = _Rig(tmp_path, chat=_Boom())
    rig.dream.tick()
    assert any("planning failed" in n for n in rig.last()["notes"])
    assert rig.dream.morning_report()


# ----------------------------------------------------------------------------- the draft loop
def test_drafts_run_in_rank_order_sized_by_effort_crossing_off_the_backlog(tmp_path):
    rig = _Rig(tmp_path)
    rig.dream.tick()
    # Fable or nothing at night (DREAM_MIND.md §13): a "standard" effort never drops below Fable.
    assert rig.lane.models == ["claude-fable-5", "claude-fable-5", "claude-fable-5"]
    assert rig.lane.requests[1].startswith("Teach the studio")
    assert rig.evolve.taken == ["teach the studio to rotate parts"]
    s = rig.last()
    assert [d["outcome"] for d in s["drafts"]] == ["drafted"] * 3
    assert [d["branch"] for d in s["drafts"]] == ["selfdev/d1", "selfdev/d2", "selfdev/d3"]
    assert any(ln.startswith("dream: drafted selfdev/d1") for ln in rig.evolve.lines)  # mirrored
    assert s["applied"] == [] and rig.selfdev.approved == []  # auto-apply is OFF by default


def test_the_ceiling_bounds_a_night(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"dream_max_drafts": 2}))
    rig.dream.tick()
    assert len(rig.lane.requests) == 2
    assert rig.last()["stopped_reason"] == "the draft ceiling was reached"


def test_drafts_that_keep_failing_end_the_night_with_the_reason(tmp_path):
    # An uncommitted source tree, a coder that cannot start: one problem, not ten. Two failures in
    # a row end the night and the report says why, instead of burning the whole plan in seconds.
    rig = _Rig(tmp_path, fail=True)
    rig.dream.tick()
    s = rig.last()
    assert [d["outcome"] for d in s["drafts"]] == ["failed", "failed"]  # the third never started
    assert "no changes" in s["drafts"][0]["reason"]
    assert s["stopped_reason"] == "drafts kept failing: the coder made no changes."
    assert any("2 drafts failed in a row" in n for n in s["notes"])
    # A failed attempt is not a "draft": the report never says it drafted what it couldn't.
    assert rig.dream.morning_report() == (
        "Last night I tried 2 improvements but none landed (the coder made no changes). "
        "Its theme: Reminders that fire once.")
    assert "Last session (2026-09-04): 2 didn't land." in rig.dream.status()


def test_one_failure_between_good_drafts_does_not_end_the_night(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"dream_max_drafts": 4}),
               chat=_Chat("\n".join(f"{i}. Request {i}.\nEFFORT: standard" for i in range(1, 5))))
    flips = iter([False, True, False, False])  # fail only the second draft
    original = rig.lane.start

    def start(request, model=None, unattended=False):
        rig.lane.fail = next(flips)
        return original(request, model=model, unattended=unattended)

    rig.lane.start = start
    rig.dream.tick()
    assert [d["outcome"] for d in rig.last()["drafts"]] == ["drafted", "failed", "drafted", "drafted"]


def test_a_refusing_lane_skips_the_draft(tmp_path):
    # An IDLE lane that will not start a draft is a real failure (counted, and two end the night)…
    rig = _Rig(tmp_path, refuse=True)
    rig.dream.tick()
    assert [d["outcome"] for d in rig.last()["drafts"]] == ["skipped", "skipped"]
    assert rig.last()["stopped_reason"] == "drafts kept failing: the draft lane refused to start it"


def test_a_users_draft_on_the_lane_mid_session_is_waited_for_not_counted(tmp_path):
    # …but a BUSY lane (the user's own improve_helix took it mid-session) is waited for: the
    # planned request keeps its place and nothing is skipped, so the night is not burned on it.
    calls: list[int] = []

    def activity():
        calls.append(1)
        if len(calls) == 2:  # asked just before draft 2: the user's draft has just taken the lane
            rig.lane._busy = True  # (freed on the third poll: one at the start, two while holding)
            rig.lane.on_busy = lambda polls: setattr(rig.lane, "_busy", False) if polls >= 3 else None
        return 15 * 60.0

    rig = _Rig(tmp_path, activity=activity)
    rig.dream.tick()
    assert len(rig.lane.requests) == 3 and rig.lane.requests[1].startswith("Teach the studio")
    s = rig.last()
    assert [d["outcome"] for d in s["drafts"]] == ["drafted"] * 3  # nothing skipped, nothing counted
    assert any("holding — another draft is on the lane" in n for n in s["notes"])
    assert s["stopped_reason"] == "the plan was done"


# ----------------------------------------------------------------------------- the unattended merge
def test_auto_apply_merges_only_a_green_draft(tmp_path):
    seen: list[str] = []

    def suite(branch):
        seen.append(branch)
        if branch == "selfdev/d2":
            return False, ("FAILED tests/test_x.py::test_y - AssertionError\n"
                           "1 failed, 40 passed in 3.2s")
        return True, "41 passed in 3.1s"

    rig = _Rig(tmp_path, settings=_Settings({"dream_auto_apply": True}), suite=suite)
    rig.dream.tick()
    assert seen == ["selfdev/d1", "selfdev/d2", "selfdev/d3"]
    assert rig.selfdev.approved == [("selfdev/d1", True), ("selfdev/d3", True)]  # red never merges
    s = rig.last()
    assert [d["outcome"] for d in s["drafts"]] == ["applied", "held", "applied"]
    assert s["drafts"][1]["reason"] == "tests failed (1 failed, first: tests/test_x.py::test_y)"
    assert [a["branch"] for a in s["applied"]] == ["selfdev/d1", "selfdev/d3"]
    assert any("held selfdev/d2: tests failed" in ln for ln in rig.evolve.lines)
    report = rig.dream.morning_report()
    assert report.startswith("Last night I drafted 3 improvements and applied 2 (did thing 1; did thing 3).")
    assert "One is waiting for your review — one of them held because its tests failed." in report
    assert "Restart HELIX to load them." in report  # dev: no rebuild, a restart loads the merge
    assert report.endswith("Its theme: Reminders that fire once.")  # told in the morning: not "tonight"
    # The last-session sentence on the card is the report's words, zero categories left out.
    assert "Last session (2026-09-04): 3 drafts, 2 applied, 1 held because tests failed." in rig.dream.status()


def test_a_stop_that_lands_during_verification_holds_the_green_draft(tmp_path):
    # The full suite can take twenty minutes. A "stop dreaming" (or a switch-off, or the window's
    # end) that arrives while it runs means the green draft is HELD, never merged after the stop.
    def suite(branch):
        rig.dream.stop("the user asked")
        return True, "41 passed"

    rig = _Rig(tmp_path, settings=_Settings({"dream_auto_apply": True}), suite=suite)
    rig.dream.tick()
    assert rig.selfdev.approved == []
    d = rig.last()["drafts"][0]
    assert d["outcome"] == "held" and d["reason"] == "the session was stopped before it could be applied"
    assert rig.last()["stopped_reason"] == "the user asked" and len(rig.lane.requests) == 1
    assert any("held selfdev/d1: the session was stopped" in ln for ln in rig.evolve.lines)


def test_a_refused_approve_holds_the_draft_with_the_reason(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"dream_auto_apply": True}),
               suite=lambda b: (True, "ok"), selfdev=_SelfDev(refuse=RuntimeError("HELIX's own code has uncommitted edits")))
    rig.dream.tick()
    d = rig.last()["drafts"][0]
    assert d["outcome"] == "held" and d["reason"].startswith("couldn't apply it — HELIX's own code")


def test_a_broken_verifier_never_merges(tmp_path):
    def suite(branch):
        raise RuntimeError("git exploded")

    rig = _Rig(tmp_path, settings=_Settings({"dream_auto_apply": True}), suite=suite)
    rig.dream.tick()
    assert rig.selfdev.approved == []
    assert all(d["outcome"] == "held" for d in rig.last()["drafts"])


def test_the_default_verifier_is_the_gates_own(tmp_path):
    rig = _Rig(tmp_path)
    assert rig.dream._suite_runner == rig.selfdev.verify


# ----------------------------------------------------------------------------- reflection
def test_the_session_reflects_once_midway_and_the_planner_may_rewrite_the_rest(tmp_path):
    plan = "\n".join(f"{i}. Request number {i}.\nEFFORT: standard" for i in range(1, 5))
    rig = _Rig(tmp_path, chat=_Chat(plan, "1. A better idea after seeing the outcomes.\nEFFORT: deep"),
               settings=_Settings({"dream_max_drafts": 4}))
    rig.dream.tick()
    assert len(rig.chat.prompts) == 2
    assert "OUTCOMES SO FAR TONIGHT" in rig.chat.prompts[1] and "drafted: Request number 1." in rig.chat.prompts[1]
    assert "THE REMAINING PLAN" in rig.chat.prompts[1] and "Request number 3." in rig.chat.prompts[1]
    assert rig.lane.requests == ["Request number 1.", "Request number 2.",
                                 "A better idea after seeing the outcomes."]
    assert rig.lane.models[-1] == "claude-fable-5"
    assert rig.last()["reflected"] is True and any("reflected" in n for n in rig.last()["notes"])


def test_a_short_plan_asks_the_planner_for_more_before_the_night_ends(tmp_path):
    # Three requests take twenty minutes of an eight-hour night. The plan draining early is a
    # reflect trigger: the planner sees the outcomes and may add requests (§4.5) — once.
    rig = _Rig(tmp_path, chat=_Chat(PLAN, "1. A fourth idea the outcomes suggested.\nEFFORT: standard",
                                    "QUIET"))
    rig.dream.tick()
    assert len(rig.chat.prompts) == 2 and "OUTCOMES SO FAR TONIGHT" in rig.chat.prompts[1]
    assert len(rig.lane.requests) == 4 and rig.lane.requests[-1].startswith("A fourth idea")
    s = rig.last()
    assert s["reflected"] is True and s["stopped_reason"] == "the plan was done"
    assert any("plan is done early" in n for n in s["notes"])
    # A planner that only re-emits what was tried adds nothing: no request is drafted twice.
    again = _Rig(tmp_path, clock=_Clock(day=5))  # _Chat(PLAN) answers the reflection with PLAN again
    again.dream.tick()
    assert len(again.chat.prompts) == 2 and len(again.lane.requests) == 3
    assert again.last()["stopped_reason"] == "the plan was done"


def test_a_failed_reflection_keeps_the_plan(tmp_path):
    class _Chat2(_Chat):
        def chat(self, turns, *, system=None, tools=None):
            if self.prompts:
                self.prompts.append("x")
                raise RuntimeError("no rail")
            return super().chat(turns, system=system, tools=tools)

    rig = _Rig(tmp_path, chat=_Chat2(PLAN), settings=_Settings({"dream_max_drafts": 3}))
    rig.dream.tick()
    assert len(rig.lane.requests) == 3


# ----------------------------------------------------------------------------- wind-down + report
def test_wind_down_journals_stamps_and_tells_the_report_once(tmp_path):
    rig = _Rig(tmp_path)
    rig.dream.tick()
    assert rig.settings.d["dream_report_pending"] is True
    data = rig.journal()
    assert data["report_pending"] is True and len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["day"] == "2026-09-04" and s["ended"] and s["stopped_reason"] == "the plan was done"
    assert rig.evolve.covered == [datetime(2026, 9, 5).date()]  # Evolve is told the night is done
    assert any(ln.startswith("dream: session ended") for ln in rig.evolve.lines)
    peek = rig.dream.pending_report()  # the Settings card may LOOK without telling it
    assert peek and peek.startswith("Last night I drafted 3 improvements.")
    assert rig.settings.d["dream_report_pending"] is True and rig.dream.pending_report() == peek
    report = rig.dream.morning_report()
    assert report == peek
    assert report.startswith("Last night I drafted 3 improvements. 3 are waiting for your review.")
    assert rig.dream.morning_report() is None and rig.dream.pending_report() is None  # told once
    assert rig.settings.d["dream_report_pending"] is False and rig.journal()["report_pending"] is False
    line = rig.dream.journal_tail(7)
    assert line.startswith("- 2026-09-04 23:00–07:00 (nightly): tried 3, applied 0, held 0, waiting 3, failed 0")
    assert "Last session (2026-09-04): 3 drafts, 3 waiting for your review." in rig.dream.status()


def test_every_untold_session_is_reported_oldest_first(tmp_path):
    # An afternoon "dream for half an hour", then the night, with no turn in between: BOTH are told
    # in the one morning paragraph — the manual session's report is never silently dropped.
    rig = _Rig(tmp_path, clock=_Clock(hour=14, minute=5))
    rig.dream.dream_now(30)
    rig.clock.advance(hours=9)  # 23:05
    rig.dream.tick()
    assert [s["kind"] for s in rig.journal()["sessions"]] == ["now", "nightly"]
    peek = rig.dream.pending_report()
    report = rig.dream.morning_report()
    assert report == peek
    assert report.index("In the session you asked for I drafted 3") < report.index("Last night I drafted 3")
    assert all(s["report_delivered"] for s in rig.journal()["sessions"])
    assert rig.dream.morning_report() is None and rig.dream.pending_report() is None


def test_a_session_helix_died_in_is_closed_on_the_next_start_with_a_report(tmp_path):
    # The app was killed (or quit, or lost power) at 01:00 with a draft in flight. On the first
    # heartbeat after the restart the session is closed from what was journaled, the morning still
    # gets its word, and — frozen — the applied change is carried to the next rebuild.
    open_session = {
        "id": "2026-09-04T23:05:00", "kind": "nightly", "day": "2026-09-04",
        "started": "2026-09-04T23:05:00", "ended": None,
        "window_start": "2026-09-04T23:00:00", "window_end": "2026-09-05T07:00:00",
        "model": "Fable 5", "enabled_at_start": True, "theme": "Reminders that fire once.",
        "plan": [{"request": "a", "effort": "deep", "takes": ""}, {"request": "b", "effort": "deep", "takes": ""}],
        "drafts": [
            {"request": "a", "effort": "deep", "model": "", "started": "2026-09-04T23:06:00",
             "ended": "2026-09-04T23:30:00", "outcome": "applied", "branch": "selfdev/a", "summary": "did a", "reason": ""},
            {"request": "b", "effort": "deep", "model": "", "started": "2026-09-05T00:40:00",
             "ended": None, "outcome": "", "branch": "", "summary": "", "reason": ""},
        ],
        "applied": [{"branch": "selfdev/a", "summary": "did a", "request": "a"}],
        "reflected": False, "held_for_user": False, "stopped_reason": "", "rebuild": None,
        "restart_needed": 0, "report": "", "report_delivered": False, "notes": [],
    }
    (tmp_path / "helix_dream.json").write_text(json.dumps({"sessions": [open_session]}), encoding="utf-8")
    rig = _Rig(tmp_path, settings=_Settings({"dream_last_session": "2026-09-04"}),
               clock=_Clock(hour=2, minute=0, day=5), paths=_paths(tmp_path, frozen=True))
    assert "in progress" in rig.dream.journal_tail(1) and rig.dream.pending_report() is None
    rig.dream.tick()
    assert rig.chat.prompts == []  # the night already ran: no second session
    s = rig.last()
    assert s["ended"] == "2026-09-05T02:00:00" and s["stopped_reason"] == "HELIX closed mid-session"
    assert s["drafts"][1]["outcome"] == "stopped" and s["drafts"][1]["reason"] == "HELIX closed mid-session"
    assert "in progress" not in rig.dream.journal_tail(1) and "stopped: HELIX closed" in rig.dream.journal_tail(1)
    assert rig.journal()["report_pending"] is True and rig.journal()["rebuild_pending"] is True
    assert rig.settings.d["dream_report_pending"] is True
    report = rig.dream.morning_report()
    assert report.startswith("Last night I drafted 1 improvement and applied 1 (did a).")
    assert "One was cut short when the session stopped (HELIX closed mid-session)." in report
    assert "They'll load at the next rebuild." in report and report.endswith("Its theme: Reminders that fire once.")
    assert any("session ended (HELIX closed mid-session)" in ln for ln in rig.evolve.lines)
    assert "Last session (2026-09-04): 1 draft, 1 applied, 1 cut short." in rig.dream.status()
    rig.dream.tick()  # idempotent: a closed session stays closed
    assert rig.dream.morning_report() is None


def test_a_journal_with_stray_entries_still_launches_and_a_failed_launch_leaves_nothing_running(tmp_path, monkeypatch):
    (tmp_path / "helix_dream.json").write_text(json.dumps({"sessions": ["junk", 5]}), encoding="utf-8")
    rig = _Rig(tmp_path)
    rig.dream.tick()
    assert len(rig.lane.requests) == 3 and rig.dream.running is False
    assert [type(s) for s in rig.journal()["sessions"]] == [dict]  # the stray values are gone
    assert rig.dream.status() and rig.dream.journal_tail()

    class _Broken(_ImmediateThread):
        def start(self):
            raise RuntimeError("no threads left")

    monkeypatch.setattr(dream_mod, "_Thread", _Broken)
    rig2 = _Rig(tmp_path, clock=_Clock(day=5))
    rig2.dream.tick()  # logged and swallowed by the heartbeat…
    assert rig2.dream.running is False  # …and never a zombie that says "already dreaming"
    assert rig2.dream.dream_now(10).startswith("I couldn't start the session — no threads left")
    assert rig2.dream.running is False and rig2.dream.stop() == "No dream session is running."


def test_status_and_the_journal_survive_a_stripped_or_older_record(tmp_path):
    (tmp_path / "helix_dream.json").write_text(json.dumps({"sessions": [
        {"id": "x", "ended": "2026-09-01T07:00:00", "day": "2026-09-01"},
        {"id": "y", "ended": "2026-09-02T07:00:00", "day": "2026-09-02", "kind": "nightly",
         "stopped_reason": "a quiet night", "drafts": [{"summary": "no outcome"}, "junk"], "applied": []},
    ]}), encoding="utf-8")
    rig = _Rig(tmp_path, clock=_Clock(hour=14))
    assert "Last session (2026-09-02): nothing drafted (a quiet night)." in rig.dream.status()
    tail = rig.dream.journal_tail(7)
    assert "- 2026-09-01" in tail and "- 2026-09-02" in tail and "tried 1, applied 0" in tail


def test_the_journal_keeps_a_bounded_tail_of_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(dream_mod, "_SESSIONS_KEPT", 2)
    settings = _Settings()
    for day in (4, 5, 6):
        rig = _Rig(tmp_path, settings=settings, clock=_Clock(hour=23, day=day), chat=_Chat("QUIET"))
        rig.dream.tick()
    assert [s["day"] for s in rig.journal()["sessions"]] == ["2026-09-05", "2026-09-06"]
    assert rig.dream.journal_tail(1).startswith("- 2026-09-06")


def test_a_corrupt_journal_is_started_over(tmp_path):
    (tmp_path / "helix_dream.json").write_text("{not json", encoding="utf-8")
    rig = _Rig(tmp_path, chat=_Chat("QUIET"))
    rig.dream.tick()
    assert len(rig.journal()["sessions"]) == 1
    assert rig.dream.morning_report()


# ----------------------------------------------------------------------------- the rebuild
def _frozen_rig(tmp_path, **kw):
    settings = kw.pop("settings", _Settings({"dream_auto_apply": True}))
    kw.setdefault("suite", lambda b: (True, "ok"))
    return _Rig(tmp_path, paths=_paths(tmp_path, frozen=True), settings=settings, **kw)


def test_a_frozen_night_that_applied_changes_schedules_the_rebuild_and_asks_to_quit(tmp_path):
    rb = _Rebuilder()
    rig = _frozen_rig(tmp_path, rebuilder=rb)
    rig.dream.tick()
    assert rb.scheduled == ["applied 3 changes"]
    assert [e.reason for e in rig.rebuilds] == ["applied 3 changes"]  # webboot quits on this
    s = rig.last()
    assert s["rebuild"]["job"].endswith("job-1.json") and s["rebuild"]["requested_at"]
    assert rig.journal()["rebuild_pending"] is False
    assert rig.states[-1].running is False  # the chip flipped before the quit was asked for
    # The morning after — the script wrote its result; the report reads it.
    (tmp_path / "rebuild").mkdir()
    (tmp_path / "rebuild" / "last_result.json").write_text(json.dumps(
        {"ok": True, "built": True, "restored": False, "seconds": 600, "message": "rebuilt and relaunched",
         "at": "2026-09-05T07:20:00"}), encoding="utf-8")
    report = rig.dream.morning_report()
    assert "and applied 3" in report and report.endswith("I rebuilt and relaunched at 7:20. Its theme: Reminders that fire once.")


def test_a_night_the_user_stopped_or_is_awake_for_never_quits_the_app_for_a_rebuild(tmp_path):
    # "Stop dreaming" on the frozen build: the rebuild would quit HELIX one second after the user
    # spoke. A night the user ended by hand — or is at the machine as it ends — carries its applied
    # changes to the next quiet night instead.
    rb = _Rebuilder()

    def suite(branch):
        if branch == "selfdev/d2":
            rig.dream.stop("the user asked")  # draft 1 applied; the stop lands during draft 2's suite
        return True, "ok"

    rig = _frozen_rig(tmp_path, rebuilder=rb, suite=suite)
    rig.dream.tick()
    s = rig.last()
    assert s["stopped_reason"] == "the user asked" and len(s["applied"]) == 1
    assert rb.scheduled == [] and rig.rebuilds == [] and rig.journal()["rebuild_pending"] is True
    assert any("after the next quiet night — you stopped the session" in n for n in s["notes"])
    assert "They'll load at the next rebuild." in rig.dream.morning_report()

    awake = _frozen_rig(tmp_path, rebuilder=rb, clock=_Clock(hour=23, day=5),
                        activity=lambda: 15 * 60.0 if len(awake.lane.requests) < 3 else 5.0)
    awake.dream.tick()  # the plan runs while the user is away; they are back as it ends
    assert awake.last()["stopped_reason"] == "the plan was done" and len(awake.last()["applied"]) == 3
    assert rb.scheduled == [] and awake.rebuilds == [] and awake.journal()["rebuild_pending"] is True
    assert any("you're using the machine" in n for n in awake.last()["notes"])


def test_the_report_is_honest_about_a_rebuild_that_failed_or_left_no_record(tmp_path):
    rig = _frozen_rig(tmp_path, rebuilder=_Rebuilder())
    rig.dream.tick()
    assert "no record of how it went" in rig.dream.morning_report()
    rig2 = _frozen_rig(tmp_path, rebuilder=_Rebuilder(), settings=_Settings({"dream_auto_apply": True}),
                       clock=_Clock(hour=23, day=6))
    rig2.dream.tick()
    (tmp_path / "rebuild").mkdir(exist_ok=True)
    (tmp_path / "rebuild" / "last_result.json").write_text(json.dumps(
        {"ok": False, "built": False, "restored": True, "seconds": 90,
         "message": "the build failed (build.py exited 1); restored the previous build and relaunched it",
         "at": "2026-09-07T07:30:00"}), encoding="utf-8")
    assert "so I put the previous build back" in rig2.dream.morning_report()


def test_no_rebuild_when_nothing_applied_or_rebuilding_is_off_or_it_cannot(tmp_path):
    quiet = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True), rebuilder=_Rebuilder(), chat=_Chat("QUIET"))
    quiet.dream.tick()
    assert quiet.rebuilder.scheduled == [] and quiet.rebuilds == []

    off = _frozen_rig(tmp_path, rebuilder=_Rebuilder(),
                      settings=_Settings({"dream_auto_apply": True, "dream_rebuild": False}),
                      clock=_Clock(hour=23, day=5))
    off.dream.tick()
    assert off.rebuilder.scheduled == [] and off.rebuilds == []
    assert off.journal()["rebuild_pending"] is True
    assert any("automatic rebuilding is off" in n for n in off.last()["notes"])
    assert "They'll load at the next rebuild." in off.dream.morning_report()

    cannot = _frozen_rig(tmp_path, rebuilder=_Rebuilder(available=False, why="the source repository isn't reachable"),
                         clock=_Clock(hour=23, day=6))
    cannot.dream.tick()
    assert cannot.rebuilds == [] and any("rebuild skipped — the source" in n for n in cannot.last()["notes"])

    broken = _frozen_rig(tmp_path, rebuilder=_Rebuilder(raises=OSError("disk full")), clock=_Clock(hour=23, day=7))
    broken.dream.tick()
    assert broken.rebuilds == [] and any("could not be scheduled — disk full" in n for n in broken.last()["notes"])


def test_a_manual_session_never_quits_the_app_but_the_next_night_catches_up(tmp_path):
    rb = _Rebuilder()
    rig = _frozen_rig(tmp_path, rebuilder=rb, clock=_Clock(hour=14))
    rig.dream.dream_now(60)
    assert rb.scheduled == [] and rig.rebuilds == [] and rig.journal()["rebuild_pending"] is True
    assert any("next nightly session will rebuild" in n for n in rig.last()["notes"])
    night = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True), rebuilder=rb, chat=_Chat("QUIET"),
                 settings=rig.settings, clock=_Clock(hour=23))
    night.dream.tick()
    assert rb.scheduled == ["changes applied earlier"] and len(night.rebuilds) == 1


def test_in_development_an_applied_change_asks_for_a_restart_not_a_rebuild(tmp_path):
    rb = _Rebuilder()
    rig = _Rig(tmp_path, settings=_Settings({"dream_auto_apply": True}), suite=lambda b: (True, "ok"),
               rebuilder=rb)
    rig.dream.tick()
    assert rb.scheduled == [] and rig.rebuilds == []
    assert rig.last()["restart_needed"] == 3 and "restart needed" in rig.dream.journal_tail(1)


# ----------------------------------------------------------------------------- schedule + status
def test_schedule_validates_saves_and_confirms(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"dream_enabled": False}))
    assert rig.dream.schedule(start="23:00", hours=8, enabled=True) == "Dreaming nightly from 23:00 for 8 hours."
    assert rig.settings.d["dream_start"] == "23:00" and rig.settings.d["dream_hours"] == 8
    assert rig.dream.schedule(start="7:05", hours=1.5) == "Dreaming nightly from 07:05 for 1.5 hours."
    assert rig.dream.schedule(start="9:5") == "Dreaming nightly from 09:05 for 1.5 hours."  # the card's shape too
    assert rig.dream.schedule(enabled=False) == "Dreaming is off — I won't dream tonight."
    assert rig.settings.d["dream_enabled"] is False
    assert rig.dream.schedule(start="22:30") == ("Dreaming is off — I won't dream tonight. When it's on, "
                                                 "I'll dream nightly from 22:30 for 1.5 hours.")


@pytest.mark.parametrize("kw, expect", [
    ({"start": "25:00"}, "isn't one I can read"),
    ({"start": "eleven"}, "isn't one I can read"),
    ({"start": "23:60"}, "isn't one I can read"),
    ({"hours": 0}, "between 1 and 12 hours"),
    ({"hours": 13}, "between 1 and 12 hours"),
    ({"hours": "lots"}, "isn't a number of hours"),
])
def test_schedule_refuses_bad_values_without_saving(tmp_path, kw, expect):
    rig = _Rig(tmp_path)
    assert expect in rig.dream.schedule(**kw)
    assert rig.settings.writes == []


@pytest.mark.parametrize("value", [False, "false", "off", "no", "0"])
def test_switching_off_while_dreaming_stops_the_session(tmp_path, value):
    # A string 'false'/'off'/'no' from any caller means OFF — never a truthy non-empty string.
    rig = _Rig(tmp_path, hold=True)

    def hand(polls):
        if polls == 1:
            assert rig.dream.schedule(enabled=value) == "Dreaming is off. I'm stopping tonight's session now."

    rig.lane.on_busy = hand
    rig.dream.tick()
    assert rig.lane.cancelled == 1 and rig.last()["stopped_reason"] == "dreaming was switched off"
    assert rig.settings.d["dream_enabled"] is False


@pytest.mark.parametrize("raw, norm", [
    ("23:00", "23:00"), ("7:05", "07:05"), ("9:5", "09:05"), ("23:0", "23:00"), ("23.00", "23:00"),
    ("7.30", "07:30"), (" 8:15 ", "08:15"), ("24:00", None), ("25:00", None), ("23:60", None),
    ("eleven", None), ("", None), (None, None),
])
def test_normalize_time_is_the_one_clock_contract(raw, norm):
    # The settings route (server.py) and the voice path must accept and refuse the same strings.
    assert dream_mod.normalize_time(raw) == norm


def test_the_model_is_named_the_way_a_person_says_it():
    assert dream_mod._humanize_model("claude-fable-5") == "Fable 5"
    assert dream_mod._humanize_model("claude-opus-4-8") == "Opus 4.8"
    assert dream_mod._humanize_model("claude-sonnet-4.5") == "Sonnet 4.5"
    assert dream_mod._humanize_model("some-other-id") == "some-other-id" and dream_mod._humanize_model("") == ""


def test_a_setting_changed_while_a_draft_runs_is_written_once_the_lane_is_idle(tmp_path):
    # The settings file is a guard file: the gate byte-reverts it when a coder run ends, so a write
    # made mid-draft would be undone. The service holds the write and lands it on the next heartbeat.
    rig = _Rig(tmp_path, settings=_Settings({"dream_enabled": False}), hold=True)
    rig.lane._busy = True  # a user's own draft is in flight
    assert rig.dream.schedule(start="22:00") == ("Dreaming is off — I won't dream tonight. When it's "
                                                 "on, I'll dream nightly from 22:00 for 8 hours.")
    assert rig.settings.writes == []                      # held…
    assert rig.dream._start_text() == "22:00"             # …but already the truth for the service
    rig.dream.tick()
    assert rig.settings.writes == []                      # still busy: still held
    rig.lane._busy = False
    rig.dream.tick()
    assert ("dream_start", "22:00") in rig.settings.writes  # …and landed once the lane was idle


def test_status_is_plain_names_the_model_and_no_tool(tmp_path):
    rig = _Rig(tmp_path, clock=_Clock(hour=14))
    text = rig.dream.status()
    assert text.startswith("Dreaming is on — the next session is tonight at 23:00 for 8 hours.")
    assert "I plan and draft on Fable 5." in text and "claude-fable-5" not in text  # spoken: no raw id
    assert "Every draft waits for your review." in text
    for name in ("dream_status", "dream_schedule", "dream_now", "stop_dreaming"):
        assert name not in text
    off = _Rig(tmp_path, settings=_Settings({"dream_enabled": False, "dream_auto_apply": True}))
    assert off.dream.status().startswith("Dreaming is off. When it's on, I dream nightly from 23:00 for 8 hours.")
    assert "applies on its own" in off.dream.status()
    frozen = _Rig(tmp_path, paths=_paths(tmp_path, frozen=True), clock=_Clock(hour=14))
    assert "rebuild and relaunch" in frozen.dream.status()
    inside = _Rig(tmp_path, settings=_Settings({"dream_last_session": "2026-09-04"}))
    assert "window is open until 07:00, but tonight's session already ran." in inside.dream.status()


def test_status_while_running_counts_the_night_so_far(tmp_path):
    rig = _Rig(tmp_path, hold=True)
    seen: list[str] = []

    def hand(polls):
        if polls == 1:
            seen.append(rig.dream.status())
            rig.dream.stop()

    rig.lane.on_busy = hand
    rig.dream.tick()
    assert seen and seen[0].startswith("Dreaming now — since 23:00, until 07:00: 1 draft so far, 0 applied.")
    assert rig.dream.running is False


# ----------------------------------------------------------------------------- the mind (Phase 2)
DISCOVERIES = [
    {"text": "The XIAO ESP32S3 Sense has 8 MB PSRAM", "source": "wiki.seeedstudio.com",
     "url": "https://wiki.seeedstudio.com/xiao_esp32s3/", "verified": True, "kind": "finding", "score": 4.25},
    {"text": "The INMP441 costs about $3", "source": "", "url": "", "verified": False, "kind": "finding",
     "score": 1.0},
]
FACTS = [
    {"id": "f1", "claim": "XIAO ESP32S3 Sense PSRAM", "value": "8 MB", "host": "wiki.seeedstudio.com",
     "url": "https://wiki.seeedstudio.com/xiao_esp32s3/", "date": "2026-09-04", "project": "IronEye",
     "topics": ["esp32"]},
    {"id": "f2", "claim": "INMP441 supply", "value": "3.3 V", "host": "invensense.tdk.com",
     "url": "https://invensense.tdk.com/products/inmp441/", "date": "2026-09-04", "project": "", "topics": []},
]
RESEARCH = [{
    "question": "Does the XIAO ESP32S3 Sense have PSRAM?", "why": "the audio buffer", "status": "ok",
    "findings": [{"text": "The XIAO ESP32S3 Sense has 8 MB PSRAM", "url": "https://wiki.seeedstudio.com/xiao_esp32s3/",
                  "host": "wiki.seeedstudio.com", "verified": True}],
    "facts": FACTS[:1], "facts_noted": 1, "ideas": [], "queries": ["searched: XIAO ESP32S3 PSRAM (8 hits)"],
}]
EXPERIMENTS = [{"idea": "measure lxml", "ok": True, "findings": "# Findings…", "recommendation": "switch to lxml",
                "summary": "recommends: switch to lxml"}]
MIND_REQUESTS = (
    Request(text="Remember the camera device in services/camera.py; add a test.", origin="research"),
    Request(text="Shorten the morning brief in services/agents.py.", deep=False),
)


def _night_summary(**kw):
    fields = dict(
        discoveries=[dict(d) for d in DISCOVERIES], facts=[dict(f) for f in FACTS], facts_noted=2,
        experiments=[dict(e) for e in EXPERIMENTS], research=[dict(r) for r in RESEARCH],
        agenda={"research": [{"question": "Does the XIAO ESP32S3 Sense have PSRAM?", "why": "the audio buffer"}],
                "verify": [], "experiments": ["measure lxml"], "improve": [r.text for r in MIND_REQUESTS]},
        self_model_delta={"added": {"capable": ["sees"]}, "dropped": {}, "kept": {"capable": 1}},
        theme="Verified parts for IronEye",
    )
    fields.update(kw)
    return NightSummary(**fields)


class _Mind:
    """A stand-in DreamMind (services/dream_mind.py): records what the session handed it, uses every
    hook the way the real mind does — notes and records as it goes, asks for the nights, the rail and
    the user's presence, hands its requests to improve — and returns the NightSummary it was built
    with. `script(mind, hooks)` runs mid-night: the test's hand inside the session thread."""

    def __init__(self, *, requests=MIND_REQUESTS, summary=None, script=None, raises=None):
        self.requests = list(requests)
        self.summary = summary
        self.script = script
        self.raises = raises
        self.calls: list[tuple] = []
        self.hooks: NightHooks | None = None
        self.seen: dict = {}
        self.drafts: list[dict] = []

    def run_night(self, deadline, budget, *, hooks=None):
        self.calls.append((deadline, budget))
        self.hooks = hooks
        if self.raises is not None:
            raise self.raises
        hooks.record({"cycles": [{"name": "research", "started": "2026-09-04T23:05:00", "ended": None}]})
        hooks.note("reflected — 1 to research, 0 to verify, 1 to try, 2 to change")
        self.seen = {"nights": hooks.nights(7), "rail": hooks.rail_problem(),
                     "activity": hooks.activity() if hooks.activity is not None else "no probe",
                     "stop": hooks.should_stop()}
        summary = self.summary or _night_summary()
        hooks.record({"research": summary.research, "facts": summary.facts, "facts_noted": summary.facts_noted,
                      "experiments": summary.experiments, "agenda": summary.agenda})
        if self.script is not None:
            self.script(self, hooks)
        hooks.record({"cycles": [{"name": "research", "started": "2026-09-04T23:05:00",
                                  "ended": "2026-09-04T23:20:00"}]})
        if self.requests and not hooks.should_stop():
            self.drafts = list(hooks.improve(list(self.requests)))
        summary.drafts = list(self.drafts)
        return summary


class _ClockEvent(threading.Event):
    """The session's stop flag with waiting turned into time passing: every wait(t) moves the fake
    clock t seconds forward and returns at once, so a pause's backoff runs in no real time."""

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.waited: list[float] = []

    def wait(self, timeout=None):
        if timeout:
            self.clock.advance(seconds=timeout)
            self.waited.append(timeout)
        return self.is_set()


class _ProbeChat(_Chat):
    """The growth chat with the pause's probe told apart by its system prompt: planning calls answer
    from `replies` as _Chat does; probes answer from `probes` in order (the last repeats), each one
    stamped with the clock and reported to `on_probe(n)` first."""

    def __init__(self, *replies, probes=("OK",), clock=None, on_probe=None):
        super().__init__(*replies)
        self.probes = list(probes) or ["OK"]
        self.clock = clock
        self.on_probe = on_probe
        self.probe_times: list = []

    def chat(self, turns, *, system=None, tools=None):
        if system == dream_mod._PROBE_SYSTEM:
            self.probe_times.append(self.clock.now() if self.clock is not None else None)
            n = len(self.probe_times)
            if self.on_probe is not None:
                self.on_probe(n)
            text = self.probes[min(n - 1, len(self.probes) - 1)]
            if isinstance(text, Exception):
                raise text
            return Reply(blocks=(Text(text),))
        return super().chat(turns, system=system, tools=tools)


class _Sub:
    """The subscription rail: live or not, why, or a probe that dies. `back_after` = the number of
    active() asks after which an inactive rail comes back (the plan's limit lifting)."""

    def __init__(self, active=True, why=None, raises=None, back_after=None):
        self._active, self._why, self._raises, self._back_after = active, why, raises, back_after
        self.asks = 0

    def active(self, *, allow_probe=True):
        self.asks += 1
        if self._raises is not None:
            raise self._raises
        if not self._active and self._back_after is not None and self.asks > self._back_after:
            self._active = True
        return self._active

    def why_inactive(self, *, allow_probe=True):
        return self._why


def _raise_on_start(lane, *texts):
    """The lane's first len(texts) starts raise with those texts — the plan's limit, as the lane sees
    it — and the rest behave."""
    original = lane.start
    pending = list(texts)

    def start(request, model=None, unattended=False):
        if pending:
            raise RuntimeError(pending.pop(0))
        return original(request, model=model, unattended=unattended)

    lane.start = start


def test_a_wired_mind_runs_the_night_through_the_hooks_and_the_report_leads_with_the_best_find(tmp_path):
    mind = _Mind()
    rig = _Rig(tmp_path, mind=mind)
    rig.dream.tick()
    # The mind owned the thinking: the session's own planner was never asked; the mind got the window's
    # end and the drafts ceiling; its requests went through the lane — Fable, unattended, the
    # research-derived one first with its origin on the record, the "standard" one on Fable anyway.
    assert rig.chat.prompts == []
    assert mind.calls == [(datetime(2026, 9, 5, 7, 0), 10)]
    assert rig.lane.requests == [r.text for r in MIND_REQUESTS] and all(rig.lane.unattended)
    assert rig.lane.models == ["claude-fable-5", "claude-fable-5"]
    s = rig.last()
    assert [d["origin"] for d in s["drafts"]] == ["research", ""]
    assert [p["origin"] for p in s["plan"]] == ["research", ""]
    assert any("standard suggested; drafting on Fable anyway" in n for n in s["notes"])
    # Everything the mind recorded as it went is on the record, and the summary's fields landed at the end.
    assert s["facts_noted"] == 2 and s["facts"] == FACTS and s["research"] == RESEARCH
    assert s["experiments"] == EXPERIMENTS and s["discoveries"] == DISCOVERIES
    assert s["agenda"]["improve"] == [r.text for r in MIND_REQUESTS] and s["agenda_remaining"] == []
    assert s["self_model_delta"]["added"] == {"capable": ["sees"]} and s["theme"] == "Verified parts for IronEye"
    assert s["stopped_reason"] == "the night's work was done" and s["cycles"][-1]["ended"]
    assert any(n.endswith("reflected — 1 to research, 0 to verify, 1 to try, 2 to change") for n in s["notes"])
    assert "facts 2, discoveries 2, experiments 1" in rig.dream.journal_tail(1)
    # The morning report LEADS with the best discovery (its host named), then the drafts, then the counts.
    report = rig.dream.morning_report()
    assert report == (
        "Last night's best find: The XIAO ESP32S3 Sense has 8 MB PSRAM (verified on wiki.seeedstudio.com) "
        "— and 1 more discovery in the journal. I drafted 2 improvements. 2 are waiting for your review. "
        "I researched 1 question, verified 2 facts and ran 1 experiment. Its theme: Verified parts for IronEye.")
    assert ("Last session (2026-09-04): 2 drafts, 2 waiting for your review, 2 discoveries, 2 facts "
            "verified, 1 experiment.") in rig.dream.status()
    # The journal page's view of the night (GET /api/dream/journal reads exactly this).
    entry = rig.dream.journal_entries(1)[0]
    assert entry["day"] == "2026-09-04" and entry["window"] == "23:00–07:00" and entry["in_progress"] is False
    assert entry["discoveries"] == DISCOVERIES and entry["facts"][0]["host"] == "wiki.seeedstudio.com"
    assert entry["facts_noted"] == 2 and entry["counts"]["facts"] == 2 and entry["counts"]["discoveries"] == 2
    assert entry["experiments"][0]["recommendation"] == "switch to lxml"
    assert entry["research"][0]["question"].startswith("Does the XIAO") and entry["agenda"]["improve"]
    assert entry["drafts"][0]["origin"] == "research" and entry["drafts"][0]["outcome"] == "drafted"
    # The page gets the report's body and the theme as separate fields (the paragraph told in the
    # morning is the body plus the rebuild result plus "Its theme: …").
    assert report == entry["report"] + " Its theme: Verified parts for IronEye."
    assert entry["theme"] == "Verified parts for IronEye" and entry["limit"] == ""


def test_an_unverified_best_find_is_said_to_be_unverified_and_a_manual_session_reads_right(tmp_path):
    summary = _night_summary(discoveries=[dict(DISCOVERIES[1])], facts=[], facts_noted=0, experiments=[],
                             research=[], theme="")
    rig = _Rig(tmp_path, mind=_Mind(summary=summary, requests=()), clock=_Clock(hour=14))
    rig.dream.dream_now(30)
    assert rig.dream.morning_report() == ("The best find of the session you asked for: The INMP441 costs "
                                          "about $3 (unverified). I found nothing worth changing, so I "
                                          "let the code rest.")


def test_the_hooks_carry_the_sessions_truth_to_the_mind(tmp_path):
    first = _Mind()
    rig = _Rig(tmp_path, mind=first)
    rig.dream.tick()
    assert first.seen == {"nights": [], "rail": None, "activity": None, "stop": False}  # no probe, no rail
    # The next night sees the first in its material — ended sessions only — and the shell's presence
    # probe reaches the mind through the session, live, exactly as the draft loop reads it.
    second = _Mind()
    later = _Rig(tmp_path, mind=second, settings=rig.settings, clock=_Clock(day=5))
    later.dream.activity = lambda: 15 * 60.0  # a quarter hour of silence: idle, so the drafts go on
    later.dream.tick()
    assert [s["day"] for s in second.seen["nights"]] == ["2026-09-04"] and second.seen["nights"][0]["ended"]
    assert second.seen["activity"] == 900.0 and len(later.lane.requests) == 2
    # An inactive plan is the rail problem the mind treats as a limit; a live one is None.
    off = _Mind()
    _Rig(tmp_path, mind=off, subscription=_Sub(active=False, why="no setup-token"), clock=_Clock(day=6),
         settings=rig.settings).dream.tick()
    assert off.seen["rail"] == "the plan isn't available right now (no setup-token)"


def test_the_minds_leftover_agenda_survives_and_the_improve_loop_names_why_it_stopped(tmp_path):
    # The ceiling: one of two requests drafted; the other is left for tomorrow — beside what the mind
    # itself had no time to hand over. Neither list erases the other.
    mind = _Mind(summary=_night_summary(agenda_remaining=["An idea the mind had no time to hand over."]))
    rig = _Rig(tmp_path, mind=mind, settings=_Settings({"dream_max_drafts": 1}))
    rig.dream.tick()
    s = rig.last()
    assert len(rig.lane.requests) == 1 and s["stopped_reason"] == "the draft ceiling was reached"
    assert s["agenda_remaining"] == [MIND_REQUESTS[1].text, "An idea the mind had no time to hand over."]
    assert rig.evolve.backlog == []  # no limit tonight: the journal keeps it; the backlog stays the user's
    # The window closing mid-improve: the first draft fits, the second is too late.
    late = _Rig(tmp_path, mind=_Mind(), clock=_Clock(hour=6, minute=35, day=5))
    late.dream.tick()
    s2 = late.last()
    assert len(late.lane.requests) == 1 and s2["stopped_reason"] == "the window was ending"
    assert s2["agenda_remaining"] == [MIND_REQUESTS[1].text]
    assert any("no draft starts this late" in n for n in s2["notes"])
    # Drafts failing back to back end the improve cycle with the reason, as in a plain night.
    failing = _Rig(tmp_path, mind=_Mind(), fail=True, clock=_Clock(day=6))
    failing.dream.tick()
    assert failing.last()["stopped_reason"] == "drafts kept failing: the coder made no changes."


def test_a_mind_that_dies_still_ends_in_a_journaled_report(tmp_path):
    rig = _Rig(tmp_path, mind=_Mind(raises=RuntimeError("the reflection exploded")))
    rig.dream.tick()
    s = rig.last()
    assert s["stopped_reason"] == "an error" and s["ended"] and rig.dream.running is False
    assert any("hit an error and stopped early" in n for n in s["notes"])
    assert rig.dream.morning_report() == ("Last night I hit an error and stopped early before changing "
                                          "anything — the journal and the log have the details.")


def test_status_names_the_minds_cycle_and_a_limit_through_the_hooks_pauses_the_session(tmp_path):
    clock = _Clock()
    chat = _ProbeChat(PLAN, probes=("OK",), clock=clock)
    seen: dict = {}

    def script(mind, hooks):
        seen["status"] = rig.dream.status()  # mid-research: the cycle is on the record
        seen["resumed"] = hooks.limit("HTTP 429 rate limit; resets at 3am")
        seen["states"] = [(st.running, st.line) for st in rig.states]

    rig = _Rig(tmp_path, mind=_Mind(script=script), chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    rig.dream.tick()
    assert seen["status"].startswith("Dreaming now (researching) — since 23:00, until 07:00: 0 drafts so far, 0 applied.")
    assert seen["resumed"] is True and chat.probe_times == [datetime(2026, 9, 4, 23, 25)]
    assert seen["states"][-2:] == [(True, "dreaming — paused for the plan's limit"), (True, "Dreaming since 23:00")]
    s = rig.last()
    assert s["limit_log"] == [{"at": "2026-09-04T23:05:00", "resumed_at": "2026-09-04T23:25:00",
                               "hint": "resets at 3am", "text": "HTTP 429 rate limit; resets at 3am"}]
    assert len(rig.lane.requests) == 2  # the night went on after the pause
    report = rig.dream.morning_report()
    assert report.startswith("Last night's best find:")
    assert "The plan's limit was reached at 23:05; I paused and resumed at 23:25." in report


def test_a_pause_the_window_outlives_ends_the_minds_night(tmp_path):
    clock = _Clock(hour=6, minute=30, day=5)
    chat = _ProbeChat(PLAN, probes=("Rate limit still in effect",), clock=clock)
    seen: dict = {}

    def script(mind, hooks):
        seen["resumed"] = hooks.limit("usage limit reached")
        seen["stop"] = hooks.should_stop()

    rig = _Rig(tmp_path, mind=_Mind(script=script), chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    rig.dream.tick()
    assert seen == {"resumed": False, "stop": True}
    s = rig.last()
    assert s["stopped_reason"] == "the window closed" and rig.lane.requests == [] and s["paused"] is None
    assert chat.probe_times == [datetime(2026, 9, 5, 6, 50)] and s["limit_log"][0]["resumed_at"] is None
    report = rig.dream.morning_report()
    assert "I didn't get to draft anything — the plan's limit got in the way." in report
    assert "The plan's limit was reached at 06:30; I paused and did not get to resume." in report


# ----------------------------------------------------------------------------- limits and model discipline (§13)
def test_a_limit_from_the_lane_pauses_the_night_probes_on_the_backoff_and_resumes(tmp_path):
    clock = _Clock()
    seen: list[str] = []
    chat = _ProbeChat(PLAN, probes=("Rate limit still in effect", "OK"), clock=clock,
                      on_probe=lambda n: seen.append(rig.dream.status()))
    rig = _Rig(tmp_path, chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    _raise_on_start(rig.lane, "HTTP 429 rate limit; resets at 3am")
    rig.dream.tick()
    # 20 minutes, then 30: two probes, the second answered; the request was retried, never dropped.
    assert chat.probe_times == [datetime(2026, 9, 4, 23, 25), datetime(2026, 9, 4, 23, 55)]
    assert rig.lane.requests[0].startswith("Debounce the reminder") and len(rig.lane.requests) == 3
    s = rig.last()
    assert [d["outcome"] for d in s["drafts"]] == ["held", "drafted", "drafted", "drafted"]
    assert s["drafts"][0]["held_for"] == "limit"
    assert s["drafts"][0]["reason"].startswith("limit — the plan's limit was reached mid-draft (resets at 3am)")
    assert s["limit_log"] == [{"at": "2026-09-04T23:05:00", "resumed_at": "2026-09-04T23:55:00",
                               "hint": "resets at 3am", "text": "HTTP 429 rate limit; resets at 3am"}]
    assert s["limit_pauses"] == 1 and s["paused"] is None and s["stopped_reason"] == "the plan was done"
    notes = s["notes"]
    assert any(n.endswith("paused at 23:05 — the plan's limit was reached (resets at 3am); waiting for it to reset")
               for n in notes)
    assert any("still paused at 23:25 — Rate limit still in effect; next probe in 30 minutes" in n for n in notes)
    assert any(n.endswith("resumed at 23:55 — the plan is answering again") for n in notes)
    # The chip, the status and Evolve's journal said so while it lasted.
    assert (True, "dreaming — paused for the plan's limit") in [(st.running, st.line) for st in rig.states]
    assert seen[0].startswith("Dreaming — paused for the plan's limit since 23:05 (resets at 3am); "
                              "the window runs until 07:00.")
    assert any(ln.startswith("dream: paused for the plan's limit at 23:05") for ln in rig.evolve.lines)
    # The morning: a limited attempt is not a draft, and the pause is told plainly.
    assert rig.dream.morning_report() == (
        "Last night I drafted 3 improvements. 3 are waiting for your review. The plan's limit was reached "
        "at 23:05; I paused and resumed at 23:55. Its theme: Reminders that fire once.")
    assert "paused for a limit 1" in rig.dream.journal_tail(1)
    assert rig.dream.status().endswith("3 waiting for your review. The plan's limit paused it.")


def test_the_window_closing_while_paused_winds_down_and_carries_the_agenda(tmp_path):
    clock = _Clock(hour=6, minute=30, day=5)
    chat = _ProbeChat(PLAN, probes=("Rate limit still in effect",), clock=clock)
    rig = _Rig(tmp_path, chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    _raise_on_start(rig.lane, "usage limit reached")
    rig.dream.tick()
    s = rig.last()
    assert chat.probe_times == [datetime(2026, 9, 5, 6, 50)]  # one probe; the next wait met the window's end
    assert s["stopped_reason"] == "the window closed" and s["paused"] is None and rig.lane.requests == []
    assert s["limit_log"][0]["resumed_at"] is None and rig.states[-1].running is False
    # Nothing is lost: the whole plan is journaled and queued for tomorrow night.
    assert s["agenda_remaining"] == [r.text for r in parse_plan(PLAN, 10)[0]]
    assert rig.evolve.backlog == s["agenda_remaining"]
    assert any("3 remaining improvements saved to the backlog for tomorrow night" in n for n in s["notes"])
    assert rig.dream.morning_report() == (
        "Last night I planned 3 improvements but didn't get to start one (the window closed). The plan's "
        "limit was reached at 06:30; I paused and did not get to resume — 0 of 3 planned improvements ran. "
        "Its theme: Reminders that fire once.")


def test_three_limit_pauses_end_the_night_early_and_say_so(tmp_path):
    clock = _Clock()
    chat = _ProbeChat(PLAN, probes=("OK",), clock=clock)
    rig = _Rig(tmp_path, chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    _raise_on_start(rig.lane, *(["rate limit exceeded"] * 6))
    rig.dream.tick()
    s = rig.last()
    # Each pause starts its backoff over: 20 minutes after each of the three limits.
    assert chat.probe_times == [datetime(2026, 9, 4, 23, 25), datetime(2026, 9, 4, 23, 45),
                                datetime(2026, 9, 5, 0, 5)]
    assert s["limit_pauses"] == 3 and len(s["limit_log"]) == 3 and all(e["resumed_at"] for e in s["limit_log"])
    assert s["stopped_reason"] == "the plan's limit was reached three times"
    assert len(s["drafts"]) == 4 and all(d["held_for"] == "limit" for d in s["drafts"])
    assert any("three pauses tonight already, so I'm ending the session early" in n for n in s["notes"])
    assert rig.lane.requests == [] and len(s["agenda_remaining"]) == 3 and rig.evolve.backlog == s["agenda_remaining"]
    assert rig.dream.morning_report() == (
        "Last night I planned 3 improvements but the plan's limit stopped every attempt. The plan's limit "
        "was hit three times (first at 23:05), so I ended the night early; what was left is saved for "
        "tomorrow night. Its theme: Reminders that fire once.")
    assert "Last session (2026-09-04): nothing drafted (the plan's limit was reached three times). The plan's limit paused it." in rig.dream.status()


def test_a_limit_mid_draft_is_held_not_failed_and_its_branch_is_discarded(tmp_path):
    clock = _Clock()
    chat = _ProbeChat(PLAN, probes=("OK",), clock=clock)
    rig = _Rig(tmp_path, chat=chat, clock=clock)
    rig.dream._stop = _ClockEvent(clock)
    original = rig.lane.start
    once = {"done": False}

    def start(request, model=None, unattended=False):
        if not once["done"]:  # the coder got half-way, then the plan ran out
            once["done"] = True
            rig.bus.publish(SelfChangeFinished(ok=False, error="usage limit reached — resets at 4am",
                                               branch="selfdev/half", unattended=unattended))
            return True
        return original(request, model=model, unattended=unattended)

    rig.lane.start = start
    rig.dream.tick()
    s = rig.last()
    first = s["drafts"][0]
    assert first["outcome"] == "held" and first["held_for"] == "limit" and first["branch"] == ""
    assert first["reason"] == ("limit — the plan's limit was reached mid-draft (resets at 4am); "
                               "the half-drafted branch was discarded")
    assert rig.selfdev.rejected == ["selfdev/half"]  # never waits for review
    assert [d["outcome"] for d in s["drafts"][1:]] == ["drafted"] * 3
    assert rig.dream.morning_report().startswith("Last night I drafted 3 improvements. 3 are waiting")


class _Opus(_GrowthModel):
    def resolve(self):
        return "claude-opus-4-8"


class _Mythos(_GrowthModel):
    def resolve(self):
        return "claude-mythos-1"


class _BrokenResolver(_GrowthModel):
    def resolve(self):
        raise RuntimeError("the plan list is unreadable")


def test_fable_or_nothing_a_sub_fable_growth_model_never_starts_a_night(tmp_path):
    rig = _Rig(tmp_path, growth_model=_Opus())
    why = "Fable isn't available on this plan right now"
    assert rig.dream.why_not_now() == why
    rig.dream.tick()
    rig.dream.tick()
    assert rig.chat.prompts == [] and rig.lane.requests == [] and "dream_last_session" not in rig.settings.d
    assert sum(ln == "dream: not dreaming tonight — " + why for ln in rig.evolve.lines) == 1  # said once
    text = rig.dream.status()
    assert text.startswith("Dreaming is paused: Fable isn't available on this plan right now.")
    assert "I only dream on Fable — never on a weaker model." in text and "Opus" not in text
    assert rig.dream.dream_now(30) == ("I can't dream right now — Fable isn't available on this plan right "
                                       "now. I only dream on Fable, never on a weaker model.")
    # A Fable-class id (Fable, or the Mythos tier above it) passes. A resolver that cannot answer
    # names no Fable-class model: the gate fails CLOSED (a night on it would hand every draft to the
    # coder's boot-time default), and the status and dream_now say what it is, never "Opus".
    assert _Rig(tmp_path, growth_model=_Mythos()).dream.why_not_now() is None
    broken = _Rig(tmp_path, growth_model=_BrokenResolver())
    unnamed = "the growth model couldn't be named right now (the plan list is unreadable)"
    assert broken.dream.why_not_now() == unnamed
    broken.dream.tick()
    assert broken.lane.requests == [] and "dream_last_session" not in broken.settings.d
    assert broken.dream.status().startswith(f"Dreaming is paused: {unnamed}.")
    assert broken.dream.dream_now(30) == (f"I can't dream right now — {unnamed}. I only dream on Fable, never "
                                          "on a weaker model.")


class _FlakyResolver(_GrowthModel):
    """Names Fable at the gate, then cannot answer at all — the plan list went unreadable mid-night."""

    def __init__(self, good_for=1):
        self.calls, self.good_for = 0, good_for

    def resolve(self):
        self.calls += 1
        if self.calls > self.good_for:
            raise RuntimeError("the plan list is unreadable")
        return "claude-fable-5"

    def work_model(self, deep):
        return self.resolve()


def test_a_night_whose_resolver_breaks_mid_way_never_drafts_on_an_unnamed_model(tmp_path):
    """Fable or nothing (§13) on the draft path too: when work_model() cannot name the model, the
    draft is HELD like a limit — never handed to the lane with model=None (the coder's default) —
    the night pauses, and its probes ask the resolver along with the rail until the window closes."""
    clock = _Clock()
    chat = _ProbeChat(PLAN, probes=("OK",), clock=clock)
    # The gate names the model once (why_not_now) and the session record once (its model line);
    # the first draft's work_model() is the third ask — and the resolver is gone by then.
    rig = _Rig(tmp_path, chat=chat, clock=clock, growth_model=_FlakyResolver(good_for=2))
    rig.dream._stop = _ClockEvent(clock)
    rig.dream.tick()
    s = rig.last()
    assert rig.lane.requests == [] and rig.lane.models == []  # nothing started on an unnamed model
    assert s["drafts"] and all(d["held_for"] == "limit" and d["model"] == "" for d in s["drafts"])
    assert s["drafts"][0]["reason"].startswith("limit — no Fable-class model could be named")
    assert any("held: no Fable-class model could be named" in n for n in s["notes"])
    assert chat.probe_times == []  # the probe found the resolver broken before asking the plan anything
    assert any("still paused at 23:25 — the growth model couldn't be named right now" in n for n in s["notes"])
    assert s["stopped_reason"] == "the window closed" and s["agenda_remaining"]
    assert "0 of 3 planned improvements ran" in rig.dream.morning_report()


def test_a_manual_session_is_never_held_for_the_presence_of_the_user_who_asked(tmp_path):
    # "Dream now" comes from a person at the keyboard: the mind gets no presence probe (as Phase 1's
    # draft loop never held a manual session) and starts at once; a nightly session keeps the probe.
    mind = _Mind()
    rig = _Rig(tmp_path, mind=mind, clock=_Clock(hour=14), activity=lambda: 5.0)
    rig.dream.dream_now(30)
    assert mind.seen["activity"] == "no probe" and mind.hooks.activity is None
    assert len(rig.lane.requests) == 2 and not any("holding" in n for n in rig.last()["notes"])
    nightly = _Mind()
    later = _Rig(tmp_path, mind=nightly, activity=lambda: 15 * 60.0, clock=_Clock(day=5), settings=rig.settings)
    later.dream.tick()
    assert nightly.seen["activity"] == 900.0 and nightly.hooks.activity is not None


def test_a_night_that_never_reached_improve_reports_what_it_planned_not_nothing_worth_changing(tmp_path):
    # The window ended (or the user was at the machine) before IMPROVE: the agenda the mind carried
    # over IS the plan tonight never started, and the report says so.
    summary = _night_summary(discoveries=[], facts=[], facts_noted=0, experiments=[], research=[],
                             reason="the window was ending", theme="",
                             agenda_remaining=["change a", "change b", "change c"])
    rig = _Rig(tmp_path, mind=_Mind(summary=summary, requests=()))
    rig.dream.tick()
    s = rig.last()
    assert [p["request"] for p in s["plan"]] == ["change a", "change b", "change c"]
    assert s["agenda_remaining"] == ["change a", "change b", "change c"]
    assert rig.dream.morning_report() == ("Last night I planned 3 improvements but didn't get to start one "
                                          "(the window was ending).")
    held = _night_summary(discoveries=[], facts=[], facts_noted=0, experiments=[], research=[],
                          reason="the window was ending", theme="", held_for_user=True)
    rig2 = _Rig(tmp_path, mind=_Mind(summary=held, requests=()), clock=_Clock(day=5), settings=rig.settings)
    rig2.dream.tick()
    assert rig2.last()["held_for_user"] is True
    assert rig2.dream.morning_report() == ("Last night I waited while you were using the machine, and the window "
                                           "ended before I could start.")
    planned_held = _night_summary(discoveries=[], facts=[], facts_noted=0, experiments=[], research=[],
                                  reason="the window was ending", theme="", held_for_user=True,
                                  agenda_remaining=["change a"])
    rig3 = _Rig(tmp_path, mind=_Mind(summary=planned_held, requests=()), clock=_Clock(day=6), settings=rig.settings)
    rig3.dream.tick()
    assert rig3.dream.morning_report() == ("Last night I planned 1 improvement but you were using the machine, "
                                           "so I didn't start any.")


class _ChatteringLane(_Lane):
    """The coder's final message the way it comes: chatter first, the change somewhere after."""

    SAID = ["All done — everything is in place. Here's a summary of the change:\n"
            "- Debounced the reminder repeat in services/reminders.py and added a test.\n- Nothing else moved.",
            "Propagation is on, so the warnings will reach both logs. All done — here's the summary."]

    def start(self, request, model=None, unattended=False):
        self.requests.append(request)
        self.models.append(model)
        self.unattended.append(unattended)
        n = len(self.requests)
        self.clock.advance(minutes=self.minutes)
        self.bus.publish(SelfChangeFinished(ok=True, summary=self.SAID[min(n - 1, 1)], branch=f"selfdev/d{n}",
                                            unattended=unattended))
        return True


def test_a_drafts_summary_is_what_changed_never_the_coders_chatter(tmp_path):
    rig = _Rig(tmp_path, settings=_Settings({"dream_max_drafts": 2}))
    rig.lane = _ChatteringLane(rig.bus, rig.clock)
    rig.dream._lane = rig.lane
    rig.dream.tick()
    s = rig.last()
    first, second = s["drafts"][0], s["drafts"][1]
    assert first["summary"] == "Debounced the reminder repeat in services/reminders.py and added a test."
    assert first["coder_said"].startswith("All done — everything is in place.")
    # Only chatter: the request's first sentence — what was asked — stands in.
    assert second["summary"] == "Teach the studio to rotate parts with a drag, in ui/studio.py; cover it in tests."
    entry = rig.dream.journal_entries(1)[0]
    assert [d["summary"] for d in entry["drafts"]] == [first["summary"], second["summary"]]
    assert any("drafted selfdev/d1 — Debounced the reminder repeat" in n for n in s["notes"])
    assert dream_mod._summary_line("Done.\nAll set!\nHere's a summary:\n", "Fix the thing. Then test it.") == "Fix the thing."


def test_the_report_counts_only_experiments_that_ran(tmp_path):
    unfinished = {"idea": "measure", "ok": False, "findings": "", "recommendation": "",
                  "summary": "The experiment ran past its 4-minute budget and was stopped."}
    ran = {"idea": "measure lxml", "ok": True, "findings": "# Findings", "recommendation": "", "summary": "no change"}
    rig = _Rig(tmp_path, mind=_Mind(summary=_night_summary(experiments=[unfinished], discoveries=[]), requests=()))
    rig.dream.tick()
    assert "I researched 1 question, verified 2 facts and tried 1 experiment that didn't finish." in rig.dream.morning_report()
    both = _Rig(tmp_path, mind=_Mind(summary=_night_summary(experiments=[ran, unfinished], discoveries=[]), requests=()),
                clock=_Clock(day=5), settings=rig.settings)
    both.dream.tick()
    assert ("verified 2 facts and ran 1 experiment (and tried 1 more that didn't finish)."
            in both.dream.morning_report())


class _OneThenFailLane(_Lane):
    def start(self, request, model=None, unattended=False):
        self.fail = len(self.requests) >= 1
        return super().start(request, model=model, unattended=unattended)


def test_a_night_that_ended_because_drafts_kept_failing_still_rebuilds_and_never_blames_the_user(tmp_path):
    # An applied change, then two failures in a row: a systemic end, not a person's stop. The frozen
    # build rebuilds as after any finished plan (the user is away), and no note says they stopped it.
    requests = [Request(text=f"change {i}", deep=True) for i in range(4)]
    mind = _Mind(requests=requests, summary=_night_summary(discoveries=[], facts=[], facts_noted=0,
                                                            experiments=[], research=[]))
    rb = _Rebuilder()
    rig = _Rig(tmp_path, mind=mind, paths=_paths(tmp_path, frozen=True), rebuilder=rb,
               settings=_Settings({"dream_auto_apply": True, "dream_rebuild": True}), activity=lambda: 3600.0)
    rig.lane = _OneThenFailLane(rig.bus, rig.clock)
    rig.dream._lane = rig.lane
    rig.dream.tick()
    s = rig.last()
    assert len(s["applied"]) == 1 and s["stopped_reason"].startswith("drafts kept failing")
    assert rb.scheduled == ["applied 1 change"] and [r.reason for r in rig.rebuilds] == ["applied 1 change"]
    assert not any("you stopped the session" in n for n in s["notes"])
    # …and with the user at the machine as it ends, the deferral names THAT, not a stop.
    awake = _Rig(tmp_path, mind=_Mind(requests=requests, summary=mind.summary), paths=_paths(tmp_path, frozen=True),
                 rebuilder=_Rebuilder(), settings=_Settings({"dream_auto_apply": True, "dream_rebuild": True}),
                 activity=lambda: 5.0, clock=_Clock(day=5))
    awake.lane = _OneThenFailLane(awake.bus, awake.clock)
    awake.dream._lane = awake.lane
    awake.dream.activity = lambda: 15 * 60.0 if len(awake.lane.requests) < 3 else 5.0
    awake.dream.tick()
    assert any("after the next quiet night — you're using the machine" in n for n in awake.last()["notes"])
    assert dream_mod._natural_end("drafts kept failing: x") and dream_mod._natural_end("reflection failed")
    assert not dream_mod._natural_end("the user asked")


def test_a_type_confused_journal_record_never_takes_status_or_the_page_down(tmp_path):
    rig = _Rig(tmp_path)
    (rig.paths.data / "helix_dream.json").write_text(json.dumps({
        "version": 1, "sessions": [{"id": "x", "day": "2026-09-01", "kind": "nightly",
                                    "window_start": "2026-09-01T23:00:00", "window_end": "2026-09-02T07:00:00",
                                    "ended": "2026-09-02T07:00:00", "drafts": 3, "applied": "none", "plan": 7,
                                    "facts_noted": "many", "discoveries": {"a": 1}, "limit_log": 4,
                                    "stopped_reason": "the plan was done"}],
        "report_pending": False, "rebuild_pending": False}), encoding="utf-8")
    entry = rig.dream.journal_entries(5)[0]
    assert entry["drafts"] == [] and entry["applied"] == [] and entry["counts"]["facts"] == 0
    status = rig.dream.status()
    assert "Last session (2026-09-01): nothing drafted (the plan was done)." in status
    assert "The plan's limit paused it." not in status  # a limit_log that is not a list is no pause
    assert rig.dream.journal_tail(1).startswith("- 2026-09-01 23:00–07:00 (nightly): tried 0")
    # An orphan of that shape is closed with a report too, not a stack trace on the first heartbeat.
    (rig.paths.data / "helix_dream.json").write_text(json.dumps({
        "version": 1, "sessions": [{"id": "y", "day": "2026-09-02", "kind": "nightly",
                                    "window_start": "2026-09-02T23:00:00", "window_end": "2026-09-03T07:00:00",
                                    "drafts": 3, "applied": "none", "plan": 7}],
        "report_pending": False, "rebuild_pending": False}), encoding="utf-8")
    fresh = _Rig(tmp_path, settings=_Settings({"dream_enabled": False}))
    fresh.dream.tick()
    closed = fresh.journal()["sessions"][-1]
    assert closed["ended"] and closed["stopped_reason"] == "HELIX closed mid-session" and closed["drafts"] == []
    assert fresh.dream.morning_report().startswith("Last night I found nothing worth changing")


def test_a_draft_that_changes_a_documented_choice_is_flagged_for_a_careful_review(tmp_path):
    rewire = Request(text="Rewire the Procurement Watcher in services/agents.py to the documented endpoint "
                          "instead of the sgs one; add a test.", origin="research", changes_decision=True)
    plain = Request(text="Shorten the morning brief in services/agents.py.")
    rig = _Rig(tmp_path, mind=_Mind(requests=(rewire, plain), summary=_night_summary(discoveries=[])))
    rig.dream.tick()
    s = rig.last()
    assert [d["changes_decision"] for d in s["drafts"]] == [True, False]
    assert [p["changes_decision"] for p in s["plan"]] == [True, False]
    assert [d["changes_decision"] for d in rig.dream.journal_entries(1)[0]["drafts"]] == [True, False]
    assert ("2 are waiting for your review. One of them changes a documented choice in the code — review it "
            "carefully.") in rig.dream.morning_report()


def test_an_inactive_plan_reads_as_the_rail_not_answering_never_as_a_downgrade(tmp_path):
    assert _Rig(tmp_path).dream._rail_problem() is None  # no subscription wired: nothing to check
    assert _Rig(tmp_path, subscription=_Sub()).dream._rail_problem() is None
    off = _Rig(tmp_path, subscription=_Sub(active=False, why="no setup-token"))
    assert off.dream._rail_problem() == "the plan isn't available right now (no setup-token)"
    assert off.dream._probe() == (False, "the plan isn't available right now (no setup-token)")
    assert off.chat.prompts == []  # a rail that is off is never asked
    dead = _Rig(tmp_path, subscription=_Sub(raises=RuntimeError("claude.exe died")))
    assert dead.dream._rail_problem() == "the plan isn't answering: claude.exe died"
    # While paused, the probe waits for the plan itself to come back — then asks it one cheap question.
    clock = _Clock()
    chat = _ProbeChat(PLAN, probes=("OK",), clock=clock)
    rig = _Rig(tmp_path, chat=chat, clock=clock, subscription=_Sub(active=False, why="limit", back_after=1))
    rig.dream._stop = _ClockEvent(clock)
    _raise_on_start(rig.lane, "rate limit exceeded")
    rig.dream.tick()
    assert chat.probe_times == [datetime(2026, 9, 4, 23, 55)]  # 23:25 found the rail off; 23:55 asked it
    assert len(rig.lane.requests) == 3 and rig.last()["stopped_reason"] == "the plan was done"
    assert any("still paused at 23:25 — the plan isn't available right now (limit)" in n
               for n in rig.last()["notes"])


# ----------------------------------------------------------------------------- frozen truth (config)
def test_build_info_reads_the_stamp_and_is_empty_without_one(tmp_path):
    stamp = tmp_path / "build_info.json"
    assert config.build_info(stamp) == {}
    stamp.write_text(json.dumps({"source_root": "C:/HELIX", "python": "C:/py.exe", "sha": "abc"}),
                     encoding="utf-8")
    assert config.build_info(stamp)["sha"] == "abc"
    stamp.write_text("[]", encoding="utf-8")
    assert config.build_info(stamp) == {}
    assert config.build_info() == {}  # the dev tree ships no stamp


def test_source_root_and_dev_python_in_development_are_the_tree_and_this_python(tmp_path):
    paths = config.AppPaths(root=tmp_path, data=tmp_path / "data", frozen=False)
    assert paths.source_root == tmp_path and paths.dev_python == sys.executable
    assert paths.is_frozen is False


def test_source_root_and_dev_python_when_frozen_come_from_settings_then_the_stamp(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)          # a repo (a linked worktree has a .git FILE — also fine)
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    paths = config.AppPaths(root=tmp_path / "dist" / "HELIX", data=data, frozen=True)
    assert paths.is_frozen is True
    monkeypatch.setattr(config, "build_info", lambda path=None: {})
    assert paths.source_root is None and paths.dev_python is None  # nothing known: honest None
    monkeypatch.setattr(config, "build_info",
                        lambda path=None: {"source_root": str(repo), "python": str(python)})
    assert paths.source_root == repo and paths.dev_python == str(python)
    # The settings override the stamp — and only count when they point at something real. A linked
    # worktree (what HELIX_V3 is) has a .git FILE, not a directory: it must count as a repo too.
    other = tmp_path / "moved"
    other.mkdir()
    (other / ".git").write_text("gitdir: ../repo/.git/worktrees/moved", encoding="utf-8")
    (data / "helix_settings.json").write_text(json.dumps(
        {"source_root": str(other), "dev_python": str(tmp_path / "missing.exe")}), encoding="utf-8")
    assert paths.source_root == other
    assert paths.dev_python == str(python)  # the setting's file is missing → the stamp's interpreter
    (data / "helix_settings.json").write_text(json.dumps({"source_root": str(tmp_path / "nowhere")}),
                                              encoding="utf-8")
    assert paths.source_root == repo  # a setting that is not a repo falls back to the stamp
    monkeypatch.setattr(config, "build_info", lambda path=None: {"source_root": str(tmp_path / "plain")})
    (tmp_path / "plain").mkdir()
    assert paths.source_root is None  # no .git → no source


def test_the_dream_journal_is_on_the_guard_skip_list():
    assert "helix_dream.json" in config.VOLATILE_STORE_NAMES
    assert any(p.name == "helix_dream.json" for p in config.volatile_data_paths(Path("d")))


def _build_module():
    """build.py by path — never `import build`, which an installed PyPA `build` would shadow."""
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "build.py"
    spec = importlib.util.spec_from_file_location("helix_build_py", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_build_py_stamps_the_source_root_interpreter_sha_and_time(tmp_path):
    build_mod = _build_module()
    out = build_mod.stamp_build_info(tmp_path, tmp_path / "build" / "build_info.json", python="C:/py.exe",
                                     sha="abc123", now=datetime(2026, 9, 4, 22, 0))
    info = json.loads(out.read_text(encoding="utf-8"))
    assert info == {"source_root": str(tmp_path.resolve()), "python": "C:/py.exe", "sha": "abc123",
                    "built_at": "2026-09-04T22:00:00"}
    assert build_mod.BUILD_INFO == build_mod.ROOT / "build" / "build_info.json"  # gitignored, not helix/
    assert build_mod._git_head(tmp_path) == ""  # not a repo: no sha, no crash


# ----------------------------------------------------------------------------- the composition root
def test_the_container_wires_the_dream_and_points_the_gate_at_the_source(tmp_path, monkeypatch):
    pytest.importorskip("PyQt6.QtWidgets")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    root = config.AppPaths.resolve().root
    monkeypatch.setattr(config.AppPaths, "resolve",
                        staticmethod(lambda: config.AppPaths(root=root, data=tmp_path)))
    from helix.app.container import Container

    c = Container()
    assert isinstance(c.dream, DreamService) and c.rebuilder is not None
    assert c.evolve._dream is c.dream, "Evolve must defer to the dream it was handed"
    assert c.selfdev._root == c.paths.source_root == root
    assert c.selfdev._python == sys.executable
    assert c.dream._suite_runner == c.selfdev.verify
    assert c.dream._rebuilder is c.rebuilder and c.dream._growth_model is c.growth_model
    c.dream.activity = lambda: 1.0  # the shell registers its presence probe exactly like this
    assert c.dream.activity() == 1.0 and c.dream._activity_seconds() == 1.0
    if hasattr(c.tools, "attach_dream"):
        assert c.tools._dream is c.dream
    # Phase 2: the session runs the mind (built on the real collaborators) and checks the plan's rail.
    from helix.services.dream_mind import DreamMind

    assert isinstance(c.dream_mind, DreamMind) and c.dream._mind is c.dream_mind
    assert c.dream._subscription is c.subscription
    assert c.dream_mind._conversation is c.conversation and c.conversation._verified is c.verified
