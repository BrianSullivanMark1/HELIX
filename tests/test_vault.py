"""THE VAULT: the adapter spawns gcloud for a token only, sends a value once in a request body and never reads one back; the board
derives expiry pills; create/rotate are audited tasks that name the bounce; delete is behind four
conditions; the routes carry it all without echoing a value."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from helix.adapters import gcp_secret_manager as G
from helix.adapters.gcp_secret_manager import GcpSecrets, Ran
from helix.ports.secrets import StoreError
from helix.services import vault as V
from helix.services.deploy import Audit
from helix.services.jobs import Jobs
from helix.services.vault import VaultService


class _Gcloud:
    """A fake Google: the token spawn plus Secret Manager's REST answers. Keeps values only to
    prove they arrived in a request body (base64, as the API wants) - never in argv."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []      # (method, url, body)
        self.spawns: list[list[str]] = []
        self.secrets = {
            "MES_DB_PASSWORD": {"labels": {"app": "mes", "env": "prod"}, "versions": [
                {"name": "projects/1/secrets/MES_DB_PASSWORD/versions/2", "state": "ENABLED", "createTime": "2026-09-01T00:00:00Z"},
                {"name": "projects/1/secrets/MES_DB_PASSWORD/versions/1", "state": "DISABLED", "createTime": "2026-05-01T00:00:00Z"}]},
            "SLACK_TOKEN": {"labels": {}, "versions": [
                {"name": "projects/1/secrets/SLACK_TOKEN/versions/1", "state": "ENABLED", "createTime": "2026-01-01T00:00:00Z"}]},
        }
        self.fail: tuple[int, str] | None = None
        self.no_token = False

    def run(self, argv, timeout_s):
        self.spawns.append(argv)
        return Ran(1, "", "no credentialed accounts") if self.no_token else Ran(0, "ya29.token\n", "")

    def __call__(self, method, url, token, body, timeout_s):
        self.calls.append((method, url, body))
        assert token == "ya29.token"
        if self.fail:
            return self.fail
        path = url.split("/projects/windy/", 1)[1].split("?")[0]
        import json as _j
        if method == "GET" and path == "secrets":
            return 200, _j.dumps({"secrets": [{"name": f"projects/1/secrets/{n}", "createTime": "2026-01-01T00:00:00Z", "labels": d["labels"]} for n, d in self.secrets.items()]})
        if method == "GET" and path.endswith("/versions"):
            n = path.split("/")[1]
            d = self.secrets.get(n)
            return (404, _j.dumps({"error": {"message": "Secret not found"}})) if d is None else (200, _j.dumps({"versions": d["versions"]}))
        if method == "POST" and path == "secrets":
            n = url.split("secretId=")[1]
            if n in self.secrets:
                return 409, _j.dumps({"error": {"message": "already exists"}})
            self.secrets[n] = {"labels": body.get("labels", {}), "versions": []}
            return 200, _j.dumps({"name": f"projects/1/secrets/{n}"})
        if method == "POST" and path.endswith(":addVersion"):
            n = path.split("/")[1].split(":")[0]
            d = self.secrets[n]
            k = len(d["versions"]) + 1
            d["versions"].insert(0, {"name": f"projects/1/secrets/{n}/versions/{k}", "state": "ENABLED", "createTime": "2026-09-20T00:00:00Z"})
            import base64 as _b
            d["value"] = _b.b64decode(body["payload"]["data"]).decode()
            return 200, _j.dumps(d["versions"][0])
        if method == "POST" and (path.endswith(":enable") or path.endswith(":disable")):
            return 200, _j.dumps({"name": path.split(":")[0], "state": path.split(":")[1].upper() + "D"})
        if method == "DELETE":
            self.secrets.pop(path.split("/")[1])
            return 200, ""
        return 500, "unknown"


def _store(fake=None):
    fake = fake or _Gcloud()
    return GcpSecrets("windy", runner=fake.run, http=fake, gcloud="gcloud")


