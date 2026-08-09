"""Console history: the launch view is ORB-ONLY (nothing replayed), while _load_history stays able
to render the stored conversation — including stripping inline viz JSON — when explicitly invoked.

Also pins two things the heartbeat/Stop paths got wrong: the 15s suggestion check must not do its
git+manifest scan on the GUI thread (and must genuinely rate-limit when there is nothing to suggest),
and a 'stop' must drop queued follow-ups so HELIX doesn't keep answering after the halt."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.domain.models import Message, Role  # noqa: E402
from helix.services.suggestions import Suggestion  # noqa: E402
from helix.ui.console_view import (  # noqa: E402
    ConsoleView,
    _Bubble,
    _table_html,
    _table_image,
    _table_slack,
)


class _Conv:
    def __init__(self, msgs):
        self._msgs = msgs
        self.asked = None

    def recent_messages(self, limit):
        self.asked = limit
        return list(self._msgs)


class _Settings:
    def get(self, k, default=None):
        return default

    def set(self, k, v):
        pass


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def _bubbles(cv):
    out = []
    for i in range(cv._tlayout.count()):
        lay = cv._tlayout.itemAt(i).layout()
        if lay is None:
            continue
        for j in range(lay.count()):
            w = lay.itemAt(j).widget()
            if isinstance(w, _Bubble):
                out.append((("you" if w._is_user else "helix"), w._text))
    return out


def test_console_launch_is_orb_only_nothing_replayed(_app):
    # Orb-only default: even with stored history, construction renders NO bubbles — the first thing
    # on screen is the orb alone. The history itself stays in the store (and the model's context).
    at = datetime(2026, 6, 29, 12, 0, 0)
    conv = _Conv([
        Message(Role.USER, "what's the weather?", at),
        Message(Role.ASSISTANT, "Clear and 72.", at),
    ])
    cv = ConsoleView(conv, _Settings())
    assert _bubbles(cv) == []


def test_load_history_renders_recent_history_when_invoked(_app):
    at = datetime(2026, 6, 29, 12, 0, 0)
    msgs = [
        Message(Role.USER, "what's the weather?", at),
        Message(Role.ASSISTANT, "Clear and 72.", at),
        Message(Role.USER, "thanks", at),
        Message(Role.ASSISTANT, "Anytime.", at),
    ]
    conv = _Conv(msgs)
    cv = ConsoleView(conv, _Settings())
    cv._load_history()
    assert conv.asked == 50  # asked for the last 50
    rendered = _bubbles(cv)
    # all four, in order, on the correct sides
    assert rendered == [
        ("you", "what's the weather?"),
        ("helix", "Clear and 72."),
        ("you", "thanks"),
        ("helix", "Anytime."),
    ]


def test_console_history_strips_inline_viz_json(_app):
    # An assistant reply that carried a chart block should show only its prose in the bubble, not raw JSON.
    reply = 'Here are the numbers.\n```viz {"type":"table","columns":["A"],"rows":[["1"]]}```'
    cv = ConsoleView(_Conv([Message(Role.ASSISTANT, reply, datetime(2026, 6, 29, 12, 0, 0))]), _Settings())
    cv._load_history()
    texts = [t for _, t in _bubbles(cv)]
    assert texts == ["Here are the numbers."]
    assert all("viz" not in t and "{" not in t for t in texts)


def test_console_empty_history_is_fine(_app):
    cv = ConsoleView(_Conv([]), _Settings())
    assert _bubbles(cv) == []


def test_table_slack_copy_is_a_single_fenced_aligned_table():
    spec = {
        "type": "table", "title": "Open Actions",
        "columns": ["Item", "Owner", "Status"],
        "rows": [["Fix login", "Bren", "In Progress"], ["Deploy 2.6.8", "Brian", "Blocked"]],
    }
    out = _table_slack(spec)
    assert out.startswith("*Open Actions*\n\n")   # Slack-bold title above the fence, then a blank line
    lines = out.splitlines()
    # exactly ONE fenced code block wraps the whole table — Slack renders it as one aligned monospace grid
    assert out.count("```") == 2
    assert lines[2] == "```" and lines[-1] == "```"
    body = lines[3:-1]  # header, separator, two data rows
    assert len(body) == 4
    assert body[0].startswith("Item")            # header row, not wrapped in inline backticks anymore
    assert set(body[1]) <= set("-+ ")            # a dashes separator row under the header
    # the non-final columns are padded to a constant width so columns line up down the block
    prefixes = [ln.split(" | ")[0] for ln in body if " | " in ln]
    assert len({len(p) for p in prefixes}) == 1
    assert "Fix login" in out and "Blocked" in out


def test_table_slack_copy_neutralizes_a_triple_backtick_in_a_cell():
    # A stray ``` inside a cell must not close the fence early and spill the rest as plain text.
    out = _table_slack({"type": "table", "columns": ["Cmd"], "rows": [["run ```build``` now"]]})
    assert out.count("```") == 2                  # only the opening + closing fence survive
    assert "'''build'''" in out                   # the cell's fence was defanged, content preserved


def test_table_html_is_a_real_table_for_rich_paste():
    spec = {
        "type": "table", "title": "Open Actions",
        "columns": ["Item", "Owner"],
        "rows": [["Fix login", "Bren"], ["Deploy", "Brian"]],
    }
    html = _table_html(spec)
    assert "<table" in html and "</table>" in html
    assert html.count("<th ") == 2      # two header cells (space avoids matching <thead>)
    assert html.count("<tr>") == 3      # one header row + one per data row
    assert html.count("<td ") == 4      # 2 columns x 2 data rows
    assert "<b>Open Actions</b>" in html  # title as bold above the table
    assert "Fix login" in html and "Brian" in html


def test_table_html_escapes_cell_markup():
    # Cell content must never inject live HTML into the pasted table.
    html = _table_html({"type": "table", "columns": ["X"], "rows": [["<script>alert(1)</script>"]]})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_table_image_renders_a_bordered_picture():
    # The 'copy as image' path (a bordered table for pasting into Slack, which has no table support).
    img = _table_image({
        "type": "table", "title": "Hours",
        "columns": ["Employee", "Project", "Hours"],
        "rows": [["Brendan", "BRMS", "8"], ["Brian", "BRMS", "6"]],
    })
    assert img is not None and not img.isNull()
    assert img.width() > 0 and img.height() > 0


def test_table_image_wide_table_is_capped_and_wraps():
    # A very wide free-text column must not produce an unbounded strip — the width is capped so cells wrap.
    wide = "x " * 400  # a ~800-char activity cell
    img = _table_image({
        "type": "table", "columns": ["Who", "Activity"], "rows": [["Bren", wide]],
    })
    assert img is not None and not img.isNull()
    from helix.ui.console_view import _IMG_MAX_W, _IMG_SCALE
    assert img.width() <= _IMG_MAX_W * _IMG_SCALE  # capped, so it wrapped instead of a huge strip


# ----- the heartbeat's suggestion check: off the GUI thread, and actually rate-limited -----


class _Suggest:
    """Stands in for SuggestionService at the seam. Records the thread each scan ran on, and can be made
    to block so an overlapping heartbeat is observable."""

    def __init__(self, result=None) -> None:
        self._result = result
        self.calls = 0
        self.threads: list[int] = []
        self.entered = threading.Event()
        self.gate: threading.Event | None = None

    def candidate(self):
        self.calls += 1
        self.threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        return self._result


def _settle(cv, app, timeout: float = 5.0) -> None:
    """Let the in-flight scan finish and its queued signals land on the GUI thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker = cv._suggest_scan
        if worker is None:
            break
        worker.wait(50)
        app.processEvents()
    app.processEvents()


