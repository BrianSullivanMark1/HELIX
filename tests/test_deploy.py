"""The Deploy lane: gates before anything runs, an audit row before the action, prod's four
conditions, rollback only to a revision that served, and the create plan that runs nothing."""
from __future__ import annotations

import json
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

    def deploy_argv(self, app, env, action="be-deploy", *, typed="", create=False):
        argv = ["powershell", "-File", "dev.ps1", "-Action", action, "-App", app.lower(), "-Env", env]
        return argv + (["-Typed", typed] if typed else []) + (["-Create"] if create else []), None

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


# ------------------------------------------------------------------------------ 2026-09-20: the run

class _Talks(_Scripts):
    """A scripts double whose dev.ps1 says given lines and returns a given code, per action."""

    def __init__(self, says):
        super().__init__()
        self._says = says

    def stream(self, argv, cwd, on_line, timeout_s=0, on_start=None):
        self.ran.append(argv)
        lines, rc = self._says[argv[argv.index("-Action") + 1]]
        for ln in lines:
            on_line(ln)
        return rc


def _svc_with(tmp_path, scripts, identity="brian_sullivan@mark1online.com", links=None):
    pushed = []
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState(), links=(lambda: links or {}))
    d = dp.DeployService(scripts, fleet, audit=dp.Audit(tmp_path / "audit.jsonl"), push=pushed.append,
                         identity=lambda: identity, profile=lambda: "desktop")
    return d, pushed


def _wait(d):
    import time
    for _ in range(100):
        if d.status()["running"] is False:
            return
        time.sleep(0.02)


def test_the_scripts_words_beat_its_exit_code():
    assert dp.judge(0, ["Deploying...", "Backend deployed OK."]) == (True, "")
    assert dp.judge(0, ["*** PRODUCTION ***", "Aborted."]) == (False, "Aborted.")            # the old dev.ps1: exit 0, nothing shipped
    assert dp.judge(0, ["Backend deploy FAILED (see above).", "[dev.ps1] RESULT: FAILED"])[0] is False
    assert dp.judge(0, ["Deploy hiccup - retry 2 of 5", "Frontend deploy FAILED.", "[dev.ps1] RESULT: OK"]) == (True, "")
    assert dp.judge(1, ["[dev.ps1] RESULT: OK"])[0] is False                                  # the code still counts
    assert dp.judge(0, ["[helix] FAILED is only a word when HELIX says it"]) == (True, "")
    assert dp.judge(3, []) == (False, "exit 3")


def test_a_deploy_the_script_refused_is_not_reported_green(tmp_path):
    scripts = _Talks({"be-deploy": (["Type PROD to continue", "Aborted."], 0)})
    d, pushed = _svc_with(tmp_path, scripts)
    assert d.deploy("mes", "dev")["ok"]
    _wait(d)
    assert pushed[-1]["t"] == "deploy_done" and pushed[-1]["ok"] is False
    assert d.status()["recent"][0]["ok"] is False
    assert any("exited 0 but said: Aborted." in e.get("line", "") for e in pushed)


def test_production_carries_dev_ps1s_typed_confirm_and_only_after_the_gate(tmp_path):
    scripts = _Talks({"be-deploy": (["Backend deployed OK.", "[dev.ps1] RESULT: OK"], 0)})
    d, pushed = _svc_with(tmp_path, scripts)
    assert not d.deploy("mes", "prod", phrase="deploy", sure=True)["ok"] and scripts.ran == []
    assert d.deploy("mes", "prod", phrase="deploy prod", sure=True)["ok"]
    _wait(d)
    assert scripts.ran[0][-2:] == ["-Typed", "PROD"] and pushed[-1]["ok"] is True
    assert d.deploy("mes", "qa")["ok"]
    _wait(d)
    assert "-Typed" not in scripts.ran[1]


