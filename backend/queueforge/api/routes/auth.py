from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from queueforge.api.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    Principal,
    client_ip,
    enforce_rate_limit,
    get_db,
    get_dispatcher,
    get_settings_dep,
    require_user,
)
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.errors import Forbidden
from queueforge.schemas import ErrorResponse, LoginRequest, MeResponse, RegisterRequest, UserOut
from queueforge.services import accounts

router = APIRouter(prefix="/auth", tags=["auth"])

ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
    429: {"model": ErrorResponse},
}


def _set_cookies(
    response: Response, settings: Settings, token: str, csrf: str, max_age: int
) -> None:
    common = {"secure": settings.cookie_secure, "samesite": "lax", "path": "/", "max_age": max_age}
    response.set_cookie(SESSION_COOKIE, token, httponly=True, **common)  # type: ignore[arg-type]
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, **common)  # type: ignore[arg-type]


@router.post(
    "/register", response_model=UserOut, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> UserOut:
    """Create an account. Disabled when QF_ALLOW_REGISTRATION=false."""
    if not settings.allow_registration:
        raise Forbidden("Registration is disabled on this instance")
    await enforce_rate_limit(
        dispatcher,
        response,
        f"register:{client_ip(request, settings)}",
        settings.login_rate_limit_per_minute,
    )
    user = await accounts.register_user(db, body.email, body.password, body.display_name)
    return UserOut.model_validate(user)


@router.post("/login", response_model=MeResponse, responses=ERRORS)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> MeResponse:
    """Start a dashboard session (HttpOnly cookie) and return the CSRF token."""
    ip = client_ip(request, settings)
    await enforce_rate_limit(
        dispatcher,
        response,
        f"login:{ip}:{body.email.strip().lower()}",
        settings.login_rate_limit_per_minute,
    )
    new = await accounts.login(
        db, settings, body.email, body.password, request.headers.get("user-agent"), ip
    )
    _set_cookies(response, settings, new.token, new.csrf_token, settings.session_ttl_hours * 3600)
    return MeResponse(
        user=UserOut.model_validate(new.user),
        csrf_token=new.csrf_token,
        projects=await accounts.memberships(db, new.user.id),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: Principal = Depends(require_user),
    db: Database = Depends(get_db),
) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await accounts.logout(db, token)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@router.get("/me", response_model=MeResponse, responses=ERRORS)
async def me(
    principal: Principal = Depends(require_user), db: Database = Depends(get_db)
) -> MeResponse:
    assert principal.user is not None and principal.session is not None
    return MeResponse(
        user=UserOut.model_validate(principal.user),
        csrf_token=principal.session.csrf_token,
        projects=await accounts.memberships(db, principal.user.id),
    )
