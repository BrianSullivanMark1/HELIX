"""Faculty packs and the two profiles — HELIX_MARK1_PLAN.md §4 and §5.

The load-bearing tests here are the two that catch a MISTAKE rather than a regression:

  - test_every_registered_tool_is_claimed_by_exactly_one_pack: adding a tool and forgetting to place it
    would otherwise make it visible everywhere, including on CLOUD.
  - test_cloud_carries_nothing_that_writes_spends_or_builds: the CLOUD allowlist is the whole security
    boundary for the team's deployment, and it is small enough to assert item by item.
"""
from __future__ import annotations

import pytest

from helix.domain import packs
from helix.domain.runtime_profile import (
    CLOUD_TOOLS, HelixProfile, PROFILE_ENV, UnknownProfile,
    allowed_tools, from_env, may_dream, may_reach_production, may_self_modify, must_not_build, parse,
)

# The 80 tools HELIX registers today (README, "Every tool HELIX has"), plus the two Phase 1 fleet reads.
# Kept here as a literal so this file is what notices when the registry and the packs disagree; the
# integration test below cross-checks it against services/tools.py when that module can be imported.
REGISTERED = sorted(set(packs.CORE_TOOLS) | {t for p in packs.PACKS for t in p.tools})


# ---------------------------------------------------------------------------- the partition

def test_every_registered_tool_is_claimed_by_exactly_one_pack():
    assert packs.unknown_tools(REGISTERED) == frozenset()


def test_no_tool_is_claimed_by_two_packs():
    assert packs.double_claimed() == frozenset()


def test_core_and_the_packs_cover_the_whole_surface():
    """80 tools in the README, plus fleet_status and fleet_history, plus the five SAP tools Brendan
    added on 2026-09 (which this very test caught unplaced on first run against the real registry).
    If this number moves without a pack moving with it, something was added and never placed."""
    assert len(REGISTERED) == 87


def test_pack_ids_are_unique_and_settings_keys_are_too():
    ids = [p.id for p in packs.PACKS]
    keys = [p.setting for p in packs.PACKS]
    assert len(ids) == len(set(ids))
    assert len(keys) == len(set(keys))


def test_settings_keys_are_prefixed_so_they_are_greppable():
    assert all(p.setting == f"pack_{p.id}" for p in packs.PACKS)


def test_pack_of_answers_for_packs_and_stays_quiet_for_core():
    assert packs.pack_of("design_enclosure") == "maker"
    assert packs.pack_of("fleet_status") == "fleet"
    assert packs.pack_of("view_camera") == "vision"
    assert packs.pack_of("search_amazon") == "purchasing"
    assert packs.pack_of("sap_table") == "sap"
    assert packs.pack_of("build_app") is None, "core tools belong to no pack"
    assert packs.pack_of("nonsense") is None


# ---------------------------------------------------------------------------- switching packs off

def _settings(**overrides):
    """A settings getter that answers from `overrides` and falls back to each pack's default."""
    def get(key, default=None):
        return overrides.get(key, default)
    return get


def test_all_packs_on_by_default():
    assert packs.enabled(_settings()) == packs.PACK_IDS


def test_disabling_every_pack_leaves_exactly_core_and_helix_still_converses():
    off = _settings(**{p.setting: False for p in packs.PACKS})
    assert packs.enabled(off) == frozenset()
    tools = packs.tools_for(packs.enabled(off))
    assert tools == packs.CORE_TOOLS
    # The things that make HELIX itself are all still there.
    for essential in ("build_app", "remember", "read_file", "research_search",
                      "improve_helix", "think_harder"):
        assert essential in tools


def test_disabling_one_pack_removes_only_its_tools():
    on = packs.enabled(_settings(pack_purchasing=False))
    tools = packs.tools_for(on)
    assert "search_amazon" not in tools
    assert "design_enclosure" in tools
    assert "fleet_status" in tools
    assert "build_app" in tools


