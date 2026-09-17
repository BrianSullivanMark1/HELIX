"""Faculty packs — which capabilities are switched on, as a unit.

Pure data + pure validators, like `constitution.py` and `fleet.py`. No I/O, no other HELIX layer.

WHY PACKS. HELIX has 80 tools across five unrelated worlds: the Forge, the maker flow, sight, shopping,
and now the fleet. A CLOUD deployment for the team needs exactly one of those worlds. Without packs,
"the team gets the fleet page" means shipping all of HELIX with most of it hidden, and hidden is not the
same as absent. A pack is the unit that makes the CLOUD profile (runtime_profile.py) a genuinely small
thing rather than a big thing wearing a smaller UI.

THE FAIL-CLOSED HINGE is `unknown_tools`. If someone registers a tool and forgets to put it in a pack,
the safe outcome is a red test, not a tool that is quietly available everywhere. `tests/test_packs.py`
asserts that set is empty, so forgetting costs a test run rather than a hole.

This file is in PROTECTED_FILES (HELIX_MARK1_PLAN.md §10.1): what capability a pack carries decides what
a remote user can reach, so the nightly dream may not edit it.

Contract: HELIX_MARK1_PLAN.md §4.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

# --------------------------------------------------------------------------------------------------
# CORE — always on, never gated. Converse, remember, read the user's world, research, build, and
# self-improve. Disabling every pack must still leave a working HELIX, which is what makes a pack
# genuinely optional rather than load-bearing.
# --------------------------------------------------------------------------------------------------

CORE_TOOLS: frozenset[str] = frozenset({
    # The Forge — making things is what HELIX is
    "build_app", "build_task", "create_agent", "create_knowledge", "create_workflow",
    "run_task", "run_agent", "run_workflow", "open_build", "rename_build", "delete_build",
    "prioritize_build", "cancel_build", "set_agent_enabled",
    "list_apps", "list_builds", "list_workflows",
    # Memory, knowledge and grounding
    "remember", "remember_about_me", "set_location", "search_knowledge",
    # The user's world, read-only (plus the one guarded write)
    "check_email", "check_calendar", "list_folder", "read_file", "write_file",
    "call_api", "connect_service",
    # Research and verified knowledge
    "research_search", "research_read", "verified_facts", "note_verified_fact", "forget_verified",
    # Reminders and timers
    "set_reminder", "cancel_reminder", "list_reminders",
    # Self-improvement — the dream lane
    "improve_helix", "approve_self_change", "reject_self_change",
    "dream_schedule", "dream_now", "stop_dreaming", "rebuild_helix", "note_improvement",
    "list_self_changes", "show_self_change", "dream_status",
    # The machine, and thinking harder
    "open_program", "media_control", "system_status", "go_to_sleep", "think_harder",
})


@dataclass(frozen=True)
class Pack:
    """One switchable bundle of capability.

    `requires` exists because the packs are not fully independent and pretending otherwise would break
    something quietly. `maker` needs `vision`: check_fit and camera_measure are maker capabilities that
    run on the vision pack's camera panel and tracker. Turning vision off while maker is on would leave
    tools present that cannot work.
    """

    id: str
    label: str
    setting: str            # the settings key that switches it
    default_on: bool
    tools: frozenset[str]
    pages: frozenset[str] = frozenset()      # face pages this pack contributes
    requires: frozenset[str] = frozenset()   # other pack ids this one cannot work without
    note: str = ""


PACKS: tuple[Pack, ...] = (
    Pack(
        id="fleet",
        label="Fleet",
        setting="pack_fleet",
        default_on=True,
        tools=frozenset({"fleet_status", "fleet_history"}),
        pages=frozenset({"strand"}),
        note="The four systems and their environments. Reads in Phase 1; the deploy lane from Phase 2.",
    ),
    Pack(
        id="vision",
        label="Sight",
        setting="pack_vision",
        default_on=True,
        tools=frozenset({
            "view_screen", "view_camera", "annotate_camera", "camera_panel",
            "find_images", "view_image",
        }),
        note="The screen, the webcam, and drawing on the live view.",
    ),
    Pack(
        id="maker",
        label="Maker",
        setting="pack_maker",
        default_on=True,
        tools=frozenset({
            "build_3d_model", "file_hologram", "install_cad_engine", "load_hologram_parts",
            "suggest_components", "design_enclosure", "save_parts", "remove_parts", "show_parts",
            "check_fit", "camera_measure", "project_hologram",
            "print_hologram", "printer_status",
        }),
        pages=frozenset({"studio"}),
        requires=frozenset({"vision"}),
        note="Components, the enclosure generator, holograms, AR fit check, the printer. "
             "check_fit and camera_measure run on the vision pack's camera, hence the dependency.",
    ),
    Pack(
        id="sap",
        label="SAP",
        setting="pack_sap",
        default_on=True,
        tools=frozenset({"sap_lookup", "sap_table", "sap_join", "sap_sql", "sap_edw"}),
        note="The SAP data-model faculty (Brendan, 2026-09): a catalog of what the EDW holds, looked "
             "up instead of recited. Four reads plus sap_edw, which rewrites the catalog and is fenced.",
    ),
    Pack(
        id="purchasing",
        label="Purchasing",
        setting="pack_purchasing",
        default_on=True,
        tools=frozenset({
            "search_amazon", "lookup_amazon", "add_to_cart", "remove_from_cart",
            "stage_parts", "open_cart", "check_amazon_cart", "show_cart",
        }),
        note="Reading Amazon and staging a cart. HELIX never checks out.",
    ),
)

PACK_IDS: frozenset[str] = frozenset(p.id for p in PACKS)


def by_id(pack_id: str) -> Pack | None:
    for pack in PACKS:
        if pack.id == pack_id:
            return pack
    return None


def defaults() -> dict[str, bool]:
    """Settings a fresh install starts with."""
    return {p.setting: p.default_on for p in PACKS}


def enabled(settings_get: Callable[[str, object], object]) -> frozenset[str]:
    """Which pack ids are on, read live from settings.

    `settings_get(key, default)` is the store's own getter, passed in rather than imported — the domain
    does no I/O and must not know what a settings file is.

    A pack whose requirements are off is itself off. Silently leaving it on would present tools that
    cannot work, and a tool that is present but broken is worse than one that is absent.
    """
    on = {p.id for p in PACKS if bool(settings_get(p.setting, p.default_on))}
    # Settle dependencies to a fixed point: dropping maker could in principle drop something that
    # depended on maker, so keep going until nothing more falls out.
    while True:
        dropped = {pid for pid in on
                   if (pack := by_id(pid)) and not pack.requires.issubset(on)}
        if not dropped:
            return frozenset(on)
        on -= dropped


def tools_for(pack_ids: Iterable[str]) -> frozenset[str]:
    """Every tool visible with these packs on. CORE_TOOLS is always included."""
    wanted = set(pack_ids)
    out = set(CORE_TOOLS)
    for pack in PACKS:
        if pack.id in wanted:
            out |= pack.tools
    return frozenset(out)


def pages_for(pack_ids: Iterable[str]) -> frozenset[str]:
    wanted = set(pack_ids)
    out: set[str] = set()
    for pack in PACKS:
        if pack.id in wanted:
            out |= pack.pages
    return frozenset(out)


def pack_of(tool: str) -> str | None:
    """Which pack owns `tool`, or None for a core tool or an unknown one."""
    for pack in PACKS:
        if tool in pack.tools:
            return pack.id
    return None


def unknown_tools(registered: Iterable[str]) -> frozenset[str]:
    """Registered tools that no pack and CORE_TOOLS claims.

    THE FAIL-CLOSED HINGE. A tool nobody classified would otherwise be visible everywhere by accident,
    including on the CLOUD profile. `tests/test_packs.py` asserts this is empty against the live
    registry, so adding a tool and forgetting to place it is a red test rather than a quiet hole.
    """
    claimed = set(CORE_TOOLS)
    for pack in PACKS:
        claimed |= pack.tools
    return frozenset(t for t in registered if t not in claimed)


def double_claimed() -> frozenset[str]:
    """Tools claimed by more than one pack, or by a pack AND core. Must be empty: two owners means two
    answers to 'is this on?', and the packs stop being a partition of the surface."""
    seen: set[str] = set()
    dupes: set[str] = set()
    for pack in PACKS:
        for tool in pack.tools:
            if tool in seen or tool in CORE_TOOLS:
                dupes.add(tool)
            seen.add(tool)
    return frozenset(dupes)
