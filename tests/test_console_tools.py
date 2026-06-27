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
    assert QGuiApplication.clipboard().text() == "hello there"
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
    assert QGuiApplication.clipboard().text() == "Q1\t10\nQ2\t25"
    assert msgs and "Copied" in msgs[-1]


class _FakeSettings:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


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
