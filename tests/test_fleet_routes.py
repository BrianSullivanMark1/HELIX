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
    assert paths == {"/api/fleet", "/api/fleet/refresh", "/api/fleet/history", "/api/fleet/link", "/api/fleet/git"}  # link edits a setting, never the fleet
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


# ------------------------------------------------------------------------------ project links

class _Settings:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


def _linked_app(token=True):
    from types import SimpleNamespace
    from helix.domain.project_links import links_from, SETTING
    settings = _Settings()
    repos = _Repos()
    if not token:
        repos.available = lambda: (False, "No GitHub token is connected.")
    svc = FleetService(_reader(), repos, MemoryFleetState(), links=lambda: links_from(settings.get(SETTING)))
    app = FastAPI()
    fleet_routes.mount_fleet(app, SimpleNamespace(fleet=svc, runtime_profile=HelixProfile.DESKTOP, settings=settings))
    return app, settings


def test_every_card_carries_its_link_and_no_token_means_unlinked():
    app, _ = _linked_app(token=False)
    _, doc = _call(app, "GET", "/api/fleet")
    mes = doc["companies"][0]["apps"][0]
    assert mes["link"]["repo"] == "BrendanSullivanMark1/BRMS_MES_WEB_VERSION"
    assert mes["link"]["linked"] is False and "token" in mes["link"]["why"]
    assert mes["link"]["custom"] is False and mes["link"]["folder"] is None


def test_saving_a_link_changes_what_the_board_reads_and_survives_in_settings():
    app, settings = _linked_app()
    st, doc = _call(app, "PUT", "/api/fleet/link", {"app": "wms", "repo": "https://github.com/Alex-Mark1/WMS_V2.git", "branch": "v3", "folder": "C:\\code\\wms"})
    assert st == 200
    wms = doc["companies"][0]["apps"][1]
    assert wms["link"] == {"repo": "Alex-Mark1/WMS_V2", "branch": "v3", "folder": "C:\\code\\wms", "custom": True, "linked": True, "why": None}
    assert all(e["repo"] == "Alex-Mark1/WMS_V2" and e["branch"] == "v3" for e in wms["envs"])
    assert settings.d["project_links"] == {"WMS": {"repo": "Alex-Mark1/WMS_V2", "branch": "v3", "folder": "C:\\code\\wms"}}
    # the read that follows asks GitHub for the linked repo, and the card is linked
    _, doc = _call(app, "POST", "/api/fleet/refresh", {"app": "WMS"})
    wms = doc["companies"][0]["apps"][1]
    assert wms["link"]["linked"] is True and all(e["repo_ok"] is True for e in wms["envs"] if e["exists"])
    # clearing every field returns the app to the table
    st, doc = _call(app, "PUT", "/api/fleet/link", {"app": "WMS"})
    assert st == 200 and doc["companies"][0]["apps"][1]["link"]["repo"] == "Alex-Mark1/WMS_V1"
    assert settings.d["project_links"] == {}


def test_a_bad_repo_or_app_is_refused_in_one_sentence():
    app, _ = _linked_app()
    st, doc = _call(app, "PUT", "/api/fleet/link", {"app": "MES", "repo": "not a repo"})
    assert st == 400 and "owner/name" in doc["error"]
    st, doc = _call(app, "PUT", "/api/fleet/link", {"app": "SAP", "repo": "a/b"})
    assert st == 404


def test_a_repo_that_does_not_answer_makes_the_card_unlinked_with_the_reason():
    app, _ = _linked_app()
    from types import SimpleNamespace
    bad = _Repos()
    bad.read_head = lambda repo, branch: RepoRead(repo=repo, branch=branch, ok=False, problem="The repo Alex-Mark1/WMS_V1 was not found, or the token cannot see it.")
    svc = FleetService(_reader(), bad, MemoryFleetState())
    app = FastAPI()
    fleet_routes.mount_fleet(app, SimpleNamespace(fleet=svc, runtime_profile=HelixProfile.DESKTOP, settings=_Settings()))
    _, doc = _call(app, "POST", "/api/fleet/refresh", {"app": "MES"})
    mes = doc["companies"][0]["apps"][0]
    assert mes["link"]["linked"] is False and "not found" in mes["link"]["why"]


# ------------------------------------------------------------------------------ the git graph

class _GraphRepos(_Repos):
    def branches(self, repo, *, timeout_s=20.0):
        return [{"name": "main", "sha": "c3"}, {"name": "v3", "sha": "b2"}], None

    def commits(self, repo, ref, *, limit=40, timeout_s=20.0):
        if ref == "main":
            return [{"sha": "c3", "parents": ["c2"], "subject": "three", "author": "b", "at": "2026-09-19T03:00:00Z"},
                    {"sha": "c2", "parents": ["c1"], "subject": "two", "author": "b", "at": "2026-09-19T02:00:00Z"},
                    {"sha": "c1", "parents": [], "subject": "one", "author": "b", "at": "2026-09-19T01:00:00Z"}], None
        return [{"sha": "b2", "parents": ["c2"], "subject": "branch", "author": "k", "at": "2026-09-19T02:30:00Z"},
                {"sha": "c2", "parents": ["c1"], "subject": "two", "author": "b", "at": "2026-09-19T02:00:00Z"}], None


def test_the_git_graph_merges_branch_tips_and_sorts_by_time():
    from types import SimpleNamespace
    svc = FleetService(_reader(), _GraphRepos(), MemoryFleetState())
    app = FastAPI()
    fleet_routes.mount_fleet(app, SimpleNamespace(fleet=svc, runtime_profile=HelixProfile.DESKTOP))
    st, doc = _call(app, "GET", "/api/fleet/git?app_name=mes")
    assert st == 200 and doc["repo"] == "BrendanSullivanMark1/BRMS_MES_WEB_VERSION" and doc["branch"] == "main"
    assert [c["sha"] for c in doc["commits"]] == ["c3", "b2", "c2", "c1"]
    assert [b["name"] for b in doc["branches"]] == ["main", "v3"]
    st, doc = _call(app, "GET", "/api/fleet/git?app_name=nope")
    assert st == 404


def test_a_failed_cloud_read_carries_what_gcloud_said():
    from types import SimpleNamespace
    from helix.ports.fleet import CellRead

    class _Bad:
        def available(self):
            return True, None

        def read_cell(self, service, *, timeout_s=30.0):
            return CellRead(service=service, ok=False, problem="gcloud answered, but not in a shape HELIX understands.",
                            detail="ERROR: (gcloud.run.services.describe) You do not currently have an active account selected.")

        def read_all(self, services, *, timeout_s=90.0):
            return [self.read_cell(s) for s in services]

    svc = FleetService(_Bad(), _Repos(), MemoryFleetState())
    app = FastAPI()
    fleet_routes.mount_fleet(app, SimpleNamespace(fleet=svc, runtime_profile=HelixProfile.DESKTOP))
    _, doc = _call(app, "POST", "/api/fleet/refresh", {"app": "MES"})
    dev = doc["companies"][0]["apps"][0]["envs"][0]
    assert "not in a shape" in dev["note"] and "active account" in dev["detail"]
