"""Knowledge UI — the Menu's Vault tab renders vaults, and the KnowledgeView manages one vault
(add a note, search, list/remove docs) without a webview."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDropEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from helix.services.builds import BuildService  # noqa: E402
from helix.services.knowledge import KnowledgeService  # noqa: E402
from helix.ui.knowledge_view import KnowledgeView, _DocRow  # noqa: E402
from helix.ui.launcher_view import LauncherView, _KNOWLEDGE  # noqa: E402


class _NoRepo:
    def init(self, _ws) -> None: ...
    def commit_all(self, _ws, _msg) -> None: ...


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 6, 29, 9, 0, 0)


class _Agents:
    def list(self):
        return []


class _Tasks:
    pass


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


def _knowledge(tmp_path):
    builds = BuildService(tmp_path, _NoRepo(), _FixedClock())
    return builds, KnowledgeService(builds, _NoRepo(), _FixedClock())


def _doc_rows(view: KnowledgeView) -> int:
    return sum(
        1 for i in range(view._list.count())
        if isinstance(view._list.itemAt(i).widget(), _DocRow)
    )


def _rows(view: KnowledgeView) -> list[_DocRow]:
    return [
        view._list.itemAt(i).widget() for i in range(view._list.count())
        if isinstance(view._list.itemAt(i).widget(), _DocRow)
    ]


def _settle(view: KnowledgeView, app, timeout: float = 5.0) -> None:
    """Let the search debounce fire, any worker finish, and its queued signals land on the GUI thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if not view._workers and not view._search_timer.isActive():
            break
        for w in list(view._workers):
            w.wait(20)
        time.sleep(0.005)
    app.processEvents()


def _spin(app, predicate, timeout: float = 5.0) -> bool:
    """Pump the GUI thread until `predicate` holds (or we give up)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _Watched:
    """The real KnowledgeService, wrapped so a test can see WHICH thread each call ran on and hold one
    of them mid-flight. Everything not named here passes straight through."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.ingest_threads: list[int] = []
        self.preview_threads: list[int] = []
        self.read_threads: list[int] = []
        self.previews = 0
        self.progress: list[str] = []
        self.ingesting = threading.Event()
        self.previewing = threading.Event()
        self.ingest_gate: threading.Event | None = None
        self.read_gate: threading.Event | None = None
        self.preview_gates: dict[str, threading.Event] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def add_files(self, slug, paths, *, on_progress=None):
        self.ingest_threads.append(threading.get_ident())
        self.ingesting.set()
        if self.ingest_gate is not None:
            self.ingest_gate.wait(5)

        def spy(line: str) -> None:
            self.progress.append(line)
            if on_progress is not None:
                on_progress(line)

        return self._inner.add_files(slug, paths, on_progress=spy)

    def preview(self, query, base_name=None):
        self.previews += 1
        self.preview_threads.append(threading.get_ident())
        self.previewing.set()
        gate = self.preview_gates.get(query)
        if gate is not None:
            gate.wait(5)
        return self._inner.preview(query, base_name)

    def doc_text(self, slug, doc_id):
        # Recorded BEFORE the gate: a test that wants to prove a second read never started has to be
        # able to see one that did, even while both are held.
        self.read_threads.append(threading.get_ident())
        if self.read_gate is not None:
            self.read_gate.wait(5)
        return self._inner.doc_text(slug, doc_id)


def _list_text(view: KnowledgeView) -> str:
    """All visible label text across whatever the content list is currently showing."""
    out: list[str] = []
    for i in range(view._list.count()):
        w = view._list.itemAt(i).widget()
        if w is not None:
            out.extend(lbl.text() for lbl in w.findChildren(QLabel))
            if isinstance(w, QLabel):
                out.append(w.text())
    return " ".join(out)


def test_launcher_has_a_vault_tab_that_lists_vaults(_app, tmp_path):
    builds, ks = _knowledge(tmp_path)
    ks.create("Recipes")
    ks.add_note("recipes", "Pie crust: butter, flour, a little salt.")
    view = LauncherView(builds, _Agents(), _Tasks(), ks)
    assert view._tabs[_KNOWLEDGE].text() == "Vault"
    view.refresh()
    # the vault shows in the Vault grid, and NOT in the Apps grid
    assert view._knowledge_grid.count() == 1
    assert view._apps_grid.count() == 0


