"""The fleet's files are immutable to autonomous self-modification (HELIX_MARK1_PLAN.md §10.1).

`helix/domain/` and `helix/services/` are the Constitution's growable surface, so WITHOUT the six
entries this file pins, the nightly dream may rewrite the module that says "production is human-only",
the flags that have previously cost a service its database, and the allowlist of what a CLOUD user can
reach. A rule an autonomous process can edit is not a rule.

If a test here fails, someone has removed those entries. That is not a refactor; it is a change to who
is in control of production, and it should be treated as one.
"""
from __future__ import annotations

import pytest

from helix.domain import constitution

# The exact six. Listed literally rather than derived, so a typo in the Constitution (a path that never
# matches anything) fails loudly instead of silently protecting nothing.
FLEET_PROTECTED = (
    "helix/domain/fleet.py",
    "helix/domain/packs.py",
    "helix/domain/runtime_profile.py",
    "helix/services/fleet.py",
    "helix/adapters/github_fleet.py",
    "helix/adapters/rest_fleet.py",
)


@pytest.mark.parametrize("path", FLEET_PROTECTED)
def test_each_fleet_file_is_protected(path):
    assert constitution.is_protected(path), f"{path} must be immutable to self-modification"


@pytest.mark.parametrize("path", FLEET_PROTECTED)
def test_no_fleet_file_is_on_the_growable_surface(path):
    assert not constitution.is_editable(path)


def test_a_self_change_touching_any_of_them_is_refused():
    problems = constitution.check(list(FLEET_PROTECTED))
    assert len(problems) == len(FLEET_PROTECTED)
    for path in FLEET_PROTECTED:
        assert any(path in p for p in problems), f"the refusal names {path}"


def test_deleting_one_is_refused_too():
    """`check` takes changed AND deleted paths, and renames arrive decomposed into add+delete. A rule
    you cannot edit but can delete is not protected."""
    assert constitution.check([], ["helix/domain/fleet.py"])


def test_they_are_named_in_protected_files_not_merely_matched_by_a_prefix():
    """These live under EDITABLE_PREFIXES, so a prefix rule can never cover them - they have to be
    named individually. This test is what notices if someone 'tidies' them out of PROTECTED_FILES and
    assumes a prefix picks them up."""
    named = {p.replace("\\", "/") for p in constitution.PROTECTED_FILES}
    for path in FLEET_PROTECTED:
        assert path in named


def test_the_rest_of_helix_is_still_growable():
    """The point of the Constitution is a NARROW protected core. Protecting the fleet must not have
    quietly frozen the parts of HELIX that are supposed to keep growing."""
    for path in ("helix/services/tools.py", "helix/services/conversation.py",
                 "helix/domain/components.py", "helix/domain/vocabulary.py",
                 "helix/ui/console_view.py", "helix/adapters/speech.py",
                 "tests/test_fleet_laws.py"):
        assert constitution.is_editable(path), f"{path} should still be editable"


def test_the_original_vital_organs_are_untouched():
    """Adding to the list must not have removed from it."""
    for path in ("helix/domain/constitution.py", "helix/services/selfdev.py",
                 "helix/services/sandbox.py", "helix/adapters/git_repo.py",
                 "helix/config.py", "main.py", "helix/services/forge.py",
                 "helix/services/connections.py", "helix/services/files.py",
                 "helix/services/prompts.py"):
        assert constitution.is_protected(path)


def test_human_approval_is_still_locked():
    assert constitution.LOCKED_SETTINGS["human_approval_required"] is True
    assert constitution.locked_setting_violation("human_approval_required", False)


def test_the_skeleton_is_still_the_skeleton():
    assert constitution.is_protected("helix/ports/fleet.py"), "the fleet CONTRACT is protected by prefix"
    assert constitution.is_protected("helix/app/container.py"), "so is the composition root"


def test_fingerprint_is_stable_across_calls():
    """It changed once when the six lines were added - that trips the tamper wire deliberately, and is
    re-stamped by hand. What must NOT happen is it changing on its own between two calls."""
    assert constitution.fingerprint() == constitution.fingerprint()
