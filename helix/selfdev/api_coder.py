"""API-based fallback coder — build an app with the Anthropic API directly, no Claude Code CLI.

When the Claude Code CLI isn't installed, this lets HELIX still build apps with only the user's
Anthropic API key: a small, sandboxed agentic loop with file tools (write/read/list) scoped to the
build workspace. Less capable than the CLI (no shell, no codebase search), but enough for the small,
self-contained apps HELIX targets — a single HTML file, or a short stdlib script.

Returns a `coder.CoderResult` so callers treat the CLI and API paths identically. This module only
WRITES files into the workspace; the caller (`builds.build_app`) handles git.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from helix.ai.claude import ClaudeClient, ClaudeConfig, DEFAULT_CLAUDE_MODEL, estimate_cost
from helix.selfdev.coder import CoderResult

# Files the agent must never touch — HELIX's own workspace bookkeeping.
_PROTECTED_NAMES = {".git", ".helixbuild.json"}

FILE_TOOLS = [
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the app folder. Use a relative path like "
                       "'index.html' or 'src/main.py'. Writing a file replaces its whole contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the app folder."},
                "content": {"type": "string", "description": "The complete file contents."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file you've already written, by relative path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": "List the files currently in the app folder.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "done",
        "description": "Call this when the app is complete and runnable. Provide a short summary of "
                       "what you built and how to run it.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]

_SYSTEM = (
    "You are HELIX's app-builder, writing a brand-new standalone app into a dedicated folder using the "
    "file tools provided. There is no existing code. Build the simplest thing that genuinely satisfies "
    "the request and actually runs. Strongly prefer a SINGLE self-contained file with no third-party "
    "dependencies — a single index.html (HTML+CSS+JS) is ideal; otherwise a single Python stdlib "
    "script. Always also write a short README.md saying what it does and how to run it. Never write "
    "outside the folder, never use absolute or parent (..) paths, and never touch .git or "
    ".helixbuild.json. When everything is written and runnable, call the done tool with a summary."
)


def _safe_target(workspace: Path, rel: str) -> Path:
    """Resolve `rel` inside the workspace, rejecting escapes and protected names."""
    rel = (rel or "").strip().lstrip("/\\")
    if not rel:
        raise ValueError("empty path")
    target = (workspace / rel).resolve()
    ws = workspace.resolve()
    if target != ws and ws not in target.parents:
        raise ValueError("path escapes the app folder")
    parts = Path(rel).parts
    if parts and parts[0] in _PROTECTED_NAMES:
        raise ValueError("that file is protected")
    return target


def _run_tool(name: str, tool_input: dict, workspace: Path, written: list) -> str:
    try:
        if name == "write_file":
            target = _safe_target(workspace, str(tool_input.get("path", "")))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(tool_input.get("content", "")), encoding="utf-8")
            rel = str(target.relative_to(workspace.resolve())).replace("\\", "/")
            if rel not in written:
                written.append(rel)
            return f"wrote {rel}"
        if name == "read_file":
            target = _safe_target(workspace, str(tool_input.get("path", "")))
            if not target.exists():
                return "(file does not exist yet)"
            return target.read_text(encoding="utf-8", errors="replace")[:8000]
        if name == "list_files":
            files = [
                str(p.relative_to(workspace)).replace("\\", "/")
                for p in workspace.rglob("*")
                if p.is_file() and ".git" not in p.parts
            ]
            return "\n".join(sorted(files)) or "(empty)"
        if name == "done":
            return "ok"
    except Exception as error:  # noqa: BLE001 — a tool error is fed back to the model, not fatal
        return f"error: {error}"
    return f"(unknown tool {name})"


def run_build(
    prompt: str,
    workspace: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_CLAUDE_MODEL,
    on_step: Callable[[str], None] | None = None,
    max_iters: int = 24,
) -> CoderResult:
    """Drive the model to write an app into `workspace`. Returns a CoderResult (no git — caller commits)."""
    def step(msg: str) -> None:
        if on_step:
            try:
                on_step(msg)
            except Exception:
                pass

    ws = Path(workspace)
    client = ClaudeClient(ClaudeConfig(model=model, timeout_seconds=120), api_key=api_key)
    if not client.is_configured():
        return CoderResult(False, prompt, error="No Claude API key — add one in Settings to build apps.")

    messages: list = [{"role": "user", "content": prompt}]
    written: list = []
    summary = ""
    cost = 0.0
    step("Claude is writing your app…")
    for _ in range(max_iters):
        try:
            body = client.chat(messages, system=_SYSTEM, tools=FILE_TOOLS, max_tokens=8000, model=model)
        except Exception as error:  # noqa: BLE001
            return CoderResult(False, prompt, summary=summary, error=f"Build call failed: {error}")
        usage = client.last_usage or {}
        cost += estimate_cost(model, int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0))
        blocks = body.get("content", []) or []
        text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text" and b.get("text")).strip()
        tool_uses = [b for b in blocks if b.get("type") == "tool_use"]

        if body.get("stop_reason") != "tool_use" or not tool_uses:
            if text:
                summary = summary or text
            break

        messages.append({"role": "assistant", "content": blocks})
        results = []
        finished = False
        for use in tool_uses:
            name = use.get("name", "")
            inp = use.get("input") or {}
            if name == "write_file":
                step(f"Writing {str(inp.get('path', 'a file'))}")
            elif name == "done":
                summary = str(inp.get("summary", "")) or summary
                finished = True
            out = _run_tool(name, inp, ws, written)
            results.append({"type": "tool_result", "tool_use_id": use.get("id", ""), "content": out})
        messages.append({"role": "user", "content": results})
        if finished:
            break

    if not written:
        return CoderResult(False, prompt, summary=summary, cost_usd=cost,
                           error="The builder didn't write any files.")
    return CoderResult(
        ok=True, task=prompt, summary=summary, changed_files=tuple(written), cost_usd=round(cost, 6),
    )
