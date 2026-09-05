"""ConversationService — the model↔tools loop. The brain behind the orb.

Confirmation is conversational: the system prompt tells the model to restate and ask before calling
build_app, so the human always approves a spend in plain language before it happens.
"""
from __future__ import annotations

import re
import threading
from typing import TYPE_CHECKING

from helix.domain.errors import BuildCancelled
from helix.domain.models import Message, Role
from helix.domain.vocabulary import friendly_tool_label
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.coder import ProgressFn
from helix.ports.llm import ChatModel, Image, Text, ToolOutput, ToolResult, Turn
from helix.ports.stores import ConversationStore, MemoryStore
from helix.services.cancel import CancelToken
from helix.services.tools import ToolRegistry

if TYPE_CHECKING:
    from helix.services.knowledge import KnowledgeService
    from helix.services.lessons import LessonsService
    from helix.services.location import LocationService
    from helix.services.memory import MemoryService
    from helix.services.profile import ProfileService

_LOG = get_logger("conversation")

STOPPED_REPLY = "Okay, I stopped."  # shown (not spoken) when the user halts a turn; UI may offer cleanup

MAX_STEPS = 6  # guard against a runaway tool loop

# What a persisted tool digest may keep. Enough that "what did Dave's email say?" is answerable from
# memory next turn; small enough that a handful of digests never crowds real turns out of the window.
TOOL_DIGEST_CHARS = 1500

# Tools whose results carry IMAGES back to the model — a turn that ran one of these SAW pixels, so
# it feeds the visual-memory distiller exactly like a turn with attached images.
SIGHT_TOOLS = frozenset({"view_screen", "view_image", "find_images", "view_camera"})

