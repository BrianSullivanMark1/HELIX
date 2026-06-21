"""Neutral email primitives, shared by any feature that needs to reach the user (currently the Forge's
optional email-approval path in `helix/selfdev/mailer.py`). Pure stdlib (smtplib) with an injectable
`smtp_factory` for tests.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from helix.core.settings import AppSettings

SMS_SENDER_SETTING = "sms_sender_email"
SMS_APP_PASSWORD_SETTING = "sms_app_password"
DEFAULT_SENDER = "helixaifriend@gmail.com"


class NotifyError(RuntimeError):
    pass


def send_text_via_email(
    sender: str,
    app_password: str,
    recipient: str,
    body: str,
    subject: str = "HELIX",
    *,
    smtp_factory=None,
) -> None:
    """Send `body` as an email through Gmail SMTP. `smtp_factory` is injectable for testing."""
    if not (sender and app_password and recipient):
        raise NotifyError("Email not configured (need sender, Gmail app password, and recipient).")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    try:
        server = smtp_factory() if smtp_factory is not None else smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        try:
            server.starttls(context=ssl.create_default_context())
            server.login(sender, app_password)
            server.send_message(message)
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except (smtplib.SMTPException, OSError) as error:
        raise NotifyError(f"Failed to send email: {error}") from error


def sms_config(settings: AppSettings | None = None) -> dict[str, str]:
    settings = settings or AppSettings()
    return {
        "sender": settings.get(SMS_SENDER_SETTING, DEFAULT_SENDER) or DEFAULT_SENDER,
        "app_password": settings.get(SMS_APP_PASSWORD_SETTING, "") or "",
    }
