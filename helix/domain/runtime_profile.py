"""The two profiles — DESKTOP and CLOUD — and the fail-closed allowlist that separates them.

Pure data + pure validators. No I/O, no other HELIX layer.

  DESKTOP  Brian, on his machine. Every pack, self-modification, the dream, files, the shell.
  CLOUD    The team, in a browser. The fleet pack only. No self-modification, no dream, no files,
           no shell, no coder. Read-only until a later phase says otherwise.

ONE CODEBASE, TWO PROFILES. Two codebases drift, and the drift is always in the safety direction you
did not want. One build, one test suite, one Constitution.

THE WHOLE DESIGN IS IN `CLOUD_TOOLS`. It is written out by name: not "everything except", not a prefix
rule, not derived from a pack. A new tool is invisible on CLOUD until a human types its name into that
frozenset. So the failure mode of forgetting is that a feature is MISSING, never that a door is OPEN.

NAMING. `helix/services/profile.py` already exists and is the distilled *user* profile — who is speaking
and what they like. It has nothing to do with this. The module names are what keep the two apart, which
is why this one is `runtime_profile.py` and not `profile.py`.

This file is in PROTECTED_FILES (HELIX_MARK1_PLAN.md §10.1): it decides what a remote user may reach,
so the nightly dream may not edit it.

Contract: HELIX_MARK1_PLAN.md §5.
"""
from __future__ import annotations

import os
from enum import Enum
from typing import Iterable

PROFILE_ENV = "HELIX_PROFILE"


class HelixProfile(str, Enum):
    DESKTOP = "desktop"
    CLOUD = "cloud"


class UnknownProfile(ValueError):
    """Raised for an unrecognised HELIX_PROFILE. Deliberately fatal at startup.

    A typo'd profile that silently fell back to DESKTOP in the cloud is precisely the accident this
    whole split exists to prevent, so it refuses to start rather than starting wrong.
    """


# --------------------------------------------------------------------------------------------------
# The CLOUD allowlist — written out by name, on purpose
# --------------------------------------------------------------------------------------------------

CLOUD_TOOLS: frozenset[str] = frozenset({
    # Phase 1: the fleet, read-only. That is the entire cloud surface today.
    "fleet_status",
    "fleet_history",
})

# Services the CLOUD container must NOT CONSTRUCT — not "construct and hide". An object that was never
# built cannot be called by a bug, a stray route, or a tool name that slipped an allowlist.
CLOUD_UNBUILT: frozenset[str] = frozenset({
    "selfdev",     # self-modification
    "dream",       # the nightly session
    "coder",       # the thing that writes code
    "growth_coder",
    "files",       # the user's disk
    "desktop",     # launching programs, pressing keys
    "camera",
    "maker",
    "shopping",
    "gmail",
    "calendar",
})


def parse(value: str | None) -> HelixProfile:
    """Turn a raw setting into a profile. Empty or missing means DESKTOP; anything unrecognised raises.

    DESKTOP is the default because that is the overwhelmingly common case and the one where a human is
    sitting in front of it. CLOUD is never reached by accident — it must be asked for by name.
    """
    text = (value or "").strip().lower()
    if not text:
        return HelixProfile.DESKTOP
    for profile in HelixProfile:
        if text == profile.value:
            return profile
    known = ", ".join(p.value for p in HelixProfile)
    raise UnknownProfile(f"{PROFILE_ENV}={value!r} is not a profile. Use one of: {known}.")


def from_env(environ: dict[str, str] | None = None) -> HelixProfile:
    """The profile this process is running as, from the environment ONLY.

    Deliberately not from settings (a settings file is writable by anything with disk access) and never
    from a request header (trivially forged). A CLOUD deployment sets HELIX_PROFILE=cloud in its Cloud
    Run service definition, and nothing inside the process can change it afterwards.
    """
    env = os.environ if environ is None else environ
    return parse(env.get(PROFILE_ENV))


def allowed_tools(profile: HelixProfile, pack_tools: Iterable[str]) -> frozenset[str]:
    """The tools this profile may see, given what the packs already allow.

    An INTERSECTION, never a union. A pack cannot widen CLOUD, and CLOUD cannot reach a tool its packs
    have switched off. Both have to say yes.
    """
    tools = frozenset(pack_tools)
    if profile is HelixProfile.DESKTOP:
        return tools
    return CLOUD_TOOLS & tools


def may_self_modify(profile: HelixProfile) -> bool:
    return profile is HelixProfile.DESKTOP


def may_dream(profile: HelixProfile) -> bool:
    return profile is HelixProfile.DESKTOP


def may_reach_production(profile: HelixProfile) -> bool:
    """§10.2 condition 1 of four. Being on DESKTOP is necessary and nowhere near sufficient — a live
    identity on the allowlist, a typed confirmation, and an audit row written first are the other
    three, and none of them is derivable from this one."""
    return profile is HelixProfile.DESKTOP


def must_not_build(profile: HelixProfile) -> frozenset[str]:
    """Which container attributes must be None for this profile. Pinned by a test so the CLOUD
    container cannot quietly acquire a coder."""
    return CLOUD_UNBUILT if profile is HelixProfile.CLOUD else frozenset()
