"""Worker-side state transitions: claim, lease renewal, fenced completion, graceful release.

PostgreSQL decides ownership. A claim is a compare-and-set on `state = 'queued'` under a row
lock; completion is fenced on (worker_id, attempts) so a worker whose lease expired can
never overwrite the outcome of the attempt that replaced it (ADR 0002).
"""

from __future__ import annotations

import json
import random
import traceback as tb
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from queueforge.db import Database
from queueforge.lifecycle import AttemptOutcome, JobState, RetryPolicy, error_fingerprint
from queueforge.logs import redact_value
from queueforge.models import Job, JobAttempt, Queue, Worker
from queueforge.services.common import AfterCommit, db_now, transition


class ClaimStatus(StrEnum):
    CLAIMED = "claimed"
    STALE = "stale"  # job no longer queued (cancelled, already claimed, deleted)
    PAUSED = "paused"
    AT_CAPACITY = "at_capacity"


@dataclass(frozen=True)
class Claim:
    job_id: uuid.UUID
    project_id: uuid.UUID
    queue_id: uuid.UUID
    queue_name: str
    type: str
    payload: dict[str, Any]
    attempt: int
    max_attempts: int
    timeout_seconds: int
    priority: int
    queued_at: datetime | None
    started_at: datetime


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    claim: Claim | None = None
    score_hint: tuple[int, datetime] | None = None  # (priority, queued_at) to re-add to Redis


async def _claim_locked(
    session: AsyncSession, job: Job, queue: Queue, worker_id: str, lease_seconds: int
) -> ClaimResult:
    if job.state != JobState.QUEUED:
        return ClaimResult(ClaimStatus.STALE)
    hint = (job.priority, job.queued_at or job.created_at)
    if queue.paused:
        return ClaimResult(ClaimStatus.PAUSED, score_hint=hint)
    if queue.concurrency_limit is not None:
        # Serialize claims for this queue so two workers can't both see "one slot left".
        await session.execute(select(Queue.id).where(Queue.id == queue.id).with_for_update())
        running = (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.queue_id == queue.id, Job.state == JobState.RUNNING)
            )
        ).scalar_one()
        if running >= queue.concurrency_limit:
            return ClaimResult(ClaimStatus.AT_CAPACITY, score_hint=hint)

    now = await db_now(session)
    job.attempts += 1
    job.worker_id = worker_id
    job.lease_expires_at = now + timedelta(seconds=lease_seconds)
    job.started_at = job.started_at or now
    job.updated_at = now
    transition(
        session,
        job,
        JobState.RUNNING,
        actor=f"worker:{worker_id}",
        message=f"attempt {job.attempts} of {job.max_attempts} started",
        at=now,
    )
    wait_ms = int((now - job.queued_at).total_seconds() * 1000) if job.queued_at else None
    session.add(
        JobAttempt(
            job_id=job.id,
            project_id=job.project_id,
            attempt_number=job.attempts,
            worker_id=worker_id,
            outcome=AttemptOutcome.RUNNING,
            queued_at=job.queued_at,
            started_at=now,
            wait_ms=max(wait_ms, 0) if wait_ms is not None else None,
        )
    )
    return ClaimResult(
        ClaimStatus.CLAIMED,
        Claim(
            job_id=job.id,
            project_id=job.project_id,
            queue_id=job.queue_id,
            queue_name=queue.name,
            type=job.type,
            payload=job.payload,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
            timeout_seconds=job.timeout_seconds,
            priority=job.priority,
            queued_at=job.queued_at,
            started_at=now,
        ),
    )


async def claim_job(
    db: Database,
    job_id: uuid.UUID,
    worker_id: str,
    lease_seconds: int,
    after: AfterCommit | None = None,
) -> ClaimResult:
    """Claim a specific job popped from Redis."""
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(Job, Queue)
                .join(Queue, Queue.id == Job.queue_id)
                .where(Job.id == job_id)
                .with_for_update(of=Job)
            )
        ).one_or_none()
        if row is None:
            return ClaimResult(ClaimStatus.STALE)
        job, queue = row
        result = await _claim_locked(session, job, queue, worker_id, lease_seconds)
        if result.status == ClaimStatus.CLAIMED and after is not None:
            after.publish(job, queue.name)
    return result


