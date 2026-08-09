"""CoderAgent adapter — the Claude Code CLI, headless and streaming.

Runs the same `claude` CLI that ships with the desktop app, but as an independent subprocess (`-p`)
authenticated by HELIX's own token/key (the interactive desktop login does NOT carry into a subprocess).
The CLI edits files in the workspace; HELIX owns git, so the build prompt forbids the model from
committing. Streams `stream-json` events so the orb can show live progress.

Windows-first (mirrors the proven prototype): resolves the CLI from the desktop app's MSIX install dirs
or PATH. Edge/I-O module — subprocess + filesystem only.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from helix.logging_setup import get_logger
from helix.ports.coder import CoderResult, ProgressFn

_LOG = get_logger("coder.cli")


def _kill_tree(proc) -> None:
    """Kill the coder process AND its children. claude.exe spawns a child engine process; a bare
    proc.kill() on Windows leaves that child orphaned (it keeps running and billing after a timeout or
    a user 'stop'). taskkill /T tears down the whole tree; proc.kill() is the POSIX/fallback path."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10,
            )
            return
        except Exception:  # noqa: BLE001 — fall through to a plain kill
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass

DEFAULT_MODEL = "claude-opus-4-8"
TIMEOUT_SECONDS = 1800
CLI_OVERRIDE_ENV = "HELIX_CLAUDE_CLI"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def _version_key(name: str) -> tuple:
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) if nums else (0,)


def _search_bases() -> list[Path]:
    """Dirs that may hold <version>/claude.exe — the desktop app's sandbox and real MSIX paths."""
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata) / "Claude" / "claude-code")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.extend((Path(local) / "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude-code"))
    return bases


def cli_candidates() -> list[str]:
    """Every claude.exe worth trying, best first: HELIX_CLAUDE_CLI override → the desktop app's
    copies newest-version-first → `claude` on PATH. Existing on disk is all this checks; whether a
    candidate can actually be LAUNCHED is decided by _launchable()."""
    out: list[str] = []
    override = os.environ.get(CLI_OVERRIDE_ENV)
    if override and Path(override).exists():
        out.append(override)
    found: list[Path] = []
    for base in _search_bases():
        if base.is_dir():
            found.extend(p for p in base.glob("*/claude.exe") if p.is_file())
    found.sort(key=lambda p: _version_key(p.parent.name), reverse=True)
    out.extend(str(p) for p in found)
    on_path = shutil.which("claude")
    if on_path:
        out.append(on_path)
    seen: set[str] = set()
    ordered: list[str] = []
    for p in out:  # the same exe can be reached by two routes (override == PATH); probe it once
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            ordered.append(p)
    return ordered


_PROBE_TIMEOUT_S = 20.0
_launch_ok: dict[str, bool] = {}   # normcased path -> did `--version` actually run
_launch_lock = threading.Lock()


def reset_cli_cache() -> None:
    """Forget which candidates are launchable — call after installing/updating a CLI (and in tests)."""
    with _launch_lock:
        _launch_ok.clear()


def _launch_known(path: str) -> bool | None:
    """Cached launchability without ever spawning: True/False if probed, None if not yet."""
    with _launch_lock:
        return _launch_ok.get(os.path.normcase(os.path.abspath(path)))


