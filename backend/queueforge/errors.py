"""Consistent error contract: every error response is

{"error": {"code": str, "message": str, "details": any | null, "request_id": str | null}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        details: Any = None,
        headers: dict[str, str] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.headers = headers
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class BacklogFull(AppError):
    status_code = 429
    code = "backlog_full"


class QuotaExceeded(AppError):
    status_code = 429
    code = "quota_exceeded"


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"


def error_body(
    code: str, message: str, details: Any = None, request_id: str | None = None
) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": message, "details": details, "request_id": request_id}
    }
