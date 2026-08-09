"""The composition root actually composes.

Nothing else in the suite constructs `Container`. Every other test builds the one or two services it
needs by hand, so a wiring break — a renamed service, a constructor argument that drifted, an adapter
that moved — is invisible to the whole suite and only shows up when Brian launches the app. This builds
the real thing against a throwaway data dir.

It also guards two properties that are otherwise only provable by launching: that wiring stays cheap
(the heavy import stacks must not be dragged in while composing), and that the "which Claude rail is
live" diagnostic reports the actual missing piece rather than blaming a credential.
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

import helix.config as config  # noqa: E402

# Every service the UI and the tool registry reach for off the container.
EXPECTED_SERVICES = (
    "paths", "settings", "store", "repo", "clock", "bus", "subscription", "chat", "coder",
    "growth_coder", "builds", "secrets", "model_baker", "forge", "build_queue", "selfdev",
    "selfdev_lane", "connections", "knowledge", "files", "user_memory", "tools", "profile",
    "lessons", "evolve", "conversation", "agents", "scheduler", "workflows", "speech_in",
    "speech_out", "voice_id",
)


@pytest.fixture(scope="module")
def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def container(_app, tmp_path, monkeypatch):
    """The real Container, pointed at a temp data dir so the user's own data is never touched."""
    root = config.AppPaths.resolve().root      # the repo itself — read-only for our purposes
    monkeypatch.setattr(
        config.AppPaths, "resolve",
        staticmethod(lambda: config.AppPaths(root=root, data=tmp_path)),
    )
    from helix.app.container import Container

    return Container()


def test_container_composes_every_service(container):
    missing = [name for name in EXPECTED_SERVICES if not hasattr(container, name)]
    assert not missing, f"container no longer wires: {missing}"


def test_container_exposes_a_tool_surface(container):
    specs = container.tools.specs()
    assert len(specs) > 20, f"only {len(specs)} tools registered — the registry lost most of its surface"
    names = {s.name for s in specs}
    assert len(names) == len(specs), "duplicate tool names would make dispatch ambiguous"


def test_composing_does_not_drag_in_the_heavy_stacks(tmp_path):
    """The deferral in tests/test_startup_cost.py is about IMPORTING the container module; this is about
    CONSTRUCTING it. Moving an import is useless if wiring still builds the object that needs it — which
    is why the hologram baker is constructed lazily behind a proxy.

    Runs in a FRESH interpreter on purpose: tests/test_model_baker.py imports trimesh quite legitimately,
    so asserting on this process's sys.modules would pass alone and fail in a full run — an
    order-dependent test, which is worse than none."""
    import subprocess

    script = (
        "import os, sys\n"
        "os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')\n"
        "from pathlib import Path\n"
        "import helix.config as config\n"
        "root = config.AppPaths.resolve().root\n"
        f"data = Path(r'{tmp_path}')\n"
        "config.AppPaths.resolve = staticmethod(lambda: config.AppPaths(root=root, data=data))\n"
        "from PyQt6.QtWidgets import QApplication\n"
        "QApplication.instance() or QApplication([])\n"
        "from helix.app.container import Container\n"
        "Container()\n"
        "print(','.join(m for m in ('trimesh', 'networkx', 'anthropic') if m in sys.modules) or 'clean')\n"
    )
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"constructing the container failed:\n{proc.stderr[-3000:]}"
    verdict = proc.stdout.strip().splitlines()[-1]
    assert verdict == "clean", (
        f"constructing the container pulled in {verdict} — a lazy seam (the baker proxy, or the "
        f"deferred anthropic import) is no longer lazy"
    )


def test_rail_diagnostic_names_the_missing_piece_not_the_credential(container):
    """A fresh data dir has no token and no API key. The diagnostic must say which, in words, because
    the failure it replaced told users to check a token that was often perfectly good."""
    assert container.subscription.active(allow_probe=False) is False
    reason = container.subscription.why_inactive(allow_probe=False)
    assert reason, "why_inactive() went quiet about an unusable rail"
    assert "setup-token" in reason, f"expected the missing token to be named, got: {reason}"
