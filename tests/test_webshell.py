"""ShellSession tests — the web console's brain, driven through a fake container.

These pin the ported Qt-console contracts at the SERVER: the submit gauntlet's order, the stop
contract, the QUIET sentinel, the coalescing announcer's wording, the cleanup-answer classifier,
the voice-button truth rules, and the board's legend ordering. The event stream is captured as a
plain list; no FastAPI, no sockets, no threads beyond the ones the shell itself spawns.
"""
from __future__ import annotations

import time

import pytest

from helix.adapters.signal_bus import SignalBus
from helix.api import shell as shell_mod
from helix.api.shell import ShellSession, _Board


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


# ----- the submit gauntlet -----

def test_a_typed_sleep_with_no_ears_answers_honestly_and_never_runs_a_turn(rig):
    container, events, sh = rig
    sh.submit("go to sleep")
    sh._finish_wait()
    assert any("nothing to put it to sleep" in (x or "") or "nothing to put to sleep" in (x or "")
               for x in _texts(events)), _texts(events)
    assert container.conversation.turns == []


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
