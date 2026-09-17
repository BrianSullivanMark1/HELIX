"""GcloudFleet — reads Cloud Run through the gcloud CLI under the user's own login.

The DESKTOP profile's FleetReader (HELIX_MARK1_PLAN.md §7.2). No new credentials, no service account:
whatever `gcloud auth login` gave the user is what this reads with. Read-only by construction - there
is no code path in this module that mutates anything, and the only commands it knows are
`services describe`, `revisions list` and `--version`.

TWO RULES FROM THE REST OF THE CODEBASE, KEPT:

  1. A CLI problem must never be reported as a credential problem (claude_code_cli.py's rule). A
     missing gcloud, a not-logged-in gcloud, a 403 and a timeout are four different sentences, and
     `classify()` names which one actually happened rather than letting a good login take the blame.
  2. Nothing raises across the port (CadEngine's rule). Every path returns a CellRead.

PROVENANCE, THREE WAYS. "What commit is serving?" is answered from, in order:
  a. the revision's `version=` label (dev.ps1 stamps this on WMS/MRP since 2026-09-08, §6.6);
  b. the app's own /api/health `version` field (MES's backend/deploy.ps1 has baked this in since
     August - the console verifies against it after every deploy);
  c. nothing - `commit=None`, honestly. Never a guess.
(b) is the one HTTP call this adapter makes, and it is fenced: only the service's own `*.run.app`
host, GET only, no redirects followed, a small body cap. Since 2026-09-17 it is read even when the
label answered, because the health body is also the only source of `db`, `readOnly` and `flags`
(appCheckRequired among them) - seen live: brms-mes-api-dev answered
{"db":"BRMS_database_dev","ok":true,"readOnly":false,"flags":{...},"version":"3dc6631"}.

The HOSTING half of a cell is NOT read in this version. `site` comes back as UNKNOWN with a note
saying so, rather than as absent or healthy. §6.1's open question stands; this adapter is honest
about which half it can see.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Sequence
from urllib.parse import urlparse

from helix.domain.fleet import Health, Serving, Service
from helix.ports.fleet import CellRead

# One plain sentence per failure kind. ASCII (rule 7): these get printed on a Windows console.
NOT_INSTALLED = "The gcloud CLI is not installed on this machine, so the fleet cannot be read."
NOT_LOGGED_IN = "gcloud is installed but not logged in. Run: gcloud auth login"
NO_PERMISSION = "Your gcloud account is not allowed to read this service."
TIMED_OUT = "Reading this service took too long and was stopped."
BAD_OUTPUT = "gcloud answered, but not in a shape HELIX understands."
NOT_FOUND_NOTE = "The fleet table says this service exists, but Cloud Run says it does not."
HOSTING_NOTE = "Hosting is not read yet - only the API half of this cell is shown."

_HEALTH_BODY_CAP = 8 * 1024


@dataclass(frozen=True)
class Ran:
    """What one subprocess did. Tests build these by hand; the real runner builds them from
    subprocess.run. `rc=127` is reserved to mean 'the binary was not found'."""

    rc: int
    out: str
    err: str


Runner = Callable[[list[str], float], Ran]
HttpGet = Callable[[str, float], tuple[int, str]]   # (status, body) - never raises


def _real_runner(argv: list[str], timeout_s: float) -> Ran:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                           encoding="utf-8", errors="replace")
        return Ran(p.returncode, p.stdout or "", p.stderr or "")
    except FileNotFoundError:
        return Ran(127, "", "not found")
    except subprocess.TimeoutExpired:
        return Ran(124, "", "timeout")
    except OSError as exc:  # noqa: BLE001 - one class of failure, one sentence
        return Ran(1, "", str(exc))


def _real_http_get(url: str, timeout_s: float) -> tuple[int, str]:
    import urllib.request
    import urllib.error

    class _Refuse(urllib.request.HTTPRedirectHandler):
        """Refuses every redirect. A request that follows a redirect has been handed to whoever
        answered; connections.py already knows this, and the health probe is not allowed to forget it
        even though it carries no token."""

        def redirect_request(self, *a, **k):  # noqa: D401
            return None

    opener = urllib.request.build_opener(_Refuse)
    try:
        with opener.open(urllib.request.Request(url, method="GET",
                                                headers={"Accept": "application/json"}),
                         timeout=timeout_s) as r:
            return r.status, r.read(_HEALTH_BODY_CAP).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:  # noqa: BLE001
        return 0, ""


def classify(ran: Ran) -> tuple[str, str] | None:
    """(problem sentence, detail) for a failed run, or None when it succeeded.

    Order matters: 'not installed' before 'not logged in' before 'no permission', because the later
    strings can appear inside the earlier failures' output and the earlier one is the real cause."""
    if ran.rc == 0:
        return None
    err = (ran.err or "") + (ran.out or "")
    low = err.lower()
    if ran.rc == 127:
        return NOT_INSTALLED, err.strip()[:400]
    if ran.rc == 124:
        return TIMED_OUT, err.strip()[:400]
    if ("gcloud auth login" in low or "reauthentication" in low or "no active account" in low
            or "not logged in" in low or "credentials" in low and "expired" in low):
        return NOT_LOGGED_IN, err.strip()[:400]
    if "permission_denied" in low or "403" in low or "does not have permission" in low:
        return NO_PERMISSION, err.strip()[:400]
    return BAD_OUTPUT, err.strip()[:400]


