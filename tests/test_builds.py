"""BuildService — rename keeps name↔slug consistent; legacy manifests get an is_model classification."""
from __future__ import annotations

import json
from datetime import datetime

from helix.domain.models import App
from helix.services.builds import BuildService


def _legacy_build(tmp_path, slug: str, html: str) -> None:
    """Write a build with a pre-is_model manifest (no flag) + an index.html, like an old install."""
    ws = tmp_path / slug
    ws.mkdir()
    (ws / "index.html").write_text(html, encoding="utf-8")
    (ws / ".helixbuild.json").write_text(
        json.dumps({
            "slug": slug, "name": slug.replace("-", " ").title(), "request": "r",
            "kind": "html", "entry_point": "index.html", "created_at": "2026-06-25T12:00:00",
        }),
        encoding="utf-8",
    )


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


def test_is_model_round_trips_through_the_manifest(tmp_path):
    svc = _svc(tmp_path)
    app = App.from_request("Battery", "show how a battery works")
    app.is_model = True
    svc.create_workspace(app)
    (svc.workspace("battery") / "index.html").write_text("<html></html>", encoding="utf-8")
    svc.finalize(app)
    listed = {a.slug: a.is_model for a in svc.list()}
    assert listed == {"battery": True}
    # a renamed model stays a model
    out = svc.rename("battery", "Battery Cell")
    assert out is not None and out.is_model is True
    assert {a.slug: a.is_model for a in svc.list()} == {"battery-cell": True}


def test_legacy_manifests_are_classified_and_persisted_on_read(tmp_path):
    svc = _svc(tmp_path)
    _legacy_build(tmp_path, "old-model", '<script type="importmap">{"imports":'
                  '{"three":"x/three.module.js"}}</script>')
    _legacy_build(tmp_path, "old-app", "<h1>hello</h1>")
    listed = {a.slug: a.is_model for a in svc.list()}
    assert listed == {"old-model": True, "old-app": False}
    # the classification was written back, so it isn't re-detected next time
    data = json.loads((tmp_path / "old-model" / ".helixbuild.json").read_text(encoding="utf-8"))
    assert data["is_model"] is True


def test_rename_blank_or_missing_returns_none(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Thing")
    assert svc.rename("thing", "   ") is None
    assert svc.rename("does-not-exist", "Whatever") is None
