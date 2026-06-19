"""The self-improvement *coder*: HELIX edits its own code with Opus 4.8 via the Claude Code CLI.

HELIX runs the same `claude.exe` that ships with the Claude desktop app — but headless (`-p`), as an
independent subprocess authenticated by HELIX's own Anthropic API key. (The interactive desktop login
does NOT carry into a subprocess, so the key is required; billing is per-token to the API account.)
The work happens on a throwaway `selfdev/*` branch (see `gitops`), so the change is fully reviewable
and reversible before anything touches `main` — the coder commits to the branch, then switches the
working tree back to the deployed branch so a restart can never load unapproved code.

Edge / I-O module (subprocess + filesystem). The git plumbing lives in `gitops.py`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from helix.core.config import load_config
from helix.core.settings import AppSettings
from helix.selfdev import gitops

DEFAULT_CODER_MODEL = "claude-opus-4-8"   # Opus 4.8 — the coding brain, at Brian's direction
CODER_TIMEOUT_SECONDS = 1800              # a real coding task can take a few minutes
CLAUDE_CLI_OVERRIDE_ENV = "HELIX_CLAUDE_CLI"
CLAUDE_API_KEY_SETTING = "claude_api_key"  # mirrors the rest of HELIX (DESIGN.md §5)
CLAUDE_OAUTH_TOKEN_SETTING = "claude_code_oauth_token"  # subscription auth from `claude setup-token`
CLAUDE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def _version_key(name: str) -> tuple:
    nums = re.findall(r"\d+", name)
    return tuple(int(n) for n in nums) if nums else (0,)


def _claude_search_bases() -> list[Path]:
    """Directories that may hold <version>/claude.exe. The desktop app is MSIX-packaged, so its files
    appear at %APPDATA%\\Claude\\claude-code inside the package sandbox but at the real
    %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude\\claude-code from a normal process
    (which is how HELIX runs). We check both so resolution works in either context."""
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata) / "Claude" / "claude-code")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        bases.extend((Path(local) / "Packages").glob("Claude_*/LocalCache/Roaming/Claude/claude-code"))
    return bases


def resolve_claude_cli() -> str | None:
    """Locate the Claude Code CLI executable.

    Order: the HELIX_CLAUDE_CLI override -> the newest `claude.exe` from the desktop app's install
    dirs (sandbox + real MSIX paths, see `_claude_search_bases`) -> `claude` on PATH. None if missing.
    """
    override = os.environ.get(CLAUDE_CLI_OVERRIDE_ENV)
    if override and Path(override).exists():
        return override
    candidates: list[Path] = []
    for base in _claude_search_bases():
        if base.is_dir():
            candidates.extend(p for p in base.glob("*/claude.exe") if p.is_file())
    if candidates:
        return str(max(candidates, key=lambda p: _version_key(p.parent.name)))
    return shutil.which("claude")


def resolve_api_key(explicit: str | None = None) -> str | None:
    """arg -> ANTHROPIC_API_KEY env -> saved `claude_api_key` setting (mirrors ClaudeClient's order)."""
    if explicit:
        return explicit
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    try:
        return AppSettings().get(CLAUDE_API_KEY_SETTING) or None
    except Exception:
        return None


def resolve_oauth_token(explicit: str | None = None) -> str | None:
    """Subscription auth: arg -> CLAUDE_CODE_OAUTH_TOKEN env -> saved setting (from `claude setup-token`).

    Preferred over the API key so self-coding bills the Claude subscription (e.g. the Enterprise plan's
    large limit window), not the per-token API console."""
    if explicit:
        return explicit
    env = os.environ.get(CLAUDE_OAUTH_TOKEN_ENV)
    if env:
        return env
    try:
        return AppSettings().get(CLAUDE_OAUTH_TOKEN_SETTING) or None
    except Exception:
        return None


@dataclass(frozen=True)
class CoderResult:
    """The outcome of one self-coding run. On success the change sits committed on `branch`, ready for
    review/approval; the working tree has been returned to the base (deployed) branch."""
    ok: bool
    task: str
    branch: str | None = None
    base: str | None = None
    commit: str | None = None
    summary: str = ""
    changed_files: tuple[str, ...] = ()
    diff: str = ""
    diffstat: str = ""
    cost_usd: float | None = None
    error: str = ""


def branch_name(task: str, *, now: datetime | None = None) -> str:
    """A readable, unique work-branch name: selfdev/<slug>-<MMDD-HHMMSS>."""
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40].strip("-") or "task"
    stamp = (now or datetime.now()).strftime("%m%d-%H%M%S")
    return f"selfdev/{slug}-{stamp}"


