"""VersionedRepo adapter — backed by the git CLI. The only place that shells out to git."""
from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from helix.ports.repo import Commit

_FMT = "%H%x1f%s%x1f%cI"  # sha, subject, committer-date(ISO) joined by 0x1f


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, git: str = "git") -> None:
        self._git = git
        # An empty dir so HELIX-driven git NEVER executes a (possibly planted) repo hook. A hook in
        # .git/hooks would otherwise fire during merge/checkout — arbitrary code the scan can't see.
        self._no_hooks = tempfile.mkdtemp(prefix="helix-nohooks-")

    def _run(self, repo_dir: Path, *args: str) -> str:
        proc = subprocess.run(
            [self._git, "-c", f"core.hooksPath={self._no_hooks}", *args],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # git emits UTF-8; don't crash on a non-ASCII app name/path
        )
        if proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {msg}")
        return proc.stdout.strip()

    def _parse_commit(self, line: str) -> Commit:
        sha, summary, iso = line.split("\x1f")
        return Commit(sha=sha, summary=summary, at=datetime.fromisoformat(iso))

    def _head(self, repo_dir: Path) -> Commit:
        return self._parse_commit(self._run(repo_dir, "log", "-1", f"--pretty={_FMT}"))

    # ----- VersionedRepo -----
    def init(self, repo_dir: Path) -> None:
        repo_dir.mkdir(parents=True, exist_ok=True)
        self._run(repo_dir, "init", "-q")
        self._run(repo_dir, "config", "user.name", "HELIX")
        self._run(repo_dir, "config", "user.email", "helix@localhost")

    def current_branch(self, repo_dir: Path) -> str:
        return self._run(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")

    def create_branch(self, repo_dir: Path, name: str) -> None:
        self._run(repo_dir, "checkout", "-q", "-b", name)

    def checkout(self, repo_dir: Path, ref: str) -> None:
        self._run(repo_dir, "checkout", "-q", ref)

    def commit_all(self, repo_dir: Path, message: str) -> Commit:
        self._run(repo_dir, "add", "-A")
        self._run(repo_dir, "commit", "-q", "--allow-empty", "-m", message)
        return self._head(repo_dir)

    def merge_no_ff(self, repo_dir: Path, branch: str, message: str) -> Commit:
        self._run(repo_dir, "merge", "--no-ff", "-q", "-m", message, branch)
        return self._head(repo_dir)

    def revert(self, repo_dir: Path, sha: str) -> Commit:
        self._run(repo_dir, "revert", "--no-edit", sha)
        return self._head(repo_dir)

    def restore_to(self, repo_dir: Path, sha: str) -> None:
        self._run(repo_dir, "reset", "--hard", sha)

    def discard_changes(self, repo_dir: Path) -> None:
        self._run(repo_dir, "reset", "--hard")  # drop staged/unstaged edits
        self._run(repo_dir, "clean", "-fd")  # drop untracked files (respects .gitignore)

    def log(self, repo_dir: Path, limit: int = 100) -> list[Commit]:
        out = self._run(repo_dir, "log", f"-{int(limit)}", f"--pretty={_FMT}")
        return [self._parse_commit(ln) for ln in out.splitlines() if ln.strip()]

    def changed_paths(self, repo_dir: Path, ref_a: str, ref_b: str) -> list[str]:
        out = self._run(repo_dir, "diff", "--name-only", "--no-renames", "--diff-filter=ACMR", ref_a, ref_b)
        return [ln for ln in out.splitlines() if ln.strip()]

    def deleted_paths(self, repo_dir: Path, ref_a: str, ref_b: str) -> list[str]:
        out = self._run(repo_dir, "diff", "--name-only", "--no-renames", "--diff-filter=D", ref_a, ref_b)
        return [ln for ln in out.splitlines() if ln.strip()]

    def diff(self, repo_dir: Path, ref_a: str, ref_b: str) -> str:
        return self._run(repo_dir, "diff", "--no-color", "--no-renames", ref_a, ref_b)

    def hooks_dir(self, repo_dir: Path) -> Path:
        # Use the common-dir (NOT --git-path hooks, which honors our core.hooksPath override) so the
        # tripwire scans the REAL default hooks location where a planted hook would sit.
        raw = self._run(repo_dir, "rev-parse", "--git-common-dir")
        common = Path(raw)
        if not common.is_absolute():
            common = repo_dir / common
        return common / "hooks"

    def is_clean(self, repo_dir: Path) -> bool:
        return not self._run(repo_dir, "status", "--porcelain").strip()

    def stage_all(self, repo_dir: Path) -> None:
        self._run(repo_dir, "add", "-A")

    def staged_changed(self, repo_dir: Path) -> list[str]:
        out = self._run(repo_dir, "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=ACMR")
        return [ln for ln in out.splitlines() if ln.strip()]

    def staged_deleted(self, repo_dir: Path) -> list[str]:
        # --no-renames so a renamed protected/shell file shows its OLD path as a deletion.
        out = self._run(repo_dir, "diff", "--cached", "--name-only", "--no-renames", "--diff-filter=D")
        return [ln for ln in out.splitlines() if ln.strip()]

    def list_branches(self, repo_dir: Path, prefix: str = "") -> list[str]:
        out = self._run(repo_dir, "branch", "--list", f"{prefix}*", "--format=%(refname:short)")
        return [ln.strip() for ln in out.splitlines() if ln.strip()]

    def delete_branch(self, repo_dir: Path, name: str) -> None:
        self._run(repo_dir, "branch", "-D", name)

    def branch_head(self, repo_dir: Path, branch: str) -> Commit:
        return self._parse_commit(self._run(repo_dir, "log", "-1", f"--pretty={_FMT}", branch))

    def add_worktree(self, repo_dir: Path, path: Path, ref: str) -> None:
        self._run(repo_dir, "worktree", "add", "-q", str(path), ref)

    def remove_worktree(self, repo_dir: Path, path: Path) -> None:
        self._run(repo_dir, "worktree", "remove", "--force", str(path))
