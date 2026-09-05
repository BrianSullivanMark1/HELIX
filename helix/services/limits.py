"""Limits — recognising a plan or rate limit in failure text, and the pause it earns.

READ_ME/DREAM_MIND.md §13, Brian's rule: "If the dream state runs out of limit, it should have a neat
and easy way to handle this. The solution is NOT to drop down to a low-end model." So a limit never
degrades the night — it PAUSES it. This module is the one place that decides what a limit looks like
(`looks_like_limit`), what the provider said about when it lifts (`reset_hint`), and how long to wait
before the next cheap probe (`backoff_minutes`: 20, then 30, then 45, then every 60). The dream session
(services/dream.py) owns the pause itself; the research faculty may import these too, so every leg of
the night reads a limit the same way.

Pure functions, no I/O, no state: the text of a lane failure, a coder's error, or an exception from a
reflect/research turn goes in; a yes/no, a phrase, or a number of minutes comes out.

`scrub_secrets` lives here too — the one shared redactor the night's material goes through before it
is fenced into a prompt (the week's user turns, the log tail, the backlog, the research trail): a key
the user pasted in chat, or a credential a call_api error line echoed, must never ride into a reflect
prompt from where the model could carry it into a search query the journal keeps verbatim.
"""
from __future__ import annotations

import re

# The phrases a provider, an SDK, or the CLI uses when the plan is exhausted or the service is at
# capacity. Case-insensitive substrings — except the bare status code, which must stand alone (a
# "1429-line file" is not a rate limit).
_LIMIT_PHRASES: tuple[str, ...] = (
    "rate limit", "rate_limit", "ratelimit", "usage limit", "hit your limit", "limit reached",
    "quota", "overloaded", "resets at", "try again in", "too many requests", "capacity",
)
_STATUS_429_RE = re.compile(r"(?<![\d.])429(?![\d.])")

# "resets at 3pm", "will reset at 3:00 PM (America/New_York)", "try again in 2 hours",
# "resets in 45 minutes" — the phrase a person wants to hear, cut at the sentence's natural end.
_RESET_RE = re.compile(
    r"(?i)\b((?:(?:will|should)\s+)?resets?\s+(?:at|in|on)\s+[^.;\n)]{1,60}|try\s+again\s+in\s+[^.;\n)]{1,40})"
)

# The backoff between probes while paused (minutes): 20, then 30, then 45, then every 60 — capped,
# never longer, so a lifted limit is noticed within the hour whatever the provider said.
LIMIT_BACKOFF_MINUTES: tuple[int, ...] = (20, 30, 45, 60)
# Three separate pauses in one night end the session early (journaled): a plan that is out of room
# never spins all night on probes.
MAX_LIMIT_PAUSES = 3


def looks_like_limit(text: str) -> bool:
    """Does this failure text read as a plan/rate limit or a provider at capacity? Case-insensitive
    over the provider phrases; the bare 429 counts only when it stands alone."""
    low = (text or "").casefold()
    if not low:
        return False
    if any(phrase in low for phrase in _LIMIT_PHRASES):
        return True
    return _STATUS_429_RE.search(low) is not None


def reset_hint(text: str) -> str:
    """The "resets at 3pm" / "try again in 2 hours" phrase in the text, or "" when it says nothing
    about when the limit lifts. One phrase, trimmed, in the provider's own words."""
    m = _RESET_RE.search(text or "")
    if m is None:
        return ""
    return " ".join(m.group(1).split()).rstrip(" ,:")


def backoff_minutes(retry: int) -> int:
    """How long the n-th probe (1-based) waits while paused: 20, 30, 45, then 60 for every later
    one. A retry below 1 reads as the first."""
    n = max(1, int(retry))
    return LIMIT_BACKOFF_MINUTES[min(n, len(LIMIT_BACKOFF_MINUTES)) - 1]


# ----- secrets in text the night reads -----
# The shapes a credential takes in the wild: Anthropic keys and setup-tokens, GitHub tokens, Slack
# tokens, AWS access keys, a bearer/basic value after its scheme word, a header or query-param value
# after a secret-ish name, and a long hex run (a session id, a hash used as a token). Each is replaced
# whole by the mask; the scheme word / parameter name is kept so the text still reads.
_MASK = "•••"
_SECRET_WHOLE_RES: tuple[re.Pattern, ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprse]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*[0-9])[0-9a-fA-F]{32,}\b"),
)
_SECRET_NAMED_RES: tuple[re.Pattern, ...] = (
    re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{12,})"),
    re.compile(r"(?i)\b(authorization|x-api-key|api[-_]?key|apikey|access[-_]?token|auth[-_]?token|"
               r"oauth[-_]?token|secret|password|passwd|pwd|token)(\s*[:=]\s*[\"']?)([^\s\"'&,;]{6,})"),
)


def scrub_secrets(text: str) -> str:
    """Redact anything that reads as a credential in `text`, keeping the words around it. Applied to
    the night's material before it is fenced, and to every research-trail line before it is
    journaled. Empty in, empty out; ordinary prose and URLs pass through untouched."""
    out = str(text or "")
    if not out:
        return out
    for pattern in _SECRET_WHOLE_RES:
        out = pattern.sub(_MASK, out)
    out = _SECRET_NAMED_RES[0].sub(lambda m: f"{m.group(1)} {_MASK}", out)
    out = _SECRET_NAMED_RES[1].sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", out)
    return out


__all__ = ["LIMIT_BACKOFF_MINUTES", "MAX_LIMIT_PAUSES", "backoff_minutes", "looks_like_limit",
           "reset_hint", "scrub_secrets"]
