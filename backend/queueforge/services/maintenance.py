"""Scheduler tasks: promotion, recurring schedules, lease recovery, reconciliation, retention.

Every task is safe to run concurrently from several scheduler processes: rows are claimed
with FOR UPDATE SKIP LOCKED and Redis operations are idempotent.
"""

from __future__ import annotations

import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import QUEUE_KEY_PREFIX, Dispatcher, DispatchUnavailable
from queueforge.errors import QuotaExceeded, ValidationFailed
from queueforge.handlers import validate_payload
from queueforge.lifecycle import ACTIVE_STATES, AttemptOutcome, JobState
from queueforge.logs import get_logger
from queueforge.models import AuditEvent, Job, JobAttempt, Project, Queue, Schedule, Session, Worker
from queueforge.services.common import Actor, AfterCommit, audit, db_now, transition
from queueforge.services.execution import _schedule_retry_or_fail, build_error
from queueforge.services.jobs import build_job, check_daily_quota, transition_record
from queueforge.services.schedules import next_fire, spec_for

log = get_logger("queueforge.scheduler")
SCHEDULER = Actor.system("scheduler")


async def promote_due(db: Database, dispatcher: Dispatcher | None, limit: int = 500) -> int:
    """Move scheduled/retrying jobs whose run_at has passed into the queue."""
    after = AfterCommit()
    async with db.transaction() as session:
        now = await db_now(session)
        jobs = (
            (
                await session.execute(
                    select(Job)
                    .where(
                        Job.state.in_([JobState.SCHEDULED, JobState.RETRYING]), Job.run_at <= now
                    )
                    .order_by(Job.run_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            message = "retry due" if job.state == JobState.RETRYING else "scheduled time reached"
            job.queued_at = now
            transition(
                session, job, JobState.QUEUED, actor=SCHEDULER.label, message=message, at=now
            )
            after.push(job)
            after.publish(job)
    await after.apply(dispatcher)
    return len(jobs)


async def materialize_schedules(
    db: Database, dispatcher: Dispatcher | None, limit: int = 100, settings: Settings | None = None
) -> int:
    """Create jobs for due recurring schedules. One job per (schedule, occurrence)."""
    after = AfterCommit()
    created = 0
    async with db.transaction() as session:
        now = await db_now(session)
        rows = (
            await session.execute(
                select(Schedule, Queue, Project)
                .join(Queue, Queue.id == Schedule.queue_id)
                .join(Project, Project.id == Schedule.project_id)
                .where(Schedule.enabled.is_(True), Schedule.next_run_at <= now)
                .order_by(Schedule.next_run_at)
                .limit(limit)
                .with_for_update(of=Schedule, skip_locked=True)
            )
        ).all()
        for schedule, queue, project in rows:
            occurrence = schedule.next_run_at
            assert occurrence is not None
            # Coalesce missed occurrences: the next run is computed from now, not from the past.
            schedule.next_run_at = next_fire(
                schedule.cron_expression,
                schedule.interval_seconds,
                now,
                anchor=occurrence if schedule.interval_seconds else None,
            )
            schedule.updated_at = now
            if schedule.skip_if_running and schedule.last_job_id is not None:
                last_state = (
                    await session.execute(select(Job.state).where(Job.id == schedule.last_job_id))
                ).scalar_one_or_none()
                if last_state in ACTIVE_STATES:
                    schedule.last_skipped_at = now
                    log.info("schedule_skipped_overlap", schedule_id=str(schedule.id))
                    continue
            try:
                validate_payload(schedule.job_type, schedule.payload, settings)
            except ValidationFailed as exc:
                schedule.enabled = False
                audit(
                    session,
                    project_id=schedule.project_id,
                    actor=SCHEDULER,
                    action="schedule.disabled",
                    target_type="schedule",
                    target_id=str(schedule.id),
                    details={"reason": exc.message},
                )
                continue
            if settings is not None:
                try:
                    await check_daily_quota(session, project.id, settings)
                except QuotaExceeded:
                    schedule.last_skipped_at = now
                    log.info("schedule_skipped_quota", schedule_id=str(schedule.id))
                    continue
            job = build_job(
                spec_for(schedule, queue, project, now, occurrence), project.id, now, SCHEDULER
            )
            try:
                async with session.begin_nested():
                    session.add(job)
                    await session.flush()
            except IntegrityError:
                continue  # this occurrence was already materialized
            transition_record(
                session,
                job,
                SCHEDULER.label,
                f"schedule {schedule.name!r} occurrence {occurrence.isoformat()}",
            )
            schedule.last_job_id = job.id
            schedule.last_run_at = now
            after.push(job)
            after.publish(job, queue.name)
            created += 1
    await after.apply(dispatcher)
    return created


async def recover_expired_leases(
    db: Database, dispatcher: Dispatcher | None, limit: int = 200, rng: random.Random | None = None
) -> int:
    """Running jobs whose worker stopped renewing the lease are retried or dead-lettered."""
    after = AfterCommit()
    async with db.transaction() as session:
        now = await db_now(session)
        jobs = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.state == JobState.RUNNING, Job.lease_expires_at < now)
                    .order_by(Job.lease_expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            worker = job.worker_id or "unknown"
            error = build_error(
                None,
                error_type="WorkerLost",
                message=f"Lease expired at {job.lease_expires_at:%H:%M:%S} UTC; worker {worker} stopped renewing it",
                retryable=True,
                attempt=job.attempts,
            )
            await session.execute(
                update(JobAttempt)
                .where(
                    JobAttempt.job_id == job.id,
                    JobAttempt.attempt_number == job.attempts,
                    JobAttempt.outcome == AttemptOutcome.RUNNING,
                )
                .values(
                    outcome=AttemptOutcome.LOST,
                    finished_at=now,
                    error=error,
                    duration_ms=func.greatest(
                        0, func.floor(func.extract("epoch", now - JobAttempt.started_at) * 1000)
                    ),
                )
            )
            job.lease_expires_at = None
            job.updated_at = now
            job.last_error = error
            job.error_fingerprint = error["fingerprint"]
            if job.cancel_requested_at is not None:
                job.finished_at = now
                transition(
                    session,
                    job,
                    JobState.CANCELLED,
                    actor=SCHEDULER.label,
                    message="worker lost while cancellation was pending",
                    at=now,
                )
            else:
                _schedule_retry_or_fail(session, job, error, now, SCHEDULER.label, rng)
            job.worker_id = None
            after.publish(job)
            log.warning(
                "lease_expired_recovered", job_id=str(job.id), worker_id=worker, new_state=job.state
            )
    await after.apply(dispatcher)
    return len(jobs)


@dataclass
class ReconcileReport:
    pushed_missing: int = 0
    removed_stale: int = 0
    removed_orphan_keys: int = 0
    redis_available: bool = True
    by_queue: dict[str, int] = field(default_factory=dict)


async def reconcile_dispatch(
    db: Database,
    dispatcher: Dispatcher,
    grace_seconds: float = 10.0,
    batch: int = 2000,
    head: int = 200,
) -> ReconcileReport:
    """Make Redis dispatch sets agree with PostgreSQL (see docs/architecture.md)."""
    report = ReconcileReport()
    async with db.session() as session:
        now = await db_now(session)
        queued = (
            await session.execute(
                select(Job.id, Job.queue_id, Job.priority, Job.queued_at)
                .where(
                    Job.state == JobState.QUEUED,
                    Job.queued_at < now - timedelta(seconds=grace_seconds),
                )
                .order_by(Job.queued_at)
                .limit(batch)
            )
        ).all()
        queue_ids = set((await session.execute(select(Queue.id))).scalars().all())

    try:
        # 1. queued in PostgreSQL but missing from Redis -> push.
        grouped: dict[uuid.UUID, list[tuple[uuid.UUID, int, object]]] = defaultdict(list)
        for job_id, queue_id, priority, queued_at in queued:
            grouped[queue_id].append((job_id, priority, queued_at))
        for queue_id, items in grouped.items():
            missing = set(await dispatcher.missing(queue_id, [str(i[0]) for i in items]))
            if missing:
                entries = [
                    (jid, queue_id, prio, qat) for jid, prio, qat in items if str(jid) in missing
                ]
                await dispatcher.enqueue(entries)  # type: ignore[arg-type]
                report.pushed_missing += len(entries)
                report.by_queue[str(queue_id)] = len(entries)

        # 2. IDs in Redis whose job is no longer queued -> remove; keys of deleted queues -> drop.
        keys: list[str] = [
            k async for k in dispatcher.redis.scan_iter(match=f"{QUEUE_KEY_PREFIX}*", count=500)
        ]
        for key in keys:
            raw_id = key.removeprefix(QUEUE_KEY_PREFIX)
            try:
                queue_id = uuid.UUID(raw_id)
            except ValueError:
                continue
            if queue_id not in queue_ids:
                await dispatcher.delete_queue(queue_id)
                report.removed_orphan_keys += 1
                continue
            members = await dispatcher.head(queue_id, head)
            if not members:
                continue
            ids = []
            for m in members:
                try:
                    ids.append(uuid.UUID(m))
                except ValueError:
                    continue
            async with db.session() as session:
                still_queued = {
                    str(i)
                    for i in (
                        await session.execute(
                            select(Job.id).where(Job.id.in_(ids), Job.state == JobState.QUEUED)
                        )
                    ).scalars()
                }
            stale = [m for m in members if m not in still_queued]
            if stale:
                report.removed_stale += await dispatcher.remove(queue_id, stale)
    except DispatchUnavailable as exc:
        report.redis_available = False
        log.warning("reconcile_redis_unavailable", error=str(exc))
    if report.pushed_missing or report.removed_stale or report.removed_orphan_keys:
        log.info(
            "reconcile_repaired",
            pushed=report.pushed_missing,
            removed=report.removed_stale,
            orphan_keys=report.removed_orphan_keys,
        )
    return report


async def mark_lost_workers(db: Database, heartbeat_seconds: float) -> int:
    async with db.transaction() as session:
        result = await session.execute(
            update(Worker)
            .where(
                Worker.state.in_(["running", "draining"]),
                Worker.last_heartbeat_at < func.now() - timedelta(seconds=heartbeat_seconds * 3),
            )
            .values(state="lost")
            .returning(Worker.id)
        )
        lost = result.scalars().all()
        await session.execute(
            delete(Worker).where(
                Worker.state.in_(["stopped", "lost"]),
                Worker.last_heartbeat_at < func.now() - timedelta(days=7),
            )
        )
    for worker_id in lost:
        log.warning("worker_lost", worker_id=worker_id)
    return len(lost)


async def enforce_retention(db: Database, batch: int = 1000) -> int:
    """Delete terminal jobs past their project's retention, plus expired sessions."""
    async with db.transaction() as session:
        result = await session.execute(
            text(
                """
                DELETE FROM jobs WHERE id IN (
                    SELECT j.id FROM jobs j JOIN projects p ON p.id = j.project_id
                    WHERE j.state IN ('succeeded', 'failed', 'cancelled')
                      AND j.finished_at < now() - make_interval(days => p.retention_days)
                    LIMIT :batch
                    FOR UPDATE OF j SKIP LOCKED
                )
                """
            ),
            {"batch": batch},
        )
        deleted = int(getattr(result, "rowcount", 0) or 0)
        await session.execute(delete(Session).where(Session.expires_at < func.now()))
        await session.execute(
            delete(AuditEvent).where(AuditEvent.at < func.now() - timedelta(days=365))
        )
    if deleted:
        log.info("retention_deleted_jobs", count=deleted)
    return deleted
