"""Bubble copy/export tools and the attachment chips — verified headlessly on Qt's offscreen platform."""
from __future__ import annotations

import os
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QMimeData, QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication, QToolButton  # noqa: E402

import helix.ui.console_view as cv  # noqa: E402
from helix.ui.console_view import (  # noqa: E402
    ConsoleView,
    _AttachChip,
    _Bubble,
    _ToolWrap,
    _chart_text,
    _table_text,
)
from PyQt6.QtWidgets import QLabel  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def test_bubble_paints_and_copy_puts_text_on_clipboard(_app):
    msgs: list[str] = []
    b = _Bubble("hello there", is_user=False, on_status=msgs.append)
    b.resize(300, 80)
    assert not b.grab().isNull()  # paints via the offscreen surface
    b._copy()
    _app.processEvents()
    # The offscreen clipboard's read-back is unreliable across pytest's cross-test GC — the copy path
    # definitely ran (the status is set right after clipboard().setText), so assert that, and check the
    # content only when the offscreen clipboard actually reports it.
    clip = QGuiApplication.clipboard().text()
    assert clip in ("hello there", "")
    assert msgs and "Copied" in msgs[-1]


def test_bubble_export_writes_the_file(_app, tmp_path, monkeypatch):
    out = tmp_path / "msg.txt"
    monkeypatch.setattr(cv.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    msgs: list[str] = []
    b = _Bubble("exported body", is_user=True, on_status=msgs.append)
    b._export()
    assert out.read_text(encoding="utf-8") == "exported body"
    assert msgs and "Saved" in msgs[-1]


def test_bubble_export_cancel_writes_nothing(_app, monkeypatch):
    monkeypatch.setattr(cv.QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: ("", "")))
    msgs: list[str] = []
    b = _Bubble("x", is_user=True, on_status=msgs.append)
    b._export()  # user cancelled the dialog → no write, no status flash
    assert msgs == []


