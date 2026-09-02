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

from helix.domain import cadpy as scad
from helix.domain.errors import BuildCancelled, BuildError
from helix.domain.events import BuildDeleted
from helix.domain.models import App, BuildKind
from helix.ports.cad import CadResult
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


# ----- holograms: the MODEL check is the baker's -----
# A hologram is a PROGRAM (model.py) the baker lints, compiles through the CadEngine, renders and
# critiques. The Forge's only job is to ASK the baker in its pre-finalize gate and to carry whatever the
# baker says into the existing one-pass repair loop. These pin that seam with a fake baker and a coder
# that records the prompts it was handed — no engine, no model, no git.

class _Baker:
    """Records the Forge's calls; `verdicts` are what check() answers, in order (None = passes). `log`
    is an optional shared list every call (and a cooperating coder) appends to, so a test can read the
    ORDER of the cycle — prepare before the coder, check after it, bake last."""

    def __init__(self, verdicts=(None,), log: list | None = None) -> None:
        self._verdicts = list(verdicts)
        self._log = log if log is not None else []
        self.prepared: list[Path] = []
        self.checked: list[Path] = []
        self.baked: list[Path] = []

    def prepare(self, workspace):
        self.prepared.append(Path(workspace))
        self._log.append("prepare")

    def check(self, workspace):
        self.checked.append(Path(workspace))
        self._log.append("check")
        return self._verdicts.pop(0) if self._verdicts else None

    def bake(self, workspace):
        self.baked.append(Path(workspace))
        self._log.append("bake")

    def engine_missing(self) -> bool:
        return False


# The smallest design that passes the baker's static lints (brief, params block, build()).
_TINY_PY = (
    '"""Design: Bracket - a tiny test part."""\n'
    "from helix_parts import *\n\n"
    "# --- Parameters ---\n"
    "w = 80.0  # [40..160] width, mm\n"
    "# --- End Parameters ---\n\n\n"
    "def build():\n"
    "    return Box(w, 10, 5)\n"
)


class _PromptCoder(_Coder):
    """A coder that writes model.py and remembers every prompt, so a test can read the repair pass.
    `log` (shared with a _Baker) records when it ran; `listings` is what the workspace held at that
    moment — what a coder that looks around before writing would see."""

    def __init__(self, log: list | None = None) -> None:
        self.prompts: list[str] = []
        self.listings: list[list[str]] = []
        self._log = log if log is not None else []

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
        self.prompts.append(prompt)
        self._log.append("coder")
        self.listings.append(sorted(p.name for p in Path(repo_dir).iterdir()))
        Path(repo_dir).joinpath("model.py").write_text(_TINY_PY, encoding="utf-8")
        return CoderResult(ok=True, summary="drafted")


def _model_forge(rig: _Rig, coder, baker) -> ForgeService:
    return ForgeService(rig.builds, coder, rig.bus, rig.repo, rig.root, model_baker=baker)


def test_a_model_check_is_delegated_to_the_baker_and_a_pass_bakes(tmp_path):
    # The Forge asks the baker (lint + compile + critique live there), and only on a pass does it bake.
    rig = _Rig(tmp_path)
    coder, baker = _PromptCoder(), _Baker(verdicts=(None,))
    app = _model_forge(rig, coder, baker).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    ws = rig.builds.workspace(app.slug)
    assert baker.checked == [ws], "the MODEL branch of the pre-finalize gate must ask baker.check(ws)"
    assert baker.baked == [ws], "a passing check must still be followed by bake()"
    assert len(coder.prompts) == 1, "a passing hologram must not get a repair pass"


