"""Job API: submission, validation, idempotency, queries, cancellation, retry, bulk actions."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import text

from queueforge.db import Database
from queueforge.dispatch import Dispatcher, queue_key
from tests.conftest import Api, job_state


def jobs_url(project: dict[str, Any]) -> str:
    return f"/api/v1/projects/{project['id']}/jobs"


async def submit(api: Api, project: dict[str, Any], **body: Any) -> httpx.Response:
    body.setdefault("type", "bench.synthetic")
    body.setdefault("payload", {"work_ms": 1})
    return await api.post(jobs_url(project), json=body)


async def test_submit_persists_and_enqueues(
    keyed: Api, project: dict[str, Any], dispatcher: Dispatcher, db: Database
) -> None:
    r = await submit(keyed, project, priority=7, max_attempts=3, timeout_seconds=30)
    assert r.status_code == 201, r.text
    job = r.json()["job"]
    assert r.json()["created"] is True
    assert r.headers["location"].endswith(job["id"])
    assert job["state"] == "queued" and job["priority"] == 7 and job["max_attempts"] == 3
    assert job["created_by"].startswith("api key ci")
    assert job["events"][0]["to_state"] == "queued"
    queues = (await keyed.get(f"/api/v1/projects/{project['id']}/queues")).json()
    default = next(q for q in queues if q["name"] == "default")
    assert default["queued"] == 1 and default["dispatch_depth"] == 1
    members = await dispatcher.redis.zrange(queue_key(default["id"]), 0, -1)
    assert members == [job["id"]]
    assert await job_state(db, job["id"]) == "queued"


async def test_delayed_job_is_scheduled_not_enqueued(
    keyed: Api, project: dict[str, Any], dispatcher: Dispatcher
) -> None:
    r = await submit(keyed, project, delay_seconds=600)
    assert r.json()["job"]["state"] == "scheduled"
    run_at = datetime.fromisoformat(r.json()["job"]["run_at"])
    assert run_at > datetime.now(UTC) + timedelta(seconds=590)
    assert await dispatcher.redis.keys("qf:q:*") == []
    future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    assert (await submit(keyed, project, run_at=future)).json()["job"]["state"] == "scheduled"
    both = await submit(keyed, project, run_at=future, delay_seconds=5)
    assert both.status_code == 422


async def test_submit_validation_errors(keyed: Api, project: dict[str, Any]) -> None:
    cases = [
        ({"type": "os.system", "payload": {}}, 422, "type"),
        ({"type": "bench.synthetic", "payload": {"work_ms": "slow"}}, 422, "work_ms"),
        ({"type": "bench.synthetic", "payload": {}, "queue": "missing"}, 422, "queue"),
        ({"type": "bench.synthetic", "payload": {}, "queue": "Bad Name"}, 422, "queue"),
        ({"type": "bench.synthetic", "payload": {}, "priority": 10}, 422, "priority"),
        ({"type": "bench.synthetic", "payload": {}, "max_attempts": 26}, 422, "max_attempts"),
        ({"type": "bench.synthetic", "payload": {}, "surprise": 1}, 422, "surprise"),
        ({"type": "bench.synthetic", "payload": {}, "run_at": "2030-01-01T00:00:00"}, 422, None),
        (
            {
                "type": "bench.synthetic",
                "payload": {},
                "backoff_base_seconds": 100,
                "backoff_max_seconds": 10,
            },
            422,
            None,
        ),
    ]
    for body, status, field in cases:
        r = await keyed.post(jobs_url(project), json=body)
        assert r.status_code == status, (body, r.text)
        err = r.json()["error"]
        assert err["code"] == "validation_error"
        if field:
            assert any(field in [str(x) for x in d["loc"]] for d in err["details"]), (body, err)


async def test_payload_and_body_size_limits(keyed: Api, project: dict[str, Any]) -> None:
    big = {"csv": "a\n" + ("x" * 70_000 + "\n"), "steps": []}
    r = await keyed.post(jobs_url(project), json={"type": "data.transform", "payload": big})
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
    huge = "x" * 1_100_000
    r = await keyed.post(
        jobs_url(project), json={"type": "data.transform", "payload": {"csv": huge}}
    )
    assert r.status_code == 413


async def test_idempotency_key_returns_original_or_conflicts(
    keyed: Api, project: dict[str, Any]
) -> None:
    first = await submit(keyed, project, idempotency_key="order-42", payload={"work_ms": 5})
    again = await submit(keyed, project, idempotency_key="order-42", payload={"work_ms": 5})
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["job"]["id"] == first.json()["job"]["id"]
    different = await submit(keyed, project, idempotency_key="order-42", payload={"work_ms": 6})
    assert different.status_code == 409
    assert different.json()["error"]["details"]["job_id"] == first.json()["job"]["id"]


async def test_concurrent_duplicate_submissions_create_one_job(
    keyed: Api, project: dict[str, Any], db: Database
) -> None:
    responses = await asyncio.gather(
        *[
            submit(keyed, project, idempotency_key="burst-1", payload={"work_ms": 3})
            for _ in range(25)
        ]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(201) == 1 and statuses.count(200) == 24
    assert len({r.json()["job"]["id"] for r in responses}) == 1
    async with db.session() as session:
        count = (
            await session.execute(
                text("SELECT count(*) FROM jobs WHERE idempotency_key = 'burst-1'")
            )
        ).scalar_one()
    assert count == 1


async def test_list_filter_sort_paginate(keyed: Api, project: dict[str, Any]) -> None:
    ids = []
    for i in range(5):
        r = await submit(keyed, project, priority=i, idempotency_key=f"list-{i}")
        ids.append(r.json()["job"]["id"])
    await submit(
        keyed,
        project,
        type="notification.send",
        payload={
            "channel": "email",
            "template": "t",
            "subject": "s",
            "body": "b",
            "recipients": [{"address": "a@example.com"}],
        },
    )
    await keyed.post(f"{jobs_url(project)}/{ids[0]}/cancel")

    page = (
        await keyed.get(
            jobs_url(project),
            params={
                "limit": 2,
                "offset": 0,
                "sort": "priority",
                "order": "desc",
                "type": "bench.synthetic",
            },
        )
    ).json()
    assert page["total"] == 5 and len(page["items"]) == 2
    assert [j["priority"] for j in page["items"]] == [4, 3]
    page2 = (
        await keyed.get(
            jobs_url(project),
            params={
                "limit": 2,
                "offset": 4,
                "sort": "priority",
                "order": "desc",
                "type": "bench.synthetic",
            },
        )
    ).json()
    assert [j["priority"] for j in page2["items"]] == [0]

    cancelled = (await keyed.get(jobs_url(project), params=[("state", "cancelled")])).json()
    assert [j["id"] for j in cancelled["items"]] == [ids[0]]
    multi = (
        await keyed.get(jobs_url(project), params=[("state", "cancelled"), ("state", "queued")])
    ).json()
    assert multi["total"] == 6

    by_key = (await keyed.get(jobs_url(project), params={"q": "list-3"})).json()
    assert [j["id"] for j in by_key["items"]] == [ids[3]]
    by_prefix = (await keyed.get(jobs_url(project), params={"q": ids[2][:8]})).json()
    assert ids[2] in [j["id"] for j in by_prefix["items"]]

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert (await keyed.get(jobs_url(project), params={"created_from": future})).json()[
        "total"
    ] == 0
    assert (await keyed.get(jobs_url(project), params={"state": "exploded"})).status_code == 422
    assert (await keyed.get(jobs_url(project), params={"limit": 500})).status_code == 422


async def test_cancel_queued_job_removes_dispatch_entry(
    keyed: Api, project: dict[str, Any], dispatcher: Dispatcher
) -> None:
    job = (await submit(keyed, project)).json()["job"]
    r = await keyed.post(f"{jobs_url(project)}/{job['id']}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "cancelled"
    assert r.json()["finished_at"] is not None
    assert await dispatcher.redis.zcard(f"qf:q:{(await default_queue(keyed, project))['id']}") == 0
    again = await keyed.post(f"{jobs_url(project)}/{job['id']}/cancel")
    assert again.status_code == 409


async def default_queue(api: Api, project: dict[str, Any]) -> dict[str, Any]:
    queues = (await api.get(f"/api/v1/projects/{project['id']}/queues")).json()
    return next(q for q in queues if q["name"] == "default")


async def test_cancel_running_job_only_requests_cancellation(
    keyed: Api, project: dict[str, Any], db: Database
) -> None:
    job = (await submit(keyed, project)).json()["job"]
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE jobs SET state = 'running', attempts = 1, worker_id = 'w' WHERE id = :i"),
            {"i": job["id"]},
        )
    r = await keyed.post(f"{jobs_url(project)}/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "running" and r.json()["cancel_requested"] is True


async def test_retry_failed_job_grants_new_budget(
    keyed: Api, project: dict[str, Any], db: Database, dispatcher: Dispatcher
) -> None:
    job = (await submit(keyed, project, max_attempts=2)).json()["job"]
    assert (
        await keyed.post(f"{jobs_url(project)}/{job['id']}/retry")
    ).status_code == 409  # queued: not retryable
    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE jobs SET state = 'failed', attempts = 2, finished_at = now(), error_fingerprint = 'abc' WHERE id = :i"
            ),
            {"i": job["id"]},
        )
    await dispatcher.redis.flushdb()
    r = await keyed.post(f"{jobs_url(project)}/{job['id']}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "queued" and body["max_attempts"] == 4 and body["manual_retries"] == 1
    assert body["finished_at"] is None
    assert "manual retry" in body["events"][-1]["message"]
    assert await dispatcher.redis.zrange(
        f"qf:q:{(await default_queue(keyed, project))['id']}", 0, -1
    ) == [job["id"]]


async def test_bulk_actions_report_per_item(keyed: Api, project: dict[str, Any]) -> None:
    a = (await submit(keyed, project)).json()["job"]["id"]
    b = (await submit(keyed, project)).json()["job"]["id"]
    missing = str(uuid.uuid4())
    r = await keyed.post(
        f"{jobs_url(project)}/bulk", json={"action": "cancel", "job_ids": [a, b, missing, a]}
    )
    body = r.json()
    assert r.status_code == 200
    assert body["succeeded"] == 2 and body["failed"] == 1
    assert {x["job_id"]: x["ok"] for x in body["results"]} == {a: True, b: True, missing: False}
    r = await keyed.post(f"{jobs_url(project)}/bulk", json={"action": "retry", "job_ids": [a]})
    assert r.json()["results"][0]["state"] == "queued"
    too_many = await keyed.post(
        f"{jobs_url(project)}/bulk",
        json={"action": "cancel", "job_ids": [str(uuid.uuid4()) for _ in range(101)]},
    )
    assert too_many.status_code == 422


async def test_backlog_cap_rejects_new_jobs(
    owner: Api, keyed: Api, project: dict[str, Any]
) -> None:
    r = await owner.patch(f"/api/v1/projects/{project['id']}", json={"max_queued_jobs": 2})
    assert r.status_code == 200
    assert (await submit(keyed, project)).status_code == 201
    assert (await submit(keyed, project, delay_seconds=60)).status_code == 201
    full = await submit(keyed, project)
    assert full.status_code == 429 and full.json()["error"]["code"] == "backlog_full"
    assert full.headers["retry-after"] == "30"


async def test_project_defaults_apply_and_settings_validate(
    owner: Api, keyed: Api, project: dict[str, Any]
) -> None:
    r = await owner.patch(
        f"/api/v1/projects/{project['id']}",
        json={"default_max_attempts": 7, "default_timeout_seconds": 45},
    )
    assert r.status_code == 200
    job = (await submit(keyed, project)).json()["job"]
    assert job["max_attempts"] == 7 and job["timeout_seconds"] == 45
    bad = await owner.patch(
        f"/api/v1/projects/{project['id']}",
        json={"default_backoff_base_seconds": 5000, "default_backoff_max_seconds": 60},
    )
    assert bad.status_code == 422
    audit = (await owner.get(f"/api/v1/projects/{project['id']}/audit")).json()
    assert any(e["action"] == "project.settings_updated" for e in audit["items"])


async def test_queue_management(owner: Api, keyed: Api, project: dict[str, Any]) -> None:
    base = f"/api/v1/projects/{project['id']}/queues"
    created = await owner.post(base, json={"name": "emails", "concurrency_limit": 2})
    assert created.status_code == 201
    assert (await owner.post(base, json={"name": "emails"})).status_code == 409
    qid = created.json()["id"]
    paused = await owner.patch(
        f"{base}/{qid}", json={"paused": True, "clear_concurrency_limit": True}
    )
    assert paused.json()["paused"] is True and paused.json()["concurrency_limit"] is None
    assert (await submit(keyed, project, queue="emails")).status_code == 201
    assert (await owner.delete(f"{base}/{qid}")).status_code == 409  # has job records
    default_id = next(q["id"] for q in (await owner.get(base)).json() if q["name"] == "default")
    assert (await owner.delete(f"{base}/{default_id}")).status_code == 403
    empty = (await owner.post(base, json={"name": "scratch"})).json()
    assert (await owner.delete(f"{base}/{empty['id']}")).status_code == 204


async def test_job_types_are_published(client: httpx.AsyncClient, owner: Api) -> None:
    r = await client.get("/api/v1/job-types")
    names = [t["name"] for t in r.json()]
    assert names == sorted(names) and "notification.send" in names
    assert all(t["payload_schema"]["type"] == "object" for t in r.json())