def _svc(tmp_path, fake, *, who="brian_sullivan@mark1online.com", folders=None, jobs=None, audit=True, now=None):
    return VaultService(_store(fake), policy_path=tmp_path / "vault_policy.json",
                        audit=Audit(tmp_path / "audit.jsonl") if audit else None, identity=lambda: who,
                        jobs=jobs, folders=lambda: folders or {}, clock=lambda: now or datetime(2026, 9, 20, tzinfo=timezone.utc))


# ------------------------------------------------------------------------------ the adapter

def test_values_travel_in_a_request_body_never_in_argv_and_nothing_reads_one_back():
    fake = _Gcloud()
    s = _store(fake)
    s.create("NEW_KEY", "hunter2", {"app": "mes"})
    assert fake.secrets["NEW_KEY"]["value"] == "hunter2" and fake.secrets["NEW_KEY"]["labels"] == {"app": "mes"}
    assert all("hunter2" not in " ".join(a) for a in fake.spawns)          # only the token is ever spawned
    assert [a[1:] for a in fake.spawns] == [["auth", "print-access-token"]]
    assert s.add_version("NEW_KEY", "hunter3") == 2 and fake.secrets["NEW_KEY"]["value"] == "hunter3"
    assert not any(":access" in c[1] for c in fake.calls)      # the read-a-value verb is never spoken
    assert not hasattr(s, "access") and not hasattr(s, "value")
    s.list(); assert len(fake.spawns) == 1                     # the token is cached


def test_list_folds_versions_in_and_sorts_by_name():
    s = _store()
    rows = s.list()
    assert [r.name for r in rows] == ["MES_DB_PASSWORD", "SLACK_TOKEN"]
    mes = rows[0]
    assert mes.labels == {"app": "mes", "env": "prod"} and mes.latest.number == 2 and mes.latest.state == "enabled" and mes.versions_n == 2


def test_google_failures_become_one_sentence_each():
    assert G.classify(0, "timed out") == G.OFFLINE.format(why="timed out")
    assert G.classify(401, "") == G.NOT_SIGNED_IN
    assert G.classify(403, '{"error":{"message":"Permission denied on resource"}}') == G.NO_PERMISSION
    assert G.classify(403, '{"error":{"message":"Secret Manager API has not been used in project windy before or it is disabled."}}', project="windy") == G.API_OFF.format(project="windy")
    assert G.classify(409, "", name="X") == G.EXISTS.format(name="X")
    assert G.classify(404, "", name="X") == G.NOT_FOUND.format(name="X")
    assert G.classify(400, '{"error":{"message":"Invalid label"}}') == "Invalid label"
    assert G.classify(200, "{}") is None
    fake = _Gcloud()
    fake.fail = (403, '{"error":{"message":"Permission denied"}}')
    try:
        _store(fake).list()
        raise AssertionError("should have raised")
    except StoreError as exc:
        assert str(exc) == G.NO_PERMISSION
    fake = _Gcloud()
    fake.no_token = True
    try:
        _store(fake).list()
        raise AssertionError("should have raised")
    except StoreError as exc:
        assert str(exc) == G.NOT_SIGNED_IN
    try:
        _store().create("bad name!", "v", {})
        raise AssertionError("should have raised")
    except StoreError as exc:
        assert str(exc) == G.NAME_RULE


# ------------------------------------------------------------------------------ the board

def test_board_derives_expiry_pills_from_the_policy(tmp_path):
    v = _svc(tmp_path, _Gcloud())
    b = v.board()
    assert b["identity"].startswith("brian") and b["may_delete"] is True and b["problem"] is None
    by = {r["name"]: r for r in b["secrets"]}
    mes, slack = by["MES_DB_PASSWORD"], by["SLACK_TOKEN"]
    assert mes["app"] == "MES" and mes["env"] == "prod" and mes["age_days"] == 19 and mes["due_days"] == 71 and mes["state"] == "fine"
    assert slack["age_days"] == 262 and slack["state"] == "overdue" and slack["explicit"] is False
    v.set_policy("SLACK_TOKEN", None)                      # never expires
    assert {r["name"]: r for r in v.board()["secrets"]}["SLACK_TOKEN"]["state"] == "never"
    v.set_policy("MES_DB_PASSWORD", 30)                    # 19 days old, due in 11: soon
    assert {r["name"]: r for r in v.board()["secrets"]}["MES_DB_PASSWORD"]["state"] == "soon"
    v.set_policy("MES_DB_PASSWORD", 0)                     # back to the default
    assert {r["name"]: r for r in v.board()["secrets"]}["MES_DB_PASSWORD"]["explicit"] is False