async def claim_next_from_db(
    db: Database,
    queue_ids: list[uuid.UUID],
    worker_id: str,
    lease_seconds: int,
    after: AfterCommit | None = None,
) -> ClaimResult | None:
    """Fallback when Redis is unavailable: pick the best runnable job with SKIP LOCKED."""
    if not queue_ids:
        return None
    running = (
        select(func.count())
        .select_from(Job)
        .where(Job.queue_id == Queue.id, Job.state == JobState.RUNNING)
        .correlate(Queue)
        .scalar_subquery()
    )
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(Job, Queue)
                .join(Queue, Queue.id == Job.queue_id)
                .where(
                    Job.state == JobState.QUEUED,
                    Job.queue_id.in_(queue_ids),
                    Queue.paused.is_(False),
                    or_(Queue.concurrency_limit.is_(None), running < Queue.concurrency_limit),
                )
                .order_by(Job.priority.desc(), Job.queued_at)
                .limit(1)
                .with_for_update(of=Job, skip_locked=True)
            )
        ).one_or_none()
        if row is None:
            return None
        job, queue = row
        result = await _claim_locked(session, job, queue, worker_id, lease_seconds)
        if result.status == ClaimStatus.CLAIMED and after is not None:
            after.publish(job, queue.name)
    return result


@dataclass(frozen=True)
class LeaseStatus:
    renewed: set[uuid.UUID]
    cancel_requested: set[uuid.UUID]
    lost: set[uuid.UUID]


async def renew_leases(
    db: Database,
    worker_id: str,
    attempts: dict[uuid.UUID, int],
    lease_seconds: int,
    heartbeat: dict[str, Any] | None = None,
) -> LeaseStatus:
    """Extend leases for jobs this worker still owns and record the worker heartbeat."""
    renewed: set[uuid.UUID] = set()
    cancel: set[uuid.UUID] = set()
    async with db.transaction() as session:
        if attempts:
            rows = (
                await session.execute(
                    update(Job)
                    .where(
                        Job.id.in_(list(attempts)),
                        Job.worker_id == worker_id,
                        Job.state == JobState.RUNNING,
                    )
                    .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
                    .returning(Job.id, Job.attempts, Job.cancel_requested_at)
                )
            ).all()
            for job_id, attempt, cancel_at in rows:
                if attempts.get(job_id) == attempt:
                    renewed.add(job_id)
                    if cancel_at is not None:
                        cancel.add(job_id)
        if heartbeat is not None:
            await session.execute(
                update(Worker)
                .where(Worker.id == worker_id)
                .values(last_heartbeat_at=func.now(), **heartbeat)
            )
    return LeaseStatus(renewed=renewed, cancel_requested=cancel, lost=set(attempts) - renewed)


def build_error(
    exc: BaseException | None,
    *,
    error_type: str | None = None,
    message: str | None = None,
    retryable: bool,
    attempt: int,
    include_traceback: bool = True,
) -> dict[str, Any]:
    etype = error_type or (type(exc).__name__ if exc else "Error")
    msg = message if message is not None else (str(exc) if exc else "")
    msg = str(redact_value(msg))[:2000] or etype
    error: dict[str, Any] = {
        "type": etype,
        "message": msg,
        "retryable": retryable,
        "attempt": attempt,
        "fingerprint": error_fingerprint(etype, msg),
    }
    if exc is not None and include_traceback:
        frames = "".join(tb.format_exception(type(exc), exc, exc.__traceback__, limit=12))
        error["traceback"] = str(redact_value(frames))[-6000:]
    return error


def bounded_result(result: dict[str, Any], limit: int) -> dict[str, Any]:
    serialized = json.dumps(result, default=str)
    if len(serialized.encode()) <= limit:
        return json.loads(serialized)  # normalizes datetimes etc. to JSON types
    return {
        "truncated": True,
        "bytes": len(serialized.encode()),
        "limit": limit,
        "preview": serialized[: max(0, limit - 512)],
    }


@dataclass(frozen=True)
class Outcome:
    kind: AttemptOutcome  # SUCCEEDED, FAILED, TIMED_OUT or CANCELLED
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


