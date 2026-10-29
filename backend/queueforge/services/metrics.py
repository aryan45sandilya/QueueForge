"""Operational metrics computed from persisted job and attempt records. Nothing is sampled
or estimated; every number here can be reproduced with SQL against the same tables.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.schemas import LatencySummary, MetricsOverview, QueueHealth, SeriesPoint

WINDOWS: dict[str, tuple[timedelta, int]] = {
    "1h": (timedelta(hours=1), 60),
    "24h": (timedelta(hours=24), 900),
    "7d": (timedelta(days=7), 10800),
}

DEFINITIONS = {
    "submitted": "Jobs created in the window (API, schedules and manual runs).",
    "succeeded": "Jobs that reached 'succeeded' in the window (by finished_at).",
    "failed": "Jobs that reached 'failed' (dead letter) in the window and are still failed.",
    "cancelled": "Jobs cancelled in the window.",
    "retries": "Attempts started in the window that were not a job's first attempt.",
    "failure_rate": "failed / (succeeded + failed) over jobs finished in the window.",
    "completed_per_minute": "(succeeded + failed) / window minutes: completed-job throughput.",
    "submitted_per_minute": "submitted / window minutes: arrival rate.",
    "wait_time": "Per attempt: started_at minus the time the job became runnable (queued_at). Attempts started in the window.",
    "execution_time": "Per attempt: finished_at minus started_at, for attempts that succeeded, failed or timed out in the window.",
    "active": "Jobs currently in each non-terminal state (point in time).",
    "queue_depth": "Jobs currently 'queued' (runnable, waiting for a worker).",
    "workers": "All worker processes, by heartbeat health: healthy (< 2 heartbeats old), stale, lost, stopped.",
    "retention": "Windows longer than the project's retention period undercount terminal jobs that were deleted.",
}

_TOTALS = """
SELECT
  (SELECT count(*) FROM jobs WHERE project_id = :p AND created_at >= :start) AS submitted,
  (SELECT count(*) FROM jobs WHERE project_id = :p AND finished_at >= :start AND state = 'succeeded') AS succeeded,
  (SELECT count(*) FROM jobs WHERE project_id = :p AND finished_at >= :start AND state = 'failed') AS failed,
  (SELECT count(*) FROM jobs WHERE project_id = :p AND finished_at >= :start AND state = 'cancelled') AS cancelled,
  (SELECT count(*) FROM job_attempts WHERE project_id = :p AND started_at >= :start AND attempt_number > 1) AS retries
"""

_LATENCY = """
SELECT count(v) AS samples,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY v) AS p95,
       percentile_cont(0.99) WITHIN GROUP (ORDER BY v) AS p99,
       max(v) AS mx
