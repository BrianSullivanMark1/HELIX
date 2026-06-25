"""BuildService.rename — keeps name↔slug consistent, moves the folder, refuses collisions."""
from __future__ import annotations

from datetime import datetime

from helix.domain.models import App
from helix.services.builds import BuildService


class _NoRepo:
    """A git stand-in: create_workspace/finalize only need init + commit_all to be no-ops."""

    def init(self, _ws) -> None: ...
    def commit_all(self, _ws, _msg) -> None: ...


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 6, 25, 12, 0, 0)


def _svc(tmp_path) -> BuildService:
    return BuildService(tmp_path, _NoRepo(), _FixedClock())


def _make(svc: BuildService, name: str, request: str = "x") -> App:
    app = App.from_request(name, request)
    svc.create_workspace(app)
    (svc.workspace(app.slug) / "index.html").write_text("<html></html>", encoding="utf-8")
    return svc.finalize(app)


def test_rename_moves_folder_and_updates_manifest(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Tip Calc")
    out = svc.rename("tip-calc", "Gratuity Helper")
    assert out is not None and out.slug == "gratuity-helper" and out.name == "Gratuity Helper"
    assert not svc.workspace("tip-calc").exists()
    assert svc.exists("gratuity-helper")
    # the moved workspace keeps its files
    assert (svc.workspace("gratuity-helper") / "index.html").exists()
    assert {a.slug: a.name for a in svc.list()} == {"gratuity-helper": "Gratuity Helper"}


def test_rename_refuses_collision_and_leaves_original(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Alpha")
    _make(svc, "Beta")
    assert svc.rename("beta", "Alpha") is None  # would collide with the existing alpha
    assert svc.workspace("beta").exists() and svc.workspace("alpha").exists()
    assert {a.name for a in svc.list()} == {"Alpha", "Beta"}


def test_rename_refuses_when_target_folder_exists_without_manifest(tmp_path):
    # A stray (manifest-less) folder occupying the target slug must still block the move — guarding
    # on directory existence, not just exists()/manifest. Otherwise os.rename dead-ends.
    svc = _svc(tmp_path)
    _make(svc, "Source")
    (tmp_path / "dest").mkdir()  # slugify("Dest") -> "dest"
    assert svc.rename("source", "Dest") is None
    assert svc.workspace("source").exists()  # original left untouched


def test_rename_same_slug_just_updates_name(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Notes")
    out = svc.rename("notes", "Notes!")  # slugifies back to "notes" — no move
    assert out is not None and out.slug == "notes" and out.name == "Notes!"
    assert svc.workspace("notes").exists()


def test_rename_blank_or_missing_returns_none(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Thing")
    assert svc.rename("thing", "   ") is None
    assert svc.rename("does-not-exist", "Whatever") is None