def test_board_says_when_gcloud_is_not_signed_in_or_the_store_is_missing(tmp_path):
    fake = _Gcloud()
    fake.no_token = True
    assert _svc(tmp_path, fake).board()["problem"] == G.NOT_SIGNED_IN
    none = VaultService(None, policy_path=tmp_path / "p.json")
    assert none.board()["problem"] == V.NO_STORE and none.create("X", "v")["error"] == V.NO_STORE


def test_who_reads_it_greps_the_linked_folders_by_whole_word(tmp_path):
    proj = tmp_path / "mes"
    (proj / "backend").mkdir(parents=True)
    (proj / "backend" / "deploy.ps1").write_text("gcloud run deploy --set-secrets DB=MES_DB_PASSWORD:latest\n", encoding="utf-8")
    (proj / "notes.md").write_text("MES_DB_PASSWORD_OLD is retired\n", encoding="utf-8")     # not a whole word
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "x.js").write_text("MES_DB_PASSWORD", encoding="utf-8")          # skipped
    v = _svc(tmp_path, _Gcloud(), folders={"MES": str(proj)})
    hits = v.readers("MES_DB_PASSWORD")
    assert hits == [{"app": "MES", "file": "backend/deploy.ps1", "line": 1}]
    assert v.readers("SLACK_TOKEN") == []


# ------------------------------------------------------------------------------ writes

def test_create_and_rotate_are_audited_tasks_that_name_the_bounce(tmp_path):
    fake = _Gcloud()
    jobs = Jobs(tmp_path / "jobs.json")
    v = _svc(tmp_path, fake, jobs=jobs)
    r = v.create("WMS_API_KEY", "abc", app="WMS", env="dev", note="the warehouse api")
    assert r["ok"] and r["version"] == 1 and "container starts" in r["bounce"] and "WMS_API_KEY" in r["bounce"]
    assert fake.secrets["WMS_API_KEY"]["value"] == "abc" and fake.secrets["WMS_API_KEY"]["labels"] == {"app": "wms", "env": "dev"}
    r = v.rotate("WMS_API_KEY", "def")
    assert r["ok"] and r["version"] == 2 and fake.secrets["WMS_API_KEY"]["value"] == "def"
    rows = jobs.list()
    assert [j["title"] for j in rows] == ["Rotate secret WMS_API_KEY", "Create secret WMS_API_KEY"]
    assert all(j["state"] == "done" and j["kind"] == "vault" for j in rows)
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "secret_create" in audit and "secret_rotate" in audit and "abc" not in audit and "def" not in audit
    assert json.dumps(rows).count("abc") == 0 and "def" not in json.dumps(rows)
    assert v.create("WMS_API_KEY", "x")["error"] == G.EXISTS.format(name="WMS_API_KEY")
    assert v.rotate("WMS_API_KEY", "")["error"] == V.EMPTY_VALUE


