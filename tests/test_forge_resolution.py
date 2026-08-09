"""ForgeService build-name resolution — the 'update my garden' → the right build behaviour — and how a
build SETTLES: every exit clears the in-progress marker, and every BAD exit rolls the workspace back.

Exercises _resolve_prior directly (it only needs name/kind data, not the coder/repo/bus), locking the
fuzzy fallback that fixes 'HELIX can't find the model to edit': a paraphrase resolves to the build the
user means, but only when it's unambiguous and the same kind. The settle tests drive the real build()
loop with a fake coder and a fake repo (no git, no network).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from helix.domain.errors import BuildCancelled, BuildError
from helix.domain.events import BuildDeleted
from helix.domain.models import App, BuildKind
from helix.ports.coder import CoderResult
from helix.services.builds import BuildService
from helix.services.cancel import CancelToken
from helix.services.forge import ForgeService


def _forge() -> ForgeService:
    # _resolve_prior touches none of the wired collaborators, so None placeholders are fine here.
    return ForgeService(builds=None, coder=None, bus=None, repo=None, app_root=Path("."))


def _app(name: str, slug: str, kind: BuildKind) -> App:
    return App(slug=slug, name=name, request="", build_kind=kind)


def test_exact_slug_and_name_still_win():
    f = _forge()
    existing = [_app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL)]
    assert f._resolve_prior("garden-walkthrough", "garden-walkthrough", BuildKind.MODEL, existing).slug == (
        "garden-walkthrough"
    )
    assert f._resolve_prior("Garden Walkthrough", "x", BuildKind.MODEL, existing).slug == "garden-walkthrough"


def test_fuzzy_resolves_a_paraphrase_to_the_only_same_kind_build():
    f = _forge()
    existing = [
        _app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL),
        _app("Tip Calculator", "tip-calculator", BuildKind.APP),
    ]
    # "garden", "my garden model" — all the obvious ways a user refers to it — find the one model.
    for said in ("garden", "my garden model", "the garden"):
        prior = f._resolve_prior(said, "garden", BuildKind.MODEL, existing)
        assert prior is not None and prior.slug == "garden-walkthrough", said


def test_fuzzy_is_kind_scoped():
    f = _forge()
    existing = [_app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL)]
    # Asking to build an APP called "garden" must NOT hijack the MODEL — it makes a new app.
    assert f._resolve_prior("garden", "garden", BuildKind.APP, existing) is None


def test_fuzzy_ambiguity_makes_a_new_build():
    f = _forge()
    existing = [
        _app("Garden Walkthrough", "garden-walkthrough", BuildKind.MODEL),
        _app("Garden Pond", "garden-pond", BuildKind.MODEL),
    ]
    # Two models contain "garden" — don't guess; fall through to a new build (None).
    assert f._resolve_prior("garden", "garden", BuildKind.MODEL, existing) is None


# ----- how a build settles: the marker and the rollback -----
class _Repo:
    """Git stand-in: build() needs init/commit_all/hooks_dir, and the rollback verbs are recorded so a
    test can prove an iteration was returned to its last committed version."""

    def __init__(self) -> None:
        self.discarded: list[Path] = []
        self.restored: list[str] = []
        self._commits: dict[str, list[str]] = {}

    def init(self, _ws) -> None: ...

    def commit_all(self, ws, msg) -> None:
        self._commits.setdefault(str(ws), []).append(msg)

    def log(self, ws, limit: int = 100):
        return list(reversed(self._commits.get(str(ws), [])))[:limit]

    def hooks_dir(self, repo_dir):
        return Path(repo_dir) / ".git" / "hooks"

    def discard_changes(self, ws) -> None:
        self.discarded.append(Path(ws))

    def restore_paths(self, _repo_dir, paths) -> None:
        self.restored.extend(paths)


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 9, 12, 0, 0)


class _Coder:
    name = "fake"

    def __init__(self, fn) -> None:
        self._fn = fn  # fn(workspace, cancel) -> CoderResult

    def available(self) -> bool:
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
        return self._fn(Path(repo_dir), cancel)


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    def publish(self, e) -> None:
        self.events.append(e)


class _Rig:
    """A real BuildService over tmp_path with fakes at the git/coder/bus seams, plus one source file so
    the escape tripwire has something outside the workspace to notice."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "app"
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "app.py").write_text("# source", encoding="utf-8")
        self.repo = _Repo()
        self.bus = _Bus()
        self.builds = BuildService(self.root / "data" / "builds", self.repo, _Clock())

    def forge(self, fn) -> ForgeService:
        return ForgeService(self.builds, _Coder(fn), self.bus, self.repo, self.root)

    def good(self, name: str) -> App:
        return self.forge(_writes_a_page).build(name, "v1")


def _writes_a_page(ws: Path, _cancel) -> CoderResult:
    ws.joinpath("index.html").write_text("<h1>v1 works</h1>", encoding="utf-8")
    return CoderResult(ok=True, summary="built")


def _stops(ws: Path, cancel) -> CoderResult:
    ws.joinpath("index.html").write_text("<h1>half</h1>", encoding="utf-8")
    if cancel is not None:
        cancel.cancel()  # the user pressed 'stop' while the coder was working
    return CoderResult(ok=False, summary="", error="cancelled")


def _fails(ws: Path, _cancel) -> CoderResult:
    ws.joinpath("index.html").write_text("<h1>half</h1>", encoding="utf-8")
    return CoderResult(ok=False, summary="", error="boom")


