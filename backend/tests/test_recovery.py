"""Recovery: worker crashes (real process kill), Redis loss and divergence, Redis outages."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import text

from queueforge.api.app import create_app
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, make_redis, queue_key
from queueforge.services import maintenance
from tests.conftest import SECRET, TEST_DB, TEST_REDIS, signup, wait_for
from tests.helpers import fetch, make_project, running_worker, scalar, submit

BACKEND = Path(__file__).resolve().parent.parent


def spawn_worker() -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "QF_ENV": "test",
        "QF_DATABASE_URL": TEST_DB,
        "QF_REDIS_URL": TEST_REDIS,
        "QF_SECRET_KEY": SECRET,
        "QF_LEASE_SECONDS": "4",
        "QF_HEARTBEAT_SECONDS": "1",
        "QF_QUEUE_REFRESH_SECONDS": "0.5",
        "QF_LOG_LEVEL": "WARNING",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "queueforge.worker", "--concurrency", "2"],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def test_killed_worker_process_is_recovered(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(
        db,
        dispatcher,
        settings,
        project,
        payload={"work_ms": 4000},
        max_attempts=3,
        backoff_base_seconds=1,
        backoff_max_seconds=1,
    )
    proc = spawn_worker()
    try:

        async def running() -> bool:
            return await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == "running"

        await wait_for(running, timeout=30)
        crashed_worker = await scalar(db, "SELECT worker_id FROM jobs WHERE id = :i", i=job)
    finally:
        proc.kill()  # SIGKILL / TerminateProcess: no cleanup code runs
        proc.wait(timeout=10)

    async def recovered() -> bool:
        await maintenance.recover_expired_leases(db, dispatcher)
        return await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == "retrying"

    await wait_for(recovered, timeout=15, interval=0.25)
    last_error = await scalar(db, "SELECT last_error FROM jobs WHERE id = :i", i=job)
    assert last_error["type"] == "WorkerLost" and crashed_worker in last_error["message"]

    async def worker_marked_lost() -> bool:
        await maintenance.mark_lost_workers(db, settings.heartbeat_seconds)
        return (
            await scalar(db, "SELECT state FROM workers WHERE id = :w", w=crashed_worker) == "lost"
        )

    await wait_for(worker_marked_lost, timeout=10, interval=0.25)

    # A healthy worker (plus promotion) finishes the job.
    async with running_worker(settings, db, dispatcher, concurrency=1):

        async def succeeded() -> bool:
            await maintenance.promote_due(db, dispatcher)
            return await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == "succeeded"

        await wait_for(succeeded, timeout=20, interval=0.25)
    outcomes = await fetch(
        db, "SELECT outcome FROM job_attempts WHERE job_id = :i ORDER BY attempt_number", i=job
    )
    assert [o[0] for o in outcomes] == ["lost", "succeeded"]


async def test_lease_recovery_dead_letters_when_budget_is_spent(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(db, dispatcher, settings, project, max_attempts=1)
    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE jobs SET state = 'running', attempts = 1, worker_id = 'gone', lease_expires_at = now() - interval '5 seconds' "
                "WHERE id = :i"
            ),
            {"i": job},
        )
        await session.execute(
            text(
                "INSERT INTO job_attempts (job_id, project_id, attempt_number, worker_id, outcome, started_at) "
                "VALUES (:i, :p, 1, 'gone', 'running', now() - interval '10 seconds')"
            ),
            {"i": job, "p": project.id},
        )
    assert await maintenance.recover_expired_leases(db, dispatcher) == 1
    assert await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job) == "failed"
    assert await scalar(db, "SELECT outcome FROM job_attempts WHERE job_id = :i", i=job) == "lost"


async def test_redis_data_loss_is_repaired_by_reconciler(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    ids = [
        await submit(db, dispatcher, settings, project, payload={"work_ms": 5}) for _ in range(5)
    ]
    await dispatcher.redis.flushdb()  # Redis restarted without persistence

    report = await maintenance.reconcile_dispatch(db, dispatcher, grace_seconds=0)
    assert report.pushed_missing == 5 and report.redis_available
    again = await maintenance.reconcile_dispatch(db, dispatcher, grace_seconds=0)
    assert again.pushed_missing == 0  # idempotent

    async with running_worker(settings, db, dispatcher, concurrency=3):

        async def done() -> bool:
            return await scalar(db, "SELECT count(*) FROM jobs WHERE state = 'succeeded'") == len(
                ids
            )

        await wait_for(done)


async def test_reconciler_respects_grace_period(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    await submit(db, dispatcher, settings, project)
    await dispatcher.redis.flushdb()
    report = await maintenance.reconcile_dispatch(db, dispatcher, grace_seconds=60)
    assert report.pushed_missing == 0  # too young: the API may still be about to push it


async def test_reconciler_removes_stale_and_orphan_entries(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    job = await submit(db, dispatcher, settings, project)
    queue_id = await scalar(db, "SELECT queue_id FROM jobs WHERE id = :i", i=job)
    async with db.transaction() as session:  # job cancelled without its Redis entry being removed
        await session.execute(
            text("UPDATE jobs SET state = 'cancelled', finished_at = now() WHERE id = :i"),
            {"i": job},
        )
    orphan = uuid.uuid4()
    await dispatcher.enqueue([(uuid.uuid4(), orphan, 5, datetime.now(UTC))])
    report = await maintenance.reconcile_dispatch(db, dispatcher, grace_seconds=0)
    assert report.removed_stale == 1 and report.removed_orphan_keys == 1
    assert await dispatcher.redis.zcard(queue_key(queue_id)) == 0
    assert await dispatcher.redis.exists(queue_key(orphan)) == 0


async def test_api_accepts_jobs_while_redis_is_down(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    dead = Dispatcher(make_redis("redis://127.0.0.1:1/0"))
    app = create_app(settings, db=db, dispatcher=dead)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            api = await signup(c, "outage@example.com")  # rate limiting fails open
            project = (
                await api.post("/api/v1/projects", json={"name": "Outage", "slug": "outage"})
            ).json()
            r = await api.post(
                f"/api/v1/projects/{project['id']}/jobs",
                json={"type": "bench.synthetic", "payload": {}},
            )
            assert r.status_code == 201
            job_id = r.json()["job"]["id"]
            ready = await c.get("/readyz")
            assert ready.status_code == 200 and ready.json()["status"] == "degraded"
    finally:
        await dead.redis.aclose()
    # Once Redis is back, the reconciler dispatches the job that was accepted during the outage.
    report = await maintenance.reconcile_dispatch(db, dispatcher, grace_seconds=0)
    assert report.pushed_missing == 1
    queue_id = await scalar(db, "SELECT queue_id FROM jobs WHERE id = :i", i=job_id)
    assert await dispatcher.redis.zrange(queue_key(queue_id), 0, -1) == [job_id]


async def test_worker_falls_back_to_postgres_when_redis_is_down(
    settings: Settings, db: Database
) -> None:
    project = await make_project(db)
    dead = Dispatcher(make_redis("redis://127.0.0.1:1/0"))
    try:
        ids = [
            await submit(db, dead, settings, project, payload={"work_ms": 5}, priority=p)
            for p in (1, 9, 5)
        ]
        async with running_worker(settings, db, dead, concurrency=1) as worker:

            async def done() -> bool:
                return await scalar(db, "SELECT count(*) FROM jobs WHERE state = 'succeeded'") == 3

            await wait_for(done, timeout=20)
        assert worker.mode == "postgres"
        order = [
            r[0] for r in await fetch(db, "SELECT job_id FROM job_attempts ORDER BY started_at")
        ]
        assert order[0] == ids[1]  # priority still respected in fallback mode
    finally:
        await dead.redis.aclose()


async def test_readiness_fails_without_postgres(settings: Settings, dispatcher: Dispatcher) -> None:
    from queueforge.db import make_engine

    broken = Database(
        make_engine(
            settings.model_copy(
                update={
                    "database_url": "postgresql+asyncpg://queueforge:queueforge@127.0.0.1:1/none"
                }
            )
        )
    )
    app = create_app(settings, db=broken, dispatcher=dispatcher)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            ready = await c.get("/readyz")
            assert ready.status_code == 503 and ready.json()["checks"]["postgres"].startswith(
                "error"
            )
            login = await c.post(
                "/api/v1/auth/login", json={"email": "a@example.com", "password": "whatever-123"}
            )
            assert (
                login.status_code == 503 and login.json()["error"]["code"] == "service_unavailable"
            )
    finally:
        await broken.dispose()
