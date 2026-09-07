"""Orb frame-rate discipline — both orbs, pinned.

The orb is on screen for the entire life of a permanently-open app, so its draw loop is the one piece
of UI whose cost is paid continuously rather than per interaction. The QPainter orb has always been
careful about this (~30fps while something is moving, ~15fps for idle breathing). The WebGL orb — the
opt-in GPU layer, run only when `shader_orb` is set true in helix_settings.json (main_window defaults
it to False) — had a bare `requestAnimationFrame` loop with no cap and no visibility gate, so it drew
at the display's full refresh (60-165Hz) forever, in a separate Chromium process, whether or not
anything was moving. Opt-in is not a reason to leave it uncapped: whoever turns it on runs it for the
entire life of the app, and nothing in the app would ever tell them a second process is burning a core
in the background.

Note what is deliberately NOT pinned here: the painter orb keeps its timer running while the WebGL orb
covers it, and that is correct. Because the WebGL view is opaque (alpha 255 via setBackgroundColor),
Qt's repaint manager subtracts the covered region and the fallback's update() calls resolve to zero
paintEvents — measured at 0 paints while occluded, resuming the instant the view hides. Stopping that
timer by hand looked like an optimisation and is not one.

Neither pinned property is observable from a unit test of behaviour, and both are one careless edit
away from being lost, so they are pinned as invariants on the source itself.
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from helix.ui import shader_orb


@pytest.fixture(scope="module")
def _qt_app():
    pytest.importorskip("PyQt6.QtWidgets")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _loop_body() -> str:
    """The text of the requestAnimationFrame loop in the WebGL orb's page."""
    html = shader_orb._ORB_HTML_TEMPLATE
    start = html.index("(function loop(){")
    return html[start:html.index("})();", start)]


def test_webgl_orb_caps_its_frame_rate():
    body = _loop_body()
    # A frame budget keyed off a busy/idle decision, matching the painter orb's two rates.
    assert "1/30" in body and "1/15" in body, (
        "the WebGL draw loop no longer has a busy/idle frame budget — it will render at the display's "
        "full refresh rate continuously"
    )
    assert "busy" in body, "expected an explicit busy/idle decision driving the frame budget"


def test_webgl_orb_does_not_draw_while_hidden():
    body = _loop_body()
    assert "document.hidden" in body, (
        "the WebGL orb must stop drawing when the shell is minimised or the page is occluded"
    )


def test_webgl_orb_draw_is_gated_not_unconditional():
    """`renderer.render` must be reachable only past a guard. The original loop called it once,
    unconditionally, at the end of every animation frame."""
    body = _loop_body()
    draws = [m.start() for m in re.finditer(r"renderer\.render\(", body)]
    assert len(draws) == 2, (
        f"expected exactly two draw sites (the unthrottled first frame, and the throttled steady "
        f"state); found {len(draws)}"
    )
    # Everything after the frame-budget guard is the throttled path; the guard must precede the last draw.
    guard = body.rindex("acc <")
    assert guard < draws[-1], "the steady-state draw must sit behind the frame-budget guard"


def test_webgl_orb_first_frame_is_never_throttled():
    """The shell only swaps the painter orb out for the WebGL one when the page sets its ready title,
    and that happens on the first successful draw. Throttling that frame away would strand the app on
    the fallback orb forever."""
    body = _loop_body()
    first = body.index("if (!started)")
    budget = body.rindex("acc <")
    assert first < budget, "the !started fast path must come before the frame-budget guard"
    assert "READY" in body[first:budget], "the first-frame path must still announce readiness"


def test_painter_orb_keeps_its_idle_throttle():
    """The QPainter fallback's own discipline, pinned alongside so the two can't drift apart."""
    import inspect

    from helix.ui.orb import PresenceOrb

    src = inspect.getsource(PresenceOrb.__init__)
    assert "self._active_ms, self._idle_ms = 33, 66" in src, (
        "the painter orb's ~30fps active / ~15fps idle split changed — keep it in step with the WebGL "
        "orb's frame budget so the two orbs don't drift apart"
    )