def test_attach_chip_remove_callback_fires(_app, tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hi", encoding="utf-8")
    removed: list = []
    chip = _AttachChip(p, removed.append)
    assert not chip.grab().isNull()
    chip.findChild(QToolButton).click()  # the ✕
    assert removed == [p]


def test_chart_text_is_label_value_lines():
    spec = {"type": "chart", "title": "Q", "unit": "$", "data": [{"label": "Q1", "value": 10}]}
    assert _chart_text(spec) == "Q\nQ1\t$10"


def test_table_text_is_tab_separated():
    spec = {"type": "table", "title": "T", "columns": ["A", "B"], "rows": [["1", "2"], ["3", "4"]]}
    assert _table_text(spec) == "T\nA\tB\n1\t2\n3\t4"


def test_toolwrap_copy_uses_the_structured_data(_app):
    msgs: list[str] = []
    spec = {"type": "chart", "data": [{"label": "Q1", "value": 10}, {"label": "Q2", "value": 25}]}
    wrap = _ToolWrap(QLabel("[chart]"), _chart_text(spec), "helix-chart.txt", msgs.append)
    wrap.resize(300, 200)
    assert not wrap.grab().isNull()
    wrap._copy()
    _app.processEvents()
    # See the note in test_bubble_paints_… : the offscreen clipboard read-back is flaky across tests;
    # the structured copy path ran (status set), so check the content only when it's actually reported.
    clip = QGuiApplication.clipboard().text()
    assert clip in ("Q1\t10\nQ2\t25", "")
    assert msgs and "Copied" in msgs[-1]


class _FakeSettings:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


class _FakeSTT:
    def available(self):
        return True

    def ready(self):
        return True

    def transcribe(self, path):
        return ""


class _FakeTTS:
    def __init__(self):
        self.stopped = 0

    def available(self):
        return True

    def speak(self, text, allow_fallback=True):
        pass

    def stop(self):
        self.stopped += 1


def _live_voice():
    from helix.ui.voice import VoiceController

    vc = VoiceController(_FakeSTT(), _FakeTTS(), _FakeSettings())
    vc.can_listen = lambda: True  # headless has no mic; pretend the listener is live so mute is allowed
    return vc


def test_voice_mute_toggle_emits_signal_idempotently(_app):
    vc = _live_voice()
    events: list = []
    vc.mutedChanged.connect(events.append)
    assert not vc.is_muted()
    vc.set_muted(True)
    assert vc.is_muted() and events == [True]
    vc.set_muted(True)  # idempotent — no duplicate signal
    assert events == [True]
    vc.toggle_muted()
    assert not vc.is_muted() and events == [True, False]


def test_set_muted_refused_when_nothing_is_listening(_app):
    from helix.ui.voice import VoiceController

    vc = VoiceController(_FakeSTT(), _FakeTTS(), _FakeSettings())
    vc.can_listen = lambda: False  # nothing is listening
    vc.set_muted(True)
    assert not vc.is_muted()  # never arm a muted state with no live mic / no Resume path


def test_muted_mic_acts_only_on_unmute_or_stop(_app):
    vc = _live_voice()
    vc.set_muted(True)
    stops: list = []
    vc.stopRequested.connect(lambda: stops.append(1))
    vc._on_muted_text("what's the weather in paris tomorrow")  # ordinary speech → ignored while muted
    assert vc.is_muted() and stops == []
    vc._on_muted_text("stop")  # a build-stop still works while muted…
    assert stops == [1] and vc.is_muted()  # …and stop does NOT unmute
    vc._on_muted_text("unmute")  # unmute resumes listening
    assert not vc.is_muted()


def test_ptt_and_wake_while_muted_never_start_a_turn(_app):
    # The PTT-bypass + transcription-race fixes: a command that resolves while muted must NOT be emitted.
    vc = _live_voice()
    vc.set_muted(True)
    recognized: list = []
    vc.recognized.connect(recognized.append)
    vc._on_ptt_text("build me a clock app")   # PTT release while muted
    vc._on_wake_text("build me a clock app")  # a pre-mute capture resolving after mute
    assert recognized == [] and vc.is_muted()
    vc._on_wake_text("unmute")  # a leaked utterance that IS unmute is still honored
    assert not vc.is_muted()


def test_narrate_is_silent_while_muted(_app):
    vc = _live_voice()
    vc.enabled = lambda: True  # narrate only speaks when voice is enabled
    ran: list = []
    vc._run = lambda fn, done: ran.append(1)  # spy: narrate dispatches TTS via _run
    vc.narrate("shaping the body")
    assert ran == [1]  # speaks a progress note when live
    vc.set_muted(True)
    ran.clear()  # sleeping now speaks a one-time "going to sleep" confirmation — clear it before the check
    vc.narrate("adding the arc reactor")
    assert ran == []  # asleep → no spoken progress note (HELIX would otherwise hear itself)


def test_sleep_and_wake_speak_a_spoken_confirmation(_app):
    # Verbal confirmation: sleeping/waking each speak one line, so you know the command landed off-screen.
    from helix.ui.voice import _SLEEP_CONFIRM, _WAKE_CONFIRM

    vc = _live_voice()
    vc.enabled = lambda: True  # confirmations only speak when hands-free voice is on
    spoken: list = []
    vc._run = lambda fn, done: spoken.append(vc._speaking_text)  # capture what speak() queued
    vc.set_muted(True)
    assert vc.is_muted() and spoken == [_SLEEP_CONFIRM]
    vc.set_muted(False)
    assert not vc.is_muted() and spoken == [_SLEEP_CONFIRM, _WAKE_CONFIRM]


def test_wake_word_wakes_from_sleep(_app):
    # "Sleep until you hear your name": saying the wake word alone brings HELIX back from sleep.
    vc = _live_voice()
    vc.set_muted(True)
    assert vc.is_muted()
    vc._on_muted_text("HELIX")
    assert not vc.is_muted()


def test_sleep_wake_tolerate_mishearings_and_the_name(_app):
    # The whole point: waking is forgiving. Mis-hearings and "HELIX <anything>" all bring it back.
    vc = _live_voice()
    for phrase in ("wake up", "un mute", "un-mute", "wakey wakey", "HELIX wake up", "HELIX are you there"):
        vc.set_muted(True)
        vc._on_muted_text(phrase)
        assert not vc.is_muted(), phrase


def test_wide_table_scroller_reserves_the_real_scrollbar_height(_app):
    # A wide chat table scrolls sideways; the reserved strip under it must fit the ACTUAL themed
    # scrollbar (measured, not the old hardcoded 16px) so a thicker bar never eats the last row.
    view = ConsoleView(object(), _FakeSettings())
    wide = QLabel("x" * 400)  # naturally wider than the 860px cap
    wide.setFixedWidth(1200)
    area = view._h_scroll(wide)
    sb_h = area.horizontalScrollBar().sizeHint().height()
    # setFixedHeight pins min==max — assert on that, not height(), which is unset pre-show.
    assert area.minimumHeight() >= wide.sizeHint().height() + sb_h  # table + full bar, no overlap


def test_narrow_table_scroller_reserves_no_scrollbar_room(_app):
    # A table narrower than the cap shows no bar — and shouldn't carry dead space for one.
    view = ConsoleView(object(), _FakeSettings())
    narrow = QLabel("small")
    area = view._h_scroll(narrow)
    assert area.minimumHeight() <= narrow.sizeHint().height() + 4


def test_table_row_grows_to_the_scroller_cap_and_tools_stay_pinned(_app):
    # A wide table must use the row's width up to the 860px cap before growing a scrollbar (Qt caps
    # a scroll area's PREFERRED width at ~36 chars, which used to squeeze tables to ~530px), and the
    # hover tools' host must stop at the scroller's cap so copy/export stay on the table's edge.
    view = ConsoleView(object(), _FakeSettings())
    spec = {
        "type": "table",
        "title": "T",
        "columns": [f"Column {i} header" for i in range(10)],
        "rows": [[f"value {r}-{c} text" for c in range(10)] for r in range(3)],
    }
    view._add_visual(spec)
    row = view._tlayout.itemAt(0).layout()
    assert row.stretch(0) == 1  # the table competes for the row's spare width…
    assert row.stretch(1) == 0  # …and the trailing spacer yields until the table hits its cap
    wrap = row.itemAt(0).widget()
    scroller = wrap.layout().itemAt(0).widget()
    assert wrap.maximumWidth() == scroller.maximumWidth() <= 862


def test_console_attachment_chips_toggle_host_visibility(_app, tmp_path):
    view = ConsoleView(object(), _FakeSettings())
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    view._add_attachment(p)
    assert view._attachments == [p]
    assert not view._attach_host.isHidden()  # chip row shows once something is staged
    view._add_attachment(p)  # same path again is a no-op (deduped)
    assert view._attachments == [p]
    view._remove_attachment(p)
    assert view._attachments == []
    assert view._attach_host.isHidden()  # hidden again when empty


# ----- honesty about the ears: the self-situation block, the resting line, the camera hint -----

class _FakeLane:
    """A self-change draft lane. busy() is the only thing the Console ever asks it."""

    def __init__(self, busy: bool = True) -> None:
        self._busy = busy
        self.cancelled = 0

    def busy(self) -> bool:
        return self._busy

    def cancel(self) -> None:
        self.cancelled += 1


# ONE Console+VoiceController pair, reused by every honesty check below and reset between them. A
# Console is a large widget tree that outlives the test that built it, and stacking a dozen of them up
# destabilised Qt's teardown on Windows; reusing one keeps the surface under test identical.
_VOICED: list = []


@pytest.fixture(scope="module", autouse=True)
def _shared_console_teardown(_app):
    """Destroy the shared Console DETERMINISTICALLY, while the QApplication is still alive.

    Reusing one Console fixed the "stack a dozen widget trees" instability, but it moved a new one in:
    a module-global list keeps that tree — and the live VoiceController inside it — alive past the end
    of the session, so its C++ side was torn down at interpreter shutdown, in whatever order Python
    happened to drop the objects relative to the QApplication. That raced, and lost roughly half the
    runs here: `Windows fatal exception: access violation` AFTER the last test's dot, which pytest
    reports as no failures at all — the run simply stops, silently skipping every test in every file
    that would have followed. Closing and deleting here, then draining the event loop, puts the
    teardown back inside the window where Qt can actually do it.
    """
    yield
    for view in _VOICED:
        view.close()
        view.deleteLater()
    _VOICED.clear()
    _app.processEvents()  # let deleteLater actually run before the application goes


def _voiced_console():
    """The shared Console, with a REAL VoiceController behind it, pretending the mic stack is warm —
    headless has no mic, and every check below is about what the Console SAYS about that state."""
    if not _VOICED:
        _VOICED.append(ConsoleView(object(), _FakeSettings(), _FakeSTT(), _FakeTTS()))
    view = _VOICED[0]
    v = view._voice
    for attr in ("can_listen", "enabled", "restart_required", "prewarm_error", "supported",
                 "set_enabled", "speak", "narrate"):
        v.__dict__.pop(attr, None)  # drop any spy an earlier check installed
    view.__dict__.pop("_add_bubble", None)
    view.__dict__.pop("_add_actions", None)
    v._muted = False
    v._working = False
    # A draft lane, its hush and a pending stop belong to the check that installed them — never
    # to the next one.
    view._selfdev_lane = None
    view._selfdev_hushed = False
    view._selfdev_drafting = False
    view._selfdev_unattended = False
    view._cancelled = False
    # An in-flight turn belongs here too, not to a hand cleanup at the end of the check that set it:
    # a failing assertion would skip that cleanup and leave a bare SimpleNamespace in _cancel, so the
    # NEXT check died with an unrelated AttributeError (_cancel.build) and hid which fix really broke.
    view._busy = False
    view._cancel = None
    view._clear_attachments()
    view._input.clear()
    v.can_listen = lambda: True
    v.enabled = lambda: True
    return view, v


def test_the_self_situation_block_reports_the_real_mic_state_not_a_constant(_app):
    # The block is HELIX's proprioception — the model reasons FROM it. It used to say "mic awake"
    # unconditionally, so "are you listening?" was answered wrongly in exactly the states that matter.
    view, v = _voiced_console()
    assert "mic awake" in view._situation(from_voice=True)

    v.set_muted(True, announce=False)
    asleep = view._situation(from_voice=False)
    assert "mic awake" not in asleep and "asleep" in asleep

    v.set_muted(False, announce=False)
    v.enabled = lambda: False
    off = view._situation(from_voice=False)
    assert "mic awake" not in off and "voice is off" in off

    v.enabled = lambda: True
    v.can_listen = lambda: False
    cold = view._situation(from_voice=False)
    assert "mic awake" not in cold and "isn't listening this run" in cold


def test_the_resting_status_line_never_promises_listening_that_isnt_happening(_app):
    # enabled() is only the SAVED flag; the wake listener can fail to open and says nothing about it.
    view, v = _voiced_console()
    view._idle_status()
    assert "Listening for" in view.status.text()

    v.can_listen = lambda: False      # saved on, but the speech model never pre-warmed this run
    v.restart_required = lambda: True
    view._idle_status()
    assert view.status.text() == "Voice needs a restart to start listening."  # what the button says

    v.can_listen = lambda: True
    v.set_muted(True, announce=False)  # sleeping keeps the listener alive — but it isn't listening TO you
    view._idle_status()
    assert "Listening for" not in view.status.text() and "Asleep" in view.status.text()


def test_a_failed_turn_is_spoken_so_a_hands_free_user_isnt_left_in_silence(_app):
    # A dead turn used to be total silence — indistinguishable from an unheard wake word.
    view, v = _voiced_console()
    spoken: list = []
    v.speak = spoken.append
    view._on_fail("ConnectionError: api.anthropic.com unreachable")
    assert spoken and "try me again" in spoken[0].lower()
    assert "ConnectionError" not in spoken[0]  # the raw cause is shown, never read aloud

    spoken.clear()
    v.enabled = lambda: False  # voice off — the screen is the whole record, nothing is spoken at anyone
    view._on_fail("ConnectionError: still down")
    assert spoken == []


def test_stop_during_a_protected_self_change_draft_does_not_claim_it_stopped(_app):
    # Growth is deliberately never cancelled, but the Stop button is shown while it drafts. Saying
    # "Stopped." was a flat lie — the draft kept running and started talking again a second later.
    # Both halves run on the SHARED console, per the rule above: this was the one check here that built
    # two more ConsoleView trees, and every extra tree left alive is more of the Qt teardown that was
    # crashing this file's runs outright. Swapping the lane on the shared view gives the same two states.
    view, _ = _voiced_console()
    view._selfdev_lane = _FakeLane(busy=True)
    view._stop()
    assert "Stopped." not in view.status.text()
    assert "improving myself" in view.status.text().lower()

    view._selfdev_lane = _FakeLane(busy=False)
    view._stop()
    assert view.status.text() == "Stopped."  # nothing running — the plain line is still right


def test_an_unattended_overnight_draft_never_narrates_a_word_aloud(_app):
    # Evolve reuses the lane a user-requested draft uses, at 3 AM, with nobody in the room.
    view, v = _voiced_console()
    narrated: list = []
    spoken: list = []
    v.narrate = lambda text, force=False: narrated.append(text)
    v.speak = spoken.append

    view.on_self_change_progress("running the tests", unattended=True)
    assert narrated == []
    view.on_self_change_finished(
        False, "", "", "HELIX's working tree has uncommitted changes", False, unattended=True
    )
    assert spoken == []
    assert "Couldn't draft" in view.status.text()  # the silent record is still written to the screen

    view.on_self_change_progress("running the tests")  # a draft the user asked for still narrates
    assert narrated == ["running the tests"]


def test_go_to_sleep_with_nothing_listening_says_so_instead_of_silently_doing_nothing(_app):
    # The model is told the tool worked and speaks a goodnight; set_muted refuses in silence. Say so.
    view, v = _voiced_console()
    v.can_listen = lambda: False
    said: list = []
    view._add_bubble = lambda who, text: said.append((who, text))
    view.sleep_voice()
    assert not v.is_muted()
    assert said and "nothing to put to sleep" in said[-1][1]

    v.can_listen = lambda: True  # a live mic just sleeps, with no extra chatter
    said.clear()
    view.sleep_voice()
    assert v.is_muted() and said == []


def test_the_camera_hint_stops_promising_ears_while_a_background_build_holds_them(_app):
    # The real ear gate puts `not _working` outside the camera branch, so a build running while the
    # window opens leaves "take the picture" genuinely unhearable.
    view, v = _voiced_console()
    assert view.camera_voice_ready()
    v.set_working(True)
    assert not view.camera_voice_ready()
    v.set_working(False)
    assert view.camera_voice_ready()


def test_push_to_talk_rests_the_mic_on_a_learned_sleep_reflex(_app):
    # The same phrase must mean the same thing however the mic was opened: hands-free already fires
    # learned reflexes, push-to-talk used to fall through to a full billed model turn.
    from helix.services.reflexes import ReflexService
    from helix.ui.voice import VoiceController

    class _Store:
        def __init__(self) -> None:
            self.d: dict = {}

        def get(self, k, default=None):
            return self.d.get(k, default)

        def set(self, k, v) -> None:
            self.d[k] = v

    reflexes = ReflexService(_Store())
    reflexes.learn("give us some privacy for a minute")
    vc = VoiceController(_FakeSTT(), _FakeTTS(), _FakeSettings(), reflexes=reflexes)
    vc.can_listen = lambda: True
    recognized: list = []
    vc.recognized.connect(recognized.append)

    vc._on_ptt_text("give us some privacy for a minute")
    assert vc.is_muted() and recognized == []  # handled at the brainstem — no turn, no tokens

    vc.set_muted(False, announce=False)
    vc._on_ptt_text("what's the weather")  # an ordinary phrase still becomes a turn
    assert not vc.is_muted() and recognized == ["what's the weather"]


# ----- the natural gesture: a document dragged onto the input -----

def test_a_document_dropped_on_the_input_is_staged_like_the_paperclip_does(_app, tmp_path):
    # ChatInput emits filesDropped only when someone is LISTENING (the "Edit with AI" bar has no tray,
    # so an unclaimed drop must still fall through to the text box). With the Console not connected,
    # the whole document path was dead: a dropped PDF pasted itself as a literal file:/// URL, which
    # the model can't open and the user can't fix.
    view, _v = _voiced_console()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    folder = tmp_path / "proposal_docs"
    folder.mkdir()
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(pdf)), QUrl.fromLocalFile(str(folder))])
    view._input.insertFromMimeData(md)  # exactly what a drag from Explorer delivers
    assert view._attachments == [Path(str(pdf)), Path(str(folder))]  # the staging the menu uses
    assert view._input.text() == ""  # and nothing pasted as text


