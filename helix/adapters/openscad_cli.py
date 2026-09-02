"""CadEngine adapter — the OpenSCAD command line. The only place that shells out to the hologram engine.

OpenSCAD is not installed on most machines HELIX lands on (not on Brian's, today), so this adapter does
three jobs: FIND the binary wherever a Windows, macOS or Linux install puts it; RUN it the way every
other HELIX child process runs (no console window flashing in the --windowed build, a hard timeout that
kills the whole process tree, output captured, cwd at the model so `use <helix.scad>` resolves); and
INSTALL it just in time with winget when the user says "install it" — the same move HELIX makes for an
API key it doesn't have yet.

Two Windows facts shape the code. OpenSCAD ships TWO executables: openscad.exe is built for the GUI
subsystem and has no stdout — run it headless and every compiler message vanishes, so a failed compile
looks like a silent success with no file — while openscad.com is the console wrapper that relays the
output. We always prefer the .com. And a child with no creation flag pops a console window over the orb
on every compile, so every spawn goes through ONE runner that cannot forget the flag, the timeout, the
cwd, or the environment.

Every public method returns a CadResult and never raises: a missing engine, a syntax slip, a CGAL crash
and a timeout are all ordinary outcomes the repair loop must handle.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from helix.domain.scad import friendly_error
from helix.logging_setup import get_logger
from helix.ports.cad import CadResult

_LOG = get_logger("cad.openscad")

# In a --windowed (frozen) build a child with no creation flag flashes a console window; compiles happen
# on every hologram build and every "make it wider", so the flag is set in the one runner, once.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IS_WINDOWS = os.name == "nt"

# winget's package id for the stable release. The flags accept the source/package agreements up front so
# a headless install never parks on a y/N prompt the user cannot see.
WINGET_ID = "OpenSCAD.OpenSCAD"
WINGET_ARGS = (
    "install", "--id", WINGET_ID, "-e", "--accept-source-agreements", "--accept-package-agreements",
)

# A dark built-in colour scheme that exists in 2021.01 — the preview goes to a vision critic and into
# the console, and a technical drawing reads better on slate than on OpenSCAD's default yellow-on-cream.
# If a build rejects the name the render is retried with the default scheme rather than failing.
DARK_COLORSCHEME = "Tomorrow Night"

_VERSION_PROBE_TIMEOUT_S = 20.0
_KILL_TIMEOUT_S = 10.0
_VERSION_RE = re.compile(r"OpenSCAD version\s+([0-9][\w.\-]*)", re.I)


def _names() -> tuple[str, ...]:
    """Executable names worth trying, best first. On Windows the .com (console wrapper) MUST win over the
    .exe (GUI subsystem, swallows stdout) — see the module docstring."""
    return ("openscad.com", "openscad.exe") if _IS_WINDOWS else ("openscad",)


def _candidate_dirs() -> list[Path]:
    """Where an OpenSCAD install lands when it is not on PATH: the installer's Program Files default (also
    what winget's installer-based package uses), the per-user Programs dir, scoop's shims and apps, and
    winget's portable-package dirs. The dirs need not exist; they are merely looked in."""
    dirs: list[Path] = []
    env = os.environ
    if _IS_WINDOWS:
        for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            base = env.get(var)
            if base:
                dirs.append(Path(base) / "OpenSCAD")
                dirs.append(Path(base) / "OpenSCAD (Nightly)")
        local = env.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Programs" / "OpenSCAD")
            dirs.append(Path(local) / "Microsoft" / "WinGet" / "Links")
            pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
            try:
                dirs.extend(p for p in sorted(pkgs.glob(f"{WINGET_ID}*")) if p.is_dir())
            except OSError:
                pass
        home = env.get("USERPROFILE") or env.get("HOME")
        if home:
            dirs.append(Path(home) / "scoop" / "shims")
            dirs.append(Path(home) / "scoop" / "apps" / "openscad" / "current")
    else:
        dirs.extend(Path(p) for p in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/snap/bin"))
        if sys.platform == "darwin":
            dirs.append(Path("/Applications/OpenSCAD.app/Contents/MacOS"))
    return dirs


def _prefer_com(path: str) -> str:
    """Given any OpenSCAD executable path on Windows, hand back the sibling .com when it exists."""
    if _IS_WINDOWS and path.lower().endswith(".exe"):
        com = Path(path).with_suffix(".com")
        if com.is_file():
            return str(com)
    return path


def _find_in(dirs: Iterable[Path]) -> str | None:
    for d in dirs:
        for name in _names():
            cand = d / name
            try:
                if cand.is_file():
                    return str(cand)
            except OSError:
                continue
        if sys.platform == "darwin":
            cand = d / "OpenSCAD"
            if cand.is_file():
                return str(cand)
    return None


@dataclass
class _Run:
    """What one child process did. `error` is set when it could not even be started."""

    returncode: int
    output: str           # stdout and stderr interleaved, in order
    timed_out: bool
    error: str | None
    seconds: float


class OpenScadCli:
    """CadEngine over the OpenSCAD CLI. See the module docstring for the Windows .com/.exe story."""

    def __init__(
        self,
        path_override: Callable[[], str | None] | None = None,
        libraries_dir: Path | None = None,
    ) -> None:
        # path_override is read on every probe (not once at construction) so a path the user types into
        # settings takes effect without a restart — the same live-getter pattern the API keys use.
        self._override = path_override
        # libraries_dir goes on OPENSCADPATH so a future BOSL2 drop-in just works; it need not exist.
        self._libraries_dir = libraries_dir
        self._lock = threading.Lock()
        self._exe: str | None = None
        self._probed = False
        self._version: str | None = None

    # ----- discovery -----
    def _discover(self) -> str | None:
        """Resolve the binary, best candidate first: the settings override, PATH, then the known install
        dirs. Returns a path that exists on disk, or None."""
        if self._override is not None:
            try:
                # A pasted path often arrives wrapped in quotes (Explorer's "Copy as path"); strip them.
                raw = (self._override() or "").strip().strip('"').strip("'").strip()
            except Exception:  # noqa: BLE001 — a settings getter must never take the engine down
                raw = ""
            if raw:
                p = Path(raw)
                if p.is_dir():
                    found = _find_in([p])
                    if found:
                        return found
                elif p.is_file():
                    return _prefer_com(str(p))
                else:
                    on_path = shutil.which(raw)
                    if on_path:
                        return _prefer_com(on_path)
        for name in _names():
            on_path = shutil.which(name)
            if on_path:
                return _prefer_com(on_path)
        return _find_in(_candidate_dirs())

    def _resolve(self, force: bool = False) -> str | None:
        """The cached binary path. Discovery is a handful of stat calls, so the cache is dropped whenever
        a run fails (the binary may have been removed or upgraded under us) and after install(). A MISS
        is never cached: every user-facing sentence promises "install it and HELIX will find it", and a
        user who installs OpenSCAD themselves mid-session (or accepts the winget prompt after a timed-out
        install) would otherwise be told it is still missing until they restart — nothing runs while the
        engine is absent, so no failed run would ever drop that stale answer. Re-probing on each
        pre-flight costs a few stats, which is cheaper than one wrong "not installed"."""
        with self._lock:
            if self._probed and not force:
                return self._exe
            self._exe = self._discover()
            self._probed = self._exe is not None
            self._version = None
            return self._exe

    def _forget(self) -> None:
        with self._lock:
            self._probed = False
            self._exe = None
            self._version = None

    # ----- the one runner -----
    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path | None,
        timeout_s: float,
        on_line: Callable[[str], None] | None = None,
        kill_tree: bool = True,
    ) -> _Run:
        """Run ONE child process the HELIX way: no console window, stdin closed, stdout+stderr merged and
        streamed line by line (so install progress can be narrated and a full pipe can never deadlock),
        OPENSCADPATH set, and a hard timeout that kills the whole process tree — winget spawns the real
        installer as a child, and a bare proc.kill() would orphan it. Never raises."""
        env = dict(os.environ)
        if self._libraries_dir is not None:
            prior = env.get("OPENSCADPATH")
            env["OPENSCADPATH"] = (
                str(self._libraries_dir) + (os.pathsep + prior if prior else "")
            )
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _Run(-1, "", False, str(exc), time.monotonic() - started)

        lines: list[str] = []

        def _drain() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    lines.append(line)
                    if on_line is not None:
                        try:
                            on_line(line.rstrip("\r\n"))
                        except Exception:  # noqa: BLE001 — a narrator bug must not stop the drain
                            pass
            except Exception:  # noqa: BLE001 — the pipe closing under a kill is expected
                pass

        reader = threading.Thread(target=_drain, daemon=True, name="helix-cad-drain")
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _LOG.warning("%s exceeded %ss; killing it", Path(argv[0]).name, timeout_s)
            self._kill(proc, tree=kill_tree)
            try:
                proc.wait(timeout=_KILL_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=_KILL_TIMEOUT_S)
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        rc = proc.returncode if proc.returncode is not None else -1
        return _Run(rc, "".join(lines), timed_out, None, time.monotonic() - started)

    def _kill(self, proc, *, tree: bool) -> None:
        """Kill a child AND its children. On Windows taskkill /T takes the tree down (winget → installer);
        that call goes through the same runner so it, too, is windowless and time-boxed — with
        kill_tree=False so a stuck taskkill gets a plain kill instead of recursing."""
        if proc.poll() is not None:
            return
        if tree and _IS_WINDOWS:
            res = self._run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                cwd=None, timeout_s=_KILL_TIMEOUT_S, kill_tree=False,
            )
            if res.error is None and not res.timed_out and proc.poll() is not None:
                return
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    # ----- CadEngine -----
    def available(self) -> bool:
        return self._resolve() is not None

    def version(self) -> str | None:
        exe = self._resolve()
        if exe is None:
            return None
        with self._lock:
            cached = self._version
        if cached:
            return cached
        # --version prints to stderr on some builds and stdout on others; the runner merges both.
        res = self._run([exe, "--version"], cwd=None, timeout_s=_VERSION_PROBE_TIMEOUT_S)
        if res.error is not None:
            self._forget()
            return None
        m = _VERSION_RE.search(res.output)
        if m:
            ver = m.group(1)
        elif res.returncode == 0:   # an unfamiliar banner, but the binary runs: keep its first line
            ver = (res.output.strip().splitlines() or [""])[0].strip() or None
        else:
            ver = None
        if ver:
            with self._lock:
                self._version = ver
        return ver

    def compile_stl(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        return self._export(source, out, timeout_s=timeout_s)

    def export_3mf(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        return self._export(source, out, timeout_s=timeout_s)

    def render_png(
        self, source: Path, out: Path, *, size: tuple[int, int] = (1280, 960), timeout_s: float = 120.0,
    ) -> CadResult:
        w, h = (int(size[0]), int(size[1])) if size else (1280, 960)
        flags = [
            "--autocenter", "--viewall", f"--imgsize={w},{h}", f"--colorscheme={DARK_COLORSCHEME}",
            "--projection=p",
        ]
        res = self._export(source, out, timeout_s=timeout_s, extra=flags)
        if not res.ok and res.detail and re.search(r"colou?r ?scheme", res.detail, re.I):
            # An engine build without our scheme: the picture matters more than its palette.
            plain = [f for f in flags if not f.startswith("--colorscheme")]
            res = self._export(source, out, timeout_s=timeout_s, extra=plain)
        return res

    def _export(
        self, source: Path, out: Path, *, timeout_s: float, extra: list[str] | None = None,
    ) -> CadResult:
        """`<exe> [extra] -o <out> <source>` with cwd at the source's folder so `use <helix.scad>` resolves
        and the compiler's messages name `model.scad`, not a user path. The export goes to a temp name
        beside `out` and is moved into place only on success, so a failed recompile leaves the LAST GOOD
        file alone (the viewer keeps showing something while the repair loop runs) and a stale file can
        never masquerade as this run's result."""
        started = time.monotonic()
        exe = self._resolve()
        if exe is None:
            return CadResult(False, None, self._missing_sentence(), None, 0.0)
        source = Path(source)
        out = Path(out)
        if not source.is_file():
            return CadResult(
                False, None, "The hologram has no model.scad to compile yet.", None,
                time.monotonic() - started,
            )
        tmp = out.with_name(f"{out.stem}.helixtmp{out.suffix}")
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            if tmp.exists():
                tmp.unlink()
        except OSError as exc:
            return CadResult(
                False, None, "HELIX couldn't write into the hologram's folder.", str(exc),
                time.monotonic() - started,
            )
        argv = [exe, *(extra or []), "-o", str(tmp), source.name]
        res = self._run(argv, cwd=source.parent, timeout_s=timeout_s)
        seconds = time.monotonic() - started
        if res.error is not None:
            self._forget()   # the binary we cached could not be started — re-probe next time
            return CadResult(
                False, None, "The hologram engine couldn't be started.", res.error, seconds,
            )
        if res.timed_out:
            self._forget()
            _cleanup(tmp)
            return CadResult(
                False, None,
                f"Compiling the hologram took too long (over {int(timeout_s)} seconds) and was stopped "
                f"— the model is probably heavier than it needs to be.",
                f"Compile timed out after {int(timeout_s)}s. Output so far:\n{_tail(res.output)}",
                seconds,
            )
        produced = False
        try:
            produced = tmp.is_file() and tmp.stat().st_size > 0
        except OSError:
            produced = False
        if res.returncode != 0 or not produced:
            self._forget()
            _cleanup(tmp)
            problem, detail = friendly_error(res.output)
            if res.returncode == 0 and not produced and not detail:
                detail = "The compiler exited cleanly but wrote no file (empty top-level object?)."
            return CadResult(False, None, problem, detail, seconds)
        try:
            os.replace(tmp, out)
        except OSError as exc:
            _cleanup(tmp)
            return CadResult(
                False, None, "HELIX couldn't save the compiled hologram.", str(exc), seconds,
            )
        return CadResult(True, out, None, None, seconds)

    def install(
        self, on_progress: Callable[[str], None] | None = None, timeout_s: float = 900.0,
    ) -> CadResult:
        """Install OpenSCAD just in time. Windows → winget (the machine's own package manager; the
        installer runs with its own progress UI and no prompts because the agreements are accepted up
        front). Elsewhere → a plain no with the package manager to use. ok=True ONLY if the binary is
        actually found afterwards — winget's exit code is advisory (it says "already installed" with a
        non-zero code, and "success" for a package that then isn't on PATH)."""
        started = time.monotonic()
        if not _IS_WINDOWS:
            return CadResult(
                False, None,
                "HELIX can install the hologram engine automatically on Windows only — here, install "
                "OpenSCAD with your package manager (brew install --cask openscad, or apt install "
                "openscad) and HELIX will find it.",
                None, 0.0,
            )
        winget = shutil.which("winget")
        if winget is None:
            local = os.environ.get("LOCALAPPDATA")
            cand = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe" if local else None
            winget = str(cand) if cand is not None and cand.is_file() else None
        if winget is None:
            return CadResult(
                False, None,
                "Windows' app installer (winget) isn't available here, so HELIX can't install the "
                "hologram engine itself — install OpenSCAD from openscad.org and HELIX will find it.",
                None, time.monotonic() - started,
            )

        def _narrate(line: str) -> None:
            if on_progress is None:
                return
            clean = _progress_text(line)
            if clean:
                on_progress(clean)

        if on_progress is not None:
            on_progress("Installing the hologram engine (OpenSCAD)…")
        res = self._run([winget, *WINGET_ARGS], cwd=None, timeout_s=timeout_s, on_line=_narrate)
        self._forget()
        found = self._resolve(force=True)
        seconds = time.monotonic() - started
        if found is not None:
            _LOG.info("OpenSCAD installed at %s", found)
            return CadResult(True, Path(found), None, None, seconds)
        if res.error is not None:
            problem = ("The installer couldn't be started — install OpenSCAD from openscad.org and "
                       "HELIX will find it.")
        elif res.timed_out:
            problem = ("The install took too long and was stopped — you can finish it by hand from "
                       "openscad.org and HELIX will find it.")
        else:
            problem = ("The install didn't finish — you may need to approve it, or install OpenSCAD "
                       "from openscad.org and HELIX will find it.")
        return CadResult(False, None, problem, _tail(res.output) or res.error, seconds)

    def install_hint(self) -> str:
        if _IS_WINDOWS:
            return (
                "Holograms are compiled by OpenSCAD — free, open source, about a minute to install — "
                "and it isn't on this machine yet; just say “install it” and HELIX will set it up."
            )
        return (
            "Holograms are compiled by OpenSCAD — free and open source — and it isn't on this machine "
            "yet; install it with your package manager (brew install --cask openscad, or apt install "
            "openscad) and HELIX will find it."
        )

    # ----- helpers -----
    def _missing_sentence(self) -> str:
        return "The hologram engine isn't installed yet. " + self.install_hint()


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _tail(text: str, limit: int = 800) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


_PROGRESS_JUNK_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f▀-▟─-╿]+")


def _progress_text(line: str) -> str:
    """winget paints its progress with carriage returns, spinners and block glyphs; the console wants the
    words ('Downloading…', 'Successfully installed'), one per line, and nothing that is only a bar."""
    piece = line.replace("\r", "\n").split("\n")[-1]
    clean = " ".join(_PROGRESS_JUNK_RE.sub(" ", piece).split())
    return clean if re.search(r"[A-Za-z]{3,}", clean) else ""
