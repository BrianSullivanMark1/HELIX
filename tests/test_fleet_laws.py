"""The fleet's laws — HELIX_MARK1_PLAN.md §11, one test per rule, plus the shape of the fleet itself.

These tests exist BEFORE anything calls the code they pin. That is the point: when Phase 2 writes its
first deploy button, the rules it must fail closed against are already proven, rather than being written
in the same hour as the code they are supposed to restrain.

Every rule below came from a real incident on the existing console. A failing test here means someone
has removed a scar's stitches.
"""
from __future__ import annotations

import pytest

from helix.domain import fleet
from helix.domain.fleet import Cell, Drift, Env, Health, Service, Serving


# ---------------------------------------------------------------------------- the shape of the fleet

def test_fleet_is_four_systems_by_three_environments():
    assert len(fleet.FLEET) == 12
    assert {s.system for s in fleet.FLEET} == set(fleet.SYSTEMS)
    for system in fleet.SYSTEMS:
        assert {s.env for s in fleet.services_for(system)} == set(Env)


def test_every_cell_key_is_unique():
    keys = [s.key for s in fleet.FLEET]
    assert len(keys) == len(set(keys))


def test_prod_service_names_are_not_derivable_and_the_table_says_so():
    """MES prod drops the suffix; WMS and MRP prod carry '-prod-flask'. If someone ever 'simplifies'
    this into a format string, these three assertions are what stops it."""
    assert fleet.find("MES", Env.PROD).run_service == "brms-mes-api"
    assert fleet.find("WMS", Env.PROD).run_service == "wms-prod-flask"
    assert fleet.find("MRP", Env.PROD).run_service == "mrp-prod-flask"


def test_echo_qa_and_prod_do_not_exist():
    for env in (Env.QA, Env.PROD):
        svc = fleet.find("ECHO", env)
        assert svc is not None, "the cell is part of the fleet"
        assert not svc.exists, "but nothing is deployed in it"
    assert fleet.find("ECHO", Env.DEV).exists


def test_deployable_excludes_the_absent_cells():
    assert len(fleet.deployable()) == 10
    assert all(s.exists for s in fleet.deployable())


def test_every_cell_defaults_to_the_main_branch():
    """There is no branch-per-environment convention in the console (Git-Branch defaults to 'main'),
    so every cell compares against main until someone deliberately changes one."""
    assert {s.branch for s in fleet.FLEET} == {"main"}


# ---------------------------------------------------------------------------- rule 1: forbidden flags

@pytest.mark.parametrize("system", ["WMS", "MRP"])
@pytest.mark.parametrize("env", list(Env))
@pytest.mark.parametrize("flag", ["--set-env-vars", "--vpc-connector", "--service-account"])
def test_rule1_forbidden_flags_are_refused_on_wms_and_mrp(system, env, flag):
    problems = fleet.check_deploy(system, env, [flag])
    assert problems, f"{flag} must be refused on {system} {env.value}"
    assert any(flag in p for p in problems), "the refusal names the flag"


def test_rule1_refusal_explains_the_consequence_not_just_the_rule():
    (problem,) = fleet.check_deploy("WMS", Env.DEV, ["--set-env-vars"])
    assert "loses its database" in problem


def test_rule1_a_flag_with_a_value_attached_is_still_caught():
    """Flags arrive as '--vpc-connector=brms-egress' as often as as two arguments. Splitting on '='
    is what makes the check see the flag name in both shapes."""
    assert fleet.check_deploy("MRP", Env.DEV, ["--vpc-connector=brms-egress"])


def test_rule1_an_ordinary_deploy_with_no_flags_is_allowed():
    assert fleet.check_deploy("WMS", Env.DEV) == []
    assert fleet.check_deploy("MRP", Env.QA, ["--quiet"]) == []


def test_rule1_labels_are_allowed_because_a_label_cannot_reach_anything():
    """--labels is how a commit gets stamped onto a revision (plan §6.6). It is metadata: it cannot
    change what the service can reach, so it is deliberately NOT on the forbidden list."""
    assert fleet.check_deploy("WMS", Env.DEV, ["--labels=commit=a41f9c2"]) == []
    assert fleet.check_deploy("MRP", Env.DEV, ["--labels", "commit=a41f9c2,by=mark1"]) == []


# ---------------------------------------------------------------------------- rule 2: MES env vars

def test_rule2_mes_env_vars_have_one_source_of_truth():
    (problem,) = fleet.check_deploy("MES", Env.DEV, ["--set-env-vars"])
    assert fleet.MES_ENV_SOURCE in problem


def test_rule2_mes_may_still_pass_other_flags():
    """MES deliberately DOES set env vars — but only from backend/deploy.ps1. Everything else about a
    MES deploy stays ordinary."""
    assert fleet.check_deploy("MES", Env.DEV, ["--service-account", "--vpc-connector"]) == []


