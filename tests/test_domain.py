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


def test_allowlist_permits_only_services_and_adapters_py():
    assert not C.check(["helix/services/conversation.py"])
    assert not C.check(["helix/adapters/anthropic_chat.py"])
    assert not C.check(["helix/services/newfeature.py"])  # a new editable module is fine


def test_allowlist_refuses_everything_else():
    # shell, domain, ports, app, protected files, startup auto-run files, non-py, novel modules
    for path in [
        "helix/ui/orb.py",
        "helix/domain/models.py",
        "helix/ports/repo.py",
        "helix/app/bootstrap.py",
        "helix/services/selfdev.py",
        "helix/services/forge.py",
        "helix/config.py",
        "helix/logging_setup.py",
        "helix/services/__init__.py",
        "helix/__init__.py",
        "main.py",
        "sitecustomize.py",
        "usercustomize.py",
        "conftest.py",
        "evil.pth",
        "requirements.txt",
        "helix/newmodule.py",
    ]:
        assert C.check([path]), f"should be refused: {path}"


def test_rename_of_protected_is_refused_via_deleted_path():
    # a rename surfaces (with --no-renames) as delete(old) + add(new); the old protected path is caught
    assert C.check([], ["helix/ui/orb.py"])
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
    assert C.is_protected("helix\\domain\\constitution.py")
    assert C.is_shell("helix/ui/anything.py")
    assert not C.is_editable("helix/ui/orb.py")
