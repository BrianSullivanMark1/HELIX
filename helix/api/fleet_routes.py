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


def board_dict(fleet, *, profile: str | None) -> dict[str, Any]:
    """Company -> app cards -> environment rows. Cells the service has never read come back as
    the table's own knowledge (exists / names) with health and drift UNKNOWN, so the board has a
    shape before the first refresh rather than an empty page."""
    companies = []
    for co in COMPANIES:
        cells, at = fleet.snapshot(co)
        seen = {c.service.key: c for c in cells}
        cards = []
        for app in apps(co.id):
            rows = []
            for svc in FLEET:
                if svc.company == co.id and svc.app == app:
                    cell = seen.get(svc.key) or Cell(service=svc)
                    rows.append(cell_dict(cell))
            cards.append({
                "app": app,
                "repo": rows[0]["repo"] if rows else None,
                "envs": rows,
                "needs_attention": any(r["needs_attention"] for r in rows),
            })
        companies.append({
            "id": co.id, "label": co.label, "gcp_project": co.gcp_project, "region": co.region,
            "checked_at": _ts(at), "apps": cards,
        })
    readiness = [{"what": what, "ok": ok, "why": why} for what, ok, why in fleet.readiness()]
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
        try:
            # The read spawns gcloud a dozen times and takes 10-20 s. Off the event loop, or the
            # WS stream and every other route freeze for the duration.
            if app_name:
                await asyncio.to_thread(fleet.read_app, co, app_name)
            else:
                await asyncio.to_thread(fleet.read_company, co)
        finally:
            reading.release()
        return board_dict(fleet, profile=_profile())

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
