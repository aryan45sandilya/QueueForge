"""Metrics are derived from persisted records; verify exact values for a known workload."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.lifecycle import AttemptOutcome
from queueforge.services.execution import Outcome, build_error, claim_job, complete_job
from tests.conftest import Api
from tests.helpers import make_project, submit


async def test_metrics_match_a_known_workload(
    settings: Settings, db: Database, dispatcher: Dispatcher
) -> None:
    project = await make_project(db)
    ok = [await submit(db, dispatcher, settings, project) for _ in range(3)]
    bad = await submit(
        db,
        dispatcher,
        settings,
        project,
        max_attempts=2,
        backoff_base_seconds=1,
        backoff_max_seconds=1,
    )
    await submit(db, dispatcher, settings, project)  # stays queued
    await submit(db, dispatcher, settings, project, delay_seconds=600)  # scheduled

    for job in ok:
        claim = (await claim_job(db, job, "w1", 30)).claim
        assert claim
        assert (
            await complete_job(db, claim, "w1", Outcome(AttemptOutcome.SUCCEEDED, result={}))
            == "succeeded"
        )
    for attempt in (1, 2):
        claim = (await claim_job(db, bad, "w1", 30)).claim
        assert claim and claim.attempt == attempt
        error = build_error(
            None, error_type="Boom", message="exploded", retryable=True, attempt=attempt
        )
        state = await complete_job(db, claim, "w1", Outcome(AttemptOutcome.FAILED, error=error))
        if attempt == 1:
            assert state == "retrying"
            async with db.transaction() as session:
                await session.execute(
                    text("UPDATE jobs SET state = 'queued', queued_at = now() WHERE id = :i"),
                    {"i": bad},
                )
        else:
            assert state == "failed"

    from queueforge.services.metrics import overview

    m = await overview(db, settings, project.id, "1h")
    assert (m.submitted, m.succeeded, m.failed, m.cancelled) == (6, 3, 1, 0)
    assert m.retries == 1
    assert m.failure_rate == 0.25
    assert m.active == {"scheduled": 1, "queued": 1, "running": 0, "retrying": 0}
    assert m.wait_time.samples == 5  # one per attempt started
    assert m.execution_time.samples == 5
    assert m.execution_time.p50_ms is not None and m.execution_time.p50_ms >= 0
    assert sum(p.submitted for p in m.series) == 6
    assert sum(p.succeeded for p in m.series) == 3 and sum(p.failed for p in m.series) == 1
    assert m.bucket_seconds == 60 and 60 <= len(m.series) <= 62
    default = next(q for q in m.queues if q.queue == "default")
    assert (
        default.queued == 1
        and default.scheduled == 1
        and default.oldest_queued_age_seconds is not None
    )
    assert m.completed_per_minute == round(4 / 60, 3)
    assert set(m.definitions) >= {"wait_time", "execution_time", "failure_rate"}


async def test_metrics_endpoint_empty_project(keyed: Api, project: dict[str, Any]) -> None:
    for window in ("1h", "24h", "7d"):
        r = await keyed.get(f"/api/v1/projects/{project['id']}/metrics", params={"window": window})
        assert r.status_code == 200
        body = r.json()
        assert body["submitted"] == 0 and body["failure_rate"] is None
        assert body["wait_time"]["p95_ms"] is None
    assert (
        await keyed.get(f"/api/v1/projects/{project['id']}/metrics", params={"window": "1y"})
    ).status_code == 422


async def test_failure_groups_and_group_retry(
    keyed: Api, project: dict[str, Any], db: Database, dispatcher: Dispatcher
) -> None:
    pid = project["id"]
    ids = [
        (
            await keyed.post(
                f"/api/v1/projects/{pid}/jobs", json={"type": "bench.synthetic", "payload": {}}
            )
        ).json()["job"]["id"]
        for _ in range(3)
    ]
    async with db.transaction() as session:
        for i, job_id in enumerate(ids):
            error = build_error(
                None,
                error_type="SMTPError" if i < 2 else "Timeout",
                message=f"server 10.0.0.{i} refused after {i * 100} ms",
                retryable=True,
                attempt=1,
            )
            await session.execute(
                text(
                    "UPDATE jobs SET state = 'failed', attempts = 1, finished_at = now(), last_error = CAST(:e AS jsonb), "
                    "error_fingerprint = :f WHERE id = :i"
                ),
                {"e": __import__("json").dumps(error), "f": error["fingerprint"], "i": job_id},
            )
    groups = (await keyed.get(f"/api/v1/projects/{pid}/failures")).json()
    assert [(g["error_type"], g["count"]) for g in groups] == [("SMTPError", 2), ("Timeout", 1)]
    filtered = (
        await keyed.get(
            f"/api/v1/projects/{pid}/jobs", params={"error_fingerprint": groups[0]["fingerprint"]}
        )
    ).json()
    assert filtered["total"] == 2
    r = await keyed.post(f"/api/v1/projects/{pid}/failures/{groups[0]['fingerprint']}/retry")
    assert r.json()["retried"] == 2
    remaining = (await keyed.get(f"/api/v1/projects/{pid}/failures")).json()
    assert [g["error_type"] for g in remaining] == ["Timeout"]
