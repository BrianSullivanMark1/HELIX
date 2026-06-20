"""Git write operations for the self-improvement loop, scoped to throwaway `selfdev/*` branches.

The Enterprise digest (`enterprise/gitwork.py`) only *reads* history; this module *writes* — but every
write is confined to a work branch, never to `main` directly. The coder branches off the deployed code,
edits files, and commits on the branch; the (later) approval step is the only thing that merges to main.
So main is never modified without explicit human approval, and deleting a branch discards its work.

Stdlib-only (subprocess over the `git` CLI), mirroring `gitwork.py`'s `_git` style. Unlike the
read-only reader, write failures RAISE `GitError` (check=True) so a botched step is never silent.
"""
from __future__ import annotations

import subprocess

# On Windows a windowless launch (pythonw) flashes a console window for every child console process
# (each git call). CREATE_NO_WINDOW suppresses that; it is 0 on other platforms. Without it, a burst of
# git calls — e.g. the Archive sync at startup — pops a burst of black terminal windows on screen.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GitError(RuntimeError):
    """A git command failed (non-zero exit, or git could not run)."""


def _git(repo: str, args: list[str], *, timeout: int = 30, check: bool = True) -> str:
    """Run `git -C repo <args>`; return stripped stdout. Raises GitError on failure when check=True."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        raise GitError(f"git {' '.join(args)} could not run: {exc}") from exc
    if check and result.returncode != 0:
        raise GitError((result.stderr or result.stdout or "git command failed").strip())
    return (result.stdout or "").strip()


def is_git_repo(repo: str) -> bool:
    try:
        return _git(repo, ["rev-parse", "--is-inside-work-tree"]) == "true"
    except GitError:
        return False


def current_branch(repo: str) -> str:
    return _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


def head_commit(repo: str) -> str:
    return _git(repo, ["rev-parse", "HEAD"])


def is_clean(repo: str) -> bool:
    """True if the working tree has no staged, unstaged, or untracked changes."""
    return _git(repo, ["status", "--porcelain"]) == ""


def branch_exists(repo: str, name: str) -> bool:
    out = _git(repo, ["branch", "--list", name])
    return bool(out)


def create_work_branch(repo: str, name: str, *, base: str | None = None) -> str:
    """Create and switch to `name` (off `base`, or current HEAD if None). Fails if it already exists."""
    args = ["switch", "-c", name]
    if base:
        args.append(base)
    _git(repo, args)
    return name


def switch(repo: str, name: str) -> None:
    """Check out an existing branch."""
    _git(repo, ["switch", name])


def changed_files(repo: str) -> list[str]:
    """Every changed/added/untracked path in the working tree (porcelain), staged or not."""
    out = _git(repo, ["status", "--porcelain"])
    files: list[str] = []
    for line in out.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:  # a rename shows "old -> new"; keep the destination
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            files.append(path)
    return files


def stage_all(repo: str) -> None:
    _git(repo, ["add", "-A"])


def staged_diff(repo: str, *, timeout: int = 60) -> str:
    return _git(repo, ["diff", "--cached"], timeout=timeout)


def staged_diffstat(repo: str) -> str:
    return _git(repo, ["diff", "--cached", "--stat"], timeout=30)


def commit_all(repo: str, message: str) -> str:
    """Stage everything and commit; return the new commit hash. Assumes there is something to commit."""
    stage_all(repo)
    _git(repo, ["commit", "-m", message])
    return head_commit(repo)


def delete_branch(repo: str, name: str, *, force: bool = True) -> None:
    """Delete a branch (best-effort; never raises — used in cleanup paths)."""
    _git(repo, ["branch", "-D" if force else "-d", name], check=False)


def merge_to(repo: str, name: str, *, into: str = "main", message: str | None = None) -> str:
    """Merge work branch `name` into `into` with an explicit (no-ff) merge commit; return its hash.

    Used only by the approval step, after a human says yes. --no-ff keeps the merge a single,
    revertible commit so a bad self-improvement can be undone with one `git revert`.
    """
    switch(repo, into)
    args = ["merge", "--no-ff", name]
    if message:
        args += ["-m", message]
    _git(repo, args, timeout=60)
    return head_commit(repo)


def show_file(repo: str, ref: str, path: str, *, timeout: int = 30) -> str | None:
    """Return the contents of `path` as it existed at `ref` (commit/branch/tag), or None if absent there."""
    try:
        return _git(repo, ["show", f"{ref}:{path}"], timeout=timeout)
    except GitError:
        return None


def diff_names(repo: str, base: str, branch: str, *, timeout: int = 30) -> list[str]:
    """Files that `branch` changes relative to `base` (the merge-base three-dot diff), repo-relative and
    forward-slashed. Used by the approval gate to scan a self-change against the protected paths."""
    out = _git(repo, ["diff", "--name-only", f"{base}...{branch}"], timeout=timeout)
    return [line.strip() for line in out.splitlines() if line.strip()]


def log_merges(repo: str, branch: str = "main", *, limit: int = 200) -> list[dict]:
    """First-parent merge commits on `branch`, newest first: [{sha, date, subject, body}].

    These are the self-improvement landings (`merge_to` makes one --no-ff merge per approval), so they
    are the natural unit for the Archive's whole-app version list.
    """
    sep, rec = "\x1f", "\x1e"
    fmt = sep.join(["%H", "%cI", "%s", "%b"]) + rec
    try:
        out = _git(
            repo, ["log", "--first-parent", "--merges", f"-{int(limit)}", f"--format={fmt}", branch], timeout=60
        )
    except GitError:
        return []
    versions: list[dict] = []
    for chunk in out.split(rec):
        if not chunk.strip():
            continue
        parts = chunk.strip("\n").split(sep)
        if len(parts) < 4:
            continue
        versions.append(
            {"sha": parts[0].strip(), "date": parts[1].strip(), "subject": parts[2].strip(), "body": parts[3].strip()}
        )
    return versions


def restore_tree(repo: str, ref: str, *, timeout: int = 60) -> None:
    """Make the index + working tree exactly match `ref`'s tree WITHOUT moving HEAD — the basis for a
    non-destructive 'restore to version X' (the caller commits the result on the current branch, so
    history is preserved and nothing is lost). Tracked files only; the gitignored data/ dir is untouched."""
    _git(repo, ["read-tree", "-u", "--reset", ref], timeout=timeout)


def push(repo: str, *, remote: str = "origin", ref: str = "main", timeout: int = 120) -> str:
    """Push `ref` to `remote` (manual GitHub backup). Raises GitError on failure."""
    return _git(repo, ["push", remote, ref], timeout=timeout)


def add_worktree(repo: str, path: str, ref: str) -> None:
    """Check out `ref` (detached) into a separate worktree at `path` — used to smoke-check a branch's
    code without disturbing the live working tree."""
    _git(repo, ["worktree", "add", "--detach", path, ref], timeout=60)


def remove_worktree(repo: str, path: str) -> None:
    """Remove a worktree (best-effort; never raises — used in cleanup paths)."""
    _git(repo, ["worktree", "remove", "--force", path], check=False, timeout=60)
