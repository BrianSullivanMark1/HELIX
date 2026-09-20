"""GithubRepos — the RepoReader for GitHub: HEAD of a branch, and how a serving commit relates to it.

An egress boundary, and in PROTECTED_FILES for that reason (HELIX_MARK1_PLAN.md §7.3, §10.1). It
carries its OWN host allowlist (api.github.com, nothing else), refuses every redirect (a token that
follows a redirect has been given away), caps the body, and never logs the token. It reads two things
and writes nothing.

GitHub is the host today; GitLab is a second adapter behind the same RepoReader port when it is
needed (§17.10). Nothing above the port cares which.

The token comes from a getter, not a value, so it is read fresh each call from the secrets store and
never sits in this object - the same pattern container.py uses for the Claude key.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote

from helix.ports.fleet import CompareRead, RepoRead

API_HOST = "api.github.com"
_BODY_CAP = 256 * 1024

NO_TOKEN = "No GitHub token is connected. Click the gear on any card and paste one."
NOT_FOUND = "GitHub cannot find {repo}, or this token is not allowed to see it. Check the repo name in the gear, and that you are a collaborator on it."
NO_BRANCH = "{repo} has no branch called {branch}. Pick the branch in the gear - this repo's default is {default}."
MOVED = "{repo} was renamed or moved on GitHub. Put its new name in the gear."
RATE_LIMITED = "GitHub is rate-limiting this token right now. Try again in a few minutes."
UNREACHABLE = "GitHub could not be reached. Check the internet connection."
BAD_OUTPUT = "GitHub answered {repo} with status {status}, which HELIX could not read."

HttpGet = Callable[[str, dict, float], tuple[int, str]]   # (url, headers, timeout) -> (status, body)


def _real_http_get(url: str, headers: dict, timeout_s: float) -> tuple[int, str]:
    import urllib.request
    import urllib.error

    class _Refuse(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    opener = urllib.request.build_opener(_Refuse)
    try:
        with opener.open(urllib.request.Request(url, method="GET", headers=headers),
                         timeout=timeout_s) as r:
            return r.status, r.read(_BODY_CAP).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


class GithubRepos:
    def __init__(self, token_getter: Callable[[], str | None], *,
                 http_get: HttpGet | None = None) -> None:
        self._token = token_getter
        self._http = http_get or _real_http_get

    def available(self) -> tuple[bool, str | None]:
        return (True, None) if (self._token() or "").strip() else (False, NO_TOKEN)

    # ---------------------------------------------------------------- plumbing

    def _get(self, path: str, timeout_s: float) -> tuple[int, dict | None, str]:
        """(status, parsed json or None, raw). The URL is BUILT here from a path, never accepted from
        a caller, so nothing can point this adapter at another host."""
        token = (self._token() or "").strip()
        if not token:
            return -1, None, ""
        url = f"https://{API_HOST}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "HELIX-fleet"}
        status, body = self._http(url, headers, timeout_s)
        doc = None
        if body:
            try:
                parsed = json.loads(body)
                doc = parsed if isinstance(parsed, dict) else None
            except ValueError:
                doc = None
        return status, doc, body[:400]

    def _get_list(self, path: str, timeout_s: float) -> tuple[int, list, str]:
        """Like _get for endpoints that answer with a JSON array."""
        token = (self._token() or "").strip()
        if not token:
            return -1, [], ""
        url = f"https://{API_HOST}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "HELIX-fleet"}
        status, body = self._http(url, headers, timeout_s)
        docs: list = []
        if body:
            try:
                parsed = json.loads(body)
                docs = [d for d in parsed if isinstance(d, dict)] if isinstance(parsed, list) else []
            except ValueError:
                docs = []
        return status, docs, body[:400]

    @staticmethod
    def _problem(status: int, repo: str = "the repo", branch: str = "", default: str = "?") -> str:
        if status == -1:
            return NO_TOKEN
        if status in (403, 429):
            return RATE_LIMITED
        if status == 404:
            return NOT_FOUND.format(repo=repo)
        if status in (301, 302, 307, 308):
            return MOVED.format(repo=repo)
        if status == 422 and branch:
            return NO_BRANCH.format(repo=repo, branch=branch, default=default)
        if status == 0:
            return UNREACHABLE
        return BAD_OUTPUT.format(repo=repo, status=status)

    def default_branch(self, repo: str, *, timeout_s: float = 20.0) -> str | None:
        """The repo's default branch (main, master, v3...), or None when GitHub will not say."""
        status, doc, _ = self._get(f"repos/{quote(repo)}", timeout_s)
        if status != 200 or not doc:
            return None
        return str(doc.get("default_branch") or "") or None

    # ---------------------------------------------------------------- reads

    def read_head(self, repo: str, branch: str, *, timeout_s: float = 20.0) -> RepoRead:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return RepoRead(repo=repo, branch=branch, ok=False, problem=NOT_FOUND,
                            detail="repo must be 'owner/name'")
        status, doc, raw = self._get(
            f"repos/{quote(owner)}/{quote(name)}/commits/{quote(branch, safe='')}", timeout_s)
        if status == 422:
            # GitHub's "No commit found for SHA: main" - the branch is not there. Read the repo's
            # default branch instead and SAY so (the cell keeps its own branch name).
            default = self.default_branch(repo, timeout_s=timeout_s)
            if default and default != branch:
                status, doc, raw = self._get(
                    f"repos/{quote(owner)}/{quote(name)}/commits/{quote(default, safe='')}", timeout_s)
                if status == 200 and doc and doc.get("sha"):
                    head = self._head_from(repo, default, doc)
                    return RepoRead(repo=repo, branch=default, ok=True, commit=head.commit, full_sha=head.full_sha,
                                    subject=head.subject, committed_at=head.committed_at,
                                    problem=NO_BRANCH.format(repo=repo, branch=branch, default=default))
            return RepoRead(repo=repo, branch=branch, ok=False,
                            problem=self._problem(422, repo, branch, default or "unknown"), detail=raw)
        if status != 200 or not doc:
            return RepoRead(repo=repo, branch=branch, ok=False, problem=self._problem(status, repo, branch),
                            detail=raw)
        if not doc.get("sha"):
            return RepoRead(repo=repo, branch=branch, ok=False, problem=BAD_OUTPUT.format(repo=repo, status=status), detail=raw)
        return self._head_from(repo, branch, doc)

    @staticmethod
    def _head_from(repo: str, branch: str, doc: dict) -> RepoRead:
        sha = str(doc.get("sha") or "")
        commit = doc.get("commit") or {}
        message = str(commit.get("message") or "")
        when = ((commit.get("committer") or {}).get("date")
                or (commit.get("author") or {}).get("date"))
        return RepoRead(repo=repo, branch=branch, ok=True, commit=sha[:7], full_sha=sha,
                        subject=message.splitlines()[0][:120] if message else None,
                        committed_at=_ts(when))

    def compare(self, repo: str, serving: str, head: str, *, timeout_s: float = 20.0) -> CompareRead:
        """GitHub's compare is BASE...HEAD and reports HEAD relative to BASE. We put the SERVING
        commit as base, so 'ahead' from GitHub means the repo has moved on = serving is BEHIND, the
        ordinary state. The word is flipped here, once, so callers get the cell's point of view."""
        owner, _, name = repo.partition("/")
        if not owner or not name or not serving or not head:
            return CompareRead(repo=repo, serving=serving, head=head, ok=False, problem=NOT_FOUND)
        status, doc, raw = self._get(
            f"repos/{quote(owner)}/{quote(name)}/compare/{quote(serving, safe='')}...{quote(head, safe='')}",
            timeout_s)
        if status != 200 or not doc:
            return CompareRead(repo=repo, serving=serving, head=head, ok=False,
                               problem=self._problem(status), detail=raw)
        gh = str(doc.get("status") or "unknown")
        ahead_of_base = int(doc.get("ahead_by") or 0)    # commits HEAD has that serving lacks
        behind_base = int(doc.get("behind_by") or 0)     # commits serving has that HEAD lacks
        flipped = {"identical": "identical", "ahead": "behind", "behind": "ahead",
                   "diverged": "diverged"}.get(gh, "unknown")
        return CompareRead(repo=repo, serving=serving, head=head, ok=True, status=flipped,
                           behind_by=ahead_of_base, ahead_by=behind_base)


    # ---------------------------------------------------------------- the graph (Git drawer)

    def branches(self, repo: str, *, timeout_s: float = 20.0) -> tuple[list[dict], str | None]:
        """[{name, sha}] for the repo, newest-activity first is not available here - alphabetical."""
        status, docs, _ = self._get_list(f"repos/{quote(repo)}/branches?per_page=60", timeout_s)
        if status != 200:
            return [], self._problem(status, repo)
        out = [{"name": str(d.get("name") or ""), "sha": str((d.get("commit") or {}).get("sha") or "")} for d in docs]
        return [b for b in out if b["name"] and b["sha"]], None

    def commits(self, repo: str, sha_or_branch: str, *, limit: int = 40, timeout_s: float = 20.0) -> tuple[list[dict], str | None]:
        """[{sha, parents[], subject, author, at}] walking back from a ref. One page."""
        limit = max(1, min(100, int(limit)))
        status, docs, _ = self._get_list(
            f"repos/{quote(repo)}/commits?sha={quote(sha_or_branch)}&per_page={limit}", timeout_s)
        if status != 200:
            return [], self._problem(status, repo)
        out = []
        for d in docs:
            c = d.get("commit") or {}
            author = (c.get("author") or {})
            out.append({
                "sha": str(d.get("sha") or ""),
                "parents": [str(p.get("sha") or "") for p in (d.get("parents") or []) if isinstance(p, dict)],
                "subject": str(c.get("message") or "").split("\n", 1)[0][:120],
                "author": str(author.get("name") or (d.get("author") or {}).get("login") or ""),
                "at": str(author.get("date") or ""),
            })
        return [c for c in out if c["sha"]], None

    # ---------------------------------------------------------------- the secrets scan

    def tree(self, repo: str, ref: str, *, timeout_s: float = 30.0) -> tuple[list[dict], str | None]:
        """[{path, size, sha}] for every blob on the ref (one recursive read; GitHub truncates
        past ~100k entries, far beyond any of ours)."""
        status, doc, _ = self._get(f"repos/{quote(repo)}/git/trees/{quote(ref, safe='')}?recursive=1", timeout_s)
        if status != 200 or not doc:
            return [], self._problem(status, repo, ref)
        out = [{"path": str(e.get("path") or ""), "size": int(e.get("size") or 0), "sha": str(e.get("sha") or "")}
               for e in (doc.get("tree") or []) if isinstance(e, dict) and e.get("type") == "blob"]
        return out, None

    def blob(self, repo: str, sha: str, *, timeout_s: float = 30.0) -> bytes | None:
        """The bytes of one blob (base64 through the API), or None."""
        import base64
        status, doc, _ = self._get(f"repos/{quote(repo)}/git/blobs/{quote(sha, safe='')}", timeout_s)
        if status != 200 or not doc or doc.get("encoding") != "base64":
            return None
        try:
            return base64.b64decode(str(doc.get("content") or "").replace("\n", ""))
        except Exception:  # noqa: BLE001
            return None
