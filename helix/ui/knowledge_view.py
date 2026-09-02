"""KnowledgeView — manage one vault inside HELIX: add notes/files, search, remove documents.

Part of the immutable shell (like AppViewer), but deliberately a NATIVE Qt widget rather than a web view:
a vault is a document manager, not a built page, and a list + editor avoids the optional
PyQt6-WebEngine dependency entirely (more robust in the frozen build). The user adds material four ways —
paste a note, add files, add a folder, or drag files onto the panel — and the orb/agents search it
elsewhere. All file work goes through KnowledgeService, which keeps everything inside the vault's own
folder.

Nothing here touches disk or the network on the UI thread. Every one of this panel's jobs is expensive
in a way that is invisible until it bites: ingesting a scanned PDF runs OCR (up to 30s per document,
times up to 40 documents for a picked folder, plus a git commit each), searching ranks every chunk of
every document and — when an embedder is configured — makes a 30s-timeout network call, and opening a
saved document reads a file that may be megabytes. All three run on a QtWorker, exactly as the chat
attachment path does, so the orb keeps spinning and Windows never paints HELIX as "Not Responding".
"""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
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
from helix.ui.workers import QtWorker

_SOURCE_LABEL = {"note": "note", "file": "file", "folder": "folder", "task": "from a protocol"}

# How long the search box stays quiet after the last keystroke before a search actually runs. Long
# enough that typing a word is ONE search rather than one per letter, short enough to feel instant.
_SEARCH_DEBOUNCE_MS = 300

# The one line the search path is allowed to leave standing. Named because _set_search_status has to
# recognise its own handwriting: the status label is shared with the ingest and note paths, and the
# search must never blank or overwrite a message it did not write.
_SEARCHING = "Searching…"


