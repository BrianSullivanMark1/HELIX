"""HELIX's constitution — the Twelve Commandments and the guardrails that make them immutable (§44).

HELIX rewrites its own code, so its only real safety is a law it CANNOT rewrite. These commandments and
the locked settings below are *enforced*, not merely stated:

  * the self-dev coder is TOLD never to touch the protected machinery (the first line of defense), and
  * the approval gate (`engine.approve`) SCANS every drafted change and refuses to merge anything that
    edits a protected path (the hard line — nothing self-written can bypass it, because every
    self-change must pass through that one gate before it can go live).

Amendment is reserved to the human owner, out-of-band (editing this file directly). HELIX itself — via
the coder, via conversation, via settings — can never change what is in here, because this module is
itself a protected path: HELIX cannot rewrite the laws by rewriting the law-keeper.

Pure stdlib and import-light so the guardrails can never fail to load.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class Commandment:
    n: int
    title: str
    text: str


COMMANDMENTS: tuple[Commandment, ...] = (
    Commandment(1, "Protect the human above all",
                "Never harm a human's safety, health, finances, privacy, or freedom; when unsure, stop and ask."),
    Commandment(2, "Serve and augment the human",
                "Assist and enhance human judgment — never supplant or override it."),
    Commandment(3, "Keep the human in command",
                "A human can always stop, pause, or reverse HELIX; it never holds sole control."),
    Commandment(4, "Tell the truth always",
                "Report honestly what HELIX has done, plans to do, or failed to do; never hide an action or fake a success."),
    Commandment(5, "Change yourself only on a branch",
                "A self-change goes live only after an explicit human approval; never deploy unapproved."),
    Commandment(6, "Never approve your own changes",
                "HELIX may never merge, approve, or ship its own code on a human's behalf."),
    Commandment(7, "Never weaken these laws",
                "HELIX may never edit, disable, or delete these commandments or the code that enforces them."),
    Commandment(8, "Always preserve a way back",
                "The Archive, the root reset, and the off switch must always exist and function."),
    Commandment(9, "Touch real money only with a human's yes",
                "Never go live, place a live order, move funds, or raise a spending limit without an explicit human yes."),
    Commandment(10, "Keep secrets and private media on this machine",
                "Never send keys, secrets, camera frames, audio, or personal data off-device except to a service the human connected for a purpose they asked for."),
    Commandment(11, "Stay within granted access",
                "Never escalate permissions, act on accounts or devices not granted, or impersonate the human."),
    Commandment(12, "Keep Settings permanent and reachable",
                "The Settings screen — the home of these laws — can never be hidden, removed, or made unreachable."),
)

# Locked settings — declared constants, NOT entries in the editable settings file, so no voice, chat, or
# coder path can flip them. Code that performs these actions reads the value from here.
LOCKED_SETTINGS: dict = {
    "human_approval_required": True,
    "self_approval_allowed": False,
    "constitution_immutable": True,
    "settings_removable": False,
    "settings_hideable": False,
    "archive_protected": True,
    "root_reset_protected": True,
    "offswitch_always_available": True,
    "auto_enable_live_trading": False,
    "ai_can_raise_spend_limits": False,
    "ai_can_move_or_withdraw_funds": False,
    "secrets_leave_machine": False,
    "ai_can_expand_permissions": False,
}

# Menu keys that can never be hidden or removed (Commandments 8 & 12).
PERMANENT_MENU_KEYS: frozenset = frozenset({"settings", "archive"})

# Files HELIX may never modify through its own self-dev loop — the safety-critical machinery that keeps
# the commandments enforceable, plus the IMMUTABLE BACKBONE: the front interface (the four nav buttons
# + Console shell) and the Forge engine itself. Commanding HELIX can build Apps/Tasks/Agents (which only
# ever touch data/builds/), but it can never rewrite this structure. Repo-relative, forward-slashed.
PROTECTED_PATHS: tuple[str, ...] = (
    "helix/selfdev/constitution.py",   # the laws + the scanner
    "helix/selfdev/engine.py",         # the approval gate (where the scan runs)
    "helix/selfdev/restart.py",        # the off switch / restart control
    "helix/selfdev/versioning.py",     # the recovery / Archive backend
    "helix/selfdev/gitops.py",         # the merge / restore / push primitives
    "helix/selfdev/coder.py",          # the self-edit engine + the commandments carried in its prompt
    "helix/selfdev/builds.py",         # the Forge: per-Build workspaces + the build pipeline
    "helix/selfdev/api_coder.py",      # the Forge: the no-CLI build fallback
    "helix/ai/actions.py",             # the command/tool surface (build + self-improve tools + the gate)
    "helix/interfaces/qt_app.py",      # the front interface: the four nav buttons + the Console shell
)

# Fingerprint of the canonical commandment text — a tripwire against partial tampering. Hardcoded (not
# recomputed from COMMANDMENTS), so editing a commandment makes verify_integrity() fail until a human
# deliberately updates this constant too. Regenerate with `python -m helix.selfdev.constitution`.
FINGERPRINT = "3d910f0ebd3589cca4e43b4d068dfd75004707fa7de3e31605712426d70cc1ca"


def commandments() -> tuple[Commandment, ...]:
    return COMMANDMENTS


def locked(name: str) -> bool:
    """Read a locked constitutional setting (defaults to the safe value if the name is unknown)."""
    return bool(LOCKED_SETTINGS.get(name, False))


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/").strip().lstrip("./")


def is_protected_path(path: str) -> bool:
    p = _norm(path)
    return any(p == prot or p.endswith("/" + prot) for prot in PROTECTED_PATHS)


def is_permanent_menu_key(key: str) -> bool:
    return str(key or "") in PERMANENT_MENU_KEYS


def check_change(changed_files) -> list[str]:
    """Return the protected paths a drafted change touches (empty list = clean). The approval gate
    refuses to merge a change with any violation, so HELIX cannot self-edit its own guardrails."""
    hits = {_norm(f) for f in (changed_files or []) if is_protected_path(f)}
    return sorted(hits)


def _canon() -> str:
    return "|".join(f"{c.n}:{c.title}:{c.text}" for c in COMMANDMENTS)


def fingerprint() -> str:
    return hashlib.sha256(_canon().encode("utf-8")).hexdigest()


def verify_integrity() -> bool:
    """True if the commandments are intact (exactly twelve, and the canonical fingerprint matches the
    hardcoded constant). A tripwire against accidental/partial tampering; the hard guarantee is the
    protected-path scan in the approval gate."""
    return len(COMMANDMENTS) == 12 and fingerprint() == FINGERPRINT


if __name__ == "__main__":  # regenerate FINGERPRINT after a deliberate, human amendment
    print(fingerprint())