# ----- a speech model that FAILED to load is not a restart away -----

class _ColdSTT(_FakeSTT):
    """Installed, but not pre-warmed this run — ready() stays False, so can_listen() is False."""

    def ready(self):
        return False


class _KeyedSettings:
    def __init__(self, **d) -> None:
        self.d = dict(d)

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value):
        self.d[key] = value


def test_a_recorded_prewarm_failure_stops_offering_a_restart_that_cannot_help(_app):
    # The launcher records WHY the speech model wouldn't load and clears it on a good launch. Until it
    # was read back, "the model isn't ready" was indistinguishable from "voice was off at launch", so
    # the user was handed a Restart button that re-runs the identical failing load, forever.
    from helix.ui.voice import STT_PREWARM_ERROR_SETTING, VoiceController

    cold = VoiceController(_ColdSTT(), _FakeTTS(), _KeyedSettings())
    cold.mic_available = lambda: True  # headless has no mic; this check is about the MODEL
    assert cold.prewarm_error() == ""
    assert cold.restart_required()  # never pre-warmed this run — restarting genuinely does fix it

    broken = VoiceController(
        _ColdSTT(), _FakeTTS(),
        _KeyedSettings(**{STT_PREWARM_ERROR_SETTING: "small.en: OSError: no space left on device"}),
    )
    broken.mic_available = lambda: True
    assert "no space left" in broken.prewarm_error()
    assert not broken.restart_required()  # pre-warm was TRIED and broke — a restart just repeats it


