"""Slack read-only client + activity digest for the Enterprise tab.

A hand-written `urllib` client (mirrors `helix/brokers/alpaca.py`) over the Slack Web API. It is
**read-only**: it pulls who-you-are, the conversations you're in, and recent messages, then assembles
a compact "what needs your attention" digest (mentions, DMs, busy channels). Posting messages is NOT
implemented here — any outward action would be a deliberate, gated feature later.

Auth: a Slack **user token** (`xoxp-…`) saved in settings (git-ignored). Create a Slack app at
api.slack.com/apps, add the User Token Scopes below, install it to your workspace, and paste the
"User OAuth Token". Scopes needed: `channels:history channels:read groups:history groups:read
im:history im:read mpim:history mpim:read users:read`.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from helix.core.settings import AppSettings

SLACK_TOKEN_SETTING = "enterprise_slack_token"
SLACK_API_BASE = "https://slack.com/api/"

# Scopes shown in the UI help so the user can set the token up correctly.
SLACK_USER_SCOPES = (
    "channels:history channels:read groups:history groups:read "
    "im:history im:read mpim:history mpim:read users:read"
)


class SlackError(RuntimeError):
    pass


class SlackClient:
    def __init__(self, token: str, timeout_seconds: int = 20) -> None:
        self.token = (token or "").strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: AppSettings | None = None) -> "SlackClient":
        settings = settings or AppSettings()
        token = settings.get(SLACK_TOKEN_SETTING, "")
        if not token:
            raise SlackError("Slack is not configured. Save a Slack user token (xoxp-…) first.")
        return cls(token)

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One Slack Web API call. Slack returns 200 with `{ok: false, error: ...}` on failures, so
        the `ok` flag is the real status — not just the HTTP code."""
        url = SLACK_API_BASE + method
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        if query:
            url += "?" + query
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise SlackError(f"Slack API HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise SlackError(f"Slack connection failed: {error.reason}") from error
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError as error:
            raise SlackError("Slack returned invalid JSON.") from error
        if not data.get("ok", False):
            raise SlackError(f"Slack error: {data.get('error', 'unknown')}")
        return data

    def auth_test(self) -> dict[str, Any]:
        return self._call("auth.test")

    def list_conversations(self, types: str, limit: int = 200) -> list[dict[str, Any]]:
        data = self._call(
            "users.conversations",
            {"types": types, "exclude_archived": "true", "limit": str(limit)},
        )
        channels = data.get("channels", [])
        return channels if isinstance(channels, list) else []

    def conversations_history(self, channel: str, oldest: str, limit: int = 30) -> list[dict[str, Any]]:
        data = self._call(
            "conversations.history",
            {"channel": channel, "oldest": oldest, "limit": str(limit)},
        )
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    def users_info(self, user_id: str) -> dict[str, Any]:
        data = self._call("users.info", {"user": user_id})
        user = data.get("user", {})
        return user if isinstance(user, dict) else {}


def _display_name(user: dict[str, Any]) -> str:
    profile = user.get("profile", {}) or {}
    return (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user.get("id", "someone")
    )


def gather_slack_digest(
    client: SlackClient,
    *,
    lookback_hours: int = 24,
    max_channels: int = 25,
    max_messages: int = 30,
) -> dict[str, Any]:
    """Assemble a bounded, read-only "what needs my attention" digest. Best-effort: a per-channel
    failure is skipped, not fatal. Returns {me, team, mentions, dms, channel_activity, totals}."""
    auth = client.auth_test()
    me_id = auth.get("user_id", "")
    me_name = auth.get("user", "")
    team = auth.get("team", "")
    mention_token = f"<@{me_id}>"
    oldest = f"{time.time() - lookback_hours * 3600:.6f}"

    channels = client.list_conversations(
        types="public_channel,private_channel,im,mpim", limit=200
    )

    name_cache: dict[str, str] = {}

    def author_name(uid: str) -> str:
        if not uid:
            return "someone"
        if uid not in name_cache:
            try:
                name_cache[uid] = _display_name(client.users_info(uid))
            except SlackError:
                name_cache[uid] = uid
        return name_cache[uid]

    mentions: list[dict[str, Any]] = []
    dms: list[dict[str, Any]] = []
    channel_activity: list[dict[str, Any]] = []

    for channel in channels[:max_channels]:
        cid = channel.get("id", "")
        is_im = channel.get("is_im", False)
        if is_im:
            label = "a direct message with " + author_name(channel.get("user", ""))
        elif channel.get("is_mpim"):
            label = "a group message"
        else:
            clean = str(channel.get("name", cid)).replace("-", " ").replace("_", " ")
            label = f"the {clean} channel"
        try:
            messages = client.conversations_history(cid, oldest, limit=max_messages)
        except SlackError:
            continue
        # Skip messages I sent; count what others said.
        others = [m for m in messages if m.get("user") and m.get("user") != me_id and m.get("type") == "message"]
        if not others:
            continue
        channel_activity.append({"channel": label, "count": len(others), "is_dm": bool(is_im)})
        for message in others:
            text = str(message.get("text", "")).strip()
            entry = {
                "channel": label,
                "author": author_name(message.get("user", "")),
                "text": text,
                "ts": message.get("ts", ""),
            }
            if is_im:
                dms.append(entry)
            elif mention_token in text:
                mentions.append(entry)

    channel_activity.sort(key=lambda row: row["count"], reverse=True)
    return {
        "me": me_name,
        "team": team,
        "lookback_hours": lookback_hours,
        "mentions": mentions,
        "dms": dms,
        "channel_activity": channel_activity,
        "totals": {
            "mentions": len(mentions),
            "dms": len(dms),
            "active_channels": len(channel_activity),
        },
    }


def format_slack_digest(digest: dict[str, Any], max_items: int = 10) -> str:
    """Plain, human, symbol-free rendering of the digest — readable on screen, safe to read aloud, and
    good input for Claude. No markdown, hashes, middle-dots, ellipses, or Slack mention codes."""
    if not digest:
        return "Slack is not connected."
    totals = digest["totals"]
    window = _window_phrase(digest.get("lookback_hours", 24))
    lines = [
        f"Slack for {digest.get('me', 'you')} on {digest.get('team', '')}, over {window}.",
        f"You have {_plural(totals['mentions'], 'mention')} and "
        f"{_plural(totals['dms'], 'direct message')}, across "
        f"{_plural(totals['active_channels'], 'active channel')}.",
    ]
    if digest.get("mentions"):
        lines.append("")
        lines.append("People who mentioned you:")
        for m in digest["mentions"][:max_items]:
            lines.append(f"  In {m['channel']}, {m['author']} said, {_clip(m['text'])}")
    if digest.get("dms"):
        lines.append("")
        lines.append("Direct messages:")
        for m in digest["dms"][:max_items]:
            lines.append(f"  {m['author']} said, {_clip(m['text'])}")
    return "\n".join(lines)


def _window_phrase(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return "the last day" if days == 1 else f"the last {days} days"
    return f"the last {hours} hours"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# Slack wraps user mentions, channel links and URLs in angle brackets — strip them to plain words so
# the text reads cleanly on screen and aloud (no "<@U04NGUNP5F0>" garbage).
_RE_USER = re.compile(r"<@[^>]+>")  # any "@person" mention code, fully removed
_RE_CHANNEL = re.compile(r"<#[CG][A-Z0-9]+\|([^>]+)>")
_RE_LINK_LABEL = re.compile(r"<[^|>]+\|([^>]+)>")
_RE_LINK_BARE = re.compile(r"<([^>]+)>")


def _clean_slack_text(text: str) -> str:
    text = str(text)
    text = _RE_USER.sub("", text)            # drop "@person" id codes
    text = _RE_CHANNEL.sub(r"\1", text)      # channel link -> its name
    text = _RE_LINK_LABEL.sub(r"\1", text)   # <url|label> -> label
    text = _RE_LINK_BARE.sub(r"\1", text)    # <url> -> url
    text = text.replace("&amp;", "and").replace("&lt;", "").replace("&gt;", "")
    return " ".join(text.split())


def _clip(text: str, width: int = 180) -> str:
    text = _clean_slack_text(text)
    return text if len(text) <= width else text[:width].rstrip() + " and so on"
