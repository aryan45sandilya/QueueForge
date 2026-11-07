from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select

from queueforge.api.deps import (
    Principal,
    ProjectAccess,
    get_db,
    get_dispatcher,
    get_settings_dep,
    project_access,
    require_user,
)
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.models import ApiKey, AuditEvent, User
from queueforge.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    ApiKeyRotate,
    AuditOut,
    ErrorResponse,
    Page,
    ProjectCreate,
    ProjectOut,
    ProjectSettingsUpdate,
    QueueCreate,
    QueueOut,
    QueueUpdate,
    WebhookSecretOut,
)
from queueforge.services import accounts

router = APIRouter(tags=["projects"])
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.get("/projects", response_model=list[ProjectOut], responses=ERRORS)
async def list_projects(
    principal: Principal = Depends(require_user), db: Database = Depends(get_db)
) -> list[ProjectOut]:
    assert principal.user is not None
    out: list[ProjectOut] = []
    for m in await accounts.memberships(db, principal.user.id):
        membership = await accounts.get_membership(db, m.project_id, principal.user.id)
        if membership:
            out.append(accounts.project_out(*membership))
    return out


@router.post(
    "/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
async def create_project(
    body: ProjectCreate,
    principal: Principal = Depends(require_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ProjectOut:
    """Create a project; the caller becomes its owner and a `default` queue is created."""
    assert principal.user is not None
    return await accounts.create_project(db, principal.user, body, settings)


@router.get("/projects/{project_id}", response_model=ProjectOut, responses=ERRORS)
async def get_project(access: ProjectAccess = Depends(project_access())) -> ProjectOut:
    return accounts.project_out(access.project, access.role)


@router.patch("/projects/{project_id}", response_model=ProjectOut, responses=ERRORS)
async def update_project(
    body: ProjectSettingsUpdate,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ProjectOut:
    """Update project name and job defaults (retry policy, timeout, retention, backlog cap). Owners only."""
    return await accounts.update_project(db, access.project.id, body, access.actor, settings)


@router.get(
    "/projects/{project_id}/webhook-secret", response_model=WebhookSecretOut, responses=ERRORS
)
async def get_webhook_secret(
    access: ProjectAccess = Depends(project_access(owner=True)), db: Database = Depends(get_db)
) -> WebhookSecretOut:
    """The secret used to sign this project's `http.request` webhook calls (created on first use)."""
    return WebhookSecretOut(secret=await accounts.webhook_secret(db, access.project.id))


@router.post(
    "/projects/{project_id}/webhook-secret/rotate",
    response_model=WebhookSecretOut,
    responses=ERRORS,
)
async def rotate_webhook_secret(
    access: ProjectAccess = Depends(project_access(owner=True)), db: Database = Depends(get_db)
) -> WebhookSecretOut:
    """Replace the signing secret. Calls made after this are signed with the new secret."""
    return WebhookSecretOut(
        secret=await accounts.webhook_secret(db, access.project.id, rotate=True, actor=access.actor)
    )


# --- API keys -------------------------------------------------------------------------------


@router.get("/projects/{project_id}/api-keys", response_model=list[ApiKeyOut], responses=ERRORS)
async def list_keys(
    access: ProjectAccess = Depends(project_access(owner=True)), db: Database = Depends(get_db)
) -> list[ApiKeyOut]:
    return await accounts.list_keys(db, access.project.id)


@router.post(
    "/projects/{project_id}/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
async def create_key(
    body: ApiKeyCreate,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ApiKeyCreated:
    """Create a project-scoped API key. The `secret` is returned only in this response."""
    key, secret = await accounts.create_key(db, settings, access.project.id, body, access.actor)
    return ApiKeyCreated(key=key, secret=secret)


@router.post(
    "/projects/{project_id}/api-keys/{key_id}/revoke", response_model=ApiKeyOut, responses=ERRORS
)
async def revoke_key(
    key_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
) -> ApiKeyOut:
    return await accounts.revoke_key(db, access.project.id, key_id, access.actor)


@router.post(
    "/projects/{project_id}/api-keys/{key_id}/rotate",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
async def rotate_key(
    key_id: uuid.UUID,
    body: ApiKeyRotate,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> ApiKeyCreated:
    """Issue a replacement key. The old key keeps working for `grace_period_hours` (0 = revoke now)."""
    key, secret = await accounts.rotate_key(
        db, settings, access.project.id, key_id, body.grace_period_hours, access.actor
    )
    return ApiKeyCreated(key=key, secret=secret)


# --- queues ---------------------------------------------------------------------------------


@router.get("/projects/{project_id}/queues", response_model=list[QueueOut], responses=ERRORS)
async def list_queues(
    access: ProjectAccess = Depends(project_access("jobs:read")),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> list[QueueOut]:
    return await accounts.list_queues(db, dispatcher, access.project.id)


@router.post(
    "/projects/{project_id}/queues",
    response_model=QueueOut,
    status_code=status.HTTP_201_CREATED,
    responses=ERRORS,
)
async def create_queue(
    body: QueueCreate,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> QueueOut:
    return await accounts.create_queue(db, access.project.id, body, access.actor, settings)


@router.patch("/projects/{project_id}/queues/{queue_id}", response_model=QueueOut, responses=ERRORS)
async def update_queue(
    queue_id: uuid.UUID,
    body: QueueUpdate,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> QueueOut:
    """Change description, concurrency limit (global across workers) or pause/resume dispatch."""
    return await accounts.update_queue(
        db, access.project.id, queue_id, body, access.actor, settings
    )


@router.delete(
    "/projects/{project_id}/queues/{queue_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=ERRORS,
)
async def delete_queue(
    queue_id: uuid.UUID,
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
) -> Response:
    await accounts.delete_queue(db, dispatcher, access.project.id, queue_id, access.actor)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- audit ----------------------------------------------------------------------------------


@router.get("/projects/{project_id}/audit", response_model=Page[AuditOut], responses=ERRORS)
async def audit_log(
    access: ProjectAccess = Depends(project_access(owner=True)),
    db: Database = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=100_000),
) -> Page[AuditOut]:
    from sqlalchemy import func

    async with db.session() as session:
        total = (
            await session.execute(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.project_id == access.project.id)
            )
        ).scalar_one()
        rows = (
            await session.execute(
                select(AuditEvent, User.email, ApiKey.prefix)
                .outerjoin(User, User.id == AuditEvent.actor_user_id)
                .outerjoin(ApiKey, ApiKey.id == AuditEvent.actor_api_key_id)
                .where(AuditEvent.project_id == access.project.id)
                .order_by(AuditEvent.at.desc(), AuditEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    items = [
        AuditOut(
            id=e.id,
            at=e.at,
            action=e.action,
            target_type=e.target_type,
            target_id=e.target_id,
            actor=(e.details or {}).get("actor")
            or (f"user:{email}" if email else f"key:{prefix}" if prefix else "system"),
            details={k: v for k, v in (e.details or {}).items() if k != "actor"},
        )
        for e, email, prefix in rows
    ]
    return Page[AuditOut](items=items, total=int(total), limit=limit, offset=offset)