def build_coder_prompt(task: str) -> str:
    """The instruction handed to the headless Opus 4.8 coder."""
    return (
        "You are improving HELIX, a local-first personal AI desktop app (Python 3.11 + PyQt6). "
        "The repository is your current working directory and you are already on a throwaway work "
        "branch, so edit files freely.\n\n"
        f"TASK:\n{task.strip()}\n\n"
        "Rules:\n"
        "- Keep the change minimal and consistent with the existing code. DESIGN.md is the source of "
        "truth for architecture and conventions (stdlib-first; pure core with I/O at the edges; "
        "`from __future__ import annotations` at the top of every module).\n"
        "- Do not break imports or existing behavior. Keep any new feature self-contained.\n"
        "- Do NOT run `git commit`, `git push`, or any git command — HELIX handles version control. "
        "Just edit the files.\n"
        "- Never modify the `data/` directory, secrets, or API keys.\n"
        "- When done, briefly summarize what you changed and why, in a few sentences."
    )


def _parse_cli_json(stdout: str) -> tuple[str, float | None, bool]:
    """Pull (summary, cost_usd, is_error) out of `claude -p --output-format json` output.

    Tolerant: handles a single JSON object, a stream of JSON lines (takes the last object), or plain
    text (returned as the summary)."""
    raw = (stdout or "").strip()
    if not raw:
        return "", None, False
    obj = None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(obj, dict):
        return raw[:4000], None, False
    summary = str(obj.get("result") or obj.get("text") or "").strip()
    cost = obj.get("total_cost_usd")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return summary, cost, bool(obj.get("is_error"))


def _describe_tool(name: str, tool_input: dict) -> str:
    """A short, human 'what Opus is doing now' line from a Claude Code tool-use event."""
    path = str(tool_input.get("file_path") or tool_input.get("path") or "")
    short = path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""
    table = {
        "Edit": f"Editing {short}" if short else "Editing a file",
        "MultiEdit": f"Editing {short}" if short else "Editing files",
        "Write": f"Writing {short}" if short else "Writing a file",
        "Read": f"Reading {short}" if short else "Reading a file",
        "NotebookEdit": f"Editing {short}" if short else "Editing a notebook",
        "Bash": "Running a command",
        "Grep": "Searching the code",
        "Glob": "Looking for files",
        "TodoWrite": "Planning the change",
        "WebFetch": "Reading a page",
        "WebSearch": "Searching the web",
    }
    return table.get(name, f"Using {name}" if name else "Working")


def _describe_event(event: dict) -> str | None:
    """Turn a streamed Claude Code event into a one-line progress note, or None if not worth showing."""
    if event.get("type") != "assistant":
        return None
    for block in (event.get("message", {}) or {}).get("content", []) or []:
        if block.get("type") == "tool_use":
            return _describe_tool(block.get("name", ""), block.get("input", {}) or {})
        if block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text:
                return "Opus: " + text.split("\n", 1)[0][:90]
    return None