# ---------------------------------------------------------------------------- rule 4: prod refusals

@pytest.mark.parametrize("system", ["MES", "WMS", "MRP"])
@pytest.mark.parametrize("action", ["local-run", "clear-records"])
def test_rule4_prod_refuses_local_run_and_record_clearing(system, action):
    problems = fleet.check_deploy(system, Env.PROD, action=action)
    assert problems and any(action in p for p in problems)


@pytest.mark.parametrize("action", ["local-run", "clear-records"])
def test_rule4_those_actions_are_fine_below_prod(action):
    assert fleet.check_deploy("MES", Env.DEV, action=action) == []


# ---------------------------------------------------------------------------- rule 5: never create

def test_rule5_a_service_that_does_not_exist_is_refused_not_created():
    (problem,) = fleet.check_deploy("ECHO", Env.PROD)
    assert "does not exist" in problem
    assert "will not create it" in problem


def test_rule5_refusal_says_does_not_exist_rather_than_failed():
    """An absent service is a known absence, not a failure. The wording is the test: 'failed' sends
    someone hunting for a broken deploy that never happened."""
    (problem,) = fleet.check_deploy("ECHO", Env.QA)
    assert "fail" not in problem.lower()


def test_rule5_a_system_outside_the_fleet_is_refused():
    (problem,) = fleet.check_deploy("PAYROLL", Env.DEV)
    assert "not part of the fleet" in problem


# ---------------------------------------------------------------------------- rule 7: ASCII only

def test_rule7_plain_ascii_passes():
    fleet.assert_ascii("Deployed brms-mes-api-dev to us-west2.")


@pytest.mark.parametrize("bad", [
    "Deployed — ok",       # em dash, the one a writing tool inserts
    "Rolled back → 6d1ac83",
    "café",
    "100°",
])
def test_rule7_non_ascii_is_refused(bad):
    with pytest.raises(ValueError) as exc:
        fleet.assert_ascii(bad, what="a generated .ps1")
    assert "rule 7" in str(exc.value)
    assert "a generated .ps1" in str(exc.value)


def test_rule7_reports_where_the_bad_character_is():
    found = fleet.non_ascii("ok ok — no")
    assert found == ((6, "—"),)


def test_rule7_every_refusal_this_module_produces_is_itself_ascii():
    """The refusals get printed to a Windows console, so they are subject to their own rule. This is
    the test that catches a nicely-typeset em dash added to a refusal string later."""
    produced: list[str] = []
    produced += fleet.check_deploy("WMS", Env.DEV, ["--set-env-vars", "--vpc-connector"])
    produced += fleet.check_deploy("MES", Env.DEV, ["--set-env-vars"])
    produced += fleet.check_deploy("ECHO", Env.PROD)
    produced += fleet.check_deploy("PAYROLL", Env.DEV)
    produced += fleet.check_deploy("MES", Env.PROD, action="local-run")
    produced += fleet.check_rollback("MES", Env.DEV, "never-served", ["mes-00021-abc"])
    produced += fleet.check_rollback("ECHO", Env.PROD, "x", [])
    assert produced, "the refusals actually fired"
    for line in produced:
        assert fleet.is_ascii(line), f"refusal is not ASCII: {line!r}"


# ---------------------------------------------------------------------------- rollback (§6.5)

def test_rollback_to_a_revision_that_never_served_is_refused():
    (problem,) = fleet.check_rollback("MES", Env.PROD, "mes-00099-zzz", ["mes-00021-abc", "mes-00020-aaa"])
    assert "never served traffic" in problem


def test_rollback_to_a_revision_that_did_serve_is_allowed():
    assert fleet.check_rollback("MES", Env.PROD, "mes-00020-aaa",
                                ["mes-00021-abc", "mes-00020-aaa"]) == []


def test_rollback_to_what_is_already_serving_is_refused():
    (problem,) = fleet.check_rollback("MES", Env.PROD, "mes-00021-abc",
                                      ["mes-00021-abc", "mes-00020-aaa"],
                                      currently_serving="mes-00021-abc")
    assert "already serving" in problem


def test_rollback_on_an_absent_service_is_refused():
    (problem,) = fleet.check_rollback("ECHO", Env.PROD, "anything", ["anything"])
    assert "does not exist" in problem


def test_rollback_takes_no_flags_so_rule_1_cannot_apply():
    """A rollback is a traffic shift, not a deploy: there is no flag surface to get wrong. This is
    why it is safe enough to ship alongside the first deploy button."""
    import inspect
    params = inspect.signature(fleet.check_rollback).parameters
    assert "flags" not in params