def test_knowledge_view_add_search_and_remove(_app, tmp_path):
    _builds, ks = _knowledge(tmp_path)
    base = ks.create("Notes")
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")
    assert _doc_rows(view) == 0  # empty base → no rows

    # add a note through the UI
    view._note.setPlainText("The wifi password is hunter2.")
    view._add_note()
    assert ks.count(base.slug) == 1
    assert _doc_rows(view) == 1

    # searching swaps the list to ranked passages (no _DocRow rows while a query is active). The search
    # is debounced and runs on a worker, so the assertions come after the panel has settled.
    view._search.setText("wifi password")
    _settle(view, _app)
    assert _doc_rows(view) == 0
    assert "hunter2" in _list_text(view)  # the matching passage is shown

    # clearing the search restores the document list
    view._search.clear()
    assert _doc_rows(view) == 1

    # removing the doc through the service + reload empties the list
    doc = ks.docs(base.slug)[0]
    ks.remove_doc(base.slug, doc.id)
    view.reload()
    assert _doc_rows(view) == 0


def test_knowledge_view_reload_reflects_external_add(_app, tmp_path):
    _builds, ks = _knowledge(tmp_path)
    base = ks.create("Work")
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Work")
    # the orb saving a note elsewhere → main window calls reload()
    ks.add_note(base.slug, "Standup moved to 9:30.")
    view.reload()
    assert _doc_rows(view) == 1


# ───────────────── nothing on this panel blocks the window ─────────────────
def test_adding_files_to_a_vault_runs_off_the_gui_thread(_app, tmp_path):
    # Ingestion reaches doc_extract, which since OCR landed can hold ONE scanned PDF for 30 seconds and
    # a picked folder for forty of those, plus a git commit per stored document. Run where the click
    # arrives, that is a frozen, un-repainting window; the chat attachment path was moved off the UI
    # thread for exactly this reason and the Vault never was.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Docs")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Docs")
    f = tmp_path / "contract.txt"
    f.write_text("The lease renews in March.", encoding="utf-8")

    ks.ingest_gate = threading.Event()
    view._ingest(lambda k, slug, emit: k.add_files(slug, [f], on_progress=emit), source="files")
    assert ks.ingesting.wait(5)          # the ingest is genuinely in flight…
    assert view._busy                    # …and it is NOT the GUI thread sitting inside it
    assert not view._add_files_btn.isEnabled() and not view._add_folder_btn.isEnabled()
    ks.ingest_gate.set()
    _settle(view, _app)

    assert ks.ingest_threads and ks.ingest_threads[0] != threading.get_ident()
    assert view._add_files_btn.isEnabled() and not view._busy  # the panel is handed back
    assert _doc_rows(view) == 1
    assert any("contract.txt" in line for line in ks.progress)  # the wait was narrated, not silent
    assert not view._workers                                    # retired cleanly
    view.shutdown()


