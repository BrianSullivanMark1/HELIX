"""ChatInput — a prompt box that sends on Enter and breaks to a new line on Shift+Enter.

A drop-in replacement for the single-line QLineEdit the Console and the live "Edit with AI" bar used:
plain Enter sends (exactly like the old returnPressed), while Shift+Enter (or Ctrl+Enter) drops to a new
line so a request can be written in paragraph form. The box opens one line tall and grows with the text
up to a few lines, then scrolls — so it stays as compact as a line edit until you actually need the room.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QTextEdit

# The fixed chrome of the input QSS (theme.py: padding 10px top/bottom + a 1px border, both sides). Used
# only as a fallback height before the widget is first laid out — once shown we measure it for real.
_QSS_CHROME = 2 * 10 + 2 * 1


class ChatInput(QTextEdit):
    """A multi-line text box with line-edit ergonomics: Enter submits, Shift/Ctrl+Enter inserts a
    newline. Subclasses QTextEdit so it inherits the global input styling (the `QLineEdit, QTextEdit`
    QSS) for free, and exposes text()/setText() so existing line-edit call sites barely change."""

    submitted = pyqtSignal()  # Enter pressed — the owner reads text() and sends, like returnPressed did

    def __init__(self, placeholder: str = "", *, max_lines: int = 6) -> None:
        super().__init__()
        self._max_lines = max(1, max_lines)
        self.setPlaceholderText(placeholder)
        self.setAcceptRichText(False)            # plain text only — a paste never injects formatting
        self.setTabChangesFocus(True)            # Tab moves to the next control, it doesn't type a tab
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Drop the document's own margin so a single line is exactly as tall as the old line edit (the
        # 10px QSS padding already supplies the inner breathing room).
        self.document().setDocumentMargin(0)
        self.document().documentLayout().documentSizeChanged.connect(self._on_doc_resized)
        self._fit_height()

    # ----- line-edit compatibility (so callers keep using text()/setText()) -----
    def text(self) -> str:
        return self.toPlainText()

    def setText(self, text: str) -> None:
        self.setPlainText(text or "")

    # ----- Enter sends · Shift/Ctrl+Enter = new line -----
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            newline = event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
            )
            if newline:
                super().keyPressEvent(event)     # Shift/Ctrl+Enter: paragraph form, don't send
            else:
                self.submitted.emit()            # plain Enter: send (mirrors QLineEdit.returnPressed)
            return
        super().keyPressEvent(event)

    # ----- grow with the text, up to max_lines, then scroll -----
    def _on_doc_resized(self, *_args) -> None:
        self._fit_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        doc = self.document()
        doc.setTextWidth(self.viewport().width())
        line = self.fontMetrics().lineSpacing()
        # Border + QSS padding is constant whatever the widget's height, so measuring it from the live
        # geometry is stable; before the first layout the viewport isn't sized yet, so fall back.
        chrome = self.height() - self.viewport().height()
        if chrome < _QSS_CHROME:
            chrome = _QSS_CHROME
        wanted = doc.size().height()
        target = int(round(min(max(wanted, line), self._max_lines * line) + chrome))
        if target != self.height():
            self.setFixedHeight(target)
