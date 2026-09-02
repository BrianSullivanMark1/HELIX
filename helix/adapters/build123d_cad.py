"""CadEngine adapter — build123d (the OCCT B-rep kernel) behind the same port OpenSCAD stood behind.

The engine swap the V3 hologram overhaul is built on: `model.py` (build123d) replaces `model.scad`.
Real fillets and chamfers, STEP export the slicer can eat natively (Bambu Studio imports STEP), and a
design language LLMs write far more accurately than OpenSCAD's dialect.

Isolation contract: build123d is NEVER imported in the app process (it drags the OCCT kernel — ~2s
import, heavy resident memory — and the startup-cost test forbids it). Every call here spawns the
`helix.cad.runner` worker subprocess, which computes ONE job and writes every artifact of that source
in a single run: STL + STEP + 3MF + the critic's preview PNG + a meta report. The port's three
compile methods are then served from that one run — `compile_stl` pays for it, `export_3mf` and
`render_png` ride the cache — so a hologram build costs one kernel session, not three (OpenSCAD paid
for three full compiles).

Every method returns a CadResult and never raises, per the port: a missing engine, a design bug, a
timeout, or a worker crash are ordinary outcomes for the repair loop.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from helix.logging_setup import get_logger
from helix.ports.cad import CadResult

_LOG = get_logger("build123d_cad")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Artifact names relative to the requested STL's folder — one run writes the whole set.
STEP_NAME = "model.step"
MF_NAME = "model.3mf"
PNG_NAME = "preview.png"
META_NAME = "model.meta.json"

_INSTALL_HINT = (
    "Holograms are computed by the build123d CAD kernel — a free Python library, about a minute to "
    "install — and it isn't set up in this environment yet; ask HELIX to install it."
)


class Build123dCad:
    """The hologram engine. Stateless between runs except a per-source cache of the last run, so the
    baker's compile→3mf→preview sequence spawns ONE worker."""

    def __init__(self, app_root: Path | None = None) -> None:
        # The worker needs the helix package importable. In dev that is the app root on PYTHONPATH;
        # frozen, the worker is HELIX.exe itself re-invoked with the `cadworker` command.
        self._app_root = Path(app_root) if app_root else Path(__file__).resolve().parents[2]
        self._lock = threading.Lock()
        self._available: bool | None = None
        # source path (resolved str) -> {"sha": str, "outputs": {...}, "seconds": float}
        self._runs: dict[str, dict] = {}
        self._warm: subprocess.Popen | None = None   # the resident --serve worker (sliders)
        self._warm_io = threading.Lock()             # one job on its pipe at a time

    # ----- availability -----
    def available(self) -> bool:
        """Cheap: a find_spec probe, no import, cached. reset() forgets after an install."""
        with self._lock:
            if self._available is None:
                try:
                    self._available = importlib.util.find_spec("build123d") is not None
                except Exception:  # noqa: BLE001 — a probing hiccup reads as missing, never a crash
                    self._available = False
            return self._available

    def reset(self) -> None:
        with self._lock:
            self._available = None
            self._runs.clear()

    def version(self) -> str | None:
        if not self.available():
            return None
        try:
            from importlib.metadata import version

            return version("build123d")
        except Exception:  # noqa: BLE001
            return None

    # ----- the port's compile surface -----
    def compile_stl(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        res = self._ensure_run(Path(source), Path(out), timeout_s=timeout_s)
        if not res.ok:
            return res
        return self._serve(res, "stl", Path(out))

    def export_3mf(self, source: Path, out: Path, *, timeout_s: float = 180.0) -> CadResult:
        res = self._ensure_run(Path(source), Path(out).parent / "model.stl", timeout_s=timeout_s)
        if not res.ok:
            return res
        return self._serve(res, "mf", Path(out))

    def render_png(
        self, source: Path, out: Path, *, size: tuple[int, int] = (1280, 960),
        timeout_s: float = 120.0,
    ) -> CadResult:
        res = self._ensure_run(Path(source), Path(out).parent / "model.stl", timeout_s=timeout_s)
        if not res.ok:
            return res
        return self._serve(res, "png", Path(out))

    # ----- the studio's live-recompile surface (beyond the port) -----
    def preview(
        self, source: Path, out_dir: Path, overrides: dict | None = None, *,
        timeout_s: float = 90.0,
    ) -> CadResult:
        """Recompile with parameter OVERRIDES into a scratch dir — the studio's slider path. Writes
        model.stl + model.meta.json under out_dir; the design file itself is untouched (committing a
        parameter change is a separate, deliberate act through domain.cadpy.set_params).

        Runs on the WARM worker (`--serve`: the kernel imports once and stays resident), so a slider
        drag recomputes in about a second instead of paying the ~3s kernel import every time. Falls
        back to a one-shot worker if the warm one is unavailable or wedged."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        outputs = {"stl": out_dir / "model.stl", "meta": out_dir / META_NAME}
        res = self._warm_job(Path(source), outputs, dict(overrides or {}), timeout_s)
        if res is not None:
            return res
        return self._run_job(
            Path(source), outputs=outputs,
            overrides=dict(overrides or {}), timeout_s=timeout_s, cache=False,
        )

    def _warm_worker(self):
        """The resident --serve worker, started on first use. Returns the Popen or None."""
        with self._lock:
            proc = self._warm
            if proc is not None and proc.poll() is None:
                return proc
            self._warm = None
        if not self.available():
            return None
        env = dict(os.environ)
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = str(self._app_root) + os.pathsep + env.get("PYTHONPATH", "")
        cmd = ([sys.executable, "cadworker", "--serve"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "helix.cad.runner", "--serve"])
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, creationflags=_CREATE_NO_WINDOW, env=env,
            )
            ready = proc.stdout.readline().strip()  # "ready" once the kernel import lands
            if ready != "ready":
                proc.kill()
                return None
        except Exception:  # noqa: BLE001
            _LOG.warning("warm cad worker failed to start", exc_info=True)
            return None
        with self._lock:
            self._warm = proc
        return proc

    def _warm_job(self, source: Path, outputs: dict[str, Path], overrides: dict,
                  timeout_s: float) -> CadResult | None:
        """One job through the warm worker; None means 'use the one-shot fallback'."""
        proc = self._warm_worker()
        if proc is None:
            return None
        t0 = time.time()
        scratch = Path(tempfile.gettempdir()) / "helix-cad"
        scratch.mkdir(parents=True, exist_ok=True)
        job_path = scratch / f"job-{uuid.uuid4().hex}.json"
        result_path = scratch / f"result-{uuid.uuid4().hex}.json"
        job_path.write_text(json.dumps({
            "source": str(source), "workspace": str(source.parent), "overrides": overrides,
            "outputs": {k: str(v) for k, v in outputs.items()}, "result": str(result_path),
        }), encoding="utf-8")
        done: list[str] = []

        def read_done() -> None:
            try:
                done.append(proc.stdout.readline())
            except Exception:  # noqa: BLE001
                pass

        try:
            with self._warm_io:
                proc.stdin.write(str(job_path) + "\n")
                proc.stdin.flush()
                reader = threading.Thread(target=read_done, daemon=True)
                reader.start()
                reader.join(timeout_s)
                if reader.is_alive():  # wedged mid-job: kill it; the next call restarts warm
                    proc.kill()
                    with self._lock:
                        self._warm = None
                    return CadResult(False, None,
                                     "The design took too long to compute — it's probably heavier "
                                     "than it needs to be.", f"warm worker timeout {timeout_s:.0f}s",
                                     time.time() - t0)
        except Exception:  # noqa: BLE001
            with self._lock:
                self._warm = None
            return None
        finally:
            try:
                job_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # the warm worker died or wrote nothing — fall back one-shot
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass
        seconds = float(payload.get("seconds") or (time.time() - t0))
        if not payload.get("ok"):
            return CadResult(False, None,
                             payload.get("problem") or "The design couldn't be computed.",
                             payload.get("detail"), seconds)
        stl = (payload.get("outputs") or {}).get("stl")
        return CadResult(True, Path(stl) if stl else None, None, None, seconds)

    def meta_for(self, source: Path) -> dict | None:
        """The last run's meta report for this source (bbox, volume, parts), or None."""
        with self._lock:
            run = self._runs.get(str(Path(source).resolve()))
        if not run:
            return None
        return dict(run.get("meta") or {}) or None

    # ----- install -----
    def install(
        self, on_progress: Callable[[str], None] | None = None, timeout_s: float = 900.0,
    ) -> CadResult:
        """pip-install the kernel into THIS interpreter (dev). A frozen HELIX ships the kernel inside
        the bundle, so a missing engine there is a packaging fault an install cannot fix — say so."""
        t0 = time.time()
        if getattr(sys, "frozen", False):
            return CadResult(
                ok=False, output=None,
                problem="This HELIX build is missing its CAD kernel — reinstall HELIX to restore it.",
                detail="frozen bundle without build123d", seconds=0.0,
            )
        say = on_progress or (lambda _line: None)
        say("Installing the CAD kernel (build123d) — this can take a minute…")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "build123d"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=_CREATE_NO_WINDOW,
            )
            deadline = time.time() + timeout_s
            for line in iter(proc.stdout.readline, ""):
                if time.time() > deadline:
                    proc.kill()
                    return CadResult(False, None, "The install ran out of time.", "pip timeout",
                                     time.time() - t0)
                line = line.strip()
                if line and ("Collecting" in line or "Installing" in line or "Successfully" in line):
                    say(line)
            proc.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            return CadResult(False, None, "The install couldn't run.", str(exc), time.time() - t0)
        self.reset()
        ok = self.available()
        return CadResult(
            ok=ok, output=None,
            problem=None if ok else "The install finished but the engine still isn't importable.",
            detail=None if ok else "pip exited but find_spec('build123d') is empty",
            seconds=time.time() - t0,
        )

    def install_hint(self) -> str:
        return _INSTALL_HINT

    # ----- internals -----
    def _sha(self, source: Path) -> str:
        import hashlib

        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        lib = source.parent / "helix_parts.py"
        try:
            lib_text = lib.read_text(encoding="utf-8", errors="replace") if lib.is_file() else ""
        except OSError:
            lib_text = ""
        return hashlib.sha256((text + "\n" + lib_text).encode("utf-8")).hexdigest()

    def _ensure_run(self, source: Path, stl_out: Path, *, timeout_s: float) -> CadResult:
        """Serve from the cached run when the source text hasn't changed; otherwise run the worker
        once with the FULL artifact set anchored beside the requested STL."""
        key = str(source.resolve())
        sha = self._sha(source)
        with self._lock:
            run = self._runs.get(key)
        if run and run.get("sha") == sha and sha:
            outs = run.get("outputs") or {}
            if outs.get("stl") and Path(outs["stl"]).is_file():
                return CadResult(True, Path(outs["stl"]), None, None, 0.0)
        folder = stl_out.parent
        res = self._run_job(
            source,
            outputs={
                "stl": stl_out, "step": folder / STEP_NAME, "mf": folder / MF_NAME,
                "png": folder / PNG_NAME, "meta": folder / META_NAME,
            },
            overrides={}, timeout_s=timeout_s, cache=True, sha=sha,
        )
        return res

    def _serve(self, run_result: CadResult, kind: str, want: Path) -> CadResult:
        """Hand back one artifact of the last run, copying if the caller asked for a different
        location than the run wrote."""
        source_key = None
        with self._lock:
            for key, run in self._runs.items():
                outs = run.get("outputs") or {}
                if outs.get("stl") and Path(outs["stl"]) == run_result.output:
                    source_key = key
                    break
            run = self._runs.get(source_key) if source_key else None
        outs = (run or {}).get("outputs") or {}
        have = outs.get(kind)
        if not have or not Path(have).is_file():
            label = {"mf": "3MF export", "png": "preview render", "stl": "mesh"}.get(kind, kind)
            return CadResult(False, None, f"The {label} wasn't produced this run.",
                             (run or {}).get("problems") and "; ".join(run["problems"]) or None, 0.0)
        have_p = Path(have)
        if have_p.resolve() != want.resolve():
            try:
                want.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(have_p, want)
            except OSError as exc:
                return CadResult(False, None, "The artifact couldn't be copied into place.",
                                 str(exc), 0.0)
        return CadResult(True, want, None, None, 0.0)

    def _worker_cmd(self, job_path: Path) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "cadworker", str(job_path)]
        return [sys.executable, "-m", "helix.cad.runner", str(job_path)]

    def _run_job(
        self, source: Path, *, outputs: dict[str, Path], overrides: dict, timeout_s: float,
        cache: bool, sha: str = "",
    ) -> CadResult:
        if not self.available():
            return CadResult(False, None, self.install_hint(), None, 0.0)
        t0 = time.time()
        scratch = Path(tempfile.gettempdir()) / "helix-cad"
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError:
            scratch = Path(tempfile.gettempdir())
        job_path = scratch / f"job-{uuid.uuid4().hex}.json"
        result_path = scratch / f"result-{uuid.uuid4().hex}.json"
        job = {
            "source": str(source), "workspace": str(source.parent),
            "overrides": overrides,
            "outputs": {k: str(v) for k, v in outputs.items()},
            "result": str(result_path),
        }
        env = dict(os.environ)
        if not getattr(sys, "frozen", False):
            env["PYTHONPATH"] = str(self._app_root) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            job_path.write_text(json.dumps(job), encoding="utf-8")
            proc = subprocess.run(
                self._worker_cmd(job_path), timeout=timeout_s, capture_output=True, text=True,
                creationflags=_CREATE_NO_WINDOW, env=env, cwd=str(source.parent),
            )
        except subprocess.TimeoutExpired:
            return CadResult(False, None,
                             "The design took too long to compute — it's probably heavier than it "
                             "needs to be.", f"worker timeout after {timeout_s:.0f}s",
                             time.time() - t0)
        except OSError as exc:
            return CadResult(False, None, "The CAD worker couldn't be started.", str(exc),
                             time.time() - t0)
        finally:
            try:
                job_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-1200:]
            _LOG.warning("cad worker wrote no result (exit %s): %s", proc.returncode, tail)
            return CadResult(False, None, "The CAD engine crashed while computing the design.",
                             tail or f"worker exit {proc.returncode}", time.time() - t0)
        finally:
            try:
                result_path.unlink(missing_ok=True)
            except OSError:
                pass
        seconds = float(payload.get("seconds") or (time.time() - t0))
        if not payload.get("ok"):
            return CadResult(False, None,
                             payload.get("problem") or "The design couldn't be computed.",
                             payload.get("detail"), seconds)
        outs = payload.get("outputs") or {}
        stl = outs.get("stl")
        if cache and stl:
            with self._lock:
                self._runs[str(source.resolve())] = {
                    "sha": sha or self._sha(source), "outputs": outs,
                    "meta": payload.get("meta") or {},
                    "problems": (payload.get("meta") or {}).get("problems") or [],
                }
        return CadResult(True, Path(stl) if stl else None, None, None, seconds)
