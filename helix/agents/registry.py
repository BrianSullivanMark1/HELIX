"""Agent registry + run loop (§agents). Settings-backed so agents persist across restarts.

An agent is a dict: {key, name, goal, trigger, enabled, created_at, last_run, last_result}. `run_agent`
drives the existing AI tool-loop (`helix/ai/actions`) toward the agent's goal — so an agent can use the
same tools the conversation can (build apps, etc.), with the same confirmation gate: anything that needs
the user's yes pauses instead of auto-running.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

AGENTS_SETTING = "agents"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-")
    return slug or "agent"


def list_agents(settings: Any) -> list[dict]:
    raw = settings.get(AGENTS_SETTING)
    return list(raw) if isinstance(raw, list) else []


def get_agent(settings: Any, key: str) -> dict | None:
    return next((a for a in list_agents(settings) if a.get("key") == key), None)


def _save(settings: Any, agents: list[dict]) -> None:
    settings.set(AGENTS_SETTING, agents)


def add_agent(settings: Any, name: str, goal: str, *, trigger: str = "manual") -> dict:
    """Create and persist an agent. Returns the stored record (with a unique key)."""
    agents = list_agents(settings)
    existing = {a.get("key") for a in agents}
    base = slugify(name)
    key, n = base, 2
    while key in existing:
        key = f"{base}-{n}"
        n += 1
    record = {
        "key": key,
        "name": name.strip() or "Agent",
        "goal": goal.strip(),
        "trigger": trigger,
        "enabled": False,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "last_run": "",
        "last_result": "",
    }
    agents.append(record)
    _save(settings, agents)
    return record


def update_agent(settings: Any, key: str, **fields) -> dict | None:
    agents = list_agents(settings)
    found = None
    for a in agents:
        if a.get("key") == key:
            a.update(fields)
            found = a
    if found is not None:
        _save(settings, agents)
    return found


def delete_agent(settings: Any, key: str) -> bool:
    agents = list_agents(settings)
    remaining = [a for a in agents if a.get("key") != key]
    if len(remaining) == len(agents):
        return False
    _save(settings, remaining)
    return True


def run_agent(settings: Any, memory: Any, agent: dict, *, on_step: Callable[[str], None] | None = None) -> dict:
    """Run an agent once through the AI tool-loop. Returns {ok, reply, pending, ran_at}.

    `pending` is set (a short description) when the agent wants to do something that needs the user's
    explicit approval — it is NOT auto-confirmed here; the agent reports what it would do instead."""
    from helix.ai.claude import ClaudeClient, ClaudeConfig, DEFAULT_CLAUDE_MODEL
    from helix.ai import actions

    ran_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    client = ClaudeClient(ClaudeConfig(model=DEFAULT_CLAUDE_MODEL, timeout_seconds=120))
    if not client.is_configured():
        return {"ok": False, "reply": "Add your Claude API key in Settings to run agents.",
                "pending": None, "ran_at": ran_at}

    def research_fn(prompt: str) -> str:
        return client.complete(prompt, max_tokens=2000)

    ctx = actions.ActionContext(
        memory=memory, settings=settings, research_fn=research_fn,
        on_progress=on_step or (lambda _m: None),
    )
    router = actions.ActionRouter(ctx)
    system = (
        "You are HELIX running as an automated agent (not a live chat). Your standing goal:\n"
        f"{agent.get('goal', '')}\n\n"
        "Take the actions needed to make progress on the goal right now, using your tools. Be concise "
        "and report plainly what you did. If an action requires the user's confirmation, do NOT assume "
        "yes — describe what you would do and stop."
    )
    messages = [{"role": "user", "content": "Run now and work toward the goal. Report what you did."}]
    try:
        result = actions.run_chat_turn(client, DEFAULT_CLAUDE_MODEL, system, messages, router, on_step=on_step)
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "reply": f"Agent run failed: {error}", "pending": None, "ran_at": ran_at}

    pending = None
    if result.pending:
        pending = f"wants to {result.pending[0].replace('_', ' ')} — needs your approval"
    reply = (result.reply or "").strip() or "(no output)"
    update_agent(settings, agent.get("key", ""), last_run=ran_at, last_result=reply)
    return {"ok": True, "reply": reply, "pending": pending, "ran_at": ran_at}