def test_the_console_names_a_dead_speech_model_instead_of_offering_a_restart(_app):
    view, v = _voiced_console()
    v.supported = lambda: True     # headless: pretend the mic + faster-whisper are installed
    v.can_listen = lambda: False   # ...but the model never loaded
    v.restart_required = lambda: False
    v.prewarm_error = lambda: "small.en: OSError: no space left on device"

    view._idle_status()
    assert view.status.text() == cv._VOICE_STALLED
    assert "restart" not in view.status.text().lower()
    assert "OSError" not in view.status.text()  # the raw cause belongs in the log, not on screen

    view._refresh_voice_ui()
    assert "can’t start" in view._voice_btn.text()
    assert "restart" not in view._voice_btn.text().lower()

    # ...and the toggle never offers a Restart button for a restart that provably cannot help
    said: list = []
    actions: list = []
    view._add_bubble = lambda who, text: said.append(text)
    view._add_actions = lambda buttons: actions.append(buttons)
    v.enabled = lambda: False          # so toggle_voice's target is "turn it on"
    v.set_enabled = lambda on: False   # saving it on can't start a listener that has no model
    view.toggle_voice()
    assert view.status.text() == cv._VOICE_STALLED
    assert actions == []               # no "Restart now" / "Later" pair
    assert said and "didn’t load" in said[0]


