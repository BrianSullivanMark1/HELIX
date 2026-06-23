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

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

from helix.domain import constitution
from helix.domain.errors import BuildError, ConstitutionViolation
from helix.domain.models import PendingChange
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.coder import CoderAgent, ProgressFn
from helix.ports.repo import VersionedRepo
from helix.ports.stores import SettingsStore
from helix.services.prompts import improve_helix_prompt

_LOG = get_logger("selfdev")

SmokeCheck = Callable[[Path], "tuple[bool, str]"]
BRANCH_PREFIX = "selfdev/"
FINGERPRINT_SETTING = "constitution_fingerprint"


def import_smoke_check(worktree: Path) -> tuple[bool, str]:
    """Default smoke-check: import the app's heavy modules in the worktree. Catches syntax/import breaks.

    Runs offscreen so importing the Qt UI needs no display. (Dev-mode safeguard: in a frozen build
    sys.executable is the app, not Python — see ARCHITECTURE 'Known limitations'.)
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import helix.app.container, helix.ui.main_window"],
            cwd=str(worktree), capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=env, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"smoke-check could not run: {exc}"
    if proc.returncode == 0:
        return True, ""
    return False, (proc.stderr or proc.stdout or "import failed").strip()[:600]


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
        smoke_check: SmokeCheck = import_smoke_check,
    ) -> None:
        self._coder = coder
        self._repo = repo
        self._settings = settings
        self._clock = clock
        self._root = root
        self._worktrees = worktrees_dir
        self._smoke = smoke_check
        # Record the Constitution fingerprint on first run; later mismatches pause self-editing.
        if not self._settings.get(FINGERPRINT_SETTING):
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
    def propose(self, request: str, *, on_progress: ProgressFn | None = None) -> PendingChange:
        self._require_intact()
        if not self._repo.is_clean(self._root):
            raise BuildError("HELIX's working tree has uncommitted changes; cannot self-edit safely.")

        base = self._repo.current_branch(self._root)
        branch = self._branch_name(request)
        self._repo.create_branch(self._root, branch)
        try:
            result = self._coder.run_task(
                self._root, improve_helix_prompt(request), on_progress=on_progress
            )
            if not result.ok:
                raise BuildError(result.error or "the coder produced no change.")

            self._repo.stage_all(self._root)
            changed = self._repo.staged_changed(self._root)
            deleted = self._repo.staged_deleted(self._root)
            if not changed and not deleted:
                raise BuildError("the coder made no changes.")

            violations = constitution.check(changed, deleted)
            if violations:
                raise ConstitutionViolation(
                    "refused — this change touches HELIX's protected/shell code: " + "; ".join(violations)
                )

            summary = (result.summary or "").strip()
            commit = self._repo.commit_all(
                self._root, f"selfdev: {request.strip()[:64]}" + (f"\n\n{summary}" if summary else "")
            )
            # Leave the deployed branch checked out; the change waits on its branch until approved.
            self._repo.checkout(self._root, base)
            _LOG.info("proposed self-change on %s", branch)
            return PendingChange(
                id=branch, branch=branch, summary=summary or commit.summary,
                request=request, created_at=self._clock.now(),
            )
        except Exception:
            self._safe_abort(base, branch)
            raise

    def pending(self) -> list[PendingChange]:
        out: list[PendingChange] = []
        for branch in self._repo.list_branches(self._root, BRANCH_PREFIX):
            try:
                head = self._repo.branch_head(self._root, branch)
            except Exception:
                continue
            out.append(PendingChange(id=branch, branch=branch, summary=head.summary,
                                     request="", created_at=head.at))
        return out

    def approve(self, change_id: str) -> str:
        self._require_intact()
        if change_id not in self._repo.list_branches(self._root, BRANCH_PREFIX):
            raise BuildError("no such pending change.")

        # Smoke-check the change in an isolated worktree before it can touch the live tree.
        ok, err = self._smoke_in_worktree(change_id)
        if not ok:
            raise BuildError(f"smoke-check failed — not merging: {err}")

        self._repo.merge_no_ff(self._root, change_id, f"merge {change_id}")
        self._repo.delete_branch(self._root, change_id)
        _LOG.info("approved + merged %s", change_id)
        return "Applied. Restart HELIX to load the new version."

    def reject(self, change_id: str) -> None:
        self._repo.delete_branch(self._root, change_id)
        _LOG.info("rejected %s", change_id)

    # ----- helpers -----
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

    def _safe_abort(self, base: str, branch: str) -> None:
        try:
            self._repo.discard_changes(self._root)  # drop the coder's uncommitted edits
        except Exception:
            _LOG.exception("abort: could not discard changes")
        try:
            self._repo.checkout(self._root, base)
        except Exception:
            _LOG.exception("abort: could not switch back to %s", base)
        try:
            self._repo.delete_branch(self._root, branch)
        except Exception:
            _LOG.exception("abort: could not delete %s", branch)

    def _branch_name(self, request: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", request.lower()).strip("-")[:40].strip("-") or "change"
        stamp = self._clock.now().strftime("%m%d-%H%M%S")
        return f"{BRANCH_PREFIX}{slug}-{stamp}"
