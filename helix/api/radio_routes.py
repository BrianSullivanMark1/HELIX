"""HELIX RADIO's routes. Human-only, on the local face.

  GET  /api/radio?app=            the deck: station, folders, tracks (with cached flags), bucket, cache
  POST /api/radio/upload          multipart: file + title, artist, theme, bpm, folder -> the new track
  PUT  /api/radio/upload?name=&title=&artist=&theme=&bpm=&folder=
                                  the file as the body, streamed: a task with real progress (the
                                  sending half, then the bucket half), cancellable -> the new track
  GET  /api/radio/play/{id}       the file, with Range (seek) once cached; streamed while it arrives
  POST /api/radio/prefetch        {"id"}  start caching a track in the background (the next one)
  POST /api/radio/cache           cache every track on this PC, as one task
  PUT  /api/radio/station         {"app": "MES", "name": "MES FM"}  (catalog) or {"local": "..."} (this HELIX only)
  PUT  /api/radio/hide            {"id": "..."}   hide, never delete
  PUT  /api/radio/track           {"id", "folder"?, "title"?, "artist"?, "theme"?, "bpm"?}  shelve / retitle
  POST /api/radio/folder          {"path": "Rock/80s"}         a new shelf
  PUT  /api/radio/folder          {"old": "Rock", "new": "Rock and Roll"}   rename (tracks follow)
  DELETE /api/radio/folder?path=  remove an EMPTY shelf
  PUT  /api/radio/bucket          {"bucket": "helix-radio-mark1"}

Mounted by server.build_app with `mount_radio(app, container)`.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

RADIO_DOWN = "The radio is not available on this HELIX."
BUCKET_SETTING = "radio_bucket"
STATION_SETTING = "radio_station"
MAX_UPLOAD = 600 * 1024 * 1024
TOO_BIG = "That file is over 600 MB."
STOPPED = "Stopped before it reached the bucket."


def mount_radio(app: FastAPI, container) -> None:
    c = container

    def _radio():
        return getattr(c, "radio", None)

    def _jobs():
        return getattr(c, "jobs", None)

    def _down():
        return JSONResponse({"error": RADIO_DOWN}, status_code=503)

    def _who() -> str:
        try:
            return str(getattr(c, "identity", lambda: "")() or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _body(request: Request) -> dict:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return b if isinstance(b, dict) else {}

    @app.get("/api/radio")
    async def radio_board(app_key: str = "default"):
        radio = _radio()
        if radio is None:
            return _down()
        return await asyncio.to_thread(radio.board, app_key or "default")

    @app.post("/api/radio/upload")
    async def radio_upload(file: UploadFile, title: str = Form(""), artist: str = Form(""),
                           theme: str = Form(""), bpm: str = Form(""), folder: str = Form("")):
        radio = _radio()
        if radio is None:
            return _down()
        name = Path(file.filename or "track").name
        tmpdir = Path(tempfile.mkdtemp(prefix="helix_radio_up_"))
        local = tmpdir / name
        size = 0
        with local.open("wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    fh.close(); shutil.rmtree(tmpdir, ignore_errors=True)
                    return JSONResponse({"error": TOO_BIG}, status_code=413)
                fh.write(chunk)
        try:
            bpm_n = int(bpm) if str(bpm).strip().isdigit() else None
            track, why = await asyncio.to_thread(
                radio.add_track, local, title=title, artist=artist, theme=theme, bpm=bpm_n, uploaded_by=_who(), folder=folder)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True, "track": track}

    @app.put("/api/radio/upload")
    async def radio_upload_stream(request: Request, name: str = "track", title: str = "", artist: str = "",
                                  theme: str = "", bpm: str = "", folder: str = ""):
        """The body is the file. Progress is real on both halves; a cancel (the task's button, or the
        page dropping the connection) leaves nothing in the catalog."""
        radio = _radio()
        if radio is None:
            return _down()
        jobs = _jobs()
        fname = Path(name or "track").name or "track"
        from helix.services.radio import CANCEL_UPLOAD, kind_of
        kind, why = kind_of(fname)
        if kind is None:
            return JSONResponse({"error": why}, status_code=400)
        length = request.headers.get("content-length")
        total = int(length) if length and length.isdigit() else None
        if total is not None and total > MAX_UPLOAD:
            return JSONResponse({"error": TOO_BIG}, status_code=413)
        job = jobs.start("upload", f"Upload {title.strip() or fname}", by=_who(), progress=0.0, note="Sending to HELIX",
                         cancel=lambda: None, cancel_note=CANCEL_UPLOAD) if jobs is not None else None
        tmpdir = Path(tempfile.mkdtemp(prefix="helix_radio_up_"))
        local = tmpdir / fname
        size = 0
        try:
            with local.open("wb") as fh:
                async for chunk in request.stream():
                    if job is not None and jobs.cancelled(job):
                        jobs.finish(job, False, note=STOPPED)
                        return JSONResponse({"error": STOPPED, "cancelled": True}, status_code=409)
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        if job is not None:
                            jobs.finish(job, False, note=TOO_BIG)
                        return JSONResponse({"error": TOO_BIG}, status_code=413)
                    fh.write(chunk)
                    if job is not None and total:
                        jobs.progress(job, 0.5 * size / total, f"Sending to HELIX - {size // (1024 * 1024)} of {total // (1024 * 1024)} MB")
            if size == 0:
                if job is not None:
                    jobs.finish(job, False, note="The file was empty.")
                return JSONResponse({"error": "The file was empty."}, status_code=400)
            if job is not None:
                jobs.progress(job, 0.5, "Storing in the bucket")
            bpm_n = int(bpm) if str(bpm).strip().isdigit() else None
            track, why = await asyncio.to_thread(
                radio.add_track, local, title=title, artist=artist, theme=theme, bpm=bpm_n, uploaded_by=_who(),
                folder=folder, job=job)
        except Exception as e:  # noqa: BLE001 - a dropped connection lands here
            if job is not None:
                jobs.line(job, f"[helix] {type(e).__name__}: {e}")
                jobs.finish(job, False, note=STOPPED if (job.cancel_asked) else "The upload broke off.")
            raise
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if why:
            if job is not None:
                jobs.line(job, "[helix] " + why)
                jobs.finish(job, False, note=why)
            return JSONResponse({"error": why, "cancelled": bool(job is not None and job.cancel_asked)}, status_code=400)
        if job is not None:
            jobs.line(job, f"track {track['id']} in the catalog" + (f" (shelf: {track['folder']})" if track.get("folder") else ""))
            jobs.finish(job, True, note="In the catalog")
        return {"ok": True, "track": track, "job": job.id if job is not None else None}

    @app.get("/api/radio/play/{tid}")
    async def radio_play(tid: str, request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        got = await asyncio.to_thread(radio.play_stream, tid)
        if "problem" in got:
            return JSONResponse({"error": got["problem"]}, status_code=404)
        mime = got["mime"]
        if "file" in got:
            return FileResponse(str(got["file"]), media_type=mime, headers={"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"})
        f = got["fetching"]

        async def grow():
            # Never hold the .part open across waits: on Windows an open file cannot be renamed,
            # and the download thread renames .part to the final name when it is complete.
            sent = 0
            total = f.total or 0
            while sent < total:
                src = f.final if f.final.exists() else f.part
                chunk = b""
                try:
                    with src.open("rb") as fh:
                        fh.seek(sent)
                        chunk = fh.read(1024 * 1024)
                except OSError:
                    chunk = b""
                if chunk:
                    sent += len(chunk)
                    yield chunk
                    continue
                if f.done.is_set():
                    if f.error or not f.final.exists():
                        return
                    continue        # the rename just landed: read the rest from the final file
                await asyncio.sleep(0.15)

        headers = {"Cache-Control": "no-store", "Content-Length": str(f.total), "X-Helix-Arriving": "1"}
        return StreamingResponse(grow(), media_type=mime, headers=headers)

    @app.post("/api/radio/prefetch")
    async def radio_prefetch(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        b = await _body(request)
        why = await asyncio.to_thread(radio.prefetch, str(b.get("id") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=404)
        return {"ok": True}

    @app.post("/api/radio/cache")
    async def radio_cache_all():
        radio = _radio()
        if radio is None:
            return _down()
        job, why = await asyncio.to_thread(radio.cache_all, _who())
        if why:
            return JSONResponse({"error": why}, status_code=409)
        return {"ok": True, "job": job}

    @app.put("/api/radio/station")
    async def radio_station(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        body = await _body(request)
        settings = getattr(c, "settings", None)
        if "local" in body and settings is not None:
            settings.set(STATION_SETTING, str(body.get("local") or "").strip()[:40])
            return {"ok": True, "station": radio.station_name("default")}
        why = await asyncio.to_thread(radio.set_station, str(body.get("app") or "default"), str(body.get("name") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True}

    @app.put("/api/radio/hide")
    async def radio_hide(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        body = await _body(request)
        why = await asyncio.to_thread(radio.hide, str(body.get("id") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True}

    @app.put("/api/radio/track")
    async def radio_track(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        b = await _body(request)
        tid = str(b.get("id") or "")
        track, why = None, None
        if "folder" in b:
            track, why = await asyncio.to_thread(radio.move_track, tid, str(b.get("folder") or ""))
            if why:
                return JSONResponse({"error": why}, status_code=400)
        if any(k in b for k in ("title", "artist", "theme", "bpm")):
            bpm = b.get("bpm")
            bpm_n = int(bpm) if str(bpm or "").strip().isdigit() else (None if bpm in (None, "") else 0)
            track, why = await asyncio.to_thread(radio.retitle, tid, title=b.get("title"), artist=b.get("artist"),
                                                 theme=b.get("theme"), bpm=bpm_n)
            if why:
                return JSONResponse({"error": why}, status_code=400)
        if track is None:
            return JSONResponse({"error": "Nothing to change."}, status_code=400)
        return {"ok": True, "track": track}

    @app.post("/api/radio/folder")
    async def radio_folder_new(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        b = await _body(request)
        folders, why = await asyncio.to_thread(radio.create_folder, str(b.get("path") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True, "folders": folders}

    @app.put("/api/radio/folder")
    async def radio_folder_rename(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        b = await _body(request)
        folders, why = await asyncio.to_thread(radio.rename_folder, str(b.get("old") or ""), str(b.get("new") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True, "folders": folders}

    @app.delete("/api/radio/folder")
    async def radio_folder_remove(path: str = ""):
        radio = _radio()
        if radio is None:
            return _down()
        folders, why = await asyncio.to_thread(radio.remove_folder, path)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True, "folders": folders}

    @app.put("/api/radio/bucket")
    async def radio_bucket(request: Request):
        settings = getattr(c, "settings", None)
        if settings is None:
            return JSONResponse({"error": "Settings are not available on this HELIX."}, status_code=503)
        body = await _body(request)
        bucket = str(body.get("bucket") or "").strip().removeprefix("gs://").strip("/")
        if bucket and not all(ch.isalnum() or ch in "-_." for ch in bucket):
            return JSONResponse({"error": "A bucket name is letters, digits, dashes, dots and underscores."}, status_code=400)
        settings.set(BUCKET_SETTING, bucket)
        return {"ok": True, "bucket": bucket or None}