def _is_not_found(ran: Ran) -> bool:
    """Cloud Run's own 'no such service' - and ONLY that. rc 127 is the gcloud binary missing and its
    message also contains the words 'not found', which is exactly the confusion rule 1 forbids, so
    the CLI-level codes are excluded before any text is inspected."""
    if ran.rc in (0, 124, 127):
        return False
    low = ((ran.err or "") + (ran.out or "")).lower()
    return ("not_found" in low or "cannot find service" in low or "could not be found" in low
            or "could not find" in low)


def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _ready(obj: dict) -> Health:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            if c.get("status") == "True":
                return Health.OK
            if c.get("status") == "False":
                return Health.DOWN
    return Health.UNKNOWN


def _split_version(label: str | None) -> tuple[str | None, bool]:
    """'a41f9c2-dirty' -> ('a41f9c2', True). Both dev.ps1's label and MES's /api/health use this shape."""
    if not label:
        return None, False
    v = str(label).strip().lower()
    if v in ("", "unknown"):
        return None, False
    if v.endswith("-dirty"):
        return v[:-6] or None, True
    return v, False


class GcloudFleet:
    def __init__(self, project: str, region: str, *, runner: Runner | None = None,
                 http_get: HttpGet | None = None, gcloud: str = "gcloud", workers: int = 6) -> None:
        self._project = project
        self._region = region
        self._run = runner or _real_runner
        self._http = http_get or _real_http_get
        # On Windows the Cloud SDK ships `gcloud.cmd` and `gcloud.ps1`, no `gcloud.exe`. CreateProcess
        # does not consult PATHEXT, so spawning bare "gcloud" raises FileNotFoundError - which this
        # adapter would report as NOT_INSTALLED on a machine where gcloud plainly works (seen on
        # Brian's box 2026-09-17; the .ps1 is also blocked by execution policy). shutil.which DOES
        # honour PATHEXT and returns the .cmd, so resolve once here and spawn what it found.
        self._gcloud = shutil.which(gcloud) or gcloud
        self._workers = max(1, workers)
        self._probe_lock = threading.Lock()
        self._probe: tuple[bool, str | None] | None = None

    # ---------------------------------------------------------------- availability

    def available(self) -> tuple[bool, str | None]:
        """Is there a gcloud to call? One `--version` spawn, cached for the life of the object.
        Login state is NOT probed here - that is per-read, and a login that expires mid-session
        is reported by the read that hit it, in its own words."""
        with self._probe_lock:
            if self._probe is not None:
                return self._probe
            if shutil.which(self._gcloud) is None and self._run is _real_runner:
                self._probe = (False, NOT_INSTALLED)
                return self._probe
            ran = self._run([self._gcloud, "--version"], 15.0)
            self._probe = (True, None) if ran.rc == 0 else (False, NOT_INSTALLED)
            return self._probe

    # ---------------------------------------------------------------- reads

    def _base(self) -> list[str]:
        return [self._gcloud, "run"]

    def _tail(self) -> list[str]:
        return ["--project", self._project, "--region", self._region, "--format", "json"]

    def read_cell(self, service: Service, *, timeout_s: float = 30.0) -> CellRead:
        import time
        t0 = time.monotonic()
        if service.run_service is None:
            # A known absence (§6.1). Not a failure, not a read - the table already knows.
            return CellRead(service=service, ok=True, api=None, site=None,
                            seconds=time.monotonic() - t0)

        svc = self._run(self._base() + ["services", "describe", service.run_service] + self._tail(),
                        timeout_s)
        if _is_not_found(svc):
            return CellRead(service=service, ok=True,
                            api=Serving(health=Health.ABSENT),
                            site=Serving(health=Health.UNKNOWN),
                            problem=NOT_FOUND_NOTE, detail=(svc.err or "")[:400],
                            seconds=time.monotonic() - t0)
        bad = classify(svc)
        if bad:
            return CellRead(service=service, ok=False, problem=bad[0], detail=bad[1],
                            seconds=time.monotonic() - t0)
        try:
            sdoc = json.loads(svc.out or "{}")
        except ValueError:
            return CellRead(service=service, ok=False, problem=BAD_OUTPUT,
                            detail=(svc.out or "")[:400], seconds=time.monotonic() - t0)

        revs = self._run(self._base() + ["revisions", "list", "--service", service.run_service]
                         + self._tail(), timeout_s)
        rdocs: list[dict] = []
        if revs.rc == 0:
            try:
                rdocs = [r for r in json.loads(revs.out or "[]") if isinstance(r, dict)]
            except ValueError:
                rdocs = []
        rdocs.sort(key=lambda r: (r.get("metadata") or {}).get("creationTimestamp") or "",
                   reverse=True)

        api = self._api_half(sdoc, rdocs, timeout_s)
        served = tuple(str((r.get("metadata") or {}).get("name") or "")
                       for r in rdocs if (r.get("metadata") or {}).get("name"))
        return CellRead(service=service, ok=True, api=api,
                        site=Serving(health=Health.UNKNOWN),
                        served_revisions=served, problem=None,
                        detail=HOSTING_NOTE, seconds=time.monotonic() - t0)

    def _api_half(self, sdoc: dict, rdocs: list[dict], timeout_s: float) -> Serving:
        status = sdoc.get("status") or {}
        # The serving revision = the traffic entry with the most traffic. Splits are reported, not
        # assumed away (§15 q10).
        traffic = [t for t in (status.get("traffic") or []) if isinstance(t, dict)]
        top = max(traffic, key=lambda t: int(t.get("percent") or 0), default=None)
        rev_name = (top or {}).get("revisionName") or status.get("latestReadyRevisionName")
        percent = int((top or {}).get("percent") or 0) if top else None
        rdoc = next((r for r in rdocs if (r.get("metadata") or {}).get("name") == rev_name), None)
        meta = (rdoc or {}).get("metadata") or {}
        labels = meta.get("labels") or {}
        commit, dirty = _split_version(labels.get("version"))
        by = labels.get("by") or None
        # The app's own /api/health, the MES convention. Always read when there is a URL: it is the
        # only source for db / readOnly / flags, and fallback (b) for the commit when no label exists.
        url = status.get("url")
        hd = self._health_doc(str(url), min(timeout_s, 8.0)) if url else {}
        if commit is None:
            commit, dirty = _split_version(hd.get("version") if isinstance(hd.get("version"), (str, int)) else None)
        db = hd.get("db") if isinstance(hd.get("db"), str) else None
        read_only = hd.get("readOnly") if isinstance(hd.get("readOnly"), bool) else None
        raw_flags = hd.get("flags") if isinstance(hd.get("flags"), dict) else {}
        flags = tuple(sorted((str(k), bool(v)) for k, v in raw_flags.items() if isinstance(v, bool)))
        health = _ready(sdoc)
        if rdoc is not None and health is Health.OK:
            health = _ready(rdoc) if _ready(rdoc) is not Health.UNKNOWN else health
        # A Ready revision whose own health endpoint says ok:false is DEGRADED, not OK: Cloud Run is
        # reporting the container answers, the app is reporting it cannot do its job.
        if health is Health.OK and hd.get("ok") is False:
            health = Health.DEGRADED
        return Serving(revision=rev_name, commit=commit, dirty=dirty,
                       deployed_at=_ts(meta.get("creationTimestamp")), deployed_by=by,
                       health=health, traffic_percent=percent,
                       db=db, read_only=read_only, flags=flags)

    def _health_doc(self, url: str, timeout_s: float) -> dict:
        """GET <url>/api/health and return the JSON object, or {} for anything else. Fenced to the
        service's own *.run.app host: the URL came from Cloud Run, but the fence costs nothing and
        means a poisoned service doc still cannot point this probe at an arbitrary host."""
        host = (urlparse(url).hostname or "").lower()
        if not host.endswith(".run.app"):
            return {}
        status, body = self._http(url.rstrip("/") + "/api/health", timeout_s)
        if status != 200 or not body:
            return {}
        try:
            doc = json.loads(body)
        except ValueError:
            return {}
        return doc if isinstance(doc, dict) else {}

    def _health_version(self, url: str, timeout_s: float) -> tuple[str | None, bool]:
        """Kept for callers/tests that want only the commit: the (sha, dirty) pair from /api/health."""
        doc = self._health_doc(url, timeout_s)
        v = doc.get("version")
        return _split_version(str(v) if isinstance(v, (str, int)) else None)

    def read_all(self, services: Sequence[Service], *, timeout_s: float = 90.0) -> list[CellRead]:
        per = max(5.0, min(30.0, timeout_s / max(1, len(services)) * self._workers))
        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="helix-fleet") as ex:
            return list(ex.map(lambda s: self.read_cell(s, timeout_s=per), services))
