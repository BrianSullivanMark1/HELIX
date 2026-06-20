"""Interface versioning, provenance & safe rollback for the self-improvement loop (§selfdev).

Every self-improvement already lands as a revertible merge commit on `main` (`gitops.merge_to` uses
--no-ff). This module turns that git history into a user-facing **Archive**: it records each version's
construction prompt into SQLite, lets the user restore any prior version (a WHOLE-APP snapshot), pin a
master **default**, and **reset to the immutable ROOT baseline** — the app with a blank menu — if
something breaks.

Design split: **git is the version STORE** (immutable, revertible); **SQLite is the human-friendly
INDEX** (prompts, labels, the default/root pointers). Restores are **non-destructive**: they make a NEW
commit that sets the working tree back to a chosen version, so nothing in history is ever lost. GitHub
backup is manual (an explicit push), per the chosen policy.

Qt-free and dependency-light (stdlib + gitops), so the policy stays testable. `memory` is duck-typed
(any object with the `interface_versions` / `feature_provenance` helpers on SQLiteMemory).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from helix.selfdev import gitops, registry

REGISTRY_GIT_PATH = "helix/selfdev/registry.py"          # forward slashes for `git show ref:path`
REGISTRY_REL = Path("helix", "selfdev", "registry.py")    # OS path for filesystem writes

ROOT_LABEL = "Root baseline — blank menu"
ROOT_PROMPT = "Factory baseline: the core pillars only, with a blank menu. The lifeline if HELIX breaks."

# A known-good, truly-empty registry, used only if the in-place blanking can't find the expected
# MENU_FEATURES literal (so a Reset-to-Root ALWAYS yields a genuinely blank menu).
_CANONICAL_BLANK_REGISTRY = '''"""Registry of SELF-ADDED features that appear in the launcher menu (§selfdev).