def test_files_dropped_onto_a_vault_are_read_off_the_gui_thread(_app, tmp_path):
    # The drop handler was the worst of the three ingest paths: a dropped folder of scans is up to forty
    # OCR passes, and it ran inside dropEvent — i.e. on the UI thread that delivered the drop.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Docs")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Docs")
    f = tmp_path / "invoice.txt"
    f.write_text("Invoice 41 is due on the 9th.", encoding="utf-8")

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(f))])
    event = QDropEvent(
        QPointF(4.0, 4.0), Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    ks.ingest_gate = threading.Event()
    view.dropEvent(event)
    assert ks.ingesting.wait(5)
    assert view._busy
    ks.ingest_gate.set()
    _settle(view, _app)

    assert ks.ingest_threads and ks.ingest_threads[0] != threading.get_ident()
    assert _doc_rows(view) == 1
    view.shutdown()


def test_a_second_drop_is_refused_while_an_ingest_is_still_running(_app, tmp_path):
    # Two ingests into one vault would queue behind each other invisibly (the service serializes index
    # writes), so the panel refuses the second rather than looking stuck twice over.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Docs")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Docs")
    f = tmp_path / "one.txt"
    f.write_text("first", encoding="utf-8")

    ks.ingest_gate = threading.Event()
    view._ingest(lambda k, slug, emit: k.add_files(slug, [f], on_progress=emit), source="files")
    assert ks.ingesting.wait(5)
    view._ingest(lambda k, slug, emit: k.add_files(slug, [f], on_progress=emit), source="files")
    ks.ingest_gate.set()
    _settle(view, _app)
    assert len(ks.ingest_threads) == 1
    view.shutdown()


def test_typing_in_the_vault_search_box_does_not_search_on_every_keystroke(_app, tmp_path):
    # One search ranks every chunk of every document and, with an embedder configured, makes a network
    # call with a 30s timeout. Wired straight to textChanged, a four-letter word was four of those.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "The wifi password is hunter2.")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    for i in range(1, len("wifi") + 1):
        view._search.setText("wifi"[:i])
    assert ks.previews == 0  # still quiet — nothing has searched yet
    _settle(view, _app)
    assert ks.previews == 1  # one search for the whole word
    assert "hunter2" in _list_text(view)
    view.shutdown()


def test_a_vault_search_runs_off_the_gui_thread(_app, tmp_path):
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "The wifi password is hunter2.")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    ks.preview_gates["wifi"] = threading.Event()
    view._search.setText("wifi")
    assert _spin(_app, lambda: ks.previewing.is_set())
    assert view._status.text() == "Searching…"   # the panel says so instead of freezing
    ks.preview_gates["wifi"].set()
    _settle(view, _app)
    assert ks.preview_threads and ks.preview_threads[0] != threading.get_ident()
    assert "hunter2" in _list_text(view)
    view.shutdown()


def test_a_slow_vault_search_never_paints_over_a_newer_query(_app, tmp_path):
    # Searches overlap, so the slow one has to recognise itself as stale. Without that guard the older
    # query's hits land last and replace what the user is actually looking at.
    #
    # Completion ORDER is the whole subject here, so nothing in this test races for it. BOTH previews
    # are parked on their own gate, and the older one is released only after the newer one has already
    # painted — an unguarded _show_hits is then guaranteed to overwrite, every run, instead of doing it
    # about three runs in five. The note bodies are also shaped so that the words spun on ("tunnels",
    # "warrens") appear only in a note's body and never in the TITLE the document list shows: a
    # predicate the plain document list already satisfies would wait for nothing at all.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "Field notes one\naardvark tunnels run deep underground")
    real.add_note(base.slug, "Field notes two\nbunny warrens stay near the surface")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")
    listed = _list_text(view)
    assert _doc_rows(view) == 2 and "tunnels" not in listed and "warrens" not in listed

    slow, quick = threading.Event(), threading.Event()
    ks.preview_gates["aardvark"] = slow
    ks.preview_gates["bunny"] = quick

    view._search.setText("aardvark")
    view._search_timer.stop()   # both searches are driven by hand; the debounce is pinned separately
    view._run_search()          # the older, slow query — now parked inside preview()
    assert _spin(_app, lambda: ks.previews == 1)

    view._search.setText("bunny")
    view._search_timer.stop()
    view._run_search()          # the newer query, parked too — neither can finish behind our back
    assert _spin(_app, lambda: ks.previews == 2)

    quick.set()                 # the NEWER query returns first and paints its hits…
    assert _spin(_app, lambda: _doc_rows(view) == 0 and "warrens" in _list_text(view))

    slow.set()                  # …and only now does the older one come back, strictly afterwards
    _settle(view, _app)

    shown = _list_text(view)
    assert "warrens" in shown and "tunnels" not in shown
    assert ks.previews == 2      # and no third search was conjured up along the way
    view.shutdown()