def test_delete_is_behind_four_conditions_and_a_second_yes_when_something_reads_it(tmp_path):
    proj = tmp_path / "mes"
    proj.mkdir()
    (proj / "deploy.ps1").write_text("MES_DB_PASSWORD\n", encoding="utf-8")
    fake = _Gcloud()
    jobs = Jobs(tmp_path / "jobs.json")
    # 1 no identity
    assert _svc(tmp_path, fake, who=None).delete("MES_DB_PASSWORD", typed="MES_DB_PASSWORD", sure=True, anyway=True)["error"] == V.NO_IDENTITY
    # 2 not on the list
    r = _svc(tmp_path, fake, who="alex@mark1online.com").delete("MES_DB_PASSWORD", typed="MES_DB_PASSWORD", sure=True, anyway=True)
    assert "alex@mark1online.com may not delete" in r["error"]
    v = _svc(tmp_path, fake, folders={"MES": str(proj)}, jobs=jobs)
    # 3 the name typed back
    assert v.delete("MES_DB_PASSWORD", typed="MES_DB_PASSWRD", sure=True, anyway=True)["error"] == V.BAD_TYPED
    # 4 are you sure
    assert v.delete("MES_DB_PASSWORD", typed="MES_DB_PASSWORD", sure=False, anyway=True)["error"] == V.NOT_SURE
    # 5 something reads it: refused with the readers, until 'anyway'
    r = v.delete("MES_DB_PASSWORD", typed="MES_DB_PASSWORD", sure=True, anyway=False)
    assert r["error"].startswith("1 place still reads MES_DB_PASSWORD") and r["readers"][0]["file"] == "deploy.ps1"
    assert "MES_DB_PASSWORD" in fake.secrets and not any(c[0] == "DELETE" for c in fake.calls)
    r = v.delete("MES_DB_PASSWORD", typed="MES_DB_PASSWORD", sure=True, anyway=True)
    assert r["ok"] and "MES_DB_PASSWORD" not in fake.secrets
    audit = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    opened = [a for a in audit if a.get("action") == "secret_delete"]
    assert opened and opened[0]["readers"] == 1 and opened[0]["anyway"] is True and opened[0]["who"].startswith("brian")
    assert jobs.list()[0]["title"] == "Delete secret MES_DB_PASSWORD" and jobs.list()[0]["state"] == "done"
    # a secret nothing reads goes on the first yes
    assert v.delete("SLACK_TOKEN", typed="SLACK_TOKEN", sure=True, anyway=False)["ok"]


# ------------------------------------------------------------------------------ the routes

def test_routes_board_one_create_rotate_state_policy_delete(tmp_path):
    from fastapi import FastAPI
    from helix.api import vault_routes
    from tests.test_fleet_routes import _call
    fake = _Gcloud()
    v = _svc(tmp_path, fake)
    app = FastAPI()
    vault_routes.mount_vault(app, SimpleNamespace(vault=v))
    st, doc = _call(app, "GET", "/api/secrets")
    assert st == 200 and len(doc["secrets"]) == 2 and doc["may_delete"] is True
    st, doc = _call(app, "GET", "/api/secrets/MES_DB_PASSWORD")
    assert st == 200 and doc["versions"][0]["number"] == 2 and doc["readers"] == []
    st, doc = _call(app, "POST", "/api/secrets", {"name": "NEW_ONE", "value": "s3cret", "app": "MRP", "env": "qa"})
    assert st == 200 and doc["ok"] and "s3cret" not in json.dumps(doc)
    st, doc = _call(app, "POST", "/api/secrets/NEW_ONE/rotate", {"value": "s3cret2"})
    assert st == 200 and doc["version"] == 2 and "s3cret2" not in json.dumps(doc)
    st, doc = _call(app, "POST", "/api/secrets/NEW_ONE/versions/1/disable")
    assert st == 200 and doc["enabled"] is False
    assert _call(app, "POST", "/api/secrets/NEW_ONE/versions/1/destroy")[0] == 400
    st, doc = _call(app, "PUT", "/api/secrets/NEW_ONE/policy", {"days": 30})
    assert st == 200 and doc["days"] == 30
    st, doc = _call(app, "POST", "/api/secrets/NEW_ONE/delete", {"typed": "NEW", "sure": True})
    assert st == 400 and doc["error"] == V.BAD_TYPED
    st, doc = _call(app, "POST", "/api/secrets/NEW_ONE/delete", {"typed": "NEW_ONE", "sure": True})
    assert st == 200 and doc["ok"]
    st, doc = _call(FastAPI(), "GET", "/api/secrets")
    down = FastAPI()
    vault_routes.mount_vault(down, SimpleNamespace())
    assert _call(down, "GET", "/api/secrets")[0] == 503
