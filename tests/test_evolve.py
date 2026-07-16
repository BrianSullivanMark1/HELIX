"""EvolveService — the nightly self-improvement pass: gating, QUIET nights, and real proposals."""
from __future__ import annotations

from datetime import datetime

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
    def __init__(self, hour=4):
        self.dt = datetime(2026, 7, 16, hour, 5, 0)

    def now(self):
        return self.dt


class _Lane:
    def __init__(self, busy=False, raises=False):
        self._busy = busy
        self._raises = raises
        self.requests = []

    def busy(self):
        return self._busy

    def start(self, request):
        if self._raises:
            raise RuntimeError("boom")
        self.requests.append(request)
        return True


class _SelfDev:
    def __init__(self, pending=None):
        self._pending = list(pending or [])

    def pending(self):
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


def _svc(chat=None, settings=None, clock=None, lane=None, selfdev=None, lessons=None, tail="a log"):
    return EvolveService(
        chat or _Chat("QUIET"), lessons, lane or _Lane(), selfdev or _SelfDev(),
        settings or _Settings(), clock or _Clock(), log_tail=lambda: tail,
    )


def test_disabled_toggle_never_calls_the_model():
    chat, settings = _Chat("A proposal."), _Settings({"evolve_enabled": False})
    _svc(chat=chat, settings=settings).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


def test_before_the_quiet_hours_skips():
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, clock=_Clock(hour=2)).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


def test_a_daytime_launch_never_fires_the_nightly_pass():
    # The window is 3-6 AM only: opening HELIX at 11 AM must not run a "nightly" pass at lunch.
    chat, settings = _Chat("A proposal."), _Settings()
    _svc(chat=chat, settings=settings, clock=_Clock(hour=11)).tick()
    assert chat.prompts == [] and "evolve_last_run" not in settings.d


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
