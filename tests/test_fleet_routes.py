"""The fleet routes at the ASGI layer: the board's shape, refresh, history, and the 503 when the
service did not compose. Plain ASGI on purpose (see test_api_lifecycle.py for why no TestClient).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
from datetime import datetime, timezone

from fastapi import FastAPI

from helix.adapters import gcloud_fleet as g
from helix.adapters.memory_state import MemoryFleetState
from helix.api import fleet_routes
from helix.domain import fleet
from helix.domain.fleet import Env, OATS_OVERNIGHT
from helix.domain.runtime_profile import HelixProfile
from helix.ports.fleet import CompareRead, FleetEvent, RepoRead
from helix.services.fleet import FleetService

_FIX = pathlib.Path(__file__).parent / "fixtures" / "mes_dev"


# ------------------------------------------------------------------------------ an ASGI driver

def _call(app, method: str, path: str, body: dict | None = None):
    """One request through the ASGI stack -> (status, parsed json)."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"host", b"127.0.0.1:8737")]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    if "?" in path:
        path, qs = path.split("?", 1)
    else:
        qs = ""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
        "method": method, "path": path, "raw_path": path.encode(), "root_path": "",
        "query_string": qs.encode(), "headers": headers,
        "client": ("127.0.0.1", 40000), "server": ("127.0.0.1", 8737),
    }
    status: list[int] = []
    chunks: list[bytes] = []
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            await asyncio.sleep(3600)
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status.append(msg["status"])
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    asyncio.run(app(scope, receive, send))
    text = b"".join(chunks).decode() or "null"
    return status[0], json.loads(text)


# ------------------------------------------------------------------------------ a real-ish fleet

class _Repos:
    """A GitHub that knows MES HEAD is one commit past what is serving."""

    def available(self):
        return True, None

    def read_head(self, repo, branch):
        return RepoRead(repo=repo, branch=branch, ok=True, commit="9ab12cd", full_sha="9ab12cd" * 5,
                        subject="fix", committed_at=datetime(2026, 9, 16, tzinfo=timezone.utc))

    def compare(self, repo, serving, head):
        return CompareRead(repo=repo, serving=serving, head=head, ok=True, status="behind",
                           behind_by=1, ahead_by=0)


def _reader(health_body: str | None = None):
    svc_json = (_FIX / "service.json").read_text(encoding="utf-8")
    rev_json = (_FIX / "revisions.json").read_text(encoding="utf-8")
    body = health_body if health_body is not None else (_FIX / "health.json").read_text(encoding="utf-8")

    def replay(argv, timeout):
        if "--version" in argv:
            return g.Ran(0, "Google Cloud SDK 500.0.0", "")
        name = argv[argv.index("describe") + 1] if "describe" in argv else argv[argv.index("--service") + 1]
        if name != "brms-mes-api-dev":
            return g.Ran(1, "", f"ERROR: (gcloud.run.services.describe) Cannot find service [{name}]: NOT_FOUND")
        if "describe" in argv:
            return g.Ran(0, svc_json, "")
        return g.Ran(0, rev_json, "")

    return g.GcloudFleet("p", "r", runner=replay, http_get=lambda u, t: (200, body), workers=2)


def _app(with_fleet=True, profile=HelixProfile.DESKTOP):
    from types import SimpleNamespace
    app = FastAPI()
    if with_fleet:
        svc = FleetService(_reader(), _Repos(), MemoryFleetState())
        container = SimpleNamespace(fleet=svc, runtime_profile=profile)
    else:
        container = SimpleNamespace()
    fleet_routes.mount_fleet(app, container)
    return app, container


# ------------------------------------------------------------------------------------- tests

def test_the_board_has_its_shape_before_anything_was_read():
    app, _ = _app()
    status, doc = _call(app, "GET", "/api/fleet")
    assert status == 200
    assert doc["profile"] == "desktop"
    assert [r["what"] for r in doc["readiness"]] == ["cloud", "repos"]
    co = doc["companies"][0]
    assert co["id"] == "oats-overnight" and co["label"] == "Oats Overnight"
    assert co["checked_at"] is None
    assert [a["app"] for a in co["apps"]] == ["MES", "WMS", "MRP", "ECHO"]
    mes = co["apps"][0]
    assert [e["env"] for e in mes["envs"]] == ["dev", "qa", "prod"]
    assert all(e["health"] == "unknown" and e["drift"] == "unknown" for e in mes["envs"])
    echo = co["apps"][3]
    assert echo["envs"][1]["exists"] is False and echo["envs"][1]["health"] == "absent"


