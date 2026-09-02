"""ChatInput — the prompt box shared by the Console and the live "Edit with AI" bar: plain Enter sends,
Shift+Enter drops to a new line (paragraph form), and the box grows with the text up to a few lines."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QEvent, QMimeData, Qt, QUrl  # noqa: E402
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


def _drop(widget: ChatInput, *paths: str) -> QMimeData:
    """Build the mime payload a real drag from Explorer carries (file URLs, which Qt also exposes as
    text) and hand it to the widget exactly as a drop would."""
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(p) for p in paths])
    widget.insertFromMimeData(md)
    return md


def test_dropping_a_document_attaches_it_instead_of_pasting_its_path(_app):
    # The paperclip reads PDFs; dragging one in used to fail the image filter and paste
    # 'file:///C:/tmp/report.pdf' into the box, which the model can never open.
    w = ChatInput("ph")
    dropped: list[list[str]] = []
    w.filesDropped.connect(dropped.append)
    _drop(w, "C:/tmp/report.pdf")
    assert dropped == [["C:/tmp/report.pdf"]]
    assert w.text() == ""                    # nothing pasted as text


def test_dropping_a_folder_attaches_it_like_the_attach_folder_menu_does(_app):
    w = ChatInput("ph")
    dropped: list[list[str]] = []
    w.filesDropped.connect(dropped.append)
    _drop(w, "C:/tmp/proposal_docs")         # no suffix at all — still an attachment, not text
    assert dropped == [["C:/tmp/proposal_docs"]]
    assert w.text() == ""


def test_a_mixed_drop_attaches_both_the_image_and_the_document(_app):
    # Taking only the first branch would silently lose the other half of the drag.
    w = ChatInput("ph")
    images: list[list[str]] = []
    others: list[list[str]] = []
    w.imageFilesPasted.connect(images.append)
    w.filesDropped.connect(others.append)
    _drop(w, "C:/tmp/pic.png", "C:/tmp/report.pdf")
    assert images == [["C:/tmp/pic.png"]]
    assert others == [["C:/tmp/report.pdf"]]
    assert w.text() == ""


def test_a_dropped_file_still_pastes_as_text_where_nothing_is_listening(_app):
    # The "Edit with AI" bar wires submitted only and has no attachment tray — claiming the drop there
    # would swallow it with no text and no attachment, which is worse than the visible path.
    w = ChatInput("ph")
    _drop(w, "C:/tmp/report.pdf")
    assert "report.pdf" in w.text()
