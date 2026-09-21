"""The Deploy lane: gates before anything runs, an audit row before the action, prod's four
conditions, rollback only to a revision that served, and the create plan that runs nothing."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from helix.adapters.console_scripts import ConsoleScripts, NO_CONSOLE, NOT_WINDOWS
from helix.services import deploy as dp
from helix.services.fleet import FleetService
from helix.adapters.memory_state import MemoryFleetState
from helix.domain.fleet import COMPANIES, Cell, Env, Serving, find
from tests.test_fleet_routes import _reader, _Repos


class _Scripts:
    def __init__(self, root="C:/console"):
        self._r = root
        self.ran = []

    def root(self):
        return Path(self._r)

    def deploy_argv(self, app, env, action="be-deploy"):
        return ["powershell", "-File", "dev.ps1", "-Action", action, "-App", app.lower(), "-Env", env], None

    def rollback_argv(self, project, region, service, revision):
        return ["gcloud", "run", "services", "update-traffic", service, "--to-revisions", f"{revision}=100"]

    def stream(self, argv, cwd, on_line, timeout_s=0):
        self.ran.append(argv)
        on_line("Deploying..."); on_line("Service URL: https://x.run.app")
        return 0


def _svc(tmp_path, identity="brian_sullivan@mark1online.com", profile="desktop", served=()):
    pushed = []
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    if served:
        co = COMPANIES[0]
        svc = find("MES", Env.PROD)
        fleet._last[co.id] = ([Cell(service=svc, api=Serving(revision=served[0]), served_revisions=tuple(served))], None)
    scripts = _Scripts()
    d = dp.DeployService(scripts, fleet, audit=dp.Audit(tmp_path / "audit.jsonl"), push=pushed.append,
                         identity=lambda: identity, profile=lambda: profile)
    return d, scripts, pushed


def test_a_dev_deploy_runs_the_wrapped_script_and_audits(tmp_path):
    import time
    d, scripts, pushed = _svc(tmp_path)
    r = d.deploy("mes", "dev")
    assert r["ok"] and r["job"]["argv"][-2:] == ["-Env", "dev"]
    time.sleep(0.3)
    assert scripts.ran and scripts.ran[0][4] == "be-deploy"
    lines = [e for e in pushed if e.get("t") == "deploy"]
    assert any("Service URL" in e["line"] for e in lines)
    assert pushed[-1]["t"] == "deploy_done" and pushed[-1]["ok"] is True
    rows = d.status()["recent"]
    assert rows[0]["kind"] == "deploy" and rows[0]["ok"] is True and rows[0]["by"].startswith("brian")


def test_rule5_refuses_a_missing_service(tmp_path):
    d, scripts, _ = _svc(tmp_path)
    r = d.deploy("echo", "prod", phrase="deploy prod", sure=True)
    assert not r["ok"] and "does not exist" in r["error"] and scripts.ran == []


def test_prod_needs_all_four_conditions(tmp_path):
    d, scripts, _ = _svc(tmp_path, identity="alex@mark1online.com")
    assert "allowlist" in d.deploy("mes", "prod", phrase="deploy prod", sure=True)["error"]
    d, scripts, _ = _svc(tmp_path)
    assert "Type exactly 'deploy prod'" in d.deploy("mes", "prod", phrase="deploy", sure=True)["error"]
    assert "second gate" in d.deploy("mes", "prod", phrase="deploy prod", sure=False)["error"]
    d, scripts, _ = _svc(tmp_path, profile="cloud")
    assert "own PC" in d.deploy("mes", "prod", phrase="deploy prod", sure=True)["error"]
    d, scripts, _ = _svc(tmp_path, identity=None)
    assert "No gcloud account" in d.deploy("mes", "prod", phrase="deploy prod", sure=True)["error"]
    assert scripts.ran == []


def test_prod_deploy_passes_with_the_phrase_and_records_it(tmp_path):
    import time
    d, scripts, _ = _svc(tmp_path)
    r = d.deploy("mes", "prod", phrase="Deploy Prod", sure=True)
    assert r["ok"]
    time.sleep(0.3)
    assert d.status()["recent"][0]["phrase"] == "Deploy Prod"


def test_rollback_only_to_a_revision_that_served(tmp_path):
    import time
    d, scripts, _ = _svc(tmp_path, served=("brms-mes-api-00018-ht7", "brms-mes-api-00017-9hn"))
    t = d.targets("mes", "prod")
    assert t["current"] == "brms-mes-api-00018-ht7" and t["targets"] == ["brms-mes-api-00017-9hn"]
    assert "never served" in d.rollback("mes", "prod", "brms-mes-api-00001-xxx", phrase="rollback prod", sure=True)["error"]
    assert "already serving" in d.rollback("mes", "prod", "brms-mes-api-00018-ht7", phrase="rollback prod", sure=True)["error"]
    r = d.rollback("mes", "prod", "brms-mes-api-00017-9hn", phrase="rollback prod", sure=True)
    assert r["ok"]
    time.sleep(0.3)
    assert scripts.ran[-1][3] == "update-traffic" and "brms-mes-api-00017-9hn=100" in scripts.ran[-1]


def test_one_job_at_a_time(tmp_path):
    d, scripts, _ = _svc(tmp_path)
    d._job = {"kind": "deploy"}
    assert d.deploy("mes", "dev")["error"] == dp.BUSY


def test_create_plan_is_gated_and_runs_nothing(tmp_path):
    d, scripts, _ = _svc(tmp_path)
    assert "already exists" in d.create_plan("mes", "qa", typed="MES", sure=True)["error"]
    assert "Type the app's name" in d.create_plan("echo", "qa", typed="ECH", sure=True)["error"]
    assert "second gate" in d.create_plan("echo", "qa", typed="echo", sure=False)["error"]
    r = d.create_plan("echo", "qa", typed="echo", sure=True)
    assert r["ok"] and r["planned"] and r["service"] == "brms-echo-api-qa" and scripts.ran == []
    p = d.create_plan("echo", "prod", typed="ECHO", sure=True)
    assert p["service"] == "brms-echo-api"
    d2, _, _ = _svc(tmp_path, identity="alex@mark1online.com")
    assert "allowlist" in d2.create_plan("echo", "qa", typed="ECHO", sure=True)["error"]


def test_console_scripts_say_where_dev_ps1_is_missing(tmp_path):
    s = ConsoleScripts(lambda: "", platform="win32")
    assert s.script() == (None, NO_CONSOLE)
    s = ConsoleScripts(lambda: str(tmp_path), platform="win32")
    assert "dev.ps1 was not found" in s.script()[1]
    (tmp_path / "dev.ps1").write_text("# x")
    argv, why = s.deploy_argv("echo", "dev")
    assert why is None and argv[-6:] == ["-Action", "be-deploy", "-App", "echo", "-Env", "dev"]
    assert ConsoleScripts(lambda: str(tmp_path), platform="linux").script() == (None, NOT_WINDOWS)


# ------------------------------------------------------------------------------ CURRENT TASKS

def test_a_deploy_is_a_task_that_can_be_stopped_and_a_rollback_is_not(tmp_path):
    import threading, time
    from helix.services.jobs import Jobs
    jobs = Jobs(tmp_path / "jobs.json")
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    gate = threading.Event()
    killed = []

    class Slow(_Scripts):
        def stream(self, argv, cwd, on_line, timeout_s=0, on_start=None):
            self.ran.append(argv)
            proc = SimpleNamespace(pid=4242, kill=lambda: (killed.append(4242), gate.set()))
            if on_start:
                on_start(proc)
            on_line("Deploying...")
            gate.wait(5)
            return 1 if killed else 0
    scripts = Slow()
    d = dp.DeployService(scripts, fleet, audit=dp.Audit(tmp_path / "a.jsonl"), push=lambda e: None,
                         identity=lambda: "brian_sullivan@mark1online.com", profile=lambda: "desktop", jobs=jobs)
    r = d.deploy("mes", "dev")
    assert r["ok"]
    time.sleep(0.2)
    task = jobs.running("deploy")[0]
    assert task.title == "Ship MES dev (be-deploy)" and task.can_cancel and "old revision keeps serving" in task.cancel_note
    assert task.lines[-1] == "Deploying..."
    assert jobs.cancel(task.id) is None
    for _ in range(50):
        if task.over:
            break
        time.sleep(0.05)
    assert task.state == "cancelled" and killed == [4242]
    assert d.status()["recent"][0]["ok"] is False
    # a rollback: one call, seconds - not cancellable, by design
    co = COMPANIES[0]
    svc = find("MES", Env.DEV)
    fleet._last[co.id] = ([Cell(service=svc, api=Serving(revision="r2"), served_revisions=("r2", "r1"))], None)
    gate.set()
    r = d.rollback("mes", "dev", "r1")
    assert r["ok"]
    time.sleep(0.2)
    rb = [j for j in jobs.list() if j["kind"] == "rollback"][0]
    assert rb["title"] == "Roll back MES dev to r1" and not rb["cancel_note"] and not rb["can_cancel"]


def test_the_tools_report_names_each_fix_in_plain_words(tmp_path, monkeypatch):
    from helix.api import deploy_routes as dr
    monkeypatch.setattr(dr, "_which", lambda name: None)
    monkeypatch.setattr(dr, "gcloud_identity", lambda: None)
    settings = {"console_root": str(tmp_path), "github_token": ""}
    rep = dr.tools_report(SimpleNamespace(settings=SimpleNamespace(get=settings.get)))
    by = {r["key"]: r for r in rep["tools"]}
    assert not rep["ok"] and set(rep["needs"]) == {"gcloud", "firebase", "console_root", "github_token"}
    assert "npm install -g firebase-tools" in by["firebase"]["fix"] and "gcloud auth login" in by["gcloud"]["fix"]
    assert by["firebase_project"]["ok"] and by["firebase_project"]["value"] == COMPANIES[0].gcp_project
    assert "No dev.ps1" in by["console_root"]["fix"]
    (tmp_path / "dev.ps1").write_text("# x")
    monkeypatch.setattr(dr, "_which", lambda name: "C:/tools/" + name)
    monkeypatch.setattr(dr, "_version", lambda argv: "14.2.0")
    monkeypatch.setattr(dr, "gcloud_identity", lambda: "kate@mark1online.com")
    settings["github_token"] = "ghp_x"
    settings["firebase_project"] = "oats-echo"
    rep = dr.tools_report(SimpleNamespace(settings=SimpleNamespace(get=settings.get)))
    by = {r["key"]: r for r in rep["tools"]}
    assert rep["ok"] and by["gcloud"]["value"] == "kate@mark1online.com" and by["firebase"]["value"] == "v14.2.0"
    assert by["firebase_project"]["value"] == "oats-echo" and by["github_token"]["value"] == "set"
