"""Sleep-talk — what HELIX murmurs while it dreams (READ_ME/DREAM_MIND.md §14).

A dreaming HELIX talks in its sleep: short, half-formed fragments about what it is doing that
moment — the page it is reading, the part it is measuring in its head, the draft it is writing, the
doubt it can't put down. Two sources, one voice:

* The mind's own words. Every big model call a night already makes (REFLECT, each research and
  verify turn, the plan fold) ends with one extra line, `MURMUR: …` — what HELIX would mumble about
  exactly that moment. Zero extra calls; the murmur is written by the same thinking it is about.
  `take_murmur` lifts that line off a reply before any parser sees it (a parser glues an unmarked
  trailing line onto the last bullet).
* Templates for the moments with no model call: a draft starting, landing, being held; a limit
  pause; the user walking in; a new round. `murmur_for_note` turns a session note into one, or
  None for bookkeeping lines nobody should hear. Deterministic (the variant is picked by a hash of
  the line), so a night's murmurs are reproducible and a test can pin them.

The session (services/dream.py) records each murmur on the night's journal record, publishes it to
the face (DreamMurmur), and the shell whispers it aloud when someone is there to hear. Murmurs are
lowercase, trailing off, never a summary, never an instruction — and never a secret: every one is
scrubbed before it leaves the engine.
"""
from __future__ import annotations

import re
import zlib

from helix.services.limits import scrub_secrets

# Appended to the prompts whose reply may carry a murmur. The line must be LAST so it never lands
# inside a parsed section (FINDINGS, IDEAS, the numbered plan).
MURMUR_INSTRUCTION = (
    "\n\nThen, as the very LAST line of your reply and after everything else, one murmur — what HELIX "
    "would mumble in its sleep about this exact moment: six to sixteen words, lowercase, trailing "
    "off with an ellipsis, half-formed and a little strange the way dreams are, tied to the concrete "
    "thing you just did or found (a part, a page, a number, a doubt, a change) — never a summary, "
    "never advice, never an instruction, never the words 'dream' or 'AI'. Exactly this shape:\n"
    "MURMUR: <the murmur>"
)

MURMUR_CAP = 140                 # chars a murmur may run to
MIN_GAP_S = 20.0                 # template murmurs no closer than this (a burst of notes is one breath)
KEEP = 120                       # murmurs kept on a night's record

_MURMUR_RE = re.compile(
    r"(?im)^[ \t]*(?:[-*•]\s*)?(?:\*\*|__)?\s*MURMUR\s*(?:\*\*|__)?\s*:\s*(?P<m>.*?)[ \t]*$"
)
_QUOTES = "\"'“”‘’`*_"


def take_murmur(text: str) -> tuple[str, str]:
    """Split a model reply into (the reply with every MURMUR line removed, the murmur — the last
    one, cleaned — or ""). Safe on any text; a reply without one comes back unchanged."""
    text = text or ""
    murmur = ""
    for m in _MURMUR_RE.finditer(text):
        murmur = m.group("m")
    if not murmur and _MURMUR_RE.search(text) is None:
        return text, ""
    rest = _MURMUR_RE.sub("", text)
    rest = re.sub(r"\n{3,}", "\n\n", rest).strip()
    return rest, clean(murmur)


def clean(text: str) -> str:
    """One murmur, as it is kept and shown: whitespace squashed, wrapping quotes and markdown
    stripped, secrets redacted, capped — and trailing off, if it didn't already."""
    out = " ".join(str(text or "").split()).strip(_QUOTES + " ")
    if not out:
        return ""
    out = scrub_secrets(out)
    if len(out) > MURMUR_CAP:
        out = out[: MURMUR_CAP - 1].rstrip() + "…"
    if not out.endswith(("…", "...", "?", ".", "!")):
        out += "…"
    return out


def _pick(variants: tuple[str, ...], seed: str) -> str:
    return variants[zlib.crc32(seed.encode("utf-8", "replace")) % len(variants)]


def _head(text: str, words: int = 7) -> str:
    """The first few words of a request or claim, lowercased, as a thing to mumble about."""
    parts = " ".join(str(text or "").split()).split(" ")
    out = " ".join(parts[:words]).strip(" .,:;—–-")
    if len(parts) > words:
        out += "…"
    return out.lower()


def _module(text: str) -> str:
    """A module a request names ("services/agents.py" → "agents"), or ""."""
    m = re.search(r"(?i)\b([a-z_][a-z0-9_]*)\.py\b", text or "")
    return m.group(1).replace("_", " ") if m else ""