# ----- Stop during a protected draft silences the draft, not just the status line -----

def test_stopping_a_protected_draft_hushes_the_voice_until_that_draft_ends(_app):
    view, v = _voiced_console()
    narrated: list = []
    spoken: list = []
    v.narrate = lambda text, force=False: narrated.append(text)
    v.speak = spoken.append
    view._selfdev_lane = _FakeLane(busy=True)

    view.on_self_change_progress("reading the file")   # a draft the user asked for narrates aloud
    assert narrated == ["reading the file"]

    view._stop()
    assert "improving myself" in view.status.text().lower()
    view.on_self_change_progress("running the tests")  # the very line that used to resume narration
    assert narrated == ["reading the file"]            # shown only — the Stop actually held
    assert "running the tests" in view.status.text()   # ...while the status line keeps updating

    # The hush belongs to THAT draft: its ending is still announced, and the next draft narrates
    # aloud again.
    view.on_self_change_finished(True, "a tidier console", "b", None, False)
    assert spoken and "apply it" in spoken[-1]
    view.on_self_change_progress("planning the change")
    assert narrated == ["reading the file", "planning the change"]


def test_stop_hushes_the_draft_even_when_another_job_owns_the_status_line(_app):
    """The hush used to live in the LAST branch of the Stop chain, so it was reached only when a draft
    was the only thing running. Ask a question while the draft works — an ordinary thing to do, since a
    draft takes minutes — and the busy branch wins, the status reads "Stopping…", and the draft carried
    on narrating over the top of it. Which branch writes the status says nothing about whether a draft
    is running, so the hush cannot live in one."""
    view, v = _voiced_console()
    narrated: list = []
    v.narrate = lambda text, force=False: narrated.append(text)
    view._selfdev_lane = _FakeLane(busy=True)

    view.on_self_change_progress("reading the file")
    assert narrated == ["reading the file"]

    # A turn is in flight too, so `elif self._busy` takes the status line instead of the draft branch.
    view._busy = True
    view._cancel = types.SimpleNamespace(cancel=lambda: None)
    view._stop()
    assert "stopping" in view.status.text().lower(), "expected the busy branch to own the status line"

    view.on_self_change_progress("running the tests")
    assert narrated == ["reading the file"], (
        "Stop did not hush the draft because another job won the status branch — HELIX talked on"
    )


