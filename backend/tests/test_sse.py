"""Server-Sent Events over a real HTTP server (ASGI test transports buffer streaming bodies)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import uvicorn

from queueforge.api.app import create_app
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, make_redis
from tests.conftest import make_key, signup


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextlib.asynccontextmanager
async def serve(app: Any) -> AsyncIterator[str]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def read_events(
    lines: AsyncIterator[str], until: str, timeout: float = 10
) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    name = "message"

    async def consume() -> None:
        nonlocal name
        async for line in lines:
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                events.append((name, json.loads(line[5:].strip())))
                if name == until:
                    return
                name = "message"

    await asyncio.wait_for(consume(), timeout=timeout)
    return events


async def test_job_updates_stream_to_subscribers(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    app = create_app(settings, db=db, dispatcher=dispatcher)
    async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as c:
        api = await signup(c, "sse@example.com")
        project = (await api.post("/api/v1/projects", json={"name": "Live", "slug": "live"})).json()
        secret = await make_key(api, project["id"])
        headers = {"Authorization": f"Bearer {secret}"}
        async with c.stream(
            "GET", f"/api/v1/projects/{project['id']}/events", headers=headers
        ) as stream:
            assert stream.status_code == 200
            assert stream.headers["content-type"].startswith("text/event-stream")
            lines = stream.aiter_lines()
            ready = await read_events(lines, until="ready")
            assert ready[-1][0] == "ready"
            r = await c.post(
                f"/api/v1/projects/{project['id']}/jobs",
                headers=headers,
                json={"type": "bench.synthetic", "payload": {}},
            )
            job_id = r.json()["job"]["id"]
            events = await read_events(lines, until="job")
            name, data = events[-1]
            assert name == "job" and data["type"] == "job.updated"
            assert data["job"]["id"] == job_id and data["job"]["state"] == "queued"


async def test_stream_reports_unavailable_when_redis_is_down(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    dead = Dispatcher(make_redis("redis://127.0.0.1:1/0"))
    app = create_app(settings, db=db, dispatcher=dead)
    try:
        async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as c:
            api = await signup(c, "sse2@example.com")
            project = (
                await api.post("/api/v1/projects", json={"name": "Dark", "slug": "dark"})
            ).json()
            async with c.stream("GET", f"/api/v1/projects/{project['id']}/events") as stream:
                events = await read_events(stream.aiter_lines(), until="unavailable")
                assert events[-1][0] == "unavailable"
    finally:
        await dead.redis.aclose()


async def test_stream_requires_project_access(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    app = create_app(settings, db=db, dispatcher=dispatcher)
    async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=10) as c:
        api = await signup(c, "a@example.com")
        project = (await api.post("/api/v1/projects", json={"name": "A", "slug": "a-proj"})).json()
        c.cookies.clear()
        other = await signup(c, "b@example.com")
        assert other.csrf
        r = await c.get(f"/api/v1/projects/{project['id']}/events")
        assert r.status_code == 404
