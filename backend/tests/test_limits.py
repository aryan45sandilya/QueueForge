"""Per-account limits for shared instances."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from queueforge.api.app import create_app
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from tests.conftest import Api, signup

LIMITS = {
    "max_projects_per_user": 2,
    "max_jobs_per_day_per_project": 3,
    "max_schedules_per_project": 1,
    "min_schedule_interval_seconds": 300,
    "max_job_timeout_seconds": 60,
    "default_max_queued_jobs": 50,
    "max_queued_jobs_limit": 100,
    "default_queue_concurrency": 2,
    "disabled_job_types": ["bench.synthetic"],
}


@pytest.fixture
async def limited(
    db: Database, dispatcher: Dispatcher
) -> AsyncIterator[tuple[Api, dict[str, Any]]]:
    from tests.conftest import make_settings

    app = create_app(make_settings(**LIMITS), db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        api = await signup(c, "limited@example.com")
        project = (await api.post("/api/v1/projects", json={"name": "Mine", "slug": "mine"})).json()
        yield api, project


def job(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "data.transform", "payload": {"csv": "a\n1\n"}}
    body.update(over)
    return body


async def test_project_limit_and_new_project_defaults(limited: tuple[Api, dict[str, Any]]) -> None:
    api, project = limited
    assert project["max_queued_jobs"] == 50
    assert project["default_timeout_seconds"] == 60
    assert (
        await api.post("/api/v1/projects", json={"name": "Two", "slug": "two"})
    ).status_code == 201
    third = await api.post("/api/v1/projects", json={"name": "Three", "slug": "three"})
    assert third.status_code == 403 and third.json()["error"]["code"] == "quota_exceeded"


async def test_daily_job_quota(limited: tuple[Api, dict[str, Any]]) -> None:
    api, project = limited
    url = f"/api/v1/projects/{project['id']}/jobs"
    for _ in range(3):
        assert (await api.post(url, json=job())).status_code == 201
    over = await api.post(url, json=job())
    assert over.status_code == 429 and over.json()["error"]["code"] == "quota_exceeded"
    assert over.headers["retry-after"] == "3600"


async def test_disabled_job_types_are_hidden_and_rejected(
    limited: tuple[Api, dict[str, Any]],
) -> None:
    api, project = limited
    types = [t["name"] for t in (await api.get("/api/v1/job-types")).json()]
    assert "bench.synthetic" not in types and "http.request" in types
    r = await api.post(
        f"/api/v1/projects/{project['id']}/jobs", json=job(type="bench.synthetic", payload={})
    )
    assert r.status_code == 422 and "disabled" in r.json()["error"]["message"]


async def test_timeout_and_backlog_caps(limited: tuple[Api, dict[str, Any]]) -> None:
    api, project = limited
    too_long = await api.post(
        f"/api/v1/projects/{project['id']}/jobs", json=job(timeout_seconds=61)
    )
    assert too_long.status_code == 422
    raise_cap = await api.patch(f"/api/v1/projects/{project['id']}", json={"max_queued_jobs": 101})
    assert raise_cap.status_code == 422
    assert (
        await api.patch(f"/api/v1/projects/{project['id']}", json={"max_queued_jobs": 100})
    ).status_code == 200


async def test_queue_concurrency_is_capped(limited: tuple[Api, dict[str, Any]]) -> None:
    api, project = limited
    base = f"/api/v1/projects/{project['id']}/queues"
    default = next(q for q in (await api.get(base)).json() if q["name"] == "default")
    assert default["concurrency_limit"] == 2
    created = (await api.post(base, json={"name": "fast", "concurrency_limit": 50})).json()
    assert created["concurrency_limit"] == 2
    unlimited = (await api.post(base, json={"name": "other"})).json()
    assert unlimited["concurrency_limit"] == 2
    cleared = await api.patch(f"{base}/{created['id']}", json={"clear_concurrency_limit": True})
    assert cleared.json()["concurrency_limit"] == 2
    lowered = await api.patch(f"{base}/{created['id']}", json={"concurrency_limit": 1})
    assert lowered.json()["concurrency_limit"] == 1


async def test_schedule_limits(limited: tuple[Api, dict[str, Any]]) -> None:
    api, project = limited
    url = f"/api/v1/projects/{project['id']}/schedules"
    base = {"job_type": "report.generate", "payload": {}}
    too_often = await api.post(url, json={**base, "name": "a", "interval_seconds": 60})
    assert too_often.status_code == 422 and "300 seconds" in too_often.json()["error"]["message"]
    every_minute = await api.post(url, json={**base, "name": "b", "cron_expression": "* * * * *"})
    assert every_minute.status_code == 422
    preview = await api.post("/api/v1/schedules/preview", json={"interval_seconds": 120})
    assert preview.status_code == 422
    assert (
        await api.post(url, json={**base, "name": "c", "cron_expression": "*/5 * * * *"})
    ).status_code == 201
    second = await api.post(url, json={**base, "name": "d", "interval_seconds": 3600})
    assert second.status_code == 403 and second.json()["error"]["code"] == "quota_exceeded"
