"""Structured logging with secret redaction.

Job payloads and results are never logged; only identifiers, states and error types.
The redaction processor is a second line of defence for anything that slips through.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|csrf|payload|credential)", re.I
)
_KEY_PATTERN = re.compile(r"qf_[0-9a-f]{12}_[A-Za-z0-9_\-]{20,}")
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _KEY_PATTERN.sub("qf_[REDACTED]", value)
        return _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


def _redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> Mapping[str, Any]:
    return {
        key: ("[REDACTED]" if _SENSITIVE_KEYS.search(key) else redact_value(value))
        for key, value in event_dict.items()
    }


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        level=log_level, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s"
    )
    # uvicorn's access log would include query strings; we log requests ourselves.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