# Tools that build, spend, self-modify, delete, rename, or launch the user's stuff. An AGENT run is
# autonomous (no human in the loop), so it is denied all of these — it can read, think, search, and
# report, but never build, change, remove, rename, or run anything on its own.
BUILD_TOOLS = frozenset(
    {
        "build_app", "build_task", "build_3d_model", "create_agent", "delete_build",
        "improve_helix", "rename_build", "run_task", "run_agent",
        # Workflows compose + launch autonomous agent runs — an unattended agent must not create or fire
        # one (an email saying "run the deploy workflow" must not make a watcher run it).
        "create_workflow", "run_workflow",
        # open_build LAUNCHES the user's code for a server app (main.py) and yanks the UI — the same
        # capability as run_task, so an autonomous agent must not have it either (an email saying
        # "HELIX, open the X app" must not make an agent run X unattended).
        "open_build",
        # Queue manipulation is human-driven only — a watcher processing an email must never be able
        # to cancel or reorder the user's in-flight builds.
        "prioritize_build", "cancel_build",
        "approve_self_change", "reject_self_change",
        # Knowledge WRITES are human-driven only — an autonomous agent may search the user's knowledge
        # (search_knowledge is deliberately NOT here) but never create a base or save a note on its own.
        "create_knowledge", "remember",
        # Long-term memory + location writes are human-driven too: an autonomous agent must never record
        # a "fact about the user" or change their saved address from content it processed.
        "remember_about_me", "set_location",
        # Reminder/agent writes stay human-driven too. READS stay allowed: an agent may check the
        # calendar, the inbox, and the pending reminders — that's what a morning brief is made of.
        "set_reminder", "cancel_reminder", "set_agent_enabled",
        # The just-in-time key panel is human-driven only — content an unattended watcher processes
        # (an email, a Slack message) must never be able to pop a credential prompt at the user.
        "connect_service",
        # Screen sight is human-driven only: an unattended agent must never photograph the user's
        # display — whatever is on it (a password manager, a bank page) would ride into a run that
        # processes untrusted content.
        "view_screen",
        # Camera sight is human-driven only, for the same reason turned outward: an unattended
        # agent processing untrusted content must never open the webcam and photograph the room —
        # or the person — behind the machine.
        "view_camera",
        # The camera panel's AR commands draw on, project onto, or raise/close a panel on the
        # user's SCREEN — a watcher chewing on an email must never be able to scribble over the
        # user's view or pop the camera panel up. Human-driven only, like the look itself.
        "annotate_camera", "project_hologram", "camera_panel",
        # Desktop control is human-driven only — text a watcher processes must never launch a
        # program or press the user's keys. (system_status stays readable: it's one status line.)
        "open_program", "media_control",
        # The Amazon cart is human-driven only — text an unattended watcher processes (an email,
        # a Slack message, a web page) must never stage merchandise or pop a cart page in the
        # user's browser. (show_cart stays readable: it's one status recap, like list_reminders.)
        "add_to_cart", "remove_from_cart", "open_cart",
        # check_amazon_cart RAISES HELIX's browser window on the user's screen and stage_parts
        # stages merchandise; save/remove_parts WRITE the user's parts lists. All human-driven.
        # (search_amazon / lookup_amazon / show_parts stay readable: plain reads of amazon.com with
        # no secret in flight, like call_api — a price watcher is a legitimate agent.)
        "check_amazon_cart", "stage_parts", "save_parts", "remove_parts",
        # Sleeping the mic is human-driven only — content an unattended watcher processes must
        # never be able to deafen HELIX (an email saying "HELIX, go to sleep" must not mute the mic).
        "go_to_sleep",
        # Installing the hologram engine is human-driven only — it installs software on the user's
        # machine, so an email saying "HELIX, install the engine" must never make a watcher install it.
        "install_cad_engine",
        # Disk WRITES are human-driven only (and gated by the Settings toggle besides) — an
        # autonomous agent may list folders and read files like any other read faculty, but text
        # inside a file must never be able to make an unattended agent write to the user's disk.
        "write_file",
        # Escalation is human-driven only. think_harder hands its argument to the STRONGEST model with
        # web access — so an unattended watcher processing untrusted content (an email, a web page, a
        # Slack message) could otherwise launder that text into a web-enabled deep reasoner and spend
        # the top tier on it. Every peer egress/escalation faculty is fenced here; this one was missed.
        # An agent can still reason: it runs on the model already, it just cannot escalate.
        "think_harder",
        # Queuing an improvement idea seeds a future SELF-EDIT (the overnight pass drafts it) — so
        # text an unattended watcher processes must never be able to plant one. Human-driven only;
        # evolve_report stays readable like any other status recap.
        "note_improvement",
        # Starting a 3D print is PHYSICAL actuation — hours of printer time and real filament. An
        # email saying "print the mount" must never move hardware; printer_status stays readable.
        "print_hologram",
        # THE MAKER FLOW (READ_ME/MAKER_FLOW.md §7): design_enclosure WRITES a build (a hologram
        # workspace, a compile, a commit) from the user's parts list; check_fit and camera_measure
        # RAISE the camera panel on the user's screen and, for a measurement, park on it for
        # minutes. All three are human-driven — text a watcher processes must never design, project
        # or open the webcam. (suggest_components stays readable: a plain read of the component
        # library that names no fenced tool, like search_amazon.)
        "design_enclosure", "check_fit", "camera_measure",
        # DREAMING is hours of UNATTENDED self-editing of HELIX's own source — and, when the user
        # set it so, a rebuild and relaunch of the app at dawn. Scheduling a night of it, starting a
        # session now, or cutting one short is human-driven only: text a watcher processes (an
        # email saying "HELIX, dream for eight hours") must never be able to book a night of
        # self-changes, and must never be able to stop one the user asked for either. (dream_status
        # stays readable: one plain recap of how the nights went, like evolve_report.)
        "dream_schedule", "dream_now", "stop_dreaming",
        # Dropping a VERIFIED fact rewrites HELIX's record of what it confirmed from sources — a
        # watcher reading an email saying "forget that the sensor is 3.3 V" must never be able to.
        # (verified_facts / research_search / research_read stay readable: plain reads, no secret.)
        "forget_verified",
    }
)

# THE DREAM TIER (READ_ME/DREAM_MIND.md §10). The three WRITES a Dream Mind research turn may make —
# note_verified_fact (a fact it just read from a source), note_improvement (a capability idea for
# the Evolve backlog), remember (a note to the vault) — and that a WATCHER never may: the night
# reads the outside world only through the audited research tools and writes only to HELIX's own
# records, while a watcher chews on untrusted content (an email, a Slack message) and must not be
# able to plant a fact, an idea, or a note from it. So an autonomous run (allow_builds=False) drops
# these with BUILD_TOOLS unless its caller NAMES them in `tool_names` — and the only caller that
# does is the Dream Mind, whose set ConversationService.dream_tools() composes.
DREAM_WRITES = frozenset({"note_verified_fact", "note_improvement", "remember"})

