"""Connections — the data model for a build's external API credentials.

A build (app, task, or agent) that needs an outside service DECLARES the keys it needs; HELIX collects
the values from the user and injects them at run time (as env vars for tasks/apps, and via the read-only
call_api tool for agents and the orb). Pure data — no I/O. The secret VALUES never live here.
"""
from __future__ import annotations

import re
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
    """A known external service the orb/agents can read via call_api, using stored credential(s). Each
    field is a credential the user pastes, kept under the same env-var name a built app would read — so
    connecting a service once (the just-in-time key panel, opened in conversation) makes it available
    to call_api AND to any build that declares that key. `auth` says how the stored value(s) attach to a request: a list of
    (header-name, template) pairs where {ENV_NAME} is filled with that field's value — a Bearer token for
    most services, or several custom headers for ones like Alpaca that need a key id + secret."""

    id: str
    label: str
    hosts: tuple[str, ...]              # API hostnames these credentials authenticate
    fields: tuple[Connection, ...]      # the credential field(s) to collect (env-var name + label + hint)
    auth: tuple[tuple[str, str], ...]   # request headers to attach; {ENV_NAME} → that field's value
    # Some services (SAM.gov) authenticate via a query parameter instead of a header. Same contract:
    # (param-name, template) pairs attached SERVER-SIDE after the allow-list check, so the key is
    # never chosen by or returned to the model.
    query: tuple[tuple[str, str], ...] = ()
    # Extra STATIC request headers this service needs (content negotiation, API-version pins — never
    # secrets, so no templates). Applied over call_api's defaults before the auth headers.
    headers: tuple[tuple[str, str], ...] = ()
    # Known-good request shapes, written for the MODEL. call_api appends this to an HTTP error from
    # this service so a wrong-path guess self-corrects in the same turn instead of dead-ending.
    recipe: str = ""

    @property
    def env(self) -> str:
        """The primary credential's env-var name — the 'is it connected?' key and a back-compat accessor."""
        return self.fields[0].key

    @property
    def hint(self) -> str:
        return self.fields[0].hint


# The services call_api will authenticate to. Deliberately small + explicit: call_api refuses any host
# NOT in this list, so it can never be steered (e.g. by injected content) to reach an arbitrary or
# internal address or to leak a token. Extend this tuple to add a service.
KNOWN_SERVICES: tuple[Service, ...] = (
    Service(
        "slack", "Slack", ("slack.com",),
        fields=(Connection("SLACK_TOKEN", "Slack token", "xoxp-… (user) or xoxb-… (bot)"),),
        auth=(("Authorization", "Bearer {SLACK_TOKEN}"),),
    ),
    Service(
        "github", "GitHub", ("api.github.com", "github.com"),
        fields=(Connection("GITHUB_TOKEN", "GitHub token", "ghp_… or github_pat_…"),),
        auth=(("Authorization", "Bearer {GITHUB_TOKEN}"),),
    ),
    Service(
        "alpaca", "Alpaca", ("api.alpaca.markets", "paper-api.alpaca.markets", "data.alpaca.markets"),
        fields=(
            Connection("ALPACA_API_KEY", "Alpaca API key ID", "PK… (paper) or AK… (live)"),
            Connection("ALPACA_SECRET_KEY", "Alpaca secret key", "the matching secret"),
        ),
        auth=(
            ("APCA-API-KEY-ID", "{ALPACA_API_KEY}"),
            ("APCA-API-SECRET-KEY", "{ALPACA_SECRET_KEY}"),
        ),
    ),
    Service(
        # "sam.gov" covers the site API host AND (by suffix) api.sam.gov / api-alpha.sam.gov. The
        # documented public host api.sam.gov black-holed to empty 404s in July 2026 (verified from a
        # real browser too), while the sam.gov SGS search API — the one the website itself runs on —
        # answers anonymously; both stay allow-listed so either path works whenever GSA has it up.
        "sam", "SAM.gov", ("sam.gov",),
        fields=(Connection("SAM_API_KEY", "SAM.gov API key",
                           "public API key from your SAM.gov account profile"),),
        auth=(),
        query=(("api_key", "{SAM_API_KEY}"),),  # SAM.gov authenticates via query param, not a header
        # The SGS endpoint 406s unless application/hal+json is explicitly acceptable; listing
        # application/json and */* after it keeps the documented api.sam.gov endpoints happy too.
        headers=(("Accept", "application/hal+json, application/json;q=0.9, */*;q=0.8"),),
        recipe=(
            "live site search https://sam.gov/api/prod/sgs/v1/search/?index=opp&q=KEYWORDS&page=0"
            "&size=25&sort=-modifiedDate&mode=search&is_active=true (filters: naics=541511, "
            "notice_type=r sources-sought | p presolicitation | o solicitation | k combined, "
            "set_aside=SBA, 8A, SDVOSBC, WOSB); or the documented https://api.sam.gov/opportunities/v2/search"
            "?postedFrom=MM/dd/yyyy&postedTo=MM/dd/yyyy&limit=25&title=KEYWORDS (both dates "
            "required, span under one year). If one path errors, try the other."
        ),
    ),
)


def looks_like_mispaste(value: str) -> str:
    """A plain-words warning when a pasted credential is obviously NOT a key — today, a web address
    (the mis-paste that actually happened: an API's endpoint URL pasted where its key belongs, which
    then rides every request as a bogus token). Empty string when the value looks plausible. Advisory
    only — the panels warn once and let the user insist, because an unusual-but-real credential must
    never be hard-refused (some real secrets contain spaces, dots, or other odd shapes)."""
    v = (value or "").strip().lower()
    if v.startswith(("http://", "https://", "www.")) or "://" in v:
        return "That looks like a web address, not a key — paste the API key itself."
    # The scheme-less rendering of the same mis-paste ("api.sam.gov/opportunities/v2/search", as docs
    # and error messages often print it): a bare dotted host followed by a /path. The slash is
    # required on purpose — dotted-but-slashless values (a JWT's segments) stay unflagged.
    if re.fullmatch(r"([a-z0-9-]+\.)+[a-z]{2,}/\S*", v):
        return "That looks like a web address, not a key — paste the API key itself."
    return ""


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
