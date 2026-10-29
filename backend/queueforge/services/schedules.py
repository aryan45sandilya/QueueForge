"""Recurring schedules: validation, next-fire computation and CRUD.

All schedule times are UTC. Missed occurrences (scheduler down) are coalesced into a single
run when the scheduler returns, and the cadence then resumes from the current time.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.errors import Conflict, NotFound, ValidationFailed
from queueforge.handlers import validate_payload
from queueforge.models import Job, Project, Queue, Schedule
from queueforge.schemas import ScheduleCreate, ScheduleOut, SchedulePreview, ScheduleUpdate
from queueforge.services.common import Actor, AfterCommit, audit, db_now
from queueforge.services.jobs import NewJobSpec, build_job, check_payload_size, transition_record

_MIN_CRON_GAP = timedelta(seconds=60)


def validate_trigger(cron_expression: str | None, interval_seconds: int | None) -> None:
    if cron_expression is not None:
        expr = cron_expression.strip()
        if len(expr.split()) != 5 or not croniter.is_valid(expr):
            raise ValidationFailed(
                "cron_expression must be a valid 5-field cron expression (minute hour day month weekday)",
                details=[
                    {
                        "loc": ["cron_expression"],
                        "msg": "invalid cron expression",
                        "type": "value_error",
                    }
                ],
            )


def next_fire(
    cron_expression: str | None,
    interval_seconds: int | None,
    after: datetime,
    anchor: datetime | None = None,
) -> datetime:
    """The first occurrence strictly after `after`."""
    after = after.astimezone(UTC)
    if cron_expression is not None:
        nxt = croniter(cron_expression.strip(), after).get_next(datetime)
        return nxt if nxt.tzinfo else nxt.replace(tzinfo=UTC)
    assert interval_seconds is not None
    step = timedelta(seconds=interval_seconds)
    if anchor is None or anchor > after:
        return after + step if anchor is None else anchor
    periods = int((after - anchor) / step) + 1
    return anchor + periods * step


def describe(cron_expression: str | None, interval_seconds: int | None) -> str:
    if interval_seconds is not None:
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if interval_seconds % size == 0:
                n = interval_seconds // size
                return f"Every {n} {unit}{'s' if n != 1 else ''}"
        return f"Every {interval_seconds} seconds"
    return f"Cron '{cron_expression}' (UTC)"


def preview(
    cron_expression: str | None, interval_seconds: int | None, count: int
) -> SchedulePreview:
    validate_trigger(cron_expression, interval_seconds)
    now = datetime.now(UTC).replace(microsecond=0)
    first = next_fire(cron_expression, interval_seconds, now)
    runs = [first]
    while len(runs) < count:
        runs.append(next_fire(cron_expression, interval_seconds, runs[-1], anchor=first))
    if (
        cron_expression is not None
        and len(runs) > 1
        and min(b - a for a, b in itertools.pairwise(runs)) < _MIN_CRON_GAP
    ):
        raise ValidationFailed("Schedules may fire at most once per minute")
    return SchedulePreview(description=describe(cron_expression, interval_seconds), next_runs=runs)


def to_out(schedule: Schedule, queue_name: str, last_state: str | None) -> ScheduleOut:
    base = {
        c: getattr(schedule, c)
        for c in (
            "id",
            "name",
            "job_type",
            "payload",
            "cron_expression",
            "interval_seconds",
            "priority",
            "max_attempts",
            "timeout_seconds",
            "skip_if_running",
            "enabled",
            "next_run_at",
            "last_run_at",
            "last_job_id",
            "last_skipped_at",
            "created_at",
        )
    }
    future: list[datetime] = []
    if schedule.enabled and schedule.next_run_at is not None:
        future = [schedule.next_run_at]
        while len(future) < 3:
            future.append(
                next_fire(
                    schedule.cron_expression,
                    schedule.interval_seconds,
                    future[-1],
                    anchor=future[0],
                )
            )
    return ScheduleOut(**base, queue=queue_name, last_job_state=last_state, upcoming=future)


async def list_schedules(db: Database, project_id: uuid.UUID) -> list[ScheduleOut]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Schedule, Queue.name, Job.state)
                .join(Queue, Queue.id == Schedule.queue_id)
                .outerjoin(Job, Job.id == Schedule.last_job_id)
                .where(Schedule.project_id == project_id)
                .order_by(Schedule.name)
            )
        ).all()
    return [to_out(s, q, st) for s, q, st in rows]


async def _queue(db_session: Any, project_id: uuid.UUID, name: str) -> Queue:
    queue = (
        await db_session.execute(
            select(Queue).where(Queue.project_id == project_id, Queue.name == name)
        )
    ).scalar_one_or_none()
    if queue is None:
        raise ValidationFailed(
            f"Queue {name!r} does not exist in this project",
            details=[{"loc": ["queue"], "msg": "unknown queue", "type": "value_error"}],
        )
    return queue


async def create_schedule(
    db: Database, settings: Settings, project_id: uuid.UUID, req: ScheduleCreate, actor: Actor
) -> ScheduleOut:
    validate_trigger(req.cron_expression, req.interval_seconds)
    preview(req.cron_expression, req.interval_seconds, 3)
    check_payload_size(req.payload, settings)
    validate_payload(req.job_type, req.payload)
    async with db.transaction() as session:
        queue = await _queue(session, project_id, req.queue)
        now = await db_now(session)
        schedule = Schedule(
            id=uuid.uuid4(),
            project_id=project_id,
            queue_id=queue.id,
            name=req.name,
            job_type=req.job_type,
            payload=req.payload,
            cron_expression=req.cron_expression.strip() if req.cron_expression else None,
            interval_seconds=req.interval_seconds,
            priority=req.priority,
            max_attempts=req.max_attempts,
            timeout_seconds=req.timeout_seconds,
            skip_if_running=req.skip_if_running,
            enabled=req.enabled,
            next_run_at=next_fire(req.cron_expression, req.interval_seconds, now),
            created_at=now,
            updated_at=now,
        )
        try:
            async with session.begin_nested():
                session.add(schedule)
                await session.flush()
        except IntegrityError as exc:
            raise Conflict(f"A schedule named {req.name!r} already exists") from exc
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="schedule.created",
            target_type="schedule",
            target_id=str(schedule.id),
            details={"name": req.name},
        )
    return to_out(schedule, queue.name, None)


async def update_schedule(
    db: Database,
    settings: Settings,
    project_id: uuid.UUID,
    schedule_id: uuid.UUID,
    req: ScheduleUpdate,
    actor: Actor,
) -> ScheduleOut:
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(Schedule, Queue.name)
                .join(Queue, Queue.id == Schedule.queue_id)
                .where(Schedule.id == schedule_id, Schedule.project_id == project_id)
                .with_for_update(of=Schedule)
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Schedule not found")
        schedule, queue_name = row
        changes = req.model_dump(exclude_unset=True)
        trigger_changed = "cron_expression" in changes or "interval_seconds" in changes
        if trigger_changed:
            cron = changes.get("cron_expression")
            interval = changes.get("interval_seconds")
            if (cron is None) == (interval is None):
                raise ValidationFailed("specify exactly one of cron_expression or interval_seconds")
            preview(cron, interval, 3)
            schedule.cron_expression = cron.strip() if cron else None
            schedule.interval_seconds = interval
        if "payload" in changes and req.payload is not None:
            check_payload_size(req.payload, settings)
            validate_payload(schedule.job_type, req.payload)
            schedule.payload = req.payload
        for field in (
            "name",
            "priority",
            "max_attempts",
            "timeout_seconds",
            "skip_if_running",
            "enabled",
        ):
            if field in changes and changes[field] is not None:
                setattr(schedule, field, changes[field])
        now = await db_now(session)
        if trigger_changed or ("enabled" in changes and schedule.enabled):
            schedule.next_run_at = next_fire(
                schedule.cron_expression, schedule.interval_seconds, now
            )
        schedule.updated_at = now
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise Conflict("A schedule with that name already exists") from exc
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="schedule.updated",
            target_type="schedule",
            target_id=str(schedule.id),
            details={"fields": sorted(changes)},
        )
        last_state = None
        if schedule.last_job_id:
            last_state = (
                await session.execute(select(Job.state).where(Job.id == schedule.last_job_id))
            ).scalar_one_or_none()
    return to_out(schedule, queue_name, last_state)


async def delete_schedule(
    db: Database, project_id: uuid.UUID, schedule_id: uuid.UUID, actor: Actor
) -> None:
    async with db.transaction() as session:
        schedule = (
            await session.execute(
                select(Schedule).where(
                    Schedule.id == schedule_id, Schedule.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        if schedule is None:
            raise NotFound("Schedule not found")
        await session.delete(schedule)
        audit(
            session,
            project_id=project_id,
            actor=actor,
            action="schedule.deleted",
            target_type="schedule",
            target_id=str(schedule_id),
            details={"name": schedule.name},
        )


async def trigger_now(
    db: Database,
    dispatcher: Dispatcher | None,
    project: Project,
    schedule_id: uuid.UUID,
    actor: Actor,
) -> uuid.UUID:
    """Run a schedule immediately, outside its cadence. Doesn't change next_run_at."""
    after = AfterCommit()
    async with db.transaction() as session:
        row = (
            await session.execute(
                select(Schedule, Queue)
                .join(Queue, Queue.id == Schedule.queue_id)
                .where(Schedule.id == schedule_id, Schedule.project_id == project.id)
                .with_for_update(of=Schedule)
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Schedule not found")
        schedule, queue = row
        validate_payload(schedule.job_type, schedule.payload)
        now = await db_now(session)
        job = build_job(spec_for(schedule, queue, project, now, None), project.id, now, actor)
        session.add(job)
        await session.flush()
        transition_record(session, job, actor.label, f"manual run of schedule {schedule.name!r}")
        schedule.last_job_id = job.id
        schedule.last_run_at = now
        after.push(job)
        after.publish(job, queue.name)
        audit(
            session,
            project_id=project.id,
            actor=actor,
            action="schedule.triggered",
            target_type="schedule",
            target_id=str(schedule.id),
            details={"job_id": str(job.id)},
        )
    await after.apply(dispatcher)
    return job.id


def spec_for(
    schedule: Schedule, queue: Queue, project: Project, now: datetime, occurrence: datetime | None
) -> NewJobSpec:
    from queueforge.handlers import get_handler

    handler = get_handler(schedule.job_type)
    default_timeout = (
        handler.default_timeout_seconds if handler else project.default_timeout_seconds
    )
    return NewJobSpec(
        type=schedule.job_type,
        payload=schedule.payload,
        queue=queue,
        priority=schedule.priority,
        run_at=now,
        max_attempts=schedule.max_attempts or project.default_max_attempts,
        timeout_seconds=schedule.timeout_seconds
        or min(default_timeout, project.default_timeout_seconds),
        backoff_base_seconds=project.default_backoff_base_seconds,
        backoff_max_seconds=project.default_backoff_max_seconds,
        schedule_id=schedule.id,
        scheduled_for=occurrence,
    )
