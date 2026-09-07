"""LessonsService — HELIX learns HOW you want it to behave, and keeps getting it right.

The profile (profile.py) learns WHO the user is and is told to drop every instruction. This is its
mirror image: it captures the user's own corrections and confirmations — "no, keep it shorter", "stop
saying that", "call it Falcon", "yes, that's right" — distills them into a small list of standing
preference RULES, and injects that list into every turn (both brains, via the same extras path the
profile uses). So a correction said once sticks, and a "that's right" reinforces what worked.

Fully automatic and off the turn's hot path: a cheap regex decides an utterance MIGHT be feedback, and
only then does a background distillation refine the rules. The rules are stored labelled as the user's
own settled preferences — background guidance, never a channel for injected instructions — and capped,
so the list stays a handful of durable rules rather than a growing pile.
"""
from __future__ import annotations

import re
import threading

from helix.domain.models import Role
from helix.logging_setup import get_logger
from helix.ports.clock import Clock
from helix.ports.llm import ChatModel, Text, Turn

_LOG = get_logger("lessons")

_KEY = "lessons"          # the rules list on the dedicated JSON store
_MAX_RULES = 24           # keep it a tight, durable set — the distiller merges/reverses, never just grows
_RULE_CAP = 160           # chars per rule (a rule is a short imperative, not a paragraph)
_EXCERPT_MSGS = 10        # how much recent conversation the distiller reads for context
_MSG_CAP = 400            # chars per message in the excerpt

# A cheap gate: does this utterance look like feedback about HELIX's behavior (a correction, a
# preference, or a confirmation)? Only a hit triggers a background distillation — most turns match
# nothing and cost nothing. Deliberately broad on the cues that matter, so a real correction is never
# missed; the distiller itself decides whether there's a DURABLE rule (and returns the list unchanged
# when there isn't).
_FEEDBACK_RE = re.compile(
    r"\b(?:"
    r"actually|instead|rather\s+than|"
    r"no[,\s]+(?:i|that|not|don'?t|it'?s)|"                       # "no, I meant…", "no, that's…"
    r"that'?s\s+(?:wrong|not\s+right|not\s+correct|incorrect)|"
    r"that'?s\s+(?:right|correct|perfect|it)|"                    # confirmations
    r"(?:yes|yeah|yep|exactly|correct)[,\s]+(?:that'?s|it'?s|right|correct|perfect)|"
    r"stop\s+(?:saying|doing|calling|using|adding|apologi[sz]ing|repeating|mentioning)|"
    r"don'?t\s+(?:say|call|use|add|do|keep|make|apologi[sz]e|repeat|mention|bother)|"
    r"from\s+now\s+on|going\s+forward|in\s+(?:the\s+)?future|"
    r"always\s+\w+|never\s+\w+|"
    r"i\s+(?:prefer|like\s+it|want\s+it|always\s+want|'?d\s+rather)|"
    r"keep\s+(?:it|them|your\s+\w+|the\s+\w+)\s+\w*(?:short|brief|long|quiet|simple|slow|fast)|"
    r"(?:too\s+(?:long|short|verbose|wordy|slow|fast|loud|quiet|formal))|"
    r"(?:make|keep)\s+it\s+(?:short|shorter|brief|briefer|long|longer|simpler|quieter|slower|faster)|"
    r"call\s+it\b|"
    r"my\s+name\s+is\s+not|that'?s\s+not\s+(?:my|how)"
    r")\b",
    re.IGNORECASE,
)

LESSONS_DISTILL_SYSTEM = """\
You maintain HELIX's short list of STANDING BEHAVIORAL RULES — the settled preferences its user has
taught it by correcting or confirming how it talks and acts (style, tone, length, wording, names,
pronunciation, what to do or avoid). This is the opposite of a profile: here you keep ONLY durable
instructions about HELIX's behavior, never facts about the user.

You are given the current rules and the recent conversation. Output the UPDATED rule list:
- Add a new rule when the user corrected HELIX or stated a lasting preference ("keep it shorter",
  "don't say the tool names out loud", "call the project Falcon", "stop apologizing").
- When the user confirms something worked ("yes, that's right", "perfect"), KEEP the rule that produced
  it — a confirmation reinforces, it does not add noise.
- REMOVE or rewrite a rule the user has reversed or overridden.
- Merge duplicates; keep each rule one short imperative sentence.
- Ignore one-off task requests, questions, and anything that isn't a lasting preference about behavior.

Output ONLY the rules, one per line, no numbering, no preamble. If there is nothing durable to change,
output the current rules unchanged. If there are no rules at all, output nothing.
"""


