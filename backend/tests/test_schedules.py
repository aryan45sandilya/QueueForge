"""Recurring schedules: trigger math, API validation, materialization rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.services import maintenance
from queueforge.services.schedules import describe, next_fire
from tests.conftest import Api
from tests.helpers import scalar

T0 = datetime(2026, 3, 1, 10, 7, 30, tzinfo=UTC)


def test_cron_next_fire_is_strictly_after() -> None:
    assert next_fire("*/15 * * * *", None, T0) == datetime(2026, 3, 1, 10, 15, tzinfo=UTC)
    assert next_fire("0 9 * * 1", None, T0) == datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # next Monday
    assert next_fire("0 10 * * *", None, datetime(2026, 3, 1, 10, 0, tzinfo=UTC)) == datetime(
        2026, 3, 2, 10, 0, tzinfo=UTC
    )


def test_interval_keeps_cadence_from_anchor() -> None:
    anchor = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    assert next_fire(None, 300, T0, anchor=anchor) == datetime(2026, 3, 1, 10, 10, tzinfo=UTC)
    assert next_fire(None, 300, T0) == T0 + timedelta(seconds=300)
    assert describe(None, 7200) == "Every 2 hours" and describe(None, 90) == "Every 90 seconds"


def url(project: dict[str, Any]) -> str:
    return f"/api/v1/projects/{project['id']}/schedules"


async def test_preview_validates_and_lists_runs(owner: Api) -> None:
    r = await owner.post(
        "/api/v1/schedules/preview", json={"cron_expression": "30 2 * * *", "count": 3}
    )
    assert r.status_code == 200
    runs = [datetime.fromisoformat(x) for x in r.json()["next_runs"]]
    assert len(runs) == 3 and all(x.hour == 2 and x.minute == 30 for x in runs)
    assert (
        await owner.post("/api/v1/schedules/preview", json={"cron_expression": "61 * * * *"})
    ).status_code == 422
    assert (
        await owner.post("/api/v1/schedules/preview", json={"cron_expression": "* * * * * *"})
    ).status_code == 422
    assert (
        await owner.post("/api/v1/schedules/preview", json={"interval_seconds": 5})
    ).status_code == 422
    assert (await owner.post("/api/v1/schedules/preview", json={})).status_code == 422


async def test_schedule_crud_and_trigger(owner: Api, keyed: Api, project: dict[str, Any]) -> None:
    body = {
        "name": "nightly",
        "job_type": "report.generate",
        "cron_expression": "0 3 * * *",
        "payload": {"period_hours": 24},
    }
    created = await keyed.post(url(project), json=body)
    assert created.status_code == 201, created.text
    sched = created.json()
    assert sched["queue"] == "default" and len(sched["upcoming"]) == 3
    assert (await keyed.post(url(project), json=body)).status_code == 409
    bad = await keyed.post(
        url(project), json={**body, "name": "bad", "payload": {"period_hours": 0}}
    )
    assert bad.status_code == 422
    both = await keyed.post(url(project), json={**body, "name": "both", "interval_seconds": 60})
    assert both.status_code == 422

    patched = await keyed.patch(
        f"{url(project)}/{sched['id']}", json={"interval_seconds": 600, "cron_expression": None}
    )
    assert patched.status_code == 200 and patched.json()["interval_seconds"] == 600
    disabled = await keyed.patch(f"{url(project)}/{sched['id']}", json={"enabled": False})
    assert disabled.json()["enabled"] is False and disabled.json()["upcoming"] == []

    run = await keyed.post(f"{url(project)}/{sched['id']}/trigger")
    assert run.status_code == 201
    assert run.json()["schedule_id"] == sched["id"] and run.json()["state"] == "queued"
    listed = (await keyed.get(url(project))).json()
    assert listed[0]["last_job_id"] == run.json()["id"] and listed[0]["last_job_state"] == "queued"

    assert (await keyed.delete(f"{url(project)}/{sched['id']}")).status_code == 204
    assert (await keyed.get(url(project))).json() == []


async def make_due(db: Database, schedule_id: str) -> None:
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE schedules SET next_run_at = now() - interval '1 second' WHERE id = :i"),
            {"i": schedule_id},
        )


async def test_materialization_skips_overlap_and_disables_invalid(
    owner: Api,
    keyed: Api,
    project: dict[str, Any],
    db: Database,
    dispatcher: Dispatcher,
    settings: Settings,
) -> None:
    sched = (
        await keyed.post(
            url(project),
            json={
                "name": "probe",
                "job_type": "bench.synthetic",
                "interval_seconds": 60,
                "payload": {"work_ms": 1},
            },
        )
    ).json()
    await make_due(db, sched["id"])
    assert await maintenance.materialize_schedules(db, dispatcher) == 1
    job_id = await scalar(db, "SELECT last_job_id FROM schedules WHERE id = :i", i=sched["id"])
    assert await scalar(db, "SELECT scheduled_for IS NOT NULL FROM jobs WHERE id = :i", i=job_id)

    # Previous run still queued -> the next occurrence is skipped (skip_if_running defaults to true).
    await make_due(db, sched["id"])
    assert await maintenance.materialize_schedules(db, dispatcher) == 0
    assert await scalar(
        db, "SELECT last_skipped_at IS NOT NULL FROM schedules WHERE id = :i", i=sched["id"]
    )

    # A payload that no longer validates disables the schedule rather than failing every minute.
    async with db.transaction() as session:
        await session.execute(
            text("UPDATE jobs SET state = 'succeeded', finished_at = now() WHERE id = :i"),
            {"i": job_id},
        )
        await session.execute(
            text("UPDATE schedules SET payload = '{\"work_ms\": -5}' WHERE id = :i"),
            {"i": sched["id"]},
        )
    await make_due(db, sched["id"])
    assert await maintenance.materialize_schedules(db, dispatcher) == 0
    assert await scalar(db, "SELECT enabled FROM schedules WHERE id = :i", i=sched["id"]) is False
    audit = (await owner.get(f"/api/v1/projects/{project['id']}/audit")).json()["items"]
    assert any(e["action"] == "schedule.disabled" for e in audit)
    assert settings.env == "test"
