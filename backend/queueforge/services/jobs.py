"""API-side job operations: submission, queries, cancellation, manual retry."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.errors import (
    BacklogFull,
    Conflict,
    NotFound,
    PayloadTooLarge,
    QuotaExceeded,
    ValidationFailed,
)
from queueforge.handlers import get_handler, validate_payload
from queueforge.lifecycle import (
    BACKLOG_STATES,
    CANCELLABLE_STATES,
    RETRYABLE_BY_USER,
    JobState,
)
from queueforge.models import ApiKey, Job, JobAttempt, JobEvent, Project, Queue, User
from queueforge.schemas import (
    AttemptOut,
    BulkItemResult,
    BulkResult,
    EventOut,
    FailureGroup,
    JobCreate,
    JobDetail,
    JobSummary,
    Page,
)
from queueforge.services.common import Actor, AfterCommit, audit, db_now, transition

SORTABLE = {
    "created_at": Job.created_at,
    "updated_at": Job.updated_at,
    "finished_at": Job.finished_at,
    "priority": Job.priority,
    "attempts": Job.attempts,
    "run_at": Job.run_at,
}


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(job_type: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256(f"{job_type}\n{canonical_json(payload)}".encode()).hexdigest()


def summary(job: Job, queue_name: str) -> JobSummary:
    err = job.last_error or {}
    return JobSummary(
        id=job.id,
        type=job.type,
        state=JobState(job.state),
        queue=queue_name,
        priority=job.priority,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        idempotency_key=job.idempotency_key,
        run_at=job.run_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        cancel_requested=job.cancel_requested_at is not None,
        error_type=err.get("type"),
        error_message=err.get("message"),
        schedule_id=job.schedule_id,
    )


async def _get_queue(session: AsyncSession, project_id: uuid.UUID, name: str) -> Queue:
    queue = (
        await session.execute(
            select(Queue).where(Queue.project_id == project_id, Queue.name == name)
        )
    ).scalar_one_or_none()
    if queue is None:
        raise ValidationFailed(
            f"Queue {name!r} does not exist in this project",
            details=[{"loc": ["queue"], "msg": "unknown queue", "type": "value_error"}],
        )
    return queue


async def _backlog_size(session: AsyncSession, project_id: uuid.UUID, cap: int) -> int:
    """Counts backlog rows, stopping at cap + 1 so the check stays cheap on huge backlogs."""
    inner = (
        select(Job.id)
        .where(Job.project_id == project_id, Job.state.in_([s.value for s in BACKLOG_STATES]))
        .limit(cap + 1)
        .subquery()
    )
    return int((await session.execute(select(func.count()).select_from(inner))).scalar_one())


@dataclass
class NewJobSpec:
    """A validated job, independent of whether it came from the API or a schedule."""

    type: str
    payload: dict[str, Any]
    queue: Queue
    priority: int
    run_at: datetime
    max_attempts: int
    timeout_seconds: int
    backoff_base_seconds: int
    backoff_max_seconds: int
    idempotency_key: str | None = None
    schedule_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None


def build_job(spec: NewJobSpec, project_id: uuid.UUID, now: datetime, actor: Actor) -> Job:
    runnable = spec.run_at <= now
    return Job(
        id=uuid.uuid4(),
        project_id=project_id,
        queue_id=spec.queue.id,
        type=spec.type,
        state=JobState.QUEUED.value if runnable else JobState.SCHEDULED.value,
        priority=spec.priority,
        payload=spec.payload,
        payload_hash=payload_hash(spec.type, spec.payload),
        idempotency_key=spec.idempotency_key,
        attempts=0,
        max_attempts=spec.max_attempts,
        attempt_budget=spec.max_attempts,
        timeout_seconds=spec.timeout_seconds,
        backoff_base_seconds=spec.backoff_base_seconds,
        backoff_max_seconds=spec.backoff_max_seconds,
        manual_retries=0,
        run_at=spec.run_at if not runnable else now,
        queued_at=now if runnable else None,
        created_at=now,
        updated_at=now,
        schedule_id=spec.schedule_id,
        scheduled_for=spec.scheduled_for,
        created_by_api_key_id=actor.api_key_id,
        created_by_user_id=actor.user_id,
    )


def check_payload_size(payload: dict[str, Any], settings: Settings) -> None:
    size = len(canonical_json(payload).encode())
    if size > settings.max_payload_bytes:
        raise PayloadTooLarge(
            f"Job payload is {size} bytes; the limit is {settings.max_payload_bytes} bytes",
            details={"size": size, "limit": settings.max_payload_bytes},
        )


async def check_daily_quota(
    session: AsyncSession, project_id: uuid.UUID, settings: Settings
) -> None:
    """Reject submissions once a project created QF_MAX_JOBS_PER_DAY_PER_PROJECT jobs in 24 h."""
    limit = settings.max_jobs_per_day_per_project
    recent = (
        select(Job.id)
        .where(Job.project_id == project_id, Job.created_at > func.now() - timedelta(days=1))
        .limit(limit)
        .subquery()
    )
    count = int((await session.execute(select(func.count()).select_from(recent))).scalar_one())
    if count >= limit:
        raise QuotaExceeded(
            f"This project created {limit} jobs in the last 24 hours, the limit on this instance",
            details={"limit": limit, "window_hours": 24},
            headers={"Retry-After": "3600"},
        )


async def submit_job(
    db: Database,
    dispatcher: Dispatcher | None,
    settings: Settings,
    project: Project,
    req: JobCreate,
    actor: Actor,
) -> tuple[uuid.UUID, bool]:
    """Persist a job. Returns (job_id, created). The job is durable before this returns."""
    check_payload_size(req.payload, settings)
    validate_payload(req.type, req.payload, settings)
    if req.timeout_seconds is not None and req.timeout_seconds > settings.max_job_timeout_seconds:
        raise ValidationFailed(
            f"timeout_seconds may be at most {settings.max_job_timeout_seconds} on this instance",
            details=[{"loc": ["timeout_seconds"], "msg": "too large", "type": "less_than_equal"}],
        )
    handler = get_handler(req.type)
    assert handler is not None
    digest = payload_hash(req.type, req.payload)
    after = AfterCommit()

    async with db.transaction() as session:
        if req.idempotency_key:
            existing = await _find_by_idempotency(session, project.id, req.idempotency_key)
            if existing is not None:
                return _idempotent_match(existing, req.type, digest), False

        queue = await _get_queue(session, project.id, req.queue)
        await check_daily_quota(session, project.id, settings)
        if (
            await _backlog_size(session, project.id, project.max_queued_jobs)
            >= project.max_queued_jobs
        ):
            raise BacklogFull(
                f"Project backlog is at its limit of {project.max_queued_jobs} pending jobs",
                details={"limit": project.max_queued_jobs},
                headers={"Retry-After": "30"},
            )
        now = await db_now(session)
        if req.run_at is not None:
            run_at = req.run_at
        elif req.delay_seconds:
            run_at = now + timedelta(seconds=req.delay_seconds)
        else:
            run_at = now
        base = req.backoff_base_seconds or project.default_backoff_base_seconds
        ceiling = req.backoff_max_seconds or project.default_backoff_max_seconds
        if base > ceiling:
            raise ValidationFailed(
                "backoff_base_seconds must not exceed backoff_max_seconds",
                details=[
                    {
                        "loc": ["backoff_base_seconds"],
                        "msg": "greater than backoff_max_seconds",
                        "type": "value_error",
                    }
                ],
            )
        job = build_job(
            NewJobSpec(
                type=req.type,
                payload=req.payload,
                queue=queue,
                priority=req.priority,
                run_at=run_at,
                max_attempts=req.max_attempts or project.default_max_attempts,
                timeout_seconds=req.timeout_seconds
                or min(
                    handler.default_timeout_seconds,
                    project.default_timeout_seconds,
                    settings.max_job_timeout_seconds,
                ),
                backoff_base_seconds=base,
                backoff_max_seconds=ceiling,
                idempotency_key=req.idempotency_key,
            ),
            project.id,
            now,
            actor,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError:
            # A concurrent request with the same idempotency key committed first.
            if not req.idempotency_key:
                raise
            existing = await _find_by_idempotency(session, project.id, req.idempotency_key)
            if existing is None:
                raise
            return _idempotent_match(existing, req.type, digest), False

        message = (
            "submitted" if job.state == JobState.QUEUED else f"scheduled for {run_at.isoformat()}"
        )
        transition_record(session, job, actor.label, message)
        if job.state == JobState.QUEUED:
            after.push(job)
        after.publish(job, queue.name)

    await after.apply(dispatcher)
    return job.id, True


def transition_record(session: AsyncSession, job: Job, actor: str, message: str) -> None:
    session.add(
        JobEvent(
            job_id=job.id,
            project_id=job.project_id,
            from_state=None,
            to_state=job.state,
            actor=actor[:160],
            message=message,
            at=job.created_at,
        )
    )


async def _find_by_idempotency(
    session: AsyncSession, project_id: uuid.UUID, key: str
) -> Job | None:
    return (
        await session.execute(
            select(Job).where(Job.project_id == project_id, Job.idempotency_key == key)
        )
    ).scalar_one_or_none()


def _idempotent_match(existing: Job, job_type: str, digest: str) -> uuid.UUID:
    if existing.type != job_type or existing.payload_hash != digest:
        raise Conflict(
            "Idempotency key was already used with a different job type or payload",
            details={"job_id": str(existing.id)},
        )
    return existing.id


# --- queries ----------------------------------------------------------------------------------


@dataclass
class JobFilters:
    states: list[str] | None = None
    types: list[str] | None = None
    queues: list[str] | None = None
    search: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    schedule_id: uuid.UUID | None = None
    error_fingerprint: str | None = None
    sort: str = "created_at"
    order: str = "desc"
    limit: int = 50
    offset: int = 0


async def list_jobs(db: Database, project_id: uuid.UUID, f: JobFilters) -> Page[JobSummary]:
    conditions: list[Any] = [Job.project_id == project_id]
    if f.states:
        conditions.append(Job.state.in_(f.states))
    if f.types:
        conditions.append(Job.type.in_(f.types))
    if f.queues:
        conditions.append(Queue.name.in_(f.queues))
    if f.created_from:
        conditions.append(Job.created_at >= f.created_from)
    if f.created_to:
        conditions.append(Job.created_at < f.created_to)
    if f.schedule_id:
        conditions.append(Job.schedule_id == f.schedule_id)
    if f.error_fingerprint:
        conditions.append(Job.error_fingerprint == f.error_fingerprint)
    if f.search:
        term = f.search.strip()
        try:
            conditions.append(Job.id == uuid.UUID(term))
        except ValueError:
            like = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            conditions.append(
                or_(
                    cast(Job.id, String).like(like.lower()),
                    Job.idempotency_key.like(like),
                    Job.type.like(like),
                )
            )
    where = and_(*conditions)
    sort_col = SORTABLE.get(f.sort, Job.created_at)
    ordering = sort_col.desc().nulls_last() if f.order == "desc" else sort_col.asc().nulls_last()

    async with db.session() as session:
        total = (
            await session.execute(
                select(func.count())
                .select_from(Job)
                .join(Queue, Queue.id == Job.queue_id)
                .where(where)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(Job, Queue.name)
                .join(Queue, Queue.id == Job.queue_id)
                .where(where)
                .order_by(ordering, Job.id)
                .limit(f.limit)
                .offset(f.offset)
            )
        ).all()
    return Page[JobSummary](
        items=[summary(job, qname) for job, qname in rows],
        total=int(total),
        limit=f.limit,
        offset=f.offset,
    )


async def get_job_detail(db: Database, project_id: uuid.UUID, job_id: uuid.UUID) -> JobDetail:
    async with db.session() as session:
        row = (
            await session.execute(
                select(Job, Queue.name, User.email, ApiKey.name, ApiKey.prefix)
                .join(Queue, Queue.id == Job.queue_id)
                .outerjoin(User, User.id == Job.created_by_user_id)
                .outerjoin(ApiKey, ApiKey.id == Job.created_by_api_key_id)
                .where(Job.id == job_id, Job.project_id == project_id)
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Job not found")
        job, queue_name, email, key_name, key_prefix = row
        attempts = (
            (
                await session.execute(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await session.execute(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.id)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    created_by = (
        f"user:{email}"
        if email
        else f"api key {key_name} ({key_prefix})"
        if key_prefix
        else ("schedule" if job.schedule_id else None)
    )
    base = summary(job, queue_name).model_dump()
    return JobDetail(
        **base,
        payload=job.payload,
        result=job.result,
        last_error=job.last_error,
        timeout_seconds=job.timeout_seconds,
        backoff_base_seconds=job.backoff_base_seconds,
        backoff_max_seconds=job.backoff_max_seconds,
        attempt_budget=job.attempt_budget,
        manual_retries=job.manual_retries,
        queued_at=job.queued_at,
        lease_expires_at=job.lease_expires_at,
        worker_id=job.worker_id,
        scheduled_for=job.scheduled_for,
        created_by=created_by,
        attempt_history=[AttemptOut.model_validate(a) for a in attempts],
        events=[EventOut.model_validate(e) for e in events],
    )


# --- cancellation and retry -------------------------------------------------------------------


async def _lock_job(
    session: AsyncSession, project_id: uuid.UUID, job_id: uuid.UUID
) -> tuple[Job, str]:
    row = (
        await session.execute(
            select(Job, Queue.name)
            .join(Queue, Queue.id == Job.queue_id)
            .where(Job.id == job_id, Job.project_id == project_id)
            .with_for_update(of=Job)
        )
    ).one_or_none()
    if row is None:
        raise NotFound("Job not found")
    return row[0], row[1]


async def cancel_job(
    db: Database,
    dispatcher: Dispatcher | None,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: Actor,
) -> str:
    """Cancel a pending job immediately, or request cancellation of a running one.

    A running handler is cancelled by its worker at the next lease renewal
    (within `heartbeat_seconds`); the job then becomes `cancelled`.
    """
    after = AfterCommit()
    async with db.transaction() as session:
        job, queue_name = await _lock_job(session, project_id, job_id)
        if job.state not in CANCELLABLE_STATES:
            raise Conflict(
                f"Job is {job.state} and can no longer be cancelled", details={"state": job.state}
            )
        now = await db_now(session)
        if job.state == JobState.RUNNING:
            if job.cancel_requested_at is None:
                job.cancel_requested_at = now
                job.updated_at = now
                transition(
                    session,
                    job,
                    JobState.RUNNING,
                    actor=actor.label,
                    message="cancellation requested; the worker will stop the handler",
                    at=now,
                )
        else:
            was_queued = job.state == JobState.QUEUED
            job.cancel_requested_at = now
            job.finished_at = now
            transition(
                session, job, JobState.CANCELLED, actor=actor.label, message="cancelled", at=now
            )
            if was_queued:
                after.pull(job)
        after.publish(job, queue_name)
        state = job.state
    await after.apply(dispatcher)
    return state


async def retry_job(
    db: Database,
    dispatcher: Dispatcher | None,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    actor: Actor,
) -> str:
    """Move a failed (dead-lettered) or cancelled job back to the queue with a fresh budget."""
    after = AfterCommit()
    async with db.transaction() as session:
        job, queue_name = await _lock_job(session, project_id, job_id)
        _apply_retry(session, job, actor, await db_now(session))
        after.push(job)
        after.publish(job, queue_name)
    await after.apply(dispatcher)
    return JobState.QUEUED.value


def _apply_retry(session: AsyncSession, job: Job, actor: Actor, now: datetime) -> None:
    if job.state not in RETRYABLE_BY_USER:
        raise Conflict(
            f"Only failed or cancelled jobs can be retried; this job is {job.state}",
            details={"state": job.state},
        )
    job.max_attempts = job.attempts + job.attempt_budget
    job.manual_retries += 1
    job.cancel_requested_at = None
    job.finished_at = None
    job.run_at = now
    job.queued_at = now
    job.lease_expires_at = None
    job.worker_id = None
    transition(
        session,
        job,
        JobState.QUEUED,
        actor=actor.label,
        message=f"manual retry #{job.manual_retries}: {job.attempt_budget} more attempt(s)",
        at=now,
    )


async def bulk_action(
    db: Database,
    dispatcher: Dispatcher | None,
    project_id: uuid.UUID,
    action: str,
    job_ids: list[uuid.UUID],
    actor: Actor,
) -> BulkResult:
    """Applies the action to each job independently; one failure doesn't affect the others."""
    results: list[BulkItemResult] = []
    for job_id in dict.fromkeys(job_ids):
        try:
            if action == "cancel":
                state = await cancel_job(db, dispatcher, project_id, job_id, actor)
            else:
                state = await retry_job(db, dispatcher, project_id, job_id, actor)
            results.append(BulkItemResult(job_id=job_id, ok=True, state=state))
        except (Conflict, NotFound) as exc:
            results.append(BulkItemResult(job_id=job_id, ok=False, error=exc.message))
    async with db.transaction() as session:
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action=f"jobs.bulk_{action}",
            target_type="job",
            details={"requested": len(job_ids), "succeeded": sum(r.ok for r in results)},
        )
    ok = sum(1 for r in results if r.ok)
    return BulkResult(action=action, succeeded=ok, failed=len(results) - ok, results=results)


