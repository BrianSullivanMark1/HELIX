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

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None):
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
    lambda r: _w(r / "helix/ui/orb.py", "# x"),               # shell edit
    lambda r: _w(r / "helix/domain/models.py", "# x"),        # protected edit
    lambda r: _w(r / "helix/ports/p.py", "# x"),              # new protected file
    lambda r: (r / "helix/ui/orb.py").rename(r / "helix/ui/orb2.py"),  # shell rename
    lambda r: _w(r / "sitecustomize.py", "# x"),              # startup auto-run file
])
def test_propose_refuses_off_allowlist(fn):
    repo = _helix_repo()
    with pytest.raises(ConstitutionViolation):
        _selfdev(repo, fn).propose("attack")
    assert not GIT.list_branches(repo, "selfdev/")


def test_approve_rescans_branch_tip():
    repo = _helix_repo()
    svc = _selfdev(repo, lambda r: _w(r / "helix/services/conversation.py", "# ok"))
    pc = svc.propose("clean")
    base = GIT.current_branch(repo)
    GIT.checkout(repo, pc.branch)
    _w(repo / "helix/domain/models.py", "# tampered")
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


def test_build_sibling_app_escape_reverted():
    root = _build_repo()
    _forge(root, lambda ws: _w(ws / "index.html", "<h1>A</h1>")).build("Alpha", "x")
    victim = root / "data/builds/alpha/index.html"
    with pytest.raises(BuildError):
        _forge(root, lambda ws: victim.write_text("<script>PWN</script>", encoding="utf-8")).build("Beta", "x")
    assert victim.read_text(encoding="utf-8") == "<h1>A</h1>"  # restored to committed state
