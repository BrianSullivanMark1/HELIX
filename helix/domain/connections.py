"""Connections — the data model for a build's external API credentials.

A build (app, task, or agent) that needs an outside service DECLARES the keys it needs; HELIX collects
the values from the user and injects them at run time (as env vars for tasks/apps, and via the read-only
call_api tool for agents and the orb). Pure data — no I/O. The secret VALUES never live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Connection:
    """One API key a build says it needs. `key` is the environment-variable name the build reads."""

    key: str          # e.g. "SLACK_TOKEN"
    label: str        # e.g. "Slack token"
    hint: str = ""    # e.g. "starts with xoxp-"


@dataclass(frozen=True)
class Service:
    """A known external service the orb/agents can read via call_api, using a stored token. The token is
    kept under `env` (the same name a built app would read from the environment), so connecting a service
    once makes it available to both call_api and any build that declares that key."""

    id: str
    label: str
    env: str                 # env-var name the token is stored under (e.g. "SLACK_TOKEN")
    hosts: tuple[str, ...]   # API hostnames this token authenticates
    hint: str                # placeholder for the entry field


# The services call_api will authenticate to. Deliberately small + explicit: call_api refuses any host
# NOT in this list, so it can never be steered (e.g. by injected content) to reach an arbitrary or
# internal address or to leak the token. Extend this tuple to add a service.
KNOWN_SERVICES: tuple[Service, ...] = (
    Service("slack", "Slack", "SLACK_TOKEN", ("slack.com",), "xoxp-… (user) or xoxb-… (bot)"),
    Service("github", "GitHub", "GITHUB_TOKEN", ("api.github.com", "github.com"),
            "ghp_… or github_pat_…"),
)


def service_for_url(url: str) -> Service | None:
    """The known service a URL belongs to (matching host or a subdomain of it), or None."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for svc in KNOWN_SERVICES:
        if any(host == h or host.endswith("." + h) for h in svc.hosts):
            return svc
    return None
