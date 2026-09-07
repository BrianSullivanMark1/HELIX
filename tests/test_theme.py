"""Theme pins — the scrollbar thickness contract, both orientations.

Scrollbars app-wide must stay thick enough to grab with a mouse (Brian's ask: the 8px default was
too thin to click). Both orientations are styled — an unstyled horizontal bar silently falls back
to the thin platform default, which is exactly the regression this guards against.
"""
from __future__ import annotations

import re

from helix.ui.theme import _STYLESHEET

_MIN_THICKNESS = 16  # px — anything thinner regresses to "hard to grab"


def _px(pattern: str) -> int:
    m = re.search(pattern, _STYLESHEET)
    assert m, f"stylesheet no longer styles: {pattern}"
    return int(m.group(1))


def test_vertical_scrollbar_is_thick_enough_to_grab():
    assert _px(r"QScrollBar:vertical\s*\{[^}]*width:\s*(\d+)px") >= _MIN_THICKNESS


def test_horizontal_scrollbar_is_styled_and_thick_enough_to_grab():
    # Wide chat tables scroll sideways — that bar must be as grabbable as the vertical one.
    assert _px(r"QScrollBar:horizontal\s*\{[^}]*height:\s*(\d+)px") >= _MIN_THICKNESS


def test_both_scrollbar_handles_have_a_hover_state():
    # The hover highlight is the "you're on it" cue that makes the bar feel clickable.
    for orient in ("vertical", "horizontal"):
        assert re.search(rf"QScrollBar::handle:{orient}:hover", _STYLESHEET)