def test_stale_search_hits_are_dropped_without_touching_the_panel(_app, tmp_path):
    # The same guard as above, pinned with no threads at all: _show_hits is handed a sequence number
    # from a query that has already been superseded and must change nothing. This one cannot flake,
    # which matters because the guard it protects is one `if` that is easy to delete by accident.
    _builds, ks = _knowledge(tmp_path)
    base = ks.create("Notes")
    ks.add_note(base.slug, "Field notes one\naardvark tunnels run deep underground")
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    hits = ks.preview("aardvark", "Notes")
    assert hits and "tunnels" in hits[0].text  # the results really would be visible if they landed

    before_rows, before_count = _doc_rows(view), view._count.text()
    view._show_hits("aardvark", hits, view._search_seq - 1)   # a query the user has already moved off
    assert _doc_rows(view) == before_rows
    assert view._count.text() == before_count
    assert "tunnels" not in _list_text(view)

    view._show_hits("aardvark", hits, view._search_seq)       # the current one still paints normally
    assert _doc_rows(view) == 0 and "tunnels" in _list_text(view)
    view.shutdown()


# ───────────────────────────── the read pane ─────────────────────────────
def test_a_saved_document_can_be_opened_and_read_back(_app, tmp_path):
    # Before this the panel could only show a title, a byte count and a Remove button: the one thing a
    # user could not do with their own saved material was read it.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "The spare key is under the third flowerpot.")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    (row,) = _rows(view)
    assert row.body.isHidden() and not row.expanded  # a list at a glance until asked
    row.open_btn.click()
    _settle(view, _app)
    assert row.expanded
    assert "third flowerpot" in row.body.text()
    assert ks.read_threads and ks.read_threads[0] != threading.get_ident()  # read off the GUI thread

    row.open_btn.click()  # the same button closes it again
    assert not row.expanded and row.body.text() == ""
    view.shutdown()


def test_a_document_read_that_lands_after_the_list_was_rebuilt_is_dropped(_app, tmp_path):
    # The row's C++ object is gone once the list is rebuilt (a reload, a search, another vault), so a
    # read still in flight must land on nobody rather than reach into a deleted widget.
    _builds, ks = _knowledge(tmp_path)
    base = ks.create("Notes")
    ks.add_note(base.slug, "The spare key is under the third flowerpot.")
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    (row,) = _rows(view)
    row.open_btn.click()
    view._render_docs()       # the list is rebuilt while the read is in flight
    _settle(view, _app)
    assert not view._reading  # the stale row is no longer a legal target
    assert all(not r.expanded for r in _rows(view))

    # Observe the GUARD, not just its bookkeeping. Asserting only on `_reading` and on the CURRENT
    # rows passes with the membership check deleted, because _clear_list() alone satisfies both — the
    # old row is never looked at, so the one thing under test is never exercised. Hand _show_doc a row
    # the rebuild has retired and confirm nothing is painted into it.
    (fresh,) = _rows(view)
    view._reading.discard(fresh)
    view._show_doc(fresh, "The spare key is under the third flowerpot.")
    assert fresh.body.text() == "" and not fresh.expanded, (
        "a read that landed after its row was retired painted into it anyway"
    )
    view.shutdown()


def test_reopening_a_document_mid_read_does_not_start_a_second_read(_app, tmp_path):
    # Open, close, open again — three clicks on one row, faster than a big PDF comes back off disk. The
    # "already loading" guard used to be keyed off `expanded`, which show_text sets on the way in, so it
    # could never be true and the third click cheerfully started a SECOND doc_text worker on the same
    # file: twice the read for one pane, and whichever landed second overwrote the other for nothing.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "The spare key is under the third flowerpot.")
    ks = _Watched(real)
    ks.read_gate = threading.Event()   # every read parks until this test says otherwise
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    (row,) = _rows(view)
    row.open_btn.click()                                   # Open — the read is now parked in doc_text
    assert _spin(_app, lambda: len(ks.read_threads) == 1)
    row.open_btn.click()                                   # Close, while that read is still on the wire
    assert not row.expanded
    row.open_btn.click()                                   # Open again — must ride the read in flight
    assert row.expanded and row.body.text() == "Opening…"

    ks.read_gate.set()
    _settle(view, _app)
    assert len(ks.read_threads) == 1                       # one click-storm, one read of the file
    assert "third flowerpot" in row.body.text()            # and the re-armed row is still painted
    view.shutdown()