def test_maker_needs_vision_and_switches_itself_off_without_it():
    """check_fit and camera_measure are maker tools that run on the vision pack's camera. Leaving maker
    on with vision off would present tools that cannot work, and a broken tool is worse than an absent
    one."""
    on = packs.enabled(_settings(pack_vision=False))
    assert "vision" not in on
    assert "maker" not in on, "maker requires vision"
    assert "fleet" in on and "purchasing" in on, "unrelated packs are unaffected"
    tools = packs.tools_for(on)
    assert "check_fit" not in tools and "design_enclosure" not in tools


def test_the_dependency_settles_rather_than_looping():
    """enabled() runs to a fixed point. A dependency chain must terminate, not spin."""
    assert packs.enabled(_settings(pack_vision=False, pack_maker=True)) == frozenset(
        {"fleet", "sap", "purchasing"})


def test_pages_follow_their_pack():
    assert "strand" in packs.pages_for(packs.PACK_IDS)
    assert "strand" not in packs.pages_for(packs.enabled(_settings(pack_fleet=False)))
    assert "studio" in packs.pages_for({"maker"})


def test_defaults_are_all_on_for_a_fresh_desktop_install():
    assert packs.defaults() == {p.setting: True for p in packs.PACKS}


# ---------------------------------------------------------------------------- the CLOUD allowlist

def test_cloud_is_exactly_the_two_fleet_reads_today():
    assert CLOUD_TOOLS == frozenset({"fleet_status", "fleet_history"})


@pytest.mark.parametrize("tool", [
    # writes and spends
    "build_app", "build_task", "write_file", "delete_build", "add_to_cart", "open_cart",
    # self-modification
    "improve_helix", "approve_self_change", "rebuild_helix", "dream_now",
    # the machine
    "open_program", "media_control", "go_to_sleep",
    # the user's private world
    "read_file", "list_folder", "check_email", "check_calendar", "view_screen", "view_camera",
    # memory about a person
    "remember_about_me", "set_location",
    # credentials
    "connect_service", "call_api",
    # the SAP catalog and its write
    "sap_sql", "sap_edw",
])
def test_cloud_carries_nothing_that_writes_spends_or_builds(tool):
    """Enumerated by name so ADDING one of these to CLOUD_TOOLS is a red test, not a code review that
    might not happen."""
    assert tool not in CLOUD_TOOLS


def test_cloud_tools_are_a_subset_of_what_the_packs_offer():
    """A tool on the cloud allowlist that no pack carries would be unreachable and would quietly
    misrepresent the size of the cloud surface."""
    everything = packs.tools_for(packs.PACK_IDS)
    assert CLOUD_TOOLS <= everything


def test_allowed_tools_is_an_intersection_so_a_pack_cannot_widen_cloud():
    everything = packs.tools_for(packs.PACK_IDS)
    assert allowed_tools(HelixProfile.CLOUD, everything) == CLOUD_TOOLS
    # And a pretend registry containing a dangerous tool cannot smuggle it through.
    assert allowed_tools(HelixProfile.CLOUD, everything | {"rm_rf"}) == CLOUD_TOOLS


def test_cloud_cannot_reach_a_tool_its_packs_switched_off():
    """Both have to say yes. With the fleet pack off, CLOUD has nothing at all."""
    off = packs.tools_for(packs.enabled(_settings(pack_fleet=False)))
    assert allowed_tools(HelixProfile.CLOUD, off) == frozenset()


def test_desktop_gets_whatever_its_packs_allow():
    everything = packs.tools_for(packs.PACK_IDS)
    assert allowed_tools(HelixProfile.DESKTOP, everything) == everything


# ---------------------------------------------------------------------------- profile resolution

@pytest.mark.parametrize("raw,expected", [
    (None, HelixProfile.DESKTOP),
    ("", HelixProfile.DESKTOP),
    ("   ", HelixProfile.DESKTOP),
    ("desktop", HelixProfile.DESKTOP),
    ("DESKTOP", HelixProfile.DESKTOP),
    ("  Cloud  ", HelixProfile.CLOUD),
    ("cloud", HelixProfile.CLOUD),
])
def test_profile_parsing(raw, expected):
    assert parse(raw) is expected


