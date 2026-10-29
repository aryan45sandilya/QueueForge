"""Helpers for tests that drive workers and the scheduler in-process."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select, text

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.models import Project, User
from queueforge.scheduler import Scheduler
from queueforge.schemas import JobCreate, ProjectCreate, QueueCreate
from queueforge.services import accounts
from queueforge.services.common import Actor
from queueforge.services.jobs import submit_job
from queueforge.worker import WorkerProcess

ACTOR = Actor(label="test")


async def make_project(
    db: Database, slug: str = "acme", queues: list[dict[str, Any]] | None = None
) -> Project:
    user = await accounts.register_user(
        db, f"{slug}-{uuid.uuid4().hex[:6]}@example.com", "password-123", slug
    )
    out = await accounts.create_project(db, user, ProjectCreate(name=slug.title(), slug=slug))
    for q in queues or []:
        await accounts.create_queue(db, out.id, QueueCreate(**q), ACTOR)
    async with db.session() as session:
        return (await session.execute(select(Project).where(Project.id == out.id))).scalar_one()


async def submit(
    db: Database, dispatcher: Dispatcher | None, settings: Settings, project: Project, **body: Any
) -> uuid.UUID:
    body.setdefault("type", "bench.synthetic")
    body.setdefault("payload", {"work_ms": 10})
    job_id, _ = await submit_job(db, dispatcher, settings, project, JobCreate(**body), ACTOR)
    return job_id


@contextlib.asynccontextmanager
async def running_worker(
    settings: Settings, db: Database, dispatcher: Dispatcher, **kwargs: Any
) -> AsyncIterator[WorkerProcess]:
    worker = WorkerProcess(settings, db, dispatcher, **kwargs)
    task = asyncio.create_task(worker.run())
    try:
        yield worker
    finally:
        worker.request_stop()
        await asyncio.wait_for(task, timeout=30)


@contextlib.asynccontextmanager
async def running_scheduler(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> AsyncIterator[Scheduler]:
    scheduler = Scheduler(settings, db, dispatcher)
    task = asyncio.create_task(scheduler.run())
    try:
        yield scheduler
    finally:
        scheduler.stop_event.set()
        await asyncio.wait_for(task, timeout=10)


async def fetch(db: Database, sql: str, **params: Any) -> list[Any]:
    async with db.session() as session:
        return list((await session.execute(text(sql), params)).all())


async def scalar(db: Database, sql: str, **params: Any) -> Any:
    async with db.session() as session:
        return (await session.execute(text(sql), params)).scalar_one_or_none()


async def first_user(db: Database) -> User:
    async with db.session() as session:
        return (await session.execute(select(User).limit(1))).scalar_one()
