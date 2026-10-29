"""Request dependencies: infrastructure handles, authentication, authorization, rate limits."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import Depends, Request, Response

from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher, DispatchUnavailable
from queueforge.errors import Forbidden, NotFound, RateLimited, Unauthorized
from queueforge.logs import get_logger
from queueforge.models import ApiKey, Project, Session, User
from queueforge.services import accounts
from queueforge.services.common import Actor

SESSION_COOKIE = "qf_session"
CSRF_COOKIE = "qf_csrf"
CSRF_HEADER = "x-csrf-token"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

log = get_logger("queueforge.api")


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_dispatcher(request: Request) -> Dispatcher:
    dispatcher: Dispatcher = request.app.state.dispatcher
    return dispatcher


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


async def enforce_rate_limit(
    dispatcher: Dispatcher, response: Response | None, bucket: str, limit: int
) -> None:
    try:
        allowed, remaining, reset = await dispatcher.hit(bucket, limit)
    except DispatchUnavailable:
        # Fail open: availability of the API matters more than strict limiting while Redis is down.
        log.warning("rate_limit_unavailable", bucket=bucket.split(":")[0])
        return
    if response is not None:
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset)
    if not allowed:
        raise RateLimited(
            f"Rate limit of {limit} requests per minute exceeded",
            headers={
                "Retry-After": str(reset),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
            },
            details={"retry_after_seconds": reset},
        )


@dataclass
class Principal:
    user: User | None = None
    session: Session | None = None
    api_key: ApiKey | None = None
    key_project: Project | None = None

    @property
    def actor(self) -> Actor:
        if self.api_key is not None:
            return Actor(
                label=f"api_key:{self.api_key.name} ({self.api_key.prefix})",
                api_key_id=self.api_key.id,
            )
        assert self.user is not None
        return Actor(label=f"user:{self.user.email}", user_id=self.user.id)


async def get_principal(
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> Principal:
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthorized("Authorization header must be 'Bearer <api key>'")
        key, project = await accounts.authenticate_key(db, settings, token.strip())
        await enforce_rate_limit(
            dispatcher, response, f"key:{key.id}", settings.rate_limit_per_minute
        )
        return Principal(api_key=key, key_project=project)

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        raise Unauthorized("Authentication required")
    resolved = await accounts.resolve_session(db, cookie)
    if resolved is None:
        raise Unauthorized("Session expired or invalid")
    user, session = resolved
    if request.method in UNSAFE_METHODS:
        supplied = request.headers.get(CSRF_HEADER, "")
        if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
            raise Forbidden("Missing or invalid CSRF token", code="csrf_failed")
    await enforce_rate_limit(
        dispatcher, response, f"user:{user.id}", settings.rate_limit_per_minute
    )
    return Principal(user=user, session=session)


async def require_user(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.user is None:
        raise Forbidden("This endpoint requires a dashboard session, not an API key")
    return principal


@dataclass
class ProjectAccess:
    project: Project
    principal: Principal
    role: str | None  # None for API keys

    @property
    def actor(self) -> Actor:
        return self.principal.actor


def project_access(
    scope: str | None = None, *, owner: bool = False
) -> Callable[..., Awaitable[ProjectAccess]]:
    """Authorize access to /projects/{project_id}/... for a session member or a scoped API key.

    Cross-project access with an API key yields 404, not 403, so project IDs can't be probed.
    """

    async def dependency(
        project_id: uuid.UUID,
        principal: Principal = Depends(get_principal),
        db: Database = Depends(get_db),
    ) -> ProjectAccess:
        if principal.api_key is not None:
            assert principal.key_project is not None
            if principal.key_project.id != project_id:
                raise NotFound("Project not found")
            if owner:
                raise Forbidden("This endpoint requires a dashboard session with the owner role")
            if scope is not None and scope not in principal.api_key.scopes:
                raise Forbidden(f"API key lacks the '{scope}' scope", code="insufficient_scope")
            return ProjectAccess(project=principal.key_project, principal=principal, role=None)
        assert principal.user is not None
        membership = await accounts.get_membership(db, project_id, principal.user.id)
        if membership is None:
            raise NotFound("Project not found")
        project, role = membership
        if owner and role != "owner":
            raise Forbidden("Only project owners can do this")
        return ProjectAccess(project=project, principal=principal, role=role)

    return dependency
