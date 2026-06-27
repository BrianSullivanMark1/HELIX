"""Inline viz widgets paint headlessly without error — every chart kind, plus odd/empty data.

The chart code is QPainter, so it's verified on Qt's 'offscreen' platform (no display needed). Good-data
cases call each painter DIRECTLY so a real bug raises (rather than being hidden by paintEvent's
fall-back-to-bars guard); empty/malformed cases go through the full paintEvent via grab().
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QRectF  # noqa: E402
from PyQt6.QtGui import QPainter, QPixmap  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui.console_view import ConsoleView, _ChartWidget  # noqa: E402
from helix.ui.orb import OrbState, PresenceOrb  # noqa: E402
from helix.ui.voice import _pcm_bands  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    app = QApplication.instance() or QApplication([])
    yield app


_DATA = [{"label": f"Q{i}", "value": v} for i, v in enumerate([10, 25, 7, 18, 31], 1)]


@pytest.mark.parametrize("kind", ["bar", "column", "line", "area", "pie", "donut"])
def test_each_painter_runs_without_error(_app, kind):
    w = _ChartWidget({"type": "chart", "title": "Quarterly", "unit": "$", "kind": kind, "data": _DATA})
    w.resize(460, 260)
    w._t = 1.0
    pm = QPixmap(460, 260)
    p = QPainter(pm)
    area = QRectF(0.0, 28.0, 460.0, 232.0)
    try:
        if kind in ("pie", "donut"):
            w._paint_pie(p, area, donut=kind == "donut")
        elif kind in ("line", "area"):
            w._paint_line(p, area, fill=kind == "area")
        else:
            w._paint_bars(p, area)
    finally:
        p.end()


@pytest.mark.parametrize("kind", ["bar", "line", "area", "pie", "donut", "unknown-kind", None])
@pytest.mark.parametrize("data", [_DATA, [], [{"label": "a"}], [{"value": "x"}], "nonsense", None])
def test_full_paintevent_survives_any_data(_app, kind, data):
    spec = {"type": "chart", "data": data}
    if kind is not None:
        spec["kind"] = kind
    w = _ChartWidget(spec)
    w.resize(440, 240)
    w.render_now()
    assert not w.grab().isNull()  # grab() forces the real paintEvent (with its fall-back guard)


def test_table_numeric_cells_are_detected():
    # Right-alignment of value columns keys off this helper.
    assert ConsoleView._looks_numeric("1,234")
    assert ConsoleView._looks_numeric("$42")
    assert ConsoleView._looks_numeric("87%")
    assert not ConsoleView._looks_numeric("Revenue")
    assert not ConsoleView._looks_numeric("")


@pytest.mark.parametrize("state", list(OrbState))
def test_orb_paints_in_every_state_with_a_spectrum(_app, state):
    orb = PresenceOrb()
    orb.resize(320, 320)
    orb.set_state(state)
    orb.set_level(0.7)
    orb.set_bands([0.2, 0.9, 0.4, 0.7, 0.1] * 4)  # 20 values → truncated to 16
    orb._tick()  # advance the eased values once so the spectral ring has magnitude
    assert not orb.grab().isNull()


def test_orb_set_bands_is_robust_to_odd_input(_app):
    orb = PresenceOrb()
    orb.resize(200, 200)
    for bands in ([], [0.5], [1.0] * 64, "nonsense", None):
        orb.set_bands(bands)  # defensive: never raises, even on garbage
        orb._tick()
        assert not orb.grab().isNull()


def test_pcm_bands_silence_is_near_zero():
    pytest.importorskip("numpy")
    bands = _pcm_bands(b"\x00\x00" * 1024)
    assert len(bands) == 16 and max(bands) < 0.05


def test_pcm_bands_loud_tone_lights_up():
    pytest.importorskip("numpy")
    import array
    import math as _math

    n = 1024
    tone = array.array("h", [int(22000 * _math.sin(2 * _math.pi * 8 * i / n)) for i in range(n)])
    bands = _pcm_bands(tone.tobytes())
    assert len(bands) == 16 and max(bands) > 0.1