def _launchable(path: str) -> bool:
    """True when this claude.exe actually STARTS. Existing on disk is not enough: the desktop app is
    an MSIX package, so its bundled claude.exe lives in the package's LocalCache where a
    non-packaged process can see the file but Windows may still refuse to launch it (its dependencies
    resolve through the package graph). That surfaces as FileNotFoundError from CreateProcess — which
    the Agent SDK reports as CLINotFoundError, so HELIX used to dead-end on a path it had just
    'found'. Probe once per path per process (spawning a ~278 MB exe is not free) and cache."""
    key = os.path.normcase(os.path.abspath(path))
    with _launch_lock:
        cached = _launch_ok.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        proc = subprocess.run(
            [path, "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            timeout=_PROBE_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = proc.returncode == 0
        if not ok:
            _LOG.warning("claude CLI at %s exited %s on --version; skipping it", path, proc.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        # OSError covers the MSIX case (FileNotFoundError even though the file exists) and any
        # missing-dependency / permission failure; SubprocessError covers the probe timing out.
        _LOG.warning("claude CLI at %s cannot be launched (%s: %s); skipping it",
                     path, type(exc).__name__, exc)
    with _launch_lock:
        _launch_ok[key] = ok
    return ok


def resolve_claude_cli(*, allow_probe: bool = True) -> str | None:
    """The first claude.exe that actually launches, or None when nothing on this machine does.
    Candidates are re-listed on each call (so a newly installed CLI is picked up without a restart)
    but launchability is cached, so this costs no subprocess after the first resolution.

    `allow_probe=False` guarantees NO subprocess: it answers from the cache alone and, for a candidate
    nothing has probed yet, answers optimistically. **Qt GUI-thread callers must pass allow_probe=False**
    — probing spawns a ~278 MB exe, and a UI that blocks on that (SettingsView's "which brain is live"
    label is constructed before the first frame) freezes the window for as long as the spawn takes. The
    container warms this cache on a daemon thread at startup, so the optimistic answer is only ever used
    in the first moment after launch; a wrong guess there costs one turn that falls back to the API
    rail, never a frozen window."""
    for path in cli_candidates():
        known = _launch_known(path)
        if known is True:
            return path
        if known is None:
            if not allow_probe:
                return path            # unprobed: assume usable rather than block the caller
            if _launchable(path):
                return path
    return None


def cli_unavailable_reason(*, allow_probe: bool = True) -> str | None:
    """None when a CLI is usable; otherwise a plain sentence saying what is actually wrong. Exists so
    a CLI problem is never reported to the user as a credential problem. See resolve_claude_cli for
    what allow_probe=False means and why GUI-thread callers need it."""
    candidates = cli_candidates()
    if not candidates:
        return ("No Claude Code CLI was found on this machine. Install it with "
                "`npm install -g @anthropic-ai/claude-code`, or set HELIX_CLAUDE_CLI to a claude.exe.")
    if resolve_claude_cli(allow_probe=allow_probe) is None:
        return (f"Found {len(candidates)} Claude Code CLI(s) but none of them will start — the first "
                f"was {candidates[0]}. The desktop app's copy often can't be launched from outside "
                f"its installer package; install a standalone CLI with "
                f"`npm install -g @anthropic-ai/claude-code`, or point HELIX_CLAUDE_CLI at one.")
    return None


def _describe_tool(name: str, tool_input: dict) -> str:
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    short = path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""
    table = {
        "Edit": f"Editing {short}" if short else "Editing a file",
        "MultiEdit": f"Editing {short}" if short else "Editing files",
        "Write": f"Writing {short}" if short else "Writing a file",
        "Read": f"Reading {short}" if short else "Reading a file",
        "Bash": "Running a command",
        "Grep": "Searching the code",
        "Glob": "Looking for files",
        "TodoWrite": "Planning the build",
    }
    return table.get(name, f"Using {name}" if name else "Working")


def _describe_event(event: dict) -> str | None:
    if event.get("type") != "assistant":
        return None
    blocks = (event.get("message", {}) or {}).get("content", []) or []
    # Prefer the model's own words — the fluent, plain-language progress narration — over a mechanical
    # tool label, so the user hears "adding the camera lens", not "Editing model.js".
    for block in blocks:
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                return text.split("\n", 1)[0][:90]
    for block in blocks:
        if block.get("type") == "tool_use":
            return _describe_tool(block.get("name", ""), block.get("input", {}) or {})
    return None


class ClaudeCodeCli:
    name = "claude-code-cli"

    def __init__(
        self,
        api_key_provider: Callable[[], str | None],
        oauth_provider: Callable[[], str | None] = lambda: None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: int = TIMEOUT_SECONDS,
        cli_path: str | None = None,
    ) -> None:
        self._key_provider = api_key_provider
        self._oauth_provider = oauth_provider
        self._model = model
        self._timeout = timeout
        self._cli_path = cli_path

    def _cli(self) -> str | None:
        return self._cli_path or resolve_claude_cli()

    def _auth(self) -> tuple[str | None, str | None]:
        return (self._oauth_provider() or None, self._key_provider() or None)

    def available(self) -> bool:
        token, key = self._auth()
        return self._cli() is not None and bool(token or key)

    def run_task(
        self, repo_dir: Path, prompt: str, *, on_progress: ProgressFn | None = None, cancel=None,
        model: str | None = None,
    ) -> CoderResult:
        cli = self._cli()
        if not cli:
            return CoderResult(ok=False, summary="", error="Claude Code CLI not found.")
        token, key = self._auth()
        if not (token or key):
            return CoderResult(ok=False, summary="", error="No Claude auth (token or API key).")

        env = dict(os.environ)
        if token:
            env[OAUTH_TOKEN_ENV] = token
            env.pop("ANTHROPIC_API_KEY", None)  # prefer the subscription token
        else:
            env["ANTHROPIC_API_KEY"] = key
            env.pop(OAUTH_TOKEN_ENV, None)  # don't pass a stale token alongside the key

        cmd = [
            cli, "-p", prompt,
            "--output-format", "stream-json", "--verbose",
            "--model", (model or self._model), "--permission-mode", "acceptEdits",
            # No shell: the coder edits files via Write/Edit only. Denying Bash removes the easiest
            # path to git-hook injection, `git branch -f`, and writing outside the workspace.
            "--disallowedTools", "Bash",
        ]
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(repo_dir), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
                stdin=subprocess.DEVNULL, creationflags=no_window,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return CoderResult(ok=False, summary="", error=f"Could not run the coder: {exc}")

        # Drain stderr concurrently — otherwise a child that fills the stderr pipe buffer while we
        # block reading stdout would deadlock until the timeout killer fires.
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            try:
                for line in proc.stderr:  # type: ignore[union-attr]
                    stderr_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Killing the coder makes it exit nonzero, so the exit code alone cannot tell a crash from the
        # ceiling firing. Record the cause before the kill or the timeout reaches the user as "exited 1".
        timed_out = threading.Event()

        def _on_timeout() -> None:
            timed_out.set()
            _LOG.warning("coder exceeded its %ss ceiling; killing the process tree", self._timeout)
            _kill_tree(proc)

        killer = threading.Timer(self._timeout, _on_timeout)
        killer.start()

        # Stop watcher: if the user cancels mid-build, kill the child so its stdout closes and the read
        # loop unwinds at once (instead of waiting out the whole build).
        watch_stop = threading.Event()

        def _watch_cancel() -> None:
            while not watch_stop.wait(0.2):
                if cancel is not None and cancel.is_set():
                    _kill_tree(proc)  # kill the whole tree so no orphan child keeps running/billing
                    return

        cancel_thread = None
        if cancel is not None:
            cancel_thread = threading.Thread(target=_watch_cancel, daemon=True)
            cancel_thread.start()

        final = None
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    final = event
                elif on_progress:
                    note = _describe_event(event)
                    if note:
                        on_progress(note)
            proc.wait()
        except Exception as exc:  # a stream failure must not crash the worker
            _kill_tree(proc)  # the tree: a bare kill orphans claude.exe's engine child, still billing
            proc.wait()  # reap so we don't leave a zombie / open FDs
            return CoderResult(ok=False, summary="", error=f"Coder stream failed: {exc}")
        finally:
            killer.cancel()
            watch_stop.set()
            if cancel_thread is not None:
                cancel_thread.join(timeout=1)
            stderr_thread.join(timeout=2)

        if cancel is not None and cancel.is_set():
            return CoderResult(ok=False, summary="", error="cancelled")

        stderr = "".join(stderr_lines)

        summary = str((final or {}).get("result") or "").strip()
        is_error = bool((final or {}).get("is_error"))
        if timed_out.is_set() and not summary:
            limit = (f"{self._timeout / 60:.0f} minutes" if self._timeout >= 60
                     else f"{self._timeout:g} seconds")
            tail = stderr.strip()[-200:]
            return CoderResult(
                ok=False, summary="",
                error=f"Coder timed out after {limit} and was stopped." + (f" {tail}" if tail else ""),
            )
        if proc.returncode not in (0, None) and not summary:
            return CoderResult(
                ok=False, summary="", error=f"Coder exited {proc.returncode}: {stderr.strip()[:500]}"
            )
        if is_error and not summary:
            return CoderResult(ok=False, summary="", error="The coder reported an error.")
        return CoderResult(ok=True, summary=summary or "built")