def test_ship_both_runs_the_server_then_the_site_and_stops_at_the_first_failure(tmp_path):
    scripts = _Talks({"be-deploy": (["[dev.ps1] RESULT: OK"], 0), "fe-deploy": (["[dev.ps1] RESULT: OK"], 0)})
    d, pushed = _svc_with(tmp_path, scripts)
    assert d.deploy("mrp", "dev", action="both")["ok"]
    _wait(d)
    assert [a[a.index("-Action") + 1] for a in scripts.ran] == ["be-deploy", "fe-deploy"] and pushed[-1]["ok"] is True
    scripts = _Talks({"be-deploy": (["Backend deploy FAILED (see above).", "[dev.ps1] RESULT: FAILED"], 1), "fe-deploy": ([], 0)})
    d, pushed = _svc_with(tmp_path, scripts)
    assert d.deploy("mrp", "dev", action="both")["ok"]
    _wait(d)
    assert len(scripts.ran) == 1 and pushed[-1]["ok"] is False
    assert "not something HELIX ships" in d.deploy("mrp", "dev", action="rev-clean")["error"]


def test_create_run_is_gated_runs_both_steps_and_hands_back_the_console_steps(tmp_path):
    ok = {"be-deploy": (["Creating Cloud Run service 'brms-echo-api-qa' - first deploy (-Create).", "[dev.ps1] RESULT: OK"], 0),
          "fe-deploy": (["[dev.ps1] RESULT: OK"], 0)}
    scripts = _Talks(ok)
    d, pushed = _svc_with(tmp_path, scripts)
    assert "Type the app's name" in d.create_run("echo", "qa", typed="", sure=True)["error"]
    assert "second gate" in d.create_run("echo", "qa", typed="ECHO", sure=False)["error"]
    assert "already exists" in d.create_run("mes", "qa", typed="MES", sure=True)["error"]
    d2, _ = _svc_with(tmp_path, scripts, identity="alex@mark1online.com")
    assert "allowlist" in d2.create_run("echo", "qa", typed="ECHO", sure=True)["error"]
    assert "Type exactly 'create prod'" in d.create_run("echo", "prod", typed="ECHO", sure=True, phrase="")["error"]
    assert scripts.ran == []
    r = d.create_run("echo", "qa", typed="echo", sure=True)
    assert r["ok"] and r["job"]["service"] == "brms-echo-api-qa" and r["job"]["site"] == "oo-echo-qa"
    _wait(d)
    be, fe = scripts.ran
    assert be[-1] == "-Create" and "be-deploy" in be and "-Typed" not in be
    assert "fe-deploy" in fe and "-Create" not in fe
    done = pushed[-1]
    assert done["t"] == "deploy_done" and done["ok"] is True and done["job"]["kind"] == "create"
    steps = done["followups"]["steps"]
    assert [s["key"] for s in steps] == ["auth-domain", "recaptcha-domain"] and all(s["copy"] == "oo-echo-qa.web.app" for s in steps)
    assert steps[0]["link"].startswith("https://console.firebase.google.com/project/windy-celerity-392822/authentication")
    row = d.status()["recent"][0]
    assert row["kind"] == "create" and row["ok"] is True and row["site"] == "oo-echo-qa"
    # prod: the phrase, and dev.ps1's typed confirm on both steps
    scripts2 = _Talks(ok)
    d3, pushed3 = _svc_with(tmp_path, scripts2)
    assert d3.create_run("echo", "prod", typed="ECHO", sure=True, phrase="create prod")["ok"]
    _wait(d3)
    assert scripts2.ran[0][-3:] == ["-Typed", "PROD", "-Create"] and scripts2.ran[1][-2:] == ["-Typed", "PROD"]
    assert pushed3[-1]["followups"]["url"] == "https://oo-echo.web.app"


def test_create_run_stops_when_the_server_fails_and_never_ships_the_site(tmp_path):
    scripts = _Talks({"be-deploy": (["Backend deploy FAILED (see above).", "[dev.ps1] RESULT: FAILED"], 1), "fe-deploy": ([], 0)})
    d, pushed = _svc_with(tmp_path, scripts)
    assert d.create_run("echo", "qa", typed="ECHO", sure=True)["ok"]
    _wait(d)
    assert len(scripts.ran) == 1 and pushed[-1]["ok"] is False and pushed[-1]["followups"] is None


