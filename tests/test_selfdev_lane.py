"""SelfDevLane — drafts a self-change in the background, announces it, runs one at a time, cancels."""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

import pytest

from helix.adapters.git_repo import GitRepo
from helix.adapters.system_clock import SystemClock
from helix.domain.errors import BuildError, ConstitutionViolation
from helix.domain.events import (
    SelfChangeFinished,
    SelfChangeProgress,
    SleepRequest,
    SleepRequested,
)
from helix.domain.models import PendingChange
from helix.ports.coder import CoderResult
from helix.services import selfdev as selfdev_mod
from helix.services.selfdev import SelfDevService
from helix.services.selfdev_lane import SelfDevLane

GIT = GitRepo()


class _Bus:
    def __init__(self) -> None:
        self.events: list = []
        self._cond = threading.Condition()

    def publish(self, e) -> None:
        with self._cond:
            self.events.append(e)
            self._cond.notify_all()

    def subscribe(self, *a) -> None:
        pass

    def wait_for(self, pred, timeout=5.0) -> bool:
        with self._cond:
            return self._cond.wait_for(pred, timeout=timeout)

    def finished(self):
        return [e for e in self.events if isinstance(e, SelfChangeFinished)]


class _Selfdev:
    def __init__(self, fn) -> None:
        self._fn = fn

    def propose(self, request, *, on_progress=None, cancel=None, model=None):
        self.model = model  # the lane threads the chosen coder model through to propose
        return self._fn(request, on_progress, cancel)


