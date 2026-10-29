"""report.generate: builds a job-activity report for the submitting project.

The report is computed from the project's own persisted job and attempt records (read-only)
and rendered as Markdown or CSV. Re-running it is safe; results reflect the data at run time.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from queueforge.handlers.base import HandlerSpec, JobContext, PermanentError, register


class ReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["job_activity"] = "job_activity"
    title: str = Field(default="Job activity report", min_length=1, max_length=120)
    period_hours: int = Field(default=24, ge=1, le=720)
    group_by: Literal["type", "queue"] = "type"
    format: Literal["markdown", "csv"] = "markdown"


_QUERY = """
WITH scoped AS (
    SELECT j.id, j.state, j.attempts, {group_expr} AS grp
    FROM jobs j JOIN queues q ON q.id = j.queue_id
    WHERE j.project_id = :project_id AND j.created_at >= :since AND j.created_at < :until
),
durations AS (
    SELECT {group_expr} AS grp, a.duration_ms
    FROM job_attempts a JOIN jobs j ON j.id = a.job_id JOIN queues q ON q.id = j.queue_id
    WHERE a.project_id = :project_id AND a.finished_at >= :since AND a.finished_at < :until
      AND a.outcome IN ('succeeded', 'failed', 'timed_out') AND a.duration_ms IS NOT NULL
)
SELECT g.grp,
       g.submitted, g.succeeded, g.failed, g.cancelled, g.active, g.retried,
       d.p50, d.p95
FROM (
    SELECT grp,
           count(*) AS submitted,
           count(*) FILTER (WHERE state = 'succeeded') AS succeeded,
           count(*) FILTER (WHERE state = 'failed') AS failed,
           count(*) FILTER (WHERE state = 'cancelled') AS cancelled,
           count(*) FILTER (WHERE state IN ('scheduled', 'queued', 'running', 'retrying')) AS active,
           count(*) FILTER (WHERE attempts > 1) AS retried
    FROM scoped GROUP BY grp
) g
LEFT JOIN (
    SELECT grp,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95
    FROM durations GROUP BY grp
) d ON d.grp = g.grp
ORDER BY g.submitted DESC, g.grp
"""

_COLUMNS = [
    "group",
    "submitted",
    "succeeded",
    "failed",
    "cancelled",
    "active",
    "retried",
    "p50_ms",
    "p95_ms",
    "success_rate",
]


def _rate(succeeded: int, failed: int) -> float | None:
    finished = succeeded + failed
    return round(succeeded / finished, 4) if finished else None


def _markdown(
    title: str, since: datetime, until: datetime, group_by: str, rows: list[dict[str, Any]]
) -> str:
    lines = [
        f"# {title}",
        "",
        f"Period: {since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M} UTC. Grouped by job {group_by}.",
        "",
        "| " + " | ".join(_COLUMNS) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(_COLUMNS) - 1)) + "|",
    ]
    for row in rows:
        cells = ["" if row[c] is None else str(row[c]) for c in _COLUMNS]
        lines.append("| " + " | ".join(cells) + " |")
    if not rows:
        lines.append("| (no jobs in period) |" + " |" * (len(_COLUMNS) - 1))
    return "\n".join(lines) + "\n"


async def generate_report(payload: ReportPayload, ctx: JobContext) -> dict[str, Any]:
    if ctx.db is None:
        raise PermanentError("report.generate requires database access")
    until = datetime.now(UTC)
    since = until - timedelta(hours=payload.period_hours)
    group_expr = "j.type" if payload.group_by == "type" else "q.name"
    async with ctx.db.session() as session:
        result = await session.execute(
            text(_QUERY.format(group_expr=group_expr)),
            {"project_id": ctx.project_id, "since": since, "until": until},
        )
        raw = result.mappings().all()
    await ctx.checkpoint()

    rows: list[dict[str, Any]] = []
    for r in raw:
        rows.append(
            {
                "group": r["grp"],
                "submitted": int(r["submitted"]),
                "succeeded": int(r["succeeded"]),
                "failed": int(r["failed"]),
                "cancelled": int(r["cancelled"]),
                "active": int(r["active"]),
                "retried": int(r["retried"]),
                "p50_ms": None if r["p50"] is None else round(float(r["p50"])),
                "p95_ms": None if r["p95"] is None else round(float(r["p95"])),
                "success_rate": _rate(int(r["succeeded"]), int(r["failed"])),
            }
        )
    totals = {
        k: sum(int(r[k]) for r in rows)
        for k in ("submitted", "succeeded", "failed", "cancelled", "active", "retried")
    }

    if payload.format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        document = buffer.getvalue()
    else:
        document = _markdown(payload.title, since, until, payload.group_by, rows)

    ctx.logger.info("report_generated", groups=len(rows), period_hours=payload.period_hours)
    return {
        "kind": payload.kind,
        "title": payload.title,
        "period": {
            "from": since.isoformat(),
            "to": until.isoformat(),
            "hours": payload.period_hours,
        },
        "group_by": payload.group_by,
        "totals": {**totals, "success_rate": _rate(totals["succeeded"], totals["failed"])},
        "rows": rows,
        "format": payload.format,
        "document": document,
    }


SPEC = register(
    HandlerSpec(
        name="report.generate",
        title="Generate activity report",
        description=(
            "Summarizes this project's jobs over a period: volume, outcomes, retries and "
            "execution-time percentiles, grouped by job type or queue."
        ),
        side_effects="Read-only queries against this project's records; safe to retry.",
        payload_model=ReportPayload,
        run=generate_report,
        default_timeout_seconds=60,
        example={
            "title": "Daily operations report",
            "period_hours": 24,
            "group_by": "type",
            "format": "markdown",
        },
    )
)
