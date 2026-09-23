"""The Deploy lane - ship, roll back, and (deliberately) create an environment.

Every action goes through the domain's gates first (check_deploy / check_rollback, the prod
allowlist), then writes an AUDIT row, then runs. Production adds the four conditions of plan
section 10.2: DESKTOP profile, an allowlisted live identity (the active gcloud account), a typed
confirmation naming the app and environment ("deploy prod" / "rollback prod"), and the audit row
written BEFORE anything runs. One job at a time; the lines stream to the page through the hub.

Creating an environment (a prototype growing QA or PROD) is not a deploy and not automatic: it is
a named, gated, human act. This round it PLANS: the exact commands, shown to the person, nothing
run. Running them is the next step once the plan has been read once with a human beside it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from helix.domain.fleet import (COMPANIES, Env, PROD_ENVS, check_deploy, check_rollback, console_steps, find,
                                may_touch_production, planned, rollback_targets)

BUSY = "A deploy or rollback is already running. One at a time."
NOT_DESKTOP = "Deploys run from a person's own PC (DESKTOP profile), never from the cloud."
NO_IDENTITY = "No gcloud account is signed in on this PC, so HELIX cannot say who is deploying."
NOT_ALLOWED = "{who} is not on the production allowlist. Production is Brian, Brendan and Kate."
BAD_PHRASE = "Type exactly '{phrase}' to confirm. Nothing ran."
CANCEL_DEPLOY = "Stops the script. Cloud Run only moves traffic once a new revision is healthy, so the old revision keeps serving; a half-built revision is left unused. Firebase Hosting keeps the last release."
CANCEL_ROLLBACK = ""   # a rollback is one call that takes seconds: not cancellable, by design
NOT_SURE = "Confirm with 'yes' in the second gate. Nothing ran."
NOT_LINKED_ENV = "{app} {env} does not exist yet. Create it first (the gear on the card)."

CANCEL_CREATE = ("Stops the script. Whatever was already made stays and costs nothing while idle: a Hosting site "
                 "with no release, or a Cloud Run service with no traffic. Run Create again to finish it.")
NO_PLAN = "{app} {env} has no decided names yet (service and site). That is a decision for a person first."
NOT_TARGETED = ("{folder} has no Hosting target '{env}' in .firebaserc yet. Pull {app}'s repo (the firebase.json "
                "targets change), then try again. Nothing ran.")
SHIP_ACTIONS = {"be-deploy": ("be-deploy",), "fe-deploy": ("fe-deploy",), "both": ("be-deploy", "fe-deploy")}
SHIP_WORDS = {"be-deploy": "the server", "fe-deploy": "the site", "both": "the server, then the site"}

Push = Callable[[dict], None]
QUIET_S = 45.0
CLOUD_BUILD_URL = "https://console.cloud.google.com/cloud-build/builds?project=windy-celerity-392822"


def _process_tree(pid: int | None) -> str:
    """The names under a pid, as one line ("powershell > gcloud > python"), on Windows through one
    CIM query; elsewhere through ps. Empty when it cannot be read. Only used while a run is quiet."""
    if not pid:
        return ""
    try:
        if sys.platform.startswith("win"):
            p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"],
                               capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            rows = json.loads(p.stdout or "[]")
        else:
            p = subprocess.run(["ps", "-eo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10)
            rows = [{"ProcessId": int(a), "ParentProcessId": int(b), "Name": c} for a, b, c in
                    (ln.split(None, 2) for ln in p.stdout.splitlines() if len(ln.split(None, 2)) == 3)]
    except Exception:  # noqa: BLE001
        return ""
    kids: dict[int, list[tuple[int, str]]] = {}
    for r in rows if isinstance(rows, list) else []:
        try:
            kids.setdefault(int(r["ParentProcessId"]), []).append((int(r["ProcessId"]), str(r["Name"])))
        except Exception:  # noqa: BLE001
            continue
    names: list[str] = []

    def walk(p: int, depth: int) -> None:
        for cid, name in kids.get(p, []):
            if depth < 6 and len(names) < 8:
                names.append(name.lower().removesuffix(".exe").removesuffix(".cmd"))
                walk(cid, depth + 1)

    walk(int(pid), 0)
    return " > ".join(names)

# dev.ps1 sets an exit code only since 2026-09-20, and before that a refused production gate
# ("Aborted.") left exit 0. So the script's own words are read as well: its result line wins,
# then any line that says it failed, then the exit code.
_RESULT = re.compile(r"^\[dev\.ps1\] RESULT: (OK|FAILED)\s*$")
_BAD = re.compile(r"(\bFAILED\b|^Aborted\.|\bRefusing to\b|deploy blocked|^ERROR:|is not available on PROD|"
                  r"cannot source-deploy|was not found at:)")


# THE PHASES. dev.ps1, backend/deploy.ps1, gcloud and firebase each announce what they are doing;
# these lines become a fraction and a plain note, so the strand fills and the card says where the
# run is ("Building the container - the long part"). Order matters: first match wins; progress
# never goes backwards within one script run.
PHASES: tuple[tuple[re.Pattern, float, str], ...] = tuple((re.compile(rx, re.I), f, note) for rx, f, note in (
    (r"Checking gcloud sign-in|Checking deploy credentials", 0.03, "Checking the sign-in"),
    (r"Running (backend|frontend).*tests", 0.08, "Running the tests first"),
    (r"^Tests passed", 0.14, "Tests passed"),
    (r"npm run build|vite v\d|building for production", 0.20, "Building the site"),
    (r"built in \d|\u2713 built", 0.40, "Site built"),
    (r"Building (using Dockerfile|Container)|Build in progress|Logs are available at", 0.30, "Building the container on Cloud Build - the long part (2-8 min)"),
    (r"Deploying .* to Cloud Run", 0.17, "Handing the source to Cloud Build"),
    (r"Creating temporary archive|Uploading sources|Uploading tarball", 0.22, "Uploading the source"),
    (r"Building Container\.*done|Container built|Setting IAM Policy", 0.68, "Container built"),
    (r"Creating Revision", 0.76, "Creating the revision"),
    (r"Routing traffic", 0.90, "Routing traffic to it"),
    (r"Service URL:|Backend deployed OK", 0.97, "Server deployed - smoke check"),
    (r"Smoke check: GET .* (server is up|HTTP)", 0.99, "The server answers"),
    (r"hosting\[.*\]: (beginning deploy|found \d+ files)", 0.55, "Uploading the site"),
    (r"file upload complete|hosting\[.*\]: upload complete", 0.72, "Upload complete"),
    (r"finalizing version", 0.85, "Finalizing the release"),
    (r"release complete|Deploy complete", 0.96, "Site released"),
    (r"Opening https?://", 0.99, "Site is live"),
))


def phase_of(line: str) -> tuple[float, str] | None:
    text = (line or "").strip()
    if not text or text.startswith("[helix]"):
        return None
    for rx, frac, note in PHASES:
        if rx.search(text):
            return frac, note
    return None


def judge(rc: int, lines: list[str]) -> tuple[bool, str]:
    """(ok, why) for one script run: what the script SAID, then what it returned."""
    said_bad = ""
    for ln in lines:
        text = ln.strip()
        m = _RESULT.match(text)
        if m:
            return (m.group(1) == "OK" and rc == 0, "" if m.group(1) == "OK" and rc == 0 else (said_bad or f"exit {rc}"))
        if not said_bad and not text.startswith("[helix]") and _BAD.search(text):
            said_bad = text[:200]
    if said_bad:
        return False, said_bad
    return rc == 0, "" if rc == 0 else f"exit {rc}"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Audit:
    """One JSON line per action, appended BEFORE the action runs, then closed with its result."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def open(self, row: dict) -> str:
        rid = f"{int(time.time() * 1000):x}"
        row = {"id": rid, "at": utcnow(), **row}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        return rid

    def close(self, rid: str, **result) -> None:
        row = {"id": rid, "closed_at": utcnow(), **result}
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    def recent(self, limit: int = 40) -> list[dict]:
        if not self._path.exists():
            return []
        rows = [json.loads(ln) for ln in self._path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # fold closes into opens
        by: dict[str, dict] = {}
        for r in rows:
            if "closed_at" in r and r["id"] in by:
                by[r["id"]].update(r)
            elif "closed_at" not in r:
                by[r["id"]] = dict(r)
        return list(by.values())[-limit:][::-1]


class DeployService:
    def __init__(self, scripts, fleet, *, audit: Audit, push: Push, identity: Callable[[], str | None],
                 profile: Callable[[], str | None], runner=None, jobs=None, auth_domains=None) -> None:
        self._scripts = scripts
        self._auth_domains = auth_domains   # adapters.firebase_auth.FirebaseAuthDomains, or None
        self._fleet = fleet
        self._audit = audit
        self._push = push
        self._identity = identity
        self._profile = profile
        self._runner = runner       # for a rollback's one gcloud call: (argv, timeout) -> Ran
        self._jobs = jobs           # services.jobs.Jobs: every run is a task in CURRENT TASKS
        self._lock = threading.Lock()
        self._job: dict | None = None
        self._task = None           # the register's Job for the running action
        self._log: list[str] = []
        self._last_line_at = time.monotonic()
        self._step_base, self._step_span, self._frac = 0.0, 1.0, 0.0

    # ---------------------------------------------------------------- state
    def status(self) -> dict:
        return {"running": self._job is not None, "job": self._job, "log": self._log[-400:], "recent": self._audit.recent()}

    def _line(self, text: str) -> None:
        self._log.append(text)
        if len(self._log) > 2000:
            del self._log[:1000]
        self._push({"t": "deploy", "line": text})
        self._last_line_at = time.monotonic()
        if self._jobs is not None and self._task is not None:
            self._jobs.line(self._task, text)
            ph = phase_of(text)
            if ph is not None:
                frac = self._step_base + ph[0] * self._step_span
                if frac >= self._frac:
                    self._frac = frac
                    self._jobs.progress(self._task, frac, ph[1])

    def _steps(self, k: int, n: int) -> None:
        """Step k of n of a multi-script run: the strand's window for this script."""
        self._step_base = (k - 1) / max(1, n)
        self._step_span = 1.0 / max(1, n)
        self._frac = self._step_base
        if self._jobs is not None and self._task is not None:
            self._jobs.progress(self._task, self._frac, None)

    def _quiet_watch(self, proc_box: dict, stop: threading.Event) -> None:
        """While a script runs without a word for QUIET_S, say so - and what its process tree is
        doing - so a silent Cloud Build never looks like a hang, and a real hang is named."""
        n = 0
        while not stop.wait(5.0):
            quiet = time.monotonic() - self._last_line_at
            if quiet < QUIET_S * (n + 1):
                continue
            n += 1
            p = proc_box.get("proc")
            tree = _process_tree(getattr(p, "pid", None)) if p is not None else ""
            what = f" Running underneath: {tree}." if tree else ""
            hint = (" A Cloud Build build says nothing for minutes; watch it at " + CLOUD_BUILD_URL
                    if ("gcloud" in tree or "python" in tree) else "")
            self._line(f"[helix] quiet for {int(quiet)} s - still running.{what}{hint}")

    def _open_task(self, kind: str, title: str, *, app: str, env: str, by: str, cancel_note: str) -> None:
        """Register the running action in CURRENT TASKS. `cancel_note` empty = not cancellable."""
        if self._jobs is None:
            return
        proc_box: dict = {}

        def stop():
            p = proc_box.get("proc")
            if p is None:
                return
            # Windows: the script is a tree (powershell -> gcloud -> python...), so the whole tree
            # goes with taskkill /T; then the handle itself is killed too, which is harmless when
            # taskkill already did it and is the only thing that works on a double in the tests.
            if sys.platform.startswith("win"):
                try:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:  # noqa: BLE001
                    pass
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

        self._task = self._jobs.start(kind, title, app=app, env=env, by=by,
                                      cancel=stop if cancel_note else None, cancel_note=cancel_note)
        self._proc_box = proc_box

    def _on_proc(self, proc) -> None:
        box = getattr(self, "_proc_box", None)
        if box is not None:
            box["proc"] = proc

    def _close_task(self, ok: bool, rc: int, why: str = "") -> None:
        if self._jobs is not None and self._task is not None:
            self._jobs.finish(self._task, ok, rc=rc, note=("Done" if ok else (why[:120] or f"Failed (exit {rc})")))
        self._task = None
        self._proc_box = None

    def _start(self, job: dict) -> bool:
        with self._lock:
            if self._job is not None:
                return False
            self._job = job
            self._log = []
            return True

    def _finish(self, ok: bool, rc: int) -> None:
        with self._lock:
            job = self._job
            self._job = None
        self._push({"t": "deploy_done", "ok": ok, "rc": rc, "job": job})

    # ---------------------------------------------------------------- the gates
    def _prod_gate(self, app: str, env: Env, verb: str, phrase: str, sure: bool) -> str | None:
        if env not in PROD_ENVS:
            return None
        if (self._profile() or "desktop") != "desktop":
            return NOT_DESKTOP
        who = (self._identity() or "").strip()
        if not who:
            return NO_IDENTITY
        if not may_touch_production(who):
            return NOT_ALLOWED.format(who=who)
        expected = f"{verb} prod"
        if (phrase or "").strip().lower() != expected:
            return BAD_PHRASE.format(phrase=expected)
        if not sure:
            return NOT_SURE
        return None

    # ---------------------------------------------------------------- deploy
    def deploy(self, app: str, env_name: str, *, phrase: str = "", sure: bool = False,
               action: str = "be-deploy") -> dict:
        app = app.strip().upper()
        try:
            env = Env(env_name.lower())
        except ValueError:
            return {"ok": False, "error": f"{env_name} is not an environment."}
        svc = find(app, env)
        problems = check_deploy(app, env, action="deploy", service_exists=bool(svc and svc.exists))
        if problems:
            return {"ok": False, "error": " ".join(problems)}
        gate = self._prod_gate(app, env, "deploy", phrase, sure)
        if gate:
            return {"ok": False, "error": gate}
        steps = SHIP_ACTIONS.get(action)
        if steps is None:
            return {"ok": False, "error": f"{action} is not something HELIX ships. Pick the server, the site, or both."}
        signed = self._signed_in()
        if signed:
            return {"ok": False, "error": signed}
        argvs = []
        for step in steps:
            argv, why = self._argv(app, env, step)
            if why:
                return {"ok": False, "error": why}
            argvs.append(argv)
        argv = argvs[0]
        who = self._identity() or "unknown"
        job = {"kind": "deploy", "app": app, "env": env.value, "action": action, "by": who, "argv": argv}
        if not self._start(job):
            return {"ok": False, "error": BUSY}
        rid = self._audit.open({"kind": "deploy", "app": app, "env": env.value, "action": action, "by": who,
                                "phrase": phrase if env in PROD_ENVS else "", "argv": argv})
        job["audit"] = rid
        self._open_task("deploy", f"Ship {app} {env.value} ({SHIP_WORDS[action]})", app=app, env=env.value, by=who, cancel_note=CANCEL_DEPLOY)

        def run():
            ok, rc = True, 0
            for k, (step, step_argv) in enumerate(zip(steps, argvs), 1):
                self._steps(k, len(steps))
                self._line(f"[helix] {who} ships {app} {env.value}: {SHIP_WORDS[step]} ({step}) via dev.ps1")
                ok, rc, why = self._run_script(step_argv, self._scripts.root())
                if not ok:
                    break
            self._line(f"[helix] {'done' if ok else 'FAILED'} (exit {rc})")
            self._audit.close(rid, ok=ok, rc=rc)
            self._close_task(ok, rc, "" if ok else why)
            self._finish(ok, rc)
            try:
                co = COMPANIES[0]
                self._fleet.read_app(co, app)
            except Exception:  # noqa: BLE001
                pass

        self._guarded("helix-deploy", run)
        return {"ok": True, "job": job}

    # ---------------------------------------------------------------- rollback
    def rollback(self, app: str, env_name: str, to_revision: str, *, phrase: str = "", sure: bool = False) -> dict:
        app = app.strip().upper()
        try:
            env = Env(env_name.lower())
        except ValueError:
            return {"ok": False, "error": f"{env_name} is not an environment."}
        svc = find(app, env)
        if svc is None or not svc.exists or not svc.run_service:
            return {"ok": False, "error": NOT_LINKED_ENV.format(app=app, env=env.value)}
        co = COMPANIES[0]
        cells, _ = self._fleet.snapshot(co)
        cell = next((c for c in cells if c.service.key == svc.key), None)
        served = list(getattr(cell, "served_revisions", ()) or ()) if cell else []
        if cell and cell.api and cell.api.revision and cell.api.revision not in served:
            served.insert(0, cell.api.revision)
        current = cell.api.revision if (cell and cell.api) else None
        problems = check_rollback(app, env, to_revision, served, current)
        if problems:
            return {"ok": False, "error": " ".join(problems)}
        gate = self._prod_gate(app, env, "rollback", phrase, sure)
        if gate:
            return {"ok": False, "error": gate}
        who = self._identity() or "unknown"
        argv = self._scripts.rollback_argv(co.gcp_project, co.region, svc.run_service, to_revision)
        job = {"kind": "rollback", "app": app, "env": env.value, "to": to_revision, "from": current, "by": who, "argv": argv}
        if not self._start(job):
            return {"ok": False, "error": BUSY}
        rid = self._audit.open({"kind": "rollback", "app": app, "env": env.value, "to": to_revision, "from": current, "by": who,
                                "phrase": phrase if env in PROD_ENVS else ""})
        job["audit"] = rid
        self._open_task("rollback", f"Roll back {app} {env.value} to {to_revision}", app=app, env=env.value, by=who, cancel_note=CANCEL_ROLLBACK)

        def run():
            self._line(f"[helix] {who} moves {app} {env.value} traffic to {to_revision} (was {current or '?'})")
            rc = self._stream(argv, None)
            ok = rc == 0
            self._line(f"[helix] {'done' if ok else 'FAILED'} (exit {rc})")
            self._audit.close(rid, ok=ok, rc=rc)
            self._close_task(ok, rc)
            self._finish(ok, rc)
            try:
                self._fleet.read_app(co, app)
            except Exception:  # noqa: BLE001
                pass

        self._guarded("helix-rollback", run)
        return {"ok": True, "job": job}

    def _stream(self, argv: list[str], cwd) -> int:
        try:
            return self._scripts.stream(argv, cwd, self._line, on_start=self._on_proc)
        except TypeError:       # a scripts double without on_start
            return self._scripts.stream(argv, cwd, self._line)

    def _guarded(self, name: str, run) -> None:
        """Every run thread goes through here: an exception inside a run closes the task as
        failed with the error in the log instead of leaving it 'running' forever (2026-09-22: a
        NameError in the adapter left a deploy at one line for seven minutes)."""
        def go():
            try:
                run()
            except Exception as exc:  # noqa: BLE001
                _msg = f"[helix] HELIX itself failed: {type(exc).__name__}: {exc}"
                try:
                    self._line(_msg)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self._close_task(False, -1, _msg[8:])
                finally:
                    self._finish(False, -1)
        threading.Thread(target=go, daemon=True, name=name).start()

    def _run_script(self, argv: list[str], cwd) -> tuple[bool, int, str]:
        """One script run, judged by its words and its exit code (see judge)."""
        mark = len(self._log)
        self._last_line_at = time.monotonic()
        stop = threading.Event()
        box = getattr(self, "_proc_box", None)
        if box is None:
            box = {}
        threading.Thread(target=self._quiet_watch, args=(box, stop), daemon=True, name="helix-quiet-watch").start()
        try:
            rc = self._stream(argv, cwd)
        finally:
            stop.set()
        ok, why = judge(rc, self._log[mark:])
        if rc == 0 and not ok:
            self._line(f"[helix] the script exited 0 but said: {why}")
        return ok, rc, why

    def _signed_in(self) -> str | None:
        """The sentence when gcloud cannot hand out a token without a prompt, else None. A double
        without signed_in() is trusted."""
        fn = getattr(self._scripts, "signed_in", None)
        if not callable(fn):
            return None
        ok, why = fn()
        return None if ok else (why or "gcloud is not signed in.")

    def _argv(self, app: str, env: Env, action: str, *, create: bool = False) -> tuple[list[str], str | None]:
        """dev.ps1's argv for one action. Production carries dev.ps1's own typed confirm, which is
        only ever added here - after _prod_gate held. Doubles without the keywords still work."""
        typed = "PROD" if env in PROD_ENVS else ""
        if not typed and not create:
            return self._scripts.deploy_argv(app, env.value, action)
        return self._scripts.deploy_argv(app, env.value, action, typed=typed, create=create)

    def targets(self, app: str, env_name: str) -> dict:
        """The live rollback targets for a cell, newest first; what is serving; the revisions seen."""
        app = app.strip().upper()
        env = Env(env_name.lower())
        svc = find(app, env)
        if svc is None or not svc.exists:
            return {"served": [], "current": None, "targets": []}
        co = COMPANIES[0]
        cells, _ = self._fleet.snapshot(co)
        cell = next((c for c in cells if c.service.key == svc.key), None)
        served = list(getattr(cell, "served_revisions", ()) or ()) if cell else []
        current = cell.api.revision if (cell and cell.api) else None
        if current and current not in served:
            served.insert(0, current)
        return {"served": served, "current": current, "targets": list(rollback_targets(served, current))}

    # ---------------------------------------------------------------- create an environment
    def _create_gate(self, app: str, env_name: str, typed: str, sure: bool, phrase: str = "", *, run: bool = False):
        """The gates shared by the plan and the run. Returns (error | None, app, env, svc, who).
        A plan needs the app's name typed, Are you sure, and an allowlisted person. RUNNING it on
        production adds the production conditions (DESKTOP, the typed phrase 'create prod')."""
        app = app.strip().upper()
        try:
            env = Env(env_name.lower())
        except ValueError:
            return f"{env_name} is not an environment.", app, None, None, ""
        svc = find(app, env)
        if svc is None:
            return f"{app} is not part of the fleet.", app, env, None, ""
        if svc.exists:
            return f"{app} {env.value} already exists.", app, env, svc, ""
        if (typed or "").strip().upper() != app:
            return f"Type the app's name, {app}, to confirm. Nothing ran.", app, env, svc, ""
        if not sure:
            return NOT_SURE, app, env, svc, ""
        who = (self._identity() or "").strip()
        if not who or not may_touch_production(who):
            return NOT_ALLOWED.format(who=who or "nobody"), app, env, svc, who
        if run:
            if (self._profile() or "desktop") != "desktop":
                return NOT_DESKTOP, app, env, svc, who
            gate = self._prod_gate(app, env, "create", phrase, sure)
            if gate:
                return gate, app, env, svc, who
        return None, app, env, svc, who

    def _names(self, app: str, env: Env) -> tuple[str, str | None]:
        """The service and site a new cell gets: the decided names (domain PLANNED) when there are
        any, else MES's pattern applied to the dev cell (prod drops the suffix)."""
        decided = planned(app, env)
        if decided:
            return decided
        dev = find(app, Env.DEV)
        base = (dev.run_service if dev and dev.run_service else f"{app.lower()}-api-dev")
        stem = re.sub(r"-dev$", "", base)
        service = f"{stem}-{env.value}" if env is not Env.PROD else stem
        site = (dev.hosting_site or "").replace("-dev", f"-{env.value}") if dev and dev.hosting_site else None
        return service, site

    def followups(self, app: str, env_name: str, done: dict[str, str] | None = None) -> dict:
        """The console steps only a person can do for a cell's site (domain console_steps)."""
        app = app.strip().upper()
        try:
            env = Env(env_name.lower())
        except ValueError:
            return {"ok": False, "error": f"{env_name} is not an environment."}
        svc = find(app, env)
        if svc is None:
            return {"ok": False, "error": f"{app} is not part of the fleet."}
        site = svc.hosting_site or (self._names(app, env)[1] if not svc.exists else None)
        co = COMPANIES[0]
        steps = []
        for st in console_steps(app, env, site, co.gcp_project):
            row = dict(st, steps=list(st["steps"]))
            if done and st["key"] in done:
                row["done_by_helix"] = done[st["key"]]          # the sentence HELIX can say for it
            elif st.get("by_helix"):
                row["by_helix_failed"] = "HELIX could not add it this time - do it by hand:"
            steps.append(row)
        return {"ok": True, "app": app, "env": env.value, "site": site,
                "url": f"https://{site}.web.app" if site else None, "steps": steps}

    def allow_sign_in(self, site: str | None) -> tuple[bool, str]:
        """Add the new site's domain to Firebase Auth's authorized domains through HELIX's own hands.
        (added-or-already, sentence); never raises, never removes anything."""
        if not site or self._auth_domains is None:
            return False, "HELIX has no way to reach Firebase Authentication on this PC."
        try:
            _added, why = self._auth_domains.add(f"{site}.web.app")
            return True, why
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def create_plan(self, app: str, env_name: str, *, typed: str = "", sure: bool = False) -> dict:
        err, app, env, _svc, who = self._create_gate(app, env_name, typed, sure)
        if err:
            return {"ok": False, "error": err}
        service, site = self._names(app, env)
        co = COMPANIES[0]
        flags = "-Create" + (" -Typed PROD" if env in PROD_ENVS else "")
        e = env.value.upper()
        steps = [
            {"what": f"Build the server for {e}", "who": "HELIX",
             "detail": f"Cloud Run service {service} in {co.gcp_project} ({co.region}), made by its first deploy. Settings come from dev.ps1's block for {app} with {e} swapped in.",
             "cmd": f"dev.ps1 -App {app.lower()} -Env {env.value} -Action be-deploy {flags}"},
        ]
        if site:
            steps.append({"what": f"Put up the site: {site}.web.app", "who": "HELIX",
                          "detail": f"Firebase Hosting site {site} (made if missing), then the first release through the '{env.value}' target in {app}'s firebase.json.",
                          "cmd": f"dev.ps1 -App {app.lower()} -Env {env.value} -Action fe-deploy" + (" -Typed PROD" if env in PROD_ENVS else "")})
            steps.append({"what": "Allow sign-in on the new site", "who": "HELIX",
                          "detail": f"{site}.web.app goes on Firebase Authentication's authorized domains, through Google's API as you.",
                          "cmd": f"PATCH identitytoolkit/admin/v2/projects/{co.gcp_project}/config  authorizedDomains += {site}.web.app"})
            steps.append({"what": "One reCAPTCHA setting", "who": "you",
                          "detail": "Google has no way to change a classic reCAPTCHA key's domain list except by hand. HELIX opens a card with the link, the value to paste and the clicks when the run ends. About a minute.",
                          "cmd": "google.com/recaptcha/admin > the key > Settings > Domains > + " + f"{site}.web.app"})
        steps.append({"what": "Tell the board", "who": "a one-line code change",
                      "detail": f"HELIX's fleet table gains the new cell so the Console reads it. HELIX prints the line at the end of the run.",
                      "cmd": f"helix/domain/fleet.py: Service(\"{app}\", Env.{env.value.upper()}, \"{service}\", {json.dumps(site)}, ...)"})
        rid = self._audit.open({"kind": "create-plan", "app": app, "env": env.value, "by": who, "service": service, "site": site})
        self._audit.close(rid, ok=True, planned=True)
        return {"ok": True, "planned": True, "app": app, "env": env.value, "service": service, "site": site, "steps": steps,
                "prod": env in PROD_ENVS,
                "note": "Nothing has run yet."}

    def create_run(self, app: str, env_name: str, *, typed: str = "", sure: bool = False, phrase: str = "") -> dict:
        """RUN the plan for ONE environment: the first server deploy (-Create), then the first
        site release. One task, every line streamed, the audit row first. The Deploy window's
        rule 5 is untouched - this is the only path that may create, and it is gated like prod."""
        err, app, env, _svc, who = self._create_gate(app, env_name, typed, sure, phrase, run=True)
        if err:
            return {"ok": False, "error": err}
        if planned(app, env) is None:
            return {"ok": False, "error": NO_PLAN.format(app=app, env=env.value)}
        signed = self._signed_in()
        if signed:
            return {"ok": False, "error": signed}
        service, site = self._names(app, env)
        folder = self._linked_folder(app)
        if site and folder and not _has_target(folder, env.value):
            return {"ok": False, "error": NOT_TARGETED.format(folder=folder, env=env.value, app=app)}
        be, why = self._argv(app, env, "be-deploy", create=True)
        if why:
            return {"ok": False, "error": why}
        fe: list[str] = []
        if site:
            fe, why = self._argv(app, env, "fe-deploy", create=True)
            if why:
                return {"ok": False, "error": why}
            fe = [a for a in fe if a != "-Create"]          # only the server is created by a deploy
        job = {"kind": "create", "app": app, "env": env.value, "by": who, "service": service, "site": site, "argv": be}
        if not self._start(job):
            return {"ok": False, "error": BUSY}
        rid = self._audit.open({"kind": "create", "app": app, "env": env.value, "by": who, "service": service, "site": site,
                                "phrase": phrase if env in PROD_ENVS else "", "argv": be})
        job["audit"] = rid
        self._open_task("deploy", f"Create {app} {env.value} ({service})", app=app, env=env.value, by=who, cancel_note=CANCEL_CREATE)

        def run():
            self._line(f"[helix] {who} creates {app} {env.value}: service {service}, site {site or 'none'}")
            self._steps(1, 3)
            self._line("[helix] step 1 of 3 - the server (first deploy, -Create)")
            ok, rc, why = self._run_script(be, self._scripts.root())
            done_by_helix: dict[str, str] = {}
            if ok and fe:
                self._steps(2, 3)
                self._line("[helix] step 2 of 3 - the site (made if missing, then the first release)")
                ok, rc, why = self._run_script(fe, self._scripts.root())
            if ok and site:
                self._steps(3, 3)
                self._line(f"[helix] step 3 of 3 - allow sign-in on {site}.web.app (Firebase Authentication)")
                added, sentence = self.allow_sign_in(site)
                self._line(f"[helix] {sentence}")
                if added:
                    done_by_helix["auth-domain"] = sentence
            if ok:
                self._line(f"[helix] {app} {env.value} exists. One thing is left that only you can do - the card that just opened walks you through it.")
                self._line(f"[helix] then add to helix/domain/fleet.py:  Service(\"{app}\", Env.{env.value.upper()}, \"{service}\", {json.dumps(site)}, ...)  so the board reads it.")
            else:
                self._line(f"[helix] FAILED (exit {rc}){': ' + why if why else ''}. Nothing is half-live: see what Stop leaves behind; run Create again once fixed.")
            self._audit.close(rid, ok=ok, rc=rc)
            self._close_task(ok, rc, "" if ok else why)
            with self._lock:
                done_job = self._job
                self._job = None
            self._push({"t": "deploy_done", "ok": ok, "rc": rc, "job": done_job,
                        "followups": self.followups(app, env.value, done_by_helix) if ok else None})

        self._guarded("helix-create", run)
        return {"ok": True, "job": job}

    def _linked_folder(self, app: str) -> str | None:
        try:
            links = self._fleet._links() if hasattr(self._fleet, "_links") else {}
            folder = getattr(links.get(app), "folder", None)
            return folder if folder and Path(folder).is_dir() else None
        except Exception:  # noqa: BLE001
            return None


def _has_target(folder: str, env: str) -> bool:
    """Does the app's checkout name a Hosting target for this environment? (.firebaserc beside
    firebase.json - at the repo root for WMS/MRP, under frontend/ for MES/ECHO.)"""
    for rel in ("frontend/.firebaserc", ".firebaserc"):
        p = Path(folder) / rel
        if p.is_file():
            try:
                rc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            for proj in (rc.get("targets") or {}).values():
                if env in ((proj or {}).get("hosting") or {}):
                    return True
            return False
    return True     # no .firebaserc to judge by: let the script say so