FROM ({inner}) s
"""
_WAIT_INNER = "SELECT wait_ms AS v FROM job_attempts WHERE project_id = :p AND started_at >= :start AND wait_ms IS NOT NULL"
_EXEC_INNER = (
    "SELECT duration_ms AS v FROM job_attempts WHERE project_id = :p AND finished_at >= :start "
    "AND outcome IN ('succeeded', 'failed', 'timed_out') AND duration_ms IS NOT NULL"
)

_SERIES = """
WITH b AS (
  SELECT generate_series(date_bin(make_interval(secs => :bucket), CAST(:start AS timestamptz), TIMESTAMPTZ '2000-01-01 00:00:00+00'),
                         CAST(:end AS timestamptz), make_interval(secs => :bucket)) AS t
),
sub AS (
  SELECT date_bin(make_interval(secs => :bucket), created_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS t, count(*) AS c
  FROM jobs WHERE project_id = :p AND created_at >= :start GROUP BY 1
),
fin AS (
  SELECT date_bin(make_interval(secs => :bucket), finished_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS t,
         count(*) FILTER (WHERE state = 'succeeded') AS s, count(*) FILTER (WHERE state = 'failed') AS f
  FROM jobs WHERE project_id = :p AND finished_at >= :start GROUP BY 1
),
att AS (
  SELECT date_bin(make_interval(secs => :bucket), started_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS t,
         count(*) FILTER (WHERE attempt_number > 1) AS r,
         percentile_cont(0.95) WITHIN GROUP (ORDER BY wait_ms) AS w95
  FROM job_attempts WHERE project_id = :p AND started_at >= :start GROUP BY 1
),
exe AS (
  SELECT date_bin(make_interval(secs => :bucket), finished_at, TIMESTAMPTZ '2000-01-01 00:00:00+00') AS t,
         percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS e95
  FROM job_attempts WHERE project_id = :p AND finished_at >= :start
    AND outcome IN ('succeeded', 'failed', 'timed_out') GROUP BY 1
)
SELECT b.t AS bucket, coalesce(sub.c, 0) AS submitted, coalesce(fin.s, 0) AS succeeded, coalesce(fin.f, 0) AS failed,
       coalesce(att.r, 0) AS retried, att.w95, exe.e95
FROM b
LEFT JOIN sub ON sub.t = b.t LEFT JOIN fin ON fin.t = b.t LEFT JOIN att ON att.t = b.t LEFT JOIN exe ON exe.t = b.t
ORDER BY b.t
"""

_ACTIVE = """
SELECT state, count(*) FROM jobs
WHERE project_id = :p AND state IN ('scheduled', 'queued', 'running', 'retrying')
GROUP BY state
"""

_QUEUES = """
SELECT q.name, q.paused, q.concurrency_limit,
       count(j.id) FILTER (WHERE j.state = 'queued') AS queued,
       count(j.id) FILTER (WHERE j.state = 'running') AS running,
       count(j.id) FILTER (WHERE j.state = 'scheduled') AS scheduled,
       count(j.id) FILTER (WHERE j.state = 'retrying') AS retrying,
       extract(epoch FROM (now() - min(j.queued_at) FILTER (WHERE j.state = 'queued'))) AS oldest
FROM queues q
LEFT JOIN jobs j ON j.queue_id = q.id AND j.state IN ('queued', 'running', 'scheduled', 'retrying')
WHERE q.project_id = :p
GROUP BY q.id ORDER BY q.name
"""

_WORKERS = """
SELECT
  count(*) FILTER (WHERE state IN ('running', 'draining') AND last_heartbeat_at >= now() - make_interval(secs => :hb2)) AS healthy,
  count(*) FILTER (WHERE state IN ('running', 'draining') AND last_heartbeat_at < now() - make_interval(secs => :hb2)) AS stale,
  count(*) FILTER (WHERE state = 'lost') AS lost,
  count(*) FILTER (WHERE state = 'stopped' AND stopped_at >= now() - interval '1 hour') AS stopped_recently,
  coalesce(sum(concurrency) FILTER (WHERE state IN ('running', 'draining') AND last_heartbeat_at >= now() - make_interval(secs => :hb2)), 0) AS slots
FROM workers
"""


def _latency(row: Any) -> LatencySummary:
    def f(v: Any) -> float | None:
        return None if v is None else round(float(v), 1)

    return LatencySummary(
        samples=int(row.samples or 0),
        p50_ms=f(row.p50),
        p95_ms=f(row.p95),
        p99_ms=f(row.p99),
        max_ms=f(row.mx),
    )


async def overview(
    db: Database, settings: Settings, project_id: uuid.UUID, window: str
) -> MetricsOverview:
    span, bucket = WINDOWS[window]
    async with db.session() as session:
        end: datetime = (await session.execute(text("SELECT now()"))).scalar_one()
        start = end - span
        params = {"p": project_id, "start": start}
        totals = (await session.execute(text(_TOTALS), params)).one()
        wait = (await session.execute(text(_LATENCY.format(inner=_WAIT_INNER)), params)).one()
        execution = (await session.execute(text(_LATENCY.format(inner=_EXEC_INNER)), params)).one()
        series_rows = (
            await session.execute(text(_SERIES), {**params, "end": end, "bucket": bucket})
        ).all()
        active_rows = (await session.execute(text(_ACTIVE), {"p": project_id})).all()
        queue_rows = (await session.execute(text(_QUEUES), {"p": project_id})).all()
        workers = (
            await session.execute(text(_WORKERS), {"hb2": settings.heartbeat_seconds * 2})
        ).one()

    minutes = span.total_seconds() / 60
    succeeded, failed = int(totals.succeeded), int(totals.failed)
    finished = succeeded + failed
    active = {"scheduled": 0, "queued": 0, "running": 0, "retrying": 0}
    for state, count in active_rows:
        active[state] = int(count)
    return MetricsOverview(
        window=window,
        window_start=start,
        window_end=end,
        bucket_seconds=bucket,
        submitted=int(totals.submitted),
        succeeded=succeeded,
        failed=failed,
        cancelled=int(totals.cancelled),
        retries=int(totals.retries),
        failure_rate=round(failed / finished, 4) if finished else None,
        completed_per_minute=round(finished / minutes, 3),
        submitted_per_minute=round(int(totals.submitted) / minutes, 3),
        active=active,
        wait_time=_latency(wait),
        execution_time=_latency(execution),
        series=[
            SeriesPoint(
                t=r.bucket,
                submitted=int(r.submitted),
                succeeded=int(r.succeeded),
                failed=int(r.failed),
                retried=int(r.retried),
                wait_p95_ms=None if r.w95 is None else round(float(r.w95), 1),
                exec_p95_ms=None if r.e95 is None else round(float(r.e95), 1),
            )
            for r in series_rows
        ],
        queues=[
            QueueHealth(
                queue=r.name,
                paused=r.paused,
                concurrency_limit=r.concurrency_limit,
                queued=int(r.queued),
                running=int(r.running),
                scheduled=int(r.scheduled),
                retrying=int(r.retrying),
                oldest_queued_age_seconds=None if r.oldest is None else round(float(r.oldest), 1),
            )
            for r in queue_rows
        ],
        workers={
            "healthy": int(workers.healthy),
            "stale": int(workers.stale),
            "lost": int(workers.lost),
            "stopped_recently": int(workers.stopped_recently),
            "slots": int(workers.slots),
        },
        definitions=DEFINITIONS,
    )
