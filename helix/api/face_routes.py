"""Updates - the routes behind Settings -> Updates and the flashing Settings button.

  GET  /api/face/status     is web/dist behind web/src; has the Python changed since launch;
                            is a build running; the last build's result
  POST /api/face/build      run `npm run build` (off the event loop); the page reloads itself after
  POST /api/face/restart    relaunch HELIX so changed Python takes effect (Restarter + graceful quit)

Human-only by construction: these are HTTP routes on the local face, never tools the model can call.
Mounted by server.build_app with `mount_face(app, container)`.
"""
from __future__ import annotations

import asyncio
import threading

from fastapi import FastAPI
from fastapi.responses import JSONResponse

FACE_DOWN = "The face builder is not available on this HELIX."
NO_RESTART = "This HELIX has no relaunch hook, so it cannot restart itself from here."


def mount_face(app: FastAPI, container) -> None:
    c = container

    def _face():
        return getattr(c, "face", None)

    @app.get("/api/face/status")
    def face_status():
        face = _face()
        if face is None:
            return JSONResponse({"error": FACE_DOWN}, status_code=503)
        return face.status()

    @app.post("/api/face/build")
    async def face_build():
        face = _face()
        if face is None:
            return JSONResponse({"error": FACE_DOWN}, status_code=503)
        # Vite takes a few seconds; off the loop so the WS stream keeps breathing meanwhile.
        result = await asyncio.to_thread(face.build)
        if result.get("busy"):
            return JSONResponse(result, status_code=409)
        return result

    @app.post("/api/face/restart")
    def face_restart():
        """Spawn the next HELIX (it waits for the single-instance lock), then quit this one the
        same graceful way Settings -> Quit does. The page shows 'restarting' and polls until the
        snapshot answers again."""
        restart = getattr(c, "restart", None)
        do_quit = getattr(app.state, "quit", None)
        if not callable(restart) or do_quit is None:
            return JSONResponse({"error": NO_RESTART}, status_code=501)
        try:
            restart()
        except Exception as exc:  # noqa: BLE001 - one sentence, and this instance keeps running
            return JSONResponse({"error": f"Could not start the next HELIX: {exc}"}, status_code=500)
        app.state.quitting = True
        threading.Timer(0.4, do_quit).start()  # let this response reach the page first
        return {"ok": True}
