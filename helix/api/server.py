"""The web shell's server: FastAPI over 127.0.0.1 — the SPA, the API, the event stream.

Surface map:
  /                     the built React app (web/dist), SPA-fallback routed
  /builds/…             static files of the user's builds (viewer pages, exports, assets)
  /ws                   the ONE event stream (ShellSession.push lands here)
  /api/…                actions + reads; every route is a thin call into services or the shell

Security posture (localhost is still an audience): bind 127.0.0.1 only; every /api and /ws request
must carry the per-install token (X-Helix-Token header, or ?t= on the WS/URL) — pywebview and the
opened browser tab get it in the launch URL; and any request carrying an Origin/Host that isn't
localhost is refused, so a random web page poking local ports can neither read nor act. Static
routes are tokenless (the SPA must load before it knows the token) but same-origin gated.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket
import threading
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from helix.domain import cadpy
from helix.domain.constitution import LOCKED_SETTINGS
from helix.domain.models import BuildKind
from helix.domain.vocabulary import kind_label
from helix.logging_setup import get_logger
from helix.services.camera import read_layout
from helix.services.connections import CONNECTABLE

_LOG = get_logger("webserver")

TOKEN_SETTING = "web_token"
PORT_SETTING = "web_port"
DEFAULT_PORT = 8737

# Settings the web Settings page may read/write. An allowlist, not a passthrough: locked settings
# stay locked (the Constitution), credentials are write-only (presence is reported, values never).
_SETTING_KEYS = (
    "wake_word", "narration_mode", "proactive_speech", "trust_household_voice",
    "file_write_access", "evolve_enabled", "model_detail", "tts_voice", "tts_rate",
    "voice_input_on", "remote_enabled", "remote_lan", "auto_deep_turns",
    # The camera panel's preferences: which camera (by its label — browser device ids are
    # per-origin and change), a mirrored preview, clip length, and whether a typed message
    # while the panel is live carries the current view along with it.
    "camera_device", "camera_mirror", "camera_clip_seconds", "camera_attach_view",
)
_SECRET_SETTINGS = ("claude_api_key", "claude_code_oauth_token")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EventHub:
    """Fan-out from any thread to every connected WebSocket, through the asyncio loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set = set()
        self._lock = threading.Lock()

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.discard(ws)

    def push(self, event: dict) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        text = json.dumps(event)

        def _send() -> None:
            with self._lock:
                clients = list(self._clients)
            for ws in clients:
                asyncio.ensure_future(self._send_one(ws, text))

        try:
            loop.call_soon_threadsafe(_send)
        except RuntimeError:
            pass

    async def _send_one(self, ws: WebSocket, text: str) -> None:
        try:
            await ws.send_text(text)
        except Exception:  # noqa: BLE001 — a dropped tab just leaves
            self.remove(ws)


