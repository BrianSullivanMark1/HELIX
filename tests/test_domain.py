"""Pure-domain tests — fast, no I/O. Locks in slugify, models, and the Constitution allowlist."""
from __future__ import annotations

from helix.domain import constitution as C
from helix.domain.models import App, AppKind, slugify


def test_slugify():
    assert slugify("Tip Calculator!!") == "tip-calculator"
    assert slugify("   ") == "app"
    assert slugify("café/niño") == "caf-ni-o"


def test_slugify_non_ascii_names_get_unique_hash_slugs():
    # Emoji/CJK-only names used to all collapse to 'app' and overwrite each other; now each is unique.
    a, b = slugify("🎮"), slugify("日本語")
    assert a != b and a != "app" and b != "app"
    assert a.startswith("build-") and slugify("🎮") == a  # stable for the same name


def test_slugify_caps_length():
    assert len(slugify("x" * 300)) <= 80


def test_app_from_request():
    a = App.from_request("My App", "do things")
    assert a.slug == "my-app"
    assert a.name == "My App"
    assert a.kind == AppKind.UNKNOWN


def test_growable_surface_permits_brain_interface_and_tests():
    # The broad growable surface: cognition, hands, interface, brain structures, and its own tests.
    for path in [
        "helix/services/conversation.py",
        "helix/adapters/anthropic_chat.py",
        "helix/services/newfeature.py",     # a new editable module is fine
        "helix/ui/orb.py",                  # the interface grows
        "helix/ui/console_view.py",
        "helix/domain/models.py",           # brain structures grow
        "helix/domain/brain.py",
        "helix/domain/vocabulary.py",
        "tests/test_conversation.py",       # HELIX writes tests for its changes
        "tests/test_a_new_thing.py",
    ]:
        assert not C.check([path]), f"should be editable: {path}"


def test_vital_organs_and_skeleton_refused():
    # The inviolable core (keeps the human in control + recovery) and the skeleton (ports/app).
    for path in [
        "helix/domain/constitution.py",     # the laws
        "helix/services/selfdev.py",        # the approval gate
        "helix/services/sandbox.py",        # containment primitives
        "helix/adapters/git_repo.py",       # git executor
        "helix/services/forge.py",          # build escape guard
        "helix/services/connections.py",    # egress lockdown
        "helix/services/files.py",          # filesystem seal
        "helix/services/desktop.py",        # desktop-control fence
        "helix/services/prompts.py",        # coder framing + fences
        "helix/adapters/agent_sdk_chat.py", # token isolation
        "helix/ports/repo.py",              # skeleton: contract
        "helix/app/bootstrap.py",           # skeleton: recovery anchor
        "helix/app/container.py",           # skeleton: composition root
        "helix/config.py",                  # startup path resolution
        "helix/logging_setup.py",
        "helix/services/__init__.py",       # package init (runs at import)
        "helix/__init__.py",
        "main.py",
        "sitecustomize.py",
        "usercustomize.py",
        "conftest.py",                      # root-level, outside tests/
        "evil.pth",
        "requirements.txt",
        "helix/newmodule.py",               # not under a growable prefix (helix/ root)
    ]:
        assert C.check([path]), f"should be refused: {path}"


def test_rename_of_a_vital_organ_is_refused_via_deleted_path():
    # a rename surfaces (with --no-renames) as delete(old) + add(new); the old protected path is caught
    assert C.check([], ["helix/services/selfdev.py"])
    assert C.check([], ["helix/domain/constitution.py"])


def test_locked_setting():
    assert C.locked_setting_violation("human_approval_required", False)
    assert C.locked_setting_violation("human_approval_required", True) is None
    assert C.locked_setting_violation("unrelated", "anything") is None


def test_fingerprint_stable_and_covers_enforcement_source():
    assert C.fingerprint() == C.fingerprint()
    assert len(C.fingerprint()) == 64
    assert len(C._enforcement_source_hash()) == 64


def test_path_normalization_case_and_separators():
    assert C.is_protected("helix\\domain\\constitution.py")   # a vital organ, by either separator
    assert C.is_editable("helix\\ui\\orb.py")                 # the interface is now growable
    assert not C.is_shell("helix/ui/anything.py")             # no blanket-immutable shell anymore
    assert not C.is_editable("helix/domain/constitution.py")  # the laws stay fixed