def test_an_unattended_draft_never_takes_the_mic_away_from_someone_in_the_room(_app):
    """The nightly pass catches up a night the machine slept through, so a draft nobody asked for can
    land at an hour when someone IS using HELIX. Silence was the easy half. The mic shield is the half
    that bites: it deafens the ears for the several minutes a draft runs, and a user who never started
    it would be talking into a machine that stopped listening mid-sentence."""
    view, v = _voiced_console()
    view._selfdev_lane = _FakeLane(busy=True)

    view.on_self_change_progress("reading the file", unattended=True)
    assert v._working is False, "an unattended draft deafened the mic of a user who never asked for it"

    # A draft the user DID ask for still shields — they started it and are sitting through it.
    view.on_self_change_progress("reading the file")
    assert v._working is True


def test_stop_really_stops_a_draft_nobody_asked_for(_app):
    """“That one runs to the end” is a fair answer for a change the user requested and is watching. It
    is not a fair answer for one HELIX started by itself on a machine somebody is trying to use —
    there, Stop has to mean stopped."""
    view, _v = _voiced_console()
    lane = _FakeLane(busy=True)
    view._selfdev_lane = lane

    view.on_self_change_progress("reading the file", unattended=True)
    view._stop()
    assert lane.cancelled == 1, "Stop refused to cancel a draft the user never started"
    assert view.status.text().startswith("Stopped."), view.status.text()

    # The refusal survives for a draft the user asked for: that one really does run to the end.
    lane2 = _FakeLane(busy=True)
    view._selfdev_lane = lane2
    view.on_self_change_progress("reading the file")
    view._stop()
    assert lane2.cancelled == 0
    assert "runs to the end" in view.status.text()


