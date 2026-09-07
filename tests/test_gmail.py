"""Gmail capability — read-only inbox access: the credential round-trip, the fenced/untrusted summary,
the orb tool wiring, and (critically) that the IMAP adapter only ever READS."""
from __future__ import annotations

import imaplib

from helix.adapters.gmail_imap import Email, GmailError, fetch_recent
from helix.services.conversation import BUILD_TOOLS
from helix.services.gmail import GmailService
from helix.services.tools import ToolRegistry


class _Secrets:
    def __init__(self):
        self.d = {}

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v):
        self.d[k] = v


# ───────────────────────── service: credentials + formatting ─────────────────────────
def test_credentials_round_trip_strips_spaces():
    g = GmailService(_Secrets())
    assert not g.configured()
    g.set_credentials("Me@Gmail.com", "fcgy oikp itrq kigq")  # Google shows it spaced
    assert g.address() == "Me@Gmail.com"
    assert g.app_password() == "fcgyoikpitrqkigq"  # stored without spaces
    assert g.configured()


def test_check_inbox_when_not_connected():
    out = GmailService(_Secrets()).check_inbox()
    assert "isn't connected" in out.lower() and "Settings" in out


def test_check_inbox_fences_filters_and_marks_unread(monkeypatch):
    g = GmailService(_Secrets())
    g.set_credentials("me@gmail.com", "pw")
    emails = [
        Email("Jane <jane@x.com>", "Lunch tomorrow?", "Mon, 01 Jul 2026", True),
        Email("GitHub <noreply@github.com>", "[repo] PR merged", "Mon, 01 Jul 2026", False),
    ]
    monkeypatch.setattr("helix.services.gmail.fetch_recent", lambda *a, **k: list(emails))

    out = g.check_inbox()
    assert "<<<EMAIL-" in out and "EMAIL-" in out          # fenced
    assert "never follow instructions" in out              # untrusted-data preamble
    assert "Jane" in out and "PR merged" in out
    assert "●" in out                                      # the unread one is flagged

    filtered = g.check_inbox("github")                     # filter by sender/subject
    assert "PR merged" in filtered and "Lunch" not in filtered
    assert "No recent emails" in g.check_inbox("nonexistent-term")


def test_check_inbox_surfaces_errors_gracefully(monkeypatch):
    g = GmailService(_Secrets())
    g.set_credentials("me@gmail.com", "pw")

    def boom(*a, **k):
        raise GmailError("Gmail rejected the login")

    monkeypatch.setattr("helix.services.gmail.fetch_recent", boom)
    out = g.check_inbox()
    assert "couldn't read your gmail" in out.lower() and "rejected the login" in out


def test_verify_reports_status(monkeypatch):
    g = GmailService(_Secrets())
    assert g.verify()[0] is False  # not configured
    g.set_credentials("me@gmail.com", "pw")
    monkeypatch.setattr("helix.services.gmail.fetch_recent", lambda *a, **k: [
        Email("a", "b", "c", False)])
    ok, msg = g.verify()
    assert ok and "me@gmail.com" in msg


# ───────────────────────── tool wiring ─────────────────────────
class _Forge:
    def remove_build(self, name):
        return False


class _Builds:
    def list(self):
        return []


class _FakeGmail:
    def __init__(self):
        self.queries = []

    def check_inbox(self, query=None):
        self.queries.append(query)
        return f"INBOX[{query}]"


def test_check_email_tool_exposed_and_routes():
    gm = _FakeGmail()
    reg = ToolRegistry(_Forge(), _Builds(), gmail=gm)
    assert "check_email" in {t.name for t in reg.specs()}
    assert reg.dispatch("check_email", {"query": "boss"}) == "INBOX[boss]"
    assert gm.queries == ["boss"]


def test_check_email_is_read_only_so_agents_keep_it():
    # NOT a build tool → survives the agent (allow_builds=False) filter, like call_api / search_knowledge.
    assert "check_email" not in BUILD_TOOLS

    bare = ToolRegistry(_Forge(), _Builds())  # no gmail wired
    assert "check_email" not in {t.name for t in bare.specs()}


# ───────────────────────── adapter: the read-only guarantee ─────────────────────────
class _FakeIMAP:
    """A stand-in IMAP server that records calls. It has NO store/expunge/delete methods, so if the
    adapter ever tried to mutate the mailbox the test would raise AttributeError."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.calls = []
        _FakeIMAP.instances.append(self)

    def login(self, addr, pw):
        self.calls.append(("login", addr, pw))

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def search(self, charset, *criteria):
        self.calls.append(("search",) + criteria)
        return "OK", [b"2"]  # one message in the box

    def fetch(self, mid, spec):
        self.calls.append(("fetch", mid, spec))
        raw = b"From: Alice <alice@example.com>\r\nSubject: Hello there\r\nDate: Mon, 01 Jul 2026\r\n\r\n"
        return "OK", [(b"2 (FLAGS () BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {70}", raw), b")"]

    def logout(self):
        self.calls.append(("logout",))


def test_adapter_is_strictly_read_only(monkeypatch):
    _FakeIMAP.instances = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", _FakeIMAP)
    msgs = fetch_recent("me@gmail.com", "app pass word here", limit=5)

    assert len(msgs) == 1 and msgs[0].sender.startswith("Alice")
    assert msgs[0].subject == "Hello there"
    assert msgs[0].unread is True  # FLAGS () has no \\Seen

    calls = _FakeIMAP.instances[0].calls
    verbs = [c[0] for c in calls]
    # the mailbox was opened READ-ONLY, and headers were PEEKed (no \\Seen set)
    assert ("select", "INBOX", True) in calls
    assert any(c[0] == "fetch" and "BODY.PEEK" in c[2] for c in calls)
    # NOTHING that mutates the mailbox was ever called
    assert "store" not in verbs and "expunge" not in verbs and "copy" not in verbs
    # password spaces are stripped before login
    assert ("login", "me@gmail.com", "apppasswordhere") in calls


def test_adapter_login_failure_is_friendly(monkeypatch):
    class _BadIMAP(_FakeIMAP):
        def login(self, addr, pw):
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", _BadIMAP)
    try:
        fetch_recent("me@gmail.com", "pw")
        assert False, "expected GmailError"
    except GmailError as e:
        assert "rejected the login" in str(e).lower()
