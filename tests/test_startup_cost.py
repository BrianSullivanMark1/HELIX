"""Startup cost — what may NOT be imported before HELIX's first frame.

Importing the composition root used to cost ~2.8s, almost all of it two stacks that a launch does not
need: the Anthropic SDK (~1.55s, and a subscription-rail user's turns never touch it) and the mesh
stack behind the hologram baker — trimesh + networkx + scipy.spatial (~955ms, needed only when a MODEL
build finishes). Both are now deferred to the moment they are actually used, which took the import from
~2.8s to ~0.18s.

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
    "trimesh": "the mesh stack — deferred behind container._LazyBaker",
    "networkx": "pulled in by trimesh; same deferral",
    "scipy": "pulled in by trimesh; same deferral",
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
    """Deferring an import is only safe if the deferred path still resolves. Import the two modules
    the hard way — through the seams that do the lazy import — and confirm they load."""
    code = (
        "import helix.app.container as c\n"
        "import sys\n"
        "assert 'trimesh' not in sys.modules\n"
        # the lazy baker resolves ModelBaker on first bake; do the import it would do
        "from helix.services.model_baker import ModelBaker\n"
        "assert 'trimesh' in sys.modules, 'ModelBaker did not bring in trimesh'\n"
        "import anthropic\n"
        "assert hasattr(anthropic, 'Anthropic')\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_lazy_baker_builds_once_and_forwards_bake():
    """_LazyBaker must construct the real baker at most once and pass the workspace straight through —
    a proxy that rebuilt per call would re-pay the import it exists to avoid."""
    from helix.app.container import _LazyBaker

    made: list[int] = []
    baked: list[str] = []

    class _Real:
        def bake(self, workspace):
            baked.append(workspace)
            return "baked"

    def _make():
        made.append(1)
        return _Real()

    proxy = _LazyBaker(_make)
    assert made == [], "constructing the proxy must not construct the baker"
    assert proxy.bake("ws1") == "baked"
    assert proxy.bake("ws2") == "baked"
    assert made == [1], "the real baker must be built exactly once"
    assert baked == ["ws1", "ws2"]