async def failure_groups(
    db: Database, project_id: uuid.UUID, limit: int = 50
) -> list[FailureGroup]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(
                    Job.error_fingerprint,
                    func.count().label("n"),
                    func.min(Job.finished_at).label("first"),
                    func.max(Job.finished_at).label("last"),
                    func.array_agg(Job.type.distinct()).label("types"),
                    func.array_agg(Queue.name.distinct()).label("queues"),
                    func.array_agg(aggregate_order_by(Job.id, Job.finished_at.desc())).label("ids"),
                )
                .join(Queue, Queue.id == Job.queue_id)
                .where(
                    Job.project_id == project_id,
                    Job.state == JobState.FAILED,
                    Job.error_fingerprint.is_not(None),
                )
                .group_by(Job.error_fingerprint)
                .order_by(func.count().desc(), func.max(Job.finished_at).desc())
                .limit(limit)
            )
        ).all()
        sample_ids = [row.ids[0] for row in rows]
        samples: dict[uuid.UUID, dict[str, Any] | None] = (
            dict(
                (
                    await session.execute(
                        select(Job.id, Job.last_error).where(Job.id.in_(sample_ids))
                    )
                ).all()
            )
            if sample_ids
            else {}
        )
    groups: list[FailureGroup] = []
    for row in rows:
        error = samples.get(row.ids[0]) or {}
        groups.append(
            FailureGroup(
                fingerprint=row.error_fingerprint,
                error_type=error.get("type", "Error"),
                sample_message=str(error.get("message", ""))[:500],
                count=int(row.n),
                job_types=sorted(row.types or []),
                queues=sorted(row.queues or []),
                first_failed_at=row.first,
                last_failed_at=row.last,
                sample_job_id=row.ids[0],
                retryable=bool(error.get("retryable", True)),
            )
        )
    return groups


async def retry_failure_group(
    db: Database,
    dispatcher: Dispatcher | None,
    project_id: uuid.UUID,
    fingerprint: str,
    actor: Actor,
) -> int:
    after = AfterCommit()
    async with db.transaction() as session:
        jobs = (
            (
                await session.execute(
                    select(Job)
                    .where(
                        Job.project_id == project_id,
                        Job.state == JobState.FAILED,
                        Job.error_fingerprint == fingerprint,
                    )
                    .order_by(Job.finished_at)
                    .limit(1000)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        now = await db_now(session)
        for job in jobs:
            _apply_retry(session, job, actor, now)
            after.push(job)
            after.publish(job)
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="jobs.retry_failure_group",
            target_type="failure_group",
            target_id=fingerprint,
            details={"retried": len(jobs)},
        )
    await after.apply(dispatcher)
    return len(jobs)