def test_rollback_targets_stop_at_the_depth_and_skip_what_is_serving():
    served = [f"mes-{n:05d}-x" for n in range(30, 20, -1)]   # 10 revisions, newest first
    targets = fleet.rollback_targets(served, currently_serving="mes-00030-x")
    assert len(targets) == fleet.DEFAULT_ROLLBACK_DEPTH
    assert "mes-00030-x" not in targets
    assert targets[0] == "mes-00029-x"


def test_rollback_depth_default_matches_what_the_console_actually_keeps():
    """dev.ps1's Run-CleanRevisions keeps the 5 newest and DELETES the rest. If this number ever
    disagrees with that one, HELIX will offer rollback targets that no longer exist."""
    assert fleet.DEFAULT_ROLLBACK_DEPTH == 5


# ---------------------------------------------------------------------------- production is human-only

def test_production_allowlist_is_exactly_three_people():
    """Brian, Brendan and Kate (decision 2026-09-19). Adding a name is a deliberate edit here, with a
    changelog row - never a setting."""
    assert fleet.PROD_ALLOWLIST == frozenset({
        "brian_sullivan@mark1online.com",
        "brendan_sullivan@mark1online.com",
        "kate@mark1online.com",
    })


@pytest.mark.parametrize("identity", [None, "", "   ", "someone@example.com",
                                      "brian_sullivan@gmail.com"])
def test_production_is_fail_closed_for_anyone_else(identity):
    assert fleet.may_touch_production(identity) is False


def test_production_allowlist_is_case_and_whitespace_insensitive():
    assert fleet.may_touch_production("  Brian_Sullivan@Mark1Online.com  ") is True


def test_plant_data_is_read_only_with_no_way_to_turn_it_off():
    assert fleet.PLANT_DATA_IS_READ_ONLY is True
    assert not any(
        "plant" in name.lower() and callable(getattr(fleet, name))
        for name in dir(fleet)
    ), "plant-data read-only is a constant, not a function somebody can pass False to"


# ---------------------------------------------------------------------------- drift

def test_drift_is_unknown_when_no_commit_was_recorded():
    """Today this is EVERY cell: deploys run --source from a working tree, so no revision carries a
    commit. Guessing CLEAN here would make the board a lie on the day it shipped."""
    assert fleet.drift_from("a41f9c2", None) is Drift.UNKNOWN
    assert fleet.drift_from(None, "a41f9c2") is Drift.UNKNOWN
    assert fleet.drift_from(None, None) is Drift.UNKNOWN


def test_drift_clean_when_they_match():
    assert fleet.drift_from("a41f9c2", "a41f9c2") is Drift.CLEAN


def test_drift_behind_is_ordinary_undeployed_work():
    assert fleet.drift_from("a41f9c2", "7e02b18", behind_by=11, in_history=True) is Drift.BEHIND


def test_drift_diverged_when_serving_something_the_branch_does_not_contain():
    assert fleet.drift_from("a41f9c2", "f2b8e51", behind_by=3, in_history=False) is Drift.DIVERGED


def test_drift_ahead_when_not_in_history_and_nothing_behind():
    assert fleet.drift_from("a41f9c2", "f2b8e51", behind_by=0, in_history=False) is Drift.AHEAD


def test_a_rollback_never_renders_as_behind():
    """The whole reason ROLLED_BACK exists. A deliberate act an hour ago must not look identical to
    ordinary un-deployed work."""
    assert fleet.drift_from("a41f9c2", "6d1ac83", behind_by=8,
                            in_history=True, rolled_back=True) is Drift.ROLLED_BACK


def test_only_ahead_and_diverged_ask_for_attention():
    assert fleet.ATTENTION == frozenset({Drift.AHEAD, Drift.DIVERGED})
    assert Drift.BEHIND not in fleet.ATTENTION
    assert Drift.ROLLED_BACK not in fleet.ATTENTION


# ---------------------------------------------------------------------------- cells and halves

def _cell(api_health=Health.OK, site_health=Health.OK, system="MES", env=Env.PROD):
    svc = fleet.find(system, env)
    return Cell(service=svc,
                api=Serving(revision="r1", health=api_health),
                site=Serving(revision="v1", health=site_health))


def test_a_cell_is_only_as_healthy_as_its_worse_half():
    """A healthy API behind a dead frontend is not a healthy cell. Modelling only the API is how a
    board reports green while nobody can load the page."""
    assert _cell(Health.OK, Health.OK).health is Health.OK
    assert _cell(Health.OK, Health.DOWN).health is Health.DOWN
    assert _cell(Health.DEGRADED, Health.OK).health is Health.DEGRADED
    assert _cell(Health.OK, Health.UNKNOWN).health is Health.UNKNOWN


