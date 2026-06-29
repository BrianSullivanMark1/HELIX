"""ConnectionsService — the Forge's credential capability.

Reads what API keys a build DECLARED it needs (a connections.json the coder writes), stores the values
the user enters (locally, never in the build's folder/git or the browser), injects them as environment
variables when a task/app runs, and exposes a read-only call_api the orb + agents use to read a connected
service. One contract across every build kind, so anything HELIX builds that needs a key just works once
the user pastes it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from helix.domain.connections import KNOWN_SERVICES, Connection, service_for_url
from helix.logging_setup import get_logger
from helix.ports.stores import SettingsStore
from helix.services.builds import BuildService

_LOG = get_logger("connections")

CONNECTIONS_FILE = "connections.json"  # a build declares its needed keys here
_MAX_BODY = 200_000                    # cap a call_api response so a huge payload can't blow up context


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never auto-follow a redirect on an authenticated call_api request. urllib would otherwise re-send
    the Authorization header to the redirect target WITHOUT re-checking it against the service allow-list —
    so a 3xx (e.g. an open-redirect or an attacker-induced one) could bounce the user's token to an
    arbitrary or internal host, in cleartext. We refuse all redirects; a genuine 3xx from a read API is
    surfaced as an error instead of being followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


# An opener with redirects disabled. build_opener replaces the default HTTPRedirectHandler with ours.
_OPENER = urllib.request.build_opener(_NoRedirect)


class ConnectionsService:
    def __init__(self, builds: BuildService, secrets: SettingsStore) -> None:
        self._builds = builds
        self._secrets = secrets  # a JSON store dedicated to secret values (data/helix_secrets.json)

    # ----- what a build declared it needs -----
    def declared(self, slug: str) -> list[Connection]:
        """The keys a build asked for (its connections.json). Empty if it needs none or the file is bad."""
        path = self._builds.workspace(slug) / CONNECTIONS_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out: list[Connection] = []
        for d in raw if isinstance(raw, list) else []:
            key = (d.get("key") if isinstance(d, dict) else "") or ""
            key = key.strip()
            if key:
                out.append(Connection(
                    key=key,
                    label=str(d.get("label") or key).strip(),
                    hint=str(d.get("hint") or "").strip(),
                ))
        return out

    def needs_connection(self, slug: str) -> bool:
        return bool(self.declared(slug))

    def missing(self, slug: str) -> list[Connection]:
        """Declared keys that have no value set yet (so the UI can flag 'not connected')."""
        return [c for c in self.declared(slug) if not self.value(c.key)]

    # ----- values (stored globally by env-var name; never in the build folder / git / browser) -----
    def value(self, key: str) -> str:
        return (self._secrets.get(key) or "").strip()

    def set_value(self, key: str, value: str) -> None:
        self._secrets.set(key, (value or "").strip())

    def env_for(self, slug: str) -> dict[str, str]:
        """The env vars to inject when this build runs: each SET declared key → its value."""
        env: dict[str, str] = {}
        for c in self.declared(slug):
            v = self.value(c.key)
            if v:
                env[c.key] = v
        return env

    # ----- call_api: read-only access to a CONNECTED service, for the orb + agents -----
    def call_api(self, url: str, timeout: float = 20.0) -> str:
        """GET `url` from a connected service with the user's saved token attached, and return the body.

        Locked down on purpose: https only, GET only, and ONLY hosts of a known connected service — so it
        can't be steered (e.g. by injected message content) to reach an arbitrary or internal host, and it
        can't write anything. The token is attached here and never returned to the model."""
        url = (url or "").strip()
        if not url.lower().startswith("https://"):
            return "call_api error: only https URLs are allowed."
        svc = service_for_url(url)
        if svc is None:
            names = ", ".join(s.label for s in KNOWN_SERVICES)
            return (f"call_api error: that host isn't a connectable service. I can only read: {names}.")
        token = self.value(svc.env)
        if not token:
            return (f"call_api error: {svc.label} isn't connected yet. Ask the user to add the "
                    f"{svc.label} token in Settings → Connections (or the build's Connect panel).")
        req = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + token, "User-Agent": "HELIX", "Accept": "*/*"},
        )
        try:
            # _OPENER refuses redirects, so the token can never be re-sent to a host the allow-list didn't
            # clear (no SSRF / credential exfiltration via a 3xx, no http downgrade).
            with _OPENER.open(req, timeout=timeout) as r:
                return r.read(_MAX_BODY).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(2000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                detail = ""
            return f"call_api {svc.label} returned HTTP {e.code}. {detail}".strip()
        except Exception as e:  # noqa: BLE001 - never raise into the tool loop
            _LOG.warning("call_api failed for %s: %s", svc.label, e)
            return f"call_api error reaching {svc.label}: {e}"
