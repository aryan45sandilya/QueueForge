"""Transactional email (verification, password reset) over SMTP.

Without QF_SMTP_HOST the message is written to the log instead, which keeps local development
and tests working. Production refuses to start with verification required and no SMTP host.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from queueforge.config import Settings
from queueforge.logs import get_logger

log = get_logger("queueforge.email")


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    text: str


# Captured messages for tests and for the log-only transport.
outbox: list[OutgoingEmail] = []


def _send_smtp(settings: Settings, message: OutgoingEmail) -> None:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = message.to
    msg["Subject"] = message.subject
    msg.set_content(message.text)
    host, port, timeout = (
        settings.smtp_host or "",
        settings.smtp_port,
        settings.smtp_timeout_seconds,
    )
    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password or "")
        client.send_message(msg)


async def send_email(settings: Settings, message: OutgoingEmail) -> bool:
    """Send one message. Returns False (and logs) on failure; callers never leak SMTP errors."""
    if not settings.smtp_host:
        outbox.append(message)
        del outbox[:-50]
        if settings.env == "production":
            # Never log links in production: a reset link in a log file is an account takeover.
            log.warning("email_not_sent_smtp_not_configured", subject=message.subject)
            return False
        # In development the link is the point of the message; log it so it can be followed.
        log.info("email_not_sent_no_smtp", subject=message.subject, body=message.text)
        return True
    try:
        await asyncio.to_thread(_send_smtp, settings, message)
    except (OSError, smtplib.SMTPException) as exc:
        log.error("email_send_failed", error_type=type(exc).__name__, subject=message.subject)
        return False
    log.info("email_sent", subject=message.subject, to_domain=message.to.rsplit("@", 1)[-1])
    return True


def verification_email(settings: Settings, to: str, name: str, token: str) -> OutgoingEmail:
    link = f"{settings.public_url}/verify-email?token={token}"
    return OutgoingEmail(
        to=to,
        subject="Confirm your QueueForge email address",
        text=(
            f"Hi {name},\n\n"
            f"Confirm your email address to start using QueueForge:\n\n{link}\n\n"
            "The link expires in 48 hours. If you didn't create an account, ignore this email.\n"
        ),
    )


def reset_email(settings: Settings, to: str, name: str, token: str) -> OutgoingEmail:
    link = f"{settings.public_url}/reset-password?token={token}"
    return OutgoingEmail(
        to=to,
        subject="Reset your QueueForge password",
        text=(
            f"Hi {name},\n\n"
            f"Someone asked to reset the password for this account. To choose a new one, open:\n\n"
            f"{link}\n\n"
            "The link expires in 1 hour and can be used once. If it wasn't you, ignore this email; "
            "your password stays the same.\n"
        ),
    )
