"""KnowledgeView — manage one knowledge base inside HELIX: add notes/files, search, remove documents.

Part of the immutable shell (like AppViewer), but deliberately a NATIVE Qt widget rather than a web view:
a knowledge base is a document manager, not a built page, and a list + editor avoids the optional
PyQt6-WebEngine dependency entirely (more robust in the frozen build). The user adds material four ways —
paste a note, add files, add a folder, or drag files onto the panel — and the orb/agents search it
elsewhere. All file work goes through KnowledgeService, which keeps everything inside the base's own
folder.
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from helix.services.knowledge import KnowledgeService
from helix.ui.theme import CYAN, LINE, MUTED, PANEL, STATUS_DONE

_SOURCE_LABEL = {"note": "note", "file": "file", "folder": "folder", "task": "from a flow"}


class _DocRow(QFrame):
    """One stored document: title + a small provenance line, with a Remove button."""

    def __init__(self, title: str, meta: str, on_remove) -> None:
        super().__init__()
        self.setObjectName("Card")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        col = QVBoxLayout()
        col.setSpacing(2)
        name = QLabel(title)
        name.setTextFormat(Qt.TextFormat.PlainText)  # titles are user/file-derived — never live rich text
        name.setStyleSheet(f"color:{CYAN};font-weight:600;")
        name.setWordWrap(True)
        sub = QLabel(meta)
        sub.setTextFormat(Qt.TextFormat.PlainText)
        sub.setStyleSheet(f"color:{MUTED};font-size:12px;")
        col.addWidget(name)
        col.addWidget(sub)
        row.addLayout(col, stretch=1)
        remove = QPushButton("✕ Remove")
        remove.clicked.connect(on_remove)
        row.addWidget(remove, alignment=Qt.AlignmentFlag.AlignTop)


class KnowledgeView(QWidget):
    """A header (Back / title / count) above an add bar, a search box, and the document list."""

    closeRequested = pyqtSignal()

    def __init__(self, knowledge: KnowledgeService) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self._knowledge = knowledge
        self._slug: str | None = None
        self._name: str = ""
        self.setAcceptDrops(True)  # drop files anywhere on the panel to ingest them

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        bar = QWidget()
        bar.setStyleSheet(f"border-bottom:1px solid {LINE};")
        hrow = QHBoxLayout(bar)
        hrow.setContentsMargins(16, 10, 16, 10)
        hrow.setSpacing(8)
        back = QPushButton("←  Back")
        back.setObjectName("Nav")
        back.clicked.connect(self.closeRequested.emit)
        self._title = QLabel("")
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setStyleSheet(f"color:{CYAN};font-weight:600;")
        self._count = QLabel("")
        self._count.setStyleSheet(f"color:{MUTED};")
        hrow.addWidget(back)
        hrow.addWidget(self._title)
        hrow.addStretch(1)
        hrow.addWidget(self._count)
        root.addWidget(bar)

        body = QVBoxLayout()
        body.setContentsMargins(28, 18, 28, 22)
        body.setSpacing(12)

        # Add bar: paste a note + the file/folder pickers.
        self._note = QTextEdit()
        self._note.setPlaceholderText("Paste or type a note to remember…  (or drop files onto this panel)")
        self._note.setFixedHeight(72)
        body.addWidget(self._note)

        actions = QHBoxLayout()
        save_note = QPushButton("＋ Save note")
        save_note.setObjectName("Primary")
        save_note.clicked.connect(self._add_note)
        add_files = QPushButton("📄 Add files")
        add_files.clicked.connect(self._add_files)
        add_folder = QPushButton("📁 Add folder")
        add_folder.clicked.connect(self._add_folder)
        actions.addWidget(save_note)
        actions.addWidget(add_files)
        actions.addWidget(add_folder)
        actions.addStretch(1)
        body.addLayout(actions)

        # Search-this-base box: typing previews the passages the orb would find.
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 Search this knowledge…")
        self._search.textChanged.connect(self._on_search)
        body.addWidget(self._search)

        self._status = QLabel("")
        self._status.setObjectName("Status")
        self._status.setWordWrap(True)
        body.addWidget(self._status)

        # Content area: the document list, or search results when there's a query.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._host = QWidget()
        self._list = QVBoxLayout(self._host)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(10)
        self._list.addStretch(1)
        scroll.setWidget(self._host)
        body.addWidget(scroll, stretch=1)

        root.addLayout(body, stretch=1)

    # ----- lifecycle -----
    def open_base(self, slug: str, name: str) -> None:
        self._slug = slug
        self._name = name
        self._search.clear()  # clearing triggers _on_search → shows the document list
        self._note.clear()
        self._status.setText("")
        self._title.setText(f"Knowledge › {name}")
        self._render_docs()

    def reload(self) -> None:
        """Re-read from disk (e.g. the orb just saved a note into the open base)."""
        if self._slug is None:
            return
        if self._search.text().strip():
            self._on_search(self._search.text())
        else:
            self._render_docs()

    # ----- add -----
    def _add_note(self) -> None:
        if self._slug is None:
            return
        text = self._note.toPlainText().strip()
        if not text:
            self._status.setText("Type a note first.")
            return
        doc = self._knowledge.add_note(self._slug, text)
        if doc is None:
            self._status.setText("That note was empty.")
            return
        self._note.clear()
        self._status.setText(f"Saved “{doc.title}”.")
        self.reload()

    def _add_files(self) -> None:
        if self._slug is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files to this knowledge")
        if paths:
            added = self._knowledge.add_files(self._slug, [Path(p) for p in paths])
            self._after_ingest(added, source="files")

    def _add_folder(self) -> None:
        if self._slug is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "Add a folder to this knowledge")
        if folder:
            added = self._knowledge.add_folder(self._slug, Path(folder))
            self._after_ingest(added, source="folder")

    def _after_ingest(self, added: list, *, source: str) -> None:
        if added:
            self._status.setText(
                f"Added {len(added)} document{'s' if len(added) != 1 else ''} from {source}."
            )
        else:
            self._status.setText(
                "Nothing readable to add — text, Markdown, code, PDF, and Word files are supported "
                "(other binaries are skipped)."
            )
        self.reload()

    # ----- remove -----
    def _remove(self, doc_id: str, title: str) -> None:
        if self._slug is None:
            return
        confirm = QMessageBox.question(
            self, "Remove", f"Remove “{title}” from this knowledge? This can’t be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._knowledge.remove_doc(self._slug, doc_id)
            self.reload()

    # ----- render -----
    def _clear_list(self) -> None:
        while self._list.count():
            item = self._list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _render_docs(self) -> None:
        if self._slug is None:
            return
        self._clear_list()
        docs = self._knowledge.docs(self._slug)
        self._count.setText(f"{len(docs)} document{'s' if len(docs) != 1 else ''}")
        if not docs:
            empty = QLabel("Nothing saved yet. Paste a note, add files, or drop files onto this panel.")
            empty.setObjectName("Status")
            empty.setWordWrap(True)
            self._list.addWidget(empty)
        else:
            for doc in reversed(docs):  # newest first
                meta = f"{_SOURCE_LABEL.get(doc.source, doc.source)} · {self._kb(doc.bytes)}"
                self._list.addWidget(
                    _DocRow(doc.title, meta, lambda _c=False, d=doc.id, t=doc.title: self._remove(d, t))
                )
        self._list.addStretch(1)

    def _on_search(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._render_docs()
            return
        if self._slug is None:
            return
        self._clear_list()
        hits = self._knowledge.preview(text, self._name)
        if not hits:
            label = QLabel(f"No passages match “{text}”.")
            label.setObjectName("Status")
            label.setWordWrap(True)
            self._list.addWidget(label)
        else:
            self._count.setText(f"{len(hits)} match{'es' if len(hits) != 1 else ''}")
            for h in hits:
                card = QFrame()
                card.setObjectName("Card")
                col = QVBoxLayout(card)
                col.setContentsMargins(14, 10, 14, 10)
                col.setSpacing(4)
                t = QLabel(h.title)
                t.setTextFormat(Qt.TextFormat.PlainText)
                t.setStyleSheet(f"color:{STATUS_DONE};font-weight:600;font-size:12px;")
                body = QLabel(self._snippet(h.text))
                body.setTextFormat(Qt.TextFormat.PlainText)
                body.setWordWrap(True)
                body.setStyleSheet(f"color:{MUTED};")
                col.addWidget(t)
                col.addWidget(body)
                self._list.addWidget(card)
        self._list.addStretch(1)

    @staticmethod
    def _snippet(text: str, limit: int = 320) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit] + "…"

    @staticmethod
    def _kb(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        return f"{n // 1024} KB"

    # ----- drag & drop -----
    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        if self._slug is None:
            return
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if str(p)]
        if not paths:
            return
        files = [p for p in paths if p.is_file()]
        dirs = [p for p in paths if p.is_dir()]
        added: list = []
        if files:
            added += self._knowledge.add_files(self._slug, files)
        for d in dirs:
            added += self._knowledge.add_folder(self._slug, d)
        self._after_ingest(added, source="drop")
        event.acceptProposedAction()
