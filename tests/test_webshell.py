"""ShellSession tests — the web console's brain, driven through a fake container.

These pin the ported Qt-console contracts at the SERVER: the submit gauntlet's order, the stop
contract, the QUIET sentinel, the coalescing announcer's wording, the cleanup-answer classifier,
the voice-button truth rules, and the board's legend ordering. The event stream is captured as a
plain list; no FastAPI, no sockets, no threads beyond the ones the shell itself spawns.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from helix.adapters.signal_bus import SignalBus
from helix.api import shell as shell_mod
from helix.api.shell import ShellSession, _Board
from helix.domain.events import SelfChangeFinished


class _Settings:
    def __init__(self, **kv):
        self._d = dict(kv)

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class _Conversation:
    def __init__(self):
        self.turns = []
        self.reply = "Done."

    def recent_messages(self, n):
        return ["x"]  # not a fresh install — no greeting

    def run_turn(self, text, **kw):
        self.turns.append((text, kw))
        return self.reply


class _Queue:
    def __init__(self):
        self.cancelled = False
        self.cleared = []

    def active_names(self):
        return []

    def cancel_active(self):
        self.cancelled = True

    def clear_queued(self):
        return self.cleared


class _Container:
    def __init__(self):
        self.settings = _Settings(claude_api_key="sk-test")
        self.bus = SignalBus()
        self.conversation = _Conversation()
        self.build_queue = _Queue()
        self.voice_id = None

    class _Stub:
        def __getattr__(self, name):
            return lambda *a, **k: None

    def __getattr__(self, name):  # everything else the shell touches lazily
        return _Container._Stub()


@pytest.fixture
def rig():
    container = _Container()
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    yield container, events, sh
    sh.shutdown()


def _texts(events, t="msg"):
    return [e.get("text") for e in events if e.get("t") == t]


# ----- the camera -----

def _cam_events(events):
    return [e for e in events if e.get("t") == "camera"]


def test_manual_camera_open_raises_a_fresh_window(rig):
    _c, events, sh = rig
    out = sh.camera_open()
    assert out["ok"] and out["id"]
    cams = _cam_events(events)
    assert len(cams) == 1 and cams[0]["manual"] is True and cams[0]["id"] == out["id"]
    assert sh._camera is not None and sh._camera["manual"]


def test_reopening_the_camera_always_pops_a_new_window(rig):
    # THE BUG: after one session the camera "wouldn't pop up again". A manual open must ALWAYS
    # retire any stale window and raise a fresh one — re-open can never stick.
    _c, events, sh = rig
    first = sh.camera_open()["id"]
    second = sh.camera_open()["id"]
    assert first != second
    cams = _cam_events(events)
    # two fresh windows, newest wins; the swap is silent (the face remounts on the new id, so no
    # extra close event is needed — one camera event per open is the whole contract).
    assert [c["id"] for c in cams] == [first, second]
    assert sh._camera["id"] == second


def test_a_manual_frame_becomes_a_component_turn(rig, monkeypatch):
    _c, events, sh = rig
    started: list = []
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: started.append(a))
    cid = sh.camera_open()["id"]
    assert sh.camera_frame(cid, b"\x89PNG-fake") is True
    # The panel is persistent now: a shot does NOT fold it — the user keeps working with it.
    assert sh._camera is not None and sh._camera["id"] == cid
    assert not any(e.get("t") == "camera.close" for e in events)
    assert len(started) == 1                           # a turn was kicked off…
    assert "electronic component" in started[0][0]     # …with the identify-the-part brief
    assert started[0][2] and str(started[0][2][0]).endswith(".png")  # carrying the saved photo


def test_a_stale_frame_id_is_ignored(rig, monkeypatch):
    _c, _events, sh = rig
    monkeypatch.setattr(sh, "_start_turn", lambda *a, **k: pytest.fail("stale frame started a turn"))
    sh.camera_open()
    assert sh.camera_frame("not-the-open-id", b"x") is False


def test_a_hologram_command_with_no_name_falls_back_to_the_slug_instead_of_crashing(rig, tmp_path):
    # The hologram branch used to read `slug` on the right side of the very assignment that bound
    # it — a NameError for any payload whose name was empty, caught (and reported to the model as
    # "That didn't work: name 'slug' is not defined") only by the branch's own safety net.
    from helix.domain.events import CameraCommandRequested
    from helix.services.camera import CameraCommand

    container, _events, sh = rig
    container.builds = SimpleNamespace(workspace=lambda slug: tmp_path / slug)  # no mesh baked
    sh.camera_open()
    cmd = CameraCommand("hologram", {"slug": "iron-eye", "name": ""})
    container.bus.publish(CameraCommandRequested(request=cmd))
    reply = cmd.wait(timeout=2.0)
    assert reply is not None and "slug" not in reply.lower()
    assert "iron-eye" in reply  # the fallback name is the slug; no mesh → the honest "no mesh yet"


# ----- the submit gauntlet -----

def test_a_typed_sleep_with_no_ears_answers_honestly_and_never_runs_a_turn(rig):
    container, events, sh = rig
    sh.submit("go to sleep")
    sh._finish_wait()
    assert any("nothing to put it to sleep" in (x or "") or "nothing to put to sleep" in (x or "")
               for x in _texts(events)), _texts(events)
    assert container.conversation.turns == []


class _SleepyVoice:
    """Recording ears for the sleep-holder chain — the exact surface _on_sleep_requested and
    voice_state touch, nothing lazy: a missing method here should fail loudly, not stub silently."""

    def __init__(self, ears: bool = True):
        self._ears = ears
        self._muted = False
        self.muted_calls: list[tuple] = []
        self.learned: list[str] = []

    def supported(self):
        return self._ears

    def enabled(self):
        return self._ears

    def can_listen(self):
        return self._ears

    def prewarm_error(self):
        return ""

    def is_muted(self):
        return self._muted

    def set_muted(self, on, announce=True):
        self._muted = bool(on)
        self.muted_calls.append((bool(on), announce))

    def learn_sleep(self, command):
        self.learned.append(command)

    def shutdown(self):
        pass


def test_the_sleep_tool_holder_is_fulfilled_and_the_phrase_learned():
    """The go_to_sleep chain, end to end at the shell: the tool's holder is claimed, the SPOKEN
    phrase that led here is consolidated into a reflex (next time no model turn), the mute is quiet
    (the model's goodnight IS the announce), the holder settles True, and the UI hears the new
    voice state. Every link — a chain with one dead link reports sleep that never happened."""
    from helix.domain.events import SleepRequest, SleepRequested

    container = _Container()
    events: list[dict] = []
    voice = _SleepyVoice()
    sh = ShellSession(container, events.append, voice=voice)
    try:
        sh._last_user_utterance = "wind down for the night"  # what submit(from_voice=True) records
        req = SleepRequest()
        container.bus.publish(SleepRequested(request=req))
        assert req.wait(timeout=2.0) is True, req.error
        assert voice.muted_calls == [(True, False)]
        assert voice.learned == ["wind down for the night"]
        assert any(e.get("t") == "voice" and e.get("muted") for e in events)
    finally:
        sh.shutdown()


def test_the_sleep_holder_fails_honestly_with_no_ears(rig):
    from helix.domain.events import SleepRequest, SleepRequested

    container, events, sh = rig  # the rig's shell has voice=None
    req = SleepRequest()
    container.bus.publish(SleepRequested(request=req))
    assert req.wait(timeout=2.0) is False
    assert "nothing" in (req.error or "").lower()  # the model relays this instead of a goodnight


def test_an_abandoned_sleep_holder_never_mutes():
    """The worker gave up (cancel/timeout) before the shell got there: claiming fails and the ears
    must be left exactly as they were — a late mute would close the mic with nobody told."""
    from helix.domain.events import SleepRequest, SleepRequested

    container = _Container()
    voice = _SleepyVoice()
    sh = ShellSession(container, lambda e: None, voice=voice)
    try:
        req = SleepRequest()
        req.abandon()
        container.bus.publish(SleepRequested(request=req))
        assert voice.muted_calls == []
    finally:
        sh.shutdown()


def test_a_typed_stop_never_becomes_a_turn(rig):
    container, events, sh = rig
    sh.submit("stop")
    sh._finish_wait()
    assert container.conversation.turns == []
    assert container.build_queue.cancelled


def test_no_credential_keeps_the_message_and_hints_once(rig):
    container, events, sh = rig
    container.settings.set("claude_api_key", "")
    sh.submit("build me a timer")
    sh.submit("build me a timer")
    sh._finish_wait()
    keeps = [e for e in events if e.get("t") == "keep_input"]
    assert len(keeps) == 2 and keeps[0]["text"] == "build me a timer"
    hints = [x for x in _texts(events) if x and "kept your message" in x]
    assert len(hints) == 1, "the connect hint is shown ONCE per disconnect"
    assert container.conversation.turns == []


def test_a_normal_submit_runs_a_turn_with_the_situation_block(rig):
    container, events, sh = rig
    sh.submit("hello there")
    sh._finish_wait()
    assert len(container.conversation.turns) == 1
    text, kw = container.conversation.turns[0]
    assert text == "hello there"
    assert "reached by typed message" in kw["situation"]
    assert any(x == "Done." for x in _texts(events)), "the reply lands as a bubble"


def test_a_follow_up_during_a_busy_turn_queues_and_drains(rig):
    container, events, sh = rig
    with sh._lock:
        sh._busy = True
    sh.submit("second thing")
    assert container.conversation.turns == []
    assert sh._pending and sh._pending[0][0] == "second thing"
    with sh._lock:
        sh._busy = False
    sh._drain_pending()
    sh._finish_wait()
    assert [t for t, _ in container.conversation.turns] == ["second thing"]


def test_stop_drops_queued_follow_ups_first(rig):
    container, events, sh = rig
    with sh._lock:
        sh._busy = True
        sh._pending.append(("queued", False, [], None))
    sh.submit("stop")
    assert sh._pending == [], "a stop means stopped — queued follow-ups are dropped"


# ----- the cleanup-answer classifier (spec: negation can never be yes) -----

@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("yeah remove it", True), ("sure", True),
    ("no", False), ("keep it", False), ("don't", False),
    ("no, don't remove it", False), ("not yet", False),
    ("what was that about?", None), ("tell me more about the weather today please now", None),
])
def test_cleanup_answer_classifier(rig, text, expected):
    _c, _e, sh = rig
    assert sh._cleanup_answer(text) is expected


# ----- scheduled reports: the QUIET sentinel -----

def test_quiet_reports_are_status_only(rig):
    _c, events, sh = rig
    sh._on_scheduled_report("Slack Watcher", "QUIET")
    sh._on_scheduled_report("GitHub Watcher", "  quiet.  ")
    assert _texts(events) == []
    lines = _texts(events, "status")
    assert "Slack Watcher: all quiet." in lines and "GitHub Watcher: all quiet." in lines


def test_refused_reports_never_reach_the_transcript(rig):
    _c, events, sh = rig
    sh._on_scheduled_report("Watcher", "No agent named 'Watcher'.")
    sh._on_scheduled_report("Watcher", "I got stuck — could you rephrase?")
    assert _texts(events) == []
    assert any("no report this run" in (x or "") for x in _texts(events, "status"))


def test_a_real_report_lands_as_a_bubble(rig):
    _c, events, sh = rig
    sh._on_scheduled_report("Morning Brief", "Two PRs merged overnight.")
    assert any("Morning Brief: Two PRs merged overnight." == x for x in _texts(events))


# ----- the coalescing announcer (wording pinned) -----

def test_one_done_build_announces_ready_and_iterating_announces_updated(rig):
    _c, events, sh = rig
    sh._buffer_done(("Tip Calculator", True, None, False))
    sh._flush_done()
    assert any(x == "Tip Calculator is ready — it's in the menu." for x in _texts(events))
    sh._buffer_done(("Tip Calculator", True, None, True))
    sh._flush_done()
    assert any(x == "Updated Tip Calculator." for x in _texts(events))


def test_many_done_builds_coalesce_into_one_line(rig):
    _c, events, sh = rig
    for name in ("A", "B", "C"):
        sh._buffer_done((name, True, None, False))
    sh._flush_done()
    assert any(x == "3 builds are ready: A, B, and C." for x in _texts(events))


def test_a_failed_build_names_the_reason(rig):
    _c, events, sh = rig
    sh._buffer_done(("Widget", False, "the coder gave up", False))
    sh._flush_done()
    assert any(x == "The Widget build hit a snag: the coder gave up" for x in _texts(events))


# ----- the voice truth rules (no voice wired = honest 'off') -----

def test_voice_state_without_a_voice_loop_is_off_and_ready(rig):
    _c, _e, sh = rig
    vs = sh.voice_state()
    assert vs["supported"] is False and vs["enabled"] is False
    assert vs["idle_line"] == "Ready when you are."
    assert vs["wake"] == "HELIX"


# ----- the board -----

def test_board_orders_building_done_error_and_is_self_clearing():
    board = _Board()
    board.mark("c", "Charlie", "error")
    board.mark("a", "alpha", "done")
    board.mark("b", "Bravo", "building")
    assert [r["state"] for r in board.legend()] == ["building", "done", "error"]
    board.mark("", "keyless", "building")  # a keyless build is never recorded
    assert len(board.legend()) == 3
    board.mark_seen("a")   # done + seen clears
    board.mark_seen("b")   # building is left alone
    states = {r["slug"]: r["state"] for r in board.legend()}
    assert "a" not in states and states["b"] == "building"


# ----- helper: let the turn worker thread retire -----

def _finish_wait(self, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with self._lock:
            if not self._busy:
                return
        time.sleep(0.01)


ShellSession._finish_wait = _finish_wait


# ===== dreaming: the nightly self-improvement session (READ_ME/DREAM.md §7) =====
# The engine (services/dream.py) is the other workstream's. The shell's half of the contract is
# pinned here against a fake exposing exactly the DreamService surface the shell touches: the
# activity clock it hands over, the heartbeat it beats, the morning report it delivers once, the
# start/end it pushes to the face, and the Settings card's reads and buttons.

class _Dream:
    def __init__(self, report=None, status=("Dreaming nightly from 23:00 for 8 hours.\n"
                                            "Last night: 3 drafted, 2 applied.")):
        self.activity = None  # the shell registers its callback here
        self.running = False
        self.ticks = 0
        self._report = report
        self.reports_taken = 0
        self.status_text = status
        self.calls: list[tuple] = []

    def tick(self):
        self.ticks += 1

    def status(self):
        return self.status_text

    def morning_report(self):
        self.reports_taken += 1
        text, self._report = self._report, None  # delivering clears it — the engine's contract
        return text

    def pending_report(self):
        return self._report  # the engine's non-consuming peek (DreamService.pending_report)

    def dream_now(self, minutes=30):
        self.calls.append(("dream_now", minutes))
        self.running = True
        return f"Dreaming for {minutes:g} minutes."

    def stop(self, reason="the user asked"):
        self.calls.append(("stop", reason))
        self.running = False
        return "Stopped dreaming."


class _DreamContainer(_Container):
    def __init__(self, dream=None):
        super().__init__()
        self.dream = dream if dream is not None else _Dream()
        self.paths = SimpleNamespace(source_root="C:/repo", builds="x")
        self.growth_model = SimpleNamespace(resolve=lambda: "claude-fable-5")


class _NoDreamContainer(_Container):
    dream = None  # an older container: no engine at all


class _TalkingVoice(_SleepyVoice):
    """Ears that also record what the shell SPEAKS — the report/reply order is the point."""

    def __init__(self):
        super().__init__(ears=True)
        self.spoken: list[str] = []
        self._session = False

    def speak(self, text):
        self.spoken.append(text)

    def begin_turn(self):
        pass

    def is_active(self):
        return False

    def idle(self):
        pass

    def set_working(self, on):
        pass

    def interrupt(self):
        pass


@pytest.fixture
def drig():
    container = _DreamContainer()
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    yield container, events, sh
    sh.shutdown()


def _dream_events(events):
    return [e for e in events if e.get("t") == "dream"]


def test_the_shell_hands_the_engine_a_live_activity_clock(drig):
    container, _e, sh = drig
    dream = container.dream
    assert callable(dream.activity)
    sh._last_activity -= 600           # ten minutes of silence…
    assert dream.activity() >= 599
    sh.submit("hello there")           # …until the user says something
    sh._finish_wait()
    assert dream.activity() < 5
    sh._last_activity -= 600
    sh.tap()                           # a tap is presence too
    assert dream.activity() < 5
    sh._last_activity -= 600
    sh.stop()                          # …and so is a stop
    assert dream.activity() < 5


def test_the_morning_report_is_told_once_as_its_own_bubble_before_the_reply(drig):
    container, events, sh = drig
    report = ("Last night I drafted 6 improvements and applied 4. Two are waiting for your "
              "review. I rebuilt and relaunched at 6:41.")
    container.dream._report = report
    sh.submit("hello there")
    sh._finish_wait()
    msgs = [(e["role"], e["text"]) for e in events if e.get("t") == "msg"]
    assert msgs == [("user", "hello there"), ("helix", report), ("helix", "Done.")]
    # the model is told it was just said — and what it said — so it never repeats it
    _text, kw = container.conversation.turns[0]
    assert "JUST told the user last night's dream report" in kw["situation"]
    assert "drafted 6 improvements" in kw["situation"]
    # …and it is told ONCE: the next turn carries neither the bubble nor the hint
    sh.submit("what's next?")
    sh._finish_wait()
    msgs = [(e["role"], e["text"]) for e in events if e.get("t") == "msg"]
    assert [m for m in msgs if m[1] == report] == [("helix", report)]
    _text2, kw2 = container.conversation.turns[1]
    assert "dream report" not in kw2["situation"]
    assert sh.dream_state()["report"] == report  # the Settings card still shows what was told


def test_the_report_is_spoken_ahead_of_the_reply_as_one_utterance():
    # speak() hushes whatever is playing: a report voiced at turn start would be cut off by the
    # reply seconds later. Its bubble goes up first; the voice says both together.
    container = _DreamContainer()
    container.dream._report = "Last night I drafted two improvements."
    voice = _TalkingVoice()
    sh = ShellSession(container, lambda e: None, voice=voice)
    try:
        sh.submit("hello there")
        sh._finish_wait()
        assert voice.spoken == ["Last night I drafted two improvements. Done."]
        sh.submit("and now?")
        sh._finish_wait()
        assert voice.spoken[-1] == "Done."  # no report the second time
    finally:
        sh.shutdown()


def test_a_report_waits_for_a_turn_that_actually_starts_never_mid_task(drig):
    container, events, sh = drig
    container.dream._report = "Last night I drafted one improvement."
    with sh._lock:
        sh._busy = True                # something is running
    sh.submit("second thing")          # queued behind it
    assert container.dream.reports_taken == 0
    assert not any(e.get("t") == "msg" and e.get("role") == "helix" for e in events)
    with sh._lock:
        sh._busy = False
    sh._drain_pending()                # the queued message starts its turn now — that is the moment
    sh._finish_wait()
    texts = _texts(events)
    assert texts.index("Last night I drafted one improvement.") < texts.index("Done.")


def test_the_heartbeat_beats_the_engine_and_pushes_a_start_and_an_end_once(drig):
    container, events, sh = drig
    sh._heartbeat.cancel()
    sh._tick()
    sh._tick()
    assert container.dream.ticks == 2
    assert _dream_events(events) == []             # nothing changed, nothing said
    container.dream.running = True
    sh._tick()
    sh._tick()
    dreams = _dream_events(events)
    assert len(dreams) == 1 and dreams[0]["running"] is True
    assert dreams[0]["line"] == "Dreaming nightly from 23:00 for 8 hours."  # the first line only
    container.dream.running = False
    sh._tick()
    dreams = _dream_events(events)
    assert len(dreams) == 2 and dreams[1]["running"] is False


def test_the_engines_own_event_flips_the_chip_at_once(drig):
    container, events, sh = drig
    sh._on_dream_state(SimpleNamespace(running=True, line="Dreaming — drafting the camera fix."))
    assert _dream_events(events)[-1] == {"t": "dream", "running": True,
                                         "line": "Dreaming — drafting the camera fix."}
    assert sh._dream_running
    sh._heartbeat.cancel()
    container.dream.running = True
    sh._tick()                                      # the poll then has nothing new to say
    assert len(_dream_events(events)) == 1


def test_dream_state_carries_what_the_card_needs(drig):
    _c, _e, sh = drig
    state = sh.dream_state()
    assert state["available"] is True and state["running"] is False
    assert state["status"].startswith("Dreaming nightly")
    assert state["line"] == "Dreaming nightly from 23:00 for 8 hours."
    assert state["model"] == "claude-fable-5"
    assert state["frozen_without_source"] is False and state["report"] == ""
    assert sh.snapshot()["dream"]["available"] is True  # a reloaded page shows the chip at once


def test_a_frozen_app_with_no_source_repository_says_dreaming_is_frozen(drig, monkeypatch):
    container, _e, sh = drig
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert sh.dream_state()["frozen_without_source"] is False   # a source root is known
    container.paths = SimpleNamespace(source_root=None)
    assert sh.dream_state()["frozen_without_source"] is True
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert sh.dream_state()["frozen_without_source"] is False   # dev never freezes


def test_a_shell_without_an_engine_still_answers_honestly():
    container = _NoDreamContainer()
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    try:
        assert sh.dream_state()["available"] is False
        assert sh.dream_now(30) == {"ok": False, "text": "Dreaming isn't available in this build."}
        assert sh.dream_stop()["ok"] is False
        sh._heartbeat.cancel()
        sh._tick()                                  # no engine, no crash, no event
        assert _dream_events(events) == []
        sh.submit("hello there")
        sh._finish_wait()
        assert "Done." in _texts(events)            # turns run exactly as before
    finally:
        sh.shutdown()


def test_dream_now_and_stop_from_settings_route_to_the_engine_and_flip_the_chip(drig):
    container, events, sh = drig
    assert sh.dream_now("30") == {"ok": True, "text": "Dreaming for 30 minutes."}
    assert container.dream.calls == [("dream_now", 30.0)]
    assert [e["running"] for e in _dream_events(events)] == [True]
    out = sh.dream_stop()
    assert out["ok"] and "Stopped" in out["text"]
    assert container.dream.calls[-1] == ("stop", "the user asked from Settings")
    assert [e["running"] for e in _dream_events(events)] == [True, False]
    assert sh.dream_now("junk")["ok"] and container.dream.calls[-1] == ("dream_now", 30.0)
    assert sh.dream_now(99999)["ok"] and container.dream.calls[-1] == ("dream_now", 720.0)


def test_a_draft_finishing_mid_dream_never_asks_the_sleeping_house_to_apply_it(drig):
    container, events, sh = drig
    sh._on_dream_state(SimpleNamespace(running=True, line="Dreaming."))
    container.bus.publish(SelfChangeFinished(
        ok=True, summary="the camera remembers its device", branch="selfdev/cam", unattended=True))
    text = _texts(events)[-1]
    assert text.startswith("Dreaming — drafted the camera remembers its device")
    assert "apply it" not in text
    container.bus.publish(SelfChangeFinished(ok=False, error="the coder made no changes.",
                                             unattended=True))
    assert _texts(events)[-1] == "Dreaming — one draft didn't land. the coder made no changes."
    # a draft the user asked for keeps its apply/discard prompt, dream or no dream
    container.bus.publish(SelfChangeFinished(ok=True, summary="x", branch="selfdev/x"))
    assert "apply it" in _texts(events)[-1]


def test_an_unattended_ending_with_no_progress_line_is_still_quiet():
    # The lane stamps `unattended` on its ENDING too, and a draft that dies before its first
    # progress line (a dirty tree, a refused lane) arrives as a bare Finished. The shell used to
    # read only the flag it remembered from progress lines — so that ending was spoken at 3 AM.
    container = _DreamContainer()
    voice = _TalkingVoice()
    sh = ShellSession(container, lambda e: None, voice=voice)
    try:
        container.bus.publish(SelfChangeFinished(ok=False, error="the working tree is dirty.",
                                                 unattended=True))
        assert voice.spoken == []
        container.bus.publish(SelfChangeFinished(ok=False, error="the coder gave up."))  # attended
        assert voice.spoken and "Couldn't draft" in voice.spoken[-1]
    finally:
        sh.shutdown()


def test_a_plain_stop_mid_dream_says_what_ends_the_session(drig):
    _c, events, sh = drig
    sh._on_dream_state(SimpleNamespace(running=True, line="Dreaming."))
    sh.stop()
    assert _texts(events, "status")[-1] == (
        "Stopped. I'm still dreaming — say “stop dreaming” to end the session.")


def test_the_card_shows_a_pending_report_before_any_turn_without_consuming_it():
    """GET /api/dream's `report` peeks the engine's pending_report(): the card shows last night the
    moment the app is up (a dawn relaunch included), while morning_report() is still taken only by
    the first user turn — exactly once."""
    report = "Last night I drafted 3 improvements. 3 are waiting for your review."
    container = _DreamContainer(_Dream(report=report))
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    try:
        assert sh.dream_state()["report"] == report
        assert sh.dream_state()["report"] == report          # peeking twice consumes nothing
        assert container.dream.reports_taken == 0
        sh.submit("morning")
        sh._finish_wait()
        assert container.dream.reports_taken == 1
        assert container.dream.pending_report() is None      # delivered: the engine's flag is clear
        assert sh.dream_state()["report"] == report          # and the card still shows what was told
    finally:
        sh.shutdown()


def test_an_engine_without_a_peek_or_with_a_broken_one_leaves_the_report_blank():
    class _OldDream(_Dream):
        pending_report = None  # an older engine: no peek at all

    class _BrokenDream(_Dream):
        def pending_report(self):
            raise OSError("journal unreadable")

    for dream in (_OldDream(report="x"), _BrokenDream(report="x")):
        sh = ShellSession(_DreamContainer(dream), lambda _e: None, voice=None)
        try:
            assert sh.dream_state()["report"] == ""
            assert dream.reports_taken == 0
        finally:
            sh.shutdown()


def test_the_situation_block_knows_a_dream_is_running(drig):
    container, _e, sh = drig
    sh._on_dream_state(SimpleNamespace(running=True, line="Dreaming."))
    sh.submit("hello there")
    sh._finish_wait()
    _text, kw = container.conversation.turns[0]
    assert "a dream session is drafting improvements to you in the background" in kw["situation"]


# ----- the routes, over plain ASGI (as test_api_lifecycle drives them — no TestClient) -----

class _RouteShell:
    def __init__(self):
        self.voice = None
        self.pushed: list[dict] = []
        self.calls: list[tuple] = []
        self.changed_calls = 0

    def snapshot(self):
        return {"t": "snapshot"}

    def push(self, ev):
        self.pushed.append(ev)

    def voice_state(self):
        return {"supported": False}

    def dream_state(self):
        return {"available": True, "running": False, "line": "Dreaming nightly.",
                "status": "Dreaming nightly.", "report": "", "frozen_without_source": False,
                "model": "claude-fable-5"}

    def dream_now(self, minutes):
        self.calls.append(("now", minutes))
        return {"ok": True, "text": f"Dreaming for {float(minutes):g} minutes."}

    def dream_stop(self):
        self.calls.append(("stop",))
        return {"ok": True, "text": "Stopped."}

    def dream_settings_changed(self):
        self.changed_calls += 1


def _route_app():
    from helix.api.server import EventHub, build_app

    settings = _Settings(web_token="tok-test")
    container = SimpleNamespace(
        settings=settings,
        paths=SimpleNamespace(builds="does-not-exist"),  # mounted with check_dir=False
        connections=SimpleNamespace(value=lambda key: ""),
        subscription=SimpleNamespace(active=lambda allow_probe=False: False,
                                     last_failure=lambda: None),
        gmail=SimpleNamespace(configured=lambda: False, address=lambda: ""),
        calendar=SimpleNamespace(configured=lambda: False),
    )
    shell = _RouteShell()
    return build_app(container, shell, EventHub(), None), shell, settings


def _asgi(app, method: str, path: str, body=None, token: str = "tok-test"):
    """One request straight through the ASGI stack → (status, decoded JSON body or None)."""
    payload = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"127.0.0.1:8737"), (b"x-helix-token", token.encode())]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    status: list[int] = []
    chunks: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))
    raw = b"".join(chunks)
    return status[0], (json.loads(raw) if raw else None)


def test_the_dream_routes_are_thin_calls_into_the_shell():
    app, shell, _settings = _route_app()
    status, body = _asgi(app, "GET", "/api/dream")
    assert status == 200 and body["model"] == "claude-fable-5" and body["running"] is False
    status, body = _asgi(app, "POST", "/api/dream/now", {"minutes": 45})
    assert status == 200 and body == {"ok": True, "text": "Dreaming for 45 minutes."}
    status, _body = _asgi(app, "POST", "/api/dream/now")   # a bare POST: the default half hour
    assert status == 200 and shell.calls[-1] == ("now", 30)
    status, body = _asgi(app, "POST", "/api/dream/stop")
    assert status == 200 and body["ok"] and shell.calls[-1] == ("stop",)
    assert _asgi(app, "GET", "/api/dream", token="wrong")[0] == 401  # the token is not optional


def test_the_settings_routes_carry_the_dream_keys_with_their_contract_defaults():
    app, shell, settings = _route_app()
    status, body = _asgi(app, "GET", "/api/settings")
    assert status == 200
    values = body["values"]
    assert values["dream_enabled"] is False and values["dream_start"] == "23:00"
    assert values["dream_hours"] == 8 and values["dream_max_drafts"] == 10
    assert values["dream_auto_apply"] is False and values["dream_rebuild"] is True
    status, _body = _asgi(app, "PUT", "/api/settings", {"values": {
        "dream_enabled": "true", "dream_start": "7:5", "dream_hours": 99, "dream_max_drafts": 0,
        "dream_auto_apply": True, "dream_rebuild": False,
    }})
    assert status == 200
    assert settings.get("dream_enabled") is True
    assert settings.get("dream_start") == "07:05"
    assert settings.get("dream_hours") == 12 and settings.get("dream_max_drafts") == 1
    assert settings.get("dream_auto_apply") is True and settings.get("dream_rebuild") is False
    assert shell.changed_calls == 1                 # the face is told at once
    _asgi(app, "PUT", "/api/settings", {"values": {"wake_word": "HELIX"}})
    assert shell.changed_calls == 1                 # a save touching no dream key leaves it alone
    status, body = _asgi(app, "GET", "/api/settings")
    assert body["values"]["dream_start"] == "07:05" and body["values"]["dream_hours"] == 12


def test_an_unreadable_dream_value_is_refused_not_saved_as_the_default():
    """The engine's schedule() saves nothing for a clock it can't read; the PUT does the same — the
    stored start stays, the key is named under `rejected` with the engine's sentence, and the rest of
    the save still lands (a cleared <input type=time> sends '', which must not reset a custom start)."""
    app, shell, settings = _route_app()
    _asgi(app, "PUT", "/api/settings", {"values": {"dream_start": "22:30", "dream_hours": 6}})
    assert settings.get("dream_start") == "22:30"
    for bad in ("25:00", "", None, "midnight", "23:61"):
        status, body = _asgi(app, "PUT", "/api/settings", {"values": {"dream_start": bad}})
        assert status == 200 and body["ok"] is True
        assert body["rejected"] == {"dream_start": "That start time isn't one I can read — say it like 23:00."}
        assert "dream_start" not in body["changed"]
        assert settings.get("dream_start") == "22:30", bad
    status, body = _asgi(app, "PUT", "/api/settings", {"values": {
        "dream_hours": "lots", "dream_max_drafts": "many", "dream_enabled": True,
    }})
    assert body["changed"] == ["dream_enabled"] and settings.get("dream_enabled") is True
    assert set(body["rejected"]) == {"dream_hours", "dream_max_drafts"}
    assert settings.get("dream_hours") == 6 and settings.get("dream_max_drafts") is None
    assert shell.changed_calls >= 1            # the face is still told about the part that landed
    status, body = _asgi(app, "PUT", "/api/settings", {"values": {"dream_start": "7:5"}})
    assert body["rejected"] == {} and settings.get("dream_start") == "07:05"


@pytest.mark.parametrize("key,raw,why", [
    ("dream_start", "25:00", True), ("dream_start", "", True), ("dream_start", None, True),
    ("dream_start", "23:61", True), ("dream_start", "0:30", False),
    ("dream_hours", "lots", True), ("dream_hours", None, True), ("dream_hours", float("nan"), True),
    ("dream_hours", 99, False), ("dream_max_drafts", "x", True), ("dream_max_drafts", 500, False),
    ("dream_enabled", None, False), ("dream_enabled", "garbage", False),
])
def test_read_dream_setting_says_when_a_value_cannot_be_read(key, raw, why):
    from helix.api.server import _DREAM_UNREADABLE, dream_setting, read_dream_setting

    value, reason = read_dream_setting(key, raw)
    assert bool(reason) is why
    if why:
        assert value is None and reason == _DREAM_UNREADABLE[key]  # the engine's own words
    else:
        assert value == dream_setting(key, raw)  # readable: the GET and the PUT agree


@pytest.mark.parametrize("key,raw,want", [
    ("dream_start", None, "23:00"), ("dream_start", "25:00", "23:00"),
    ("dream_start", "0:30", "00:30"), ("dream_start", "midnight", "23:00"),
    ("dream_hours", None, 8), ("dream_hours", "6.5", 6.5), ("dream_hours", -3, 1),
    ("dream_hours", "lots", 8), ("dream_hours", 12.0, 12),
    ("dream_max_drafts", None, 10), ("dream_max_drafts", 500, 30), ("dream_max_drafts", "4", 4),
    ("dream_enabled", None, False), ("dream_enabled", "on", True), ("dream_enabled", 0, False),
    ("dream_rebuild", None, True), ("dream_rebuild", "false", False),
    ("dream_auto_apply", None, False), ("dream_auto_apply", 1, True),
])
def test_dream_settings_are_held_to_the_contract(key, raw, want):
    from helix.api.server import dream_setting

    assert dream_setting(key, raw) == want


# ===== sleep-talk (services/murmur.py, READ_ME/DREAM_MIND.md §14): the engine's murmurs reach the
# face at once, ride the snapshot, and are whispered only when someone is there to hear.

class _WhisperVoice(_TalkingVoice):
    def __init__(self):
        super().__init__()
        self.murmured: list[str] = []

    def murmur(self, text):
        self.murmured.append(text)


class _KindDream(_Dream):
    def __init__(self, kind="nightly", **kw):
        super().__init__(**kw)
        self.kind = kind

    def session_kind(self):
        return self.kind


def test_a_murmur_reaches_the_face_and_the_snapshot_and_a_session_ending_clears_it():
    from helix.domain.events import DreamMurmur, DreamStateChanged

    container = _DreamContainer(_KindDream("nightly"))
    events: list[dict] = []
    sh = ShellSession(container, events.append, voice=None)
    try:
        container.bus.publish(DreamMurmur(text="pages turning by themselves…", kind="mind"))
        heard = [e for e in events if e.get("t") == "murmur"]
        assert len(heard) == 1 and heard[0]["text"] == "pages turning by themselves…"
        assert heard[0]["kind"] == "mind" and heard[0]["at"]
        assert sh.snapshot()["murmur"]["text"] == "pages turning by themselves…"  # a reload mid-session isn't mute
        container.bus.publish(DreamMurmur(text="", kind="note"))  # nothing to say is nothing pushed
        assert len([e for e in events if e.get("t") == "murmur"]) == 1
        container.bus.publish(DreamStateChanged(running=False, line=""))
        assert sh.snapshot()["murmur"] is None  # the session's last words leave with it
    finally:
        sh.shutdown()


def test_a_murmur_is_whispered_for_a_manual_session_or_a_user_who_just_spoke_never_into_a_sleeping_house():
    from helix.domain.events import DreamMurmur

    # Nightly, the house asleep for ten minutes: the face only.
    container = _DreamContainer(_KindDream("nightly"))
    voice = _WhisperVoice()
    sh = ShellSession(container, lambda e: None, voice=voice)
    try:
        sh._last_activity -= 601
        container.bus.publish(DreamMurmur(text="quiet now…", kind="note"))
        assert voice.murmured == []
        sh._last_activity = time.monotonic()  # …the user spoke a moment ago: whispered
        container.bus.publish(DreamMurmur(text="someone's up… shh…", kind="note"))
        assert voice.murmured == ["someone's up… shh…"]
    finally:
        sh.shutdown()
    # A manual "dream now": the user asked for it, so they hear it even after a long silence.
    container = _DreamContainer(_KindDream("now"))
    voice = _WhisperVoice()
    sh = ShellSession(container, lambda e: None, voice=voice)
    try:
        sh._last_activity -= 601
        container.bus.publish(DreamMurmur(text="let me read…", kind="mind"))
        assert voice.murmured == ["let me read…"]
    finally:
        sh.shutdown()
    # A voice with no murmur() (an older backend) is simply not whispered through — never spoken loud.
    container = _DreamContainer(_KindDream("now"))
    plain = _TalkingVoice()
    sh = ShellSession(container, lambda e: None, voice=plain)
    try:
        container.bus.publish(DreamMurmur(text="mm…", kind="note"))
        assert plain.spoken == []
    finally:
        sh.shutdown()


def test_a_rebuild_is_announced_before_the_lights_go_out_and_quiet_now_says_when():
    """RebuildRequested now follows ANY session that applied changes — the user may be right there.
    The shell says what is happening (a bubble, spoken) and tells the quit hook when nothing is in
    flight."""
    from helix.domain.events import RebuildRequested

    container = _DreamContainer()
    events: list[dict] = []
    voice = _TalkingVoice()
    sh = ShellSession(container, events.append, voice=voice)
    try:
        assert sh.quiet_now() is True
        container.bus.publish(RebuildRequested(reason="applied 2 changes"))
        said = [e["text"] for e in events if e.get("t") == "msg" and e.get("role") == "helix"]
        assert said and said[-1].startswith("I applied 2 changes to myself and they passed the full test suite.")
        assert "back in about six minutes" in said[-1] and voice.spoken[-1] == said[-1]
        container.bus.publish(RebuildRequested(reason="changes applied earlier"))
        said = [e["text"] for e in events if e.get("t") == "msg" and e.get("role") == "helix"]
        assert said[-1].startswith("Changes I applied earlier are waiting in my source.")
        sh._busy = True
        assert sh.quiet_now() is False  # a turn in flight holds the quit
        sh._busy = False
        voice.is_active = lambda: True  # a reply still being spoken holds it too
        assert sh.quiet_now() is False
    finally:
        sh.shutdown()