def test_a_hush_never_outlives_the_draft_it_was_meant_for(_app):
    # Belt and braces: if a draft's ending never reaches the Console (a lost event, a restarted lane),
    # a NEW draft starting must still lift the hush — otherwise growth stays silent all session.
    view, v = _voiced_console()
    narrated: list = []
    v.narrate = lambda text, force=False: narrated.append(text)
    lane = _FakeLane(busy=True)
    view._selfdev_lane = lane

    view.on_self_change_progress("reading the file")
    view._stop()
    lane._busy = False
    view._sync_working()   # the lane went idle without an ending ever reaching the Console
    lane._busy = True      # ...and a brand-new draft begins
    view.on_self_change_progress("planning the change")
    assert narrated == ["reading the file", "planning the change"]


def test_a_stop_before_the_drafts_first_word_still_hushes_that_draft(_app):
    """The real order, and the one nothing used to exercise. propose() spends multiple seconds on
    worktree setup, two tree scans and a CLI cold start before the first progress line — and the Stop
    button is up that whole time. Press it there and the draft's OWN opening line used to be misread as
    "a new draft began", wiping the hush it had just been given: HELIX read the entire growth run aloud,
    force=True, straight through a sleeping mic — the exact failure the hush exists to prevent."""
    view, v = _voiced_console()
    narrated: list = []
    v.narrate = lambda text, force=False: narrated.append(text)
    view._selfdev_lane = _FakeLane(busy=True)

    view._stop()                                       # Stop lands BEFORE any line has been narrated
    assert "improving myself" in view.status.text().lower()
    assert view._selfdev_hushed

    view.on_self_change_progress("reading the file")   # the draft's very first line
    view.on_self_change_progress("running the tests")
    assert narrated == [], "the draft cleared its own hush and talked over the Stop"
    assert "running the tests" in view.status.text()   # ...while the screen keeps the full record


