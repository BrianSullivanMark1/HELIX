"""ToolRegistry — the model's hands. Maps tool calls to service methods."""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from helix.domain.errors import BuildError
from helix.domain.events import BuildDeleteRequested, BuildRenamed
from helix.domain.models import BuildKind, slugify
from helix.ports.coder import ProgressFn
from helix.ports.events import EventBus
from helix.ports.llm import ToolSpec
from helix.services.builds import BuildService
from helix.services.forge import ForgeService
from helix.services.prompts import build_3d_model_prompt, build_task_prompt
from helix.services.selfdev import SelfDevService

if TYPE_CHECKING:  # AgentService -> ConversationService -> ToolRegistry would be a runtime import cycle
    from helix.services.agents import AgentService
    from helix.services.build_queue import BuildQueue
    from helix.services.connections import ConnectionsService
    from helix.services.knowledge import KnowledgeService
    from helix.services.tasks import TaskService

# Escalation: hand a hard question to a deeper model and get back its spoken answer. The third arg is an
# optional cancel token so a 'stop' interrupts the (expensive) deep-think call.
DeepThink = Callable[[str, ProgressFn | None, object], str]


def _enqueued_msg(name: str, ahead: int, label: str) -> str:
    """The terse acknowledgement the model relays after a build is enqueued. label: '', 'task', 'model'."""
    thing = f"the {name} {label}".rstrip() if label else name
    if ahead == 0:
        return f"Starting {thing} now."
    if ahead == 1:
        return f"Queued {thing} — it'll run right after the current build."
    return f"Queued {thing} — {ahead} builds ahead of it."