def test_create_run_refuses_a_checkout_without_the_hosting_target(tmp_path):
    from helix.domain.project_links import Link
    repo = tmp_path / "echo"
    (repo / "frontend").mkdir(parents=True)
    (repo / "frontend" / ".firebaserc").write_text('{"projects": {"default": "p"}}')
    scripts = _Talks({})
    links = {"ECHO": Link(repo="BrendanSullivanMark1/MES_OATS_DASHBOARD", branch="main", folder=str(repo))}
    d, _ = _svc_with(tmp_path, scripts, links=links)
    r = d.create_run("echo", "qa", typed="ECHO", sure=True)
    assert not r["ok"] and "no Hosting target 'qa'" in r["error"] and scripts.ran == []
    (repo / "frontend" / ".firebaserc").write_text('{"targets": {"p": {"hosting": {"qa": ["oo-echo-qa"]}}}}')
    assert dp._has_target(str(repo), "qa") and not dp._has_target(str(repo), "prod")


def test_the_plan_uses_the_decided_names_and_names_the_two_console_steps(tmp_path):
    d, scripts, _ = _svc(tmp_path)
    p = d.create_plan("echo", "prod", typed="ECHO", sure=True)
    assert (p["service"], p["site"], p["prod"]) == ("brms-echo-api", "oo-echo", True)
    assert "-Create -Typed PROD" in p["steps"][0]["cmd"]
    assert [st["who"] for st in p["steps"]] == ["HELIX", "HELIX", "HELIX", "you", "a one-line code change"]
    assert "reCAPTCHA" in p["steps"][3]["what"] and all(len(st["what"]) < 60 for st in p["steps"])
    f = d.followups("echo", "qa")
    assert f["site"] == "oo-echo-qa" and len(f["steps"]) == 2
    assert d.followups("mes", "prod")["site"] == "oo-mes"
    assert scripts.ran == []


class _Auth:
    """A Firebase Authentication double: the list, and what was written."""

    def __init__(self, have=("localhost", "windy-celerity-392822.firebaseapp.com"), fail=None):
        self.have = list(have)
        self.fail = fail
        self.written = []

    def add(self, domain):
        if self.fail:
            raise RuntimeError(self.fail)
        if domain in self.have:
            return False, f"{domain} is already on Firebase's authorized domains."
        self.have.append(domain); self.written.append(domain)
        return True, f"{domain} added to Firebase's authorized domains ({len(self.have)} on the list)."


def test_create_run_lets_helix_allow_sign_in_and_the_card_shows_only_what_is_left(tmp_path):
    ok = {"be-deploy": (["[dev.ps1] RESULT: OK"], 0), "fe-deploy": (["[dev.ps1] RESULT: OK"], 0)}
    scripts = _Talks(ok)
    auth = _Auth()
    d, pushed = _svc_with(tmp_path, scripts)
    d._auth_domains = auth
    assert d.create_run("echo", "qa", typed="ECHO", sure=True)["ok"]
    _wait(d)
    assert auth.written == ["oo-echo-qa.web.app"]
    steps = {s["key"]: s for s in pushed[-1]["followups"]["steps"]}
    assert steps["auth-domain"]["done_by_helix"].startswith("oo-echo-qa.web.app added") and steps["auth-domain"]["by_helix"]
    assert "done_by_helix" not in steps["recaptcha-domain"] and not steps["recaptcha-domain"]["by_helix"]
    assert any("step 3 of 3" in e.get("line", "") for e in pushed)
    # Firebase refuses (no role): the run is still a success, the card says do it by hand and why
    scripts2 = _Talks(ok)
    d2, pushed2 = _svc_with(tmp_path, scripts2)
    d2._auth_domains = _Auth(fail="brian may not change Firebase Authentication settings (needs the Firebase Admin role).")
    assert d2.create_run("echo", "qa", typed="ECHO", sure=True)["ok"]
    _wait(d2)
    assert pushed2[-1]["ok"] is True
    steps = {s["key"]: s for s in pushed2[-1]["followups"]["steps"]}
    assert "done_by_helix" not in steps["auth-domain"] and "by hand" in steps["auth-domain"]["by_helix_failed"]
    assert any("Firebase Admin role" in e.get("line", "") for e in pushed2)


