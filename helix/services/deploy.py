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

from helix.domain.fleet import (COMPANIES, Env, PROD_ENVS, check_deploy, check_rollback, find,
                                may_touch_production, rollback_targets)

BUSY = "A deploy or rollback is already running. One at a time."
NOT_DESKTOP = "Deploys run from a person's own PC (DESKTOP profile), never from the cloud."
NO_IDENTITY = "No gcloud account is signed in on this PC, so HELIX cannot say who is deploying."
NOT_ALLOWED = "{who} is not on the production allowlist. Production is Brian, Brendan and Kate."
BAD_PHRASE = "Type exactly '{phrase}' to confirm. Nothing ran."
CANCEL_DEPLOY = "Stops the script. Cloud Run only moves traffic once a new revision is healthy, so the old revision keeps serving; a half-built revision is left unused. Firebase Hosting keeps the last release."
CANCEL_ROLLBACK = ""   # a rollback is one call that takes seconds: not cancellable, by design
NOT_SURE = "Confirm with 'yes' in the second gate. Nothing ran."
NOT_LINKED_ENV = "{app} {env} does not exist yet. Create it first (the gear on the card)."

Push = Callable[[dict], None]


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
                 profile: Callable[[], str | None], runner=None, jobs=None) -> None:
        self._scripts = scripts
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

    # ---------------------------------------------------------------- state
    def status(self) -> dict:
        return {"running": self._job is not None, "job": self._job, "log": self._log[-400:], "recent": self._audit.recent()}

    def _line(self, text: str) -> None:
        self._log.append(text)
        if len(self._log) > 2000:
            del self._log[:1000]
        self._push({"t": "deploy", "line": text})
        if self._jobs is not None and self._task is not None:
            self._jobs.line(self._task, text)

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

    def _close_task(self, ok: bool, rc: int) -> None:
        if self._jobs is not None and self._task is not None:
            self._jobs.finish(self._task, ok, rc=rc, note=("Done" if ok else f"Failed (exit {rc})"))
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
        argv, why = self._scripts.deploy_argv(app, env.value, action)
        if why:
            return {"ok": False, "error": why}
        who = self._identity() or "unknown"
        job = {"kind": "deploy", "app": app, "env": env.value, "action": action, "by": who, "argv": argv}
        if not self._start(job):
            return {"ok": False, "error": BUSY}
        rid = self._audit.open({"kind": "deploy", "app": app, "env": env.value, "action": action, "by": who,
                                "phrase": phrase if env in PROD_ENVS else "", "argv": argv})
        job["audit"] = rid
        self._open_task("deploy", f"Ship {app} {env.value} ({action})", app=app, env=env.value, by=who, cancel_note=CANCEL_DEPLOY)

        def run():
            self._line(f"[helix] {who} deploys {app} {env.value} ({action}) via dev.ps1")
            rc = self._stream(argv, self._scripts.root())
            ok = rc == 0
            self._line(f"[helix] {'done' if ok else 'FAILED'} (exit {rc})")
            self._audit.close(rid, ok=ok, rc=rc)
            self._close_task(ok, rc)
            self._finish(ok, rc)
            try:
                co = COMPANIES[0]
                self._fleet.read_app(co, app)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=run, daemon=True, name="helix-deploy").start()
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

        threading.Thread(target=run, daemon=True, name="helix-rollback").start()
        return {"ok": True, "job": job}

    def _stream(self, argv: list[str], cwd) -> int:
        try:
            return self._scripts.stream(argv, cwd, self._line, on_start=self._on_proc)
        except TypeError:       # a scripts double without on_start
            return self._scripts.stream(argv, cwd, self._line)

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

    # ---------------------------------------------------------------- create an environment (the plan)
    def create_plan(self, app: str, env_name: str, *, typed: str = "", sure: bool = False) -> dict:
        app = app.strip().upper()
        try:
            env = Env(env_name.lower())
        except ValueError:
            return {"ok": False, "error": f"{env_name} is not an environment."}
        svc = find(app, env)
        if svc is None:
            return {"ok": False, "error": f"{app} is not part of the fleet."}
        if svc.exists:
            return {"ok": False, "error": f"{app} {env.value} already exists."}
        if (typed or "").strip().upper() != app:
            return {"ok": False, "error": f"Type the app's name, {app}, to confirm. Nothing ran."}
        if not sure:
            return {"ok": False, "error": NOT_SURE}
        who = (self._identity() or "").strip()
        if not who or not may_touch_production(who):
            return {"ok": False, "error": NOT_ALLOWED.format(who=who or "nobody")}
        dev = find(app, Env.DEV)
        base = (dev.run_service if dev and dev.run_service else f"{app.lower()}-api-dev")
        stem = re.sub(r"-dev$", "", base)
        service = f"{stem}-{env.value}" if env is not Env.PROD else stem            # MES's own pattern: prod drops the suffix
        site = (dev.hosting_site or "").replace("-dev", f"-{env.value}") if dev and dev.hosting_site else None
        co = COMPANIES[0]
        steps = [
            {"what": f"Cloud Run service {service} in {co.gcp_project}/{co.region}", "cmd": f"gcloud run deploy {service} --source <backend dir> --project {co.gcp_project} --region {co.region} --allow-unauthenticated --labels version=<sha>,by={who.split('@')[0]}"},
            {"what": "Environment variables from dev.ps1's block for this app, with the environment swapped (never hand-typed)", "cmd": "dev.ps1: the app's BackendEnv, ALLOWED_ORIGINS pointed at the new site"},
        ]
        if site:
            steps.append({"what": f"Firebase Hosting site {site}", "cmd": f"firebase hosting:sites:create {site} --project {co.gcp_project}"})
            steps.append({"what": "Hosting target in firebase.json for the new environment", "cmd": f"firebase target:apply hosting {env.value} {site}"})
        steps.append({"what": "The fleet table gains the new cell (a code change, reviewed)", "cmd": f"helix/domain/fleet.py: Service(\"{app}\", Env.{env.value.upper()}, \"{service}\", {json.dumps(site)}, ...)"})
        rid = self._audit.open({"kind": "create-plan", "app": app, "env": env.value, "by": who, "service": service, "site": site})
        self._audit.close(rid, ok=True, planned=True)
        return {"ok": True, "planned": True, "app": app, "env": env.value, "service": service, "site": site, "steps": steps,
                "note": "This is the plan. Nothing ran. Running it is the next step, with the plan read once beside a person."}
