"""Knowledge UI — the Menu's Vault tab renders vaults, and the KnowledgeView manages one vault
(add a note, search, list/remove docs) without a webview."""
from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")

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

    # searching swaps the list to ranked passages (no _DocRow rows while a query is active)
    view._search.setText("wifi password")
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