Core features (Investments, Home, Work, Learning, Settings) are NOT listed here and cannot be removed.
This is the blank-menu baseline; the coder appends self-added features here as they are built.
"""
from __future__ import annotations

MENU_FEATURES: list[dict] = []


def feature_keys() -> set[str]:
    return {f.get("key", "") for f in MENU_FEATURES if f.get("key")}
'''


# -- small parsers --------------------------------------------------------------- #

def _parse_menu_keys(content: str | None) -> set[str]:
    """Extract MENU_FEATURES keys from registry.py source. Reads ONLY inside the MENU_FEATURES list
    literal, so example keys in the module docstring/comments (e.g. {"key": "..."}) are never counted."""
    block = re.search(
        r"MENU_FEATURES\s*(?::\s*list\[dict\])?\s*=\s*\[(.*?)\]", content or "", flags=re.DOTALL
    )
    body = block.group(1) if block else ""
    return set(re.findall(r"""["']key["']\s*:\s*["']([^"']+)["']""", body))


def _menu_keys_at(repo: str, ref: str) -> set[str]:
    """The set of menu-feature keys present in registry.py at a given commit/ref."""
    content = gitops.show_file(repo, ref, REGISTRY_GIT_PATH)
    return _parse_menu_keys(content) if content is not None else set()


def _branch_from_subject(subject: str) -> str:
    """Pull the work-branch name out of a 'selfdev: merge <branch>' subject."""
    match = re.search(r"merge\s+(\S+)", subject or "")
    return match.group(1).strip() if match else ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return (text or "").strip()


def _blank_menu_file(repo: str) -> None:
    """Rewrite registry.py so MENU_FEATURES is empty (a blank menu). Falls back to a canonical blank
    registry if the literal can't be found, so the result is always a genuinely empty menu."""
    path = Path(repo) / REGISTRY_REL
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        path.write_text(_CANONICAL_BLANK_REGISTRY, encoding="utf-8")
        return
    blanked, replaced = re.subn(
        r"(MENU_FEATURES\s*(?::\s*list\[dict\])?\s*=\s*)\[.*?\]",
        r"\1[]",
        content,
        count=1,
        flags=re.DOTALL,
    )
    if replaced == 0 or _parse_menu_keys(blanked):  # literal not found / still non-empty — use canonical blank
        blanked = _CANONICAL_BLANK_REGISTRY
    path.write_text(blanked, encoding="utf-8")


# -- the Archive operations ------------------------------------------------------ #

def sync(memory: Any, settings: Any, repo: str) -> int:
    """Reconcile git merge history into the SQLite Archive (idempotent). Backfills existing versions,
    attaches each version's construction prompt, attributes prompts to the menu buttons they built, and
    prunes provenance for features that have since been removed. Returns the version count seen."""
    if not gitops.is_git_repo(repo):
        return 0
    if not memory.get_root_version():  # pin the current (known-good) HEAD as the root baseline, once
        try:
            memory.ensure_root_version(gitops.head_commit(repo), ROOT_LABEL, ROOT_PROMPT)
        except Exception:
            pass
    pending: dict[str, dict] = {}
    try:
        for record in (settings.get("selfdev_pending") or []):
            if record.get("branch"):
                pending[record["branch"]] = record
    except Exception:
        pending = {}
    root = memory.get_root_version()
    root_sha = root["commit_sha"] if root else None
    count = 0
    for merge in gitops.log_merges(repo, "main", limit=300):
        sha = merge["sha"]
        if sha == root_sha:  # the root baseline owns this commit; don't relabel it as a feature version
            continue
        branch = _branch_from_subject(merge["subject"])
        record = pending.get(branch, {})
        prompt = (record.get("task") or merge["body"] or merge["subject"]).strip()
        label = _first_line(record.get("task") or merge["body"] or branch or merge["subject"])[:120]
        memory.upsert_interface_version(sha, label, prompt, branch, created_at=merge["date"])
        count += 1
        try:
            added = _menu_keys_at(repo, sha) - _menu_keys_at(repo, sha + "^")
        except Exception:
            added = set()
        for key in added:
            memory.upsert_feature_provenance(key, label, prompt, branch, sha, "build")
    try:
        memory.prune_feature_provenance(registry.feature_keys())  # cleanup for any removed feature
    except Exception:
        pass
    return count


def restore_version(repo: str, commit_sha: str, *, blank_menu: bool = False, label: str = "") -> str:
    """Roll the WHOLE app back to `commit_sha` as a NEW, non-destructive commit on main (history kept).

    Returns the new commit hash, or "" if the tree already matches (nothing to do). Raises GitError if
    the working tree is dirty — refusing rather than risk losing uncommitted work."""
    if not gitops.is_clean(repo):
        raise gitops.GitError("the working tree isn't clean, so I won't restore (nothing gets lost this way)")
    gitops.switch(repo, "main")
    gitops.restore_tree(repo, commit_sha)        # index + worktree now match commit_sha; HEAD still on main
    if blank_menu:
        _blank_menu_file(repo)
    if gitops.is_clean(repo):
        return ""                                # already at this exact state
    short = (commit_sha or "")[:10]
    label = (label or "").strip()
    message = f"selfdev: restore to {label or short}"
    if blank_menu and "root" not in message.lower():
        message += " (blank menu)"
    return gitops.commit_all(repo, message)


def reset_to_root(repo: str, memory: Any) -> str:
    """Factory reset: restore the immutable ROOT baseline (a clean app with a blank menu). The lifeline
    if a self-improvement breaks the app. Non-destructive (a new commit); history is preserved."""
    root = memory.get_root_version()
    if not root:
        sha = gitops.head_commit(repo)
        memory.ensure_root_version(sha, ROOT_LABEL, ROOT_PROMPT)
        root = memory.get_root_version()
    return restore_version(repo, root["commit_sha"], blank_menu=True, label="root")


def purge_version(repo: str, memory: Any, version_id: int) -> tuple[bool, str]:
    """Permanently remove a version from the Archive: delete its work branch and its index row. Refuses
    to touch the master default or the root baseline. Does NOT rewrite main's history (that single
    operation is the one thing that could corrupt the repo / lose unrelated work)."""
    row = memory.get_interface_version(version_id)
    if not row:
        return False, "That version no longer exists."
    if row.get("is_root"):
        return False, "The root baseline can't be removed — it's your factory reset."
    if row.get("is_default"):
        return False, "That's the master default — set a different default first, then remove it."
    branch = (row.get("branch") or "").strip()
    if branch and gitops.branch_exists(repo, branch):
        gitops.delete_branch(repo, branch)
    memory.delete_interface_version(version_id)
    label = row.get("label") or (row.get("commit_sha") or "")[:10]
    return True, f"Permanently removed “{label}”."


def set_default(memory: Any, version_id: int) -> bool:
    """Pin one version as the master default (the user-set known-good checkpoint)."""
    return memory.set_default_version(version_id)


def push_to_github(repo: str) -> tuple[bool, str]:
    """Manual off-machine backup: push main to origin. Returns (ok, message)."""
    try:
        out = gitops.push(repo, remote="origin", ref="main")
        return True, "Pushed main to GitHub." + (f" {out}" if out else "")
    except gitops.GitError as exc:
        return False, f"Push failed: {exc}"
