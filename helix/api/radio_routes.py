"""HELIX RADIO's routes. Human-only, on the local face.

  GET  /api/radio?app=            the deck: station, tracks, bucket, one sentence if off
  POST /api/radio/upload          multipart: file + title, artist, theme, bpm -> the new track
  GET  /api/radio/play/{id}       the file, with Range (seek), cached from the bucket once
  PUT  /api/radio/station         {"app": "MES", "name": "MES FM"}  (catalog) or {"local": "..."} (this HELIX only)
  PUT  /api/radio/hide            {"id": "..."}   hide, never delete
  PUT  /api/radio/bucket          {"bucket": "helix-radio-mark1"}

Mounted by server.build_app with `mount_radio(app, container)`.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

RADIO_DOWN = "The radio is not available on this HELIX."
BUCKET_SETTING = "radio_bucket"
STATION_SETTING = "radio_station"
MAX_UPLOAD = 600 * 1024 * 1024


def mount_radio(app: FastAPI, container) -> None:
    c = container

    def _radio():
        return getattr(c, "radio", None)

    def _down():
        return JSONResponse({"error": RADIO_DOWN}, status_code=503)

    @app.get("/api/radio")
    async def radio_board(app_key: str = "default"):
        radio = _radio()
        if radio is None:
            return _down()
        return await asyncio.to_thread(radio.board, app_key or "default")

    @app.post("/api/radio/upload")
    async def radio_upload(file: UploadFile, title: str = Form(""), artist: str = Form(""),
                           theme: str = Form(""), bpm: str = Form("")):
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
                    return JSONResponse({"error": "That file is over 600 MB."}, status_code=413)
                fh.write(chunk)
        try:
            bpm_n = int(bpm) if str(bpm).strip().isdigit() else None
            who = ""
            try:
                who = str(getattr(c, "identity", lambda: "")() or "")
            except Exception:  # noqa: BLE001
                who = ""
            track, why = await asyncio.to_thread(
                radio.add_track, local, title=title, artist=artist, theme=theme, bpm=bpm_n, uploaded_by=who)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True, "track": track}

    @app.get("/api/radio/play/{tid}")
    async def radio_play(tid: str):
        radio = _radio()
        if radio is None:
            return _down()
        local, mime, why = await asyncio.to_thread(radio.play_file, tid)
        if local is None:
            return JSONResponse({"error": why}, status_code=404)
        return FileResponse(str(local), media_type=mime, headers={"Cache-Control": "private, max-age=3600"})

    @app.put("/api/radio/station")
    async def radio_station(request: Request):
        radio = _radio()
        if radio is None:
            return _down()
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
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
        body = await request.json()
        why = await asyncio.to_thread(radio.hide, str((body or {}).get("id") or ""))
        if why:
            return JSONResponse({"error": why}, status_code=400)
        return {"ok": True}

    @app.put("/api/radio/bucket")
    async def radio_bucket(request: Request):
        settings = getattr(c, "settings", None)
        if settings is None:
            return JSONResponse({"error": "Settings are not available on this HELIX."}, status_code=503)
        body = await request.json()
        bucket = str((body or {}).get("bucket") or "").strip().removeprefix("gs://").strip("/")
        if bucket and not all(ch.isalnum() or ch in "-_." for ch in bucket):
            return JSONResponse({"error": "A bucket name is letters, digits, dashes, dots and underscores."}, status_code=400)
        settings.set(BUCKET_SETTING, bucket)
        return {"ok": True, "bucket": bucket or None}