def build_app(container, shell, hub: EventHub, web_dist: Path | None) -> FastAPI:
    c = container
    token = (c.settings.get(TOKEN_SETTING) or "").strip()
    if not token:
        token = secrets.token_urlsafe(24)
        c.settings.set(TOKEN_SETTING, token)

    app = FastAPI(title="HELIX", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token
    servers: dict[str, int] = {}  # slug -> port of a running backend app

    def _tailnet_ok(name: str) -> bool:
        """A MagicDNS hostname (machine.tailnet.ts.net) — the shape `tailscale serve` forwards
        with. Honoured ONLY while the user has remote access switched on in Settings, read live so
        flipping the toggle needs no restart. Rebinding-safe: an attacker's domain isn't *.ts.net,
        and a foreign tailnet's name never resolves to this machine — plus every /api route still
        demands the bearer token regardless of where the request came from."""
        host = name.rsplit(":", 1)[0] if ":" in name and not name.startswith("[") else name
        return host.lower().endswith(".ts.net") and bool(c.settings.get("remote_enabled", False))

    def _local_origin(request) -> bool:
        origin = request.headers.get("origin") or ""
        host = request.headers.get("host") or ""
        ok_host = host.startswith(("127.0.0.1", "localhost")) or _tailnet_ok(host)
        ok_origin = (not origin) or origin.startswith(
            ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
        ) or (origin.startswith("https://") and _tailnet_ok(origin[len("https://"):]))
        return ok_host and ok_origin

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not _local_origin(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/"):
            sent = request.headers.get("x-helix-token") or request.query_params.get("t") or ""
            if not secrets.compare_digest(sent, app.state.token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    # ----- the event stream -----
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin") or ""
        if origin and not (origin.startswith(
                ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost"))
                or (origin.startswith("https://") and _tailnet_ok(origin[len("https://"):]))):
            await ws.close(code=4403)
            return
        sent = ws.query_params.get("t") or ""
        if not secrets.compare_digest(sent, app.state.token):
            await ws.close(code=4401)
            return
        await ws.accept()
        hub.add(ws)
        try:
            while True:
                await ws.receive_text()  # the stream is one-way; pings keep it alive
        except WebSocketDisconnect:
            pass
        finally:
            hub.remove(ws)

    # ----- shell actions -----
    @app.get("/api/snapshot")
    def snapshot():
        # 503 the moment a quit is accepted: the icon click's liveness probe (cli.backend_alive) must
        # read a TEARING-DOWN instance as dead, or a click right after "Quit HELIX" probes the last
        # seconds of the dying server, gets a 200, opens a tab on it — and then nothing is running.
        # The real teardown takes several seconds after the quit route answers; this flag doesn't.
        if getattr(app.state, "quitting", False):
            return JSONResponse({"error": "shutting down"}, status_code=503)
        return shell.snapshot()

    @app.post("/api/shell/submit")
    async def submit(request: Request):
        body = await request.json()
        shell.submit(str(body.get("text") or ""),
                     attachment_ids=list(body.get("attachments") or []),
                     from_voice=False)
        return {"ok": True}

    @app.post("/api/shell/stop")
    def stop():
        shell.stop()
        return {"ok": True}

    @app.post("/api/shell/tap")
    def tap():
        shell.tap()
        return {"ok": True}

    @app.post("/api/shell/action")
    async def action(request: Request):
        body = await request.json()
        shell.action(str(body.get("id") or ""))
        return {"ok": True}

    @app.post("/api/shell/voice")
    async def voice_op(request: Request):
        body = await request.json()
        return shell.voice_op(str(body.get("op") or ""))

    @app.post("/api/shell/quit")
    def quit_helix():
        """The polite off switch (Settings → Power). webboot wires the actual shutdown; without it
        (tests) the route reports it can't."""
        do_quit = getattr(app.state, "quit", None)
        if do_quit is None:
            return JSONResponse({"error": "no quit hook"}, status_code=501)
        # Mark the instance dying BEFORE the teardown starts: from this moment the snapshot probe
        # answers 503, so an icon click during the (seconds-long) teardown waits for the lock and
        # boots in our place instead of pointing a tab at a corpse.
        app.state.quitting = True
        threading.Timer(0.4, do_quit).start()  # let this response reach the page first
        return {"ok": True}

    @app.post("/api/shell/suggest_dismiss")
    async def suggest_dismiss(request: Request):
        body = await request.json()
        shell.suggestion_dismiss(str(body.get("id") or ""))
        return {"ok": True}

    @app.post("/api/attachments")
    async def upload(file: UploadFile, frame: str = Form("")):
        data = await file.read()
        # `frame`: the camera panel's frame id when this attachment IS the live view (the
        # "attach the view" chip) — AR callouts drawn in reply anchor to it.
        return shell.add_attachment(file.filename or "file", data, frame_id=frame)

    @app.get("/api/images/{iid}")
    def served_image(iid: str):
        """A transcript picture (a shot, a look, an attached image) — only what the shell itself
        registered, never an arbitrary path."""
        path = shell.served_image(iid)
        if path is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(str(path), headers={"Cache-Control": "private, max-age=3600"})

    # ----- the camera panel -----
    @app.post("/api/camera/open")
    def camera_open():
        return shell.camera_open()

    @app.post("/api/camera/{cam_id}/live")
    async def camera_live(cam_id: str, request: Request):
        body = await request.json()
        ok = shell.camera_live(cam_id, bool(body.get("on")), str(body.get("label") or ""))
        return {"ok": ok}

    @app.post("/api/camera/{cam_id}/frame")
    async def camera_frame(cam_id: str, request: Request):
        data = await request.body()
        ok = shell.camera_frame(cam_id, data)
        return {"ok": ok}

    @app.post("/api/camera/{cam_id}/frames")
    async def camera_frames(cam_id: str, frames: list[UploadFile], rid: str = Form(""),
                            caption: str = Form(""), mode: str = Form("still"),
                            seconds: float = Form(0.0), frame: str = Form("")):
        """A still or a clip from the panel: `frames` in time order; `rid` answers a parked look
        (empty = the user's own shot, which becomes a turn with `caption` as its question)."""
        blobs = [await f.read() for f in frames]
        ok = shell.camera_frames(cam_id, blobs, rid=rid or None, caption=caption, mode=mode,
                                 seconds=seconds, frame_id=frame)
        return {"ok": ok}

    @app.post("/api/camera/{cam_id}/cancel")
    async def camera_cancel(cam_id: str, request: Request):
        keep_open = False
        try:
            body = await request.json()
            keep_open = bool(body.get("keep_open"))
        except Exception:  # noqa: BLE001 — a bare POST closes the panel
            pass
        shell.camera_cancel(cam_id, keep_open=keep_open)
        return {"ok": True}

    @app.post("/api/camera/{cam_id}/measure")
    async def camera_measure(cam_id: str, request: Request):
        """The ruler's Send — MAKER_FLOW §5: {"mm_per_px", "reference", "items": [{"kind": "box",
        "label", "w_mm", "h_mm"} | {"kind": "distance", "label", "mm"}]} — answered with the plain
        line HELIX received; or {"cancel": true} to answer a parked measure ask with the cancel
        line (✕ on the banner). `ok` is False when the panel id is stale or nothing was measurable."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a bare POST measures nothing
            body = {}
        if not isinstance(body, dict):
            body = {}
        if body.get("cancel"):
            return {"ok": bool(shell.camera_measure_cancel(cam_id)), "line": ""}
        line = shell.camera_measured(cam_id, body)
        return {"ok": bool(line), "line": line}

    @app.get("/api/camera/holograms")
    def camera_holograms():
        """The holograms the panel can project: MODEL builds that have a baked mesh — each with its
        component layout (assets/layout.json, the §6 shape) when design_enclosure wrote one, else
        null, so the panel can draw ghost pockets inside the projected shell."""
        rows = []
        for a in c.builds.list():
            if a.build_kind != BuildKind.MODEL:
                continue
            ws = c.builds.workspace(a.slug)
            mesh = ws / "assets" / "model.stl"
            if mesh.is_file():
                rows.append({"slug": a.slug, "name": a.name,
                             "stl": f"/builds/{a.slug}/assets/model.stl",
                             "layout": read_layout(ws)})
        return {"holograms": rows}

    # ----- the Amazon cart panel -----
    @app.get("/api/cart")
    def cart_state():
        return {"cart": shell.cart_state()}

    @app.post("/api/cart/stage")
    async def cart_stage(request: Request):
        body = await request.json()
        return shell.cart_stage(str(body.get("asin") or ""), str(body.get("name") or ""),
                                body.get("price"), body.get("quantity"))

    @app.post("/api/cart/remove")
    async def cart_remove(request: Request):
        body = await request.json()
        return shell.cart_remove(str(body.get("asin") or ""))

    @app.post("/api/cart/quantity")
    async def cart_quantity(request: Request):
        body = await request.json()
        return shell.cart_quantity(str(body.get("asin") or ""), body.get("quantity"))

    @app.post("/api/cart/clear")
    def cart_clear():
        return shell.cart_clear()

    @app.post("/api/cart/open")
    def cart_open():
        return shell.cart_open()

    @app.post("/api/cart/check")
    def cart_check():
        return shell.cart_check()

    @app.post("/api/connect/{service_id}")
    async def connect(service_id: str, request: Request):
        body = await request.json()
        return shell.connect_submit(service_id, dict(body.get("values") or {}))

    # ----- builds / the menu -----
    @app.get("/api/builds")
    def builds():
        cats = c.builds.categorized()
        out = {}
        for cat, apps in cats.items():
            rows = []
            for a in apps:
                slug = a.slug
                rows.append({
                    "slug": slug, "name": a.name, "request": a.request,
                    "kind": a.build_kind.value, "label": kind_label(a.build_kind.value),
                    "entry": a.entry_point, "status": shell.board.state(slug),
                    "needs_keys": c.connections.needs_connection(slug),
                    "missing_keys": bool(c.connections.missing(slug)),
                    "docs": c.knowledge.count(slug) if a.build_kind == BuildKind.KNOWLEDGE else 0,
                })
            out[cat] = rows
        agents = [{"name": a.name, "goal": a.goal, "enabled": a.enabled,
                   "schedule": getattr(a, "schedule_hint", "") or ""} for a in c.agents.list()]
        try:
            suggested = [{"slug": s.slug, "name": s.name, "reason": s.reason}
                         for s in c.recommend.suggestions(c.builds.list())]
        except Exception:  # noqa: BLE001
            suggested = []
        return {"builds": out, "agents": agents, "suggested": suggested,
                "legend": shell.board.legend()}

    @app.post("/api/builds/{slug}/open")
    def open_build(slug: str):
        app_row = next((a for a in c.builds.list() if a.slug == slug), None)
        if app_row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        c.recommend.record_open(slug)
        shell.board.mark_seen(slug)
        shell.push({"t": "legend", "items": shell.board.legend()})
        ws = c.builds.workspace(slug)
        if app_row.build_kind == BuildKind.KNOWLEDGE:
            return {"mode": "vault", "slug": slug, "name": app_row.name}
        if app_row.build_kind == BuildKind.MODEL:
            return {"mode": "hologram", "slug": slug, "name": app_row.name}
        if app_row.entry_point and app_row.entry_point.endswith(".html"):
            return {"mode": "page", "slug": slug, "name": app_row.name,
                    "url": f"/builds/{slug}/{app_row.entry_point}"}
        if app_row.entry_point == "main.py":
            port = servers.get(slug) or free_port()
            if not c.tasks.is_running(slug):
                ok = c.tasks.run(slug, port=port, headless=True)
                if not ok:
                    return JSONResponse({"error": "could not start"}, status_code=500)
                servers[slug] = port
            return {"mode": "server", "slug": slug, "name": app_row.name, "port": servers[slug],
                    "url": f"http://127.0.0.1:{servers[slug]}/"}
        return {"mode": "folder", "slug": slug, "name": app_row.name, "path": str(ws)}

    @app.post("/api/builds/{slug}/stop")
    def stop_build_server(slug: str):
        try:
            c.tasks.stop(slug)
        finally:
            servers.pop(slug, None)
        return {"ok": True}

    @app.post("/api/builds/{slug}/run")
    def run_protocol(slug: str):
        c.recommend.record_run(slug)
        shell.board.mark_seen(slug)
        ok = c.tasks.run(slug)
        return {"ok": bool(ok)}

    @app.post("/api/builds/{slug}/edit")
    async def edit_build(slug: str, request: Request):
        body = await request.json()
        change = str(body.get("change") or "").strip()
        app_row = next((a for a in c.builds.list() if a.slug == slug), None)
        if app_row is None or not change:
            return JSONResponse({"error": "bad request"}, status_code=400)
        c.build_queue.enqueue(app_row.name, change, kind=app_row.build_kind)
        return {"ok": True}

    @app.post("/api/builds/{slug}/rename")
    async def rename_build(slug: str, request: Request):
        body = await request.json()
        new = str(body.get("name") or "").strip()
        if not new:
            return JSONResponse({"error": "empty name"}, status_code=400)
        out = c.builds.rename(slug, new)
        if out is None:
            return JSONResponse({"error": "rename refused"}, status_code=409)
        return {"ok": True, "slug": out.slug}

    @app.delete("/api/builds/{slug}")
    def delete_build(slug: str):
        # The build's own server — this HELIX's, or an orphan a previous HELIX left behind (found
        # through its pid file) — holds the workspace as its working directory, and Windows refuses
        # to move a folder under a live process. Release it first, then remove.
        try:
            c.tasks.release(slug)
        except Exception:  # noqa: BLE001
            _LOG.warning("could not release %s before deleting it", slug, exc_info=True)
        servers.pop(slug, None)
        ok = c.builds.delete(slug)
        if ok:
            from helix.domain.events import BuildDeleted

            c.bus.publish(BuildDeleted(slug))
        return {"ok": bool(ok)}

    @app.get("/api/builds/{slug}/versions")
    def versions(slug: str):
        rows = []
        for v in c.builds.versions(slug, 5):
            rows.append({"sha": v.sha, "when": v.created_at.strftime("%b %d · %I:%M %p")
                         if getattr(v, "created_at", None) else "", "message": getattr(v, "message", "")})
        return {"versions": rows}

    @app.post("/api/builds/{slug}/revert")
    async def revert(slug: str, request: Request):
        body = await request.json()
        out = c.builds.revert(slug, str(body.get("sha") or ""))
        return {"ok": out is not None}

    @app.get("/api/builds/{slug}/connections")
    def build_connections(slug: str):
        rows = []
        for conn in c.connections.declared(slug):
            has = bool(c.connections.value(conn.key))
            rows.append({"key": conn.key, "label": conn.label, "hint": conn.hint,
                         "managed": c.connections.is_managed(conn.key), "set": has})
        return {"connections": rows}

    @app.post("/api/builds/{slug}/connections")
    async def set_build_connections(slug: str, request: Request):
        body = await request.json()
        for key, value in dict(body.get("values") or {}).items():
            value = (value or "").strip()
            if value:
                c.connections.set_value(str(key), value)
        return {"ok": True}

    # ----- vault -----
    @app.get("/api/vault/{slug}")
    def vault(slug: str):
        docs = [{"id": d.id, "title": d.title, "source": d.source, "size": d.bytes}
                for d in c.knowledge.docs(slug)]
        docs.reverse()  # newest first
        name = next((a.name for a in c.knowledge.bases() if a.slug == slug), slug)
        return {"slug": slug, "name": name, "docs": docs}

    @app.post("/api/vault/{slug}/note")
    async def vault_note(slug: str, request: Request):
        body = await request.json()
        doc = c.knowledge.add_note(slug, str(body.get("text") or ""))
        return {"ok": doc is not None, "title": getattr(doc, "title", "")}

    @app.post("/api/vault/{slug}/files")
    async def vault_files(slug: str, files: list[UploadFile]):
        import tempfile

        folder = Path(tempfile.gettempdir()) / "helix-web-vault"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in files:
            safe = Path(f.filename or "file").name or "file"
            p = folder / f"{secrets.token_hex(4)}-{safe}"
            p.write_bytes(await f.read())
            paths.append(p)
        added = c.knowledge.add_files(slug, paths)
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        shell.push({"t": "builds"})
        return {"added": len(added)}

    @app.get("/api/vault/{slug}/doc/{doc_id}")
    def vault_doc(slug: str, doc_id: str):
        return {"text": c.knowledge.doc_text(slug, doc_id)}

    @app.delete("/api/vault/{slug}/doc/{doc_id}")
    def vault_remove(slug: str, doc_id: str):
        return {"ok": c.knowledge.remove_doc(slug, doc_id)}

    @app.get("/api/vault/{slug}/search")
    def vault_search(slug: str, q: str = ""):
        name = next((a.name for a in c.knowledge.bases() if a.slug == slug), None)
        hits = c.knowledge.preview(q, name) if q.strip() else []
        return {"hits": [{"title": h.title, "text": h.text, "score": h.score} for h in hits]}

    # ----- holograms (the studio) -----
    def _hologram_payload(slug: str) -> dict | None:
        ws = c.builds.workspace(slug)
        src = ws / "model.py"
        if not src.is_file():
            legacy = ws / "index.html"
            if legacy.is_file():
                return {"slug": slug, "legacy": True, "page": f"/builds/{slug}/index.html"}
            return None
        source = src.read_text(encoding="utf-8", errors="replace")
        meta = {}
        meta_file = ws / "assets" / "model.meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        files = {}
        for key, rel in (("stl", "assets/model.stl"), ("step", "assets/model.step"),
                         ("mf", "assets/model.3mf"), ("preview", "assets/preview.png")):
            files[key] = f"/builds/{slug}/{rel}" if (ws / rel).is_file() else ""
        import dataclasses

        return {
            "slug": slug, "legacy": False,
            "name": next((a.name for a in c.builds.list() if a.slug == slug), slug),
            "brief": cadpy.parse_brief(source),
            "params": [dataclasses.asdict(p) for p in cadpy.parse_params(source)],
            "files": files, "meta": meta, "engine": c.cad.version() or "",
            "source": source,
            # The maker flow (MAKER_FLOW §7): the component layout design_enclosure wrote beside
            # the mesh (null for a hologram the coder drew) and the P1S print sheet.
            "layout": read_layout(ws),
            "print_sheet": _print_sheet(slug),
        }

    def _print_sheet(slug: str) -> str:
        maker = getattr(c, "maker", None)
        if maker is None:
            return ""
        try:
            return maker.print_sheet(slug) or ""
        except Exception:  # noqa: BLE001 — the sheet is a courtesy; the studio still opens
            _LOG.warning("print sheet failed for %s", slug, exc_info=True)
            return ""

    @app.get("/api/holograms/{slug}")
    def hologram(slug: str):
        payload = _hologram_payload(slug)
        if payload is None:
            return JSONResponse({"error": "not a hologram"}, status_code=404)
        return payload

    @app.post("/api/holograms/{slug}/project")
    def hologram_project(slug: str):
        """The studio's 'Check fit on camera': the maker brain raises the camera panel when none is
        open and projects this hologram with its component layout through the shell's own camera
        command path (the same path check_fit takes), so the ghosts and the 1:1 scale ride along.
        `line` is the plain sentence the model would have read."""
        maker = getattr(c, "maker", None)
        if maker is None:
            return JSONResponse({"error": "the maker flow isn't wired"}, status_code=501)
        ok, line = maker.project(slug)
        return {"ok": bool(ok), "line": line}

    @app.post("/api/holograms/{slug}/preview")
    async def hologram_preview(slug: str, request: Request):
        body = await request.json()
        overrides = dict(body.get("overrides") or {})
        ws = c.builds.workspace(slug)
        src = ws / "model.py"
        if not src.is_file():
            return JSONResponse({"error": "not a hologram"}, status_code=404)
        out_dir = ws / "assets" / "live"
        res = c.cad.preview(src, out_dir, overrides)
        if not res.ok:
            return {"ok": False, "problem": res.problem or "The recompile failed."}
        meta = {}
        meta_file = out_dir / "model.meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except ValueError:
                meta = {}
        stamp = int(meta_file.stat().st_mtime_ns // 1_000_000) if meta_file.is_file() else 0
        return {"ok": True, "stl": f"/builds/{slug}/assets/live/model.stl?v={stamp}",
                "meta": meta, "seconds": res.seconds}

    @app.post("/api/holograms/{slug}/commit")
    async def hologram_commit(slug: str, request: Request):
        body = await request.json()
        values = dict(body.get("values") or {})
        ws = c.builds.workspace(slug)
        src = ws / "model.py"
        if not src.is_file():
            return JSONResponse({"error": "not a hologram"}, status_code=404)
        source = src.read_text(encoding="utf-8", errors="replace")
        known = {p.name: p.kind for p in cadpy.parse_params(source)}
        coerced = {}
        for key, value in values.items():
            kind = known.get(str(key))
            if kind is None:
                continue
            if kind == "number":
                try:
                    coerced[key] = float(value)
                except (TypeError, ValueError):
                    continue
            elif kind == "bool":
                coerced[key] = bool(value)
            else:
                coerced[key] = str(value)
        if not coerced:
            return {"ok": False, "problem": "No adjustable values to save."}
        src.write_text(cadpy.set_params(source, coerced), encoding="utf-8")
        try:
            c.model_baker.bake(ws)  # recompile + refresh the standalone viewer, engine cache warm
        except Exception:  # noqa: BLE001
            _LOG.warning("post-commit bake failed", exc_info=True)
        try:
            c.repo.commit_all(ws, "studio: adjust " + ", ".join(sorted(coerced)))
        except Exception:  # noqa: BLE001
            _LOG.warning("studio commit failed", exc_info=True)
        shell.push({"t": "builds"})
        return {"ok": True, **(_hologram_payload(slug) or {})}

    # ----- settings -----
    @app.get("/api/settings")
    def get_settings():
        values = {k: c.settings.get(k) for k in _SETTING_KEYS}
        values["tts_rate"] = float(values.get("tts_rate") or 1.0)
        # Camera defaults (unset = the panel's own): an un-mirrored preview (markings on a board
        # read correctly), 6-second clips, and the live view rides along with typed messages
        # while the panel is open.
        if values.get("camera_mirror") is None:
            values["camera_mirror"] = False
        if values.get("camera_attach_view") is None:
            values["camera_attach_view"] = True
        try:
            values["camera_clip_seconds"] = max(1, min(15, int(values.get("camera_clip_seconds") or 6)))
        except (TypeError, ValueError):
            values["camera_clip_seconds"] = 6
        values["camera_device"] = str(values.get("camera_device") or "")
        secretset = {k: bool((c.settings.get(k) or "").strip()) for k in _SECRET_SETTINGS}
        conns = {}
        for sid, (label, _store, fields) in CONNECTABLE.items():
            conns[sid] = {"label": label,
                          "set": all(bool(c.connections.value(k)) for k, _l, _h in fields)}
        sub = c.subscription
        sub_live = False
        try:
            sub_live = sub.active(allow_probe=False)
        except TypeError:
            sub_live = sub.active()
        except Exception:  # noqa: BLE001
            sub_live = False
        if sub_live and getattr(sub, "last_failure", lambda: None)():
            brain = ("warn", "On your Claude subscription — but a recent request on it didn't go "
                             "through.")
        elif sub_live:
            brain = ("ok", "Running on your Claude subscription — off the API meter.")
        elif secretset["claude_api_key"]:
            brain = ("warn", "Running on the API meter (Console billing).")
        elif secretset["claude_code_oauth_token"]:
            brain = ("warn", "Token saved, but the Claude Code app isn't reachable — no billing "
                             "path is active.")
        else:
            brain = ("off", "Not connected — add a subscription token (recommended) or an API key.")
        voices = ["en-GB-RyanNeural", "en-US-GuyNeural", "en-US-ChristopherNeural",
                  "en-US-JennyNeural", "en-US-AriaNeural", "en-GB-SoniaNeural",
                  "en-AU-NatashaNeural", "en-AU-WilliamNeural", "en-US-EricNeural",
                  "en-US-MichelleNeural"]
        return {"values": values, "secrets": secretset, "connections": conns,
                "brain": {"tone": brain[0], "line": brain[1]}, "voices": voices,
                "gmail": {"configured": c.gmail.configured(), "address": c.gmail.address() or ""},
                "calendar": {"configured": c.calendar.configured()}}

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await request.json()
        changed = []
        for key, value in dict(body.get("values") or {}).items():
            if key in LOCKED_SETTINGS:
                continue
            if key in _SETTING_KEYS:
                c.settings.set(key, value)
                changed.append(key)
            elif key in _SECRET_SETTINGS and str(value or "").strip():
                c.settings.set(key, str(value).strip())
                changed.append(key)
        gm = body.get("gmail")
        if isinstance(gm, dict) and (gm.get("address") or gm.get("password")):
            c.gmail.set_credentials(str(gm.get("address") or ""), str(gm.get("password") or ""))
        cal = body.get("calendar_url")
        if isinstance(cal, str) and cal.strip():
            c.calendar.set_url(cal.strip())
        if shell.voice is not None and ("wake_word" in changed or "voice_input_on" in changed):
            if "voice_input_on" in changed:
                shell.voice.set_enabled(bool(c.settings.get("voice_input_on", False)))
            shell.voice.reload_audio_input()
        shell.push({"t": "voice", **shell.voice_state()})
        return {"ok": True, "changed": changed}

    @app.post("/api/settings/remove_connection")
    async def remove_connection(request: Request):
        body = await request.json()
        sid = str(body.get("service") or "")
        entry = CONNECTABLE.get(sid)
        if entry is None:
            return JSONResponse({"error": "unknown"}, status_code=404)
        _label, _store, fields = entry
        for key, _l, _h in fields:
            if c.connections.value(key):
                c.connections.set_value(key, "")
            legacy = key.lower()
            if (c.settings.get(legacy) or "").strip():
                c.settings.set(legacy, "")
        still = [k for k, _l, _h in fields if c.connections.value(k)]
        return {"ok": True, "still_connected": still}

    # ----- agents / memory -----
    @app.post("/api/agents")
    async def add_agent(request: Request):
        body = await request.json()
        name, goal = str(body.get("name") or "").strip(), str(body.get("goal") or "").strip()
        if not name or not goal:
            return JSONResponse({"error": "Give the agent a name and a goal."}, status_code=400)
        c.agents.add(name, goal)
        return {"ok": True}

    @app.post("/api/agents/{name}/run")
    def run_agent(name: str):
        def go() -> None:
            try:
                report = c.agents.run(name, on_progress=lambda line: shell.push(
                    {"t": "status", "text": f"{name}: {line}"}))
                shell._on_scheduled_report(name, report)  # same sentinel rules as the heartbeat
            except Exception:  # noqa: BLE001
                _LOG.exception("agent run failed")

        threading.Thread(target=go, daemon=True, name="helix-agent-run").start()
        return {"ok": True}

    @app.delete("/api/agents/{name}")
    def remove_agent(name: str):
        c.agents.remove(name)
        return {"ok": True}

    @app.post("/api/agents/{name}/rename")
    async def rename_agent(name: str, request: Request):
        body = await request.json()
        out = c.agents.rename(name, str(body.get("name") or "").strip())
        return {"ok": out is not None}

    @app.get("/api/memory")
    def memory(user: str = ""):
        return {"users": c.user_memory.users(), "facts": c.user_memory.facts(user=user or "")}

    @app.post("/api/memory")
    async def memory_add(request: Request):
        body = await request.json()
        c.user_memory.add(str(body.get("text") or ""), user=str(body.get("user") or ""))
        return {"ok": True}

    @app.put("/api/memory")
    async def memory_set(request: Request):
        body = await request.json()
        c.user_memory.set_facts(list(body.get("facts") or []), user=str(body.get("user") or ""))
        return {"ok": True}

    # ----- static: builds + the SPA -----
    app.mount("/builds", StaticFiles(directory=str(c.paths.builds), check_dir=False), name="builds")

    if web_dist is not None and web_dist.is_dir():
        assets = web_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="spa-assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str = ""):
            candidate = (web_dist / full_path) if full_path else (web_dist / "index.html")
            try:
                candidate = candidate.resolve()
                if candidate.is_file() and web_dist.resolve() in candidate.parents:
                    return FileResponse(str(candidate))
            except OSError:
                pass
            return FileResponse(str(web_dist / "index.html"))
    else:
        @app.get("/")
        def no_spa():
            return Response(
                "<h1 style='font-family:sans-serif'>HELIX backend is running.</h1>"
                "<p>The web face isn't built yet — run <code>npm run build</code> in web/ "
                "(or use the Vite dev server).</p>", media_type="text/html")

    return app
