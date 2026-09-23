"""Firebase Authentication's authorized domains, through Google's own API (Identity Toolkit v2),
with the person's gcloud token - the same way the vault reaches Secret Manager.

One verb, on purpose: ADD a domain. The list is project-wide (every app's sign-in reads it), so
the one thing that could lock people out - removing an entry - has no method here. Adding is
idempotent: a domain already there is reported as such and the list is not written.

  GET   https://identitytoolkit.googleapis.com/admin/v2/projects/{p}/config           -> authorizedDomains[]
  PATCH https://identitytoolkit.googleapis.com/admin/v2/projects/{p}/config?updateMask=authorizedDomains
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from typing import Callable

from helix.adapters.gcp_secret_manager import Http, Ran, Runner, _real_http, _real_runner

API = "https://identitytoolkit.googleapis.com/admin/v2"
TOKEN_TTL_S = 50 * 60
NOT_SIGNED_IN = "No gcloud account is signed in on this PC, so HELIX cannot reach Firebase as you."
NO_RIGHTS = "{who} may not change Firebase Authentication settings in {project} (needs the Firebase Admin role)."
API_OFF = "The Identity Toolkit API is not enabled in {project}. Enable it once in the Cloud Console, then try again."


class AuthError(RuntimeError):
    pass


class FirebaseAuthDomains:
    def __init__(self, project: str, *, runner: Runner | None = None, http: Http | None = None,
                 gcloud: str = "gcloud", identity: Callable[[], str | None] | None = None) -> None:
        self._project = project
        self._run = runner or _real_runner
        self._http = http or _real_http
        self._gcloud = shutil.which(gcloud) or gcloud
        self._identity = identity or (lambda: None)
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None

    def token(self, *, fresh: bool = False) -> str:
        with self._lock:
            if not fresh and self._token and time.time() < self._token[1]:
                return self._token[0]
        ran: Ran = self._run([self._gcloud, "auth", "print-access-token"], 30.0)
        tok = (ran.out or "").strip().splitlines()[0].strip() if ran.rc == 0 and (ran.out or "").strip() else ""
        if not tok:
            raise AuthError(NOT_SIGNED_IN)
        with self._lock:
            self._token = (tok, time.time() + TOKEN_TTL_S)
        return tok

    def _call(self, method: str, query: str = "", body: dict | None = None) -> dict:
        url = f"{API}/projects/{self._project}/config{query}"
        status, text = self._http(method, url, self.token(), body, 30.0)
        if status == 401:
            status, text = self._http(method, url, self.token(fresh=True), body, 30.0)
        if status == 403:
            low = (text or "").lower()
            if "not been used" in low or "is disabled" in low or "not enabled" in low:
                raise AuthError(API_OFF.format(project=self._project))
            raise AuthError(NO_RIGHTS.format(who=self._identity() or "This account", project=self._project))
        if status == 0:
            raise AuthError(f"Firebase could not be reached: {text[:160]}")
        if status >= 400:
            raise AuthError(f"Firebase answered {status}: {text[:200]}")
        try:
            doc = json.loads(text) if (text or "").strip() else {}
        except ValueError as exc:
            raise AuthError("Firebase answered something that is not JSON.") from exc
        return doc if isinstance(doc, dict) else {}

    def domains(self) -> list[str]:
        return [str(d) for d in (self._call("GET").get("authorizedDomains") or [])]

    def add(self, domain: str) -> tuple[bool, str]:
        """(added, sentence). Never removes; a domain already listed is left alone."""
        domain = (domain or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
        if not domain:
            return False, "No domain to add."
        have = self.domains()
        if domain in (d.lower() for d in have):
            return False, f"{domain} is already on Firebase's authorized domains."
        self._call("PATCH", "?updateMask=authorizedDomains", {"authorizedDomains": [*have, domain]})
        return True, f"{domain} added to Firebase's authorized domains ({len(have) + 1} on the list)."
