from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Response, status

from queueforge.api.deps import (
    Principal,
    ProjectAccess,
    get_db,
    get_dispatcher,
    get_principal,
    get_settings_dep,
    project_access,
)
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.schemas import (
    ErrorResponse,
    JobDetail,
    ScheduleCreate,
    ScheduleOut,
    SchedulePreview,
    SchedulePreviewRequest,
    ScheduleUpdate,
)
from queueforge.services import jobs as job_svc
from queueforge.services import schedules as svc

router = APIRouter(tags=["schedules"])
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post("/schedules/preview", response_model=SchedulePreview, responses=ERRORS)
async def preview(
    body: SchedulePreviewRequest, _: Principal = Depends(get_principal)
) -> SchedulePreview:
    """Validate a cron expression or interval and list its next run times (UTC)."""
    return svc.preview(body.cron_expression, body.interval_seconds, body.count)


@router.get("/projects/{project_id}/schedules", response_model=list[ScheduleOut], responses=ERRORS)
async def list_schedules(
    access: ProjectAccess = Depends(project_access("schedules:read")),
    db: Database = Depends(get_db),
) -> list[ScheduleOut]:
    return await svc.list_schedules(db, access.project.id)


@router.post(
    "/projects/{project_id}/schedules",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
async def create_schedule(
    body: ScheduleCreate,
    access: ProjectAccess = Depends(project_access("schedules:write")),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ScheduleOut:
    return await svc.create_schedule(db, settings, access.project.id, body, access.actor)


@router.patch(
    "/projects/{project_id}/schedules/{schedule_id}", response_model=ScheduleOut, responses=ERRORS
)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    access: ProjectAccess = Depends(project_access("schedules:write")),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ScheduleOut:
    return await svc.update_schedule(
        db, settings, access.project.id, schedule_id, body, access.actor
    )


@router.delete(
    "/projects/{project_id}/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERRORS,
)
async def delete_schedule(
    schedule_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access("schedules:write")),
    db: Database = Depends(get_db),
) -> Response:
    await svc.delete_schedule(db, access.project.id, schedule_id, access.actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/projects/{project_id}/schedules/{schedule_id}/trigger",
    response_model=JobDetail,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
async def trigger(
    schedule_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access("schedules:write")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> JobDetail:
    """Run the schedule's job now, without changing its cadence."""
    job_id = await svc.trigger_now(db, dispatcher, access.project, schedule_id, access.actor)
    return await job_svc.get_job_detail(db, access.project.id, job_id)
