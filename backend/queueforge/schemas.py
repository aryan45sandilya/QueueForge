"""Pydantic request/response contracts for the public API (v1)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from queueforge.lifecycle import MAX_ATTEMPTS_CAP, JobState
from queueforge.security import API_KEY_SCOPES

T = TypeVar("T")

QUEUE_NAME = r"^[a-z0-9][a-z0-9_.\-]{0,63}$"
SLUG = r"^[a-z0-9][a-z0-9\-]{1,62}$"


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):  # noqa: UP046 - explicit TypeVar keeps pydantic generics simple
    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# --- auth -----------------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip()
        if not re.match(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,63}$", value):
            raise ValueError("not a valid email address")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class UserOut(ORM):
    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime


class MembershipOut(BaseModel):
    project_id: uuid.UUID
    slug: str
    name: str
    role: str


class MeResponse(BaseModel):
    user: UserOut
    csrf_token: str
    projects: list[MembershipOut]


# --- projects -------------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=SLUG)


class ProjectSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    default_max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    default_timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    default_backoff_base_seconds: int | None = Field(default=None, ge=1, le=86400)
    default_backoff_max_seconds: int | None = Field(default=None, ge=1, le=86400)
    retention_days: int | None = Field(default=None, ge=1, le=365)
    max_queued_jobs: int | None = Field(default=None, ge=1, le=10_000_000)


class ProjectOut(ORM):
    id: uuid.UUID
    slug: str
    name: str
    created_at: datetime
    default_max_attempts: int
    default_timeout_seconds: int
    default_backoff_base_seconds: int
    default_backoff_max_seconds: int
    retention_days: int
    max_queued_jobs: int
    role: str | None = None


# --- API keys -------------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=lambda: list(API_KEY_SCOPES), min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(API_KEY_SCOPES))
        if unknown:
            raise ValueError(f"unknown scope(s): {', '.join(unknown)}")
        return sorted(set(value))


class ApiKeyRotate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grace_period_hours: int = Field(default=24, ge=0, le=720)


class ApiKeyOut(ORM):
    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    rotated_from_id: uuid.UUID | None
    status: str = "active"


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    secret: str = Field(description="The full API key. Shown only once; store it securely.")


# --- queues ---------------------------------------------------------------------------------


class QueueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=QUEUE_NAME)
    description: str = Field(default="", max_length=255)
    concurrency_limit: int | None = Field(default=None, ge=1, le=1000)


class QueueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = Field(default=None, max_length=255)
    concurrency_limit: int | None = Field(default=None, ge=1, le=1000)
    clear_concurrency_limit: bool = False
    paused: bool | None = None


class QueueOut(ORM):
    id: uuid.UUID
    name: str
    description: str
    concurrency_limit: int | None
    paused: bool
    created_at: datetime
    queued: int = 0
    running: int = 0
    scheduled: int = 0
    retrying: int = 0
    oldest_queued_at: datetime | None = None
    dispatch_depth: int | None = None


# --- jobs -----------------------------------------------------------------------------------


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(min_length=1, max_length=64, examples=["notification.send"])
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = Field(default="default", pattern=QUEUE_NAME)
    priority: int = Field(default=5, ge=0, le=9, description="0 (lowest) to 9 (highest)")
    run_at: datetime | None = Field(default=None, description="Earliest execution time (UTC)")
    delay_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    backoff_base_seconds: int | None = Field(default=None, ge=1, le=86400)
    backoff_max_seconds: int | None = Field(default=None, ge=1, le=86400)
    idempotency_key: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$"
    )

    @model_validator(mode="after")
    def _one_delay(self) -> JobCreate:
        if self.run_at is not None and self.delay_seconds is not None:
            raise ValueError("specify either run_at or delay_seconds, not both")
        if self.run_at is not None and self.run_at.tzinfo is None:
            raise ValueError("run_at must include a timezone offset")
        return self


class JobSummary(ORM):
    id: uuid.UUID
    type: str
    state: JobState
    queue: str
    priority: int
    attempts: int
    max_attempts: int
    idempotency_key: str | None
    run_at: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested: bool
    error_type: str | None = None
    error_message: str | None = None
    schedule_id: uuid.UUID | None = None


class AttemptOut(ORM):
    attempt_number: int
    worker_id: str | None
    outcome: str
    queued_at: datetime | None
    started_at: datetime
    finished_at: datetime | None
    wait_ms: int | None
    duration_ms: int | None
    error: dict[str, Any] | None


class EventOut(ORM):
    id: int
    at: datetime
    from_state: str | None
    to_state: str
    actor: str
    message: str


class JobDetail(JobSummary):
    payload: dict[str, Any]
    result: dict[str, Any] | None
    last_error: dict[str, Any] | None
    timeout_seconds: int
    backoff_base_seconds: int
    backoff_max_seconds: int
    attempt_budget: int
    manual_retries: int
    queued_at: datetime | None
    lease_expires_at: datetime | None
    worker_id: str | None
    scheduled_for: datetime | None
    created_by: str | None = None
    attempt_history: list[AttemptOut] = []
    events: list[EventOut] = []


class JobSubmitResponse(BaseModel):
    job: JobDetail
    created: bool = Field(
        description="False when an existing job was returned for the idempotency key"
    )


class BulkAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["cancel", "retry"]
    job_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BulkItemResult(BaseModel):
    job_id: uuid.UUID
    ok: bool
    state: str | None = None
    error: str | None = None


class BulkResult(BaseModel):
    action: str
    succeeded: int
    failed: int
    results: list[BulkItemResult]


class FailureGroup(BaseModel):
    fingerprint: str
    error_type: str
    sample_message: str
    count: int
    job_types: list[str]
    queues: list[str]
    first_failed_at: datetime
    last_failed_at: datetime
    sample_job_id: uuid.UUID
    retryable: bool


class GroupRetryResult(BaseModel):
    fingerprint: str
    retried: int


class JobTypeOut(BaseModel):
    name: str
    title: str
    description: str
    side_effects: str
    default_timeout_seconds: int
    demo_only: bool
    payload_schema: dict[str, Any]
    example: dict[str, Any]


# --- schedules ------------------------------------------------------------------------------


class ScheduleBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron_expression: str | None = Field(default=None, max_length=120)
    interval_seconds: int | None = Field(default=None, ge=10, le=2_678_400)


class ScheduleCreate(ScheduleBase):
    name: str = Field(min_length=1, max_length=80)
    job_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = Field(default="default", pattern=QUEUE_NAME)
    priority: int = Field(default=5, ge=0, le=9)
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    skip_if_running: bool = True
    enabled: bool = True

    @model_validator(mode="after")
    def _one_trigger(self) -> ScheduleCreate:
        if (self.cron_expression is None) == (self.interval_seconds is None):
            raise ValueError("specify exactly one of cron_expression or interval_seconds")
        return self


class ScheduleUpdate(ScheduleBase):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    payload: dict[str, Any] | None = None
    priority: int | None = Field(default=None, ge=0, le=9)
    max_attempts: int | None = Field(default=None, ge=1, le=MAX_ATTEMPTS_CAP)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    skip_if_running: bool | None = None
    enabled: bool | None = None


class ScheduleOut(ORM):
    id: uuid.UUID
    name: str
    job_type: str
    queue: str
    payload: dict[str, Any]
    cron_expression: str | None
    interval_seconds: int | None
    priority: int
    max_attempts: int | None
    timeout_seconds: int | None
    skip_if_running: bool
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_job_id: uuid.UUID | None
    last_job_state: str | None = None
    last_skipped_at: datetime | None
    created_at: datetime
    upcoming: list[datetime] = []


class SchedulePreviewRequest(ScheduleBase):
    count: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _one_trigger(self) -> SchedulePreviewRequest:
        if (self.cron_expression is None) == (self.interval_seconds is None):
            raise ValueError("specify exactly one of cron_expression or interval_seconds")
        return self


class SchedulePreview(BaseModel):
    description: str
    next_runs: list[datetime]


# --- workers, metrics, audit ----------------------------------------------------------------


class WorkerJobOut(BaseModel):
    id: uuid.UUID
    type: str
    started_at: datetime | None


class WorkerOut(ORM):
    id: str
    hostname: str
    pid: int
    version: str
    queues: list[str]
    concurrency: int
    state: str
    health: str
    dispatch_mode: str
    started_at: datetime
    last_heartbeat_at: datetime
    heartbeat_age_seconds: float
    stopped_at: datetime | None
    running_jobs: int
    processed_count: int
    failed_count: int
    rss_bytes: int | None
    project_jobs: list[WorkerJobOut] = []


class LatencySummary(BaseModel):
    samples: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None


class SeriesPoint(BaseModel):
    t: datetime
    submitted: int
    succeeded: int
    failed: int
    retried: int
    wait_p95_ms: float | None
    exec_p95_ms: float | None


class QueueHealth(BaseModel):
    queue: str
    paused: bool
    concurrency_limit: int | None
    queued: int
    running: int
    scheduled: int
    retrying: int
    oldest_queued_age_seconds: float | None


class MetricsOverview(BaseModel):
    window: str
    window_start: datetime
    window_end: datetime
    bucket_seconds: int
    submitted: int
    succeeded: int
    failed: int
    cancelled: int
    retries: int
    failure_rate: float | None = Field(
        description="failed / (succeeded + failed) for jobs finished in the window"
    )
    completed_per_minute: float
    submitted_per_minute: float
    active: dict[str, int]
    wait_time: LatencySummary
    execution_time: LatencySummary
    series: list[SeriesPoint]
    queues: list[QueueHealth]
    workers: dict[str, int]
    definitions: dict[str, str]


class AuditOut(ORM):
    id: int
    at: datetime
    action: str
    target_type: str
    target_id: str | None
    actor: str
    details: dict[str, Any]


class HealthOut(BaseModel):
    status: str
    checks: dict[str, str]
    version: str
