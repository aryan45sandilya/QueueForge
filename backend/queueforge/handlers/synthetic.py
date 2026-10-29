"""bench.synthetic: a controllable workload for benchmarks, demos and recovery tests.

It sleeps (I/O-like, cooperative) for `work_ms`, can fail its first N attempts with a
retryable error, or fail permanently. It has no side effects.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from queueforge.handlers.base import (
    HandlerSpec,
    JobContext,
    PermanentError,
    RetryableError,
    register,
)


class SyntheticPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_ms: int = Field(default=50, ge=0, le=600_000)
    fail_attempts: int = Field(
        default=0, ge=0, le=25, description="Fail this many attempts before succeeding"
    )
    permanent_failure: bool = False
    label: str | None = Field(default=None, max_length=64)


async def run_synthetic(payload: SyntheticPayload, ctx: JobContext) -> dict[str, Any]:
    if payload.permanent_failure:
        raise PermanentError("Synthetic permanent failure requested by payload")
    remaining = payload.work_ms / 1000
    # Sleep in slices so cancellation and timeouts take effect promptly.
    while remaining > 0:
        step = min(remaining, 0.25)
        await asyncio.sleep(step)
        remaining -= step
    if ctx.attempt <= payload.fail_attempts:
        raise RetryableError(
            f"Synthetic transient failure on attempt {ctx.attempt} of {payload.fail_attempts + 1} planned"
        )
    return {"work_ms": payload.work_ms, "attempt": ctx.attempt, "label": payload.label}


SPEC = register(
    HandlerSpec(
        name="bench.synthetic",
        title="Synthetic workload",
        description="Sleeps for work_ms and optionally fails. Used by the benchmark and recovery demos.",
        side_effects="None.",
        payload_model=SyntheticPayload,
        run=run_synthetic,
        default_timeout_seconds=60,
        demo_only=True,
        example={"work_ms": 200, "fail_attempts": 1, "label": "retry-demo"},
    )
)
