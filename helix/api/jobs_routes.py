"""CURRENT TASKS' routes - the register, one task's log, cancel, dismiss, clear.

  GET    /api/jobs                the register, newest first (no lines)
  GET    /api/jobs/{id}           one task with its lines (the log window)
  POST   /api/jobs/{id}/cancel    ask it to stop (only when it declared a way; the sentence otherwise)
  DELETE /api/jobs/{id}           dismiss a finished task from the list
  POST   /api/jobs/clear          dismiss every finished task
  POST   /api/jobs                open a task the PAGE drives (an upload's sending phase):
                                  {kind, title, note?, cancel_note?} -> the task
  PUT    /api/jobs/{id}/progress  {progress, note?}   the page reports where a page-driven task is
  POST   /api/jobs/{id}/finish    {ok, note?}         the page closes a page-driven task

Mounted by server.build_app with `mount_jobs(app, container)`.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from helix.services.jobs import NO_SUCH_JOB

JOBS_DOWN = "The task register is not available on this HELIX."
PAGE_KINDS = {"upload", "task"}


def mount_jobs(app: FastAPI, container) -> None:
    c = container

    def _jobs():
        return getattr(c, "jobs", None)

    def _down():
        return JSONResponse({"error": JOBS_DOWN}, status_code=503)

    async def _body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/api/jobs")
    def jobs_list():
        jobs = _jobs()
        if jobs is None:
            return _down()
        return {"jobs": jobs.list()}

    @app.get("/api/jobs/{jid}")
    def jobs_one(jid: str):
        jobs = _jobs()
        if jobs is None:
            return _down()
        got = jobs.log(jid)
        if got is None:
            return JSONResponse({"error": NO_SUCH_JOB}, status_code=404)
        return got

    @app.post("/api/jobs/{jid}/cancel")
    def jobs_cancel(jid: str):
        jobs = _jobs()
        if jobs is None:
            return _down()
        why = jobs.cancel(jid)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True}

    @app.delete("/api/jobs/{jid}")
    def jobs_dismiss(jid: str):
        jobs = _jobs()
        if jobs is None:
            return _down()
        why = jobs.dismiss(jid)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True}

    @app.post("/api/jobs/clear")
    def jobs_clear():
        jobs = _jobs()
        if jobs is None:
            return _down()
        return {"ok": True, "cleared": jobs.clear_finished()}

    @app.post("/api/jobs")
    async def jobs_open(request: Request):
        jobs = _jobs()
        if jobs is None:
            return _down()
        b = await _body(request)
        kind = str(b.get("kind") or "task").strip().lower()
        if kind not in PAGE_KINDS:
            return JSONResponse({"error": "The page may open upload tasks only."}, status_code=400)
        title = str(b.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "A task needs a title."}, status_code=400)
        job = jobs.start(kind, title, note=str(b.get("note") or ""), by=str(b.get("by") or ""),
                         cancel=lambda: None, cancel_note=str(b.get("cancel_note") or "Stops the upload; nothing reaches the bucket."),
                         progress=0.0)
        return job.as_dict()

    @app.put("/api/jobs/{jid}/progress")
    async def jobs_progress(jid: str, request: Request):
        jobs = _jobs()
        if jobs is None:
            return _down()
        job = jobs.get(jid)
        if job is None:
            return JSONResponse({"error": NO_SUCH_JOB}, status_code=404)
        b = await _body(request)
        frac = b.get("progress")
        try:
            frac = None if frac is None else float(frac)
        except (TypeError, ValueError):
            frac = None
        jobs.progress(job, frac, str(b["note"]) if "note" in b else None)
        return {"ok": True, "cancel_asked": job.cancel_asked}

    @app.post("/api/jobs/{jid}/finish")
    async def jobs_finish(jid: str, request: Request):
        jobs = _jobs()
        if jobs is None:
            return _down()
        job = jobs.get(jid)
        if job is None:
            return JSONResponse({"error": NO_SUCH_JOB}, status_code=404)
        b = await _body(request)
        if job.kind not in PAGE_KINDS:
            return JSONResponse({"error": "Only page-driven tasks close from the page."}, status_code=400)
        note = b.get("note")
        if note is not None:
            jobs.line(job, "[page] " + str(note))
        jobs.finish(job, bool(b.get("ok")), note=str(note) if note is not None else None)
        return {"ok": True}
