"""Importing this package registers every bundled handler."""

from queueforge.handlers import notification, report, synthetic, transform, webhook
from queueforge.handlers.base import (
    HandlerSpec,
    JobContext,
    PermanentError,
    RetryableError,
    all_handlers,
    get_handler,
    validate_payload,
)

__all__ = [
    "HandlerSpec",
    "JobContext",
    "PermanentError",
    "RetryableError",
    "all_handlers",
    "get_handler",
    "notification",
    "report",
    "synthetic",
    "transform",
    "validate_payload",
    "webhook",
]