def test_firebase_auth_domains_adds_never_removes():
    from helix.adapters import firebase_auth as fa
    calls = []
    state = {"authorizedDomains": ["localhost", "x.firebaseapp.com"]}

    def http(method, url, token, body, timeout):
        calls.append((method, url, body))
        assert token == "tok" and "/admin/v2/projects/windy-celerity-392822/config" in url
        if method == "PATCH":
            assert url.endswith("?updateMask=authorizedDomains") and body["authorizedDomains"][:2] == state["authorizedDomains"]
            state["authorizedDomains"] = body["authorizedDomains"]
        return 200, json.dumps(state)
    runner = lambda argv, t: fa.Ran(0, "tok\n", "")
    a = fa.FirebaseAuthDomains("windy-celerity-392822", runner=runner, http=http)
    assert a.add("https://oo-echo-qa.web.app/") == (True, "oo-echo-qa.web.app added to Firebase's authorized domains (3 on the list).")
    assert a.add("oo-echo-qa.web.app")[0] is False and [c[0] for c in calls] == ["GET", "PATCH", "GET"]
    assert state["authorizedDomains"] == ["localhost", "x.firebaseapp.com", "oo-echo-qa.web.app"]
    # no rights -> a sentence naming the role; not signed in -> the signed-in sentence
    b = fa.FirebaseAuthDomains("p", runner=runner, http=lambda *a: (403, '{"error":{"message":"PERMISSION_DENIED"}}'), identity=lambda: "kate@mark1online.com")
    try:
        b.add("a.web.app"); assert False
    except fa.AuthError as e:
        assert "kate@mark1online.com may not" in str(e) and "Firebase Admin" in str(e)
    c = fa.FirebaseAuthDomains("p", runner=lambda argv, t: fa.Ran(1, "", "no creds"), http=http)
    try:
        c.add("a.web.app"); assert False
    except fa.AuthError as e:
        assert "signed in" in str(e)


def test_helix_finds_the_console_checkout_itself(tmp_path):
    from helix.adapters.console_scripts import find_console
    (tmp_path / "OneDrive/Desktop/BRMS_MES_WEB_APP/BRMS_MES_WEB_VERSION").mkdir(parents=True)
    (tmp_path / "OneDrive/Desktop/BRMS_MES_WEB_APP/BRMS_MES_WEB_VERSION/dev.ps1").write_text("<#\n  BRMS MES - dev & deploy helper.\n#>\n")
    (tmp_path / "OneDrive/Desktop/other").mkdir()
    (tmp_path / "OneDrive/Desktop/other/dev.ps1").write_text("# a different dev.ps1")
    assert find_console([tmp_path / "OneDrive/Desktop"]) == str((tmp_path / "OneDrive/Desktop/BRMS_MES_WEB_APP/BRMS_MES_WEB_VERSION").resolve())
    (tmp_path / "OneDrive/Desktop/copy").mkdir()
    (tmp_path / "OneDrive/Desktop/copy/dev.ps1").write_text("<# BRMS MES - dev & deploy helper #>")
    assert find_console([tmp_path / "OneDrive/Desktop"]) is None       # two: ask, do not guess
    from helix.api import deploy_routes as dr
    settings = {}
    c = SimpleNamespace(settings=SimpleNamespace(get=settings.get, set=settings.__setitem__))
    import helix.adapters.console_scripts as cs
    cs_find = cs.find_console
    cs.find_console = lambda: "C:/found"
    try:
        assert dr.ensure_console_root(c) == "C:/found" and settings["console_root"] == "C:/found"
    finally:
        cs.find_console = cs_find


def test_an_older_dev_ps1_is_refused_in_a_sentence(tmp_path):
    (tmp_path / "dev.ps1").write_text("param(\n  [string]$Action = '',\n  [string]$Env = 'dev'\n)\n")
    s = ConsoleScripts(lambda: str(tmp_path), platform="win32")
    argv, why = s.deploy_argv("echo", "qa", "be-deploy", create=True)
    assert argv == [] and "does not know -Create" in why
    argv, why = s.deploy_argv("mes", "prod", typed="PROD")
    assert argv == [] and "-Typed" in why
    (tmp_path / "dev.ps1").write_text("param(\n  [string]$Env = 'dev',\n  [string]$Typed = '',\n  [switch]$Create\n)\n")
    argv, why = s.deploy_argv("echo", "prod", "be-deploy", typed="PROD", create=True)
    assert why is None and argv[-3:] == ["-Typed", "PROD", "-Create"]


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
    assert task.title == "Ship MES dev (the server)" and task.can_cancel and "old revision keeps serving" in task.cancel_note
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
    import helix.adapters.console_scripts as cs
    monkeypatch.setattr(cs, "find_console", lambda: None)      # this test is about the report, not the PC's disk
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


