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


def resolve_claude_cli() -> str | None:
    """HELIX_CLAUDE_CLI override → newest claude.exe from the desktop app → `claude` on PATH → None."""
    override = os.environ.get(CLI_OVERRIDE_ENV)
    if override and Path(override).exists():
        return override
    candidates: list[Path] = []
    for base in _search_bases():
        if base.is_dir():
            candidates.extend(p for p in base.glob("*/claude.exe") if p.is_file())
    if candidates:
        return str(max(candidates, key=lambda p: _version_key(p.parent.name)))
    return shutil.which("claude")


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
        self, repo_dir: Path, prompt: str, *, on_progress: ProgressFn | None = None, cancel=None
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
            "--model", self._model, "--permission-mode", "acceptEdits",
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
        killer = threading.Timer(self._timeout, proc.kill)
        killer.start()

        # Stop watcher: if the user cancels mid-build, kill the child so its stdout closes and the read
        # loop unwinds at once (instead of waiting out the whole build).
        watch_stop = threading.Event()

        def _watch_cancel() -> None:
            while not watch_stop.wait(0.2):
                if cancel is not None and cancel.is_set():
                    try:
                        proc.kill()
                    except Exception:
                        pass
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
            proc.kill()
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
        if proc.returncode not in (0, None) and not summary:
            return CoderResult(
                ok=False, summary="", error=f"Coder exited {proc.returncode}: {stderr.strip()[:500]}"
            )
        if is_error and not summary:
            return CoderResult(ok=False, summary="", error="The coder reported an error.")
        return CoderResult(ok=True, summary=summary or "built")