@pytest.mark.parametrize("raw", ["clould", "prod", "desktop cloud", "1", "none"])
def test_an_unrecognised_profile_refuses_to_start(raw):
    """A typo that silently fell back to DESKTOP in the cloud is the exact accident this split exists
    to prevent, so it is fatal rather than a warning."""
    with pytest.raises(UnknownProfile) as exc:
        parse(raw)
    assert PROFILE_ENV in str(exc.value)


def test_profile_comes_from_the_environment_only():
    assert from_env({}) is HelixProfile.DESKTOP
    assert from_env({PROFILE_ENV: "cloud"}) is HelixProfile.CLOUD
    # Not from anything a request or a settings file could set.
    assert from_env({"profile": "cloud", "HELIX_PROFILE_OVERRIDE": "cloud"}) is HelixProfile.DESKTOP


# ---------------------------------------------------------------------------- what CLOUD never builds

def test_cloud_never_constructs_the_dangerous_services():
    unbuilt = must_not_build(HelixProfile.CLOUD)
    for name in ("selfdev", "dream", "coder", "files", "desktop"):
        assert name in unbuilt
    assert must_not_build(HelixProfile.DESKTOP) == frozenset()


def test_cloud_cannot_self_modify_dream_or_reach_production():
    assert may_self_modify(HelixProfile.CLOUD) is False
    assert may_dream(HelixProfile.CLOUD) is False
    assert may_reach_production(HelixProfile.CLOUD) is False


def test_desktop_can_do_all_three():
    assert may_self_modify(HelixProfile.DESKTOP) is True
    assert may_dream(HelixProfile.DESKTOP) is True
    assert may_reach_production(HelixProfile.DESKTOP) is True


# ---------------------------------------------------------------------------- purity

def test_these_modules_import_no_other_helix_layer():
    import pathlib
    for module in (packs, __import__("helix.domain.runtime_profile", fromlist=["x"])):
        src = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("from helix.services", "from helix.adapters", "from helix.ports",
                       "from helix.ui", "from helix.api", "from helix.app"):
            assert banned not in src, f"{module.__name__} must not contain {banned!r}"


# ---------------------------------------------------------------------------- against the real registry

def _registry_tool_names() -> set[str]:
    """The tool names services/tools.py actually declares, read from its SOURCE.

    Source-scanned rather than imported on purpose: tools.py pulls in most of the app, and this check
    must stay a cheap unit test that runs in isolation. The registry declares each tool as name="..."
    in its spec, so that is what we count.
    """
    import pathlib
    import re
    here = pathlib.Path(__file__).resolve().parent
    src = here.parent / "helix" / "services" / "tools.py"
    if not src.is_file():
        return set()
    text = src.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r'name="([a-z][a-z0-9_]*)"', text))


def test_the_live_registry_agrees_with_the_packs():
    """Every tool the registry declares is claimed by exactly one pack or by core.

    This is the fail-closed hinge doing its job against the real file. Add a tool to tools.py and forget
    to place it in a pack, and this goes red - instead of the tool being quietly visible everywhere,
    including on the CLOUD profile.
    """
    names = _registry_tool_names()
    if not names:
        pytest.skip("helix/services/tools.py not found next to this test tree")
    unplaced = packs.unknown_tools(names)
    assert unplaced == frozenset(), f"registered in tools.py but in no pack: {sorted(unplaced)}"


def test_the_packs_name_no_tool_the_registry_does_not_have():
    """The other direction: a pack listing a tool that no longer exists would silently over-report
    what a profile can reach, and would never fail on its own."""
    names = _registry_tool_names()
    if not names:
        pytest.skip("helix/services/tools.py not found next to this test tree")
    # fleet_status / fleet_history arrive with Phase 1's service layer; until then they are expected
    # to be absent from the registry and are the ONLY permitted exception.
    pending = {"fleet_status", "fleet_history"}
    claimed = set(packs.CORE_TOOLS) | {t for p in packs.PACKS for t in p.tools}
    phantom = claimed - names - pending
    assert phantom == frozenset(), f"named by a pack but not in tools.py: {sorted(phantom)}"
