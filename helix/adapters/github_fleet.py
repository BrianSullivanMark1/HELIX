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

NO_TOKEN = "No GitHub token is connected. Connect GitHub in Settings to read repositories."
NOT_FOUND = "GitHub says that repository or branch does not exist, or the token cannot see it."
RATE_LIMITED = "GitHub is rate-limiting this token right now. Try again in a few minutes."
UNREACHABLE = "GitHub could not be reached."
BAD_OUTPUT = "GitHub answered, but not in a shape HELIX understands."

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

    @staticmethod
    def _problem(status: int) -> str:
        if status == -1:
            return NO_TOKEN
        if status in (403, 429):
            return RATE_LIMITED
        if status == 404:
            return NOT_FOUND
        if status == 0:
            return UNREACHABLE
        return BAD_OUTPUT

    # ---------------------------------------------------------------- reads

    def read_head(self, repo: str, branch: str, *, timeout_s: float = 20.0) -> RepoRead:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return RepoRead(repo=repo, branch=branch, ok=False, problem=NOT_FOUND,
                            detail="repo must be 'owner/name'")
        status, doc, raw = self._get(
            f"repos/{quote(owner)}/{quote(name)}/commits/{quote(branch, safe='')}", timeout_s)
        if status != 200 or not doc:
            return RepoRead(repo=repo, branch=branch, ok=False, problem=self._problem(status),
                            detail=raw)
        sha = str(doc.get("sha") or "")
        commit = doc.get("commit") or {}
        message = str(commit.get("message") or "")
        when = ((commit.get("committer") or {}).get("date")
                or (commit.get("author") or {}).get("date"))
        if not sha:
            return RepoRead(repo=repo, branch=branch, ok=False, problem=BAD_OUTPUT, detail=raw)
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