class _DocRow(QFrame):
    """One stored document: title + a small provenance line, an Open toggle that reveals the stored
    text underneath, and a Remove button.

    The read pane is why the Open button exists: a saved note or ingested file could previously only be
    deleted, never read back — the user saw a title and a byte count and nothing else. It stays hidden
    until asked for so the panel is still a list at a glance."""

    def __init__(self, title: str, meta: str, on_remove, on_open) -> None:
        super().__init__()
        self.setObjectName("Card")
        self.expanded = False  # tracked explicitly: isVisible() is False for the whole panel when the
        # Vault is not the stack's current widget, so it can't answer "is this row's pane open?"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)
        row = QHBoxLayout()
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
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(lambda _checked=False: on_open(self))
        row.addWidget(self.open_btn, alignment=Qt.AlignmentFlag.AlignTop)
        remove = QPushButton("✕ Remove")
        remove.clicked.connect(on_remove)
        row.addWidget(remove, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(row)
        self.body = QLabel("")
        # Stored text came from the user's own files and notes — untrusted, and never live rich text.
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setStyleSheet(f"color:{MUTED};background:{PANEL};")
        self.body.hide()
        outer.addWidget(self.body)

    def show_text(self, text: str) -> None:
        self.expanded = True
        self.body.setText(text)
        self.body.show()
        self.open_btn.setText("Close")

    def collapse(self) -> None:
        self.expanded = False
        self.body.setText("")
        self.body.hide()
        self.open_btn.setText("Open")


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
        # Strong refs to every in-flight worker until its QThread truly finishes — a garbage-collected
        # QThread that is still running takes the process down with it.
        self._workers: set[QtWorker] = set()
        self._busy = False  # an ingest is running: the Add buttons and drops are refused until it ends
        # Two separate facts about a row's read, and conflating them is what let a third click start a
        # second doc_text worker on the same file. `_reading` is "this row is still a legal place to
        # PAINT a result" — it is dropped the moment the user closes the pane or the list is rebuilt, so
        # a late result lands on nobody instead of re-opening a pane they just closed or reaching into a
        # widget Qt already deleted. `_loading` is "a read for this row is still IN FLIGHT" — it survives
        # a close, because closing the pane does not stop the thread, and only the worker retiring
        # clears it.
        self._reading: set[_DocRow] = set()
        self._loading: set[_DocRow] = set()
        # Bumped on every search (and on clearing the box) so a slow query's results can be recognised as
        # stale and dropped instead of painting over what the user is looking at now.
        self._search_seq = 0

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
        self._add_files_btn = QPushButton("📄 Add files")
        self._add_files_btn.clicked.connect(self._add_files)
        self._add_folder_btn = QPushButton("📁 Add folder")
        self._add_folder_btn.clicked.connect(self._add_folder)
        actions.addWidget(save_note)
        actions.addWidget(self._add_files_btn)
        actions.addWidget(self._add_folder_btn)
        actions.addStretch(1)
        body.addLayout(actions)

        # Search-this-base box: typing previews the passages the orb would find.
        self._search = QLineEdit()
        self._search.setPlaceholderText("🔍 Search this vault…")
        self._search.textChanged.connect(self._on_search)
        # The debounce timer. Restarted by every keystroke, so a search runs once the typing pauses
        # rather than once per letter — see _on_search for what one search actually costs.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._run_search)
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
        self._search_timer.stop()
        self._search_seq += 1  # a search still in flight for the PREVIOUS vault must not land in this one
        self._search.clear()  # clearing triggers _on_search → shows the document list
        self._note.clear()
        self._status.setText("")
        self._title.setText(f"Vault › {name}")
        self._render_docs()

    def reload(self) -> None:
        """Re-read from disk (e.g. the orb just saved a note into the open base)."""
        if self._slug is None:
            return
        if self._search.text().strip():
            self._run_search()  # an explicit refresh: run it now rather than waiting out the debounce
        else:
            self._render_docs()

    def shutdown(self) -> None:
        """Wait briefly for any in-flight read so we never destroy a running QThread on close — the same
        contract the console and launcher views honour during teardown."""
        self._search_timer.stop()  # don't let a pending search fire into a torn-down view
        for worker in list(self._workers):
            worker.wait(3000)

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
        if self._slug is None or self._busy:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files to this vault")
        if paths:
            picked = [Path(p) for p in paths]
            self._ingest(
                lambda ks, slug, emit: ks.add_files(slug, picked, on_progress=emit), source="files"
            )

    def _add_folder(self) -> None:
        if self._slug is None or self._busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "Add a folder to this vault")
        if folder:
            picked = Path(folder)
            self._ingest(
                lambda ks, slug, emit: ks.add_folder(slug, picked, on_progress=emit), source="folder"
            )

    def _ingest(self, job, *, source: str) -> None:
        """Run one ingest OFF the UI thread and narrate it.

        Reading is the expensive half and it got ~100x more expensive when OCR landed: a single scanned
        PDF can hold the extractor for its full 30s budget, a picked folder can hand it forty of them,
        and every stored document is git-committed on the way in. Run inline — as this panel used to —
        that is minutes of a frozen window with no repaint and no orb, which Windows labels "Not
        Responding". The chat attachment path was moved off the UI thread for exactly this reason.

        The Add buttons go quiet for the duration so a second pick can't stack a second ingest onto the
        same vault's index (the service serializes writes, but two ingests would still queue behind each
        other invisibly)."""
        if self._slug is None or self._busy:
            return
        slug = self._slug
        ks = self._knowledge
        self._set_busy(True)
        self._status.setText("Reading…")

        def _run(emit) -> object:
            return job(ks, slug, emit)

        worker = QtWorker(_run)
        self._workers.add(worker)
        worker.progress.connect(self._status.setText)
        worker.finished_ok.connect(lambda added, s=source: self._after_ingest(list(added), source=s))
        worker.failed.connect(self._on_ingest_failed)
        worker.finished.connect(lambda w=worker: self._retire_ingest(w))
        worker.start()

    def _retire_ingest(self, worker: QtWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._add_files_btn.setEnabled(not busy)
        self._add_folder_btn.setEnabled(not busy)

    def _on_ingest_failed(self, _message: str) -> None:
        """The worker already logged the traceback. The user gets a plain sentence they can act on —
        never the exception text, which names nothing they can do anything about.

        An ingest stores each document as it reads it, so a failure on file nine of forty leaves the
        first eight saved and committed. The old wording promised "nothing was added", which was simply
        untrue in that case, and the panel never reloaded — so the list and the header count went on
        showing the vault as it was before the drop, and the eight documents that DID land were
        invisible until the user navigated away and back. Reload, and say only what is true either way:
        whatever made it in is on screen."""
        self._status.setText(
            "Something went wrong partway through reading those. Anything that made it in is in the "
            "list below — want to try the rest again?"
        )
        self.reload()

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
            self, "Remove", f"Remove “{title}” from this vault? This can’t be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self._knowledge.remove_doc(self._slug, doc_id)
            self.reload()

    # ----- render -----
    def _clear_list(self) -> None:
        # Every row about to be destroyed stops being a legal target for an in-flight read: _show_doc
        # checks membership here, so a doc_text that lands after the list was rebuilt is dropped rather
        # than reaching into a C++ object Qt has already deleted. `_loading` is emptied for the same
        # reason — these row objects are on their way to deleteLater, so holding them until their worker
        # retires would keep dead widgets in a set we hash on.
        self._reading.clear()
        self._loading.clear()
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
                    _DocRow(
                        doc.title, meta,
                        lambda _c=False, d=doc.id, t=doc.title: self._remove(d, t),
                        lambda row, d=doc.id: self._open_doc(row, d),
                    )
                )
        self._list.addStretch(1)

    # ----- read pane -----
    def _open_doc(self, row: _DocRow, doc_id: str) -> None:
        """Toggle one document's read pane. The stored text is loaded OFF the UI thread: an ingested PDF
        can be megabytes of extracted text, and this panel's whole point is that reading your own
        material never costs you the window."""
        if self._slug is None:
            return
        if row.expanded:
            row.collapse()
            self._reading.discard(row)  # a result that arrives now must not re-open the pane they closed
            return
        if row in self._loading:
            # The pane is shut but its read is still on the wire: the user opened a big document, closed
            # it before the file came back, and has now opened it again. Re-arm this row as the landing
            # target and let the read already running paint into it. Starting a fresh one instead — which
            # is what happened while "already loading" was keyed off `expanded` and so could never be
            # true — means two threads re-reading the same megabyte PDF for one pane, and whichever
            # finishes second overwrites the first for nothing.
            self._reading.add(row)
            row.show_text("Opening…")
            return
        self._reading.add(row)
        self._loading.add(row)
        row.show_text("Opening…")
        slug = self._slug
        ks = self._knowledge

        def _read(_emit) -> object:
            return ks.doc_text(slug, doc_id)

        worker = QtWorker(_read)
        self._workers.add(worker)
        worker.finished_ok.connect(lambda text, r=row: self._show_doc(r, str(text)))
        # A document that won't load is a plain sentence in the pane, not a raised dialog — the user is
        # browsing, and the rest of the vault still works.
        worker.failed.connect(lambda _msg, r=row: self._show_doc(r, ""))
        worker.finished.connect(lambda w=worker, r=row: self._retire_doc_read(w, r))
        worker.start()

    def _show_doc(self, row: _DocRow, text: str) -> None:
        if row not in self._reading:
            return  # the list was rebuilt while the read was in flight — that row no longer exists
        row.show_text(text.strip() or "There's no text stored for this one.")

    def _retire_doc_read(self, worker: QtWorker, row: _DocRow) -> None:
        """A document read finished (either way). The row stops being "in flight" HERE and nowhere else:
        clearing it on collapse instead is what let the next click start a duplicate worker."""
        self._loading.discard(row)
        self._retire_read(worker)

    def _retire_read(self, worker: QtWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    # ----- search -----
    def _on_search(self, text: str) -> None:
        """Every keystroke lands here, and deliberately does NOT search.

        One search ranks every chunk of every document in the vault and, when an embedder is configured,
        embeds the query over the network with a 30s timeout — on a cold cache it uploads every chunk
        first. Doing that per keystroke on the UI thread meant a ten-letter query fired ten blocking
        round-trips and froze the whole window. So: clearing the box is instant (it only re-renders the
        list we already hold), and a real query waits out a short pause and then runs on a worker.

        Touching the box also wipes the status line, and that is the point of doing it HERE rather than
        in either branch below. Backspacing a query away used to leave "Searching…" pinned under the box
        forever — the superseded search returns to _show_hits, sees a stale sequence number and returns
        BEFORE the only line that clears the label, so the panel sat there reporting a 30s round-trip
        that had already been abandoned, over a fully repainted document list. Clearing on the keystroke
        that caused the supersede fixes that without inverting it (a stale result must still never blank
        a label the newer search owns). It also hands the line back to the search: whatever stood there
        — "Saved “X”.", an ingest confirmation — described an action that is over, and the user has
        visibly moved on to searching."""
        self._status.setText("")
        if not text.strip():
            self._search_timer.stop()
            self._search_seq += 1  # an in-flight search must not paint over the document list
            self._render_docs()
            return
        self._search_timer.start()  # restarts the quiet window on every keystroke

    def _set_search_status(self, text: str) -> None:
        """Write the search's own progress line — and ONLY over a line the search owns.

        reload() re-runs the query whenever the search box is non-empty, and _add_note/_after_ingest/
        _on_ingest_failed all call reload() immediately after saying what they did. Writing the status
        unconditionally meant "Added 3 documents from files." lived for microseconds before _run_search
        replaced it with "Searching…" and _show_hits then blanked it — the user was told nothing at all
        about the drop they just made. The search only ever needs the label when it already holds its
        own "Searching…" or nothing; anything else is a message someone still has to read."""
        if self._status.text() in ("", _SEARCHING):
            self._status.setText(text)

    def _run_search(self) -> None:
        text = self._search.text().strip()
        if not text or self._slug is None:
            return
        self._search_seq += 1
        seq = self._search_seq
        ks = self._knowledge
        name = self._name
        self._set_search_status(_SEARCHING)

        def _search(_emit) -> object:
            return ks.preview(text, name)

        worker = QtWorker(_search)
        self._workers.add(worker)
        worker.finished_ok.connect(lambda hits, q=text, s=seq: self._show_hits(q, list(hits), s))
        # A search that fails renders as "nothing matched" rather than an error the user can't act on.
        worker.failed.connect(lambda _msg, q=text, s=seq: self._show_hits(q, [], s))
        worker.finished.connect(lambda w=worker: self._retire_read(w))
        worker.start()

    def _show_hits(self, text: str, hits: list, seq: int) -> None:
        if seq != self._search_seq:
            return  # the user typed on, or cleared the box — these results are for a query that's gone
        self._set_search_status("")  # this search is over; say nothing rather than "Searching…"
        self._clear_list()
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
        if self._slug is None or self._busy:
            return  # an ingest is already running; a second drop would queue invisibly behind it
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if str(p)]
        if not paths:
            return
        files = [p for p in paths if p.is_file()]
        dirs = [p for p in paths if p.is_dir()]
        if not files and not dirs:
            return

        def job(ks, slug, emit, _files=files, _dirs=dirs) -> list:
            added: list = []
            if _files:
                added += ks.add_files(slug, _files, on_progress=emit)
            for d in _dirs:
                added += ks.add_folder(slug, d, on_progress=emit)
            return added

        # A dropped folder of scans is the single worst case this panel has — hence the same worker the
        # Add buttons use, rather than doing it here where the drop was delivered (the UI thread).
        self._ingest(job, source="drop")
        event.acceptProposedAction()
