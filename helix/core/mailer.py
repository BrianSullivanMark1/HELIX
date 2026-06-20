"""Neutral email / text-message primitives, shared by any feature that needs to reach the user.

Extracted from the (personal) Home pillar so the Forge's optional email-approval path
(`helix/selfdev/mailer.py`) does not depend on Home — letting the personal pillars be removed without
breaking the self-improvement loop. Pure stdlib (smtplib) with an injectable `smtp_factory` for tests.
"""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from helix.core.settings import AppSettings

SMS_SENDER_SETTING = "sms_sender_email"
SMS_APP_PASSWORD_SETTING = "sms_app_password"
SMS_PHONE_SETTING = "sms_phone_number"
SMS_CARRIER_SETTING = "sms_carrier"
DEFAULT_SENDER = "helixaifriend@gmail.com"

# US carrier email-to-SMS gateways. NOTE: some carriers are deprecating these; reliability varies.
CARRIER_GATEWAYS = {
    "verizon": "vtext.com",
    "att": "txt.att.net",
    "tmobile": "tmomail.net",
    "sprint": "messaging.sprintpcs.com",
    "uscellular": "email.uscc.net",
    "boost": "sms.myboostmobile.com",
    "cricket": "sms.cricketwireless.net",
    "metropcs": "mymetropcs.com",
    "googlefi": "msg.fi.google.com",
}
# (label, key) pairs for a UI dropdown.
CARRIER_CHOICES = [
    ("Verizon", "verizon"),
    ("AT&T", "att"),
    ("T-Mobile", "tmobile"),
    ("US Cellular", "uscellular"),
    ("Google Fi", "googlefi"),
    ("Boost", "boost"),
    ("Cricket", "cricket"),
    ("Metro", "metropcs"),
    ("Sprint", "sprint"),
]


class NotifyError(RuntimeError):
    pass


def gateway_address(number: str, carrier: str) -> str:
    """Build `<digits>@<carrier-gateway>` (drops a leading US country code). '' if not resolvable."""
    digits = "".join(ch for ch in str(number or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    domain = CARRIER_GATEWAYS.get(str(carrier or "").strip().lower().replace(" ", ""))
    if not digits or not domain:
        return ""
    return f"{digits}@{domain}"


def send_text_via_email(
    sender: str,
    app_password: str,
    recipient: str,
    body: str,
    subject: str = "HELIX",
    *,
    smtp_factory=None,
) -> None:
    """Send `body` as an email-to-SMS through Gmail SMTP. `smtp_factory` is injectable for testing."""
    if not (sender and app_password and recipient):
        raise NotifyError("SMS not configured (need sender, Gmail app password, and recipient).")
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
        raise NotifyError(f"Failed to send text: {error}") from error


def sms_config(settings: AppSettings | None = None) -> dict[str, str]:
    settings = settings or AppSettings()
    return {
        "sender": settings.get(SMS_SENDER_SETTING, DEFAULT_SENDER) or DEFAULT_SENDER,
        "app_password": settings.get(SMS_APP_PASSWORD_SETTING, "") or "",
        "phone": settings.get(SMS_PHONE_SETTING, "") or "",
        "carrier": settings.get(SMS_CARRIER_SETTING, "") or "",
    }


def is_configured(settings: AppSettings | None = None) -> bool:
    cfg = sms_config(settings)
    return bool(cfg["app_password"] and gateway_address(cfg["phone"], cfg["carrier"]))
