"""Test fixtures. Tests run against real PostgreSQL and Redis.

Configure with QF_TEST_DATABASE_URL and QF_TEST_REDIS_URL (defaults match the CI services).
The test database is wiped: never point these at a database you care about.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from queueforge.api.app import create_app
from queueforge.config import Settings
from queueforge.db import Database, make_engine
from queueforge.dispatch import Dispatcher, make_redis

BACKEND = Path(__file__).resolve().parent.parent
TEST_DB = os.environ.get(
    "QF_TEST_DATABASE_URL",
    "postgresql+asyncpg://queueforge:queueforge@127.0.0.1:5432/queueforge_test",
)
TEST_REDIS = os.environ.get("QF_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
SECRET = "test-secret-key-0123456789abcdef-0123456789"
PASSWORD = "correct horse battery"

TABLES = [
    "audit_events",
    "job_events",
    "job_attempts",
    "jobs",
    "schedules",
    "queues",
    "api_keys",
    "project_members",
    "projects",
    "sessions",
    "users",
    "workers",
]


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database_url": TEST_DB,
        "redis_url": TEST_REDIS,
        "secret_key": SECRET,
        "cors_origins": ["http://localhost:3000"],
        "lease_seconds": 4,
        "heartbeat_seconds": 1.0,
        "shutdown_grace_seconds": 2.0,
        "queue_refresh_seconds": 0.5,
        "rate_limit_per_minute": 10_000,
        "login_rate_limit_per_minute": 1_000,
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def _alembic(*args: str) -> None:
    env = {**os.environ, "QF_DATABASE_URL": TEST_DB, "QF_ENV": "test", "QF_SECRET_KEY": SECRET}
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Fresh schema via migrations, including a downgrade round trip."""
    import asyncpg

    async def reset() -> None:
        dsn = TEST_DB.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            await conn.close()

    asyncio.run(reset())
    _alembic("upgrade", "head")
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(make_engine(settings, pool_size=20))
    yield database
    await database.dispose()


@pytest.fixture(scope="session")
async def dispatcher(settings: Settings) -> AsyncIterator[Dispatcher]:
    d = Dispatcher(make_redis(settings.redis_url))
    yield d
    await d.redis.aclose()


@pytest.fixture(autouse=True)
async def clean_state(db: Database, dispatcher: Dispatcher) -> None:
    # DELETE is much faster than TRUNCATE for the handful of rows a test creates.
    async with db.transaction() as session:
        for table in TABLES:
            await session.execute(text(f"DELETE FROM {table}"))  # noqa: S608 - fixed table names
    await dispatcher.redis.flushdb()


@pytest.fixture
def app(settings: Settings, db: Database, dispatcher: Dispatcher) -> Any:
    return create_app(settings, db=db, dispatcher=dispatcher)


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class Api:
    """Thin helper around an httpx client that remembers the session's CSRF token."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.csrf: str | None = None
        self.bearer: str | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        elif self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.get(url, headers=self._headers(kw.pop("headers", None)), **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.post(
            url, json=json, headers=self._headers(kw.pop("headers", None)), **kw
        )

    async def patch(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.client.patch(
            url, json=json, headers=self._headers(kw.pop("headers", None)), **kw
        )

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self._headers(kw.pop("headers", None)), **kw)


async def signup(client: httpx.AsyncClient, email: str) -> Api:
    r = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": email.split("@")[0]},
    )
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    api = Api(client)
    api.csrf = r.json()["csrf_token"]
    return api


@pytest.fixture
async def owner(client: httpx.AsyncClient) -> Api:
    return await signup(client, "owner@example.com")


@pytest.fixture
async def project(owner: Api) -> dict[str, Any]:
    r = await owner.post("/api/v1/projects", json={"name": "Acme", "slug": "acme"})
    assert r.status_code == 201, r.text
    return r.json()


async def make_key(
    owner: Api, project_id: str, scopes: list[str] | None = None, name: str = "ci"
) -> str:
    body: dict[str, Any] = {"name": name}
    if scopes is not None:
        body["scopes"] = scopes
    r = await owner.post(f"/api/v1/projects/{project_id}/api-keys", json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["secret"])


@pytest.fixture
async def keyed(app: Any, owner: Api, project: dict[str, Any]) -> AsyncIterator[Api]:
    """A separate client authenticated only by an API key with every scope."""
    secret = await make_key(owner, project["id"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        api = Api(c)
        api.bearer = secret
        yield api


async def wait_for(
    predicate: Callable[[], Awaitable[bool]], timeout: float = 15.0, interval: float = 0.05
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def job_state(db: Database, job_id: uuid.UUID | str) -> str | None:
    async with db.session() as session:
        return (
            await session.execute(text("SELECT state FROM jobs WHERE id = :i"), {"i": str(job_id)})
        ).scalar_one_or_none()


async def avoid_rate_window_edge() -> None:
    """Fixed-window limits reset on the minute; keep rate-limit assertions inside one window."""
    seconds = time.time() % 60
    if seconds > 50:
        await asyncio.sleep(60.5 - seconds)