def test_the_bakers_problem_reaches_the_repair_pass_verbatim(tmp_path):
    # A compile error (the warm sentence + the compiler's file:line words) comes back from check() and
    # must land in the coder's repair prompt unchanged — that text is what lets the coder fix line 12
    # instead of guessing. The second check passes, so the build finishes and bakes.
    rig = _Rig(tmp_path)
    problem = ("The hologram's source couldn't be compiled. The engine said: NameError in "
               "model.py, line 12: name 'wdth' is not defined")
    coder, baker = _PromptCoder(), _Baker(verdicts=(problem, None))
    app = _model_forge(rig, coder, baker).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    ws = rig.builds.workspace(app.slug)
    assert len(coder.prompts) == 2, "a failed check must trigger exactly one repair pass"
    assert problem in coder.prompts[1], "the baker's problem text must reach the repair prompt"
    assert baker.checked == [ws, ws], "the repaired work is checked again"
    assert baker.baked == [ws], "bake() runs once, after the repaired check passes"


def test_the_critics_verdict_reaches_the_repair_pass_with_its_preview_pointer(tmp_path):
    # The critic's problem names the rendered picture; carrying it through intact is what lets the
    # repair prompt tell the coder to LOOK at assets/preview.png before editing model.scad.
    rig = _Rig(tmp_path)
    verdict = ("Looking at the rendered preview (assets/preview.png): the second mounting hole does "
               "not go through the plate. Fix the model so it matches the brief.")
    coder, baker = _PromptCoder(), _Baker(verdicts=(verdict, None))
    _model_forge(rig, coder, baker).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert len(coder.prompts) == 2
    assert verdict in coder.prompts[1]
    assert "assets/preview.png" in coder.prompts[1]


def test_a_hologram_that_fails_both_checks_is_rolled_back_and_never_baked(tmp_path):
    # Both passes failed: the broken design must not be baked into a viewer or left in the menu.
    rig = _Rig(tmp_path)
    coder = _PromptCoder()
    baker = _Baker(verdicts=("no model.py was produced", "no model.py was produced"))
    with pytest.raises(BuildError, match="no model.py was produced"):
        _model_forge(rig, coder, baker).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert baker.baked == [], "a design that failed its checks must not be baked"
    assert not rig.builds.workspace("bracket").exists()


def test_without_a_baker_a_model_scad_alone_is_a_finished_hologram(tmp_path):
    # The no-baker fallback (a bare Forge): model.scad IS the deliverable. The old gate demanded
    # model.json or index.html and would have failed every design the new prompt produces.
    rig = _Rig(tmp_path)

    def writes_scad(ws: Path, _cancel) -> CoderResult:
        ws.joinpath("model.scad").write_text("cube(10);\n", encoding="utf-8")
        return CoderResult(ok=True, summary="drafted")

    app = rig.forge(writes_scad).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert (rig.builds.workspace(app.slug) / "model.scad").exists()


def test_without_a_baker_an_empty_model_workspace_fails_its_check(tmp_path):
    rig = _Rig(tmp_path)

    def writes_nothing(ws: Path, _cancel) -> CoderResult:
        return CoderResult(ok=True, summary="thought about it")

    with pytest.raises(BuildError, match="no model.py was produced"):
        rig.forge(writes_nothing).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)


# ----- the bake cycle is the Forge's: prepare() before the coder, check() after, bake() last -----

def test_prepare_opens_the_cycle_before_the_coder_runs_on_new_and_iterating_holograms(tmp_path):
    # prepare() seeds helix_parts.py and resets the critic's one look; both only help if they happen BEFORE
    # the coder goes looking and before the first check counts. It runs for a brand-new hologram AND for
    # an iteration of it (idempotent), and never for a plain app.
    rig = _Rig(tmp_path)
    log: list[str] = []
    coder, baker = _PromptCoder(log), _Baker(verdicts=(None, None), log=log)
    forge = _model_forge(rig, coder, baker)
    app = forge.build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert log == ["prepare", "coder", "check", "bake"]
    forge.build("Bracket", "make it wider", kind=BuildKind.MODEL)
    assert log == ["prepare", "coder", "check", "bake"] * 2
    ws = rig.builds.workspace(app.slug)
    assert baker.prepared == [ws, ws]
    # an app build never touches the baker
    _model_forge(rig, _Coder(_writes_a_page), baker).build("Notes", "a notes app", kind=BuildKind.APP)
    assert baker.prepared == [ws, ws] and log == ["prepare", "coder", "check", "bake"] * 2


