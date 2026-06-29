"""The Console shows the recent conversation on load (persists across launches)."""
from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.domain.models import Message, Role  # noqa: E402
from helix.ui.console_view import ConsoleView, _Bubble  # noqa: E402


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


def test_console_renders_recent_history_on_load(_app):
    at = datetime(2026, 6, 29, 12, 0, 0)
    msgs = [
        Message(Role.USER, "what's the weather?", at),
        Message(Role.ASSISTANT, "Clear and 72.", at),
        Message(Role.USER, "thanks", at),
        Message(Role.ASSISTANT, "Anytime.", at),
    ]
    conv = _Conv(msgs)
    cv = ConsoleView(conv, _Settings())
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
    texts = [t for _, t in _bubbles(cv)]
    assert texts == ["Here are the numbers."]
    assert all("viz" not in t and "{" not in t for t in texts)


def test_console_empty_history_is_fine(_app):
    cv = ConsoleView(_Conv([]), _Settings())
    assert _bubbles(cv) == []
