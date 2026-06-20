"""Home reminders over email-to-SMS. The generic mail/SMS primitives now live in `helix/core/mailer.py`
(so the Forge's approval path doesn't depend on the Home pillar); this module keeps only the
Home-specific reminder built from due chores.
"""
from __future__ import annotations

from helix.core.mailer import (  # re-exported for any existing importers of this module
    CARRIER_CHOICES,
    CARRIER_GATEWAYS,
    DEFAULT_SENDER,
    SMS_APP_PASSWORD_SETTING,
    SMS_CARRIER_SETTING,
    SMS_PHONE_SETTING,
    SMS_SENDER_SETTING,
    NotifyError,
    gateway_address,
    is_configured,
    send_text_via_email,
    sms_config,
)
from helix.core.settings import AppSettings
from helix.home.tasks import due_tasks, reminder_message

__all__ = [
    "CARRIER_CHOICES", "CARRIER_GATEWAYS", "DEFAULT_SENDER", "SMS_APP_PASSWORD_SETTING",
    "SMS_CARRIER_SETTING", "SMS_PHONE_SETTING", "SMS_SENDER_SETTING", "NotifyError",
    "gateway_address", "is_configured", "send_text_via_email", "sms_config", "send_reminder",
]


def send_reminder(tasks: list, settings: AppSettings | None = None, *, smtp_factory=None) -> str:
    """Text the user their due/overdue tasks. Returns a short status string."""
    settings = settings or AppSettings()
    cfg = sms_config(settings)
    recipient = gateway_address(cfg["phone"], cfg["carrier"])
    send_text_via_email(cfg["sender"], cfg["app_password"], recipient, reminder_message(tasks), smtp_factory=smtp_factory)
    return f"Texted {len(due_tasks(tasks))} reminder(s) to your phone."