def test_an_ingest_that_fails_partway_refreshes_the_panel_and_claims_nothing_untrue(_app, tmp_path):
    # An ingest stores each document as it reads it, so a failure on file nine of forty leaves the first
    # eight saved. The panel used to answer that with "nothing was added" — untrue — and never reload,
    # so the list and the header count went on describing the vault as it was before the drop.
    _builds, ks = _knowledge(tmp_path)
    base = ks.create("Docs")
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Docs")
    f = tmp_path / "lease.txt"
    f.write_text("The lease renews in March.", encoding="utf-8")

    def job(k, slug, emit, _f=f):
        k.add_files(slug, [_f], on_progress=emit)   # this one genuinely lands…
        raise OSError("the drive went away")        # …and then the ingest dies partway through

    view._ingest(job, source="files")
    _settle(view, _app)

    assert ks.count(base.slug) == 1        # the service really did store one document
    assert _doc_rows(view) == 1            # …and the panel refreshed, so the user can see it
    assert "1 document" in view._count.text()
    said = view._status.text()
    assert "nothing was added" not in said                          # a promise the code cannot keep
    assert "went wrong" in said.lower()
    assert "OSError" not in said and "drive went away" not in said  # never the exception text
    view.shutdown()


def test_abandoning_a_vault_search_stops_the_panel_claiming_it_is_still_searching(_app, tmp_path):
    # A search can be a 30s network round-trip, so the panel says "Searching…" while one runs. It then
    # has to STOP saying it. Backspacing the query away supersedes the search: the document list comes
    # straight back, and when the abandoned result finally lands _show_hits recognises the stale
    # sequence number and returns — BEFORE the only line that clears the label. So "Searching…" sat
    # pinned under the box over a fully repainted list, reporting work that had already been given up
    # on, until some unrelated action happened to write there. The panel lying about its own state.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Notes")
    real.add_note(base.slug, "The wifi password is hunter2.")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Notes")

    gate = threading.Event()
    ks.preview_gates["wifi"] = gate
    view._search.setText("wifi")
    assert _spin(_app, lambda: ks.previewing.is_set())
    assert view._status.text() == "Searching…"      # parked mid-search: the claim is true right now

    view._search.clear()                            # …and the user backspaces it away
    assert _doc_rows(view) == 1                     # the document list is back instantly…
    assert view._status.text() == ""                # …and nothing claims to still be searching

    gate.set()                                      # the abandoned search finally returns
    _settle(view, _app)
    assert view._status.text() == ""                # and retires without reviving the message
    assert _doc_rows(view) == 1                     # having painted nothing over the list
    view.shutdown()


def test_a_finished_drop_still_says_what_it_added_while_a_search_is_open(_app, tmp_path):
    # The other half of the same shared label. reload() re-runs the query whenever the search box is
    # non-empty, and _after_ingest says what it added and THEN reloads — so with a live query the
    # confirmation lived for microseconds before "Searching…" replaced it and the results blanked it.
    # The user got no answer at all about the drop they had just made. The search may only write over a
    # line it owns.
    _builds, real = _knowledge(tmp_path)
    base = real.create("Docs")
    ks = _Watched(real)
    view = KnowledgeView(ks)
    view.open_base(base.slug, "Docs")
    f = tmp_path / "lease.txt"
    f.write_text("The lease renews in March.", encoding="utf-8")

    view._search.setText("lease")
    _settle(view, _app)
    assert ks.previews == 1 and view._status.text() == ""   # a finished search says nothing

    def job(k, slug, emit, _f=f):
        return k.add_files(slug, [_f], on_progress=emit)

    view._ingest(job, source="files")
    _settle(view, _app)

    assert ks.previews == 2                                  # the refresh really did re-run the query…
    assert view._status.text() == "Added 1 document from files."   # …behind a message that survived it
    view.shutdown()