async def complete_job(
    db: Database,
    claim: Claim,
    worker_id: str,
    outcome: Outcome,
    *,
    max_result_bytes: int = 65_536,
    rng: random.Random | None = None,
    after: AfterCommit | None = None,
) -> str | None:
    """Record an attempt's outcome. Returns the job's new state, or None if the lease was lost."""
    async with db.transaction() as session:
        job = (
            await session.execute(
                select(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.state == JobState.RUNNING,
                    Job.worker_id == worker_id,
                    Job.attempts == claim.attempt,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return None
        now = await db_now(session)
        attempt = (
            await session.execute(
                select(JobAttempt).where(
                    JobAttempt.job_id == job.id, JobAttempt.attempt_number == claim.attempt
                )
            )
        ).scalar_one()
        attempt.finished_at = now
        attempt.duration_ms = max(0, int((now - attempt.started_at).total_seconds() * 1000))
        attempt.outcome = outcome.kind
        attempt.error = outcome.error
        actor = f"worker:{worker_id}"
        job.lease_expires_at = None
        job.updated_at = now
        failed_attempt = outcome.kind in (AttemptOutcome.FAILED, AttemptOutcome.TIMED_OUT)

        if outcome.kind == AttemptOutcome.SUCCEEDED:
            job.result = bounded_result(outcome.result or {}, max_result_bytes)
            job.last_error = None
            job.error_fingerprint = None
            job.finished_at = now
            transition(
                session,
                job,
                JobState.SUCCEEDED,
                actor=actor,
                message=f"succeeded in {attempt.duration_ms} ms",
                at=now,
            )
        elif outcome.kind == AttemptOutcome.CANCELLED or job.cancel_requested_at is not None:
            if outcome.error:
                job.last_error = outcome.error
            job.finished_at = now
            transition(
                session,
                job,
                JobState.CANCELLED,
                actor=actor,
                message="cancelled while running",
                at=now,
            )
        else:
            error = outcome.error or build_error(None, retryable=True, attempt=claim.attempt)
            job.last_error = error
            job.error_fingerprint = error["fingerprint"]
            _schedule_retry_or_fail(session, job, error, now, actor, rng)

        await session.execute(
            update(Worker)
            .where(Worker.id == worker_id)
            .values(
                processed_count=Worker.processed_count + 1,
                failed_count=Worker.failed_count + (1 if failed_attempt else 0),
            )
        )
        if after is not None:
            after.publish(job, claim.queue_name)
        return job.state


def _schedule_retry_or_fail(
    session: AsyncSession,
    job: Job,
    error: dict[str, Any],
    now: datetime,
    actor: str,
    rng: random.Random | None,
) -> None:
    policy = RetryPolicy(job.max_attempts, job.backoff_base_seconds, job.backoff_max_seconds)
    if policy.should_retry(job.attempts, bool(error.get("retryable", True))):
        delay = policy.backoff_seconds(job.attempts, rng)
        job.run_at = now + timedelta(seconds=delay)
        job.worker_id = None
        transition(
            session,
            job,
            JobState.RETRYING,
            actor=actor,
            message=f"{error['type']}: retry {job.attempts + 1} of {job.max_attempts} in {delay:.1f}s",
            at=now,
        )
    else:
        job.finished_at = now
        reason = (
            "permanent error"
            if not error.get("retryable", True)
            else f"exhausted {job.max_attempts} attempt(s)"
        )
        transition(
            session,
            job,
            JobState.FAILED,
            actor=actor,
            message=f"{error['type']}: {reason}; moved to dead letter",
            at=now,
        )


async def release_job(db: Database, claim: Claim, worker_id: str, after: AfterCommit) -> bool:
    """Graceful shutdown: hand a running job back to the queue without consuming an attempt."""
    async with db.transaction() as session:
        job = (
            await session.execute(
                select(Job)
                .where(
                    Job.id == claim.job_id,
                    Job.state == JobState.RUNNING,
                    Job.worker_id == worker_id,
                    Job.attempts == claim.attempt,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            return False
        now = await db_now(session)
        await session.execute(
            update(JobAttempt)
            .where(and_(JobAttempt.job_id == job.id, JobAttempt.attempt_number == claim.attempt))
            .values(
                outcome=AttemptOutcome.INTERRUPTED,
                finished_at=now,
                duration_ms=func.greatest(
                    0, func.floor(func.extract("epoch", now - JobAttempt.started_at) * 1000)
                ),
                error={
                    "type": "WorkerShutdown",
                    "message": "worker shut down gracefully; job requeued",
                    "retryable": True,
                    "attempt": claim.attempt,
                },
            )
        )
        # Attempt numbers stay unique; extending the budget means the interruption is free.
        job.max_attempts += 1
        job.worker_id = None
        job.lease_expires_at = None
        job.queued_at = now
        job.updated_at = now
        transition(
            session,
            job,
            JobState.QUEUED,
            actor=f"worker:{worker_id}",
            message="worker shutting down; job returned to queue",
            at=now,
        )
        after.push(job)
        after.publish(job, claim.queue_name)
    return True
