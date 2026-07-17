"""SelfDevService — the approval gate for HELIX editing its OWN code.

The only real safety a self-writing program has is a law it cannot rewrite. Every self-change funnels
through this service, which enforces domain/constitution.py:

    propose:  clean tree → fingerprint check → branch → coder edits → stage → CONSTITUTION SCAN →
              commit on the branch → switch the working tree back to base (deployed code unchanged)
    approve:  fingerprint check → smoke-check the branch in an ISOLATED worktree → revertible --no-ff
              merge into base → (UI then offers restart)
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
                "the Constitution was changed outside the approval gate — self-editing is paused until "
                "a human restores it (Archive → factory reset, or restore the original file)."
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
            if cancel is not None and cancel.is_set():
                raise BuildError("the self-change was stopped.")
            escaped = tree_changed(self._root, src_sig, skip=src_skip)
            if escaped:
                self._repo.discard_changes(self._root)  # the live tree must be byte-identical to base
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

        self._repo.merge_no_ff(self._root, change_id, f"merge {change_id}")
        self._repo.delete_branch(self._root, change_id)
        _LOG.info("approved + merged %s", change_id)
        return "Applied. Restart HELIX to load the new version."

    def diff(self, change_id: str) -> str:
        """The unified diff of a pending change vs base — for the human to actually review."""
        base = self._repo.current_branch(self._root)
        return self._repo.diff(self._root, base, change_id)

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
