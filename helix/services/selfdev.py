"""SelfDevService — the approval gate for HELIX editing its OWN code.

The only real safety a self-writing program has is a law it cannot rewrite. Every self-change funnels
through this service, which enforces domain/constitution.py:

    propose:  clean tree → fingerprint check → branch → coder edits → stage → CONSTITUTION SCAN →
              commit on the branch → switch the working tree back to base (deployed code unchanged)
    approve:  fingerprint check → smoke-check the branch in an ISOLATED worktree → clean-tree check
              (the merge and its unwind must own the tree) → revertible --no-ff merge into base →
              (UI then offers restart)
    reject:   delete the branch

Nothing merges without a human approving, the smoke-check must pass, and a change that touches a
PROTECTED_PATH or removes the IMMUTABLE_SHELL is refused before it can ever become approvable. This
module is itself a PROTECTED_PATH — the coder may never edit it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from helix.config import volatile_data_paths
from helix.domain import constitution
from helix.domain.errors import BuildError, ConstitutionViolation
from helix.domain.models import PendingChange
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.repo import VersionedRepo
from helix.ports.stores import SettingsStore
from helix.services.prompts import improve_helix_prompt
from helix.services.sandbox import restore_if_changed, scan_tree, snapshot_files, tree_changed

_LOG = get_logger("selfdev")

SmokeCheck = Callable[[Path], "tuple[bool, str]"]
BRANCH_PREFIX = "selfdev/"
FINGERPRINT_SETTING = "constitution_fingerprint"
DIFF_CAP = 16_000  # chars of a self-change diff a review surface gets — enough to judge, never a flood


def compile_smoke_check(worktree: Path) -> tuple[bool, str]:
    """Default smoke-check: byte-compile the source WITHOUT importing it.

    Importing a branch's code would execute its module-level statements — arbitrary code, at approve
    time, in a full-privilege process. `compileall` only parses + compiles, so it catches syntax breaks
    with zero execution. (Import-time errors slip through, but those are recoverable via Archive restore;
    avoiding approve-time code execution matters more.) Dev-mode safeguard: in a frozen build
    sys.executable is the app, not Python — see ARCHITECTURE 'Known limitations'.
    """
    # -I (isolated) + an explicit sys.path strip so the worktree's cwd is NOT importable: otherwise a
    # branch could add a root-level `compileall.py` that shadows the stdlib module and runs as code.
    # compile_dir/compile_file resolve their targets by filesystem path (cwd-relative), unaffected.
    code = (
        "import sys, os; sys.path[:] = [p for p in sys.path if p not in ('', '.')]; "
        "import compileall; "
        "ok = compileall.compile_dir('helix', quiet=1, force=True) "
        "and compileall.compile_file('main.py', quiet=1, force=True) "
        # tests/ is now a growable surface — byte-compile it too so a drafted test with a syntax
        # error is caught here (before merge), not only when the suite next runs.
        "and (not os.path.isdir('tests') or compileall.compile_dir('tests', quiet=1, force=True)); "
        "sys.exit(0 if ok else 1)"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=str(worktree), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash in a frozen build
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"smoke-check could not run: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stdout or proc.stderr or "compile failed").strip()[:600]


class SelfDevService:
    def __init__(
        self,
        coder: CoderAgent,
        repo: VersionedRepo,
        settings: SettingsStore,
        clock: Clock,
        root: Path,
        *,
        worktrees_dir: Path,
        smoke_check: SmokeCheck = compile_smoke_check,
        guard_files: list[Path] | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._coder = coder
        self._repo = repo
        self._settings = settings
        self._clock = clock
        self._root = root
        self._worktrees = worktrees_dir
        self._smoke = smoke_check
        self._guard_files = list(guard_files or [])  # reverted if the coder writes into them
        self._data_dir = data_dir  # the off-limits data/ tree (db, logs, built apps)
        # The fingerprint tripwire detects OUT-OF-BAND edits to the safety code (constitution + this
        # gate). Record it on first run. In a FROZEN build the safety code is read-only .pyc bundled in
        # the exe — a user cannot edit it out of band, so a fingerprint that no longer matches can ONLY
        # mean a legitimately NEW BUILD shipped new safety code (as this very change did). Re-stamp it
        # automatically there, so a genuine upgrade never strands the user in the paused state. In DEV
        # mode the source IS editable on disk — that is the real tamper surface — so keep the strict
        # compare-and-pause (below), re-stamping only when the fingerprint is missing entirely.
        stored = self._settings.get(FINGERPRINT_SETTING)
        if not stored or getattr(sys, "frozen", False):
            self._settings.set(FINGERPRINT_SETTING, constitution.fingerprint())

    # ----- guards -----
    def _fingerprint_ok(self) -> bool:
        return self._settings.get(FINGERPRINT_SETTING) == constitution.fingerprint()

    def _require_intact(self) -> None:
        if not self._fingerprint_ok():
            raise ConstitutionViolation(
                # Not "Archive → factory reset": there is no Archive screen in the app (the persona is
                # forbidden to name one), so the old wording sent the user hunting a menu that does not
                # exist at the moment they most need a real instruction.
                "the Constitution was changed outside the approval gate — self-editing is paused until "
                "a human puts HELIX's original safety code back in place."
            )
        if self._settings.get("human_approval_required", True) is not True:
            raise ConstitutionViolation("human_approval_required is locked on and may not be disabled.")

    # ----- propose / approve / reject -----
    def propose(
        self, request: str, *, on_progress: ProgressFn | None = None, cancel=None,
        model: str | None = None,
    ) -> PendingChange:
        self._require_intact()
        self._refuse_if_hooks_present()
        if not self._repo.is_clean(self._root):
            raise BuildError("HELIX's working tree has uncommitted changes; cannot self-edit safely.")

        base = self._repo.current_branch(self._root)
        branch = self._branch_name(request)
        self._worktrees.mkdir(parents=True, exist_ok=True)
        wt = self._worktrees / (branch.replace("/", "_") + "-draft")
        # Draft the change in an ISOLATED worktree on its own branch: the live deployed tree NEVER leaves
        # base, so a crash/kill mid-draft can't strand it on the selfdev branch, and a concurrent
        # background build never sees half-edited HELIX source. The same Constitution scan + data-guard
        # apply, on the worktree's staged diff — PLUS a live-source escape backstop (below).
        self._remove_worktree(wt)  # reap any crash-leftover draft at this path before re-creating it
        shutil.rmtree(wt, ignore_errors=True)
        try:
            self._repo.add_worktree_branch(self._root, wt, branch, base)
        except Exception:  # git creates the branch ref even when the worktree add fails — clean it up
            self._cleanup_draft(wt, branch)
            raise
        guard = snapshot_files(self._guard_files)
        # The data guard scans data/ for a coder that wrote outside its worktree — but it must SKIP the
        # app's own volatile stores (helix.db, agents/memory/reflexes stamps, the log), because a
        # self-change draft runs the coder for MINUTES while the live app keeps writing them. Without
        # this skip, HELIX's own mid-draft writes are misread as a coder escape and a good draft is
        # refused ("the coder wrote into protected data/ (helix.db, …)"). Shared with the Forge guard.
        # ALSO skip data/builds: a background BUILD (or a knowledge ingest) can run CONCURRENTLY with a
        # draft and write its own workspace under data/builds — that is the build, not the self-dev
        # coder, exactly as the Forge guard skips the builds tree for concurrent builds.
        data_skip = (
            (*volatile_data_paths(self._data_dir), self._data_dir / "builds") if self._data_dir else ()
        )
        data_sig = scan_tree(self._data_dir, skip=data_skip) if self._data_dir else {}
        # Escape backstop: the shared coder (the Claude Code CLI) can target ABSOLUTE paths, so it could
        # write into the live deployed source OUTSIDE its draft worktree. The worktree's staged diff can't
        # see that, so snapshot the live source and fail closed if anything moved (mirrors ForgeService).
        src_skip = (self._root / ".git", self._data_dir, self._worktrees)
        src_sig = scan_tree(self._root, skip=src_skip)
        try:
            result = self._coder.run_task(
                wt, improve_helix_prompt(request), on_progress=on_progress, cancel=cancel, model=model
            )
            # The containment guards run UNCONDITIONALLY — before the cancel/failure exits — because a
            # stopped run drove the same coder with the same hands. A cancel is exactly when the tree
            # cannot be assumed clean: whatever it wrote before the stop is still on disk. (Mirrors
            # ForgeService, which scans + reverts escapes ahead of its own cancel exit.)
            escaped = tree_changed(self._root, src_sig, skip=src_skip)
            if escaped:
                # Revert exactly what escaped, and nothing else. This used to be discard_changes —
                # `git reset --hard` PLUS `git clean -fd` across HELIX's live root — which undid the
                # containment breach by ALSO destroying every uncommitted edit and every untracked
                # file the user had in the tree: their half-written script, their notes, work they
                # never handed to HELIX at all. The breach is a known, enumerated list of paths, so
                # restore_paths puts precisely those back (tracked → checkout, newly added → removed)
                # and leaves the user's tree otherwise untouched. Containment must not cost more than
                # the thing it is containing.
                self._repo.restore_paths(self._root, list(escaped))
                raise ConstitutionViolation(
                    "refused — the coder wrote into the live HELIX source outside its draft worktree ("
                    + ", ".join(Path(p).name for p in escaped[:8]) + ")."
                )
            # Writes into gitignored data/ are invisible to git, so detect them on the filesystem (the
            # worktree lives OUTSIDE data/, so legit draft edits don't trip this — only a real escape).
            reverted = restore_if_changed(guard)  # settings written by the coder are reverted
            data_hit = tree_changed(self._data_dir, data_sig, skip=data_skip) if self._data_dir else []
            if reverted or data_hit:
                names = reverted + [Path(p).name for p in data_hit]
                raise ConstitutionViolation(
                    "refused — the coder wrote into protected data/ (" + ", ".join(names[:8]) + ")."
                )
            if cancel is not None and cancel.is_set():
                raise BuildError("the self-change was stopped.")
            if not result.ok:
                raise BuildError(result.error or "the coder produced no change.")

            self._repo.stage_all(wt)
            changed = self._repo.staged_changed(wt)
            deleted = self._repo.staged_deleted(wt)
            if not changed and not deleted:
                raise BuildError("the coder made no changes.")

            violations = constitution.check(changed, deleted)
            if violations:
                raise ConstitutionViolation(
                    "refused — this change touches HELIX's protected/shell code: " + "; ".join(violations)
                )

            summary = (result.summary or "").strip()
            commit = self._repo.commit_all(
                wt, f"selfdev: {request.strip()[:64]}" + (f"\n\n{summary}" if summary else "")
            )
            _LOG.info("proposed self-change on %s (isolated worktree)", branch)
            return PendingChange(
                id=branch, branch=branch, summary=summary or commit.summary,
                request=request, created_at=self._clock.now(),
            )
        except Exception:
            self._cleanup_draft(wt, branch)  # free the branch even if the worktree dir is locked
            raise
        finally:
            self._remove_worktree(wt)        # on success: drop the worktree, keep the branch + its commit

    def pending(self) -> list[PendingChange]:
        out: list[PendingChange] = []
        try:
            base = self._repo.current_branch(self._root)
        except Exception:
            base = ""
        for branch in self._repo.list_branches(self._root, BRANCH_PREFIX):
            try:
                head = self._repo.branch_head(self._root, branch)
                if base:  # skip a PHANTOM branch (empty diff vs base) — a draft that never committed; a
                    changed = self._repo.changed_paths(self._root, base, branch)  # leaked, undeletable
                    deleted = self._repo.deleted_paths(self._root, base, branch)  # branch must not show
                    if not changed and not deleted:                              # as an apply/discard item
                        continue
            except Exception:
                continue
            out.append(PendingChange(id=branch, branch=branch, summary=head.summary,
                                     request="", created_at=head.at))
        return out

    def recover_interrupted(self) -> None:
        """On startup, sweep self-change leaks from a crash/kill: prune dead worktree registrations, then
        delete any selfdev/ branch with an EMPTY diff vs base (a draft killed before it committed). NEVER
        touches a real, committed pending change — those are the user's reviewable drafts."""
        try:
            self._repo.prune_worktrees(self._root)  # un-register worktrees whose dirs are gone
        except Exception:
            _LOG.warning("could not prune selfdev worktrees", exc_info=True)
        try:
            base = self._repo.current_branch(self._root)
        except Exception:
            return
        for br in self._repo.list_branches(self._root, BRANCH_PREFIX):
            try:
                changed = self._repo.changed_paths(self._root, base, br)
                deleted = self._repo.deleted_paths(self._root, base, br)
                if not changed and not deleted:  # empty diff vs base → a phantom draft, safe to delete
                    self._delete_branch_quiet(br)
            except Exception:
                continue

    def approve(self, change_id: str) -> str:
        self._require_intact()
        self._refuse_if_hooks_present()
        if change_id not in self._repo.list_branches(self._root, BRANCH_PREFIX):
            raise BuildError("no such pending change.")

        # Re-scan the ACTUAL branch tip — a commit may have been appended after propose() scanned it.
        # The content that merges is the content that is scanned.
        base = self._repo.current_branch(self._root)
        violations = constitution.check(
            self._repo.changed_paths(self._root, base, change_id),
            self._repo.deleted_paths(self._root, base, change_id),
        )
        if violations:
            raise ConstitutionViolation(
                "refused at approval — this change touches protected/shell code: " + "; ".join(violations)
            )

        # Smoke-check (non-executing) in an isolated worktree before it can touch the live tree.
        ok, err = self._smoke_in_worktree(change_id)
        if not ok:
            raise BuildError(f"smoke-check failed — not merging: {err}")

        # A self-change may only be merged into a tree HELIX OWNS — one with nothing uncommitted in it.
        # Two reasons, and the second is the dangerous one. First, git itself refuses a merge that would
        # overwrite a locally-modified file ("Please commit your changes or stash them before you
        # merge"), so a dirty tree is precisely the case where the merge below fails. Second, the only
        # tool the unwind has is `git reset --hard <base>` across HELIX's whole root, and that reset
        # cannot tell a half-written merge from Brian's own half-finished edit to a HELIX source file:
        # it throws both away, repo-wide, while the message the user hears swears "HELIX's own code is
        # untouched". So decide it here, where refusing has touched NOTHING. This is no new burden —
        # propose() already refuses to draft against uncommitted work, so a tree that can't be applied
        # to couldn't have been drafted from either. Read it as late as possible, one call before the
        # merge: a check taken before the smoke-check would be seconds of compiling out of date.
        try:
            owns_tree = self._repo.is_clean(self._root)
        except Exception:  # noqa: BLE001 — if git can't even report status, assume we do NOT own the tree
            owns_tree = False
        if not owns_tree:
            raise BuildError(
                "HELIX's own code has uncommitted edits sitting in it right now, and applying this "
                "change could bury them. Commit or stash those edits first, then apply this again — "
                "nothing has been touched."
            )

        # Where the live tree stands BEFORE the merge, read while it is still trustworthy. merge_no_ff
        # runs `git merge` and THEN `git log -1` to report the new commit, so it can raise AFTER git
        # has already written the merge commit — a timeout, a killed subprocess, any git that dies
        # reading its own log. In that window HEAD is no longer base, and an unwind that only means
        # "reset --hard HEAD" would tidy the tree while LEAVING the change merged: HELIX would be
        # running code it just told the user it had not applied. Aim the unwind at this sha instead.
        try:
            base_sha = self._repo.branch_head(self._root, base).sha
        except Exception:  # noqa: BLE001 — not fatal; the unwind aborts in place and claims nothing
            base_sha = ""
        try:
            self._repo.merge_no_ff(self._root, change_id, f"merge {change_id}")
        except Exception as exc:
            # A merge that CONFLICTS leaves git mid-merge: MERGE_HEAD set and `<<<<<<<` markers written
            # straight into HELIX's own deployed .py files. Left there, the next launch runs on poisoned
            # source and — because the tree is now dirty — every later propose() is refused ("working
            # tree has uncommitted changes"), so self-editing is bricked until a human runs git by hand.
            # It is a reachable state: draft A, draft B, approve A (base moves), approve B. So unwind —
            # the live tree must end this call byte-identical to base, exactly as the escape backstop in
            # propose() insists. Safe to do so here and ONLY here, because the clean-tree check above
            # means everything this reset can throw away is something this call itself put there. The
            # branch is deliberately KEPT: nothing was applied, so it is still the user's to read and
            # discard.
            healed = self._unwind_failed_merge(base_sha)
            _LOG.warning("merge of %s failed; live tree %s", change_id,
                         "restored to base" if healed else "COULD NOT BE RESTORED", exc_info=True)
            if healed:
                raise BuildError(
                    "This change no longer fits the code it was drafted against — nothing was applied, "
                    "and HELIX's own code is untouched. Discard it and ask for the same improvement "
                    "again; the new draft will be written against today's version."
                ) from exc
            # The unhealed branch must not promise what the code cannot check. It is reached when the
            # reset itself failed, or when we never had a base sha to aim at — and in that second case
            # git may ALREADY have written the merge commit, so "nothing is running" would be a lie
            # that the next launch exposes. Name the one recovery that genuinely exists instead: the
            # startup self-heal (helix/app/bootstrap.py) rolls back to the last commit that booted.
            # Never "the Archive" — there is no such screen, and the persona is forbidden to name one.
            raise BuildError(
                "This change couldn't be applied cleanly, and HELIX couldn't put its own code back the "
                "way it was. I can't promise the change didn't land, so please don't ask for another "
                "self-change until this is sorted out — and if HELIX won't start after a restart, it "
                "rolls itself back to the last version that booted."
            ) from exc
        try:
            self._repo.delete_branch(self._root, change_id)
        except Exception:  # noqa: BLE001 — the merge is COMMITTED; the user must be told that, not this
            # Tidying the merged branch is bookkeeping, and failing it after a successful merge used to
            # raise straight past the return — so the caller rendered "Couldn't apply it: …" about a
            # change that IS applied and will load at the next restart. The stale branch costs nothing:
            # its diff vs base is now empty, so pending() skips it as a phantom and recover_interrupted()
            # deletes it on the next launch.
            _LOG.warning("merged %s but could not delete the branch", change_id, exc_info=True)
        _LOG.info("approved + merged %s", change_id)
        return "Applied. Restart HELIX to load the new version."

    def _unwind_failed_merge(self, base_sha: str = "") -> bool:
        """Put the live tree back exactly as it was before a merge that didn't complete.

        restore_to is `git reset --hard <sha>`, which clears MERGE_HEAD along with the half-merged
        index — git's own documented way to abandon a merge in progress — and, because it names the
        commit the tree stood on BEFORE the attempt, it also throws away a merge commit git may
        already have created before merge_no_ff raised. A bare reset to HEAD cannot: in that case HEAD
        IS the merge commit, so the "unwind" would leave the change applied and running while the user
        is told nothing was.

        Deliberately NOT discard_changes here, even though it is the verb propose() uses to undo a
        coder escape: that one is reset --hard PLUS `git clean -fd`, which also deletes every
        untracked, non-ignored file sitting in HELIX's own root — a user's notes, a half-written
        script, anything dropped beside the source. A merge leaves no untracked debris that the reset
        does not already handle, so the clean has nothing to gain here and somebody's file to lose.

        The caller MUST have just confirmed is_clean() on the live tree — approve() does, one call
        before the merge. A hard reset cannot tell a half-merge from the user's own unfinished edit to
        HELIX's source, so unwinding a tree we do not own would destroy their work repo-wide.

        Returns whether the tree is genuinely back at base, because the message the user gets depends
        on it.
        """
        try:
            if base_sha:
                self._repo.restore_to(self._root, base_sha)
                return True
            # We never got a sha (reading HEAD failed before the merge even started, so git was already
            # unwell) — and that is not an unrelated coincidence: the failing command is `git log -1`,
            # the SAME call merge_no_ff makes after committing, so this is exactly the case where the
            # merge is most likely to be committed already. Abort in place: reset --hard HEAD clears
            # MERGE_HEAD and the conflict markers, which is the damage that actually poisons the next
            # launch. Two things it deliberately does NOT do. It does not reach for discard_changes,
            # which is that same reset PLUS `git clean -fd`, deleting every untracked, non-ignored file
            # in HELIX's own root — a user's notes, a half-written script, anything dropped beside the
            # source, none of which a merge ever put there. And it does not report success: if HEAD is
            # the merge commit then this tidied the tree while LEAVING the change applied, and with git
            # log broken there is no way to tell which happened, so the caller must say so plainly
            # rather than claim a restoration we cannot see.
            self._repo.restore_to(self._root, "HEAD")
            return False
        except Exception:  # noqa: BLE001 — the caller is already raising; report, never mask
            _LOG.warning("could not unwind the failed merge in %s", self._root, exc_info=True)
            return False

    def diff(self, change_id: str) -> str:
        """The unified diff of a pending change vs base — the only way a human can see what a self-change
        ACTUALLY does before merging it into HELIX's own source. Every other surface shows a one-line
        summary written by the coder itself, and "nothing merges without a human approving" is worth
        little if the human has nothing to approve but a sentence.

        Refuse an id that isn't a pending change rather than diffing it: change_id arrives from a
        model-driven tool call, and `git diff base <anything>` would happily render an unrelated ref.
        """
        if change_id not in self._repo.list_branches(self._root, BRANCH_PREFIX):
            raise BuildError("no such pending change.")
        base = self._repo.current_branch(self._root)
        text = self._repo.diff(self._root, base, change_id)
        if len(text) > DIFF_CAP:
            # A review surface, not a firehose: a huge diff would blow the model's context (or the
            # console's) and the reviewer would see none of it. Say plainly that it was cut.
            text = text[:DIFF_CAP].rstrip() + "\n\n… (only the first part of this change is shown)"
        return text

    def reject(self, change_id: str) -> None:
        self._repo.delete_branch(self._root, change_id)
        _LOG.info("rejected %s", change_id)

    # ----- helpers -----
    def _refuse_if_hooks_present(self) -> None:
        """A planted git hook is invisible to the staged-diff scan; refuse to operate if any exist.

        Belt-and-suspenders: HELIX's own git calls already run with hooks disabled, but a hook on disk
        could still fire if the user runs git by hand, so we don't proceed while one is present.
        """
        try:
            hooks = self._repo.hooks_dir(self._root)
            planted = [
                p.name for p in hooks.glob("*")
                if p.is_file() and not p.name.endswith(".sample")
            ]
        except OSError:
            return
        if planted:
            raise ConstitutionViolation(
                f"git hooks are present ({', '.join(planted)}) — refusing to self-modify until removed."
            )

    def _smoke_in_worktree(self, branch: str) -> tuple[bool, str]:
        self._worktrees.mkdir(parents=True, exist_ok=True)
        wt = self._worktrees / branch.replace("/", "_")
        # Reap a crash/kill leftover at this exact path first, exactly as propose() does for its draft:
        # otherwise `git worktree add` refuses the existing dir and the change becomes PERMANENTLY
        # unapprovable — one interrupted approval would strand the user's reviewed work forever.
        self._remove_worktree(wt)
        shutil.rmtree(wt, ignore_errors=True)
        try:
            self._repo.add_worktree(self._root, wt, branch)
        except Exception as exc:
            return False, f"could not create smoke-check worktree: {exc}"
        try:
            return self._smoke(wt)
        finally:
            try:
                self._repo.remove_worktree(self._root, wt)
            except Exception:
                _LOG.warning("failed to remove smoke-check worktree %s", wt)

    def _remove_worktree(self, wt: Path) -> None:
        try:
            if wt.exists():
                self._repo.remove_worktree(self._root, wt)
        except Exception:
            _LOG.warning("could not remove selfdev worktree %s", wt)

    def _cleanup_draft(self, wt: Path, branch: str) -> None:
        """Tear down a failed/aborted draft: remove the worktree, prune stale registrations so the branch
        isn't pinned by a surviving (e.g. Windows-locked) worktree, then delete the branch."""
        self._remove_worktree(wt)
        try:
            self._repo.prune_worktrees(self._root)
        except Exception:
            _LOG.warning("could not prune worktrees", exc_info=True)
        self._delete_branch_quiet(branch)

    def _delete_branch_quiet(self, branch: str) -> None:
        try:
            self._repo.delete_branch(self._root, branch)
        except Exception:
            _LOG.warning("could not delete selfdev branch %s", branch)

    def _branch_name(self, request: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:40].strip("-") or "change"
        stamp = self._clock.now().strftime("%m%d-%H%M%S")
        return f"{BRANCH_PREFIX}{slug}-{stamp}"
