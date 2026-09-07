"""BuildService — rename keeps name↔slug consistent; legacy manifests get an is_model classification."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from helix.domain.errors import BuildError
from helix.domain.models import App, BuildKind
from helix.services.builds import BuildService
from helix.services.forge import ForgeService


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
    app.build_kind = BuildKind.MODEL
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


def _make_kind(svc: BuildService, name: str, kind: BuildKind, entry: str) -> App:
    app = App.from_request(name, "x")
    app.build_kind = kind
    svc.create_workspace(app)
    (svc.workspace(app.slug) / entry).write_text("x", encoding="utf-8")
    return svc.finalize(app)


def test_categorized_partitions_by_build_kind(tmp_path):
    svc = _svc(tmp_path)
    _make_kind(svc, "Tip Calc", BuildKind.APP, "index.html")
    _make_kind(svc, "Battery", BuildKind.MODEL, "index.html")
    _make_kind(svc, "Cleanup", BuildKind.TASK, "main.py")
    cat = svc.categorized()
    assert {a.slug for a in cat["apps"]} == {"tip-calc"}
    assert {a.slug for a in cat["models"]} == {"battery"}
    assert {a.slug for a in cat["tasks"]} == {"cleanup"}


def test_forge_refuses_a_kind_conflict_on_a_taken_name(tmp_path):
    # A model named "Battery" exists; asking to build a TASK of the same name must refuse (not silently
    # flip the model into a task and overwrite its workspace). The conflict is caught before any coder runs.
    builds = BuildService(tmp_path, _NoRepo(), _FixedClock())
    model = App.from_request("Battery", "show how a battery works")
    model.build_kind = BuildKind.MODEL
    builds.create_workspace(model)
    (builds.workspace("battery") / "index.html").write_text("<html></html>", encoding="utf-8")
    builds.finalize(model)

    forge = ForgeService(builds, coder=None, bus=None, repo=_NoRepo(), app_root=tmp_path)
    with pytest.raises(BuildError):
        forge.build("Battery", "now make it a task", kind=BuildKind.TASK)
    # the original model is untouched
    assert builds.categorized()["models"][0].slug == "battery"


class _RecRepo(_NoRepo):
    def __init__(self):
        self.discarded = []
        self._commits = {}  # ws -> list of commit messages (so log() reflects scaffold vs finalized)

    def commit_all(self, ws, msg):
        self._commits.setdefault(str(ws), []).append(msg)

    def discard_changes(self, ws):
        self.discarded.append(ws)

    def log(self, ws, limit=100):
        return list(reversed(self._commits.get(str(ws), [])))[:limit]


class _Bus:
    def __init__(self):
        self.events = []

    def publish(self, e):
        self.events.append(e)


def test_delete_removes_the_workspace(tmp_path):
    svc = _svc(tmp_path)
    _make(svc, "Gone")
    assert svc.exists("gone")
    assert svc.delete("gone") is True
    assert not svc.workspace("gone").exists()
    assert svc.delete("gone") is False  # already gone → honest False


def test_building_marker_is_set_then_cleared_by_finalize(tmp_path):
    svc = _svc(tmp_path)
    app = App.from_request("WIP", "x")
    svc.create_workspace(app)
    svc.mark_building(app.slug)
    assert svc.is_building("wip")
    (svc.workspace("wip") / "index.html").write_text("<html></html>", encoding="utf-8")
    svc.finalize(app)
    assert not svc.is_building("wip")  # a completed build clears the in-progress marker


def test_create_workspace_clears_a_manifestless_remnant(tmp_path):
    svc = _svc(tmp_path)
    ws = svc.workspace("tip-calc")
    ws.mkdir(parents=True)
    (ws / "garbage.txt").write_text("half-written", encoding="utf-8")  # a remnant, no manifest
    svc.create_workspace(App.from_request("Tip Calc", "x"))
    assert not (ws / "garbage.txt").exists()  # cleared before scaffolding a fresh build
    assert svc.exists("tip-calc")


def test_recover_interrupted_removes_a_never_finalized_build(tmp_path):
    repo = _RecRepo()
    builds = BuildService(tmp_path, repo, _FixedClock())
    app = App.from_request("Half Built", "x")
    builds.create_workspace(app)  # manifest written, entry_point None (never finalized)
    builds.mark_building(app.slug)
    forge = ForgeService(builds, coder=None, bus=_Bus(), repo=repo, app_root=tmp_path)
    forge.recover_interrupted()
    assert not builds.workspace("half-built").exists()  # interrupted brand-new build is removed


def test_recover_interrupted_rolls_back_an_interrupted_iteration(tmp_path):
    repo = _RecRepo()
    builds = BuildService(tmp_path, repo, _FixedClock())
    _make(builds, "Done")  # finalized once → has a 'build:' commit
    builds.mark_building("done")  # then an iteration started and was interrupted
    forge = ForgeService(builds, coder=None, bus=_Bus(), repo=repo, app_root=tmp_path)
    forge.recover_interrupted()
    assert repo.discarded == [builds.workspace("done")]  # rolled back to last good
    assert builds.exists("done")  # the build survives — it is NOT deleted
    assert not builds.is_building("done")  # the marker is cleared so recovery doesn't re-fire forever


def test_recover_keeps_a_finalized_build_even_with_no_detected_entry_point(tmp_path):
    # REGRESSION: a real, finalized build can legitimately have entry_point=None (the entry heuristic only
    # globs the top level). Recovery must NOT delete it on an interrupted iteration — it must roll back.
    repo = _RecRepo()
    builds = BuildService(tmp_path, repo, _FixedClock())
    app = App.from_request("Nested", "x")
    builds.create_workspace(app)
    sub = builds.workspace(app.slug) / "src"
    sub.mkdir()
    (sub / "index.html").write_text("<html></html>", encoding="utf-8")  # entry is nested, not top level
    builds.finalize(app)
    assert builds.list()[0].entry_point is None  # heuristic found no top-level entry — but it's committed
    builds.mark_building("nested")  # an iteration started and was interrupted
    forge = ForgeService(builds, coder=None, bus=_Bus(), repo=repo, app_root=tmp_path)
    forge.recover_interrupted()
    assert builds.workspace("nested").exists()  # NOT deleted — the good committed version survives
    assert repo.discarded == [builds.workspace("nested")]  # rolled back instead


def test_legacy_python_build_categorizes_as_a_task(tmp_path):
    # A pre-BuildKind manifest with a python entry must derive BuildKind.TASK on read (back-compat),
    # so old python builds keep showing under Tasks.
    ws = tmp_path / "old-script"
    ws.mkdir()
    (ws / "main.py").write_text("print('hi')", encoding="utf-8")
    (ws / ".helixbuild.json").write_text(
        json.dumps({"slug": "old-script", "name": "Old Script", "request": "r",
                    "kind": "python", "entry_point": "main.py", "created_at": "2026-06-25T12:00:00"}),
        encoding="utf-8",
    )
    svc = _svc(tmp_path)
    assert {a.slug for a in svc.categorized()["tasks"]} == {"old-script"}
