"""Recent git activity across a person's associated projects — "what work got done."

Pure, local, read-only: shells out to `git log` (via `subprocess`) for each configured repo and
parses recent commits + line churn into a structured digest the Enterprise tab shows and Claude
summarizes. No network, no mutation (no `pull`/`fetch`/`checkout`) — it only *reads* local history.

Repos are a user-set list of local paths (`enterprise_git_repos`), defaulting to the HELIX repo so
the feature works out of the box.
"""
from __future__ import annotations

import subprocess
from typing import Any

ENTERPRISE_REPOS_SETTING = "enterprise_git_repos"

# Field separator unlikely to appear in a commit subject (ASCII unit separator).
_SEP = "\x1f"


def parse_repos(raw: Any) -> list[str]:
    """A newline/comma/semicolon-separated list of repo paths -> cleaned, de-duped list."""
    if isinstance(raw, (list, tuple)):
        tokens = list(raw)
    else:
        tokens = str(raw or "").replace(";", "\n").replace(",", "\n").splitlines()
    out: list[str] = []
    for token in tokens:
        path = str(token).strip().strip('"')
        if path and path not in out:
            out.append(path)
    return out


def _git(repo_path: str, args: list[str], timeout: int = 15) -> str | None:
    """Run a git command in `repo_path`; return stdout (stripped) or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def is_git_repo(repo_path: str) -> bool:
    return _git(repo_path, ["rev-parse", "--is-inside-work-tree"]) == "true"


def repo_summary(repo_path: str, *, since_days: int = 7, max_commits: int = 40) -> dict[str, Any] | None:
    """Recent commits + churn for one repo. Returns None if it isn't a readable git repo."""
    if not is_git_repo(repo_path):
        return None
    branch = _git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"]) or "?"
    fmt = _SEP.join(["%h", "%an", "%ad", "%s"])
    out = _git(
        repo_path,
        [
            "log",
            f"--since={since_days} days ago",
            f"-n{max_commits}",
            f"--pretty=format:{fmt}",
            "--date=short",
            "--numstat",
        ],
        timeout=20,
    )
    commits: list[dict[str, Any]] = []
    if out:
        current: dict[str, Any] | None = None
        for line in out.splitlines():
            if _SEP in line:
                parts = line.split(_SEP)
                if len(parts) == 4:
                    current = {
                        "hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3],
                        "files": 0, "insertions": 0, "deletions": 0,
                    }
                    commits.append(current)
                continue
            if current is not None and "\t" in line:
                cols = line.split("\t")
                if len(cols) >= 3:
                    current["files"] += 1
                    if cols[0].isdigit():
                        current["insertions"] += int(cols[0])
                    if cols[1].isdigit():
                        current["deletions"] += int(cols[1])

    name = repo_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or repo_path
    return {
        "name": name,
        "path": repo_path,
        "branch": branch,
        "commits": commits,
        "totals": {
            "commits": len(commits),
            "insertions": sum(c["insertions"] for c in commits),
            "deletions": sum(c["deletions"] for c in commits),
            "authors": sorted({c["author"] for c in commits}),
        },
    }


def gather_git_digest(repo_paths: list[str], *, since_days: int = 7, max_commits: int = 40) -> list[dict[str, Any]]:
    """Summaries for each readable repo (invalid paths are skipped)."""
    summaries = []
    for path in repo_paths:
        summary = repo_summary(path, since_days=since_days, max_commits=max_commits)
        if summary is not None:
            summaries.append(summary)
    return summaries


def format_git_digest(summaries: list[dict[str, Any]], *, since_days: int = 7, max_commits: int = 15) -> str:
    """Plain, human, symbol-free rendering — readable on screen, safe to read aloud, good Claude input.
    Shows each commit's subject (the human description), not hashes/dates/line-count symbols."""
    if not summaries:
        return "No recent commits found in your projects."
    word = "project" if len(summaries) == 1 else "projects"
    lines = [f"Recent code work over the last {since_days} days, across {len(summaries)} {word}."]
    for repo in summaries:
        t = repo["totals"]
        who = (" by " + " and ".join(t["authors"])) if t["authors"] else ""
        changed = t["insertions"] + t["deletions"]
        commit_word = "commit" if t["commits"] == 1 else "commits"
        lines.append("")
        lines.append(
            f"{repo['name']}, on branch {repo['branch']}: {t['commits']} {commit_word}, "
            f"about {changed} lines changed{who}."
        )
        for commit in repo["commits"][:max_commits]:
            lines.append("  " + commit["subject"])
        if len(repo["commits"]) > max_commits:
            lines.append(f"  and {len(repo['commits']) - max_commits} more commits")
    return "\n".join(lines)
