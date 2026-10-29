"""notification.send: renders a template per recipient and hands it to a simulated provider.

No real email is sent. The simulated provider is deterministic so that demos and tests
exercise every path:

- addresses at `bounce.test` or any `.invalid` domain bounce (recorded, not a job failure);
- addresses at `flaky.test` make the provider return a temporary 421 on the first attempt,
  so the job is retried and succeeds on attempt 2;
- everything else is delivered.

Idempotency: every message gets `message_id = sha256(job_id:address)`. A real provider
integration passes it as the provider's idempotency key, so a retried batch does not
deliver twice to recipients that were already accepted in an earlier attempt.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from queueforge.handlers.base import HandlerSpec, JobContext, RetryableError, register

_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,63}$")
_PHONE = re.compile(r"^\+[1-9][0-9]{6,14}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]{0,40})\s*\}\}")


class Recipient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(min_length=3, max_length=320)
    name: str | None = Field(default=None, max_length=120)
    variables: dict[str, str] = Field(default_factory=dict, max_length=20)


class NotificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["email", "sms"] = "email"
    template: str = Field(
        pattern=r"^[a-z0-9][a-z0-9_.\-]{0,63}$", description="Template identifier"
    )
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(
        min_length=1, max_length=10_000, description="Supports {{variable}} placeholders"
    )
    recipients: list[Recipient] = Field(min_length=1, max_length=500)
    batch_size: int = Field(default=50, ge=1, le=100)

    @field_validator("recipients")
    @classmethod
    def _addresses_match_channel(cls, recipients: list[Recipient], info: Any) -> list[Recipient]:
        channel = info.data.get("channel", "email")
        pattern = _EMAIL if channel == "email" else _PHONE
        bad = [r.address for r in recipients if not pattern.match(r.address)]
        if bad:
            raise ValueError(f"{len(bad)} invalid {channel} address(es), first: {bad[0][:80]!r}")
        return recipients

    @model_validator(mode="after")
    def _email_needs_subject(self) -> NotificationPayload:
        if self.channel == "email" and not self.subject:
            raise ValueError("subject is required for the email channel")
        return self


def _render(template: str, values: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        missing.append(key)
        return match.group(0)

    return _PLACEHOLDER.sub(substitute, template), missing


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].lower() if "@" in address else ""


async def send_notifications(payload: NotificationPayload, ctx: JobContext) -> dict[str, Any]:
    seen: set[str] = set()
    unique: list[Recipient] = []
    for recipient in payload.recipients:
        key = recipient.address.lower()
        if key not in seen:
            seen.add(key)
            unique.append(recipient)

    # Simulated provider-wide temporary failure for flaky.test on the first attempt.
    flaky = [r.address for r in unique if _domain(r.address) == "flaky.test"]
    if flaky and ctx.attempt == 1:
        raise RetryableError(
            f"Provider returned 421 temporary failure for {len(flaky)} recipient(s) at flaky.test"
        )

    delivered = 0
    bounced: list[dict[str, str]] = []
    unrendered: list[dict[str, Any]] = []
    sample_ids: list[str] = []
    batches = 0
    for start in range(0, len(unique), payload.batch_size):
        batch = unique[start : start + payload.batch_size]
        batches += 1
        for recipient in batch:
            values = {"name": recipient.name or recipient.address, **recipient.variables}
            _, missing_body = _render(payload.body, values)
            _, missing_subject = _render(payload.subject or "", values)
            missing = sorted(set(missing_body + missing_subject))
            if missing:
                unrendered.append({"address": recipient.address, "missing_variables": missing})
                continue
            domain = _domain(recipient.address)
            if domain == "bounce.test" or domain.endswith(".invalid"):
                bounced.append({"address": recipient.address, "reason": "550 mailbox unavailable"})
                continue
            message_id = hashlib.sha256(
                f"{ctx.job_id}:{recipient.address.lower()}".encode()
            ).hexdigest()
            if len(sample_ids) < 5:
                sample_ids.append(message_id[:24])
            delivered += 1
        # Simulated provider round trip: ~4 ms per message, yields to the event loop.
        await asyncio.sleep(0.004 * len(batch))
        ctx.progress["processed"] = min(len(unique), start + len(batch))

    ctx.logger.info(
        "notifications_processed",
        delivered=delivered,
        bounced=len(bounced),
        unrendered=len(unrendered),
        batches=batches,
    )
    return {
        "channel": payload.channel,
        "template": payload.template,
        "requested": len(payload.recipients),
        "unique_recipients": len(unique),
        "duplicates_skipped": len(payload.recipients) - len(unique),
        "delivered": delivered,
        "bounced": bounced[:100],
        "bounced_count": len(bounced),
        "unrendered": unrendered[:100],
        "unrendered_count": len(unrendered),
        "batches": batches,
        "sample_message_ids": sample_ids,
        "provider": "simulated",
    }


SPEC = register(
    HandlerSpec(
        name="notification.send",
        title="Send notification batch",
        description=(
            "Renders a template for each recipient and delivers it through a simulated "
            "provider. Duplicate addresses are delivered once."
        ),
        side_effects=(
            "Simulated delivery only. Message IDs are derived from the job ID and address, "
            "so a retried batch can be deduplicated by the provider."
        ),
        payload_model=NotificationPayload,
        run=send_notifications,
        default_timeout_seconds=120,
        example={
            "channel": "email",
            "template": "invoice-ready",
            "subject": "Your invoice {{invoice}} is ready",
            "body": "Hi {{name}}, invoice {{invoice}} for {{amount}} is attached.",
            "recipients": [
                {
                    "address": "ada@example.com",
                    "name": "Ada",
                    "variables": {"invoice": "INV-1042", "amount": "$120.00"},
                },
                {
                    "address": "grace@example.com",
                    "name": "Grace",
                    "variables": {"invoice": "INV-1043", "amount": "$75.50"},
                },
            ],
        },
    )
)
