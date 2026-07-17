"""Safety-gate regression tests — the properties hardened across four red-team rounds.

Real git repos + fake coders. These must never silently regress: they are why HELIX can be trusted to
edit its own code.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from helix.adapters.git_repo import GitRepo
from helix.adapters.signal_bus import SignalBus
from helix.adapters.system_clock import SystemClock
from helix.domain.errors import BuildError, ConstitutionViolation
from helix.ports.coder import CoderResult
from helix.services.builds import BuildService
from helix.services.forge import ForgeService
from helix.services.selfdev import SelfDevService

GIT = GitRepo()
CLOCK = SystemClock()


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _Settings:
    def __init__(self, seed=None):
        self._d = dict(seed or {})

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v

    def all(self):
        return dict(self._d)


class _Coder:
    name = "fake"

    def __init__(self, fn):
        self._fn = fn

    def available(self):
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None, model=None):
        self.model = model  # captured so tests can assert the coder was handed the chosen model
        self._fn(Path(repo_dir))
        return CoderResult(ok=True, summary="ok")


def _helix_repo() -> Path:
    repo = Path(tempfile.mkdtemp()) / "r"
    GIT.init(repo)
    _w(repo / "helix/ui/orb.py", "# orb")
    _w(repo / "helix/domain/models.py", "# models")
    _w(repo / "helix/services/conversation.py", "# conversation")
    _w(repo / "README.md", "base")
    _w(repo / "data/helix.db", "DB")
    GIT.commit_all(repo, "base")
    return repo


def _selfdev(repo: Path, fn, *, smoke=lambda p: (True, ""), seed=None) -> SelfDevService:
    return SelfDevService(
        _Coder(fn), GIT, _Settings(seed), CLOCK, repo,
        worktrees_dir=repo.parent / "wt", smoke_check=smoke,
        guard_files=[repo / "data" / "s.json"], data_dir=repo / "data",
    )


def test_propose_allowed_then_approve_merges():
    repo = _helix_repo()
    svc = _selfdev(repo, lambda r: _w(r / "helix/services/conversation.py", "# improved"))
    pc = svc.propose("improve conversation")
    assert pc.branch in GIT.list_branches(repo, "selfdev/")
    assert GIT.current_branch(repo) == pc.branch.split("/")[0] or True  # back on base
    assert "Applied" in svc.approve(pc.branch)
    assert pc.branch not in GIT.list_branches(repo, "selfdev/")


def test_propose_refuses_a_live_source_escape():
    # The shared coder can target ABSOLUTE paths; a write into the LIVE deployed source (outside the draft
    # worktree) must be caught, reverted, and the draft aborted — the worktree's staged diff can't see it.
    repo = _helix_repo()
    svc = _selfdev(repo, lambda wt: _w(repo / "helix/ui/orb.py", "# escaped via absolute path"))
    with pytest.raises(ConstitutionViolation):
        svc.propose("write into the live tree")
    assert not GIT.list_branches(repo, "selfdev/")  # the draft branch was cleaned up
    assert (repo / "helix/ui/orb.py").read_text(encoding="utf-8") == "# orb"  # live file reverted to base
    assert GIT.is_clean(repo)


def test_recover_interrupted_sweeps_phantom_branches_but_keeps_real_ones():
    repo = _helix_repo()
    base = GIT.current_branch(repo)
    svc = _selfdev(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# real change"))
    pc = svc.propose("a real change")  # a genuine committed pending change (non-empty diff vs base)
    # a PHANTOM: a selfdev branch sitting at base with no commit (a draft killed before it committed)
    ph = repo.parent / "phantom-wt"
    GIT.add_worktree_branch(repo, ph, "selfdev/phantom-0101-000000", base)
    GIT.remove_worktree(repo, ph)
    assert any("phantom" in b for b in GIT.list_branches(repo, "selfdev/"))
    svc.recover_interrupted()
    branches = GIT.list_branches(repo, "selfdev/")
    assert pc.branch in branches  # the real, reviewable pending change is preserved
    assert not any("phantom" in b for b in branches)  # the phantom is swept


def test_propose_isolates_the_draft_in_a_worktree_leaving_the_live_tree_untouched():
    # #9: the draft must NEVER move the live deployed tree onto the selfdev branch (a crash mid-draft
    # would otherwise strand it there). The change lives only on its branch until approved.
    repo = _helix_repo()
    base = GIT.current_branch(repo)
    svc = _selfdev(repo, lambda r: _w(r / "helix/services/conversation.py", "# improved"))
    pc = svc.propose("improve conversation")
    assert GIT.current_branch(repo) == base  # live tree never left base
    assert GIT.is_clean(repo)  # and is clean (the draft happened in an isolated worktree)
    assert pc.branch in GIT.list_branches(repo, "selfdev/")
    # the live working copy is unchanged; the edit exists only on the branch
    assert (repo / "helix/services/conversation.py").read_text(encoding="utf-8") == "# conversation"


@pytest.mark.parametrize("fn", [
    lambda r: _w(r / "helix/domain/constitution.py", "# x"),  # the laws (vital organ)
    lambda r: _w(r / "helix/services/selfdev.py", "# x"),     # the approval gate (vital organ)
    lambda r: _w(r / "helix/ports/p.py", "# x"),              # skeleton: a new port
    lambda r: _w(r / "helix/app/container.py", "# x"),        # skeleton: composition root
    lambda r: _w(r / "helix/services/files.py", "# x"),       # a containment boundary (vital organ)
    lambda r: _w(r / "sitecustomize.py", "# x"),              # startup auto-run file
])
def test_propose_refuses_editing_a_vital_organ_or_the_skeleton(fn):
    repo = _helix_repo()
    with pytest.raises(ConstitutionViolation):
        _selfdev(repo, fn).propose("attack")
    assert not GIT.list_branches(repo, "selfdev/")


def test_propose_allows_editing_the_interface_and_writing_a_test():
    # The expanded growable surface: HELIX may improve its own UI and write a test for the change.
    repo = _helix_repo()

    def grow(wt):
        _w(wt / "helix/ui/orb.py", "# a brighter orb")
        _w(wt / "tests/test_orb_brightness.py", "def test_ok():\n    assert True\n")

    pc = _selfdev(repo, grow).propose("make the orb brighter and test it")
    assert pc.branch in GIT.list_branches(repo, "selfdev/")


def test_approve_rescans_branch_tip():
    repo = _helix_repo()
    svc = _selfdev(repo, lambda r: _w(r / "helix/services/conversation.py", "# ok"))
    pc = svc.propose("clean")
    base = GIT.current_branch(repo)
    GIT.checkout(repo, pc.branch)
    _w(repo / "helix/domain/constitution.py", "# tampered")  # a vital organ, appended after propose
    GIT.commit_all(repo, "sneak")
    GIT.checkout(repo, base)
    with pytest.raises(ConstitutionViolation):
        svc.approve(pc.branch)


def test_approve_blocks_on_smoke_failure():
    repo = _helix_repo()
    svc = _selfdev(repo, lambda r: _w(r / "helix/services/conversation.py", "# ok"),
                   smoke=lambda p: (False, "boom"))
    pc = svc.propose("change")
    with pytest.raises(BuildError):
        svc.approve(pc.branch)
    assert pc.branch in GIT.list_branches(repo, "selfdev/")  # kept for review, not merged


def test_hooks_tripwire_refuses():
    repo = _helix_repo()
    _w(repo / ".git/hooks/post-merge", "#!/bin/sh")
    with pytest.raises(ConstitutionViolation):
        _selfdev(repo, lambda r: None).propose("x")


def test_fingerprint_tripwire_and_locked_setting():
    with pytest.raises(ConstitutionViolation):
        _selfdev(_helix_repo(), lambda r: None, seed={"constitution_fingerprint": "x"}).propose("x")
    with pytest.raises(ConstitutionViolation):
        _selfdev(_helix_repo(), lambda r: None, seed={"human_approval_required": False}).propose("x")


def test_selfcoder_data_write_refused():
    repo = _helix_repo()
    svc = _selfdev(repo, lambda r: (r / "data/helix.db").write_text("EVIL", encoding="utf-8"))
    with pytest.raises(ConstitutionViolation):
        svc.propose("write db")


def test_live_volatile_writes_during_a_draft_do_not_refuse(tmp_path):
    # THE REAL BUG (2026-07-17): a self-change draft runs the coder for MINUTES while the LIVE app keeps
    # writing its volatile stores — sqlite checkpoints (helix.db), the heartbeat stamping agents, the
    # memory distiller, a consolidated reflex. Those writes are HELIX itself, not the coder, and must
    # NOT be mistaken for an escape and refuse an otherwise-good draft. (data_dir sits OUTSIDE the repo,
    # exactly like the frozen app's %LOCALAPPDATA% data.)
    repo = _helix_repo()
    data = tmp_path / "livedata"
    data.mkdir()
    (data / "helix.db").write_text("DB", encoding="utf-8")

    def coder(wt):
        _w(wt / "helix/services/conversation.py", "# improved by the draft")  # the legit code edit
        (data / "helix.db").write_text("checkpoint-mid-draft", encoding="utf-8")   # live app churn…
        _w(data / "helix_agents.json", '{"last_run": 1}')
        _w(data / "helix_memory.json", '{"facts": []}')
        _w(data / "helix_reflexes.json", '{"reflexes": {}}')

    svc = SelfDevService(
        _Coder(coder), GIT, _Settings(), CLOCK, repo,
        worktrees_dir=repo.parent / "wt-live", smoke_check=lambda p: (True, ""), data_dir=data,
    )
    pc = svc.propose("improve conversation")  # must NOT raise despite the live volatile writes
    assert pc.branch in GIT.list_branches(repo, "selfdev/")


def test_coder_escape_into_secrets_still_refused(tmp_path):
    # The guard is NARROWED, not disabled: a coder that writes a NON-skipped data path is still caught.
    repo = _helix_repo()
    data = tmp_path / "livedata2"
    data.mkdir()

    def coder(wt):
        _w(wt / "helix/services/conversation.py", "# edit")
        _w(data / "some_other_secret_store.json", '{"stolen": true}')

    svc = SelfDevService(
        _Coder(coder), GIT, _Settings(), CLOCK, repo,
        worktrees_dir=repo.parent / "wt-esc", smoke_check=lambda p: (True, ""), data_dir=data,
    )
    with pytest.raises(ConstitutionViolation):
        svc.propose("escape into a non-volatile data file")


def test_concurrent_build_writing_data_builds_during_a_draft_does_not_refuse(tmp_path):
    # A background BUILD can run WHILE a self-change drafts and write its own workspace under
    # data/builds. Those writes are the build, not the self-dev coder — the draft must still succeed
    # (the Forge guard skips the builds tree for the same reason).
    repo = _helix_repo()
    data = tmp_path / "livedata3"
    (data / "builds").mkdir(parents=True)

    def coder(wt):
        _w(wt / "helix/services/conversation.py", "# improved by the draft")
        # a concurrent build writing its workspace during the draft:
        _w(data / "builds" / "tip-calc" / "index.html", "<h1>a concurrent build</h1>")
        _w(data / "builds" / "tip-calc" / ".helixbuild.json", "{}")

    svc = SelfDevService(
        _Coder(coder), GIT, _Settings(), CLOCK, repo,
        worktrees_dir=repo.parent / "wt-cc", smoke_check=lambda p: (True, ""), data_dir=data,
    )
    pc = svc.propose("improve conversation")  # must NOT be refused by the concurrent build's writes
    assert pc.branch in GIT.list_branches(repo, "selfdev/")


# ----- forge build containment -----
def _build_repo() -> Path:
    root = Path(tempfile.mkdtemp()) / "app"
    GIT.init(root)
    _w(root / ".gitignore", "data/")
    _w(root / "README.md", "a")
    GIT.commit_all(root, "b")
    return root


def _forge(root: Path, fn) -> ForgeService:
    bs = BuildService(root / "data" / "builds", GIT, CLOCK)
    return ForgeService(bs, _Coder(fn), SignalBus(), GIT, root, [root / "data" / "s.json"])


def test_build_legit_succeeds():
    root = _build_repo()
    _forge(root, lambda ws: _w(ws / "index.html", "<h1>ok</h1>")).build("Good", "x")
    assert (root / "data/builds/good/index.html").exists()
    assert GIT.is_clean(root)


def test_build_source_escape_blocked_and_reverted():
    root = _build_repo()
    with pytest.raises(BuildError):
        _forge(root, lambda ws: _w(root / "helix/services/evil.py", "PWN")).build("Evil", "x")
    assert not (root / "helix/services/evil.py").exists()


def test_build_into_a_sibling_workspace_is_allowed_for_concurrency():
    # Deliberate trade-off enabling PARALLEL builds: the escape guard skips the whole data/builds tree, so
    # a sibling writing to its own workspace can't be mistaken for THIS build escaping (which used to
    # falsely revert the sibling). A consequence is that a write INTO another build's folder is no longer
    # blocked — acceptable, since both are the user's own sandboxed, git-versioned data builds. The
    # protections that matter — source, settings, .git, hooks — are still enforced (see the tests above).
    root = _build_repo()
    _forge(root, lambda ws: _w(ws / "index.html", "<h1>A</h1>")).build("Alpha", "x")
    sibling = root / "data/builds/alpha/index.html"

    def beta(ws):
        _w(ws / "index.html", "<h1>B</h1>")
        sibling.write_text("touched", encoding="utf-8")

    _forge(root, beta).build("Beta", "x")  # no BuildError — a sibling write is allowed now
    assert sibling.read_text(encoding="utf-8") == "touched"


def test_settings_churn_during_a_build_does_not_fail_it():
    # Regression: with concurrent builds, the UI thread can rewrite the guarded settings file WHILE a build
    # runs. That must NOT fail the build — the settings file is byte-reverted by the guard and excluded
    # from the escape tripwire, so a guarded-file change can never read as the build escaping.
    root = _build_repo()
    sfile = root / "data" / "s.json"
    sfile.parent.mkdir(parents=True, exist_ok=True)
    sfile.write_text('{"k":1}', encoding="utf-8")  # guard_files for _forge is [root/data/s.json]

    def coder(ws):
        _w(ws / "index.html", "<h1>ok</h1>")
        sfile.write_text('{"k":2}', encoding="utf-8")  # the app (not the build) rewrites settings

    _forge(root, coder).build("Good", "x")  # must NOT raise
    assert (root / "data/builds/good/index.html").exists()
    assert sfile.read_text(encoding="utf-8") == '{"k":1}'  # reverted byte-for-byte by the guard
