"""The print pipeline's tool layer — routing, fallbacks, and honesty, all against fakes.

The real printer needs a machine on the LAN with an access code; everything decidable WITHOUT one
is pinned here: the connect-panel flow when unconfigured, name resolution, the slice-and-start
happy path, the Studio fallback when headless slicing declines, and the status wording.
"""
from __future__ import annotations

from pathlib import Path

from helix.adapters import bambu_printer as bp
from helix.domain.events import ConnectRequested
from helix.domain.models import BuildKind
from helix.services.tools import ToolRegistry


class _Build:
    def __init__(self, name: str, kind=BuildKind.MODEL) -> None:
        self.name = name
        self.slug = name.lower().replace(" ", "-")
        self.build_kind = kind

    @property
    def is_model(self):
        return self.build_kind == BuildKind.MODEL


class _Builds:
    def __init__(self, root: Path, *builds: _Build) -> None:
        self._root = root
        self._builds = list(builds)

    def list(self):
        return list(self._builds)

    def workspace(self, slug: str) -> Path:
        return self._root / slug


class _Bus:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)


def _cfg(**kv):
    return lambda key: kv.get(key)


def _registry(tmp_path, bus=None, configured=True):
    cfg = _cfg(BAMBU_HOST="192.168.1.42", BAMBU_ACCESS_CODE="12345678",
               BAMBU_SERIAL="01P00A000000000") if configured else _cfg()
    builds = _Builds(tmp_path, _Build("IronEye"), _Build("Notes", BuildKind.APP))
    return ToolRegistry(None, builds, bus=bus or _Bus(), bambu=cfg), builds


def test_unconfigured_printer_opens_the_connect_panel(tmp_path):
    bus = _Bus()
    reg, _ = _registry(tmp_path, bus=bus, configured=False)
    out = reg.dispatch("printer_status", {})
    assert "connect panel" in out
    assert any(isinstance(e, ConnectRequested) and e.service_id == "bambu" for e in bus.published)


def test_unknown_hologram_is_named_honestly(tmp_path):
    reg, _ = _registry(tmp_path)
    assert "don't see a hologram" in reg.dispatch("print_hologram", {"name": "Garden Gnome"})


def test_an_app_is_not_a_printable_hologram(tmp_path):
    reg, _ = _registry(tmp_path)
    assert "don't see a hologram" in reg.dispatch("print_hologram", {"name": "Notes"})


def test_full_auto_slices_uploads_and_starts(tmp_path, monkeypatch):
    reg, builds = _registry(tmp_path)
    ws = builds.workspace("ironeye")
    (ws / "assets").mkdir(parents=True)
    (ws / "assets" / "model.3mf").write_bytes(b"3mf")

    actions: list[str] = []

    class _Printer:
        def __init__(self, *a):
            pass

        def upload(self, path, remote):
            actions.append(f"upload:{remote}")
            return remote

        def start_print(self, remote):
            actions.append(f"start:{remote}")

    def _slice(inp, out, **kw):
        Path(out).write_bytes(b"gcode3mf")
        actions.append("slice")
        return True

    monkeypatch.setattr(bp, "BambuPrinter", _Printer)
    monkeypatch.setattr(bp, "try_slice", _slice)
    out = reg.dispatch("print_hologram", {"name": "ironeye"})
    assert actions == ["slice", "upload:ironeye.gcode.3mf", "start:ironeye.gcode.3mf"]
    assert "started" in out


def test_a_declined_headless_slice_falls_back_to_studio(tmp_path, monkeypatch):
    reg, builds = _registry(tmp_path)
    ws = builds.workspace("ironeye")
    (ws / "assets").mkdir(parents=True)
    (ws / "assets" / "model.3mf").write_bytes(b"3mf")
    opened: list[Path] = []
    monkeypatch.setattr(bp, "BambuPrinter", lambda *a: object())
    monkeypatch.setattr(bp, "try_slice", lambda *a, **k: False)
    monkeypatch.setattr(bp, "open_in_studio", lambda p: opened.append(Path(p)) or True)
    out = reg.dispatch("print_hologram", {"name": "IronEye"})
    assert opened and opened[0].name == "model.3mf"
    assert "Bambu Studio" in out and "Print" in out


def test_a_hologram_with_no_compiled_file_says_so(tmp_path, monkeypatch):
    reg, _ = _registry(tmp_path)
    monkeypatch.setattr(bp, "BambuPrinter", lambda *a: object())
    monkeypatch.setattr(bp, "try_slice", lambda *a, **k: False)
    out = reg.dispatch("print_hologram", {"name": "IronEye"})
    assert "no compiled model file" in out


def test_status_formatting_reads_like_a_sentence():
    line = bp.format_status({"state": "printing", "percent": 42, "minutes_left": 95,
                             "job": "ironeye.gcode.3mf", "nozzle_c": 219.5})
    assert "printing" in line and "42% done" in line and "1h 35m" in line and "220°C" in line
    assert bp.format_status({"state": "idle"}) == "The printer is idle."


def test_the_printer_refuses_to_exist_without_all_three_values():
    import pytest

    with pytest.raises(bp.BambuError):
        bp.BambuPrinter("192.168.1.42", "", "01P")