def test_an_unread_cell_is_unknown_never_ok():
    svc = fleet.find("MES", Env.PROD)
    assert Cell(service=svc).health is Health.UNKNOWN


def test_an_absent_cell_reports_absent_regardless_of_anything_else():
    svc = fleet.find("ECHO", Env.PROD)
    assert Cell(service=svc).health is Health.ABSENT


def test_a_traffic_split_is_visible_rather_than_assumed_away():
    assert Serving(revision="r1", traffic_percent=100).is_split is False
    assert Serving(revision="r1", traffic_percent=60).is_split is True
    assert Serving(revision="r1").is_split is False, "unknown traffic is not a claim of a split"


def test_a_dirty_revision_is_recorded_as_such():
    """A revision built from a working tree with uncommitted edits is not reproducible from the repo.
    Showing a clean-looking SHA for it would be a lie in the one place the board exists to be honest."""
    s = Serving(revision="r1", commit="a41f9c2", dirty=True)
    assert s.dirty and s.commit == "a41f9c2"


def test_needs_attention_covers_both_drift_and_health():
    svc = fleet.find("WMS", Env.QA)
    assert Cell(service=svc, drift=Drift.DIVERGED).needs_attention
    assert Cell(service=svc, api=Serving(health=Health.DOWN), drift=Drift.CLEAN).needs_attention
    assert not Cell(service=svc, api=Serving(health=Health.OK),
                    site=Serving(health=Health.OK), drift=Drift.BEHIND).needs_attention


def test_cell_key_is_stable_readable_and_colony_prefixed():
    """Company-prefixed so two companies with an app called MES can never collide in Firestore."""
    assert fleet.find("MES", Env.PROD).key == "oats-overnight/MES/prod"
    assert fleet.find("ECHO", Env.DEV).key == "oats-overnight/ECHO/dev"


# ---------------------------------------------------------------------------- companies (§17.2)

def test_there_is_exactly_one_company_today_and_every_cell_belongs_to_it():
    assert [c.id for c in fleet.COMPANIES] == ["oats-overnight"]
    assert {s.company for s in fleet.FLEET} == {"oats-overnight"}


def test_the_company_carries_the_gcp_facts_and_the_allowlist():
    c = fleet.company("oats-overnight")
    assert c.gcp_project == "windy-celerity-392822"
    assert c.region == "us-west2"
    assert c.prod_allowlist == fleet.PROD_ALLOWLIST, "one source of truth"
    assert c.may_touch_production("brian_sullivan@mark1online.com")
    assert not c.may_touch_production("someone@example.com")
    assert not c.may_touch_production(None)


def test_apps_are_the_distinct_app_names_in_table_order():
    assert fleet.apps("oats-overnight") == ("MES", "WMS", "MRP", "ECHO")
    assert fleet.apps("no-such-colony") == ()


def test_app_is_the_plain_word_for_system():
    svc = fleet.find("WMS", Env.DEV)
    assert svc.app == svc.system == "WMS"


def test_an_unknown_company_is_none_not_an_error():
    assert fleet.company("acme") is None


# ---------------------------------------------------------------------------- purity

def test_the_domain_imports_no_other_helix_layer():
    """The dependency rule, enforced rather than reviewed: domain/ must stay pure so the laws can be
    read, tested and audited without standing up an adapter."""
    import pathlib
    src = pathlib.Path(fleet.__file__).read_text(encoding="utf-8")
    for banned in ("from helix.services", "from helix.adapters", "from helix.ports",
                   "from helix.ui", "from helix.api", "from helix.app",
                   "import subprocess", "import requests", "import urllib"):
        assert banned not in src, f"domain/fleet.py must not contain {banned!r}"


def test_wms_and_mrp_service_names_follow_dev_ps1_for_every_environment():
    """dev.ps1: ApiSvc="wms-$Env-flask" / "mrp-$Env-flask". The brief had bare 'wms-dev'; the first
    live read (2026-09-17) came back ABSENT for four cells and this is the pin so it cannot regress."""
    for system, prefix in (("WMS", "wms"), ("MRP", "mrp")):
        for env in fleet.Env:
            svc = fleet.find(system, env)
            assert svc is not None and svc.run_service == f"{prefix}-{env.value}-flask", svc


def test_mes_prod_drops_the_suffix_and_echo_has_only_dev():
    assert fleet.find("MES", fleet.Env.PROD).run_service == "brms-mes-api"
    assert fleet.find("MES", fleet.Env.QA).run_service == "brms-mes-api-qa"
    assert fleet.find("ECHO", fleet.Env.DEV).run_service == "brms-echo-api-dev"
    assert fleet.find("ECHO", fleet.Env.QA).run_service is None
    assert fleet.find("ECHO", fleet.Env.PROD).run_service is None
