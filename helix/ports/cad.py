"""CadEngine port — compiles a hologram's OpenSCAD source into meshes and pictures. The Forge's lathe.

A hologram is a PROGRAM (model.scad), not a pile of coordinates: the coder edits a named parameter and
HELIX recompiles. Something has to do that compiling, and it is deliberately behind a port:

  - today the adapter is the OpenSCAD command line (helix/adapters/openscad_cli.py), found on PATH or
    installed just in time with winget;
  - tomorrow it may be a browser-WASM compiler running inside the viewer, with no install at all;
  - in tests it is a fake that returns scripted CadResults, because the real binary is absent on most
    machines (including Brian's, today) and a unit test must never depend on it.

Services (model_baker, the build_3d_model tool) code against THIS surface only, so swapping the engine —
or having none — changes nothing above the port. Every method returns a CadResult instead of raising: a
missing engine, a syntax slip in the coder's source, a CGAL crash, or a timeout are all ordinary
outcomes the repair loop has to handle, not exceptions the UI thread would have to catch.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class CadResult:
    """The outcome of one engine call.

    `problem` is the ONE sentence a user may read (warm, plain, no paths, no compiler text) — it goes on
    the console and into the friendly page. `detail` is the compiler's own words (file:line:col + message,
    trimmed) and exists only to feed the coder's repair prompt; callers fence it as DATA, never speak it.
    They are separate fields precisely so a caller cannot leak one where the other belongs."""

    ok: bool
    output: Path | None        # the file produced, when ok
    problem: str | None        # ONE warm user-facing sentence when not ok (no paths, no stderr)
    detail: str | None         # the compiler's own words, for the coder's repair prompt; None when ok
    seconds: float             # wall time the call took (compiles are what the build budget pays for)


class CadEngine(Protocol):
    def available(self) -> bool:
        """Is there an engine to call right now? Cheap — no process is spawned — so the tool can pre-flight
        a hologram request and offer the install instead of spending coder time on a build that cannot
        compile."""
        ...

    def version(self) -> str | None:
        """The engine's version string ("2021.01"), or None when unavailable."""
        ...

    def compile_stl(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        """Compile `source` (a .scad file) to a mesh at `out`. The viewer and the printer both eat STL."""
        ...

    def export_3mf(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        """Compile `source` to 3MF at `out` — the slicer-friendly export. Best effort: not every build of
        every engine can write it."""
        ...

    def render_png(
        self, source: Path, out: Path, *, size: tuple[int, int] = (1280, 960), timeout_s: float = 120.0,
    ) -> CadResult:
        """Render a preview picture of `source` to `out` — what the vision critic looks at."""
        ...

    def install(
        self, on_progress: Callable[[str], None] | None = None, timeout_s: float = 900.0,
    ) -> CadResult:
        """Install the engine (blocking — call it from a worker, never the UI thread). Streams plain
        progress lines to `on_progress` so the console can narrate. ok=True only if the engine is
        actually found afterwards."""
        ...

    def install_hint(self) -> str:
        """ONE plain sentence telling the user what the engine is and how to get it — what the model
        says when a hologram is asked for and the engine is missing."""
        ...