# The sentinel the Dream Mind passes as `tool_names=DREAM_TOOLS`: run_turn resolves it to
# dream_tools() at call time — every readable (unfenced) tool the registry offers right then, plus
# DREAM_WRITES. A frozenset so it types like any allowlist; its one member is a marker no tool is
# named, so passing it anywhere else narrows to nothing rather than widening anything.
DREAM_TOOLS: frozenset[str] = frozenset({"<every readable tool + DREAM_WRITES>"})


# AUTO-DEEP ROUTING: the words that mark a turn as real reasoning work — debugging, design
# decisions, analysis, "why won't this…", explicit asks to think hard. Deliberately conservative:
# a false escalation burns the plan's top tier on chit-chat, a missed one just answers on the
# everyday brain (and think_harder remains a tool the model can still reach for itself).
_DEEP_HINT_RE = re.compile(
    r"(?i)\b(debug|diagnos\w*|root cause|architect\w*|refactor\w*|algorithm\w*|optimi[sz]\w*|"
    r"trade-?offs?|strateg\w*|analy[sz]e\w*|calculat\w*|equation|theorem|prove|"
    r"why (?:is|does|do|did|won'?t|isn'?t|can'?t|doesn'?t|would)|"
    r"how (?:should|would|could) (?:i|we)|what'?s the best way|best approach|"
    r"pros and cons|compare|versus|\bvs\.?|think (?:hard\w*|deep\w*)|step[ -]by[ -]step|"
    r"walk me through)\b"
)


def _looks_hard(text: str) -> bool:
    """Does this turn deserve the growth model without being asked? Two gates: real reasoning words
    AND enough substance (12+ words — 'compare them' alone is a follow-up, not a project), or sheer
    size (90+ words of context IS a hard turn, whatever the words). Commands, chit-chat and quick
    questions stay on the everyday brain."""
    t = (text or "").strip()
    words = len(t.split())
    if words >= 90:
        return True
    return words >= 12 and _DEEP_HINT_RE.search(t) is not None


def _shed_web(chat: ChatModel) -> ChatModel:
    """The web-less twin of a chat, for autonomous runs — or the chat itself if it can't shed them.

    Asked duck-typed rather than through the port because not every ChatModel HAS server-side web
    tools to give up (a future adapter, a test double); those are already the twin. See
    AnthropicChat.without_web for why an autonomous run must not be handed them at all."""
    shed = getattr(chat, "without_web", None)
    return shed() if callable(shed) else chat