def test_suggestion_scan_runs_off_the_gui_thread(_app):
    # The scan shells out to git and parses every build manifest; on the GUI thread every 15s that is
    # exactly the stutter this fix removes. Only the chip update belongs on the UI thread.
    svc = _Suggest(Suggestion(id="neglected:notes", text="You haven’t opened “Notes” in a while."))
    cv = ConsoleView(_Conv([]), _Settings(), suggestions=svc)
    cv.maybe_suggest()
    _settle(cv, _app)
    assert svc.calls == 1
    assert svc.threads == [svc.threads[0]] and svc.threads[0] != threading.get_ident()
    assert cv._suggest_current == "neglected:notes"  # the result still reached the widget
    assert not cv._suggest_host.isHidden()  # shown (the view itself is never shown in an offscreen test)


def test_nothing_to_suggest_still_charges_the_rate_limiter(_app):
    # The regression: the 25-minute limiter only advanced when a chip was SHOWN, so the normal case
    # ("nothing to suggest") re-ran the whole scan on every 15s heartbeat, forever.
    svc = _Suggest(None)
    cv = ConsoleView(_Conv([]), _Settings(), suggestions=svc)
    for _ in range(5):
        cv.maybe_suggest()
        _settle(cv, _app)          # so it's the limiter, not the in-flight guard, doing the work
    assert svc.calls == 1
    assert cv._suggest_ts > 0.0    # the window opened on the ATTEMPT
    assert cv._suggest_host.isHidden()


def test_overlapping_heartbeat_does_not_stack_scans(_app):
    # A slow scan plus a 15s heartbeat must not pile up worker threads on the same work.
    svc = _Suggest(None)
    svc.gate = threading.Event()
    cv = ConsoleView(_Conv([]), _Settings(), suggestions=svc)
    cv.maybe_suggest()
    assert svc.entered.wait(5)  # the scan is genuinely mid-flight
    cv._suggest_ts = 0.0   # defeat the limiter so ONLY the in-flight guard can stop the second call
    cv.maybe_suggest()
    assert svc.calls == 1
    svc.gate.set()
    _settle(cv, _app)
    assert svc.calls == 1
    assert cv._suggest_scan is None  # retired cleanly, and not left in _workers to block shutdown
    assert not cv._workers


def test_a_scan_that_raises_is_swallowed_and_retired(_app):
    class _Boom:
        def candidate(self):
            raise RuntimeError("git exploded")

    cv = ConsoleView(_Conv([]), _Settings(), suggestions=_Boom())
    cv.maybe_suggest()
    _settle(cv, _app)
    assert cv._suggest_scan is None and not cv._workers  # a hiccup never wedges the check
    assert cv._suggest_host.isHidden()


# ----- stop means stopped -----


def test_stop_drops_queued_follow_ups(_app):
    # After a halt, a follow-up queued while HELIX was thinking used to run from _drain_pending the
    # moment the cancelled turn retired — so HELIX kept answering after "stop".
    cv = ConsoleView(_Conv([]), _Settings())
    started: list[str] = []
    cv._start_turn = lambda text, *a, **k: started.append(text)  # type: ignore[assignment]
    cv._has_claude_auth = lambda: True  # type: ignore[assignment]
    cv._busy = True
    cv._submit("tell me about the weather too", from_voice=False)
    assert cv._pending_msgs and started == []  # queued behind the live turn, not started

    cv._cancel_active()
    assert cv._pending_msgs == []
    assert "queued message" in cv.status.text()

    cv._busy = False       # the cancelled turn retires
    cv._drain_pending()
    assert started == []   # nothing left to answer


def test_stop_with_nothing_queued_keeps_the_plain_stopped_line(_app):
    cv = ConsoleView(_Conv([]), _Settings())
    cv._cancel_active()
    assert cv.status.text() == "Stopped."
