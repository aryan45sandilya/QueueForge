"""Worker execution against real PostgreSQL and Redis: outcomes, retries, timeouts,
cancellation, graceful shutdown, priorities, pausing and queue concurrency limits."""

from __future__ import annotations

import itertools
import uuid
from datetime import datetime

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, queue_key
from queueforge.services.jobs import cancel_job
from tests.conftest import job_state, make_settings, wait_for
from tests.helpers import (
    ACTOR,
    fetch,
    make_project,
    running_scheduler,
    running_worker,
    scalar,
    submit,
)


async def states_are(db: Database, ids: list[uuid.UUID], state: str) -> bool:
    rows = await fetch(db, "SELECT state FROM jobs WHERE id = ANY(:ids)", ids=ids)
    return len(rows) == len(ids) and all(r[0] == state for r in rows)


async def test_worker_runs_registered_handlers_and_records_outcome(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    ids = [
        await submit(db, dispatcher, settings, project, payload={"work_ms": 20, "label": "a"}),
        await submit(
            db,
            dispatcher,
            settings,
            project,
            type="data.transform",
            payload={
                "csv": "a,b\n1,2\n3,4\n",
                "steps": [{"op": "cast", "column": "a", "to": "int"}],
            },
        ),
        await submit(
            db,
            dispatcher,
            settings,
            project,
            type="notification.send",
            payload={
                "channel": "email",
                "template": "t",
                "subject": "Hi",
                "body": "Hello {{name}}",
                "recipients": [{"address": "a@example.com", "name": "A"}],
            },
        ),
        await submit(
            db, dispatcher, settings, project, type="report.generate", payload={"period_hours": 1}
        ),
    ]
    async with running_worker(settings, db, dispatcher, concurrency=4) as worker:
        await wait_for(lambda: states_are(db, ids, "succeeded"))
    assert worker.stats.succeeded == 4

    transform = await fetch(db, "SELECT result FROM jobs WHERE id = :i", i=ids[1])
    assert transform[0][0]["output_rows"] == 2
    report = await fetch(db, "SELECT result FROM jobs WHERE id = :i", i=ids[3])
    assert report[0][0]["totals"]["submitted"] >= 3

    attempts = await fetch(
        db,
        "SELECT outcome, wait_ms, duration_ms, worker_id FROM job_attempts WHERE job_id = :i",
        i=ids[0],
    )
    assert len(attempts) == 1
    outcome, wait_ms, duration_ms, worker_id = attempts[0]
    assert outcome == "succeeded" and wait_ms >= 0 and duration_ms >= 15 and worker_id == worker.id
    events = await fetch(
        db, "SELECT from_state, to_state FROM job_events WHERE job_id = :i ORDER BY id", i=ids[0]
    )
    assert [tuple(e) for e in events] == [
        (None, "queued"),
        ("queued", "running"),
        ("running", "succeeded"),
    ]
    row = await fetch(db, "SELECT processed_count, state FROM workers WHERE id = :w", w=worker.id)
    assert row[0][0] == 4 and row[0][1] == "stopped"


async def test_retry_with_backoff_then_success(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db,
        dispatcher,
        settings,
        project,
        payload={"work_ms": 1, "fail_attempts": 2},
        max_attempts=4,
        backoff_base_seconds=1,
        backoff_max_seconds=1,
    )
    async with (
        running_scheduler(settings, db, dispatcher),
        running_worker(settings, db, dispatcher, concurrency=2),
    ):
        await wait_for(lambda: states_are(db, [job], "succeeded"), timeout=20)
    attempts = await fetch(
        db,
        "SELECT attempt_number, outcome, error->>'type' FROM job_attempts WHERE job_id = :i ORDER BY attempt_number",
        i=job,
    )
    assert [(a[0], a[1]) for a in attempts] == [(1, "failed"), (2, "failed"), (3, "succeeded")]
    assert attempts[0][2] == "RetryableError"
    states = [
        r[0]
        for r in await fetch(
            db, "SELECT to_state FROM job_events WHERE job_id = :i ORDER BY id", i=job
        )
    ]
    assert states == [
        "queued",
        "running",
        "retrying",
        "queued",
        "running",
        "retrying",
        "queued",
        "running",
        "succeeded",
    ]
    # Backoff was honoured: each retry started at least ~0.5s (equal jitter of 1s) after the failure.
    times = await fetch(
        db,
        "SELECT started_at, finished_at FROM job_attempts WHERE job_id = :i ORDER BY attempt_number",
        i=job,
    )
    for prev, nxt in itertools.pairwise(times):
        assert (nxt[0] - prev[1]).total_seconds() >= 0.5
    assert await scalar(db, "SELECT last_error FROM jobs WHERE id = :i", i=job) is None


async def test_exhausted_retries_go_to_dead_letter_and_can_be_retried(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db,
        dispatcher,
        settings,
        project,
        payload={"work_ms": 1, "fail_attempts": 10},
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_max_seconds=1,
    )
    async with (
        running_scheduler(settings, db, dispatcher),
        running_worker(settings, db, dispatcher, concurrency=1),
    ):
        await wait_for(lambda: states_are(db, [job], "failed"), timeout=20)
    row = (
        await fetch(
            db,
            "SELECT attempts, error_fingerprint, last_error, finished_at FROM jobs WHERE id = :i",
            i=job,
        )
    )[0]
    assert row[0] == 2 and row[1] and row[2]["retryable"] is True and row[3] is not None
    assert "Synthetic transient failure" in row[2]["message"]
    assert "Traceback" in row[2]["traceback"]
    last_event = await scalar(
        db, "SELECT message FROM job_events WHERE job_id = :i ORDER BY id DESC LIMIT 1", i=job
    )
    assert "exhausted 2 attempt(s)" in last_event


async def test_timeout_is_enforced(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db,
        dispatcher,
        settings,
        project,
        payload={"work_ms": 10_000},
        timeout_seconds=1,
        max_attempts=1,
    )
    async with running_worker(settings, db, dispatcher, concurrency=1):
        await wait_for(lambda: states_are(db, [job], "failed"), timeout=10)
    attempt = (
        await fetch(
            db,
            "SELECT outcome, duration_ms, error->>'type' FROM job_attempts WHERE job_id = :i",
            i=job,
        )
    )[0]
    assert attempt[0] == "timed_out" and attempt[2] == "Timeout"
    assert 900 <= attempt[1] < 3000


async def test_permanent_error_is_not_retried(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db, dispatcher, settings, project, payload={"permanent_failure": True}, max_attempts=5
    )
    async with running_worker(settings, db, dispatcher, concurrency=1):
        await wait_for(lambda: states_are(db, [job], "failed"))
    row = (await fetch(db, "SELECT attempts, last_error FROM jobs WHERE id = :i", i=job))[0]
    assert row[0] == 1 and row[1]["retryable"] is False and row[1]["type"] == "PermanentError"


async def test_cancelling_a_running_job_stops_the_handler(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(db, dispatcher, settings, project, payload={"work_ms": 30_000})
    async with running_worker(settings, db, dispatcher, concurrency=1):
        await wait_for(lambda: states_are(db, [job], "running"))
        assert await cancel_job(db, dispatcher, project.id, job, ACTOR) == "running"
        started = datetime.now()
        await wait_for(lambda: states_are(db, [job], "cancelled"), timeout=10)
        assert (datetime.now() - started).total_seconds() < settings.heartbeat_seconds * 3
    attempt = await scalar(db, "SELECT outcome FROM job_attempts WHERE job_id = :i", i=job)
    assert attempt == "cancelled"


async def test_graceful_shutdown_returns_job_to_queue(db: Database, dispatcher: Dispatcher) -> None:
    settings = make_settings(shutdown_grace_seconds=0.3)
    project = await make_project(db)
    job = await submit(
        db, dispatcher, settings, project, payload={"work_ms": 30_000}, max_attempts=3
    )
    async with running_worker(settings, db, dispatcher, concurrency=1):
        await wait_for(lambda: states_are(db, [job], "running"))
    # Leaving the context stops the worker; the job must be handed back without using an attempt.
    row = (
        await fetch(
            db,
            "SELECT state, attempts, max_attempts, worker_id, queue_id FROM jobs WHERE id = :i",
            i=job,
        )
    )[0]
    assert row[0] == "queued" and row[1] == 1 and row[2] == 4 and row[3] is None
    assert (
        await scalar(db, "SELECT outcome FROM job_attempts WHERE job_id = :i", i=job)
        == "interrupted"
    )
    assert await dispatcher.redis.zscore(queue_key(row[4]), str(job)) is not None


async def test_higher_priority_runs_first(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    low = [
        await submit(db, dispatcher, settings, project, priority=1, payload={"work_ms": 5})
        for _ in range(3)
    ]
    high = await submit(db, dispatcher, settings, project, priority=9, payload={"work_ms": 5})
    async with running_worker(settings, db, dispatcher, concurrency=1):
        await wait_for(lambda: states_are(db, [*low, high], "succeeded"))
    order = [
        r[0] for r in await fetch(db, "SELECT job_id FROM job_attempts ORDER BY started_at, id")
    ]
    assert order[0] == high
    assert order[1:] == low  # FIFO within a priority


async def test_paused_queue_is_not_dispatched(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db, queues=[{"name": "slow"}])
    job = await submit(db, dispatcher, settings, project, queue="slow")
    async with db.transaction() as session:
        from sqlalchemy import text

        await session.execute(text("UPDATE queues SET paused = true WHERE name = 'slow'"))
    async with running_worker(settings, db, dispatcher, concurrency=2):
        import asyncio

        await asyncio.sleep(1.5)
        assert await job_state(db, job) == "queued"
        async with db.transaction() as session:
            await session.execute(text("UPDATE queues SET paused = false WHERE name = 'slow'"))
        await wait_for(lambda: states_are(db, [job], "succeeded"))


async def test_queue_concurrency_limit_holds_across_workers(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db, queues=[{"name": "limited", "concurrency_limit": 2}])
    ids = [
        await submit(db, dispatcher, settings, project, queue="limited", payload={"work_ms": 250})
        for _ in range(10)
    ]
    async with (
        running_worker(settings, db, dispatcher, concurrency=4),
        running_worker(settings, db, dispatcher, concurrency=4),
    ):
        await wait_for(lambda: states_are(db, ids, "succeeded"), timeout=30)
    intervals = await fetch(
        db, "SELECT started_at, finished_at FROM job_attempts WHERE job_id = ANY(:ids)", ids=ids
    )
    points = sorted(
        [(s, 1) for s, _ in intervals] + [(f, -1) for _, f in intervals], key=lambda p: (p[0], p[1])
    )
    current = peak = 0
    for _, delta in points:
        current += delta
        peak = max(peak, current)
    assert peak == 2