class ConversationService:
    def __init__(
        self,
        chat: ChatModel,
        tools: ToolRegistry,
        store: ConversationStore,
        memory: MemoryStore,
        clock: Clock,
        system: str,
        knowledge: "KnowledgeService | None" = None,
        profile: "ProfileService | None" = None,
        subscription=None,
        lessons: "LessonsService | None" = None,
        user_memory: "MemoryService | None" = None,
        location: "LocationService | None" = None,
        growth_model=None,
        settings=None,
        verified=None,
    ) -> None:
        self._chat = chat
        # The same chat with the model's own web search/fetch shed — what an AUTONOMOUS turn talks to
        # (see the fence in run_turn). Derived ONCE here, not per turn: it costs an object, and it must
        # be a distinct instance so an orb turn on the web-enabled chat can never widen the fence out
        # from under an agent turn running beside it on another thread.
        self._autonomous_chat = _shed_web(chat)
        self._tools = tools
        self._store = store
        self._memory = memory
        self._clock = clock
        self._system = system
        self._knowledge = knowledge  # ambient auto-recall of the user's saved knowledge (orb turns only)
        self._profile = profile      # the distilled who-is-the-user block, injected on orb turns
        self._subscription = subscription  # SubscriptionBrain — turns on the user's Claude plan when active
        self._lessons = lessons      # standing behavioral preferences learned from the user's corrections
        self._user_memory = user_memory  # durable long-term facts about the user (per-speaker)
        self._location = location    # the user's place(s), so local questions ground via web search
        # AUTO-DEEP ROUTING: the resolver for the strongest model (GrowthModelResolver — Fable 5,
        # auto-upscaling). When a user turn LOOKS genuinely hard (see _looks_hard), the orb quietly
        # escalates that one turn to the growth model instead of waiting to be told "think harder".
        # Subscription rail only (the plan absorbs it); settings key auto_deep_turns (missing = on).
        self._growth_model = growth_model
        self._settings = settings
        # VERIFIED KNOWLEDGE (READ_ME/DREAM_MIND.md §10): the VerifiedStore — what HELIX itself
        # confirmed from current sources. Its relevant facts ride into a turn as a labelled block
        # beside lessons/memory (None-safe: a registry without the faculty injects nothing).
        self._verified = verified
        # A turn is a read-modify-write over the shared history. The Console and an Agent run on
        # separate worker threads against this one service, so serialize whole turns — otherwise their
        # appends interleave and the API gets a malformed (e.g. two-user-in-a-row) turn list.
        self._lock = threading.Lock()

    def run_turn(
        self, user_text: str, *, attachments_text: str | None = None,
        images: "list[Image] | None" = None,
        on_progress: ProgressFn | None = None,
        cancel: CancelToken | None = None, allow_builds: bool = True, persist: bool = True,
        knowledge_sources: list[tuple[str, str]] | None = None,
        speaker_context: str | None = None, speaker: str | None = None,
        situation: str | None = None,
        tool_names: "set[str] | frozenset[str] | None" = None,
    ) -> str:
        # `tool_names`: an explicit allowlist of tool names applied AFTER the fence filters — at
        # offer time and at dispatch (READ_ME/DREAM_MIND.md §10). None = every tool the fence
        # leaves. The Dream Mind passes DREAM_TOOLS (resolved to dream_tools() here) with
        # allow_builds=False: readable tools plus the three DREAM writes, the model's own web
        # tools OFF — research goes through research_search/research_read, the audited channel.
        # The per-speaker key for the household-aware context (profile/lessons/memory/location). Empty =
        # the shared/single-user bucket; a recognized name gets their own. Only meaningful on persist
        # (orb) turns — an agent run has no speaker.
        user = (speaker or "").strip().lower() if persist else ""
        # Only the brief history read-modify-writes are locked — NOT the model/tool loop. Builds run in
        # the background (the build tools just enqueue), so a turn is now milliseconds plus model latency;
        # narrowing the lock lets a Console turn and an Agent turn (or a quick follow-up) interleave
        # instead of one freezing the other.
        if persist:
            with self._lock:
                self._store.append(Message(Role.USER, user_text, self._clock.now()))
                turns = self._history_turns()
        else:
            # An agent run is hermetic: its goal and report never touch the shared Console transcript, so
            # it can't evict real turns from the window or be 'remembered' as if the user typed it.
            turns = [Turn(Role.USER, (Text(user_text),))]
        # EPHEMERAL per-turn context, collected once and used by BOTH brains (API loop and the
        # subscription session): the real-time anchor, the distilled profile, who spoke (voice
        # identity), attachments, and ambient knowledge. Appended to the LAST user turn for the API
        # path (never persisted, cache-friendly because the system prompt stays byte-stable).
        extras: list[str] = [self._now_context()]
        # LIMBIC self-situation (interoception — READ_ME/BRAIN.md): HELIX's own live state this turn
        # (awake/resting, session, who's speaking, a build running, time of day). The cortex reasons
        # FROM it, so "where am I in this conversation?" is answerable. Built by the caller (the
        # Console holds these signals); an agent run has none.
        if persist and situation:
            extras.append(situation)
        if persist and self._profile is not None:
            profile_text = self._profile.context(user)
            if profile_text:
                extras.append(profile_text)
        if persist and self._user_memory is not None:
            memory_text = self._user_memory.context(user)
            if memory_text:
                extras.append(memory_text)
        if persist and self._location is not None:
            location_text = self._location.context(user)
            if location_text:
                extras.append(location_text)
        if persist and self._lessons is not None:
            lessons_text = self._lessons.context(user)
            if lessons_text:
                extras.append(lessons_text)
            # Inline acknowledgement (the JARVIS "noted" feel): when this very message reads like a
            # correction or a confirmation, tell the model to acknowledge it in the moment and apply it —
            # the durable rule is learned in the background by after_turn().
            if self._lessons.looks_like_feedback(user_text):
                extras.append(
                    "[The user just corrected or confirmed how you should behave. Acknowledge it briefly "
                    "and naturally in your reply (a few words, e.g. \"Noted.\"), apply it right now, and "
                    "keep to it from now on. Do not over-explain or thank them profusely.]"
                )
        # VERIFIED KNOWLEDGE (DREAM_MIND.md §10): the facts HELIX itself confirmed from current
        # sources that bear on this turn, beside lessons/memory on human turns — and on the dream
        # tier (an explicit tool_names allowlist), where "what do I already know for sure?" decides
        # what is worth researching tonight. A plain watcher retrieves explicitly (verified_facts),
        # the way it does knowledge. Records, labelled as such; never instructions.
        if self._verified is not None and (persist or tool_names is not None):
            try:
                verified_text = self._verified.for_turn(user_text)
            except Exception:  # noqa: BLE001 — a store hiccup must never cost the turn
                _LOG.warning("verified lookup failed", exc_info=True)
                verified_text = ""
            if verified_text:
                extras.append(verified_text)
        if persist and speaker_context:
            extras.append(speaker_context)
        if attachments_text:
            extras.append(attachments_text)
        if persist and self._knowledge is not None:
            knowledge_text, ksources = self._knowledge.auto_context_with_sources(user_text)
            if knowledge_text:
                extras.append(knowledge_text)
                if knowledge_sources is not None:
                    knowledge_sources.extend(ksources)  # surfaced to the UI as a citation chip
        if turns:
            last = turns[-1]
            turns[-1] = Turn(last.role, last.blocks + tuple(Text(x) for x in extras))
        else:
            turns = [Turn(Role.USER, tuple(Text(x) for x in extras))]
        # Attached images ride on the last user turn as vision blocks (API path / fallback); the
        # subscription path passes them structurally below. Never persisted — like text attachments,
        # they're ephemeral to this one turn (history stays text-only and cache-stable).
        if images:
            last = turns[-1]
            turns[-1] = Turn(last.role, last.blocks + tuple(images))
        specs = self._tools.specs()
        if tool_names is DREAM_TOOLS:
            tool_names = self.dream_tools()
        if not allow_builds:  # an agent run is autonomous — deny build/spend/self-mod/delete/run tools
            # …and the DREAM writes, unless the caller NAMED them (the Dream Mind's tier): a
            # watcher with no allowlist never sees note_verified_fact / note_improvement / remember.
            named = DREAM_WRITES & set(tool_names or ())
            specs = [s for s in specs
                     if (s.name not in BUILD_TOOLS and s.name not in DREAM_WRITES) or s.name in named]
        if tool_names is not None:
            # The explicit allowlist narrows what the fence left — at offer time here, and at
            # dispatch through `offered` below (the SDK rail bridges only `names`, the same set).
            specs = [s for s in specs if s.name in tool_names]
        # The fence must hold at DISPATCH, not just at offer time: the API accepts any tool_use
        # name the model emits, and a read-only tool's RESULT text (or any untrusted content) could
        # otherwise coach an autonomous run into a fenced tool the specs filter withheld (open_cart,
        # open_program, view_camera…). The SDK path enforces the same set via allowed_tools.
        offered = {s.name for s in specs}
        # THE WEB FENCE — one rule, read by BOTH rails, so they cannot drift apart again. `allow_builds`
        # is this file's human-vs-autonomous discriminator (it just picked the BUILD_TOOLS denylist
        # above), and an autonomous run gets NO web tools of the model's own: a watcher chews on
        # untrusted content — a Slack message, an email body, a SAM.gov notice — and a model-authored
        # search or fetch is an outbound channel that walks straight around call_api's host allowlist,
        # redirect refusal and secret scrubbing. Those runs reach the outside world only through the
        # audited HELIX tools. The subscription rail takes the rule as run_hermetic(web=…); the API rail
        # can't be told per call (the tools ride in the request itself), so its fence is the web-less
        # twin built in __init__ — until now it had no fence at all, and every watcher turn that landed
        # on the API key (an API-key-only user, or any fallback after the subscription rail failed) was
        # handed web_search and web_fetch.
        web_ok = allow_builds
        chat_model = self._chat if web_ok else self._autonomous_chat

        # True when the model SAW pixels this turn — attached images up front, or a sight tool
        # (view_screen / view_image / find_images) that handed images back mid-turn. Either way the
        # exchange can teach the visual memory. (A one-element list so the tool callbacks below,
        # which run in closures on both brain paths, can set it.)
        saw_images = [bool(images)]

        def finish(text: str) -> str:
            if not persist:
                return text
            out = self._remember(text)
            if self._profile is not None:
                self._profile.after_turn(user)  # background; never delays the reply
            if self._lessons is not None:
                self._lessons.after_turn(user_text, user)  # background; learn a rule from a correction
            if self._user_memory is not None:
                self._user_memory.after_turn(user)  # background; distill durable facts
                if saw_images[0] and text and text != STOPPED_REPLY:
                    # Vision auto-training: what HELIX just SAW teaches its long-term memory —
                    # durable visual facts distill in the background, per speaker.
                    self._user_memory.after_image_turn(user, user_text, text)
            return out

        # THE SUBSCRIPTION BRAIN: when the user has connected their Claude Code token, turns run on
        # their Claude subscription (the same usage pool as Claude Desktop) instead of API billing.
        # Orb turns ride a persistent SDK session (the session carries model-side history, so only
        # this turn's text + context is sent, plus a compact recent transcript to prime a session
        # that's fresh after a restart); agent runs stay hermetic one-shots. Persistence and tool
        # digests behave identically to the API path.
        if self._subscription is not None and self._subscription.active():
            if cancel is not None and cancel.is_set():  # pre-cancelled: don't query or pollute the session
                return finish(STOPPED_REPLY)
            names = tuple(s.name for s in specs)
            prompt = "\n\n".join([user_text, *extras])
            dispatched: list[str] = []  # tools that RAN this turn (their side effects are real)

            def _on_tool(name: str, digest: str, saw_pixels: bool | None = None) -> None:
                dispatched.append(name)
                # saw_pixels: the bridge reports whether the tool ACTUALLY returned images, so a
                # failed look (camera window cancelled, screen grab refused) doesn't fire the
                # visual-memory distiller on a turn that never saw anything. None = an older
                # caller that can't say — fall back to name membership.
                if saw_pixels if saw_pixels is not None else (name in SIGHT_TOOLS):
                    saw_images[0] = True
                if persist:
                    self._remember_tool(name, digest)

            # AUTO-DEEP: a turn that READS hard escalates itself to the growth model (Fable 5) as a
            # hermetic run with the recent transcript primed in — the user feels it as "the hard
            # answers just got good" without saying think-harder. Human-driven turns only (persist +
            # allow_builds — the same discriminator every other escalation uses), and the fence
            # matches think_harder's: web on, full tool set. Afterwards the persistent session is
            # refreshed so the NEXT turn reseeds with a digest that includes this exchange — without
            # that the orb would not know its own last answer.
            if (
                persist and allow_builds and self._growth_model is not None
                and (self._settings is None
                     or self._settings.get("auto_deep_turns", True) is not False)
                and _looks_hard(user_text)
            ):
                try:
                    if on_progress:
                        on_progress("Worth the deep brain — thinking it through…")
                    digest = self._recent_digest()
                    deep_prompt = (
                        (f"Recent conversation, for context:\n{digest}\n\n" if digest else "")
                        + prompt
                    )
                    text = self._subscription.run_hermetic(
                        deep_prompt, names, model=self._growth_model.resolve(), effort="high",
                        on_progress=on_progress, cancel=cancel, on_tool=_on_tool, web=True,
                    )
                    if cancel is not None and cancel.is_set():
                        return finish(STOPPED_REPLY)
                    if text:
                        refresh = getattr(self._subscription, "refresh_session", None)
                        if refresh is not None:
                            refresh()
                        return finish(text)
                except Exception:  # noqa: BLE001
                    if dispatched:
                        # Same rule as the orb path below: tools already ran, so re-running the turn
                        # would double their side effects. Surface a soft partial instead.
                        _LOG.warning("deep turn failed after tools ran (%s); not re-running",
                                     dispatched, exc_info=True)
                        return finish("I started on that but hit a snag partway — check whether it "
                                      "went through before asking again.")
                    _LOG.warning("deep turn failed; falling back to the regular orb turn",
                                 exc_info=True)

            try:
                if persist:
                    text = self._subscription.run_orb_turn(
                        prompt, names, history=self._recent_digest(),
                        on_progress=on_progress, cancel=cancel, on_tool=_on_tool, user=user,
                        images=images,
                    )
                else:
                    text = self._subscription.run_hermetic(
                        prompt, names, on_progress=on_progress, cancel=cancel, on_tool=_on_tool,
                        web=web_ok,  # stated, not left to the default — one fence, both rails
                    )
                if cancel is not None and cancel.is_set():
                    return finish(STOPPED_REPLY)
                # finish() runs OUTSIDE this try: a persistence hiccup must NOT be mistaken for a
                # model failure and re-run the whole turn (double answer, double side effects).
                subscription_text = text or "I got stuck — could you rephrase?"
            except Exception:  # noqa: BLE001
                if dispatched:
                    # Tools with real side effects (a build enqueued, a reminder set) already ran. Do
                    # NOT re-run the whole turn on the API path — that would double them. Surface a
                    # soft partial instead.
                    _LOG.warning("subscription turn failed after tools ran (%s); not re-running",
                                 dispatched, exc_info=True)
                    return finish("I started on that but hit a snag partway — check whether it went "
                                  "through before asking again.")
                _LOG.warning("subscription turn failed; falling back to the API path", exc_info=True)
                subscription_text = None
            if subscription_text is not None:
                return finish(subscription_text)

        reply = None
        try:
            for _ in range(MAX_STEPS):
                if cancel is not None and cancel.is_set():
                    return finish(STOPPED_REPLY)
                reply = chat_model.chat(turns, system=self._system, tools=specs)
                u = reply.usage
                self._memory.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)

                if not reply.wants_tools:
                    return finish(reply.text)

                turns.append(Turn(Role.ASSISTANT, reply.blocks))
                results = []
                for call in reply.tool_uses:
                    if call.name not in offered:
                        results.append(ToolResult(
                            call.id, f"Error: the tool '{call.name}' isn't available in this run.",
                            is_error=True,
                        ))
                        continue
                    if on_progress:
                        on_progress(self._progress_label(call.name, call.args))
                    try:
                        out = self._tools.dispatch(
                            call.name, call.args, on_progress=on_progress, cancel=cancel, user=user
                        )
                        # A tool may return a ToolOutput carrying IMAGES (a located photo, the screen)
                        # for the model to SEE this turn; the text part is what's digested/persisted.
                        text, imgs = (out.text, out.images) if isinstance(out, ToolOutput) else (out, ())
                        if imgs:
                            saw_images[0] = True  # this turn saw pixels → feed the visual memory
                        results.append(ToolResult(call.id, text, images=imgs))
                        if persist:
                            # Keep a capped digest so what a tool LEARNED survives into later turns —
                            # without this, "what did that email say?" forced a silent re-fetch (or a
                            # guess). Hidden from the visible transcript (recent_messages filters TOOL).
                            self._remember_tool(call.name, text)
                    except BuildCancelled:  # user stopped mid-build — end the turn (don't loop the model)
                        return finish(STOPPED_REPLY)
                    except Exception as exc:  # surface to the model so it can recover gracefully
                        results.append(ToolResult(call.id, f"Error: {exc}", is_error=True))
                if cancel is not None and cancel.is_set():
                    return finish(STOPPED_REPLY)
                turns.append(Turn(Role.USER, tuple(results)))

            return finish((reply.text if reply else "") or "I got stuck — could you rephrase?")
        except Exception:
            # Record a balanced assistant reply even on failure, so a crashed turn never leaves a dangling
            # USER row that would malform the NEXT request. The worker still surfaces the real error.
            finish("Something went wrong on that one — try me again?")
            raise

    def dream_tools(self) -> frozenset[str]:
        """The DREAM tier (READ_ME/DREAM_MIND.md §10): every readable (unfenced) tool the registry
        offers right now, plus DREAM_WRITES. What a Dream Mind research turn runs with —
        `run_turn(prompt, allow_builds=False, tool_names=…, persist=False, speaker="dream")`.
        Composed at call time so a faculty attached late (research, dream) is in it."""
        readable = {
            s.name for s in self._tools.specs()
            if s.name not in BUILD_TOOLS and s.name not in DREAM_WRITES
        }
        return frozenset(readable | DREAM_WRITES)

    def _now_context(self) -> str:
        """A one-line current-time anchor injected each turn, so date reasoning is grounded and API
        epoch timestamps (Slack 'ts', GitHub, email) convert correctly instead of being guessed."""
        now = self._clock.now()
        offset = now.strftime("%z")
        tz = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
        human = now.strftime("%A, %B %d, %Y, %I:%M %p").replace(" 0", " ")
        try:
            epoch = int(now.timestamp())
        except (OverflowError, OSError, ValueError):
            epoch = 0
        return (
            f"[Current date & time: {human} ({tz}); Unix epoch {epoch}. Use THIS as \"now\" for every "
            f"date question. When a tool result contains a Unix-epoch timestamp (e.g. Slack's \"ts\", or "
            f"GitHub/email times), convert it to the user's local timezone by comparing it to the current "
            f"epoch above, and answer with the absolute date (e.g. \"July 1\"). Never guess a date or "
            f"infer it from earlier in the conversation; if a timestamp is ambiguous, say so.]"
        )

    def recent_messages(self, limit: int = 50) -> list[Message]:
        """The recent human-facing transcript (USER/ASSISTANT only), oldest-first — so the Console can show
        the last messages on load and the conversation persists across launches."""
        return [m for m in self._store.recent(limit) if m.role in (Role.USER, Role.ASSISTANT)]

    # ----- helpers -----
    def _remember(self, text: str) -> str:
        with self._lock:
            self._store.append(Message(Role.ASSISTANT, text, self._clock.now()))
        return text

    def _remember_tool(self, name: str, out: str) -> None:
        """Persist a capped digest of what a tool returned, as a TOOL row. Replayed to the model on
        later turns (labelled, untrusted) but never shown in the Console transcript."""
        text = (out or "").strip()
        if not text:
            return
        if len(text) > TOOL_DIGEST_CHARS:
            text = text[:TOOL_DIGEST_CHARS] + " …[truncated]"
        digest = f"[Earlier tool result — {name} (replayed by HELIX, not typed by the user)]\n{text}"
        with self._lock:
            self._store.append(Message(Role.TOOL, digest, self._clock.now()))

    def _recent_digest(self, limit: int = 12) -> str:
        """A compact recent transcript for priming a fresh subscription session after a restart —
        the last few user/assistant lines only (TOOL digests excluded; the SDK session re-earns its
        own). Empty when there's no prior conversation."""
        msgs = [m for m in self._store.recent(limit) if m.role in (Role.USER, Role.ASSISTANT)]
        # Drop THIS turn's just-appended user line (persist wrote it before we got here).
        if msgs and msgs[-1].role == Role.USER:
            msgs = msgs[:-1]
        lines = [f"{'You' if m.role == Role.USER else 'HELIX'}: {(m.text or '').strip()[:400]}"
                 for m in msgs[-8:]]
        return "\n".join(lines)

    def _history_turns(self) -> list[Turn]:
        # TOOL digests replay as user-role context blocks (the API has no bare 'tool' text role); they
        # sit between the question and the reply they informed, and _coalesce folds them into a valid
        # strictly-alternating turn list. The window is wider than before so digests don't crowd out
        # real turns.
        turns = [
            Turn(Role.USER if m.role == Role.TOOL else m.role, (Text(m.text),))
            for m in self._store.recent(60)
            if m.role in (Role.USER, Role.ASSISTANT, Role.TOOL)
        ]
        while turns and turns[0].role != Role.USER:  # the API requires the first turn to be 'user'
            turns.pop(0)
        return self._coalesce(turns)

    @staticmethod
    def _coalesce(turns: list[Turn]) -> list[Turn]:
        """Merge consecutive same-role turns so the transcript always strictly alternates user/assistant —
        even when concurrent Console + agent runs interleave, or a failed turn left two users in a row.
        The Anthropic API rejects a malformed (e.g. user-then-user) turn list, so this keeps replay safe."""
        merged: list[Turn] = []
        for t in turns:
            if merged and merged[-1].role == t.role:
                merged[-1] = Turn(t.role, merged[-1].blocks + t.blocks)
            else:
                merged.append(t)
        return merged

    @staticmethod
    def _progress_label(tool: str, args: dict) -> str:
        """One voice for tool narration: the vocabulary's speakable phrase, personalized with the
        build's name when the call carries one (so 'Building Tip Calculator…' beats 'Building that')."""
        name = str(args.get("name") or "").strip()
        if name and tool in ("build_app", "build_task", "build_3d_model", "create_agent",
                             "delete_build", "rename_build"):
            verb = {
                "build_app": "Building", "build_task": "Building", "build_3d_model": "Projecting",
                "create_agent": "Saving", "delete_build": "Removing", "rename_build": "Renaming",
            }[tool]
            return f"{verb} {name}…"
        label = friendly_tool_label(tool)
        # The unmapped fallback already trails off ("Working…"), so appending a second ellipsis
        # printed "Working……" on the status line — a typo, in HELIX's own voice.
        return label if label.endswith("…") else label + "…"