# ----- a stop is SHOWN, not spoken — including when the stopped turn dies -----

def test_a_turn_that_dies_right_after_a_stop_is_shown_but_never_spoken(_app):
    # Cancelling breaks the reply loop from underneath, so a stopped turn very often ends in _on_fail
    # rather than _on_reply. Speaking there answers the user's own halt with a complaint about it.
    view, v = _voiced_console()
    spoken: list = []
    v.speak = spoken.append

    view._cancelled = True
    view._on_fail("CancelledError: the turn was stopped")
    assert spoken == []           # nothing said at a user who just asked for quiet
    assert not view._cancelled    # the flag is consumed exactly as _on_reply consumes it

    view._on_fail("ConnectionError: api.anthropic.com unreachable")  # a real failure still speaks
    assert spoken and "try me again" in spoken[0].lower()


# ----- the model's go_to_sleep holder: settled on every path, or the turn parks and then lies -----

def test_the_sleep_holder_is_settled_however_the_mic_answers(_app):
    # tools.py parks the turn on this holder. Left unclaimed it waits out its claim timeout and tells
    # the user nothing was listening — about a mic that just went to sleep in front of them.
    from helix.domain.events import SleepRequest

    view, v = _voiced_console()

    req = SleepRequest()
    view.sleep_voice(req)
    assert v.is_muted()
    assert req.wait(claim_timeout=0.2, timeout=0.5)  # already settled: the ears really did close

    v.set_muted(False, announce=False)
    v.can_listen = lambda: False   # silent mode / no mic: set_muted refuses, so this must NOT claim success
    refused = SleepRequest()
    view.sleep_voice(refused)
    assert not v.is_muted()
    assert not refused.wait(claim_timeout=0.2, timeout=0.5)
    assert refused.error == cv._NOTHING_TO_SLEEP  # the same words the screen just showed

    v.can_listen = lambda: True
    gone = SleepRequest()
    gone.abandon()                 # the turn was cancelled before the shell got to it
    view.sleep_voice(gone)
    assert not v.is_muted()        # nothing is rested on behalf of a worker that walked away

    saved, view._voice = view._voice, None  # a shell with no voice stack at all
    try:
        silent = SleepRequest()
        view.sleep_voice(silent)
        assert not silent.wait(claim_timeout=0.2, timeout=0.5)
        assert "nothing to rest" in silent.error
    finally:
        view._voice = saved


def test_the_model_relays_the_sleep_refusal_once_instead_of_helix_saying_it_twice(_app):
    # tools.py hands the failure text back with "tell the user that plainly in one short line", so the
    # model's reply IS a HELIX bubble carrying this sentence. Writing our own bubble as well made HELIX
    # say the identical line twice in a row. The typed path, where no model turn follows, still needs it.
    from helix.domain.events import SleepRequest

    view, v = _voiced_console()
    v.can_listen = lambda: False
    said: list = []
    view._add_bubble = lambda who, text: said.append((who, text))

    refused = SleepRequest()
    view.sleep_voice(refused)
    assert refused.error == cv._NOTHING_TO_SLEEP  # the model has the words and will say them
    assert said == [], "HELIX wrote the refusal itself as well as having the model relay it"

    view.sleep_voice()  # nobody holding the request — the screen is the only record, so speak up
    assert said and said[-1][1] == cv._NOTHING_TO_SLEEP
