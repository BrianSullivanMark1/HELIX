"""ChatInput — the prompt box shared by the Console and the live "Edit with AI" bar: plain Enter sends,
Shift+Enter drops to a new line (paragraph form), and the box grows with the text up to a few lines."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QEvent, Qt  # noqa: E402
from PyQt6.QtGui import QKeyEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from helix.ui.chat_input import ChatInput  # noqa: E402


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def _press_return(widget: ChatInput, *, shift: bool = False) -> None:
    mod = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
    widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, mod, "\r"))


def test_plain_enter_submits_without_inserting_newline(_app):
    w = ChatInput("ph")
    w.setText("build me a todo app")
    fired: list[int] = []
    w.submitted.connect(lambda: fired.append(1))
    _press_return(w)
    assert fired == [1]                      # Enter sent, exactly once
    assert w.text() == "build me a todo app"  # and did NOT add a newline


def test_shift_enter_inserts_newline_and_does_not_submit(_app):
    w = ChatInput("ph")
    w.setText("first")
    fired: list[int] = []
    w.submitted.connect(lambda: fired.append(1))
    # cursor sits at the end after setText? force it to the end, then Shift+Enter
    cur = w.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    w.setTextCursor(cur)
    _press_return(w, shift=True)
    assert fired == []                       # Shift+Enter never sends
    assert w.text() == "first\n"             # it dropped to a new line (paragraph form)


def test_text_setext_are_line_edit_compatible(_app):
    w = ChatInput("ph")
    w.setText("hello")
    assert w.text() == "hello"
    w.clear()
    assert w.text() == ""
    w.setText(None)                          # tolerates None like the old setText callers expect
    assert w.text() == ""


def test_box_grows_with_lines_then_clamps(_app):
    w = ChatInput("ph", max_lines=6)
    w.resize(420, 60)
    w.show()
    _app.processEvents()

    w.setText("alpha")
    _app.processEvents()
    one = w.height()

    w.setText("\n".join(["alpha", "beta", "gamma", "delta"]))
    _app.processEvents()
    four = w.height()
    assert four > one                        # it grows as paragraphs are added

    w.setText("\n".join(["x"] * 30))
    _app.processEvents()
    huge = w.height()
    w.setText("\n".join(["x"] * 10))
    _app.processEvents()
    ten = w.height()
    assert huge == ten                       # both past max_lines → clamped to the same height

    w.setText("alpha")
    _app.processEvents()
    assert w.height() == one                 # shrinks back to a single line
