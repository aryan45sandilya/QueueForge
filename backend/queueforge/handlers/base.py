"""Registered job handlers. Submitted jobs name a handler; arbitrary code is never executed.

A handler is an async function `(payload_model_instance, JobContext) -> dict`. It signals
failures by raising:

- `PermanentError`: retrying cannot help (bad input, missing resource). Goes to `failed`.
- `RetryableError` or any other exception: retried with backoff while attempts remain.

Handlers must be idempotent with respect to external side effects (ADR 0002): use
`ctx.job_id` as a deduplication key downstream.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from queueforge.db import Database
from queueforge.errors import ValidationFailed


class PermanentError(Exception):
    """The job can never succeed as submitted."""


class RetryableError(Exception):
    """A transient failure; the job should be retried."""


@dataclass
class JobContext:
    job_id: uuid.UUID
    project_id: uuid.UUID
    attempt: int
    max_attempts: int
    db: Database | None
    logger: Any
    progress: dict[str, Any] = field(default_factory=dict)

    async def checkpoint(self) -> None:
        """Yield to the event loop so timeouts, cancellation and heartbeats stay responsive."""
        await asyncio.sleep(0)


HandlerFn = Callable[[Any, JobContext], Coroutine[Any, Any, dict[str, Any]]]


@dataclass(frozen=True)
class HandlerSpec:
    name: str
    title: str
    description: str
    payload_model: type[BaseModel]
    run: HandlerFn
    example: dict[str, Any]
    default_timeout_seconds: int = 300
    side_effects: str = ""
    demo_only: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "side_effects": self.side_effects,
            "default_timeout_seconds": self.default_timeout_seconds,
            "demo_only": self.demo_only,
            "payload_schema": self.payload_model.model_json_schema(),
            "example": self.example,
        }


_REGISTRY: dict[str, HandlerSpec] = {}


def register(spec: HandlerSpec) -> HandlerSpec:
    if spec.name in _REGISTRY:
        raise RuntimeError(f"handler {spec.name!r} registered twice")
    # The bundled example must always validate; catches drift between schema and docs.
    spec.payload_model.model_validate(spec.example)
    _REGISTRY[spec.name] = spec
    return spec


def get_handler(name: str) -> HandlerSpec | None:
    return _REGISTRY.get(name)


def all_handlers() -> list[HandlerSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: s.name)


def validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": ["payload", *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


def validate_payload(job_type: str, payload: dict[str, Any]) -> BaseModel:
    spec = get_handler(job_type)
    if spec is None:
        known = ", ".join(s.name for s in all_handlers())
        raise ValidationFailed(
            f"Unknown job type {job_type!r}. Registered types: {known}",
            details=[{"loc": ["type"], "msg": "unknown job type", "type": "value_error"}],
        )
    try:
        return spec.payload_model.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailed(
            f"Payload is not valid for job type {job_type!r}", details=validation_details(exc)
        ) from exc
