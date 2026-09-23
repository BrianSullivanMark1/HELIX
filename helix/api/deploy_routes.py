"""The Deploy lane's routes. Human-only, on the local face, DESKTOP.

  GET  /api/deploy/status                 running job, the log tail, recent audit rows
  GET  /api/deploy/targets?app=&env=      live rollback targets for a cell
  POST /api/deploy                        {app, env, action?, phrase?, sure?}
  POST /api/deploy/rollback               {app, env, to, phrase?, sure?}
  POST /api/deploy/create_plan            {app, env, typed, sure}   -> the plan, nothing runs
  POST /api/deploy/create_run             {app, env, typed, sure, phrase?}  -> runs the plan for ONE environment
  GET  /api/deploy/followups?app_name=&env=   the console steps only a person can do (links, what to paste)
  GET  /api/deploy/identity               the active gcloud account and whether it may touch prod
  PUT  /api/deploy/console_root           {path}  where dev.ps1 lives
  POST /api/fleet/open_folder             {app}   open the linked folder in Explorer
  POST /api/fleet/merge_check             {app, from, into}  conflicts? (local clone, git merge-tree)
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from helix.domain.fleet import COMPANIES, apps, may_touch_production

DEPLOY_DOWN = "The deploy lane is not available on this HELIX."
CONSOLE_ROOT_SETTING = "console_root"
_QUIET = getattr(subprocess, "CREATE_NO_WINDOW", 0)


_IDENTITY_TTL_S = 300.0
_identity_cache: dict = {"who": None, "until": 0.0}
_identity_lock = threading.Lock()


def gcloud_identity(runner=None, *, fresh: bool = False) -> str | None:
    """The active gcloud account on this PC - the live identity the prod gate checks. One spawn of
    gcloud is seconds on Windows and the page asked on every gate and every pre-flight, so the
    answer is kept for five minutes; `fresh` asks again (a sign-in just happened)."""
    if runner is None:
        with _identity_lock:
            if not fresh and time.monotonic() < _identity_cache["until"]:
                return _identity_cache["who"]
    who = _gcloud_identity_now(runner)
    if runner is None:
        with _identity_lock:
            _identity_cache.update(who=who, until=time.monotonic() + (_IDENTITY_TTL_S if who else 20.0))
    return who


def _gcloud_identity_now(runner=None) -> str | None:
    from helix.adapters.pulse import spawned
    spawned("gcloud")
    try:
        if runner is not None:
            ran = runner(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], 15.0)
            return (ran.out or "").strip().splitlines()[0].strip() if ran.rc == 0 and (ran.out or "").strip() else None
        import shutil
        g = shutil.which("gcloud") or "gcloud"
        p = subprocess.run([g, "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"], capture_output=True,
                           text=True, timeout=15, creationflags=_QUIET)
        out = (p.stdout or "").strip()
        return out.splitlines()[0].strip() if p.returncode == 0 and out else None
    except Exception:  # noqa: BLE001
        return None


FIREBASE_PROJECT_SETTING = "firebase_project"


def _which(name: str) -> str | None:
    import shutil
    for cand in (name, name + ".cmd", name + ".exe"):
        p = shutil.which(cand)
        if p:
            return p
    return None


def _version(argv: list[str]) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=25, creationflags=_QUIET)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        return out[0].strip()[:60] if p.returncode == 0 and out else None
    except Exception:  # noqa: BLE001
        return None


def ensure_console_root(c) -> str:
    """The console checkout, or HELIX finds it: when the setting is empty (or stale), look where a
    person keeps their repos and take dev.ps1 when exactly one is found. Returns the root or ""."""
    settings = getattr(c, "settings", None)
    if settings is None:
        return ""
    root = str(settings.get(CONSOLE_ROOT_SETTING) or "").strip()
    if root and (Path(root) / "dev.ps1").is_file():
        return root
    from helix.adapters.console_scripts import find_console
    found = find_console()
    if found:
        setter = getattr(settings, "set", None)
        if callable(setter):
            setter(CONSOLE_ROOT_SETTING, found)
        return found
    return root


def tools_report(c) -> dict:
    settings = getattr(c, "settings", None)
    get = (lambda k: settings.get(k)) if settings is not None else (lambda k: None)
    ensure_console_root(c)
    rows = []
    g = _which("gcloud")
    who = gcloud_identity() if g else None
    rows.append({"key": "gcloud", "label": "Google Cloud CLI", "ok": bool(g and who),
                 "value": (who or (g and "installed, not signed in") or None),
                 "fix": ("Install the Google Cloud CLI from cloud.google.com/sdk, then run: gcloud auth login" if not g
                         else "Run in a terminal: gcloud auth login" if not who else "")})
    fb = _which("firebase")
    fbv = _version([fb, "--version"]) if fb else None
    rows.append({"key": "firebase", "label": "Firebase CLI", "ok": bool(fb and fbv),
                 "value": (f"v{fbv}" if fbv else None),
                 "fix": ("Install it once: npm install -g firebase-tools   then: firebase login" if not fb
                         else "The Firebase CLI is there but did not answer. Try: firebase --version" if not fbv else "")})
    proj = str(get(FIREBASE_PROJECT_SETTING) or "").strip() or COMPANIES[0].gcp_project
    rows.append({"key": "firebase_project", "label": "Firebase project", "ok": bool(proj), "value": proj,
                 "fix": "" if proj else "Type the Firebase project id (usually the same as the Google Cloud project)."})
    root = str(get(CONSOLE_ROOT_SETTING) or "").strip()
    has_script = bool(root) and (Path(root) / "dev.ps1").is_file()
    rows.append({"key": "console_root", "label": "Console checkout (dev.ps1)", "ok": has_script, "value": root or None,
                 "fix": ("Point HELIX at the BRMS_MES_WEB_VERSION folder that holds dev.ps1." if not root
                         else f"No dev.ps1 in {root}." if not has_script else "")})
    tok = bool(str(get("github_token") or "").strip())
    rows.append({"key": "github_token", "label": "GitHub token", "ok": tok, "value": ("set" if tok else None),
                 "fix": "" if tok else "Paste a classic token with the repo scope (Settings > Projects)."})
    needs = [r for r in rows if not r["ok"]]
    return {"tools": rows, "needs": [r["key"] for r in needs], "ok": not needs}


def mount_deploy(app: FastAPI, container) -> None:
    c = container

    def _svc():
        return getattr(c, "deploy", None)

    def _down():
        return JSONResponse({"error": DEPLOY_DOWN}, status_code=503)

    async def _body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/api/deploy/status")
    def deploy_status():
        d = _svc()
        if d is None:
            return _down()
        return d.status()

    @app.get("/api/deploy/identity")
    async def deploy_identity(fresh: bool = False):
        who = await asyncio.to_thread(gcloud_identity, None, fresh=fresh)
        return {"identity": who, "prod": bool(who and may_touch_production(who))}

    @app.get("/api/deploy/ready")
    async def deploy_ready(app_name: str = "", env: str = "dev"):
        """READY? - one answer for the window: who is signed in, where the console is (found by
        HELIX when it can), what is running. The page turns it into plain words."""
        who = await asyncio.to_thread(gcloud_identity)
        root = await asyncio.to_thread(ensure_console_root, c)
        d = _svc()
        running = d.status()["job"] if d is not None else None
        return {"identity": who, "prod": bool(who and may_touch_production(who)), "console_root": root,
                "running": running}

    @app.get("/api/deploy/targets")
    async def deploy_targets(app_name: str = "", env: str = "dev"):
        d = _svc()
        if d is None:
            return _down()
        try:
            return await asyncio.to_thread(d.targets, app_name, env)
        except ValueError:
            return JSONResponse({"error": f"{env} is not an environment."}, status_code=400)

    @app.post("/api/deploy")
    async def deploy_run(request: Request):
        d = _svc()
        if d is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(d.deploy, str(b.get("app") or ""), str(b.get("env") or ""),
                                    phrase=str(b.get("phrase") or ""), sure=bool(b.get("sure")),
                                    action=str(b.get("action") or "be-deploy"))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.post("/api/deploy/rollback")
    async def deploy_rollback(request: Request):
        d = _svc()
        if d is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(d.rollback, str(b.get("app") or ""), str(b.get("env") or ""), str(b.get("to") or ""),
                                    phrase=str(b.get("phrase") or ""), sure=bool(b.get("sure")))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.post("/api/deploy/create_plan")
    async def deploy_create_plan(request: Request):
        d = _svc()
        if d is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(d.create_plan, str(b.get("app") or ""), str(b.get("env") or ""),
                                    typed=str(b.get("typed") or ""), sure=bool(b.get("sure")))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.post("/api/deploy/create_run")
    async def deploy_create_run(request: Request):
        d = _svc()
        if d is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(d.create_run, str(b.get("app") or ""), str(b.get("env") or ""),
                                    typed=str(b.get("typed") or ""), sure=bool(b.get("sure")),
                                    phrase=str(b.get("phrase") or ""))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.get("/api/deploy/followups")
    def deploy_followups(app_name: str = "", env: str = "qa"):
        d = _svc()
        if d is None:
            return _down()
        r = d.followups(app_name, env)
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.get("/api/pulse")
    def pulse():
        """What HELIX's Python is costing this PC right now (adapters/pulse.py)."""
        from helix.adapters.pulse import PULSE
        return PULSE.read()

    @app.get("/api/deploy/tools")
    async def deploy_tools():
        """What the deploy lane needs on this PC and whether it is there. Each row carries the fix
        in plain words; the page lights Settings when a row is not ok."""
        return await asyncio.to_thread(tools_report, c)

    @app.put("/api/deploy/console_root")
    async def deploy_console_root(request: Request):
        settings = getattr(c, "settings", None)
        if settings is None:
            return JSONResponse({"error": "Settings are not available."}, status_code=503)
        b = await _body(request)
        path = str(b.get("path") or "").strip().strip('"')
        if path and not (Path(path) / "dev.ps1").is_file():
            return JSONResponse({"error": f"No dev.ps1 in {path}. Point at the BRMS_MES_WEB_VERSION checkout."}, status_code=400)
        settings.set(CONSOLE_ROOT_SETTING, path)
        return {"ok": True, "path": path}

    @app.post("/api/fleet/open_folder")
    async def open_folder(request: Request):
        fleet = getattr(c, "fleet", None)
        b = await _body(request)
        app_key = str(b.get("app") or "").strip().upper()
        links = fleet._links() if fleet is not None and hasattr(fleet, "_links") else {}
        folder = getattr(links.get(app_key), "folder", None)
        if not folder:
            return JSONResponse({"error": f"{app_key} has no folder on this PC yet. Set it in the gear."}, status_code=404)
        if not Path(folder).is_dir():
            return JSONResponse({"error": f"The folder is not there: {folder}"}, status_code=404)
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer.exe", folder], creationflags=_QUIET)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"ok": True, "folder": folder}

    @app.post("/api/fleet/merge_check")
    async def merge_check(request: Request):
        """Would `from` merge into `into` cleanly? Uses the linked clone: fetch, then git merge-tree
        (no checkout is touched). Without a clone on this PC there is nothing to check against."""
        fleet = getattr(c, "fleet", None)
        b = await _body(request)
        app_key = str(b.get("app") or "").strip().upper()
        src = str(b.get("from") or "").strip()
        dst = str(b.get("into") or "main").strip()
        if not app_key or app_key not in apps(COMPANIES[0].id) or not src:
            return JSONResponse({"error": "Name the app and the branch to merge."}, status_code=400)
        links = fleet._links() if fleet is not None and hasattr(fleet, "_links") else {}
        folder = getattr(links.get(app_key), "folder", None)
        if not folder or not (Path(folder) / ".git").exists():
            return JSONResponse({"error": f"{app_key} needs a git clone linked on this PC (the gear) to check for conflicts."}, status_code=409)

        def run():
            def git(*args, timeout=120):
                p = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=timeout,
                                   encoding="utf-8", errors="replace", creationflags=_QUIET)
                return p.returncode, (p.stdout or ""), (p.stderr or "")
            rc, _, err = git("fetch", "--all", "--prune", timeout=180)
            if rc != 0:
                return {"error": f"git fetch failed: {err.strip()[:300]}"}
            rc, base, _ = git("merge-base", f"origin/{dst}", f"origin/{src}")
            if rc != 0:
                return {"error": f"No common history between {src} and {dst} on origin."}
            rc, out, err = git("merge-tree", "--write-tree", "--name-only", f"origin/{dst}", f"origin/{src}")
            conflicts = []
            if rc == 1:
                lines = out.splitlines()
                # first line is the tree oid; after a blank line the conflicted paths
                blank = lines.index("") if "" in lines else 0
                conflicts = [ln for ln in lines[blank + 1:] if ln.strip()]
            elif rc not in (0, 1):
                return {"error": f"git merge-tree could not run (git 2.38+ needed): {err.strip()[:200]}"}
            rc, ahead, _ = git("rev-list", "--count", f"origin/{dst}..origin/{src}")
            rc2, behind, _ = git("rev-list", "--count", f"origin/{src}..origin/{dst}")
            return {"ok": True, "from": src, "into": dst, "clean": not conflicts, "conflicts": conflicts,
                    "ahead": int(ahead.strip() or 0), "behind": int(behind.strip() or 0), "base": base.strip()[:7]}

        jobs = getattr(c, "jobs", None)
        job = jobs.start("merge", f"Check {src} into {dst} ({app_key})", app=app_key, note="git fetch, then a merge on paper") if jobs is not None else None
        r = await asyncio.to_thread(run)
        if job is not None:
            if "error" in r:
                jobs.line(job, "[helix] " + str(r["error"]))
                jobs.finish(job, False, note=str(r["error"]))
            else:
                for path in r.get("conflicts") or []:
                    jobs.line(job, "conflict: " + path)
                jobs.finish(job, True, note=("Clean - no conflicts" if r.get("clean") else f"{len(r.get('conflicts') or [])} conflicting file(s)"))
        return r if "error" not in r else JSONResponse(r, status_code=400)