class ToolRegistry:
    def __init__(
        self,
        forge: ForgeService,
        builds: BuildService,
        selfdev: SelfDevService | None = None,
        deep_think: DeepThink | None = None,
        agents: "AgentService | None" = None,
        queue: "BuildQueue | None" = None,
        tasks: "TaskService | None" = None,
        bus: EventBus | None = None,
        selfdev_lane=None,
        connections: "ConnectionsService | None" = None,
        knowledge: "KnowledgeService | None" = None,
    ) -> None:
        self._forge = forge
        self._builds = builds
        self._selfdev = selfdev
        self._deep_think = deep_think
        self._agents = agents
        self._queue = queue
        self._tasks = tasks
        self._bus = bus
        self._selfdev_lane = selfdev_lane  # background drafting of self-changes (no orb freeze)
        self._connections = connections  # read-only call_api to connected services (Slack, GitHub, …)
        self._knowledge = knowledge  # the user's searchable notes/documents (create/remember/search)

    def bind_agents(self, agents: "AgentService") -> None:
        """Wire the agent store after construction (it depends on ConversationService, which depends on
        this registry — so it can't be passed in at build time). Enables create_agent."""
        self._agents = agents

    def specs(self) -> list[ToolSpec]:
        tools = [
            ToolSpec(
                name="build_app",
                description=(
                    "Build a new app from a plain-language description and add it to the user's menu. "
                    "Only call this AFTER the user has confirmed they want it built — building spends "
                    "Claude time."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "A short, human app name, e.g. 'Tip Calculator'.",
                        },
                        "request": {
                            "type": "string",
                            "description": "The full plain-language description of what to build.",
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="build_3d_model",
                description=(
                    "Conjure an interactive 3D model to SHOW the user what you're discussing — a device, "
                    "a part, a layout, a concept. It opens in their browser; they orbit and explore it. "
                    "Use this to visualize an idea when a picture communicates faster than words. To "
                    "CHANGE a model, call this again with the SAME name and the change (e.g. 'make it "
                    "taller', 'show the inside') and HELIX updates that model in place. Only call after "
                    "the user confirms — building spends Claude time, like build_app."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A short, human name for the model, e.g. 'Wall Camera Unit'. Reuse the "
                                "exact same name to modify an existing model."
                            ),
                        },
                        "request": {
                            "type": "string",
                            "description": (
                                "Plain-language description of what to visualize — or, when modifying an "
                                "existing model, the change to make."
                            ),
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="build_task",
                description=(
                    "Build a TASK — a small program that DOES A THING when run (a script, an automation, "
                    "a converter, a generator) instead of opening a screen. It runs in its own console "
                    "and lands in the Tasks tab; the user runs it on demand. Use this when they want an "
                    "action performed repeatably, not an interactive app. To CHANGE a task, call this "
                    "again with the SAME name and the change. Only call AFTER the user confirms — "
                    "building spends Claude time, like build_app."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "A short, human name for the task, e.g. 'Rename Downloads'.",
                        },
                        "request": {
                            "type": "string",
                            "description": (
                                "Plain-language description of what the task should do — or, when "
                                "modifying an existing task, the change to make."
                            ),
                        },
                    },
                    "required": ["name", "request"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="list_apps",
                description="List the apps the user has already built.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            ToolSpec(
                name="delete_build",
                description=(
                    "Permanently delete something the user made — an app, a task, a 3D model, or an "
                    "agent — by its name. Use when the user clearly asks to remove or delete one of "
                    "their builds. This cannot be undone. HELIX will ask the user to confirm with one "
                    "click before anything is removed, so call this only when they've asked to delete it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the build or agent to delete.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="rename_build",
                description=(
                    "Rename something the user made — an app, a task, a 3D model, or an agent — to a new "
                    "name, by talking. Use when the user asks to rename or 'call it …' one of their "
                    "builds. The build keeps everything else; only its display name changes."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The current name of the build or agent."},
                        "new_name": {"type": "string", "description": "The new name to give it."},
                    },
                    "required": ["name", "new_name"],
                    "additionalProperties": False,
                },
            ),
        ]
        if self._tasks is not None:
            tools.append(
                ToolSpec(
                    name="run_task",
                    description=(
                        "Run one of the user's TASKS by name — launch the script so it does its thing. "
                        "Use when the user asks to run/start a task they built. It opens in its own "
                        "console; report that you've launched it."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "The task to run."}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._agents is not None:
            tools.append(
                ToolSpec(
                    name="run_agent",
                    description=(
                        "Run one of the user's saved AGENTS by name now and relay its result. Use when "
                        "the user asks to run an agent (e.g. 'run my morning brief'). The agent works "
                        "autonomously (it can read, think, search, and report, but not build or change "
                        "things); summarize what it found briefly in your own voice."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "The agent to run."}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._queue is not None:
            tools += [
                ToolSpec(
                    name="list_builds",
                    description=(
                        "Report what's building right now and what's queued behind it. READ-ONLY — use "
                        "it to answer 'what are you doing', 'how's it going', 'what's in the queue'. It "
                        "never starts, stops, pauses, or reorders anything. A build runs in the "
                        "background, so call this to give an honest status without disturbing the work."
                    ),
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                ),
                ToolSpec(
                    name="prioritize_build",
                    description=(
                        "Move a QUEUED build to the front so it runs next. Use when the user wants a "
                        "waiting build done sooner ('do the to-do list first'). You cannot reorder the "
                        "one already running — if they name that, say it's already mid-build."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "The queued build to bump up."}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                ),
                ToolSpec(
                    name="cancel_build",
                    description=(
                        "Cancel a build that is queued or currently running, by name. Use when the user "
                        "wants to stop a specific in-progress or waiting build (not delete a finished "
                        "one — that's delete_build). Confirm if it's the one actively building, since "
                        "partial work may be discarded."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "The build to cancel."}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                ),
            ]
        if self._deep_think is not None:
            tools.append(
                ToolSpec(
                    name="think_harder",
                    description=(
                        "Escalate a genuinely hard question to a more capable, deeper-thinking model and "
                        "get back its answer. Use ONLY when the question needs real reasoning, comparison, "
                        "planning, or careful analysis — not for quick facts, chit-chat, or builds. Pass "
                        "the FULL question with any needed context; the deep model can't see this "
                        "conversation. Then relay its answer briefly in your own voice."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": (
                                    "The complete question to reason about, including all relevant "
                                    "context from the conversation."
                                ),
                            }
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._connections is not None:
            tools.append(
                ToolSpec(
                    name="call_api",
                    description=(
                        "Read live data from a service the user has CONNECTED (Slack, GitHub) by GETting "
                        "one of its API URLs — HELIX attaches the user's saved token for you. Use it to "
                        "answer questions about their accounts: recent Slack messages, open GitHub PRs or "
                        "issues, statuses, etc. Pass the full https API URL (e.g. "
                        "'https://slack.com/api/conversations.list' or "
                        "'https://api.github.com/user/repos'). READ-ONLY (GET only) and limited to "
                        "connected services — it cannot reach anything else or change anything. If it says "
                        "a service isn't connected, tell the user to add its token in Settings → "
                        "Connections; never ask them to paste a token into the chat."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "url": {
                                "type": "string",
                                "description": "The full https API URL to GET from a connected service.",
                            }
                        },
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                )
            )
        if self._knowledge is not None:
            tools += [
                ToolSpec(
                    name="search_knowledge",
                    description=(
                        "Search the user's OWN saved knowledge — the notes and documents they've kept in "
                        "HELIX — and read back the most relevant passages. Use this whenever the answer "
                        "might live in something they saved (their notes, their docs, 'what did I write "
                        "about X', a personal fact like a password or address they told you to remember). "
                        "READ-ONLY. Pass a focused query; optionally name one base to search just it. "
                        "Then answer from what comes back in your own words; if it doesn't actually "
                        "answer, say so and offer to look elsewhere."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What to look for in the user's saved knowledge.",
                            },
                            "knowledge": {
                                "type": "string",
                                "description": "Optional: the name of one knowledge base to search. "
                                "Omit to search across all of them.",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                ),
                ToolSpec(
                    name="create_knowledge",
                    description=(
                        "Create a KNOWLEDGE base — a named collection of the user's notes and documents "
                        "that HELIX and its agents can later search. Use when the user wants to start a "
                        "place to keep things ('make a knowledge base for my recipes', 'start a notes "
                        "collection'). You can seed it with a first note. Creating it is instant and costs "
                        "nothing. Reuse the SAME name to refer to an existing base. Confirm once first, "
                        "like the other builds."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "A short name for the base, e.g. 'Recipes' or 'Work notes'.",
                            },
                            "note": {
                                "type": "string",
                                "description": "Optional first note to save into the new base.",
                            },
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                ),
                ToolSpec(
                    name="remember",
                    description=(
                        "Save a note into the user's knowledge so it can be recalled later. Use when the "
                        "user tells you to remember or note something ('remember the wifi password is …', "
                        "'note that the meeting moved to Friday'). Optionally name which base to file it "
                        "under; otherwise it goes to their default Notes. Saving is instant. This WRITES, "
                        "so only do it when the user asks you to remember/save something."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "note": {
                                "type": "string",
                                "description": "The note to save, in the user's words.",
                            },
                            "knowledge": {
                                "type": "string",
                                "description": "Optional: the name of the base to save it in.",
                            },
                        },
                        "required": ["note"],
                        "additionalProperties": False,
                    },
                ),
            ]
        if self._selfdev is not None:
            tools.append(
                ToolSpec(
                    name="improve_helix",
                    description=(
                        "Propose an improvement to HELIX's OWN code (how HELIX looks or works). This "
                        "DRAFTS the change on a branch — it never applies on its own, and it can never "
                        "remove HELIX's shell or safety code. After drafting, tell the user they can say "
                        "'apply it' to ship the change or 'discard it' to drop it. Only call after the "
                        "user confirms, like build_app."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "request": {
                                "type": "string",
                                "description": "Plain-language description of the change to HELIX itself.",
                            }
                        },
                        "required": ["request"],
                        "additionalProperties": False,
                    },
                )
            )
            tools.append(
                ToolSpec(
                    name="list_self_changes",
                    description=(
                        "List the drafted changes to HELIX's own code that are waiting for the user to "
                        "apply or discard. READ-ONLY. Use to answer 'what changes are pending'."
                    ),
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                )
            )
            tools.append(
                ToolSpec(
                    name="approve_self_change",
                    description=(
                        "Apply a drafted change to HELIX's own code that is waiting — this merges it (after "
                        "an automatic safety + compile check) and the user then restarts to load it. Only "
                        "call when the user explicitly says to apply/ship it. If there are several pending, "
                        "pass which one; if exactly one is pending, you may omit it."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "which": {
                                "type": "string",
                                "description": "Which drafted change to apply (its id/branch). Optional "
                                "when only one is pending.",
                            }
                        },
                        "additionalProperties": False,
                    },
                )
            )
            tools.append(
                ToolSpec(
                    name="reject_self_change",
                    description=(
                        "Discard a drafted change to HELIX's own code without applying it. Call when the "
                        "user says to drop/discard it. Pass which one; omit when only one is pending."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "which": {
                                "type": "string",
                                "description": "Which drafted change to discard (its id/branch). Optional "
                                "when only one is pending.",
                            }
                        },
                        "additionalProperties": False,
                    },
                )
            )
        if self._agents is not None:
            tools.append(
                ToolSpec(
                    name="create_agent",
                    description=(
                        "Save an AGENT — a standing goal the user can run on demand (a morning brief, a "
                        "recurring check, a routine). Use when the user describes a repeatable job they "
                        "want to keep and re-run later, not a one-off. Creating it is instant and costs "
                        "nothing (running it later does the work). Reuse the SAME name to update an "
                        "agent's goal. Confirm once first, like the other builds."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "A short name for the agent, e.g. 'Morning Brief'.",
                            },
                            "goal": {
                                "type": "string",
                                "description": "What HELIX should do each time the user runs this agent.",
                            },
                        },
                        "required": ["name", "goal"],
                        "additionalProperties": False,
                    },
                )
            )
        return tools

    def dispatch(self, name: str, args: dict, *, on_progress: ProgressFn | None = None, cancel=None) -> str:
        # Builds run in the BACKGROUND via the queue: enqueue and return a fast acknowledgement so the
        # orb keeps talking. Completion is announced separately (BuildFinished), never from this return.
        if name == "build_app" and self._queue is not None:
            ahead = self._queue.enqueue(args["name"], args["request"], kind=BuildKind.APP)
            return _enqueued_msg(args["name"], ahead, "")
        if name == "build_3d_model" and self._queue is not None:
            ahead = self._queue.enqueue(
                args["name"], args["request"], kind=BuildKind.MODEL,
                prompt=build_3d_model_prompt(args["name"], args["request"]),
            )
            return _enqueued_msg(args["name"], ahead, "model")
        if name == "build_task" and self._queue is not None:
            ahead = self._queue.enqueue(
                args["name"], args["request"], kind=BuildKind.TASK,
                prompt=build_task_prompt(args["name"], args["request"]),
            )
            return _enqueued_msg(args["name"], ahead, "task")
        if name == "list_builds" and self._queue is not None:
            return self._queue.status_line()
        if name == "prioritize_build" and self._queue is not None:
            target = args["name"]
            if self._queue.move_first(target):
                return f"Moved {target} to the front — it runs next."
            if self._queue.is_active_named(target):
                return f"{target} is already building — can't reorder the one in progress."
            return f"I don't see {target} in the queue."
        if name == "cancel_build" and self._queue is not None:
            target = args["name"]
            if self._queue.cancel_queued(target):
                return f"Dropped {target} from the queue."
            if self._queue.cancel_active_named(target):
                return f"Stopping {target}."
            return f"I don't see {target} building or queued."
        if name == "think_harder" and self._deep_think is not None:
            return self._deep_think(args["question"], on_progress, cancel)
        if name == "call_api" and self._connections is not None:
            return self._connections.call_api(args.get("url", ""))
        if name == "search_knowledge" and self._knowledge is not None:
            return self._knowledge.search(args.get("query", ""), args.get("knowledge"))
        if name == "create_knowledge" and self._knowledge is not None:
            try:
                base = self._knowledge.create(args["name"])
            except BuildError as exc:
                return str(exc)  # a friendly cross-kind-name-clash message, not a tool error
            note = (args.get("note") or "").strip()
            seeded = note and self._knowledge.add_note(base.slug, note) is not None
            extra = " Saved your first note." if seeded else ""
            return (
                f"Created the knowledge base '{base.name}'.{extra} Tell me to remember things and I'll "
                "keep them here."
            )
        if name == "remember" and self._knowledge is not None:
            return self._knowledge.remember(args.get("note", ""), args.get("knowledge"))
        if name == "list_apps":
            apps = self._builds.list()
            if not apps:
                return "No apps built yet."

            def clean(text: str) -> str:  # collapse the (untrusted) request to a one-line label
                return " ".join(text.split())[:140]

            # Include each build's kind so the model reuses the matching build_* verb to iterate and never
            # forks a near-duplicate by guessing the wrong kind.
            return "\n".join(f"- {a.name} [{a.build_kind.value}]: {clean(a.request)}" for a in apps)
        if name == "rename_build":
            return self._rename(args["name"], args.get("new_name", ""))
        if name == "run_task" and self._tasks is not None:
            task = self._tasks.find(args["name"])
            if task is None:
                return f"I don't see a task called '{args['name']}'."
            return f"Running '{task.name}'." if self._tasks.run(task.slug) else f"Couldn't launch '{task.name}'."
        if name == "run_agent" and self._agents is not None:
            target = args["name"].strip().lower()
            agent = next((a for a in self._agents.list() if a.name.strip().lower() == target), None)
            if agent is None:
                return f"I don't see an agent called '{args['name']}'."
            return self._agents.run(agent.name, on_progress=on_progress)
        if name == "improve_helix" and self._selfdev is not None:
            if self._selfdev_lane is not None:
                # Draft in the BACKGROUND so the orb isn't frozen for the (long) coder run; HELIX announces
                # when it's ready to apply. One draft at a time.
                if self._selfdev_lane.busy():
                    return "I'm still drafting the last change — one at a time. Try again once it's done."
                self._selfdev_lane.start(args["request"])
                return (
                    "On it — drafting that change in the background. I'll tell you when it's ready; then "
                    "say 'apply it' to ship it or 'discard it' to drop it."
                )
            change = self._selfdev.propose(args["request"], on_progress=on_progress)  # synchronous fallback
            return (
                f"Drafted the change ({change.summary or change.branch}). Say 'apply it' to ship it "
                "(I'll safety-check and merge it, then you restart) or 'discard it' to drop it. It won't "
                "apply until you say so."
            )
        if name == "list_self_changes" and self._selfdev is not None:
            pend = self._selfdev.pending()
            if not pend:
                return "No drafted changes to HELIX are waiting."
            return "Drafted changes waiting:\n" + "\n".join(f"- {p.id}: {p.summary}" for p in pend)
        if name == "approve_self_change" and self._selfdev is not None:
            return self._approve_self(args.get("which"))
        if name == "reject_self_change" and self._selfdev is not None:
            return self._reject_self(args.get("which"))
        if name == "create_agent" and self._agents is not None:
            replaced = self._agents.exists(args["name"])  # honest: don't silently overwrite a saved goal
            agent = self._agents.add(args["name"], args["goal"])
            verb = "Updated" if replaced else "Saved"
            return f"{verb} the agent '{agent.name}'. Run it any time from the Agents tab."
        if name == "delete_build":
            return self._request_delete(args["name"])
        return f"Unknown tool: {name}"

    # ----- delete / rename helpers -----
    def _matches(self, name: str) -> bool:
        """True if a build OR agent matches the name (slug or case-insensitive display name)."""
        target = name.strip().lower()
        slug = slugify(name)
        if any(a.slug == slug or a.name.strip().lower() == target for a in self._builds.list()):
            return True
        if self._agents is not None:
            return any(a.name.strip().lower() == target for a in self._agents.list())
        return False

    def _request_delete(self, target: str) -> str:
        """A delete is NEVER performed from the model loop — it asks the UI for one real human click first
        (defense-in-depth: injected text can't trigger a silent, irreversible rmtree). With no bus (a
        headless/test context) fall back to the direct delete so behaviour is unchanged there."""
        if not self._matches(target):
            return f"I couldn't find anything called '{target}' to delete."
        if self._bus is not None:
            self._bus.publish(BuildDeleteRequested(target))
            return (
                f"Asked the user to confirm removing '{target}' — nothing is deleted until they approve."
            )
        return self.confirm_delete(target)  # headless fallback

    def confirm_delete(self, target: str) -> str:
        """Actually remove a build or agent — called only AFTER a human confirmation (UI button click)."""
        if self._forge.remove_build(target):
            return f"Removed '{target}'."
        if self._agents is not None:
            hit = next(
                (a for a in self._agents.list() if a.name.strip().lower() == target.strip().lower()),
                None,
            )
            if hit is not None:
                self._agents.remove(hit.name)
                return f"Removed the agent '{hit.name}'."
        return f"Couldn't remove '{target}' — it may be open or running right now."

    def _rename(self, name: str, new_name: str) -> str:
        new_name = (new_name or "").strip()
        if not new_name:
            return "Give me a new name to use."
        target = name.strip().lower()
        slug = slugify(name)
        build = next(
            (a for a in self._builds.list() if a.slug == slug or a.name.strip().lower() == target), None
        )
        if build is not None:
            old_slug = build.slug
            renamed = self._builds.rename(build.slug, new_name)
            if renamed is None:
                return (
                    f"Couldn't rename '{build.name}' — that name may be taken, or it's open or building "
                    "right now. Try again in a moment."
                )
            if self._bus is not None:
                self._bus.publish(BuildRenamed(renamed, old_slug=old_slug))
            return f"Renamed '{build.name}' to '{renamed.name}'."
        if self._agents is not None:
            agent = next((a for a in self._agents.list() if a.name.strip().lower() == target), None)
            if agent is not None:
                renamed_agent = self._agents.rename(agent.name, new_name)
                if renamed_agent is None:
                    return f"Couldn't rename the agent '{agent.name}' — that name may already be in use."
                return f"Renamed the agent '{agent.name}' to '{renamed_agent.name}'."
        return f"I couldn't find anything called '{name}' to rename."

    # ----- self-change (apply / discard a drafted improvement to HELIX itself) -----
    def _resolve_change(self, which, pending):
        if which:
            w = str(which).strip().lower()
            return next((p for p in pending if w in p.id.lower() or w in (p.summary or "").lower()), None)
        return pending[0] if len(pending) == 1 else None

    def _approve_self(self, which) -> str:
        pending = self._selfdev.pending()
        if not pending:
            return "There's no drafted change to apply."
        target = self._resolve_change(which, pending)
        if target is None:
            return "Which one? Pending: " + ", ".join(p.id for p in pending)
        try:
            return self._selfdev.approve(target.id)
        except Exception as exc:
            return f"Couldn't apply it: {exc}"

    def _reject_self(self, which) -> str:
        pending = self._selfdev.pending()
        if not pending:
            return "There's no drafted change to discard."
        target = self._resolve_change(which, pending)
        if target is None:
            return "Which one? Pending: " + ", ".join(p.id for p in pending)
        try:
            self._selfdev.reject(target.id)
            return f"Discarded {target.id}."
        except Exception as exc:
            return f"Couldn't discard it: {exc}"
