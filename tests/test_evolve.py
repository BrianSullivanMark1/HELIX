"""EvolveService — the nightly self-improvement pass: gating, QUIET nights, and real proposals."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from helix.ports.llm import Reply, Text
from helix.services import evolve as evolve_mod
from helix.services.evolve import EVOLVE_SYSTEM, EvolveService
from helix.services.lessons import LessonsService


class _Chat:
    def __init__(self, text=""):
        self.text = text
        self.prompts = []
        self.systems = []

    def chat(self, turns, *, system=None, tools=None):
        self.prompts.append("".join(b.text for t in turns for b in t.blocks if isinstance(b, Text)))
        self.systems.append(system)
        return Reply(blocks=(Text(self.text),))


class _Settings:
    def __init__(self, d=None):
        # A connected brain by default — the no-brain gate is exercised by its own test below.
        self.d = {"claude_api_key": "sk-test"}
        self.d.update(d or {})

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


class _Clock:
    def __init__(self, hour=4, minute=5, day=16):
        self.dt = datetime(2026, 7, day, hour, minute, 0)

    def now(self):
        return self.dt

    def advance(self, **kw):
        """Move the wall clock on — the catch-up path reads the GAP between heartbeats to tell a
        launch/wake apart from a tick in the middle of a live session."""
        self.dt += timedelta(**kw)


class _Lane:
    def __init__(self, busy=False, raises=False):
        self._busy = busy
        self._raises = raises
        self.requests = []
        self.models = []
        self.unattended = []  # nobody is in the room at 3 AM — the lane must be told, per draft

    def busy(self):
        return self._busy

    def start(self, request, model=None, unattended=False):
        if self._raises:
            raise RuntimeError("boom")
        self.requests.append(request)
        self.models.append(model)
        self.unattended.append(unattended)
        return True


class _GrowthModel:
    """Mirrors GrowthModelResolver.work_model: deep=Fable 5, standard=Opus 4.8 floor."""

    def work_model(self, deep):
        return "claude-fable-5" if deep else "claude-opus-4-8"


class _SelfDev:
    def __init__(self, pending=None):
        self._pending = list(pending or [])
        self.probes = 0  # pending() shells out to git several times — the heartbeat must not spam it

    def pending(self):
        self.probes += 1
        return self._pending


class _Mem:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


class _ImmediateThread:
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def _lessons(rules_by_user):
    """A real LessonsService over a fake store, so Evolve reads through the real accessors."""
    mem = _Mem()
    mem.d["lessons"] = rules_by_user
    return LessonsService(_Chat(), None, mem, _Clock())


def _svc(chat=None, settings=None, clock=None, lane=None, selfdev=None, lessons=None, tail="a log",
         growth_model=None, data_dir=None):
    return EvolveService(
        chat or _Chat("QUIET"), lessons, lane or _Lane(), selfdev or _SelfDev(),
        settings or _Settings(), clock or _Clock(), log_tail=lambda: tail, growth_model=growth_model,
        data_dir=data_dir,
    )


# ----- the backlog (user-queued ideas) and the journal -----

def test_backlog_is_queued_deduped_and_mined_first(tmp_path):
    svc = _svc(data_dir=tmp_path)
    assert svc.add_backlog("teach the studio to rotate parts")
    assert svc.add_backlog("Teach the studio to rotate parts")  # case-insensitive dedupe
    assert svc.backlog() == ["teach the studio to rotate parts"]
    material = svc._material()
    assert "IMPROVEMENT BACKLOG" in material and "rotate parts" in material
    # …and the backlog section comes FIRST, matching the system prompt's "prefer" instruction.
    assert material.index("IMPROVEMENT BACKLOG") < material.index("LESSONS")


def test_a_takes_line_crosses_the_item_off_and_journals(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat = _Chat(
        "Fix the studio rotation controls.\n"
        "TAKES: teach the studio to rotate parts\n"
        "EFFORT: standard"
    )
    lane = _Lane()
    svc = _svc(chat=chat, lane=lane, data_dir=tmp_path, growth_model=_GrowthModel())
    svc.add_backlog("teach the studio to rotate parts")
    svc.tick()  # 4 AM — in window
    assert lane.requests == ["Fix the studio rotation controls."]  # TAKES/EFFORT lines stripped
    assert lane.models == ["claude-opus-4-8"]                      # standard tier honoured
    assert svc.backlog() == []                                     # crossed off the queue
    journal = (tmp_path / "evolve_journal.md").read_text(encoding="utf-8")
    assert "drafted (backlog, standard)" in journal


def test_a_quiet_night_is_journaled(tmp_path, monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    svc = _svc(data_dir=tmp_path)  # the default chat answers QUIET
    svc.tick()
    assert "quiet night" in (tmp_path / "evolve_journal.md").read_text(encoding="utf-8")
    assert "quiet night" in svc.journal_tail()


def test_no_data_dir_disables_backlog_and_journal_quietly():
    svc = _svc()
    assert svc.add_backlog("an idea") is False
    assert svc.backlog() == [] and svc.journal_tail() == ""


def test_disabled_toggle_never_calls_the_model():
    chat, settings = _Chat("A proposal."), _Settings({"evolve_enabled": False})
    _svc(chat=chat, settings=settings).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


def test_before_the_quiet_hours_skips():
    # 2 AM is outside the window. With no stamp there is no anchor either, so the pass seeds one —
    # BACKDATED to last night's 3 AM, which is what keeps tonight's window (three tests down) live.
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, clock=_Clock(hour=2)).tick()
    assert chat.prompts == [] and settings.d["evolve_last_run"] == "2026-07-15"


def test_a_daytime_launch_never_fires_the_nightly_pass():
    # The window is 3-6 AM only: opening HELIX at 11 AM must not run a "nightly" pass at lunch. It
    # seeds this morning's 3 AM as the anchor — "last night is done" — and drafts nothing.
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, clock=_Clock(hour=11)).tick()
    assert chat.prompts == [] and settings.d["evolve_last_run"] == "2026-07-16"


def test_the_backdated_seed_still_lets_tonights_window_run(monkeypatch):
    # The seed must not eat the night it was written on: HELIX opened at 2 AM and left running must
    # still evolve at 3.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings, lane, clock = _Chat("A proposal."), _Settings(), _Lane(), _Clock(hour=2)
    svc = _svc(chat=chat, settings=settings, lane=lane, clock=clock)
    svc.tick()
    clock.advance(hours=1, minutes=10)
    svc.tick()
    assert lane.requests == [chat.text] and settings.d["evolve_last_run"] == "2026-07-16"


def test_no_connected_brain_skips_silently_without_stamping():
    # A fresh install has no subscription token and no API key: skip quietly, unstamped — the
    # first night AFTER the user connects must still run.
    chat = _Chat("A proposal.")
    settings = _Settings({"claude_api_key": ""})
    _svc(chat=chat, settings=settings).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d
    # An oauth token alone is a brain too.
    settings2 = _Settings({"claude_api_key": "", "claude_code_oauth_token": "tok"})
    lane = _Lane()
    _svc(chat=_Chat("QUIET"), settings=settings2, lane=lane).tick()
    assert settings2.d["evolve_last_run"] == "2026-07-16"


def test_already_ran_today_skips():
    chat, settings = _Chat("A proposal."), _Settings({"evolve_last_run": "2026-07-16"})
    _svc(chat=chat, settings=settings).tick()
    assert chat.prompts == []


def test_pending_draft_skips_without_stamping():
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, selfdev=_SelfDev(pending=[object()])).tick()
    # Unstamped on purpose: once the user resolves the draft, tonight's window still runs.
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


def test_busy_lane_skips_without_stamping():
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, lane=_Lane(busy=True)).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


def test_quiet_reply_stamps_and_never_drafts(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings, lane = _Chat("QUIET"), _Settings(), _Lane()
    _svc(chat=chat, settings=settings, lane=lane, clock=_Clock(hour=3)).tick()  # 3 AM: window open
    assert settings.d["evolve_last_run"] == "2026-07-16"
    assert lane.requests == [] and chat.systems == [EVOLVE_SYSTEM]


def test_empty_reply_is_a_quiet_night(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings, lane = _Settings(), _Lane()
    _svc(chat=_Chat(""), settings=settings, lane=lane).tick()
    assert settings.d["evolve_last_run"] == "2026-07-16" and lane.requests == []


def test_proposal_starts_a_draft_with_the_material(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat = _Chat("Debounce the reminder repeat in services/reminders.py; the log shows two fires.")
    settings, lane = _Settings(), _Lane()
    lessons = _lessons({"": ["Keep replies short"], "brian": ["Call the project Falcon"]})
    _svc(chat=chat, settings=settings, lane=lane, lessons=lessons,
         tail="ERROR reminders: fired twice").tick()
    assert lane.requests == [chat.text]                        # the proposal, verbatim, once
    assert settings.d["evolve_last_run"] == "2026-07-16"
    prompt = chat.prompts[-1]                                 # the model saw each speaker's lessons
    assert "Keep replies short" in prompt and "Call the project Falcon" in prompt
    assert "fired twice" in prompt                             # ...and the log tail


def test_the_nightly_draft_is_marked_unattended_so_the_house_stays_quiet(monkeypatch):
    # Growth narration is deliberately SPOKEN even through a sleeping mic, so the one thing this pass
    # must do differently from a draft the user asked for is announce that nobody is in the room.
    # Without the flag the 3 AM pass reads every coder step aloud into a dark bedroom.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    lane = _Lane()
    _svc(chat=_Chat("Debounce the reminder repeat in services/reminders.py."), lane=lane).tick()
    assert lane.unattended == [True]


def test_proposal_effort_standard_drafts_on_the_opus_floor(monkeypatch):
    # The Fable-5 proposal sized the task as small → the coder drafts on the Opus 4.8 work floor,
    # and the EFFORT line is stripped from the request handed to the coder.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    lane = _Lane()
    _svc(chat=_Chat("Fix a one-line guard in services/reminders.py.\nEFFORT: standard"),
         lane=lane, growth_model=_GrowthModel()).tick()
    assert lane.models == ["claude-opus-4-8"]
    assert lane.requests == ["Fix a one-line guard in services/reminders.py."]  # EFFORT line stripped
    assert "EFFORT" not in lane.requests[0]


def test_proposal_effort_deep_drafts_on_fable(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    lane = _Lane()
    _svc(chat=_Chat("Rework the retry/backoff across adapters.\nEFFORT: deep"),
         lane=lane, growth_model=_GrowthModel()).tick()
    assert lane.models == ["claude-fable-5"]


def test_proposal_without_an_effort_line_defaults_to_deep(monkeypatch):
    # A missing/garbled EFFORT line falls back to the strongest model — the safe default for self-editing.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    lane = _Lane()
    _svc(chat=_Chat("Some worthwhile change with no effort tag."),
         lane=lane, growth_model=_GrowthModel()).tick()
    assert lane.models == ["claude-fable-5"]


def test_no_growth_model_leaves_the_coder_default(monkeypatch):
    # Without a resolver the lane gets model=None → the coder keeps its own configured (growth) model.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    lane = _Lane()
    _svc(chat=_Chat("A change.\nEFFORT: standard"), lane=lane, growth_model=None).tick()
    assert lane.models == [None]


def test_second_tick_same_day_runs_nothing(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings = _Chat("QUIET"), _Settings()
    s = _svc(chat=chat, settings=settings)
    s.tick()
    s.tick()
    assert len(chat.prompts) == 1


def test_lane_failure_never_crashes_tick(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings()
    _svc(chat=_Chat("A proposal."), settings=settings, lane=_Lane(raises=True)).tick()
    assert settings.d["evolve_last_run"] == "2026-07-16"  # stamped first — no retry loop all night


# ----- the catch-up: a laptop that sleeps through 3 AM still evolves, once, at a quiet hour -----
def test_a_night_missed_while_the_machine_slept_is_caught_up_at_the_next_quiet_hour(monkeypatch):
    # The heartbeat is a QTimer in a live process: on a machine that sleeps at night no tick ever
    # lands in the window, so without a catch-up the "nightly" pass never runs at all, ever. But the
    # lid opening at 9 AM means the user is HERE — the owed night is armed then, and lands that
    # evening, when a few minutes of coder work interrupt nobody.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat = _Chat("Debounce the reminder repeat in services/reminders.py.")
    settings = _Settings({"evolve_last_run": "2026-07-14"})  # two nights ago — the lid was shut
    lane, clock = _Lane(), _Clock(hour=9)
    svc = _svc(chat=chat, settings=settings, lane=lane, clock=clock)
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-14"  # armed, not drafted
    clock.advance(hours=4)     # 1 PM, mid-afternoon: still not an hour worth taking the machine over
    svc.tick()
    assert lane.requests == []
    clock.advance(hours=7)     # 8 PM: the quiet band opens and the owed night finally lands
    svc.tick()
    assert lane.requests == [chat.text]
    assert settings.d["evolve_last_run"] == "2026-07-16"
    clock.advance(minutes=30)  # …and ONCE: the rest of the evening's heartbeats draft nothing more,
    svc.tick()
    clock.advance(hours=6)     # not even the next wake, which finds the night already caught up
    svc.tick()
    assert len(lane.requests) == 1


def test_a_catch_up_never_takes_over_the_machine_in_the_middle_of_the_working_day(monkeypatch):
    # A draft is not a log line: it is minutes of coder work that flips the orb, owns the status line
    # and (in the shell) deafens hands-free voice, behind a Stop button that refuses. A night owed by
    # a laptop that slept must never buy the user that at 9 in the morning or 3 in the afternoon.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings({"evolve_last_run": "2026-07-15"})  # last night was missed
    lane, clock = _Lane(), _Clock(hour=8, minute=30)
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, clock=clock)
    for _ in range(10):  # 8:30 AM through 5:30 PM — every hour is a fresh "wake" to the gap check
        svc.tick()
        clock.advance(hours=1)
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-15"


def test_a_machine_only_ever_awake_at_work_still_drafts_rather_than_going_dormant(monkeypatch):
    # The quiet band must never become the new silence. A desk machine asleep every night and awake
    # only between 10 and 5 never sees an in-band tick, so after three owed nights the fuse blows and
    # the next arrival drafts whatever the clock says — degraded to roughly one draft every fourth
    # day, which is a nuisance, where dormant would be a feature that quietly does not exist.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings({"evolve_last_run": "2026-07-13"})
    lane, clock = _Lane(), _Clock(hour=10, day=14)
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, clock=clock)
    svc.tick()                            # Tuesday 10 AM: one night owed — held
    clock.advance(hours=7)
    svc.tick()                            # 5 PM, lid shut for the night
    assert lane.requests == []
    clock.advance(hours=17)
    svc.tick()                            # Wednesday 10 AM: two nights owed — still held
    clock.advance(hours=7)
    svc.tick()
    assert lane.requests == []
    clock.advance(hours=17)
    svc.tick()                            # Thursday 10 AM: three nights owed — the fuse blows
    assert lane.requests == ["A proposal."] and settings.d["evolve_last_run"] == "2026-07-16"
    clock.advance(hours=1)                # …and it is still ONE draft, never a backlog of three
    svc.tick()
    clock.advance(hours=6)
    svc.tick()
    assert len(lane.requests) == 1


def test_a_week_away_catches_up_exactly_one_draft_and_never_a_backlog(monkeypatch):
    # Nine missed nights are still one improvement to review, not nine branches waiting — and a debt
    # that old is past the patience fuse, so it lands on arrival rather than waiting for the evening.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings({"evolve_last_run": "2026-07-07"})
    lane, clock = _Lane(), _Clock(hour=15)
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, clock=clock)
    for _ in range(6):
        svc.tick()
        clock.advance(hours=1)  # every hour is a fresh "wake" as far as the gap check is concerned
    assert lane.requests == ["A proposal."]


def test_a_missed_night_waits_for_a_quiet_hour_instead_of_drafting_mid_session(monkeypatch):
    # A night owed while the machine stayed awake waits for the next launch or wake to be ARMED — and
    # then waits again for an hour that is actually quiet, so it lands in the evening, not at 10 AM.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings({"evolve_last_run": "2026-07-15"})  # last night ran on time
    lane, clock = _Lane(busy=True), _Clock(hour=2, minute=50)  # a draft is in flight through tonight
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, clock=clock)
    while clock.dt.hour < 7:  # awake all night, heartbeats every few minutes — never a "wake" gap
        svc.tick()
        clock.advance(minutes=4)
    lane._busy = False  # the user clears the draft at 7 AM — mid-session, so the owed night waits
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-15"
    clock.advance(hours=3)  # the lid closes and reopens at 10 AM: armed now, but the day is theirs
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-15"
    clock.advance(hours=10)  # 8 PM: NOW the missed night is caught up
    svc.tick()
    assert lane.requests == ["A proposal."] and settings.d["evolve_last_run"] == "2026-07-16"


def test_a_fresh_install_seeds_the_anchor_and_never_drafts_on_setup_day(monkeypatch):
    # A first-time user must not get a self-edit branch minutes after installing — but the first real
    # night after that must still land, even if it is only reached late.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings, lane, clock = _Chat("A proposal."), _Settings(), _Lane(), _Clock(hour=11)
    svc = _svc(chat=chat, settings=settings, lane=lane, clock=clock)
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-16"
    clock.advance(hours=4)  # later the same afternoon: nothing is owed yet
    svc.tick()
    assert lane.requests == []
    clock.advance(hours=16)  # 7 AM the next morning — the first real night has passed
    svc.tick()
    assert lane.requests == [chat.text] and settings.d["evolve_last_run"] == "2026-07-17"


# ----- a stamp from the future: a moved clock must not switch the pass off -----
def test_a_stamp_dated_after_today_is_re_anchored_instead_of_disabling_the_pass(monkeypatch):
    # A laptop flown two timezones east, a wrong BIOS date corrected on the next boot, a hand-edited
    # settings file: the stamp now claims a night this machine cannot have run yet. Trusted, is_due
    # would answer "nothing owed" on every heartbeat until wall-clock crawls past it — days of a
    # silently dormant nightly pass, which is precisely the failure the catch-up exists to kill.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, lane = _Chat("A proposal."), _Lane()
    settings = _Settings({"evolve_last_run": "2026-07-20"})  # four days ahead of the clock below
    _svc(chat=chat, settings=settings, lane=lane, clock=_Clock(hour=4)).tick()  # 4 AM: window open
    assert lane.requests == [chat.text]
    assert settings.d["evolve_last_run"] == "2026-07-16"  # …and the bad stamp is gone in one tick


def test_a_future_stamp_outside_the_window_is_repaired_without_drafting_at_lunchtime(monkeypatch):
    # Healing the stamp must not become an excuse to draft in the middle of somebody's afternoon: the
    # future stamp reads as NO anchor, which is the fresh-profile path — re-seed tonight's 3 AM and
    # say nothing. The very next night then runs normally, which is the whole point.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, lane, clock = _Chat("A proposal."), _Lane(), _Clock(hour=11)
    settings = _Settings({"evolve_last_run": "2027-01-01"})
    svc = _svc(chat=chat, settings=settings, lane=lane, clock=clock)
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-16"
    clock.advance(hours=20)  # 7 AM tomorrow — the first real night after the repair
    svc.tick()
    assert lane.requests == [chat.text] and settings.d["evolve_last_run"] == "2026-07-17"


# ----- the cost of asking: pending() is git subprocesses, on the GUI thread -----
def test_an_unreviewed_draft_is_not_re_probed_through_git_on_every_heartbeat(monkeypatch):
    # The pending case is deliberately left unstamped, so tick() re-enters on every 15s heartbeat for
    # the whole window — ~720 times. Each probe spawns several git processes on the GUI thread.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    selfdev, clock = _SelfDev(pending=[object()]), _Clock(hour=3)
    svc = _svc(settings=_Settings(), clock=clock, selfdev=selfdev)
    for _ in range(40):  # ten minutes of heartbeats
        svc.tick()
        clock.advance(seconds=15)
    assert selfdev.probes == 1


def test_the_pending_probe_is_retried_so_a_resolved_draft_still_runs_the_same_night(monkeypatch):
    # Throttling must not mean "asked once, gave up": once the user applies or discards the draft,
    # tonight's pass still runs — just up to ten minutes later.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    fake_monotonic = [1000.0]
    monkeypatch.setattr(evolve_mod.time, "monotonic", lambda: fake_monotonic[0])
    selfdev, lane, settings = _SelfDev(pending=[object()]), _Lane(), _Settings()
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, selfdev=selfdev,
               clock=_Clock(hour=3))
    svc.tick()
    svc.tick()
    assert selfdev.probes == 1 and lane.requests == []
    selfdev._pending.clear()   # the user reviews it
    fake_monotonic[0] += 601   # …and the next heartbeat past the throttle asks git again
    svc.tick()
    assert selfdev.probes == 2
    assert lane.requests == ["A proposal."] and settings.d["evolve_last_run"] == "2026-07-16"


# ----- the dream session owns the night: this pass stands down (READ_ME/DREAM.md §4) -----
class _Dream:
    def __init__(self, covers=True):
        self.covers = covers
        self.asked = 0

    def covers_tonight(self):
        self.asked += 1
        return self.covers


def test_the_pass_defers_to_a_dream_session_that_covers_tonight(monkeypatch):
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings, dream = _Chat("A proposal."), _Settings(), _Dream(covers=True)
    svc = _svc(chat=chat, settings=settings)
    svc.set_dream(dream)
    svc.tick()  # 4 AM, in window — and yet nothing: the dream drafts tonight, not this pass
    assert chat.prompts == [] and "evolve_last_run" not in settings.d and dream.asked == 1
    dream.covers = False  # dreaming switched off: the pass resumes exactly where it stood
    svc.tick()
    assert len(chat.prompts) == 1 and settings.d["evolve_last_run"] == "2026-07-16"


def test_a_dream_handed_to_the_constructor_counts_too():
    chat = _Chat("A proposal.")
    EvolveService(chat, None, _Lane(), _SelfDev(), _Settings(), _Clock(), log_tail=lambda: "",
                  dream=_Dream(covers=True)).tick()
    assert chat.prompts == []


def test_a_confused_dream_never_stops_the_plain_pass(monkeypatch):
    class _Broken:
        def covers_tonight(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    chat, settings = _Chat("QUIET"), _Settings()
    svc = _svc(chat=chat, settings=settings)
    svc.set_dream(_Broken())
    svc.tick()
    assert len(chat.prompts) == 1


def test_deferring_keeps_the_heartbeat_fresh_so_switching_the_dream_off_never_drafts_into_the_day(
        monkeypatch):
    # A week of dreaming (the stamp is stale, past the patience fuse), then the user switches the
    # dream off at two in the afternoon. Had the deferred ticks read as sleep, the first tick after
    # the switch would count as a WAKE and the fuse would draft straight into the working day. It
    # must wait for the evening band like any other owed night.
    monkeypatch.setattr(evolve_mod.threading, "Thread", _ImmediateThread)
    settings = _Settings({"evolve_last_run": "2026-07-08"})
    lane, clock, dream = _Lane(), _Clock(hour=14, minute=0), _Dream(covers=True)
    svc = _svc(chat=_Chat("A proposal."), settings=settings, lane=lane, clock=clock)
    svc.set_dream(dream)
    svc.tick()
    clock.advance(seconds=15)
    svc.tick()
    dream.covers = False
    clock.advance(seconds=15)
    svc.tick()
    assert lane.requests == [] and settings.d["evolve_last_run"] == "2026-07-08"
    clock.advance(hours=6)  # 20:00 — the quiet band: NOW the owed night is caught up, once
    svc.tick()
    assert lane.requests == ["A proposal."] and settings.d["evolve_last_run"] == "2026-07-16"


def test_mark_night_covered_moves_the_stamp_forward_only():
    settings = _Settings({"evolve_last_run": "2026-07-14"})
    svc = _svc(settings=settings)
    svc.mark_night_covered(date(2026, 7, 16))
    assert settings.d["evolve_last_run"] == "2026-07-16"
    svc.mark_night_covered(date(2026, 7, 15))
    assert settings.d["evolve_last_run"] == "2026-07-16"  # never backwards
    fresh = _Settings()
    _svc(settings=fresh).mark_night_covered(date(2026, 7, 16))
    assert fresh.d["evolve_last_run"] == "2026-07-16"
    junk = _Settings({"evolve_last_run": "not a date"})
    _svc(settings=junk).mark_night_covered(date(2026, 7, 16))
    assert junk.d["evolve_last_run"] == "2026-07-16"


def test_the_dream_reads_the_same_material_and_writes_the_same_journal(tmp_path):
    svc = _svc(data_dir=tmp_path, lessons=_lessons({"": ["Keep replies short"]}), tail="ERROR x")
    svc.add_backlog("an idea")
    material = svc.material()
    assert material == svc._material() and "an idea" in material and "Keep replies short" in material
    svc.journal("dream: session started")
    assert "dream: session started" in svc.journal_tail()
    svc.take_backlog("an idea")
    assert svc.backlog() == []