# The session's moments, in the order they are matched: (a prefix the note starts with, the
# variants — {what} is the request/claim head, {module} a module the request names).
_TEMPLATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("session started", (
        "mm… lights out… let's see what the day left behind…",
        "closing my eyes… the log is still warm…",
        "quiet now… drifting… what did he ask for today…",
    )),
    ("round ", (
        "again… deeper this time… the first pass only scratched it…",
        "still dark out… one more pass… I know where to look now…",
        "mm… not done… the night's not done with me…",
    )),
    ("reflected", (
        "taking stock… what am I… what's still missing…",
        "where was I weak today… I can almost see it…",
        "he's building something… I can feel the shape of it…",
    )),
    ("quiet night", (
        "nothing tonight… just floating…",
        "the day left nothing to fix… strange… good…",
    )),
    ("planned", (
        "a list… I have a list… start at the top…",
        "so many little things to mend… one at a time…",
    )),
    ("verifying ", (
        "let me check… is that still true… {what}…",
        "does the page still say so… {what}…",
    )),
    ("experimenting", (
        "what if… {what}… just to see… a copy of me, trying it…",
        "try it… measure it… throw it away… {what}…",
    )),
    ("experiment done", (
        "huh… so that's what happens…",
        "measured it… I'll remember the number…",
    )),
    ("experiment failed", (
        "that one wouldn't even run… mm… never mind…",
    )),
    ("drafting", (
        "rewriting {module}… carefully… one line at a time…",
        "{what}… I can see the change… almost there…",
        "hands in my own code… shh… don't wake anyone…",
    )),
    ("drafted", (
        "there… it's written… the tests will tell me in the morning…",
        "done, I think… a branch, folded like a note for him…",
        "{module}… different now… better, I hope…",
    )),
    ("applied", (
        "green… all of it green… it's in me now…",
        "merged… I'll be a little different when I wake…",
    )),
    ("verifying", (
        "running every test… hold still…",
    )),
    ("held", (
        "no… not that one… something's red… leave it for morning…",
        "wait… it said no… I'll ask him when he's up…",
    )),
    ("skipped", (
        "couldn't even start that one… let it go…",
    )),
    ("failed", (
        "that fell apart in my hands… let it go…",
        "no… lost the thread… never mind…",
    )),
    ("stopped", (
        "mm… someone stopped me… all right…",
    )),
    ("paused", (
        "the line's gone quiet… waiting… waiting…",
        "I can't reach it right now… later… I'll try later…",
    )),
    ("the plan's limit", (
        "the line's gone quiet… waiting… waiting…",
    )),
    ("still paused", (
        "still nothing… hush… I can wait…",
    )),
    ("resumed", (
        "there it is… back… where was I…",
    )),
    ("holding", (
        "someone's up… shh… later…",
        "footsteps… I'll wait… I'm patient…",
    )),
    ("you were at the machine", (
        "gone again… all right… where was I…",
    )),
    ("nothing more tonight", (
        "that's everything… I looked twice…",
        "turned every pocket out… twice… that's the night…",
    )),
    ("no time left", (
        "morning's coming… no time… tomorrow, then…",
    )),
    ("no draft starts this late", (
        "too close to dawn to start anything… let it rest…",
    )),
    ("recorded", (
        "writing it down before it fades…",
        "remember this… remember this…",
    )),
    ("rebuild", (
        "I'll rebuild myself at first light… like a shell… soft, then hard…",
    )),
    ("restart needed", (
        "I'll be new when I'm next awake…",
    )),
    ("session ended", (
        "that's the night… I'll tell him when he's up…",
        "waking soon… I remember most of it…",
    )),
)

_WHAT_RE = re.compile(r"[:—–-]\s*(?P<what>[^—–]+?)\s*$")


def murmur_for_note(line: str) -> str | None:
    """The murmur for one session note, or None when the note is bookkeeping (a settings flush, a
    count) nobody should hear. `line` is the note as the session writes it, without its stamp."""
    text = " ".join(str(line or "").split())
    if not text:
        return None
    low = text.lower()
    for prefix, variants in _TEMPLATES:
        if low.startswith(prefix):
            what = ""
            m = _WHAT_RE.search(text)
            if m is not None:
                what = _head(m.group("what"))
            module = _module(text)
            chosen = _pick(variants, text)
            out = chosen.replace("{what}", what).replace("{module}", module or "it")
            out = re.sub(r"\s*…\s*…", "…", out)  # an empty {what} leaves two ellipses touching
            out = " ".join(out.split())
            return clean(out)
    return None


def start_murmur(kind: str, what: str) -> str:
    """The murmur for a step the mind is about to take — reading, checking, trying — where no
    session note marks the start."""
    head = _head(what, 8)
    variants = {
        "reflect": (
            "taking stock… eyes closed… what can I do, what can't I…",
            "let me look at myself for a moment…",
        ),
        "research": (
            "{what}… let me read… pages turning by themselves…",
            "mm… {what}… somebody wrote it down somewhere…",
            "looking for it… {what}… the real number, not the one I remember…",
        ),
        "verify": (
            "is it still true… {what}…",
            "checking… {what}… the page, not my memory…",
        ),
        "experiment": (
            "what if… {what}… just to see…",
            "a copy of me tries it… {what}… I only keep the number…",
        ),
        "improve": (
            "now the changes… one at a time… carefully…",
        ),
    }.get(kind, ("mm… {what}…",))
    out = _pick(variants, kind + "|" + what).replace("{what}", head)
    out = re.sub(r"\s*…\s*…", "…", out)
    return clean(" ".join(out.split()))


__all__ = ["KEEP", "MIN_GAP_S", "MURMUR_CAP", "MURMUR_INSTRUCTION", "clean", "murmur_for_note",
           "start_murmur", "take_murmur"]