def test_lane_runs_propose_in_background_and_announces_ready():
    bus = _Bus()

    def fn(req, on_progress, cancel):
        if on_progress:
            on_progress("drafting it")
        return PendingChange(id="selfdev/x", branch="selfdev/x", summary="did x", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    assert lane.start("change it")
    assert bus.wait_for(lambda: bool(bus.finished()))
    fin = bus.finished()[0]
    assert fin.ok and fin.summary == "did x"
    assert any(isinstance(e, SelfChangeProgress) for e in bus.events)


def test_lane_runs_one_draft_at_a_time():
    bus = _Bus()
    started, release = threading.Event(), threading.Event()

    def fn(req, on_progress, cancel):
        started.set()
        release.wait(5)
        return PendingChange(id="b", branch="b", summary="", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    assert lane.start("a")
    assert started.wait(5) and lane.busy()
    assert lane.start("b") is False  # rejected while one is in flight
    release.set()
    assert bus.wait_for(lambda: bool(bus.finished()))


def test_lane_cancel_reports_stopped():
    bus = _Bus()

    def fn(req, on_progress, cancel):
        for _ in range(500):
            if cancel.is_set():
                raise RuntimeError("cancelled")
            time.sleep(0.01)
        return PendingChange(id="b", branch="b", summary="", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    lane.start("a")
    for _ in range(200):
        if lane.busy():
            break
        time.sleep(0.01)
    lane.cancel()
    assert bus.wait_for(lambda: bool(bus.finished()))
    fin = bus.finished()[0]
    assert not fin.ok and fin.stopped


# ----- attended vs unattended: the same lane, at 3 PM and at 3 AM -----
def test_an_overnight_draft_marks_every_announcement_unattended():
    # Growth narration is deliberately spoken even through a sleeping mic, so the overnight dream
    # pass reusing this lane would read every coder step aloud into a dark house. The flag has to ride
    # on EVERY event: marked halfway, the night would whisper and then shout its ending at 4 AM.
    bus = _Bus()

    def fn(req, on_progress, cancel):
        on_progress("drafting it")
        return PendingChange(id="selfdev/x", branch="selfdev/x", summary="did x", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    assert lane.start("tidy the reminder repeat", unattended=True)
    assert bus.wait_for(lambda: bool(bus.finished()))
    assert len(bus.events) >= 2                       # a progress line AND the ending
    assert all(e.unattended for e in bus.events)


def test_an_overnight_draft_that_fails_ends_unattended_too():
    # The ending is the announcement a sleeping house would actually hear ("Couldn't draft that
    # change") — the failure path must carry the flag exactly as the success path does.
    bus = _Bus()

    def fn(req, on_progress, cancel):
        raise BuildError("the coder made no changes.")

    lane = SelfDevLane(_Selfdev(fn), bus)
    lane.start("tidy something", unattended=True)
    assert bus.wait_for(lambda: bool(bus.finished()))
    fin = bus.finished()[0]
    assert not fin.ok and fin.unattended


def test_a_draft_the_user_asked_for_stays_attended():
    # improve_helix is a change the user asked for and is sitting through: it must keep narrating.
    bus = _Bus()

    def fn(req, on_progress, cancel):
        on_progress("drafting it")
        return PendingChange(id="selfdev/x", branch="selfdev/x", summary="did x", request=req)

    lane = SelfDevLane(_Selfdev(fn), bus)
    lane.start("improve yourself")  # no unattended= at all — the default is what tools.py relies on
    assert bus.wait_for(lambda: bool(bus.finished()))
    assert not any(e.unattended for e in bus.events)


# ----- the gate the lane drives: reading a draft, and a merge that conflicts -----
# These pin SelfDevService itself. They live beside the lane's tests because the lane is what produces
# the pending changes both paths act on, and they use REAL git repos + a fake coder (as test_gate does)
# — a merge conflict is git behaviour, and a fake repo would prove nothing about it.
def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _GateSettings:
    def __init__(self):
        self._d = {}

    def get(self, k, default=None):
        return self._d.get(k, default)

    def set(self, k, v):
        self._d[k] = v


class _Coder:
    name = "fake"

    def __init__(self, fn):
        self._fn = fn

    def available(self):
        return True

    def run_task(self, repo_dir, prompt, *, on_progress=None, cancel=None, model=None):
        self._fn(Path(repo_dir))
        return CoderResult(ok=True, summary="ok")


def _helix_repo() -> Path:
    repo = Path(tempfile.mkdtemp()) / "r"
    GIT.init(repo)
    _w(repo / "helix/services/conversation.py", "# conversation")
    _w(repo / "README.md", "base")
    GIT.commit_all(repo, "base")
    return repo


def _gate(repo: Path, fn, git=None, settings=None) -> SelfDevService:
    return SelfDevService(
        _Coder(fn), git or GIT, settings or _GateSettings(), SystemClock(), repo,
        worktrees_dir=repo.parent / "wt", smoke_check=lambda p: (True, ""), data_dir=repo / "data",
    )


class _MergeThenBoom:
    """The real GitRepo, except merge_no_ff performs the merge and THEN raises.

    Not a contrived failure: merge_no_ff runs `git merge` and then `git log -1` to report the new
    commit, so a timeout or a killed subprocess on that second call raises AFTER git has already
    written the merge commit. That is the state the unwind has to survive, and no fake repo could
    prove anything about it — the merge commit and the moved HEAD have to be real.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def merge_no_ff(self, repo_dir, branch, message):
        self._inner.merge_no_ff(repo_dir, branch, message)
        raise RuntimeError("git timed out reading HEAD")


class _BlindMergeThenBoom:
    """The real GitRepo with `git log -1` broken: branch_head raises AND merge_no_ff commits then raises.

    Not two coincidences — branch_head and merge_no_ff's post-merge read are the SAME `git log -1`
    command, so one sick git produces both halves at once: the unwind gets no base sha to aim at, and
    the merge it must undo is already committed. That is precisely the state the no-sha branch of
    _unwind_failed_merge exists for, and it counts every discard_changes so the test can prove that
    branch never reaches for `git clean -fd`.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.discarded = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def branch_head(self, repo_dir, branch):
        raise RuntimeError("git log -1 failed")

    def merge_no_ff(self, repo_dir, branch, message):
        self._inner.merge_no_ff(repo_dir, branch, message)
        raise RuntimeError("git log -1 failed")

    def discard_changes(self, repo_dir):
        self.discarded += 1
        self._inner.discard_changes(repo_dir)


class _DeleteBranchBoom:
    """The real GitRepo, except deleting the merged branch fails — a stale index.lock, a ref git can't
    write. It happens AFTER the merge commit is in, so the change really is applied."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def delete_branch(self, repo_dir, name):
        raise RuntimeError("cannot lock ref")


def test_a_conflicting_merge_leaves_no_conflict_markers_in_helixs_live_source():
    # Two drafts against the same file: approving the first moves base, so the second conflicts. Git
    # would leave MERGE_HEAD set and `<<<<<<<` written into HELIX's OWN deployed source — poisoned
    # code at the next launch, and a dirty tree that refuses every later self-change.
    repo = _helix_repo()
    first = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# version A"))
    second = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# version B"))
    a = first.propose("make it A")
    b = second.propose("make it B")
    assert "Applied" in first.approve(a.branch)
    with pytest.raises(BuildError) as err:
        second.approve(b.branch)
    live = (repo / "helix/services/conversation.py").read_text(encoding="utf-8")
    assert "<<<<<<<" not in live and live == "# version A"  # exactly as it was before the attempt
    assert GIT.is_clean(repo)
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert b.branch in GIT.list_branches(repo, "selfdev/")  # nothing applied → still theirs to discard
    assert "no longer fits" in str(err.value)  # …and a reason, not a bare "couldn't apply it"


def test_self_editing_still_works_after_a_conflicting_merge_was_unwound():
    # The real damage of a half-merge is the dirty tree it leaves: propose() refuses to draft anything
    # ever again until a human runs git by hand.
    repo = _helix_repo()
    first = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# version A"))
    second = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# version B"))
    a, b = first.propose("make it A"), second.propose("make it B")
    first.approve(a.branch)
    with pytest.raises(BuildError):
        second.approve(b.branch)
    third = _gate(repo, lambda wt: _w(wt / "helix/services/reminders.py", "# next"))
    assert third.propose("something else").branch in GIT.list_branches(repo, "selfdev/")


def test_a_merge_that_fails_after_git_already_committed_is_still_fully_unwound():
    # The worst version of a failed approve: git DID merge, and merge_no_ff raised on the way back.
    # HEAD is now the merge commit, so an unwind that means "reset --hard HEAD" tidies the tree while
    # leaving the change applied — HELIX would be running code it just told the user it had not
    # applied, and the branch it kept for review is already live.
    repo = _helix_repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"),
                git=_MergeThenBoom(GIT))
    pc = svc.propose("improve conversation")
    # Nothing untracked is dropped in the root here on purpose: approve() now refuses outright to merge
    # into a tree that isn't clean, so a stray file would make this test pass by never merging at all —
    # a green test proving nothing. The unwind's duty not to reach for `git clean -fd` is pinned
    # directly, on the branch that used to, by the no-base-sha test below.
    with pytest.raises(BuildError):
        svc.approve(pc.branch)
    live = (repo / "helix/services/conversation.py").read_text(encoding="utf-8")
    assert live == "# conversation"  # base content — the change did NOT stay applied
    assert [c.summary for c in GIT.log(repo, limit=5)] == ["base"]  # …and the merge commit is gone
    assert pc.branch in GIT.list_branches(repo, "selfdev/")  # still theirs to read and discard


def test_applying_a_change_refuses_a_tree_with_the_users_own_uncommitted_edits_in_it():
    # The most dangerous thing in this file: unwinding a failed merge means `git reset --hard <base>`
    # across HELIX's ENTIRE root, and a hard reset cannot tell a half-written merge from Brian's own
    # half-finished edit to a HELIX source file. It is not an exotic pairing either — git REFUSES to
    # merge over a locally modified file ("Please commit your changes or stash them"), so a dirty tree
    # is exactly the case that fails the merge and reaches the unwind, and every uncommitted change in
    # the repo (not just the conflicting file) goes with it while the user is told "HELIX's own code is
    # untouched". Refuse before the merge, where nothing has been touched yet.
    repo = _helix_repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"))
    pc = svc.propose("improve conversation")
    _w(repo / "helix/services/conversation.py", "# BRIAN'S OWN EDIT")  # the file the merge wants
    _w(repo / "README.md", "BRIAN NOTES")  # …and one it has no interest in
    _w(repo / "scratch_note.txt", "half-written script")  # untracked, dropped beside the source
    with pytest.raises(BuildError) as err:
        svc.approve(pc.branch)
    assert (repo / "helix/services/conversation.py").read_text(encoding="utf-8") == "# BRIAN'S OWN EDIT"
    assert (repo / "README.md").read_text(encoding="utf-8") == "BRIAN NOTES"
    assert (repo / "scratch_note.txt").exists()
    assert [c.summary for c in GIT.log(repo, limit=5)] == ["base"]  # the merge never ran
    assert pc.branch in GIT.list_branches(repo, "selfdev/")  # still theirs, applicable once the tree is
    assert "uncommitted" in str(err.value)  # …and the reason names something the user can act on


def test_an_unwind_with_no_base_sha_never_reaches_for_git_clean_and_never_overclaims():
    # `git log -1` is broken, so the unwind has no sha to aim at AND the merge is already committed.
    # The fallback used to be discard_changes — reset --hard PLUS `git clean -fd`, which deletes every
    # untracked, non-ignored file in HELIX's own root (notes, a half-written script) that no merge ever
    # put there — and it then reported success, so the user heard "nothing was applied, and HELIX's own
    # code is untouched" about a merge commit sitting at HEAD.
    repo = _helix_repo()
    git = _BlindMergeThenBoom(GIT)
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"), git=git)
    pc = svc.propose("improve conversation")
    with pytest.raises(BuildError) as err:
        svc.approve(pc.branch)
    assert git.discarded == 0  # never `git clean -fd` on the user's own root
    msg = str(err.value)
    assert "Archive" not in msg  # a screen that does not exist; the persona is forbidden to name one
    assert "nothing was applied" not in msg and "untouched" not in msg  # the merge IS at HEAD
    assert "can't promise the change didn't land" in msg
    assert not (repo / ".git" / "MERGE_HEAD").exists()  # the half-merge state is still cleared


def test_a_merge_that_lands_is_reported_as_applied_even_if_the_branch_cannot_be_deleted():
    # Deleting the merged branch is bookkeeping that happens AFTER the merge commit is in. Letting it
    # raise sent the user "Couldn't apply it: …" about a change that IS applied and loads at the next
    # restart — so they restart into new code they were told had failed, and never approve it again.
    repo = _helix_repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"),
                git=_DeleteBranchBoom(GIT))
    pc = svc.propose("improve conversation")
    assert "Applied" in svc.approve(pc.branch)
    assert (repo / "helix/services/conversation.py").read_text(encoding="utf-8") == "# improved"
    # The branch git wouldn't delete costs nothing: its diff vs base is empty now, so it never shows
    # up again as something to apply or discard, and the startup sweep reaps it.
    assert not [p.id for p in svc.pending()]


def test_the_paused_constitution_message_names_no_screen_that_does_not_exist():
    # It used to send the user to "Archive → factory reset". There is no Archive screen in HELIX, and
    # the persona is explicitly forbidden to name one — so the app's most alarming message was also its
    # least followable. Whatever it says has to be a thing the user can actually go and do.
    repo = _helix_repo()
    settings = _GateSettings()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"),
                settings=settings)
    settings.set(selfdev_mod.FINGERPRINT_SETTING, "tampered")  # the tripwire, tripped
    with pytest.raises(ConstitutionViolation) as err:
        svc.propose("improve conversation")
    assert "Archive" not in str(err.value) and "factory reset" not in str(err.value)


def test_a_pending_change_can_be_read_as_a_diff_before_it_is_approved():
    # The human's only way to see what a self-change ACTUALLY does before it merges into HELIX's own
    # source — every other surface shows one line the coder wrote about itself.
    repo = _helix_repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# improved"))
    pc = svc.propose("improve conversation")
    text = svc.diff(pc.branch)
    assert "helix/services/conversation.py" in text and "+# improved" in text
    with pytest.raises(BuildError):
        svc.diff("HEAD")  # an id that is not a pending change is refused, never rendered


# ----- the sleep result holder (helix/domain/events.py) -----
# It lives beside the growth events this unit owns, and so do its pins. go_to_sleep used to ASSUME the
# mic obeyed: the console wrote "there's nothing to put to sleep" on screen while the model, told the
# tool had succeeded, spoke a goodnight for ears that never closed. The holder is how the GUI thread
# answers the parked worker with what actually happened.
def test_a_sleep_that_really_rested_the_ears_reports_success():
    req = SleepRequest()
    assert req.claim()
    req.fulfil()
    assert req.wait(claim_timeout=0.2, timeout=0.2) is True
    assert req.error == ""


def test_a_sleep_with_nothing_listening_reports_the_truth_not_a_goodnight():
    req = SleepRequest()
    assert req.claim()
    req.fail("Voice isn't listening right now, so there was nothing to rest.")
    assert req.wait(claim_timeout=0.2, timeout=0.2) is False
    assert "nothing to rest" in req.error


def test_a_sleep_nobody_answers_gives_up_fast_with_a_plain_reason():
    # No UI on the other side of the bus (headless, tests, a torn-down shell): nothing rested any
    # ears, so the turn must not hang and must not claim it did.
    req = SleepRequest()
    assert req.wait(claim_timeout=0.05, timeout=5.0) is False
    assert req.error and req.abandoned


def test_a_late_answer_can_never_rewrite_an_outcome_the_model_already_reported():
    # The window between "the worker gave up" and "the UI gets round to it" is real; a fulfil landing
    # then must be dropped, not resurrected into a goodnight for a turn that already moved on.
    req = SleepRequest()
    assert req.wait(claim_timeout=0.05, timeout=0.05) is False
    req.fulfil()
    assert req.claim() is False
    assert req.wait(claim_timeout=0.05, timeout=0.05) is False


def test_a_stop_mid_sleep_breaks_the_park_instead_of_waiting_it_out():
    class _Cancel:
        def is_set(self):
            return True

    req = SleepRequest()
    assert req.claim()
    assert req.wait(cancel=_Cancel(), claim_timeout=30.0, timeout=30.0) is False
    assert "Stopped" in req.error


def test_the_sleep_event_still_publishes_without_a_holder():
    # Every existing publisher constructs SleepRequested() with no arguments; the holder is a trailing
    # default so adding it could not break them.
    assert SleepRequested().request is None
    req = SleepRequest()
    assert SleepRequested(request=req).request is req


def test_a_huge_self_change_diff_is_cut_down_to_a_reviewable_size():
    repo = _helix_repo()
    svc = _gate(repo, lambda wt: _w(wt / "helix/services/conversation.py", "# padding\n" * 20_000))
    pc = svc.propose("write a great deal")
    text = svc.diff(pc.branch)
    assert len(text) <= selfdev_mod.DIFF_CAP + 200
    assert "only the first part of this change is shown" in text
