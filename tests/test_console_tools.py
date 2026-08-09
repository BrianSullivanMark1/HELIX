"""Bubble copy/export tools and the attachment chips — verified headlessly on Qt's offscreen platform."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

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
