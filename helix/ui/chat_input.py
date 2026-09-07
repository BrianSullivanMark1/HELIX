"""ChatInput — a prompt box that sends on Enter and breaks to a new line on Shift+Enter.

A drop-in replacement for the single-line QLineEdit the Console and the live "Edit with AI" bar used:
plain Enter sends (exactly like the old returnPressed), while Shift+Enter (or Ctrl+Enter) drops to a new
line so a request can be written in paragraph form. The box opens one line tall and grows with the text
up to a few lines, then scrolls — so it stays as compact as a line edit until you actually need the room.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QTextEdit

# The fixed chrome of the input QSS (theme.py: padding 10px top/bottom + a 1px border, both sides). Used
# only as a fallback height before the widget is first laid out — once shown we measure it for real.
_QSS_CHROME = 2 * 10 + 2 * 1

# Which dropped files go to VISION rather than to the text bundle: everything local is attached, this
# set only decides which signal carries it. Kept in step with helix.services.images.IMAGE_EXTS;
# duplicated here so this low-level widget stays service-free.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


class ChatInput(QTextEdit):
    """A multi-line text box with line-edit ergonomics: Enter submits, Shift/Ctrl+Enter inserts a
    newline. Subclasses QTextEdit so it inherits the global input styling (the `QLineEdit, QTextEdit`
    QSS) for free, and exposes text()/setText() so existing line-edit call sites barely change."""

    submitted = pyqtSignal()  # Enter pressed — the owner reads text() and sends, like returnPressed did
    imagePasted = pyqtSignal(object)     # a raw image (QImage) pasted or dropped from the clipboard
    imageFilesPasted = pyqtSignal(list)  # local image file paths (str) pasted or dropped in
    filesDropped = pyqtSignal(list)      # local NON-image paths (documents, folders) pasted or dropped in

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

    # ----- paste / drop a file → attach it (instead of pasting a path or nothing) -----
    @staticmethod
    def _local_files(source) -> tuple[list[str], list[str]]:
        """Split the local paths a paste/drop carries into (images, everything else). "Everything else"
        is deliberately unfiltered — documents and whole folders included — because that is exactly what
        the paperclip's "Attach files…"/"Attach folder…" already accepts; dragging is the same gesture
        with fewer clicks, so it must not accept less."""
        if not source.hasUrls():
            return [], []
        images: list[str] = []
        others: list[str] = []
        for u in source.urls():
            if not u.isLocalFile():
                continue
            path = u.toLocalFile()
            bucket = images if Path(path).suffix.lower() in _IMAGE_SUFFIXES else others
            bucket.append(path)
        return images, others

    def canInsertFromMimeData(self, source) -> bool:
        images, others = self._local_files(source)
        if source.hasImage() or images or others:
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:
        # Local FILE(s) first (a drag from a folder, or a "copy image" that carries a file URL) — attach
        # them rather than pasting their path as text. A dropped PDF used to fail the image filter and
        # land in the box as a literal file:/// URL, which the model can't open — the path isn't even
        # resolvable, since it keeps its scheme. Images and other files go out on their own signals and
        # a MIXED drop (a PNG plus a PDF) emits BOTH, because claiming only the first kind would drop
        # the other half of the drag on the floor.
        images, others = self._local_files(source)
        handled = False
        # Emit only where someone is listening. This same widget is also the "Edit with AI" bar
        # (app_viewer), which wires `submitted` alone and has no attachment tray to stage anything into;
        # an unconditional emit there would turn a drop into a silent no-op. With nobody connected we
        # fall through to QTextEdit, so the user at least sees the path land instead of nothing at all.
        if images and self.receivers(self.imageFilesPasted):
            self.imageFilesPasted.emit(images)
            handled = True
        if others and self.receivers(self.filesDropped):
            self.filesDropped.emit(others)
            handled = True
        if handled:
            return
        # Raw image bytes on the clipboard (a screenshot, "copy image" from a browser).
        if source.hasImage():
            image = source.imageData()
            if image is not None:
                self.imagePasted.emit(image)
                return
        super().insertFromMimeData(source)

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
