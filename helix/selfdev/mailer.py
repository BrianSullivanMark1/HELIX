"""Email approval for self-improvement (§selfdev): notify Brian of a drafted change and let him
approve or reject it by replying — so he can control it remotely (from work, away from the laptop).

Sending reuses the Gmail SMTP path from `home/notify.py`. Replies are read over Gmail IMAP (same
account + app password): an UNSEEN reply whose subject carries the change's branch token, sent from the
notify address, with a yes/no first line, is applied via `engine.approve` / `engine.reject`.
Best-effort + injectable (`smtp_factory` / `imap_factory`) so it's testable and never blocks the app.
"""
from __future__ import annotations

import email as emaillib
import imaplib
import re
from email.header import decode_header, make_header
from typing import Any, Callable

from helix.core.mailer import sms_config, send_text_via_email
from helix.selfdev import engine

SELFDEV_NOTIFY_EMAIL_SETTING = "selfdev_notify_email"       # where to email approval requests (Brian)
SELFDEV_EMAIL_APPROVAL_SETTING = "selfdev_email_approval"   # default OFF (opt-in); set True to enable

_TOKEN_RE = re.compile(r"\[HELIX selfdev ([^\]]+)\]")
_AFFIRM = ("yes", "yep", "yeah", "ship it", "approve", "approved", "merge", "do it", "ok", "okay", "go ahead", "y")
_NEGATE = ("no", "nope", "reject", "discard", "scrap", "cancel", "don't", "do not", "n")


def _recipient(settings: Any) -> str:
    return str(settings.get(SELFDEV_NOTIFY_EMAIL_SETTING) or "").strip()


def is_configured(settings: Any) -> bool:
    cfg = sms_config(settings)
    return bool(cfg["app_password"] and cfg["sender"] and _recipient(settings))


def notify_drafted(settings: Any, rec: dict, *, smtp_factory=None) -> bool:
    """Email Brian a drafted change with its diffstat and how to approve. Returns whether it sent."""
    if settings.get(SELFDEV_EMAIL_APPROVAL_SETTING) is not True or not is_configured(settings):
        return False
    cfg = sms_config(settings)
    branch = rec.get("branch", "")
    body = (
        "HELIX drafted a code change for your approval.\n\n"
        f"Task: {rec.get('task', '')}\n\n"
        f"What changed:\n{rec.get('summary', '')}\n\n"
        f"Files:\n{rec.get('diffstat') or ', '.join(rec.get('files', []))}\n\n"
        "Reply YES to merge it into HELIX, or NO to discard.\n"
        f"(branch {branch})"
    )
    try:
        send_text_via_email(
            cfg["sender"], cfg["app_password"], _recipient(settings), body,
            subject=f"[HELIX selfdev {branch}] approve?", smtp_factory=smtp_factory,
        )
        return True
    except Exception:
        return False


def verdict(text: str) -> str | None:
    """'approve' / 'reject' / None from a reply's text (negative wins over affirmative)."""
    low = " " + re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip() + " "
    if any(f" {w} " in low for w in _NEGATE):
        return "reject"
    if any(f" {w} " in low for w in _AFFIRM):
        return "approve"
    return None


def _first_text(msg) -> str:
    """Best-effort plain-text body of an email.message."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain":
            try:
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
    return ""


def poll_replies(settings: Any, *, imap_factory: Callable[..., Any] | None = None) -> list[dict]:
    """Read UNSEEN Gmail replies and approve/reject the referenced change. Returns actions taken.

    Acts only on a reply (a) carrying our `[HELIX selfdev <branch>]` token, (b) from the notify
    address, with (c) a clear yes/no — then marks it Seen. Anything else is left untouched."""
    if settings.get(SELFDEV_EMAIL_APPROVAL_SETTING) is not True or not is_configured(settings):
        return []
    cfg = sms_config(settings)
    sender, password, me = cfg["sender"], cfg["app_password"], _recipient(settings)
    try:
        imap = imap_factory() if imap_factory is not None else imaplib.IMAP4_SSL("imap.gmail.com", 993)
    except Exception:
        return []
    actions: list[dict] = []
    try:
        imap.login(sender, password)
        imap.select("INBOX")
        typ, data = imap.search(None, "UNSEEN")
        if typ != "OK":
            return actions
        for num in (data[0].split() if data and data[0] else []):
            typ, msg_data = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = emaillib.message_from_bytes(msg_data[0][1])
            frm = str(make_header(decode_header(msg.get("From", ""))))
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            token = _TOKEN_RE.search(subj)
            if not token or me.lower() not in frm.lower():
                continue
            branch = token.group(1).strip()
            decision = verdict(_first_text(msg))
            if decision == "approve":
                res = engine.approve(settings, pending_id=branch)
            elif decision == "reject":
                res = engine.reject(settings, pending_id=branch)
            else:
                continue
            actions.append({"branch": branch, "action": decision, "ok": res.ok, "message": res.message})
            try:
                imap.store(num, "+FLAGS", "\\Seen")
            except Exception:
                pass
    except Exception:
        return actions
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return actions