def test_prepare_seeds_helix_parts_so_the_coder_finds_the_library_before_writing(tmp_path):
    # The prompt tells the coder helix_parts is the ONLY library here. With the real baker, a coder that
    # lists the fresh workspace must SEE it — it used to be written first by check(), after the coder had
    # already looked and found only the README and the manifest. No engine is needed for this: a missing
    # engine reads as a pass at check() and an install page at bake().
    from helix.services.model_baker import ModelBaker

    rig = _Rig(tmp_path)
    coder = _PromptCoder()
    app = _model_forge(rig, coder, ModelBaker(None)).build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert coder.listings and scad.HELIX_LIB_FILE in coder.listings[0], coder.listings
    ws = rig.builds.workspace(app.slug)
    assert (ws / scad.HELIX_LIB_FILE).read_text(encoding="utf-8") == scad.HELIX_LIB


class _Cad:
    """The smallest CadEngine: every compile and render succeeds with a stand-in file, so the real
    ModelBaker's cycle (critic included) can run under the real Forge loop with no real engine."""

    def available(self) -> bool:
        return True

    def version(self):
        return "2021.01"

    def compile_stl(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"solid x\nendsolid x\n")
        return CadResult(True, out, None, None, 0.1)

    def export_3mf(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        return CadResult(False, None, "not in a test", None, 0.0)

    def render_png(self, source: Path, out: Path, *, size=(1280, 960), timeout_s: float = 120.0) -> CadResult:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x89PNG-FAKE")
        return CadResult(True, out, None, None, 0.1)

    def install(self, on_progress=None, timeout_s: float = 900.0) -> CadResult:
        return CadResult(False, None, "not in a test", None, 0.0)

    def install_hint(self) -> str:
        return "install it"


def test_a_repair_pass_that_dies_does_not_starve_the_next_build_of_its_critic(tmp_path):
    # Build one: the critic speaks on check one, then the REPAIR PASS dies (the coder gives up) — the
    # build is rolled back and bake() never runs to close the cycle. Build two of the same hologram must
    # still get its one look: prepare() opens a fresh cycle, so the critic speaks on ITS first check. Before
    # the Forge owned the cycle, the count stayed at 1 and every later build ran with first=False.
    from helix.services.model_baker import ModelBaker

    rig = _Rig(tmp_path)
    critic_calls: list[Path] = []

    def critic(png: Path, brief: str):
        critic_calls.append(png)
        return "the bracket is the wrong shape"

    baker = ModelBaker(_Cad(), critic=critic)
    prompts: list[str] = []
    calls = {"n": 0}

    def scripted(ws: Path, _cancel) -> CoderResult:
        calls["n"] += 1
        if calls["n"] == 2:  # build one's repair pass gives up mid-edit
            return CoderResult(ok=False, summary="", error="boom")
        ws.joinpath("model.py").write_text(_TINY_PY, encoding="utf-8")
        return CoderResult(ok=True, summary="drafted")

    class _RecordingCoder(_Coder):
        def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
            prompts.append(prompt)
            return self._fn(Path(repo_dir), cancel)

    forge = ForgeService(rig.builds, _RecordingCoder(scripted), rig.bus, rig.repo, rig.root, model_baker=baker)
    with pytest.raises(BuildError, match="boom"):
        forge.build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert len(critic_calls) == 1 and len(prompts) == 2
    forge.build("Bracket", "a wall bracket", kind=BuildKind.MODEL)
    assert len(critic_calls) == 2, "the second build's first check must get the critic's look"
    assert len(prompts) == 4 and "wrong shape" in prompts[3], "and its verdict drives THAT build's repair pass"
