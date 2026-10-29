"""Helpers shared by services: actors, job events, after-commit side effects, audit records."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from queueforge.dispatch import Dispatcher, DispatchUnavailable
from queueforge.lifecycle import assert_transition
from queueforge.logs import get_logger
from queueforge.models import AuditEvent, Job, JobEvent

log = get_logger("queueforge.services")


@dataclass(frozen=True)
class Actor:
    label: str
    user_id: uuid.UUID | None = None
    api_key_id: uuid.UUID | None = None

    @classmethod
    def system(cls, component: str) -> Actor:
        return cls(label=f"system:{component}")


async def db_now(session: AsyncSession) -> datetime:
    """The database clock. Used for every time-critical comparison so hosts can't skew it."""
    return (await session.execute(select(func.now()))).scalar_one()


def transition(
    session: AsyncSession,
    job: Job,
    to_state: str,
    *,
    actor: str,
    message: str = "",
    at: datetime | None = None,
) -> None:
    """Apply a validated state change to a loaded job row and record the event."""
    from_state = job.state
    if from_state != to_state:
        assert_transition(from_state, to_state)
    job.state = to_state
    if at is not None:
        job.updated_at = at
    session.add(
        JobEvent(
            job_id=job.id,
            project_id=job.project_id,
            from_state=from_state,
            to_state=to_state,
            actor=actor[:160],
            message=message[:500],
            **({"at": at} if at is not None else {}),
        )
    )


def job_event(job: Job, queue_name: str | None = None) -> dict[str, Any]:
    return {
        "type": "job.updated",
        "job": {
            "id": str(job.id),
            "state": job.state,
            "type": job.type,
            "queue": queue_name,
            "attempts": job.attempts,
            "priority": job.priority,
            "cancel_requested": job.cancel_requested_at is not None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        },
    }


@dataclass
class AfterCommit:
    """Redis side effects collected inside a transaction and applied only after it commits.

    They are best-effort by design: if Redis is down the reconciler repairs dispatch state,
    and live updates degrade to polling.
    """

    enqueue: list[tuple[uuid.UUID, uuid.UUID, int, datetime]] = field(default_factory=list)
    dequeue: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    events: dict[uuid.UUID, list[dict[str, Any]]] = field(default_factory=dict)

    def push(self, job: Job) -> None:
        assert job.queued_at is not None
        self.enqueue.append((job.id, job.queue_id, job.priority, job.queued_at))

    def pull(self, job: Job) -> None:
        self.dequeue.append((job.queue_id, str(job.id)))

    def publish(self, job: Job, queue_name: str | None = None) -> None:
        self.events.setdefault(job.project_id, []).append(job_event(job, queue_name))

    async def apply(self, dispatcher: Dispatcher | None) -> None:
        if dispatcher is None:
            return
        try:
            if self.enqueue:
                await dispatcher.enqueue(self.enqueue)
            for queue_id, job_id in self.dequeue:
                await dispatcher.remove(queue_id, [job_id])
        except DispatchUnavailable as exc:
            log.warning("dispatch_push_failed", error=str(exc), jobs=len(self.enqueue))
        try:
            for project_id, events in self.events.items():
                await dispatcher.publish(project_id, events)
        except DispatchUnavailable as exc:
            log.warning("event_publish_failed", error=str(exc))


def audit(
    session: AsyncSession,
    *,
    project_id: uuid.UUID | None,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            project_id=project_id,
            actor_user_id=actor.user_id,
            actor_api_key_id=actor.api_key_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details={"actor": actor.label, **(details or {})},
        )
    )