def test_a_stopped_build_keeps_its_marker_until_the_user_answers(tmp_path):
    # A stop does NOT settle a build: the user still has to answer "remove it or keep it?". The
    # .building marker has to survive until that answer, because the routine way a build gets cancelled
    # is CLOSING HELIX — and BuildQueue.shutdown deliberately skips the cleanup offer (there is no UI
    # left to answer it). Clearing the marker on cancel therefore stranded the common case: recovery
    # skipped the build on the next launch and a half-edited app stayed live in the menu, looking ready.
    rig = _Rig(tmp_path)
    token = CancelToken()
    with pytest.raises(BuildCancelled):
        rig.forge(_stops).build("Scratch", "do it", cancel=token)
    assert rig.builds.is_building("scratch"), "an unanswered stop must stay recoverable"


def test_answering_keep_settles_the_build_so_recovery_leaves_it_alone(tmp_path):
    # The user stops a build and then DECLINES the cleanup offer — the work is theirs to keep. THAT is
    # what clears the marker; otherwise the next launch's recovery deletes it behind their back.
    rig = _Rig(tmp_path)
    forge = rig.forge(_stops)
    token = CancelToken()
    with pytest.raises(BuildCancelled):
        forge.build("Scratch", "do it", cancel=token)
    ws = rig.builds.workspace("scratch")
    forge.keep_build(token.build)  # ← the user clicked "Keep it"
    assert not rig.builds.is_building("scratch")
    forge.recover_interrupted()  # the next launch
    assert ws.exists() and (ws / "index.html").exists(), "recovery destroyed work the user kept"


def test_an_unanswered_stopped_new_build_is_recovered_on_the_next_launch(tmp_path):
    # The app-close path: cancelled, never answered. Recovery MUST act — a never-finalized build's
    # scaffold is half-written, so it is removed rather than left in the menu.
    rig = _Rig(tmp_path)
    with pytest.raises(BuildCancelled):
        rig.forge(_stops).build("Scratch", "do it", cancel=CancelToken())
    ws = rig.builds.workspace("scratch")
    assert ws.exists()
    rig.forge(_writes_a_page).recover_interrupted()
    assert not ws.exists(), "a half-built, unanswered build must not survive into the menu"


def test_an_unanswered_stopped_iteration_rolls_back_on_the_next_launch(tmp_path):
    rig = _Rig(tmp_path)
    app = rig.good("Keeper")
    ws = rig.builds.workspace(app.slug)
    with pytest.raises(BuildCancelled) as exc:
        rig.forge(_stops).build("Keeper", "change it", cancel=CancelToken())
    assert exc.value.iterating is True
    assert rig.builds.is_building(app.slug)
    assert rig.repo.discarded == []  # a stop must not roll back — the user gets the choice first
    rig.forge(_writes_a_page).recover_interrupted()
    assert rig.repo.discarded == [ws]  # …but an unanswered one goes back to its last good version


def test_keeping_a_stopped_iteration_survives_the_next_launch(tmp_path):
    rig = _Rig(tmp_path)
    app = rig.good("Keeper")
    ws = rig.builds.workspace(app.slug)
    forge = rig.forge(_stops)
    token = CancelToken()
    with pytest.raises(BuildCancelled):
        forge.build("Keeper", "change it", cancel=token)
    forge.keep_build(token.build)
    assert not rig.builds.is_building(app.slug)
    rig.forge(_writes_a_page).recover_interrupted()
    assert rig.repo.discarded == []  # the edit the user kept is never rolled back
    assert ws.exists()


def test_coder_failure_removes_a_never_finalized_new_build(tmp_path):
    rig = _Rig(tmp_path)
    with pytest.raises(BuildError, match="boom"):
        rig.forge(_fails).build("Doomed", "x")
    # The half-written scaffold must not linger in the menu as if it were ready.
    assert not rig.builds.workspace("doomed").exists()
    assert any(isinstance(e, BuildDeleted) for e in rig.bus.events)


def test_coder_failure_rolls_an_iteration_back_to_its_last_good_version(tmp_path):
    rig = _Rig(tmp_path)
    app = rig.good("My App")
    ws = rig.builds.workspace(app.slug)
    with pytest.raises(BuildError, match="boom"):
        rig.forge(_fails).build("My App", "add a thing")
    assert rig.repo.discarded == [ws]  # back to the committed, working version
    assert not rig.builds.is_building(app.slug)  # settled — recovery leaves it alone


def test_a_sandbox_escape_rolls_the_workspace_back_too(tmp_path):
    # Reverting only the escaped file left the rest of a build we just refused to trust live and
    # openable — while the error message claimed "I blocked it and rolled it back".
    rig = _Rig(tmp_path)

    def escaping(ws: Path, _cancel) -> CoderResult:
        ws.joinpath("index.html").write_text("<h1>half</h1>", encoding="utf-8")
        rig.root.joinpath("src", "app.py").write_text("PWN", encoding="utf-8")
        return CoderResult(ok=True, summary="built")

    with pytest.raises(BuildError, match="outside its own folder"):
        rig.forge(escaping).build("Sneaky", "x")
    assert rig.repo.restored == ["src/app.py"]  # the escape itself is still reverted
    assert not rig.builds.workspace("sneaky").exists()  # and so is the build that attempted it


def test_a_sandbox_escape_during_an_iteration_restores_the_last_good_version(tmp_path):
    rig = _Rig(tmp_path)
    app = rig.good("Keeper")

    def escaping(ws: Path, _cancel) -> CoderResult:
        ws.joinpath("index.html").write_text("<h1>tampered</h1>", encoding="utf-8")
        rig.root.joinpath("src", "app.py").write_text("PWN", encoding="utf-8")
        return CoderResult(ok=True, summary="built")

    with pytest.raises(BuildError, match="outside its own folder"):
        rig.forge(escaping).build("Keeper", "change it")
    assert rig.repo.discarded == [rig.builds.workspace(app.slug)]
    assert not rig.builds.is_building(app.slug)
