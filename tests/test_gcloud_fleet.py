"""GcloudFleet against a scripted gcloud - every failure kind names itself, nothing raises, and the
three provenance sources are tried in the right order.

No real gcloud is spawned here; `runner` and `http_get` are injected. The real runner is one function
that wraps subprocess.run, and its contract (rc 127 = binary missing, rc 124 = timeout) is what the
fakes reproduce.
"""
from __future__ import annotations

import json

import pytest

from helix.adapters import gcloud_fleet as g
from helix.domain import fleet
from helix.domain.fleet import Env, Health

MES_DEV = fleet.find("MES", Env.DEV)
WMS_DEV = fleet.find("WMS", Env.DEV)


def service_doc(url="https://wms-dev-abc-wl.a.run.app", ready=True, traffic=None, latest="wms-dev-00021-abc"):
    return {
        "metadata": {"name": "wms-dev"},
        "status": {
            "url": url,
            "latestReadyRevisionName": latest,
            "traffic": traffic if traffic is not None else [{"revisionName": latest, "percent": 100}],
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def revision_doc(name, ts, labels=None, ready=True):
    return {
        "metadata": {"name": name, "creationTimestamp": ts, "labels": labels or {}},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


class Script:
    """A gcloud that answers from a table keyed by the subcommand, and records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append(argv)
        if argv[1:2] == ["--version"]:
            return self.answers.get("version", g.Ran(0, "Google Cloud SDK 500.0.0", ""))
        sub = " ".join(argv[2:4])   # "services describe" / "revisions list"
        return self.answers[sub]


def no_http(url, t):
    return 0, ""


# ---------------------------------------------------------------------------- a clean read

def test_reads_the_serving_revision_its_label_and_who_deployed_it():
    revs = [
        revision_doc("wms-dev-00021-abc", "2026-09-16T10:00:00Z", {"version": "c9d3a70", "by": "bgsul"}),
        revision_doc("wms-dev-00020-zzz", "2026-09-10T10:00:00Z", {"version": "1b6740e", "by": "bgsul"}),
    ]
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("proj", "us-west2", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.ok
    assert r.api.revision == "wms-dev-00021-abc"
    assert r.api.commit == "c9d3a70" and r.api.dirty is False
    assert r.api.deployed_by == "bgsul"
    assert r.api.health is Health.OK
    assert r.api.traffic_percent == 100 and not r.api.is_split
    assert r.served_revisions == ("wms-dev-00021-abc", "wms-dev-00020-zzz"), "newest first"
    assert r.api.deployed_at.year == 2026


def test_every_gcloud_call_names_project_region_and_asks_for_json():
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    g.GcloudFleet("windy-celerity-392822", "us-west2", runner=run, http_get=no_http).read_cell(WMS_DEV)
    for argv in run.calls:
        assert "--project" in argv and "windy-celerity-392822" in argv
        assert "--region" in argv and "us-west2" in argv
        assert "--format" in argv and "json" in argv


def test_the_adapter_only_ever_reads():
    """No mutating verb may appear in any argv this adapter builds. This is the read-only guarantee
    of §7.2, pinned rather than promised."""
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    a = g.GcloudFleet("p", "r", runner=run, http_get=no_http)
    a.available(); a.read_cell(WMS_DEV)
    for argv in run.calls:
        for verb in ("deploy", "delete", "update", "update-traffic", "create", "set-iam-policy"):
            assert verb not in argv, f"read-only adapter issued {verb!r}"


# ---------------------------------------------------------------------------- the dirty flag

def test_a_dirty_label_is_reported_as_dirty_with_the_sha_kept():
    revs = [revision_doc("wms-dev-00021-abc", "2026-09-16T10:00:00Z", {"version": "76d4248-dirty"})]
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.api.commit == "76d4248" and r.api.dirty is True


def test_version_unknown_is_no_commit_not_the_string_unknown():
    revs = [revision_doc("wms-dev-00021-abc", "2026-09-16T10:00:00Z", {"version": "unknown"})]
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc(url="")), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.api.commit is None


# ---------------------------------------------------------------------------- provenance fallbacks

def test_no_label_falls_back_to_the_apps_own_health_endpoint():
    """MES has no label but bakes version into /api/health (backend/deploy.ps1). Source (b)."""
    revs = [revision_doc("brms-mes-api-dev-00030-q", "2026-09-16T10:00:00Z")]
    seen = []

    def http(url, t):
        seen.append(url)
        return 200, json.dumps({"db": "BRMS_database_dev", "version": "4eeae43-dirty"})

    run = Script({"services describe": g.Ran(0, json.dumps(
                      service_doc(url="https://brms-mes-api-dev-fofbmtg3bq-wl.a.run.app",
                                  latest="brms-mes-api-dev-00030-q")), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=http).read_cell(MES_DEV)
    assert seen == ["https://brms-mes-api-dev-fofbmtg3bq-wl.a.run.app/api/health"]
    assert r.api.commit == "4eeae43" and r.api.dirty is True


def test_the_health_probe_is_fenced_to_run_app_hosts():
    """The one HTTP call this adapter makes may only go to the service's own *.run.app host. A
    service doc pointing anywhere else gets no probe at all."""
    revs = [revision_doc("x-00001-a", "2026-09-16T10:00:00Z")]
    seen = []

    def http(url, t):
        seen.append(url)
        return 200, json.dumps({"version": "deadbeef"})

    run = Script({"services describe": g.Ran(0, json.dumps(service_doc(url="https://evil.example.com")), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=http).read_cell(WMS_DEV)
    assert seen == []
    assert r.api.commit is None


def test_a_label_wins_over_the_health_endpoint():
    revs = [revision_doc("wms-dev-00021-abc", "2026-09-16T10:00:00Z", {"version": "c9d3a70"})]
    seen = []

    def http(url, t):
        seen.append(url); return 200, json.dumps({"version": "different"})

    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=http).read_cell(WMS_DEV)
    assert r.api.commit == "c9d3a70" and seen == [], "no probe when the label already answered"


def test_no_provenance_anywhere_is_honestly_none():
    revs = [revision_doc("wms-dev-00021-abc", "2026-09-16T10:00:00Z")]
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, json.dumps(revs), "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=lambda u, t: (404, "")).read_cell(WMS_DEV)
    assert r.ok and r.api.commit is None and r.api.revision == "wms-dev-00021-abc"


# ---------------------------------------------------------------------------- health + traffic

def test_a_not_ready_service_is_down_never_ok():
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc(ready=False)), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.api.health is Health.DOWN


def test_a_traffic_split_is_reported_not_assumed_away():
    traffic = [{"revisionName": "wms-dev-00021-abc", "percent": 60},
               {"revisionName": "wms-dev-00020-zzz", "percent": 40}]
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc(traffic=traffic)), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.api.revision == "wms-dev-00021-abc" and r.api.traffic_percent == 60 and r.api.is_split


def test_hosting_half_is_unknown_and_says_so():
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.site.health is Health.UNKNOWN
    assert r.detail == g.HOSTING_NOTE


# ---------------------------------------------------------------------------- absences

def test_an_absent_cell_in_the_table_spawns_nothing():
    run = Script({})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(fleet.find("ECHO", Env.PROD))
    assert r.ok and r.api is None and r.site is None
    assert run.calls == []


def test_table_says_exists_but_cloud_run_says_not_found_is_a_finding_not_a_failure():
    run = Script({"services describe": g.Ran(1, "", "ERROR: (gcloud.run.services.describe) Cannot find service [wms-dev]: NOT_FOUND")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.ok is True
    assert r.api.health is Health.ABSENT
    assert r.problem == g.NOT_FOUND_NOTE


# ---------------------------------------------------------------------------- failure kinds, named

@pytest.mark.parametrize("ran,expect", [
    (g.Ran(127, "", "not found"), g.NOT_INSTALLED),
    (g.Ran(124, "", "timeout"), g.TIMED_OUT),
    (g.Ran(1, "", "ERROR: (gcloud.run.services.describe) You do not currently have an active account selected.\nRun: gcloud auth login"), g.NOT_LOGGED_IN),
    (g.Ran(1, "", "ERROR: Reauthentication required."), g.NOT_LOGGED_IN),
    (g.Ran(1, "", "ERROR: (gcloud.run.services.describe) PERMISSION_DENIED: Permission 'run.services.get' denied"), g.NO_PERMISSION),
    (g.Ran(1, "", "something else entirely"), g.BAD_OUTPUT),
])
def test_each_failure_kind_gets_its_own_sentence(ran, expect):
    run = Script({"services describe": ran})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.ok is False and r.problem == expect


def test_a_cli_problem_is_never_reported_as_a_credential_problem():
    """rc 127 with 'login' somewhere in the noise is STILL 'not installed'. The order in classify()
    is the whole point of this test."""
    run = Script({"services describe": g.Ran(127, "", "gcloud: command not found; try gcloud auth login")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.problem == g.NOT_INSTALLED


def test_every_problem_sentence_is_ascii():
    for s in (g.NOT_INSTALLED, g.NOT_LOGGED_IN, g.NO_PERMISSION, g.TIMED_OUT, g.BAD_OUTPUT,
              g.NOT_FOUND_NOTE, g.HOSTING_NOTE):
        fleet.assert_ascii(s, "gcloud_fleet problem text")


def test_garbage_json_is_bad_output_not_an_exception():
    run = Script({"services describe": g.Ran(0, "<html>login page</html>", "")})
    r = g.GcloudFleet("p", "r", runner=run, http_get=no_http).read_cell(WMS_DEV)
    assert r.ok is False and r.problem == g.BAD_OUTPUT


# ---------------------------------------------------------------------------- availability + read_all

def test_available_probes_version_once_and_caches():
    run = Script({"version": g.Ran(0, "Google Cloud SDK 500", "")})
    a = g.GcloudFleet("p", "r", runner=run, http_get=no_http)
    assert a.available() == (True, None)
    assert a.available() == (True, None)
    assert sum(1 for c in run.calls if c[1:2] == ["--version"]) == 1


def test_available_says_not_installed_when_the_probe_fails():
    run = Script({"version": g.Ran(127, "", "")})
    assert g.GcloudFleet("p", "r", runner=run, http_get=no_http).available() == (False, g.NOT_INSTALLED)


def test_read_all_keeps_order_and_includes_absent_cells():
    run = Script({"services describe": g.Ran(0, json.dumps(service_doc()), ""),
                  "revisions list": g.Ran(0, "[]", "")})
    a = g.GcloudFleet("p", "r", runner=run, http_get=no_http, workers=3)
    out = a.read_all(list(fleet.FLEET))
    assert [r.service.key for r in out] == [s.key for s in fleet.FLEET]
    absent = [r for r in out if not r.service.exists]
    assert len(absent) == 2 and all(r.ok and r.api is None for r in absent)


# ------------------------------------------------------------------------------------------------
# REAL DATA. tests/fixtures/mes_dev/ is Brian's actual `gcloud run services describe brms-mes-api-dev`
# and `revisions list` from 2026-09-17, slimmed to metadata + status (env and annotations stripped,
# 7 of the 53 revisions kept). The adapter must read the real shape exactly this way; if Cloud Run's
# output ever drifts, this is the test that goes red first.
# ------------------------------------------------------------------------------------------------

import pathlib

_FIX = pathlib.Path(__file__).parent / "fixtures" / "mes_dev"


def _real_mes_dev(http_get=None):
    svc_json = (_FIX / "service.json").read_text(encoding="utf-8")
    rev_json = (_FIX / "revisions.json").read_text(encoding="utf-8")

    def replay(argv, timeout):
        if "describe" in argv:
            return g.Ran(0, svc_json, "")
        if "revisions" in argv:
            return g.Ran(0, rev_json, "")
        return g.Ran(0, "Google Cloud SDK 500.0.0", "")

    return g.GcloudFleet(fleet.GCP_PROJECT, fleet.GCP_REGION, runner=replay,
                         http_get=http_get or (lambda url, t: (0, "")))


def test_real_mes_dev_export_reads_as_one_healthy_cell_at_full_traffic():
    r = _real_mes_dev().read_cell(fleet.find("MES", Env.DEV))
    assert r.ok and r.problem is None
    assert r.api.revision == "brms-mes-api-dev-00325-xtq"
    assert r.api.traffic_percent == 100 and not r.api.is_split
    assert r.api.health is Health.OK
    assert r.api.deployed_at.isoformat().startswith("2026-09-14T16:21:14")
    # Hosting half is not read yet - it must say so rather than pretend.
    assert r.site.health is Health.UNKNOWN and r.detail == g.HOSTING_NOTE


def test_real_mes_dev_export_has_no_version_label_yet_so_commit_is_honestly_none():
    """The label ships with the dev.ps1/deploy.ps1 patch. Until a deploy carries it, and with
    /api/health unreachable, the adapter says None - never a guess."""
    r = _real_mes_dev().read_cell(fleet.find("MES", Env.DEV))
    assert r.api.commit is None and r.api.dirty is False and r.api.deployed_by is None


def test_real_mes_dev_export_falls_back_to_api_health_for_the_commit():
    seen = []

    def http(url, t):
        seen.append(url)
        return 200, json.dumps({"status": "ok", "version": "a41f9c2-dirty"})

    r = _real_mes_dev(http).read_cell(fleet.find("MES", Env.DEV))
    assert seen == ["https://brms-mes-api-dev-fofbmtg3bq-wl.a.run.app/api/health"]
    assert (r.api.commit, r.api.dirty) == ("a41f9c2", True)


def test_real_mes_dev_export_lists_served_revisions_newest_first():
    r = _real_mes_dev().read_cell(fleet.find("MES", Env.DEV))
    assert r.served_revisions[0] == "brms-mes-api-dev-00325-xtq"
    assert r.served_revisions[1] == "brms-mes-api-dev-00324-qcg"
    assert len(r.served_revisions) == 7
    # Rollback targets are the newest N that served, excluding the one serving now.
    targets = fleet.rollback_targets(r.served_revisions, r.api.revision)
    assert targets[0] == "brms-mes-api-dev-00324-qcg"
    assert len(targets) == min(fleet.DEFAULT_ROLLBACK_DEPTH, 6)


def test_gcloud_binary_is_resolved_through_which_so_windows_finds_gcloud_cmd(monkeypatch):
    """On Windows the SDK has gcloud.cmd, not gcloud.exe; bare 'gcloud' is a FileNotFoundError."""
    monkeypatch.setattr(g.shutil, "which", lambda name: r"C:\Cloud SDK\bin\gcloud.cmd" if name == "gcloud" else None)
    fl = g.GcloudFleet("p", "r", runner=lambda a, t: g.Ran(0, "{}", ""))
    assert fl._gcloud.endswith("gcloud.cmd")
    monkeypatch.setattr(g.shutil, "which", lambda name: None)
    assert g.GcloudFleet("p", "r", runner=lambda a, t: g.Ran(0, "{}", ""))._gcloud == "gcloud"
