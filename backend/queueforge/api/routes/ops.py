from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from redis.exceptions import RedisError
from sqlalchemy import or_, select, text

from queueforge import __version__
from queueforge.api.deps import (
    ProjectAccess,
    get_db,
    get_dispatcher,
    get_settings_dep,
    project_access,
)
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, DispatchUnavailable
from queueforge.lifecycle import JobState
from queueforge.models import Job, Worker
from queueforge.schemas import ErrorResponse, HealthOut, MetricsOverview, WorkerJobOut, WorkerOut
from queueforge.services import metrics

router = APIRouter()
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
}


def worker_health(state: str, age: float, heartbeat: float) -> str:
    if state in ("stopped", "lost"):
        return state
    if age <= heartbeat * 2:
        return "healthy" if state == "running" else "draining"
    return "stale"


@router.get(
    "/projects/{project_id}/workers",
    response_model=list[WorkerOut],
    responses=ERRORS,
    tags=["workers"],
)
async def list_workers(
    access: ProjectAccess = Depends(project_access("metrics:read")),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> list[WorkerOut]:
    """Worker processes (shared infrastructure) seen in the last 24 hours.

    Only jobs belonging to this project are listed under each worker.
    """
    async with db.session() as session:
        now: datetime = (await session.execute(text("SELECT now()"))).scalar_one()
        workers = (
            (
                await session.execute(
                    select(Worker)
                    .where(
                        or_(
                            Worker.state.in_(["running", "draining"]),
                            Worker.last_heartbeat_at > text("now() - interval '24 hours'"),
                        )
                    )
                    .order_by(Worker.state, Worker.started_at.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        running = (
            await session.execute(
                select(Job.id, Job.type, Job.started_at, Job.worker_id).where(
                    Job.project_id == access.project.id, Job.state == JobState.RUNNING
                )
            )
        ).all()
    out: list[WorkerOut] = []
    for w in workers:
        age = (now - w.last_heartbeat_at).total_seconds()
        item = WorkerOut(
            id=w.id,
            hostname=w.hostname,
            pid=w.pid,
            version=w.version,
            queues=w.queues,
            concurrency=w.concurrency,
            state=w.state,
            health=worker_health(w.state, age, settings.heartbeat_seconds),
            dispatch_mode=w.dispatch_mode,
            started_at=w.started_at,
            last_heartbeat_at=w.last_heartbeat_at,
            heartbeat_age_seconds=round(age, 1),
            stopped_at=w.stopped_at,
            running_jobs=w.running_jobs,
            processed_count=w.processed_count,
            failed_count=w.failed_count,
            rss_bytes=w.rss_bytes,
            project_jobs=[
                WorkerJobOut(id=j, type=t, started_at=s) for j, t, s, wid in running if wid == w.id
            ],
        )
        out.append(item)
    return out


@router.get(
    "/projects/{project_id}/metrics",
    response_model=MetricsOverview,
    responses=ERRORS,
    tags=["metrics"],
)
async def get_metrics(
    access: ProjectAccess = Depends(project_access("metrics:read")),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
    window: Literal["1h", "24h", "7d"] = Query("24h"),
) -> MetricsOverview:
    """Operational metrics for a window. See `definitions` in the response for exact semantics."""
    return await metrics.overview(db, settings, access.project.id, window)


@router.get(
    "/projects/{project_id}/events",
    tags=["events"],
    responses=ERRORS,
    response_class=StreamingResponse,
)
async def job_events(
    request: Request,
    access: ProjectAccess = Depends(project_access("jobs:read")),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> StreamingResponse:
    """Server-Sent Events stream of `job` events for this project.

    Each event's data is `{"type": "job.updated", "job": {...}}`. A `: ping` comment is sent every
    15 s. If Redis is unavailable, an `unavailable` event is sent and the stream closes; clients
    should fall back to polling and reconnect later.
    """
    project_id = access.project.id

    async def stream() -> AsyncIterator[str]:
        yield "retry: 5000\n\n"
        try:
            async with dispatcher.subscribe(project_id) as pubsub:
                yield f"event: ready\ndata: {json.dumps({'project_id': str(project_id)})}\n\n"
                last_ping = time.monotonic()
                while not await request.is_disconnected():
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message and message.get("type") == "message":
                        yield f"event: job\ndata: {message['data']}\n\n"
                    if time.monotonic() - last_ping >= 15:
                        last_ping = time.monotonic()
                        yield ": ping\n\n"
        except (DispatchUnavailable, RedisError, OSError):
            yield f"event: unavailable\ndata: {json.dumps({'reason': 'live updates unavailable'})}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/healthz", response_model=HealthOut, tags=["health"])
async def healthz() -> HealthOut:
    """Liveness: the process is serving requests."""
    return HealthOut(status="ok", checks={}, version=__version__)


@router.get(
    "/readyz",
    response_model=HealthOut,
    tags=["health"],
    responses={503: {"model": HealthOut, "description": "PostgreSQL unavailable"}},
)
async def readyz(
    db: Database = Depends(get_db), dispatcher: Dispatcher = Depends(get_dispatcher)
) -> JSONResponse:
    """Readiness. PostgreSQL is required (503 without it); Redis down reports `degraded` (200)."""
    checks: dict[str, str] = {}
    try:
        async with db.session() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3)
        checks["postgres"] = "ok"
    except Exception as exc:  # any failure means not ready
        checks["postgres"] = f"error: {type(exc).__name__}"
    checks["redis"] = "ok" if await dispatcher.ping() else "unavailable"
    if checks["postgres"] != "ok":
        status, code = "unavailable", 503
    elif checks["redis"] != "ok":
        status, code = "degraded", 200
    else:
        status, code = "ok", 200
    return JSONResponse(
        status_code=code,
        content=HealthOut(status=status, checks=checks, version=__version__).model_dump(),
    )
