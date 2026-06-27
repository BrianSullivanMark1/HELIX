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
