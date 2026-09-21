"""The fleet's routes - what the board page reads, and the one button it has (refresh).

Mounted by server.build_app with `mount_fleet(app, container)`. Kept out of server.py for the same
reason the fleet has its own domain file: this surface will grow a deploy lane in Phase 2, and the
laws that gate it (domain/fleet.py) are easier to audit when every route that can reach them sits
in one short file.

Phase 1 = READS ONLY. There is no verb here that changes a service, and none may be added without
`check_deploy` / `check_rollback` from the domain in front of it and the four production conditions
of HELIX_MARK1_PLAN.md §10.2 behind it.

Shape, one object the page can render without arithmetic:

  GET  /api/fleet                 the last thing seen (no read happens) + readiness + profile
  POST /api/fleet/refresh         {company?, app?} -> read now (10-20 s), then the same shape
  GET  /api/fleet/history         ?company=&app=&limit=  the ledger, newest first

Every route answers 503 with one sentence when the fleet service did not compose (a broken pack,
a missing adapter): the face says why instead of spinning.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from helix.domain.project_links import SETTING, parse_link
from helix.domain.fleet import COMPANIES, FLEET, Cell, Serving, apps, company
from helix.ports.fleet import FleetEvent

FLEET_DOWN = "The fleet service is not available on this HELIX."
BUSY = "A fleet read is already in progress - wait for it to finish."


# ------------------------------------------------------------------------------- serialization

def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serving_dict(s: Serving | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "revision": s.revision,
        "commit": s.commit,
        "dirty": s.dirty,
        "deployed_at": _ts(s.deployed_at),
        "deployed_by": s.deployed_by,
        "health": s.health.value,
        "traffic_percent": s.traffic_percent,
        "is_split": s.is_split,
        "db": s.db,
        "read_only": s.read_only,
        "flags": dict(s.flags),
    }


def cell_dict(c: Cell) -> dict[str, Any]:
    svc = c.service
    return {
        "key": svc.key,
        "company": svc.company,
        "app": svc.app,
        "env": svc.env.value,
        "exists": svc.exists,
        "run_service": svc.run_service,
        "hosting_site": svc.hosting_site,
        "repo": svc.repo,
        "branch": svc.branch,
        "health": c.health.value,
        "drift": c.drift.value,
        "behind_by": c.behind_by,
        "repo_commit": c.repo_commit,
        "checked_at": _ts(c.checked_at),
        "note": c.note,
        "repo_ok": c.repo_ok,
        "detail": c.detail,
        "served": list(c.served_revisions),
        "needs_attention": c.needs_attention,
        "api": serving_dict(c.api),
        "site": serving_dict(c.site),
    }


def event_dict(e: FleetEvent) -> dict[str, Any]:
    return {
        "id": e.id, "at": _ts(e.at), "company": e.company, "app": e.app, "env": e.env,
        "action": e.action, "by": e.by, "ok": e.ok, "commit": e.commit, "revision": e.revision,
        "from_revision": e.from_revision, "note": e.note,
    }


def link_dict(app: str, rows: list[dict], links: dict, repos_ok: bool, repos_why: str | None) -> dict[str, Any]:
    """The card's gear: where this app is linked, and whether the link works. `linked` is false
    when there is no token, or the repo did not answer the last read; `why` says which."""
    link = links.get(app.upper())
    stored = link.as_dict() if link else {}
    answered = [r["repo_ok"] for r in rows if r.get("repo_ok") is not None]
    why = None
    if not repos_ok:
        why = repos_why
    elif answered and not all(answered):
        why = next((r["note"] for r in rows if r.get("repo_ok") is False and r.get("note")), "The repo did not answer.")
    return {
        "repo": rows[0]["repo"] if rows else None,
        "branch": rows[0]["branch"] if rows else None,
        "folder": stored.get("folder"),
        "custom": bool(stored.get("repo") or stored.get("branch")),
        "linked": why is None,
        "why": why,
    }


def board_dict(fleet, *, profile: str | None) -> dict[str, Any]:
    """Company -> app cards -> environment rows. Cells the service has never read come back as
    the table's own knowledge (exists / names) with health and drift UNKNOWN, so the board has a
    shape before the first refresh rather than an empty page."""
    companies = []
    readiness_rows = fleet.readiness()
    repos_ok, repos_why = next(((ok, why) for what, ok, why in readiness_rows if what == "repos"), (True, None))
    links = fleet._links() if hasattr(fleet, "_links") else {}
    for co in COMPANIES:
        cells, at = fleet.snapshot(co)
        seen = {c.service.key: c for c in cells}
        cards = []
        for app in apps(co.id):
            rows = []
            for svc in fleet.services(co, app):
                cell = seen.get(svc.key) or Cell(service=svc)
                if cell.service.repo != svc.repo or cell.service.branch != svc.branch:
                    cell = Cell(service=svc)     # the link changed since this cell was read: show the new target, unread
                rows.append(cell_dict(cell))
            cards.append({
                "app": app,
                "repo": rows[0]["repo"] if rows else None,
                "link": link_dict(app, rows, links, repos_ok, repos_why),
                "envs": rows,
                "needs_attention": any(r["needs_attention"] for r in rows),
            })
        companies.append({
            "id": co.id, "label": co.label, "gcp_project": co.gcp_project, "region": co.region,
            "checked_at": _ts(at), "apps": cards,
        })
    readiness = [{"what": what, "ok": ok, "why": why} for what, ok, why in readiness_rows]
    return {"profile": profile, "readiness": readiness, "companies": companies}


# ------------------------------------------------------------------------------------ routes

def mount_fleet(app: FastAPI, container) -> None:
    c = container
    reading = threading.Lock()

    def _fleet():
        return getattr(c, "fleet", None)

    def _profile() -> str | None:
        p = getattr(c, "runtime_profile", None)
        return getattr(p, "value", None) if p is not None else None

    def _down():
        return JSONResponse({"error": FLEET_DOWN}, status_code=503)

    @app.get("/api/fleet")
    def fleet_board():
        fleet = _fleet()
        if fleet is None:
            return _down()
        return board_dict(fleet, profile=_profile())

    @app.post("/api/fleet/refresh")
    async def fleet_refresh(request: Request):
        fleet = _fleet()
        if fleet is None:
            return _down()
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a bare POST means "everything"
            body = {}
        if not isinstance(body, dict):
            body = {}
        co = company(str(body.get("company") or COMPANIES[0].id))
        if co is None:
            return JSONResponse({"error": "no such company"}, status_code=404)
        app_name = str(body.get("app") or "").strip().upper() or None
        if app_name and app_name not in apps(co.id):
            return JSONResponse({"error": f"no such app in {co.label}: {app_name}"}, status_code=404)
        if not reading.acquire(blocking=False):
            return JSONResponse({"error": BUSY, "busy": True}, status_code=409)
        jobs = getattr(c, "jobs", None)
        job = jobs.start("read", f"Reading {app_name or 'the whole fleet'}", app=app_name or "",
                         note="Asking Cloud Run and GitHub") if jobs is not None else None
        ok = False
        try:
            # The read spawns gcloud a dozen times and takes 10-20 s. Off the event loop, or the
            # WS stream and every other route freeze for the duration.
            if app_name:
                cells = await asyncio.to_thread(fleet.read_app, co, app_name)
            else:
                cells = await asyncio.to_thread(fleet.read_company, co)
            ok = True
            if job is not None:
                for cell in cells:
                    jobs.line(job, f"{cell.service.app} {cell.service.env.value}: {cell.health.value}, {cell.drift.value}" + (f" - {cell.note}" if cell.note else ""))
        finally:
            reading.release()
            if job is not None:
                jobs.finish(job, ok, note=("Board is fresh" if ok else "The read failed"))
        return board_dict(fleet, profile=_profile())

    @app.put("/api/fleet/link")
    async def fleet_link(request: Request):
        """Save one app's project link (repo / branch / folder). Empty fields fall back to the
        table. The next Read uses it; the board answers at once with the new target, unread."""
        fleet = _fleet()
        if fleet is None:
            return _down()
        settings = getattr(c, "settings", None)
        if settings is None:
            return JSONResponse({"error": "Settings are not available on this HELIX."}, status_code=503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        co = company(str(body.get("company") or COMPANIES[0].id))
        app_name = str(body.get("app") or "").strip().upper()
        if co is None or app_name not in apps(co.id):
            return JSONResponse({"error": f"no such app: {app_name or '?'}"}, status_code=404)
        link, why = parse_link(body)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        stored = settings.get(SETTING) or {}
        stored = dict(stored) if isinstance(stored, dict) else {}
        if link.as_dict():
            stored[app_name] = link.as_dict()
        else:
            stored.pop(app_name, None)
        settings.set(SETTING, stored)
        return board_dict(fleet, profile=_profile())

    @app.get("/api/fleet/git")
    async def fleet_git(app_name: str = "", company_id: str = ""):
        """The Git drawer's graph: branches and the last commits of the linked branch (plus the tips
        of the other branches), read from GitHub with the one token. Read-only."""
        fleet = _fleet()
        if fleet is None:
            return _down()
        co = company(company_id or COMPANIES[0].id)
        app_key = app_name.strip().upper()
        if co is None or app_key not in apps(co.id):
            return JSONResponse({"error": f"no such app: {app_key or '?'}"}, status_code=404)
        svc = next(iter(fleet.services(co, app_key)), None)
        if svc is None:
            return JSONResponse({"error": "no cells"}, status_code=404)
        repos = getattr(fleet, "_repos", None)
        if repos is None or not hasattr(repos, "commits"):
            return JSONResponse({"error": "This HELIX cannot read commit history."}, status_code=501)
        ok, why = repos.available()
        if not ok:
            return JSONResponse({"error": why, "repo": svc.repo, "branch": svc.branch}, status_code=409)

        def read():
            branches, why_b = repos.branches(svc.repo)
            commits, why_c = repos.commits(svc.repo, svc.branch, limit=40)
            seen = {c["sha"] for c in commits}
            extra = 0
            for b in branches:
                if b["sha"] in seen or b["name"] == svc.branch or extra >= 6:
                    continue
                more, _ = repos.commits(svc.repo, b["sha"], limit=12)
                for c in more:
                    if c["sha"] not in seen:
                        seen.add(c["sha"]); commits.append(c)
                extra += 1
            commits.sort(key=lambda c: c["at"], reverse=True)
            return {"repo": svc.repo, "branch": svc.branch, "branches": branches, "commits": commits,
                    "problem": why_c or why_b}

        return await asyncio.to_thread(read)

    @app.post("/api/fleet/scan")
    async def fleet_scan(request: Request):
        """The secrets scan for one app: the linked folder when there is one (git-tracked files
        only, so ignored files never count), else the branch on GitHub. Findings are masked."""
        fleet = _fleet()
        if fleet is None:
            return _down()
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
        co = company(str(body.get("company") or COMPANIES[0].id))
        app_key = str(body.get("app") or "").strip().upper()
        if co is None or app_key not in apps(co.id):
            return JSONResponse({"error": f"no such app: {app_key or '?'}"}, status_code=404)
        svc = next(iter(fleet.services(co, app_key)), None)
        if svc is None:
            return JSONResponse({"error": "no cells"}, status_code=404)
        branch = str(body.get("branch") or svc.branch)
        links = fleet._links() if hasattr(fleet, "_links") else {}
        folder = getattr(links.get(app_key), "folder", None)

        def run():
            from helix.domain.secret_scan import scan_files
            if folder:
                from pathlib import Path
                import subprocess
                root = Path(folder)
                if not root.is_dir():
                    return {"error": f"The linked folder does not exist on this PC: {folder}"}
                try:
                    ls = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=60)
                    names = [n for n in ls.stdout.decode("utf-8", "replace").split("\0") if n] if ls.returncode == 0 else None
                except Exception:  # noqa: BLE001
                    names = None
                if names is None:   # not a git checkout: walk it
                    names = [str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*") if p.is_file()][:4000]

                def files():
                    for n in names:
                        fp = root / n
                        try:
                            yield n, (fp.read_bytes() if fp.stat().st_size <= 400 * 1024 else None)
                        except OSError:
                            yield n, None
                rep = scan_files(files())
                return {"where": "folder", "folder": folder, "branch": None, **rep.as_dict()}
            repos = getattr(fleet, "_repos", None)
            if repos is None or not hasattr(repos, "tree"):
                return {"error": "This HELIX cannot read the repo's files."}
            ok, why = repos.available()
            if not ok:
                return {"error": why}
            entries, why_t = repos.tree(svc.repo, branch)
            if why_t:
                return {"error": why_t}
            from helix.domain.secret_scan import scannable
            entries = entries[:1500]

            def gh_files():
                budget = 350
                for e in entries:
                    if not scannable(e["path"]) or e["size"] > 400 * 1024:
                        yield e["path"], None
                        continue
                    if budget <= 0:
                        yield e["path"], None
                        continue
                    budget -= 1
                    yield e["path"], repos.blob(svc.repo, e["sha"])
            rep = scan_files(gh_files())
            return {"where": "github", "folder": None, "repo": svc.repo, "branch": branch, **rep.as_dict()}

        jobs = getattr(c, "jobs", None)
        job = jobs.start("scan", f"Scan {app_key} for secrets", app=app_key, note=("the linked folder" if folder else f"GitHub, branch {branch}")) if jobs is not None else None
        result = await asyncio.to_thread(run)
        if job is not None:
            if "error" in result:
                jobs.line(job, "[helix] " + str(result["error"]))
                jobs.finish(job, False, note=str(result["error"]))
            else:
                n = len(result.get("findings") or [])
                for f in (result.get("findings") or [])[:200]:
                    jobs.line(job, f"{f.get('severity', '')}: {f.get('path', '?')}:{f.get('line', '?')}  {f.get('kind', '')}  {f.get('excerpt', '')}")
                jobs.finish(job, True, note=(f"{n} finding{'s' if n != 1 else ''}" if n else "Nothing found"))
        if "error" in result:
            return JSONResponse(result, status_code=400)
        return result

    @app.get("/api/fleet/history")
    def fleet_history(company_id: str = Query("", alias="company"), app: str = "", limit: int = 50):
        fleet = _fleet()
        if fleet is None:
            return _down()
        co = company(company_id or COMPANIES[0].id)
        if co is None:
            return JSONResponse({"error": "no such company"}, status_code=404)
        rows = fleet.history(co, app.strip().upper() or None, limit=limit)
        return {"company": co.id, "events": [event_dict(e) for e in rows]}