def run_coding_task(
    task: str,
    *,
    repo_dir: str | None = None,
    api_key: str | None = None,
    oauth_token: str | None = None,
    model: str = DEFAULT_CODER_MODEL,
    branch: str | None = None,
    timeout: int = CODER_TIMEOUT_SECONDS,
    cli_path: str | None = None,
    on_step: Callable[[str], None] | None = None,
) -> CoderResult:
    """Have Opus 4.8 implement `task` on a fresh `selfdev/*` branch and return the proposed change.

    Safe + reversible by construction: aborts unless the working tree is clean, does all work on a new
    branch, and — on success — commits to that branch then switches back to the base branch so the
    deployed (on-disk) code is unchanged until an explicit approval merges it. On any failure the
    branch is deleted and the tree restored.
    """
    def step(msg: str) -> None:
        if on_step:
            try:
                on_step(msg)
            except Exception:
                pass

    repo = repo_dir or str(load_config().root_dir)
    if not gitops.is_git_repo(repo):
        return CoderResult(False, task, error=f"{repo} is not a git repository.")
    cli = cli_path or resolve_claude_cli()
    if not cli:
        return CoderResult(False, task, error="Claude Code CLI not found (set HELIX_CLAUDE_CLI).")
    token = resolve_oauth_token(oauth_token)   # subscription auth (preferred)
    key = resolve_api_key(api_key)             # API-console auth (fallback)
    if not token and not key:
        return CoderResult(False, task, error="No Claude auth: run `claude setup-token`, or save an Anthropic API key.")
    if not gitops.is_clean(repo):
        return CoderResult(False, task, error="Working tree has uncommitted changes; commit or stash first.")

    base = gitops.current_branch(repo)
    work = branch or branch_name(task)
    step(f"Creating work branch {work}")
    try:
        gitops.create_work_branch(repo, work)
    except gitops.GitError as exc:
        return CoderResult(False, task, base=base, error=f"Could not create branch: {exc}")

    def abort(message: str, *, summary: str = "", cost: float | None = None) -> CoderResult:
        try:
            gitops.switch(repo, base)
            gitops.delete_branch(repo, work)
        except gitops.GitError:
            pass
        return CoderResult(False, task, base=base, summary=summary, cost_usd=cost, error=message)

    env = dict(os.environ)
    if token:
        env[CLAUDE_OAUTH_TOKEN_ENV] = token
        env.pop("ANTHROPIC_API_KEY", None)  # prefer the subscription token; don't let an API key override it
    else:
        env["ANTHROPIC_API_KEY"] = key
    # Stream the run so HELIX can show live progress ("Reading X", "Editing Y") instead of a black box.
    cmd = [cli, "-p", build_coder_prompt(task), "--output-format", "stream-json", "--verbose",
           "--model", model, "--permission-mode", "acceptEdits"]
    step("Opus 4.8 is working on the change…")
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't pop a console when HELIX runs windowless
    try:
        proc = subprocess.Popen(
            cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=env,
            stdin=subprocess.DEVNULL, creationflags=no_window,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return abort(f"Could not run the coder: {exc}")
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    final_event = None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                final_event = event
            else:
                note = _describe_event(event)
                if note:
                    step(note)
        proc.wait()
    except Exception as exc:  # noqa: BLE001 — a stream failure should abort cleanly, not crash
        proc.kill()
        return abort(f"Coder stream failed: {exc}")
    finally:
        killer.cancel()
    try:
        stderr_text = proc.stderr.read() or ""
    except Exception:
        stderr_text = ""
    if final_event is not None:
        summary = str(final_event.get("result") or "").strip()
        raw_cost = final_event.get("total_cost_usd")
        try:
            cost = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            cost = None
        is_error = bool(final_event.get("is_error"))
    else:
        summary, cost, is_error = "", None, False
    if (proc.returncode not in (0, None)) and not summary:
        return abort(f"Coder exited {proc.returncode}: {(stderr_text or 'the coder failed').strip()[:1000]}")

    files = gitops.changed_files(repo)
    if not files:
        return abort("The coder made no file changes.", summary=summary, cost=cost)

    step("Committing the proposed change to the branch")
    try:
        gitops.stage_all(repo)
        diff = gitops.staged_diff(repo)
        diffstat = gitops.staged_diffstat(repo)
        message = f"selfdev: {task.strip()[:64]}".strip()
        if summary:
            message += f"\n\n{summary}"
        commit = gitops.commit_all(repo, message)
        gitops.switch(repo, base)  # leave the deployed branch checked out; work waits on selfdev/* until approved
    except gitops.GitError as exc:
        return abort(f"Could not commit the change: {exc}", summary=summary, cost=cost)

    if is_error:
        # The CLI reported an error but still produced edits — surface it, keep the branch for review.
        step("Coder reported a problem, but changes were captured for review")

    return CoderResult(
        ok=True, task=task, branch=work, base=base, commit=commit, summary=summary,
        changed_files=tuple(files), diff=diff, diffstat=diffstat, cost_usd=cost,
    )
