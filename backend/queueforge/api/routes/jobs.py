from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse

from queueforge.api.deps import (
    ProjectAccess,
    get_db,
    get_dispatcher,
    get_settings_dep,
    project_access,
)
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.handlers import all_handlers
from queueforge.lifecycle import ALL_STATES
from queueforge.schemas import (
    BulkAction,
    BulkResult,
    ErrorResponse,
    FailureGroup,
    GroupRetryResult,
    JobCreate,
    JobDetail,
    JobSubmitResponse,
    JobSummary,
    JobTypeOut,
    Page,
)
from queueforge.services import jobs as svc

router = APIRouter(tags=["jobs"])
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


@router.get("/job-types", response_model=list[JobTypeOut])
async def job_types() -> list[JobTypeOut]:
    """Registered handlers with their payload JSON Schemas and a valid example payload."""
    return [JobTypeOut(**spec.describe()) for spec in all_handlers()]


@router.post(
    "/projects/{project_id}/jobs",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **ERRORS,
        200: {
            "model": JobSubmitResponse,
            "description": "Existing job returned for a repeated idempotency key",
        },
    },
)
async def submit_job(
    body: JobCreate,
    access: ProjectAccess = Depends(project_access("jobs:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> JSONResponse:
    """Submit a job. It is durable in PostgreSQL before this returns.

    With `idempotency_key`, repeating the request returns the original job (`200`, `created: false`);
    reusing the key with a different type or payload returns `409`.
    """
    job_id, created = await svc.submit_job(
        db, dispatcher, settings, access.project, body, access.actor
    )
    detail = await svc.get_job_detail(db, access.project.id, job_id)
    payload = JobSubmitResponse(job=detail, created=created)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=payload.model_dump(mode="json"),
        headers={"Location": f"/api/v1/projects/{access.project.id}/jobs/{job_id}"},
    )


@router.get("/projects/{project_id}/jobs", response_model=Page[JobSummary], responses=ERRORS)
async def list_jobs(
    access: ProjectAccess = Depends(project_access("jobs:read")),
    db: Database = Depends(get_db),
    state: list[str] | None = Query(
        None, description=f"Repeatable. One of: {', '.join(ALL_STATES)}"
    ),
    type: list[str] | None = Query(None, max_length=20),
    queue: list[str] | None = Query(None, max_length=20),
    q: str | None = Query(
        None,
        max_length=128,
        description="Job ID (or prefix), idempotency key prefix, or type prefix",
    ),
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    schedule_id: uuid.UUID | None = None,
    error_fingerprint: str | None = Query(None, max_length=16),
    sort: Literal[
        "created_at", "updated_at", "finished_at", "priority", "attempts", "run_at"
    ] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=1_000_000),
) -> Page[JobSummary]:
    if state:
        from queueforge.errors import ValidationFailed

        bad = [s for s in state if s not in ALL_STATES]
        if bad:
            raise ValidationFailed(
                f"Unknown state(s): {', '.join(bad)}",
                details=[
                    {"loc": ["query", "state"], "msg": "unknown state", "type": "value_error"}
                ],
            )
    filters = svc.JobFilters(
        states=state,
        types=type,
        queues=queue,
        search=q,
        created_from=created_from,
        created_to=created_to,
        schedule_id=schedule_id,
        error_fingerprint=error_fingerprint,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return await svc.list_jobs(db, access.project.id, filters)


@router.get("/projects/{project_id}/jobs/{job_id}", response_model=JobDetail, responses=ERRORS)
async def get_job(
    job_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access("jobs:read")),
    db: Database = Depends(get_db),
) -> JobDetail:
    """Full job record: payload, result, structured error, attempt history and state timeline."""
    return await svc.get_job_detail(db, access.project.id, job_id)


@router.post(
    "/projects/{project_id}/jobs/{job_id}/cancel", response_model=JobDetail, responses=ERRORS
)
async def cancel_job(
    job_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access("jobs:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> JobDetail:
    """Cancel a pending job, or request cancellation of a running one (`cancel_requested: true`)."""
    await svc.cancel_job(db, dispatcher, access.project.id, job_id, access.actor)
    return await svc.get_job_detail(db, access.project.id, job_id)


@router.post(
    "/projects/{project_id}/jobs/{job_id}/retry", response_model=JobDetail, responses=ERRORS
)
async def retry_job(
    job_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access("jobs:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> JobDetail:
    """Re-queue a failed or cancelled job with a fresh attempt budget. History is preserved."""
    await svc.retry_job(db, dispatcher, access.project.id, job_id, access.actor)
    return await svc.get_job_detail(db, access.project.id, job_id)


@router.post("/projects/{project_id}/jobs/bulk", response_model=BulkResult, responses=ERRORS)
async def bulk(
    body: BulkAction,
    access: ProjectAccess = Depends(project_access("jobs:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> BulkResult:
    """Cancel or retry up to 100 jobs. Each job is handled independently; see per-item results."""
    return await svc.bulk_action(
        db, dispatcher, access.project.id, body.action, body.job_ids, access.actor
    )


@router.get("/projects/{project_id}/failures", response_model=list[FailureGroup], responses=ERRORS)
async def failures(
    access: ProjectAccess = Depends(project_access("jobs:read")), db: Database = Depends(get_db)
) -> list[FailureGroup]:
    """Dead-lettered jobs grouped by error fingerprint."""
    return await svc.failure_groups(db, access.project.id)


@router.post(
    "/projects/{project_id}/failures/{fingerprint}/retry",
    response_model=GroupRetryResult,
    responses=ERRORS,
)
async def retry_group(
    fingerprint: str,
    access: ProjectAccess = Depends(project_access("jobs:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> GroupRetryResult:
    """Retry every failed job in a group (up to 1000 per call)."""
    count = await svc.retry_failure_group(
        db, dispatcher, access.project.id, fingerprint[:16], access.actor
    )
    return GroupRetryResult(fingerprint=fingerprint, retried=count)


_ = Response  # re-exported for type checkers in route signatures
