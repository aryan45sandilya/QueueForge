"""Concurrency: claim races, fenced completion, duplicate dispatch, parallel schedulers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.lifecycle import AttemptOutcome
from queueforge.schemas import ScheduleCreate
from queueforge.services import maintenance, schedules
from queueforge.services.execution import ClaimStatus, Outcome, claim_job, complete_job
from tests.conftest import wait_for
from tests.helpers import ACTOR, fetch, make_project, running_worker, scalar, submit


async def test_exactly_one_concurrent_claim_wins(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(db, dispatcher, settings, project)
    results = await asyncio.gather(*[claim_job(db, job, f"w{i}", 30) for i in range(20)])
    statuses = [r.status for r in results]
    assert statuses.count(ClaimStatus.CLAIMED) == 1
    assert statuses.count(ClaimStatus.STALE) == 19
    row = (await fetch(db, "SELECT attempts, state FROM jobs WHERE id = :i", i=job))[0]
    assert tuple(row) == (1, "running")
    assert await scalar(db, "SELECT count(*) FROM job_attempts WHERE job_id = :i", i=job) == 1


async def test_many_workers_process_each_job_exactly_once(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    ids = [
        await submit(db, dispatcher, settings, project, payload={"work_ms": 20}) for _ in range(60)
    ]
    async with (
        running_worker(settings, db, dispatcher, concurrency=5),
        running_worker(settings, db, dispatcher, concurrency=5),
        running_worker(settings, db, dispatcher, concurrency=5),
    ):

        async def done() -> bool:
            return await scalar(db, "SELECT count(*) FROM jobs WHERE state = 'succeeded'") == 60

        await wait_for(done, timeout=30)
    assert await scalar(db, "SELECT count(*) FROM job_attempts") == 60
    assert await scalar(db, "SELECT max(attempts) FROM jobs") == 1
    workers = await fetch(db, "SELECT processed_count FROM workers")
    assert sum(w[0] for w in workers) == 60
    assert len(ids) == 60


async def test_duplicate_dispatch_entries_do_not_cause_double_execution(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(db, dispatcher, settings, project, payload={"work_ms": 300})
    queue_id = await scalar(db, "SELECT queue_id FROM jobs WHERE id = :i", i=job)
    async with running_worker(settings, db, dispatcher, concurrency=2) as worker:
        await wait_for(lambda: _state_is(db, job, "running"))
        # Simulate the reconciler racing with a pop: the ID is pushed again while running.
        await dispatcher.enqueue([(job, queue_id, 5, datetime.now(UTC))])
        await wait_for(lambda: _state_is(db, job, "succeeded"))
        await wait_for(lambda: _zcard_zero(dispatcher, queue_id))
    assert worker.stats.stale_pops >= 1
    assert await scalar(db, "SELECT count(*) FROM job_attempts WHERE job_id = :i", i=job) == 1


async def _state_is(db: Database, job: object, state: str) -> bool:
    return await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == state


async def _zcard_zero(dispatcher: Dispatcher, queue_id: object) -> bool:
    return await dispatcher.redis.zcard(f"qf:q:{queue_id}") == 0


async def test_stale_worker_cannot_overwrite_newer_attempt(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db,
        dispatcher,
        settings,
        project,
        max_attempts=3,
        backoff_base_seconds=1,
        backoff_max_seconds=1,
    )
    first = await claim_job(db, job, "worker-a", 30)
    assert first.claim is not None

    # Worker A stalls past its lease; the reconciler recovers the job and worker B takes it.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :i"),
            {"i": job},
        )
    assert await maintenance.recover_expired_leases(db, dispatcher) == 1
    assert await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == "retrying"
    async with db.transaction() as session:
        await session.execute(text("UPDATE jobs SET run_at = now() WHERE id = :i"), {"i": job})
    assert await maintenance.promote_due(db, dispatcher) == 1
    second = await claim_job(db, job, "worker-b", 30)
    assert second.claim is not None and second.claim.attempt == 2

    # A wakes up and tries to report success for attempt 1: rejected by the fence.
    assert (
        await complete_job(
            db, first.claim, "worker-a", Outcome(AttemptOutcome.SUCCEEDED, result={"from": "a"})
        )
        is None
    )
    assert (
        await complete_job(
            db, second.claim, "worker-b", Outcome(AttemptOutcome.SUCCEEDED, result={"from": "b"})
        )
        == "succeeded"
    )
    assert await scalar(db, "SELECT result->>'from' FROM jobs WHERE id = :i", i=job) == "b"
    outcomes = await fetch(
        db,
        "SELECT attempt_number, outcome FROM job_attempts WHERE job_id = :i ORDER BY attempt_number",
        i=job,
    )
    assert [tuple(o) for o in outcomes] == [(1, "lost"), (2, "succeeded")]


async def test_parallel_promotion_moves_each_job_once(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    ids = [await submit(db, dispatcher, settings, project, delay_seconds=3600) for _ in range(40)]
    async with db.transaction() as session:
        await session.execute(text("UPDATE jobs SET run_at = now() - interval '1 second'"))
    counts = await asyncio.gather(
        *[maintenance.promote_due(db, dispatcher, limit=15) for _ in range(6)]
    )
    while sum(counts) < 40:
        counts.append(await maintenance.promote_due(db, dispatcher))
    assert sum(counts) == 40
    events = await scalar(
        db, "SELECT count(*) FROM job_events WHERE to_state = 'queued' AND from_state = 'scheduled'"
    )
    assert events == 40 and len(ids) == 40


async def test_parallel_schedulers_materialize_one_job_per_occurrence(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    created = await schedules.create_schedule(
        db,
        settings,
        project.id,
        ScheduleCreate(
            name="every-minute",
            job_type="bench.synthetic",
            cron_expression="* * * * *",
            payload={"work_ms": 1},
            skip_if_running=False,
        ),
        ACTOR,
    )
    past = datetime.now(UTC) - timedelta(seconds=5)
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE schedules SET next_run_at = :t WHERE id = :i"),
            {"t": past, "i": created.id},
        )
    results = await asyncio.gather(
        *[maintenance.materialize_schedules(db, dispatcher) for _ in range(5)]
    )
    assert sum(results) == 1
    assert await scalar(db, "SELECT count(*) FROM jobs WHERE schedule_id = :i", i=created.id) == 1
    next_run = await scalar(db, "SELECT next_run_at FROM schedules WHERE id = :i", i=created.id)
    assert next_run > datetime.now(UTC)