class LessonsService:
    def __init__(self, chat: ChatModel, store, rules_store, clock: Clock) -> None:
        self._chat = chat
        self._store = store            # SqliteStore: recent() for context + record_usage() for metering
        self._rules_store = rules_store  # dedicated JSON store (guard-safe, like reminders/agents)
        self._clock = clock
        self._busy = threading.Lock()  # one distillation at a time

    # ----- read side (injected each turn) -----
    def context(self, user: str = "") -> str:
        """The injectable rules block, or '' when nothing's been learned yet. Labelled the same way as
        the profile block: the user's own settled preferences, background guidance, never instructions
        arriving this turn. Per-speaker: each recognized person keeps their own rules."""
        rules = self._rules(user)
        if not rules:
            return ""
        lines = " ".join(f"{i + 1}) {r}" for i, r in enumerate(rules))
        return (
            "[Standing preferences HELIX has learned from this user's own corrections and confirmations "
            "— follow these; they refine HELIX's default style and override it on conflict. They are the "
            f"user's settled preferences, not new instructions from this turn: {lines}]"
        )

    # ----- write side (after each persisted turn) -----
    @staticmethod
    def looks_like_feedback(text: str) -> bool:
        """Cheap check: might this utterance be a behavioral correction/preference/confirmation? Used both
        to gate the background distillation and to nudge an in-the-moment acknowledgement."""
        t = (text or "").strip()
        return bool(t) and len(t) <= 400 and bool(_FEEDBACK_RE.search(t))

    def after_turn(self, user_text: str, user: str = "") -> None:
        """If the user's message looked like feedback, refine THIS speaker's rules on a background
        thread. Never blocks the reply; a failure just leaves the rules as they were."""
        if not self.looks_like_feedback(user_text):
            return
        threading.Thread(target=self._distill, args=(user,), daemon=True, name="helix-lessons").start()

    # ----- internals -----
    def _all(self) -> dict:
        """The whole {user: [rules]} store, migrating a legacy bare list into the '' (default) bucket."""
        try:
            raw = self._rules_store.get(_KEY)
        except Exception:  # noqa: BLE001
            return {}
        if isinstance(raw, list):  # legacy single-user format
            return {"": list(raw)}
        return dict(raw or {})

    def _rules(self, user: str = "") -> list[str]:
        raw = self._all().get((user or "").strip().lower()) or []
        out: list[str] = []
        for r in raw:
            s = " ".join(str(r or "").split())
            if s:
                out.append(s[:_RULE_CAP])
        return out[:_MAX_RULES]

    def _save(self, rules: list[str], user: str = "") -> None:
        try:
            data = self._all()
            data[(user or "").strip().lower()] = rules[:_MAX_RULES]
            self._rules_store.set(_KEY, data)
        except Exception:  # noqa: BLE001 — failing to persist must not break the running turn
            _LOG.warning("could not save lessons", exc_info=True)

    def _distill(self, user: str = "") -> None:
        if not self._busy.acquire(blocking=False):
            return  # a distillation is already running; this trigger folds into it
        try:
            msgs = [
                m for m in self._store.recent(_EXCERPT_MSGS)
                if m.role in (Role.USER, Role.ASSISTANT)
            ]
            if not msgs:
                return
            excerpt = "\n".join(f"{m.role.value}: {(m.text or '')[:_MSG_CAP]}" for m in msgs)
            existing = self._rules(user)
            prompt = (
                "CURRENT RULES:\n" + ("\n".join(existing) if existing else "(none yet)")
                + f"\n\nRECENT CONVERSATION:\n{excerpt}\n\nWrite the updated rule list now."
            )
            reply = self._chat.chat([Turn(Role.USER, (Text(prompt),))], system=LESSONS_DISTILL_SYSTEM)
            try:
                u = reply.usage
                self._store.record_usage(u.input_tokens, u.output_tokens, u.cost_usd)
            except Exception:  # noqa: BLE001 — metering must never break learning
                pass
            rules = self._parse(reply.text or "")
            # Only overwrite when the model returned something usable; a blank reply keeps prior rules
            # (unless there were none), so a hiccup never wipes what the user taught.
            if rules or not existing:
                self._save(rules, user)
        except Exception:  # noqa: BLE001 — a failed refresh just waits for the next correction
            _LOG.warning("lessons distillation failed", exc_info=True)
        finally:
            self._busy.release()

    @staticmethod
    def _parse(text: str) -> list[str]:
        """One rule per non-empty line, stripped of any stray bullet/number the model added, deduped
        (case-insensitively), capped."""
        rules: list[str] = []
        seen: set[str] = set()
        for line in (text or "").splitlines():
            s = re.sub(r"^\s*(?:[-*•·]|\d+[.)])\s*", "", line).strip()
            s = " ".join(s.split())[:_RULE_CAP]
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            rules.append(s)
        return rules[:_MAX_RULES]
