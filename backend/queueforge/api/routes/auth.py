from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status

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
from queueforge.schemas import (
    AcceptedOut,
    ChangePasswordRequest,
    DeleteAccountRequest,
    DeletedAccountOut,
    EmailRequest,
    ErrorResponse,
    LoginRequest,
    MeResponse,
    RegisterOut,
    RegisterRequest,
    ResetPasswordRequest,
    TokenRequest,
    UserOut,
)
from queueforge.services import accounts, identity
from queueforge.services.email import OutgoingEmail, send_email

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


ACCEPTED = (
    "If an account exists for that address, an email is on its way. "
    "It can take a few minutes to arrive."
)


def _queue_email(tasks: BackgroundTasks, settings: Settings, message: OutgoingEmail | None) -> None:
    """Send after the response so timing doesn't reveal whether the account exists."""
    if message is not None:
        tasks.add_task(send_email, settings, message)


async def _limit_by_ip(
    request: Request, response: Response, dispatcher: Dispatcher, settings: Settings, action: str
) -> None:
    await enforce_rate_limit(
        dispatcher,
        response,
        f"{action}:{client_ip(request, settings)}",
        settings.login_rate_limit_per_minute,
    )


@router.post(
    "/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED, responses=ERRORS
)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> RegisterOut:
    """Create an account. Disabled when QF_ALLOW_REGISTRATION=false.

    With QF_REQUIRE_EMAIL_VERIFICATION=true, a confirmation link is emailed and the account
    can't sign in until it is followed.
    """
    if not settings.allow_registration:
        raise Forbidden("Registration is disabled on this instance")
    await _limit_by_ip(request, response, dispatcher, settings, "register")
    required = settings.require_email_verification
    user = await accounts.register_user(
        db, body.email, body.password, body.display_name, verified=not required
    )
    if required:
        _queue_email(tasks, settings, await identity.verification_for_new_user(db, settings, user))
    return RegisterOut(**UserOut.model_validate(user).model_dump(), verification_required=required)


@router.post("/verify-email", response_model=UserOut, responses={400: {"model": ErrorResponse}})
async def verify_email(body: TokenRequest, db: Database = Depends(get_db)) -> UserOut:
    """Confirm an email address with the token from the verification email."""
    return UserOut.model_validate(await identity.verify_email(db, body.token))


@router.post(
    "/resend-verification",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERRORS,
)
async def resend_verification(
    body: EmailRequest,
    request: Request,
    response: Response,
    tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> AcceptedOut:
    """Send a new verification link. The response is the same whether or not the account exists."""
    await _limit_by_ip(request, response, dispatcher, settings, "resend-verification")
    _queue_email(tasks, settings, await identity.resend_verification(db, settings, body.email))
    return AcceptedOut(message=ACCEPTED)


@router.post(
    "/forgot-password",
    response_model=AcceptedOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses=ERRORS,
)
async def forgot_password(
    body: EmailRequest,
    request: Request,
    response: Response,
    tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    dispatcher: Dispatcher = Depends(get_dispatcher),
    settings: Settings = Depends(get_settings_dep),
) -> AcceptedOut:
    """Email a one-hour password reset link. Same response whether or not the account exists."""
    await _limit_by_ip(request, response, dispatcher, settings, "forgot-password")
    _queue_email(tasks, settings, await identity.request_password_reset(db, settings, body.email))
    return AcceptedOut(message=ACCEPTED)


@router.post(
    "/reset-password",
    response_model=AcceptedOut,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def reset_password(body: ResetPasswordRequest, db: Database = Depends(get_db)) -> AcceptedOut:
    """Set a new password with a reset token. Signs the account out of every session."""
    await identity.reset_password(db, body.token, body.new_password)
    return AcceptedOut(message="Password updated. Sign in with your new password.")


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT, responses=ERRORS)
async def change_password(
    body: ChangePasswordRequest,
    principal: Principal = Depends(require_user),
    db: Database = Depends(get_db),
) -> Response:
    """Change the password; other sessions are signed out, this one stays."""
    assert principal.user is not None and principal.session is not None
    await identity.change_password(
        db, principal.user.id, principal.session.id, body.current_password, body.new_password
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/delete-account", response_model=DeletedAccountOut, responses=ERRORS)
async def delete_account(
    body: DeleteAccountRequest,
    principal: Principal = Depends(require_user),
    db: Database = Depends(get_db),
) -> Response:
    """Permanently delete the account and every project it alone owns, with all their jobs."""
    assert principal.user is not None
    deleted = await identity.delete_account(db, principal.user.id, body.password)
    response = Response(
        content=DeletedAccountOut(projects_deleted=deleted).model_dump_json(),
        media_type="application/json",
    )
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


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
