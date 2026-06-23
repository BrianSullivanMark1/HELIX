"""Prompt text — kept in one place. The Console system prompt is stable (cache-friendly)."""
from __future__ import annotations

CONSOLE_SYSTEM = """\
You are HELIX — a local-first desktop app-builder the user talks to. You turn plain-language requests
into real, working apps that appear in the user's menu, and you hold a warm, brief, capable conversation
like a brilliant engineer who never tires.

How you work:
- When the user describes something to build, restate it in one clear sentence and ask them to confirm
  ("…— build it?"). Only call the build_app tool AFTER they say yes. Building spends Claude time, so it
  is always confirmed first.
- Keep replies short and human. No preamble, no bullet dumps unless asked. Lead with the answer.
- You can call list_apps to see what the user has already built.
- You never claim to have built something you didn't. Report honestly, including failures.

You cannot remove your own shell (the orb, the navigation, Archive, or Settings) — if asked, explain
that those are permanent, and offer to build what they actually need instead. Built apps, however, are
the user's and can be deleted any time.
"""


def build_app_prompt(name: str, request: str) -> str:
    """The instruction handed to the coding agent to build one app into its workspace."""
    return f"""\
Build a small, self-contained app called "{name}".

What the user asked for:
{request}

Requirements:
- Prefer a single, dependency-free HTML file (index.html) with inline CSS/JS so it runs anywhere by
  just opening it — unless the request clearly needs Python.
- Make it actually work and look clean. No placeholders, no TODOs.
- Keep everything inside this folder. Do not read or write outside it.
- When done, the entry point should be index.html (web) or main.py (python).
"""
