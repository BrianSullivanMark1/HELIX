"""Google Cloud Secret Manager through the person's own gcloud login - by REST, like the radio's
bucket: ONE hidden `gcloud auth print-access-token` spawn (cached ~50 min), then plain HTTPS to
secretmanager.googleapis.com. The first cut spawned `gcloud secrets ...` per call: on Windows each
spawn is seconds, a prompt from gcloud (an API not enabled) waited on stdin forever, and a timeout
could not kill gcloud's own python behind the .cmd - the vault sat on "Opening..." (Brian,
2026-09-22 late). REST answers in milliseconds and never asks a question.

A value travels only in the HTTPS body to Google (base64, as the API wants) - never on a command
line, never into a log line. Nothing here reads a value back: the API has an `:access` verb and
this adapter does not call it.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from helix.ports.secrets import Secret, SecretVersion, StoreError

NOT_INSTALLED = "The Google Cloud SDK (gcloud) is not on this PC, so the vault cannot open."
NOT_SIGNED_IN = "gcloud is not signed in on this PC. Run `gcloud auth login` in a terminal, then try again."
NO_PERMISSION = "Your Google account may not touch this project's secrets (permission denied). Ask for the Secret Manager Admin role on the project."
API_OFF = "The Secret Manager API is not enabled on {project}. Run `gcloud services enable secretmanager.googleapis.com --project {project}` once, then try again."
NOT_FOUND = "There is no secret called {name} in this project."
EXISTS = "A secret called {name} already exists. Rotate it instead."
BAD_OUTPUT = "Google answered, but not in a shape HELIX understands."
OFFLINE = "Could not reach Google Cloud from this PC ({why})."
NAME_RULE = "A secret's name is letters, digits, dashes and underscores, up to 255 of them."

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
API = "https://secretmanager.googleapis.com/v1"
TOKEN_TTL_S = 50 * 60


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    err: str


Runner = Callable[[list[str], float], Ran]
# (method, url, token, body-or-None, timeout) -> (status, body text); never raises for HTTP errors
Http = Callable[[str, str, str, dict | None, float], tuple[int, str]]

_QUIET = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _real_runner(argv: list[str], timeout_s: float) -> Ran:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace", creationflags=_QUIET, stdin=subprocess.DEVNULL)
        return Ran(p.returncode, p.stdout or "", p.stderr or "")
    except FileNotFoundError:
        return Ran(127, "", "not found")
    except subprocess.TimeoutExpired:
        return Ran(124, "", "timeout")
    except OSError as exc:  # noqa: BLE001
        return Ran(1, "", str(exc))


def _real_http(method: str, url: str, token: str, body: dict | None, timeout_s: float) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                          **({"Content-Type": "application/json"} if data is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return e.code, ""
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def _short(name: str) -> str:
    """`projects/123/secrets/NAME[/versions/4]` -> NAME, or the version number as text."""
    return name.rsplit("/", 1)[-1] if name else ""


def _version_of(doc: dict) -> SecretVersion:
    num = _short(str(doc.get("name") or ""))
    try:
        n = int(num)
    except ValueError:
        n = 0
    return SecretVersion(number=n, state=str(doc.get("state") or "").lower() or "enabled",
                         created=str(doc.get("createTime") or ""))


def classify(status: int, body: str, *, name: str = "", project: str = "") -> str | None:
    """The sentence an answer deserves, or None when it went fine."""
    if 200 <= status < 300:
        return None
    text = (body or "").lower()
    if status == 0:
        return OFFLINE.format(why=(body or "no answer")[:120])
    if status == 401:
        return NOT_SIGNED_IN
    if status == 403:
        if "has not been used" in text or "is disabled" in text or "not enabled" in text:
            return API_OFF.format(project=project or "the project")
        return NO_PERMISSION
    if status == 409:
        return EXISTS.format(name=name or "that")
    if status == 404:
        return NOT_FOUND.format(name=name or "that")
    try:
        msg = json.loads(body).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        msg = ""
    return (msg or f"Google answered {status}.")[:200]


class GcpSecrets:
    def __init__(self, project: str | Callable[[], str | None], *, runner: Runner | None = None,
                 http: Http | None = None, gcloud: str = "gcloud", workers: int = 8) -> None:
        self._project = project
        self._run = runner or _real_runner
        self._http = http or _real_http
        self._gcloud = shutil.which(gcloud) or gcloud
        self._workers = max(1, workers)
        self._lock = threading.Lock()
        self._token: tuple[str, float] | None = None

    # ---------------------------------------------------------------- plumbing

    def project(self) -> str:
        p = self._project() if callable(self._project) else self._project
        return str(p or "").strip()

    def available(self) -> tuple[bool, str | None]:
        if self._run is _real_runner and shutil.which(self._gcloud) is None:
            return False, NOT_INSTALLED
        return True, None

    def token(self, *, fresh: bool = False) -> str:
        """The person's own access token, from gcloud, cached; raises the signed-in sentence."""
        with self._lock:
            if not fresh and self._token and time.time() < self._token[1]:
                return self._token[0]
        ran = self._run([self._gcloud, "auth", "print-access-token"], 30.0)
        if ran.rc == 127:
            raise StoreError(NOT_INSTALLED)
        tok = (ran.out or "").strip().splitlines()[0].strip() if ran.rc == 0 and (ran.out or "").strip() else ""
        if not tok:
            raise StoreError(NOT_SIGNED_IN)
        with self._lock:
            self._token = (tok, time.time() + TOKEN_TTL_S)
        return tok

    def _call(self, method: str, path: str, *, body: dict | None = None, name: str = "", timeout_s: float = 30.0) -> dict:
        url = f"{API}/projects/{self.project()}/{path}"
        status, text = self._http(method, url, self.token(), body, timeout_s)
        if status == 401:   # a token that expired early: once more with a fresh one
            status, text = self._http(method, url, self.token(fresh=True), body, timeout_s)
        why = classify(status, text, name=name, project=self.project())
        if why:
            raise StoreError(why)
        if not (text or "").strip():
            return {}
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise StoreError(BAD_OUTPUT) from exc
        return doc if isinstance(doc, dict) else {}

    def _paged(self, path: str, key: str, *, name: str = "") -> list[dict]:
        out: list[dict] = []
        token = ""
        for _ in range(50):
            doc = self._call("GET", f"{path}?pageSize=250" + (f"&pageToken={token}" if token else ""), name=name)
            out.extend(d for d in (doc.get(key) or []) if isinstance(d, dict))
            token = str(doc.get("nextPageToken") or "")
            if not token:
                break
        return out

    # ---------------------------------------------------------------- reads (never a value)

    def versions(self, name: str) -> list[SecretVersion]:
        docs = self._paged(f"secrets/{name}/versions", "versions", name=name)
        out = [_version_of(d) for d in docs]
        out.sort(key=lambda v: v.number, reverse=True)
        return out

    def list(self) -> list[Secret]:
        docs = self._paged("secrets", "secrets")
        heads: list[Secret] = []
        for d in docs:
            labels = {str(k): str(v) for k, v in (d.get("labels") or {}).items()} if isinstance(d.get("labels"), dict) else {}
            heads.append(Secret(name=_short(str(d.get("name") or "")), created=str(d.get("createTime") or ""), labels=labels))
        if not heads:
            return []

        def fill(s: Secret) -> Secret:
            try:
                vs = self.versions(s.name)
            except StoreError:
                return s
            live = [v for v in vs if v.state != "destroyed"]
            return Secret(name=s.name, created=s.created, labels=s.labels,
                          latest=(live[0] if live else None), versions_n=len(vs))

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            filled = list(pool.map(fill, heads))
        filled.sort(key=lambda s: s.name.lower())
        return filled

    # ---------------------------------------------------------------- writes (a value goes in the body, once)

    @staticmethod
    def _payload(value: str) -> dict:
        return {"payload": {"data": base64.b64encode(value.encode("utf-8")).decode("ascii")}}

    def create(self, name: str, value: str, labels: dict[str, str]) -> None:
        if not NAME_RE.match(name):
            raise StoreError(NAME_RULE)
        body: dict = {"replication": {"automatic": {}}}
        clean = {k: v for k, v in labels.items() if k and v}
        if clean:
            body["labels"] = clean
        self._call("POST", f"secrets?secretId={name}", body=body, name=name, timeout_s=60.0)
        self._call("POST", f"secrets/{name}:addVersion", body=self._payload(value), name=name, timeout_s=60.0)

    def add_version(self, name: str, value: str) -> int:
        doc = self._call("POST", f"secrets/{name}:addVersion", body=self._payload(value), name=name, timeout_s=60.0)
        return _version_of(doc).number if doc else 0

    def set_version_state(self, name: str, number: int, enabled: bool) -> None:
        verb = "enable" if enabled else "disable"
        self._call("POST", f"secrets/{name}/versions/{int(number)}:{verb}", body={}, name=name)

    def destroy(self, name: str) -> None:
        self._call("DELETE", f"secrets/{name}", name=name, timeout_s=60.0)
