"""ToolRegistry tests — build_3d_model is exposed and routed through the Forge with the 3D prompt."""
from __future__ import annotations

from helix.services.prompts import build_3d_model_prompt
from helix.services.tools import ToolRegistry


class _App:
    def __init__(self, name: str) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")


class _FakeForge:
    """Records build() calls so we can assert which prompt the tool routed with."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build(self, name, request, *, prompt=None, is_model=None, on_progress=None):
        self.calls.append(
            {"name": name, "request": request, "prompt": prompt, "is_model": is_model}
        )
        return _App(name)


class _FakeBuilds:
    def list(self):
        return []


def _registry() -> tuple[ToolRegistry, _FakeForge]:
    forge = _FakeForge()
    return ToolRegistry(forge, _FakeBuilds()), forge


def test_build_3d_model_is_exposed():
    reg, _ = _registry()
    names = {t.name for t in reg.specs()}
    assert "build_3d_model" in names
    assert "build_app" in names  # the new faculty sits alongside the existing ones


def test_build_3d_model_routes_with_the_model_prompt():
    reg, forge = _registry()
    out = reg.dispatch(
        "build_3d_model", {"name": "Wall Camera Unit", "request": "camera, speaker and mic"}
    )
    assert len(forge.calls) == 1
    call = forge.calls[0]
    assert call["name"] == "Wall Camera Unit"
    # Routed with the 3D-model instruction, not the default app-builder prompt.
    assert call["prompt"] == build_3d_model_prompt("Wall Camera Unit", "camera, speaker and mic")
    assert "3D MODEL" in call["prompt"]
    assert call["is_model"] is True  # so it lands in the Models tab
    assert "Modeled" in out


def test_build_app_still_uses_the_default_prompt():
    reg, forge = _registry()
    reg.dispatch("build_app", {"name": "Tip Calc", "request": "a tip calculator"})
    assert forge.calls[0]["prompt"] is None  # default app-builder path is untouched
    # build_app doesn't pass is_model — the Forge decides (new build -> app; iteration -> preserved).
    assert forge.calls[0]["is_model"] is None