def test_refresh_reads_and_the_board_then_carries_what_was_seen():
    app, _ = _app()
    status, doc = _call(app, "POST", "/api/fleet/refresh", {"app": "MES"})
    assert status == 200
    co = doc["companies"][0]
    assert co["checked_at"] is not None
    dev = co["apps"][0]["envs"][0]
    assert dev["key"] == "oats-overnight/MES/dev"
    assert dev["api"]["revision"] == "brms-mes-api-dev-00325-xtq"
    assert dev["api"]["commit"] == "3dc6631" and dev["api"]["dirty"] is False
    assert dev["api"]["db"] == "BRMS_database_dev"
    assert dev["api"]["flags"]["appCheckRequired"] is False
    assert dev["drift"] == "behind" and dev["behind_by"] == 1 and dev["repo_commit"] == "9ab12cd"
    assert dev["needs_attention"] is False
    # site half is not read yet: no reading, so the cell's health is the API half's
    assert dev["site"] is None and dev["health"] == "ok"
    # WMS was not part of this refresh - still unknown, not invented
    wms = co["apps"][1]["envs"][0]
    assert wms["health"] == "unknown" and wms["checked_at"] is None


def test_refresh_of_the_whole_company_marks_absent_cells_as_absent():
    app, _ = _app()
    status, doc = _call(app, "POST", "/api/fleet/refresh", {})
    assert status == 200
    co = doc["companies"][0]
    wms_dev = co["apps"][1]["envs"][0]
    # The API half is ABSENT (Cloud Run said so) and there is no hosting reading, so the cell is
    # ABSENT, with the note saying the table and Cloud Run disagree.
    assert wms_dev["api"]["health"] == "absent"
    assert wms_dev["health"] == "absent"
    assert wms_dev["note"] == g.NOT_FOUND_NOTE
    echo_qa = co["apps"][3]["envs"][1]
    assert echo_qa["health"] == "absent" and echo_qa["note"] is None


def test_refresh_refuses_an_unknown_app_or_company():
    app, _ = _app()
    assert _call(app, "POST", "/api/fleet/refresh", {"app": "NOPE"})[0] == 404
    assert _call(app, "POST", "/api/fleet/refresh", {"company": "acme"})[0] == 404


def test_every_route_says_the_fleet_is_down_when_it_did_not_compose():
    app, _ = _app(with_fleet=False)
    for method, path in (("GET", "/api/fleet"), ("POST", "/api/fleet/refresh"),
                         ("GET", "/api/fleet/history")):
        status, doc = _call(app, method, path, {} if method == "POST" else None)
        assert status == 503 and doc["error"] == fleet_routes.FLEET_DOWN


def test_history_is_the_ledger_newest_first():
    app, c = _app()
    st = c.fleet._state
    st.record(FleetEvent(id="1", at=datetime(2026, 9, 1, tzinfo=timezone.utc), company="oats-overnight",
                         app="MES", env="dev", action="deploy", by="brian", ok=True, commit="aaa"))
    st.record(FleetEvent(id="2", at=datetime(2026, 9, 2, tzinfo=timezone.utc), company="oats-overnight",
                         app="WMS", env="prod", action="rollback", by="brendan", ok=True))
    status, doc = _call(app, "GET", "/api/fleet/history?company=oats-overnight")
    assert status == 200
    assert [e["id"] for e in doc["events"]] == ["2", "1"]
    status, doc = _call(app, "GET", "/api/fleet/history?app=mes")
    assert [e["id"] for e in doc["events"]] == ["1"]


def test_the_routes_are_reads_only():
    """Phase 1 pin: no route on this surface may carry a mutating name."""
    app, _ = _app()
    paths = {r.path for r in app.routes if r.path.startswith("/api/fleet")}
    assert paths == {"/api/fleet", "/api/fleet/refresh", "/api/fleet/history"}
    for verb in ("deploy", "rollback", "delete", "traffic", "secret", "create"):
        assert not any(verb in p for p in paths)


def test_cloud_profile_composes_with_the_not_built_reader_and_says_so():
    from types import SimpleNamespace
    from helix.adapters.rest_fleet import RestFleet, NOT_BUILT
    app = FastAPI()
    svc = FleetService(RestFleet(), _Repos(), MemoryFleetState())
    fleet_routes.mount_fleet(app, SimpleNamespace(fleet=svc, runtime_profile=HelixProfile.CLOUD))
    status, doc = _call(app, "GET", "/api/fleet")
    assert doc["profile"] == "cloud"
    assert doc["readiness"][0] == {"what": "cloud", "ok": False, "why": NOT_BUILT}
    status, doc = _call(app, "POST", "/api/fleet/refresh", {"app": "MES"})
    assert status == 200
    dev = doc["companies"][0]["apps"][0]["envs"][0]
    assert dev["health"] == "unknown" and dev["note"] == NOT_BUILT
