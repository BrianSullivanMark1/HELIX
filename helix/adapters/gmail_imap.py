"""Gmail-over-IMAP adapter — READ-ONLY access to the inbox for "what's in my mail?" questions.

Connects to imap.gmail.com over TLS with the user's address + a Google App Password, and fetches recent
INBOX headers (From / Subject / Date / unread). Read-only by construction at every layer:
  - the mailbox is opened with readonly=True (the IMAP session may not set flags),
  - headers are fetched with BODY.PEEK (so reading does NOT mark a message as \\Seen), and
  - this module only ever SELECTs/SEARCHes/FETCHes — it never STORE/EXPUNGE/DELETE/APPENDs.
A Google App Password technically grants full mailbox power (read + delete + send), so the read-only
guarantee here is OURS to keep — and it is kept by never issuing a mutating command.
"""
from __future__ import annotations

import email
import imaplib
from dataclasses import dataclass
from email.header import decode_header, make_header

from helix.logging_setup import get_logger

_LOG = get_logger("gmail")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


class GmailError(Exception):
    """A Gmail IMAP connection/login/fetch failed — surfaced to the user as a friendly message."""


@dataclass(frozen=True)
class Email:
    sender: str
    subject: str
    date: str
    unread: bool


def _decode(value: str) -> str:
    """Decode a possibly RFC2047-encoded header (=?utf-8?...?=) into plain text."""
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:  # noqa: BLE001 - a malformed header must never break the fetch
        return (value or "").strip()


def fetch_recent(address: str, app_password: str, *, limit: int = 25, timeout: float = 20.0) -> list[Email]:
    """The most recent `limit` INBOX messages (newest first), headers only. Raises GmailError on any
    auth/network failure. Never mutates the mailbox."""
    address = (address or "").strip()
    app_password = (app_password or "").replace(" ", "")  # Google shows app passwords in 4 spaced groups
    if not address or not app_password:
        raise GmailError("Gmail isn't connected — add your address and app password in Settings.")
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=timeout)
    except OSError as exc:
        raise GmailError(f"couldn't reach Gmail: {exc}") from exc
    try:
        try:
            conn.login(address, app_password)
        except imaplib.IMAP4.error as exc:
            raise GmailError(
                "Gmail rejected the login — check the address, and that the App Password is correct and "
                "IMAP is enabled. (App Passwords need 2-Step Verification on.)"
            ) from exc
        # readonly=True: this IMAP session is forbidden from changing any flags (belt-and-braces).
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise GmailError("couldn't open the Gmail inbox.")
        typ, data = conn.search(None, "ALL")
        ids = data[0].split() if (typ == "OK" and data and data[0]) else []
        recent = ids[-max(1, limit):]
        out: list[Email] = []
        for mid in reversed(recent):  # newest first
            # BODY.PEEK[...] reads WITHOUT setting the \Seen flag — so "checking" never marks mail as read.
            typ, msg_data = conn.fetch(mid, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not msg_data:
                continue
            descriptor = b""
            raw = b""
            for part in msg_data:
                if isinstance(part, tuple):
                    descriptor += part[0] or b""
                    raw += part[1] or b""
                elif isinstance(part, (bytes, bytearray)):
                    descriptor += bytes(part)
            if not raw:
                continue
            m = email.message_from_bytes(raw)
            out.append(Email(
                sender=_decode(m.get("From", "")) or "(unknown sender)",
                subject=_decode(m.get("Subject", "")) or "(no subject)",
                date=(m.get("Date", "") or "").strip(),
                unread=b"\\Seen" not in descriptor,
            ))
        return out
    except imaplib.IMAP4.error as exc:
        raise GmailError(f"Gmail read failed: {exc}") from exc
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