# ------------------------------------------------------------------------------ 2026-09-21: the pulse, the throttle

def test_the_pulse_measures_the_backend_and_counts_spawns():
    from helix.adapters.pulse import Pulse
    clock = [100.0]; cpu = [10.0]
    p = Pulse(clock=lambda: clock[0], cpu_time=lambda: cpu[0], cores=4)
    clock[0] += 2.0; cpu[0] += 1.0                      # one core busy half the time
    assert p.sample() == 50.0
    p.spawned("gcloud"); p.spawned("gcloud"); p.spawned("dev.ps1")
    r = p.read()
    assert r["cpu_core_pct"] == 50.0 and r["cpu_machine_pct"] == 12.5 and r["spawns_last_minute"] == 3
    assert r["spawns_by"] == {"gcloud": 2, "dev.ps1": 1}
    clock[0] += 61.0
    assert p.read()["spawns_last_minute"] == 0


def test_a_fresh_board_is_not_read_again_unless_forced():
    from tests.test_fleet_routes import _app, _call
    app, c = _app()
    st, first = _call(app, "POST", "/api/fleet/refresh", {})
    assert st == 200 and first["companies"][0]["checked_at"]
    seen = []
    real = c.fleet.read_company
    c.fleet.read_company = lambda co, **k: (seen.append(co), real(co, **k))[1]
    st, again = _call(app, "POST", "/api/fleet/refresh", {})
    assert st == 200 and seen == []                                  # answered from memory
    st, forced = _call(app, "POST", "/api/fleet/refresh", {"force": True})
    assert st == 200 and len(seen) == 1                              # Refresh by hand reads


# ------------------------------------------------------------------------------ 2026-09-22: the run you can see

def test_the_scripts_lines_become_phases_and_the_strand_never_goes_backwards(tmp_path):
    from helix.services.jobs import Jobs
    jobs = Jobs(tmp_path / "jobs.json")
    lines = ["Target project: OATS MES [DEV]", "Running backend behavior tests (fix #49)...", "Tests passed.",
             "Deploying brms-mes-api-dev to Cloud Run (us-west2) ...", "Building using Dockerfile and deploying container to Cloud Run service",
             "Creating Revision.....done", "Routing traffic....done", "Service URL: https://x.run.app", "Backend deployed OK.", "[dev.ps1] RESULT: OK"]
    scripts = _Talks({"be-deploy": (lines, 0)})
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    seen = []
    tick = iter(range(10_000))
    jobs = Jobs(tmp_path / "jobs.json", seen.append, clock=lambda: next(tick) * 0.5)     # every emit lands (no throttle)
    d = dp.DeployService(scripts, fleet, audit=dp.Audit(tmp_path / "a.jsonl"), push=seen.append,
                         identity=lambda: "brian_sullivan@mark1online.com", profile=lambda: "desktop", jobs=jobs)
    assert d.deploy("mes", "dev")["ok"]
    _wait(d)
    job = [j for j in jobs.list() if j["kind"] == "deploy"][0]
    assert job["progress"] == 1.0 and job["state"] == "done"                      # finish seals it
    fracs = [e["job"]["progress"] for e in seen if e.get("t") == "job" and e["job"].get("progress") is not None]
    assert fracs == sorted(fracs) and 0.97 in fracs and fracs[-1] == 1.0
    assert [n for n in (e["job"]["note"] for e in seen if e.get("t") == "job") if "Cloud Build" in n]
    assert dp.phase_of("[helix] Building Container") is None and dp.phase_of("hosting[x]: release complete")[0] == 0.96


def test_both_gives_each_script_half_the_strand(tmp_path):
    from helix.services.jobs import Jobs
    jobs = Jobs(tmp_path / "jobs.json")
    scripts = _Talks({"be-deploy": (["Backend deployed OK.", "[dev.ps1] RESULT: OK"], 0), "fe-deploy": (["Deploy complete", "[dev.ps1] RESULT: OK"], 0)})
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    seen = []
    tick = iter(range(10_000))
    jobs = Jobs(tmp_path / "jobs.json", seen.append, clock=lambda: next(tick) * 0.5)     # every emit lands (no throttle)
    d = dp.DeployService(scripts, fleet, audit=dp.Audit(tmp_path / "a.jsonl"), push=seen.append,
                         identity=lambda: "brian_sullivan@mark1online.com", profile=lambda: "desktop", jobs=jobs)
    assert d.deploy("mrp", "dev", action="both")["ok"]
    _wait(d)
    fracs = [round(e["job"]["progress"], 3) for e in seen if e.get("t") == "job" and e["job"].get("progress") is not None]
    assert 0.485 in fracs and 0.98 in fracs and fracs[-1] == 1.0            # 0.97/2, then the second half, then sealed


