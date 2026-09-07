"""DesktopService — open installed programs by name, media keys, one spoken machine-status line."""
from __future__ import annotations

from pathlib import Path

from helix.services.desktop import _MEDIA_VKS, _VOLUME_TAPS, DesktopService


def _menu(tmp_path: Path, *names: str) -> list[Path]:
    root = tmp_path / "Start Menu" / "Programs"
    root.mkdir(parents=True)
    for n in names:
        (root / n).parent.mkdir(parents=True, exist_ok=True)
        (root / n).write_bytes(b"lnk")
    return [root]


def _svc(tmp_path, *names, launched=None, taps=None, platform="win32"):
    return DesktopService(
        launcher=(launched.append if launched is not None else lambda p: None),
        key_tap=(taps.append if taps is not None else lambda vk: None),
        start_menu_dirs=_menu(tmp_path, *names) if names else [],
        platform=platform,
    )


def test_open_program_prefers_the_exact_shortcut(tmp_path):
    launched: list[str] = []
    s = _svc(tmp_path, "Excel.lnk", "Excel Viewer.lnk", "Office/Excel Tools.lnk", launched=launched)
    msg = s.open_program("excel")
    assert launched and launched[0].endswith("Excel.lnk")
    assert "Opening Excel" in msg


def test_open_program_falls_back_to_a_starting_match(tmp_path):
    launched: list[str] = []
    s = _svc(tmp_path, "Google Chrome.lnk", launched=launched)
    assert "Opening Google Chrome" in s.open_program("google")
    assert launched[0].endswith("Google Chrome.lnk")


def test_open_program_refuses_paths_and_empty_names(tmp_path):
    launched: list[str] = []
    s = _svc(tmp_path, "Excel.lnk", launched=launched)
    assert "not paths" in s.open_program(r"C:\evil\thing.exe")
    assert "not paths" in s.open_program("../escape")
    assert "Which program" in s.open_program("  ")
    assert launched == []


def test_open_program_reports_a_missing_program(tmp_path, monkeypatch):
    import helix.services.desktop as desktop_mod

    monkeypatch.setattr(desktop_mod.shutil, "which", lambda name: None)
    s = _svc(tmp_path, launched=[])
    assert "couldn't find" in s.open_program("nonexistent thing")


def test_open_program_is_windows_only(tmp_path):
    s = _svc(tmp_path, "Excel.lnk", platform="darwin")
    assert "only supported on Windows" in s.open_program("excel")


def test_media_taps_the_right_key_and_volume_nudges_several_steps(tmp_path):
    taps: list[int] = []
    s = _svc(tmp_path, taps=taps)
    assert "playback" in s.media("play_pause").lower()
    assert taps == [_MEDIA_VKS["play_pause"]]
    taps.clear()
    assert "volume up" in s.media("volume up").lower()
    assert taps == [_MEDIA_VKS["volume_up"]] * _VOLUME_TAPS


def test_media_rejects_unknown_actions_and_never_crashes_on_a_blocked_key(tmp_path):
    s = _svc(tmp_path, taps=[])
    assert "don't know" in s.media("self destruct")

    def _blocked(vk):
        raise OSError("blocked")

    s2 = DesktopService(key_tap=_blocked, start_menu_dirs=[], platform="win32")
    assert "couldn't send" in s2.media("mute")


def test_system_status_reads_like_a_sentence(tmp_path):
    s = _svc(tmp_path, platform="linux")  # off-Windows: cores + disk still report
    line = s.system_status()
    assert line.startswith("The machine:") and line.endswith(".")
    assert "cores" in line


def test_registry_exposes_and_dispatches_the_desktop_tools(tmp_path):
    from helix.services.conversation import BUILD_TOOLS
    from helix.services.tools import ToolRegistry

    launched: list[str] = []
    taps: list[int] = []
    svc = _svc(tmp_path, "Excel.lnk", launched=launched, taps=taps)
    reg = ToolRegistry(forge=None, builds=None, desktop=svc)
    names = {s.name for s in reg.specs()}
    assert {"open_program", "media_control", "system_status"} <= names
    assert "Opening Excel" in reg.dispatch("open_program", {"name": "excel"})
    assert "track" in reg.dispatch("media_control", {"action": "next"}).lower()
    assert reg.dispatch("system_status", {}).startswith("The machine:")
    # Launch + keys stay human-driven; the status line stays readable to agents.
    assert "open_program" in BUILD_TOOLS and "media_control" in BUILD_TOOLS
    assert "system_status" not in BUILD_TOOLS


def test_registry_without_a_desktop_service_hides_the_tools():
    from helix.services.tools import ToolRegistry

    names = {s.name for s in ToolRegistry(forge=None, builds=None).specs()}
    assert not ({"open_program", "media_control", "system_status"} & names)
