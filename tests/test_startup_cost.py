"""Startup cost — what may NOT be imported before HELIX's first frame.

Importing the composition root used to cost ~2.8s, almost all of it two stacks that a launch does not
need: the Anthropic SDK (~1.55s, and a subscription-rail user's turns never touch it) and the mesh
stack behind the old hologram baker — trimesh + networkx + scipy.spatial (~955ms). The SDK is now
deferred to the moment it is used, and the mesh stack is GONE from the hologram path altogether (a
hologram is compiled by the OpenSCAD engine behind the CadEngine port; the baker imports nothing heavy),
which took the import from ~2.8s to ~0.18s. The mesh stack stays forbidden so it cannot creep back in
through a new import — the cost would be invisible until the next launch.

Nothing in the type system stops someone re-adding `import anthropic` at module scope, and the cost is
invisible in a unit test (it is paid once, by the user, before anything appears on screen). So it is
pinned here instead. Each check runs in a FRESH interpreter: by the time this test executes, the suite
has almost certainly imported both stacks already, so sys.modules in-process proves nothing.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

# Modules that must not be pulled in by importing the composition root, and why.
FORBIDDEN_AT_STARTUP = {
    "anthropic": "the Anthropic SDK — deferred to AnthropicChat._client_for_current_key",
    "trimesh": "the retired primitive engine's mesh stack — holograms compile through the CadEngine now",
    "networkx": "pulled in by trimesh; nothing on the hologram path needs it",
    "scipy": "the retired procedural-texture module's only import; nothing in helix/ uses it now",
}


def _modules_after_importing(target: str) -> set[str]:
    """Top-level module names present in a fresh interpreter after importing `target`."""
    code = (
        "import sys\n"
        f"import {target}\n"
        "print('\\n'.join(sorted({m.split('.')[0] for m in sys.modules})))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"importing {target} failed:\n{proc.stderr}"
    return set(proc.stdout.split())


@pytest.mark.parametrize("target", ["helix.app.container", "helix.app.bootstrap"])
def test_startup_does_not_import_the_heavy_stacks(target):
    loaded = _modules_after_importing(target)
    offenders = {m: why for m, why in FORBIDDEN_AT_STARTUP.items() if m in loaded}
    assert not offenders, (
        f"importing {target} now pulls in {sorted(offenders)} again, putting that cost back between "
        f"launch and the first frame. Reasons these are deferred:\n"
        + "\n".join(f"  - {m}: {why}" for m, why in sorted(offenders.items()))
    )


def test_the_deferred_imports_still_actually_work():
    """Deferring an import is only safe if the deferred path still resolves. Import the deferred modules
    the hard way — through the seams that do the lazy import — and confirm they load, and that the
    baker the proxy builds on first use stays LIGHT (the OpenSCAD engine does the geometry; a baker that
    grew a mesh-stack import again would put ~1s back in front of every hologram build)."""
    code = (
        "import helix.app.container as c\n"
        "import sys\n"
        "assert 'trimesh' not in sys.modules\n"
        # the lazy baker resolves ModelBaker on first check/bake; do the import it would do
        "from helix.services.model_baker import ModelBaker\n"
        "assert 'trimesh' not in sys.modules, 'ModelBaker dragged the retired mesh stack back in'\n"
        "assert 'scipy' not in sys.modules, 'ModelBaker dragged scipy back in'\n"
        "import anthropic\n"
        "assert hasattr(anthropic, 'Anthropic')\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_lazy_baker_builds_once_and_forwards_every_forge_call():
    """_LazyBaker must construct the real baker at most once and pass every call the Forge makes
    straight through — check() (the pre-finalize gate, which is where a hologram build FIRST reaches the
    baker now), bake(), and engine_missing(). A proxy that rebuilt per call would re-pay the import it
    exists to avoid; a proxy that forwarded only bake() would make the Forge's check() raise
    AttributeError on every hologram build while the baker's own tests stay green — the half-wired shape
    this suite exists to catch."""
    from helix.app.container import _LazyBaker

    made: list[int] = []
    calls: list[tuple] = []

    class _Real:
        def check(self, workspace):
            calls.append(("check", workspace))
            return "a problem"

        def bake(self, workspace):
            calls.append(("bake", workspace))
            return "baked"

        def engine_missing(self):
            calls.append(("engine_missing",))
            return False

    def _make():
        made.append(1)
        return _Real()

    proxy = _LazyBaker(_make)
    assert made == [], "constructing the proxy must not construct the baker"
    assert proxy.check("ws1") == "a problem"
    assert proxy.bake("ws1") == "baked"
    assert proxy.bake("ws2") == "baked"
    assert proxy.engine_missing() is False
    assert made == [1], "the real baker must be built exactly once"
    assert calls == [("check", "ws1"), ("bake", "ws1"), ("bake", "ws2"), ("engine_missing",)]
