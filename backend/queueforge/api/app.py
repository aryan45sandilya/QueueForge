"""FastAPI application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from queueforge import __version__
from queueforge.api.routes import auth, jobs, ops, projects, schedules
from queueforge.config import Settings, get_settings
from queueforge.db import Database, make_engine
from queueforge.dispatch import Dispatcher, make_redis
from queueforge.errors import AppError, PayloadTooLarge, error_body
from queueforge.logs import configure_logging, get_logger

log = get_logger("queueforge.http")

API_DESCRIPTION = """
QueueForge accepts background jobs over an authenticated REST API, executes them on independent
workers, and records every attempt.

**Authentication.** API clients send `Authorization: Bearer qf_<prefix>_<secret>` with a
project-scoped API key. The dashboard uses a session cookie plus an `X-CSRF-Token` header on
state-changing requests.

**Errors.** Every error has the shape
`{"error": {"code": "...", "message": "...", "details": ..., "request_id": "..."}}`.

**Delivery.** Execution is at-least-once. Use `idempotency_key` to make submission safe to retry.
"""


class RequestContextMiddleware:
    """Request IDs, access logging (no query strings or bodies) and security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1")
        request_id = (
            incoming if 0 < len(incoming) <= 64 and incoming.isprintable() else uuid.uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                extra = [
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in ("/healthz", "/readyz"):
                log.info(
                    "request",
                    method=scope.get("method"),
                    path=path,
                    status=status_holder["status"],
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
            structlog.contextvars.unbind_contextvars("request_id")


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            response = JSONResponse(
                status_code=413,
                content=error_body(
                    "payload_too_large",
                    f"Request body exceeds {self.max_bytes} bytes",
                    request_id=scope.get("state", {}).get("request_id"),
                ),
            )
            await response(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise PayloadTooLarge(f"Request body exceeds {self.max_bytes} bytes")
            return message

        await self.app(scope, limited, send)


def _request_id(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=error_body(exc.code, exc.message, exc.details, _request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_body(
                "validation_error", "Request validation failed", details, _request_id(request)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden"}
        return JSONResponse(
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
            content=error_body(
                codes.get(exc.status_code, "http_error"),
                str(exc.detail),
                request_id=_request_id(request),
            ),
        )

    async def unavailable(request: Request, exc: Exception) -> JSONResponse:
        log.error("database_unavailable", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content=error_body(
                "service_unavailable",
                "The database is unavailable; the request was not processed. Retry later.",
                request_id=_request_id(request),
            ),
        )

    for exc_type in (OperationalError, InterfaceError, PoolTimeout, ConnectionError, OSError):
        app.add_exception_handler(exc_type, unavailable)

    @app.exception_handler(DBAPIError)
    async def dbapi_error(request: Request, exc: DBAPIError) -> JSONResponse:
        if exc.connection_invalidated:
            return await unavailable(request, exc)
        log.exception(
            "database_error", error_type=type(exc.orig).__name__ if exc.orig else "DBAPIError"
        )
        return JSONResponse(
            status_code=500,
            content=error_body(
                "internal_error", "Internal server error", request_id=_request_id(request)
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_error", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content=error_body(
                "internal_error", "Internal server error", request_id=_request_id(request)
            ),
        )


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    dispatcher: Dispatcher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owns_db = db is None
        owns_redis = dispatcher is None
        app.state.settings = settings
        app.state.db = db or Database(make_engine(settings))
        app.state.dispatcher = dispatcher or Dispatcher(make_redis(settings.redis_url))
        log.info("api_started", env=settings.env, version=__version__)
        try:
            yield
        finally:
            if owns_db:
                await app.state.db.dispose()
            if owns_redis:
                await app.state.dispatcher.redis.aclose()

    app = FastAPI(
        title="QueueForge API",
        version=__version__,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    # Settings are also needed before lifespan runs (e.g. TestClient without lifespan).
    app.state.settings = settings
    if db is not None:
        app.state.db = db
    if dispatcher is not None:
        app.state.dispatcher = dispatcher

    install_error_handlers(app)
    for router in (auth.router, projects.router, jobs.router, schedules.router, ops.router):
        app.include_router(router, prefix="/api/v1")
    app.add_api_route("/healthz", ops.healthz, methods=["GET"], include_in_schema=False)
    app.add_api_route("/readyz", ops.readyz, methods=["GET"], include_in_schema=False)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
        expose_headers=[
            "X-Request-ID",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Location",
        ],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)
    _ = Any
    return app
