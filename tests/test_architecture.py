"""The dependency rule, enforced.

READ_ME/ARCHITECTURE.md states the contract — `ui -> services -> ports <- adapters`, everything may
depend on `domain`, and `domain` depends on nothing — but until now only the Constitution's allowlist was
pinned by a test. A layering violation is the kind of thing that arrives one import at a time and is
invisible in review, so it is checked mechanically here.

Known violations are listed in ALLOWED_VIOLATIONS rather than hidden by loosening the rule. The point of
the allowlist is that it can only shrink: a NEW crossed import fails immediately, and the existing ones
stay visible and countable instead of being quietly absorbed into "how the code is". Each entry says
what it would take to remove it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

HELIX = Path(__file__).resolve().parent.parent / "helix"

# (importing module, imported module) pairs that break the rule and are accepted for now.
ALLOWED_VIOLATIONS: dict[tuple[str, str], str] = {
    ("helix.ui.settings_view", "helix.adapters.speech"):
        "reads DEFAULT_TTS_VOICE/TTS_VOICES/edge_available for the voice picker; needs a port or a "
        "service to own the voice catalogue before the view can stop reaching into the adapter",
    ("helix.services.gmail", "helix.adapters.gmail_imap"):
        "the service imports its adapter directly instead of depending on a port",
    ("helix.services.calendar", "helix.adapters.ical_http"):
        "as above, for the calendar adapter",
}


def _modules() -> list[tuple[str, Path]]:
    out = []
    for p in sorted(HELIX.rglob("*.py")):
        rel = p.relative_to(HELIX.parent).with_suffix("")
        out.append((".".join(rel.parts), p))
    return out


def _imports(path: Path) -> set[str]:
    """Every module this file imports, INCLUDING inside functions — a deferred import still couples the
    two modules; it only moves when the cost is paid."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _layer(module: str) -> str | None:
    parts = module.split(".")
    return parts[1] if len(parts) > 2 and parts[0] == "helix" else None


def _violations(rule) -> list[str]:
    """rule(importer, imported) -> True when the pair is illegal."""
    bad = []
    for module, path in _modules():
        for imported in _imports(path):
            if not imported.startswith("helix."):
                continue
            if rule(module, imported) and (module, imported) not in ALLOWED_VIOLATIONS:
                bad.append(f"{module} imports {imported}")
    return bad


def test_domain_imports_no_other_helix_layer():
    """The domain is pure: it is the one layer every other layer may depend on, so it must depend on
    nothing. A single import here inverts the whole dependency graph."""
    bad = _violations(lambda m, i: _layer(m) == "domain" and _layer(i) not in (None, "domain"))
    assert not bad, "domain is no longer pure:\n  " + "\n  ".join(bad)


def test_domain_imports_no_qt_and_no_io():
    """Qt and anything that talks to the outside world. Note what is NOT banned: `urllib.parse` is pure
    string manipulation and is legitimately how the domain recognises a URL-shaped credential — it is
    `urllib.request` that would be I/O."""
    banned_top = {"PyQt6", "sqlite3", "subprocess", "socket", "requests", "httpx", "shutil"}
    banned_exact = {"urllib.request", "urllib.error", "http.client", "smtplib", "imaplib"}
    bad = []
    for module, path in _modules():
        if _layer(module) != "domain":
            continue
        for imported in _imports(path):
            if imported.split(".")[0] in banned_top or imported in banned_exact:
                bad.append(f"{module} imports {imported}")
    assert not bad, "the domain must stay free of Qt and I/O:\n  " + "\n  ".join(bad)


def test_services_do_not_import_qt():
    """Business logic must be runnable — and testable — without a GUI toolkit."""
    bad = []
    for module, path in _modules():
        if _layer(module) != "services":
            continue
        if any(i.split(".")[0] == "PyQt6" for i in _imports(path)):
            bad.append(module)
    assert not bad, "services must not depend on Qt: " + ", ".join(bad)


def test_ui_does_not_reach_into_adapters():
    """Views talk to services; which adapter implements which port is decided once, in the container."""
    bad = _violations(lambda m, i: _layer(m) == "ui" and _layer(i) == "adapters")
    assert not bad, (
        "ui/ reached into adapters/ (add a service or port instead, or record it in "
        "ALLOWED_VIOLATIONS with what it would take to remove):\n  " + "\n  ".join(bad)
    )


def test_adapters_do_not_import_services():
    """Adapters sit at the edge implementing ports; depending inward on use-cases inverts the rule."""
    bad = _violations(lambda m, i: _layer(m) == "adapters" and _layer(i) == "services")
    assert not bad, "adapters must not depend on services:\n  " + "\n  ".join(bad)


def test_ports_are_only_protocols():
    """A port with behaviour is no longer a seam. Protocols, type aliases and dataclass payloads only."""
    offenders = []
    for module, path in _modules():
        if _layer(module) != "ports":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                offenders.append(f"{module}.{node.name} (module-level function)")
    assert not offenders, "ports/ must declare contracts, not implement them:\n  " + "\n  ".join(offenders)


def test_the_allowlist_only_shrinks():
    """Every entry must still be a real violation. When one is fixed, its entry has to be deleted — an
    allowlist that outlives its violations stops meaning anything."""
    stale = []
    by_module = {m: p for m, p in _modules()}
    for importer, imported in ALLOWED_VIOLATIONS:
        path = by_module.get(importer)
        if path is None:
            stale.append(f"{importer} no longer exists")
        elif imported not in _imports(path):
            stale.append(f"{importer} no longer imports {imported} — delete this entry")
    assert not stale, "ALLOWED_VIOLATIONS is out of date:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize("layer", ["domain", "ports", "adapters", "services", "ui", "app"])
def test_every_layer_exists(layer):
    """Guards the test itself: if a layer is renamed, the rules above would silently pass on nothing."""
    assert (HELIX / layer).is_dir(), f"helix/{layer}/ is gone — the layering rules no longer apply"
