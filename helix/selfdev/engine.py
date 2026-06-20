"""Self-improvement engine: track drafted changes and apply them only on approval (§selfdev).

A drafted change (from `coder.run_coding_task`) lives committed on a `selfdev/*` branch. This module:
  - records it as a **pending** change (settings-backed),
  - **smoke-checks** the branch in an isolated git worktree (its code must import cleanly), and
  - on explicit **approval** (voice "ship it", or later an email reply) **merges** it into `main`.

`main` is never modified without that approval; rejecting deletes the branch. A restart loads merged
code (auto-restart is a later slice). Qt-free and settings-backed, so it is unit-testable.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from helix.core.config import load_config
from helix.selfdev import coder, constitution, gitops

SELFDEV_PENDING_SETTING = "selfdev_pending"


def _repo() -> str:
    return str(load_config().root_dir)


def record_pending(settings: Any, result: coder.CoderResult) -> dict:
    """Save a successful draft as a pending change awaiting approval. Returns the stored record."""
    rec = {
        "id": (result.commit or result.branch or "")[:10],
        "branch": result.branch,
        "commit": result.commit,
        "base": result.base or "main",
        "task": result.task,
        "summary": result.summary,
        "files": list(result.changed_files),
        "diffstat": result.diffstat,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }
    pending = [p for p in (settings.get(SELFDEV_PENDING_SETTING) or []) if p.get("branch") != rec["branch"]]
    pending.append(rec)
    settings.set(SELFDEV_PENDING_SETTING, pending)
    return rec


def list_pending(settings: Any) -> list:
    return [p for p in (settings.get(SELFDEV_PENDING_SETTING) or []) if p.get("status") == "pending"]


def _select(settings: Any, pending_id: str | None) -> dict | None:
    items = list_pending(settings)
    if not items:
        return None
    if pending_id:
        for p in items:
            if pending_id in (p.get("id"), p.get("branch")):
                return p
        return None
    return items[-1]  # most recent


def _set_status(settings: Any, branch: str, status: str) -> None:
    pending = list(settings.get(SELFDEV_PENDING_SETTING) or [])
    for p in pending:
        if p.get("branch") == branch:
            p["status"] = status
    settings.set(SELFDEV_PENDING_SETTING, pending)


def _changed_files_for(repo: str, rec: dict) -> list[str]:
    """The files a pending change touches — from git (authoritative), with the recorded list as fallback."""
    base = rec.get("base") or "main"
    branch = rec.get("branch") or ""
    if branch:
        try:
            names = gitops.diff_names(repo, base, branch)
            if names:
                return names
        except gitops.GitError:
            pass
    return list(rec.get("files") or [])


def smoke_check(repo: str, ref: str) -> tuple[bool, str]:
    """Import-check a branch's code in an isolated worktree, so the live tree is never disturbed.

    Returns (ok, detail). A pass means the key modules import cleanly on that branch — a cheap guard
    against a self-edit that breaks startup. (In a frozen build sys.executable is the app, not Python;
    smoke-checking is a dev-mode safeguard for now.)"""
    worktree = tempfile.mkdtemp(prefix="helix_smoke_")
    try:
        try:
            gitops.add_worktree(repo, worktree, ref)
        except gitops.GitError as exc:
            return False, f"could not create worktree: {exc}"
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import helix.interfaces.cli, helix.interfaces.qt_app; print('ok')" % worktree
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code], cwd=worktree, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # don't flash a console window
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"smoke check could not run: {exc}"
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "import failed").strip()[:1000]
        return True, "imports ok"
    finally:
        gitops.remove_worktree(repo, worktree)
        shutil.rmtree(worktree, ignore_errors=True)


@dataclass
class ApprovalResult:
    ok: bool
    message: str
    merged_commit: str | None = None


def approve(settings: Any, *, pending_id: str | None = None, into: str = "main",
            repo: str | None = None) -> ApprovalResult:
    """Smoke-check the (latest, or named) pending change and, if it passes, merge it into `into`."""
    repo = repo or _repo()
    rec = _select(settings, pending_id)
    if not rec:
        return ApprovalResult(False, "There's no pending change to approve.")
    branch = rec["branch"]
    # GUARDRAIL (Commandments 7 & 8): a self-change may never edit the protected machinery — the laws,
    # the approval gate, the off switch, or the recovery paths. Every approval route funnels through
    # here, so this scan is the one line nothing self-written can bypass.
    violations = constitution.check_change(_changed_files_for(repo, rec))
    if violations:
        _set_status(settings, branch, "blocked_guardrail")
        return ApprovalResult(
            False,
            "Blocked by the Twelve Commandments: this change edits protected machinery "
            f"({', '.join(violations)}). HELIX can't weaken its own guardrails or recovery paths. "
            "Reject it — or, if a human truly intends this, amend the constitution out-of-band.",
        )
    ok, detail = smoke_check(repo, branch)
    if not ok:
        _set_status(settings, branch, "failed_check")
        return ApprovalResult(False, f"Smoke check failed, so I did not merge: {detail}")
    if not gitops.is_clean(repo):
        return ApprovalResult(False, "The working tree isn't clean, so I won't merge right now.")
    try:
        merged = gitops.merge_to(
            repo, branch, into=into, message=f"selfdev: merge {branch}\n\n{rec.get('task', '')}".strip()
        )
    except gitops.GitError as exc:
        return ApprovalResult(False, f"Merge failed: {exc}")
    _set_status(settings, branch, "merged")
    if into == "main":
        from helix.selfdev import restart
        restart.request_restart(settings)  # the running app restarts on a safe tick to load it
        message = f"Merged {branch} into {into}. HELIX will restart to load it."
    else:
        message = f"Merged {branch} into {into}."
    return ApprovalResult(True, message, merged_commit=merged)


def reject(settings: Any, *, pending_id: str | None = None, repo: str | None = None) -> ApprovalResult:
    """Discard the (latest, or named) pending change and delete its branch."""
    repo = repo or _repo()
    rec = _select(settings, pending_id)
    if not rec:
        return ApprovalResult(False, "There's no pending change to reject.")
    gitops.delete_branch(repo, rec["branch"])
    _set_status(settings, rec["branch"], "rejected")
    return ApprovalResult(True, f"Rejected and deleted {rec['branch']}.")
