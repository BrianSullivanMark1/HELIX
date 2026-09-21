"""THE VAULT's routes: /api/secrets (Brendan's knowledge vaults keep /api/vault/{slug}).

Values arrive in a request body over the loopback and go straight to the service; no route logs a
body, and no route ever returns one.
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from helix.services.vault import VAULT_DOWN


def mount_vault(app: FastAPI, container) -> None:
    c = container

    def _svc():
        return getattr(c, "vault", None)

    def _down():
        return JSONResponse({"error": VAULT_DOWN}, status_code=503)

    async def _body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/api/secrets")
    async def secrets_board():
        v = _svc()
        if v is None:
            return _down()
        return await asyncio.to_thread(v.board)

    @app.get("/api/secrets/{name}")
    async def secrets_one(name: str, fresh: int = 0):
        v = _svc()
        if v is None:
            return _down()
        vs, readers = await asyncio.gather(asyncio.to_thread(v.versions, name),
                                           asyncio.to_thread(v.readers, name, fresh=bool(fresh)))
        return {**vs, "readers": readers}

    @app.post("/api/secrets")
    async def secrets_create(request: Request):
        v = _svc()
        if v is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(v.create, str(b.get("name") or ""), str(b.get("value") or ""),
                                    app=str(b.get("app") or ""), env=str(b.get("env") or ""), note=str(b.get("note") or ""))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.post("/api/secrets/{name}/rotate")
    async def secrets_rotate(name: str, request: Request):
        v = _svc()
        if v is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(v.rotate, name, str(b.get("value") or ""))
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.post("/api/secrets/{name}/versions/{number}/{verb}")
    async def secrets_version_state(name: str, number: int, verb: str):
        v = _svc()
        if v is None:
            return _down()
        if verb not in ("enable", "disable"):
            return JSONResponse({"error": "A version is enabled or disabled, nothing else."}, status_code=400)
        r = await asyncio.to_thread(v.set_state, name, number, verb == "enable")
        return r if r.get("ok") else JSONResponse(r, status_code=400)

    @app.put("/api/secrets/{name}/policy")
    async def secrets_policy(name: str, request: Request):
        v = _svc()
        if v is None:
            return _down()
        b = await _body(request)
        days = b.get("days", 0)
        if days is not None:
            try:
                days = int(days)
            except (TypeError, ValueError):
                return JSONResponse({"error": "Days is a whole number, or empty for never."}, status_code=400)
        return await asyncio.to_thread(v.set_policy, name, days)

    # POST, not DELETE: the gate's answers travel in the body, and the page's helper sends none on a DELETE
    @app.post("/api/secrets/{name}/delete")
    async def secrets_delete(name: str, request: Request):
        v = _svc()
        if v is None:
            return _down()
        b = await _body(request)
        r = await asyncio.to_thread(v.delete, name, typed=str(b.get("typed") or ""), sure=bool(b.get("sure")),
                                    anyway=bool(b.get("anyway")))
        return r if r.get("ok") else JSONResponse(r, status_code=400)