def test_helix_crashing_mid_run_closes_the_task_as_failed_not_running_forever(tmp_path):
    from helix.services.jobs import Jobs
    jobs = Jobs(tmp_path / "jobs.json")

    class Boom(_Scripts):
        def stream(self, *a, **k):
            raise NameError("name '_pulse' is not defined")
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    seen = []
    d = dp.DeployService(Boom(), fleet, audit=dp.Audit(tmp_path / "a.jsonl"), push=seen.append,
                         identity=lambda: "brian_sullivan@mark1online.com", profile=lambda: "desktop", jobs=jobs)
    assert d.deploy("mes", "dev")["ok"]
    _wait(d)
    job = [j for j in jobs.list() if j["kind"] == "deploy"][0]
    assert job["state"] == "failed" and "_pulse" in job["note"]
    assert seen[-1]["t"] == "deploy_done" and seen[-1]["ok"] is False
    assert d.status()["running"] is False and d.deploy("mes", "dev")["ok"]          # not stuck BUSY


def test_an_expired_sign_in_is_refused_before_anything_runs(tmp_path):
    class Expired(_Scripts):
        def signed_in(self):
            return False, "Your Google sign-in has expired. gcloud auth login"
    scripts = Expired()
    d, _ = _svc_with(tmp_path, scripts)
    r = d.deploy("mes", "dev")
    assert not r["ok"] and "sign-in has expired" in r["error"] and scripts.ran == []
    r = d.create_run("echo", "qa", typed="ECHO", sure=True)
    assert not r["ok"] and "sign-in has expired" in r["error"] and scripts.ran == []


def test_the_wrapper_pushes_every_stream_through_a_flushed_console():
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "C:\\con sole\\dev.ps1",
            "-Action", "be-deploy", "-App", "echo", "-Env", "qa", "-Typed", "PROD", "-Create"]
    w = ConsoleScripts.wrap(argv)
    assert w[:5] == argv[:5] and w[5] == "-Command"
    body = w[6]
    assert "& 'C:\\con sole\\dev.ps1' -Action 'be-deploy' -App 'echo' -Env 'qa' -Typed 'PROD' -Create *>&1" in body
    assert "InformationRecord" in body and "[Console]::Out.Flush()" in body and body.endswith("exit $LASTEXITCODE")
    assert ConsoleScripts.wrap(["gcloud", "run"]) == ["gcloud", "run"]


def test_a_quiet_run_says_so_and_what_is_underneath(tmp_path, monkeypatch):
    import threading, time
    from helix.services.jobs import Jobs
    monkeypatch.setattr(dp, "QUIET_S", 0.2)
    monkeypatch.setattr(dp, "_process_tree", lambda pid: "powershell > gcloud > python")
    jobs = Jobs(tmp_path / "jobs.json")

    class Silent(_Scripts):
        def stream(self, argv, cwd, on_line, timeout_s=0, on_start=None):
            if on_start:
                on_start(SimpleNamespace(pid=77, kill=lambda: None))
            time.sleep(0.8)
            on_line("[dev.ps1] RESULT: OK")
            return 0
    fleet = FleetService(_reader(), _Repos(), MemoryFleetState())
    seen = []
    d = dp.DeployService(Silent(), fleet, audit=dp.Audit(tmp_path / "a.jsonl"), push=seen.append,
                         identity=lambda: "brian_sullivan@mark1online.com", profile=lambda: "desktop", jobs=jobs)
    orig_wait = threading.Event.wait
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: orig_wait(self, min(t or 0, 0.05)))
    assert d.deploy("mes", "dev")["ok"]
    _wait(d)
    quiet = [e["line"] for e in seen if e.get("t") == "deploy" and "quiet for" in e.get("line", "")]
    assert quiet and "powershell > gcloud > python" in quiet[0] and "cloud-build" in quiet[0]
