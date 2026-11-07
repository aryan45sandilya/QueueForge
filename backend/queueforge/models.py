"""SQLAlchemy models. PostgreSQL is the source of truth for every entity here (ADR 0001)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from queueforge.lifecycle import ALL_STATES, ATTEMPT_OUTCOMES

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    pass


Base.metadata.naming_convention = NAMING

TZ = DateTime(timezone=True)


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(TZ)
    email_verified_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)


class EmailToken(Base):
    """Single-use token sent by email (verification, password reset). Only a hash is stored."""

    __tablename__ = "email_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TZ)
    used_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint("purpose IN ('verify_email', 'reset_password')", name="purpose_valid"),
    )


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(TZ, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    # Defaults applied to jobs that don't specify their own policy.
    default_max_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("5"))
    default_timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("300"))
    default_backoff_base_seconds: Mapped[int] = mapped_column(Integer, server_default=text("10"))
    default_backoff_max_seconds: Mapped[int] = mapped_column(Integer, server_default=text("3600"))
    retention_days: Mapped[int] = mapped_column(SmallInteger, server_default=text("14"))
    max_queued_jobs: Mapped[int] = mapped_column(Integer, server_default=text("100000"))
    # Shared secret for signing outbound webhook requests; generated on first use.
    webhook_secret: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("default_max_attempts BETWEEN 1 AND 25", name="max_attempts_range"),
        CheckConstraint("default_timeout_seconds BETWEEN 1 AND 86400", name="timeout_range"),
        CheckConstraint(
            "default_backoff_base_seconds BETWEEN 1 AND default_backoff_max_seconds",
            name="backoff_range",
        ),
        CheckConstraint("default_backoff_max_seconds <= 86400", name="backoff_max_range"),
        CheckConstraint("retention_days BETWEEN 1 AND 365", name="retention_range"),
        CheckConstraint("max_queued_jobs BETWEEN 1 AND 10000000", name="max_queued_range"),
    )


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (CheckConstraint("role IN ('owner', 'member')", name="role_valid"),)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(80))
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    rotated_from_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)


class Queue(Base):
    __tablename__ = "queues"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), server_default="")
    concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        CheckConstraint("name ~ '^[a-z0-9][a-z0-9_.-]{0,63}$'", name="name_format"),
        CheckConstraint(
            "concurrency_limit IS NULL OR concurrency_limit BETWEEN 1 AND 1000",
            name="concurrency_range",
        ),
    )


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    queue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queues.id", ondelete="NO ACTION"))
    name: Mapped[str] = mapped_column(String(80))
    job_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    cron_expression: Mapped[str | None] = mapped_column(String(120))
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("5"))
    max_attempts: Mapped[int | None] = mapped_column(SmallInteger)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    skip_if_running: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    next_run_at: Mapped[datetime | None] = mapped_column(TZ)
    last_run_at: Mapped[datetime | None] = mapped_column(TZ)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_skipped_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "name"),
        CheckConstraint(
            "(cron_expression IS NULL) <> (interval_seconds IS NULL)", name="one_trigger"
        ),
        CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds BETWEEN 10 AND 2678400",
            name="interval_range",
        ),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority_range"),
        Index(
            "ix_schedules_due",
            "next_run_at",
            postgresql_where=text("enabled AND next_run_at IS NOT NULL"),
        ),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    queue_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queues.id", ondelete="NO ACTION"))
    type: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(SmallInteger, server_default=text("5"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_fingerprint: Mapped[str | None] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger)
    # The per-run budget requested at submission; a manual retry grants this many more attempts.
    attempt_budget: Mapped[int] = mapped_column(SmallInteger)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    backoff_base_seconds: Mapped[int] = mapped_column(Integer)
    backoff_max_seconds: Mapped[int] = mapped_column(Integer)
    manual_retries: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    run_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    queued_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZ)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(TZ)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schedules.id", ondelete="SET NULL")
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(TZ)
    created_by_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(_in("state", ALL_STATES), name="state_valid"),
        CheckConstraint("priority BETWEEN 0 AND 9", name="priority_range"),
        CheckConstraint("max_attempts BETWEEN 1 AND 1000", name="max_attempts_range"),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 86400", name="timeout_range"),
        Index("ix_jobs_project_created", "project_id", text("created_at DESC")),
        Index("ix_jobs_project_state_created", "project_id", "state", text("created_at DESC")),
        Index("ix_jobs_project_finished", "project_id", "finished_at"),
        Index(
            "ix_jobs_queue_active",
            "queue_id",
            "state",
            postgresql_where=text("state IN ('queued', 'running')"),
        ),
        Index(
            "ix_jobs_promotable",
            "run_at",
            postgresql_where=text("state IN ('scheduled', 'retrying')"),
        ),
        Index(
            "ix_jobs_running_lease", "lease_expires_at", postgresql_where=text("state = 'running'")
        ),
        Index(
            "ix_jobs_queued_order",
            "queue_id",
            text("priority DESC"),
            "queued_at",
            postgresql_where=text("state = 'queued'"),
        ),
        Index(
            "ix_jobs_failed_fingerprint",
            "project_id",
            "error_fingerprint",
            postgresql_where=text("state = 'failed'"),
        ),
        Index(
            "uq_jobs_project_idempotency",
            "project_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_jobs_schedule_occurrence",
            "schedule_id",
            "scheduled_for",
            unique=True,
            postgresql_where=text("schedule_id IS NOT NULL"),
        ),
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    attempt_number: Mapped[int] = mapped_column(SmallInteger)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(16))
    queued_at: Mapped[datetime | None] = mapped_column(TZ)
    started_at: Mapped[datetime] = mapped_column(TZ)
    finished_at: Mapped[datetime | None] = mapped_column(TZ)
    wait_ms: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number"),
        CheckConstraint(_in("outcome", ATTEMPT_OUTCOMES), name="outcome_valid"),
        Index("ix_job_attempts_project_started", "project_id", "started_at"),
        Index("ix_job_attempts_project_finished", "project_id", "finished_at"),
    )


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(160))
    message: Mapped[str] = mapped_column(String(500), server_default="")

    __table_args__ = (Index("ix_job_events_job", "job_id", "id"),)


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(String(32))
    queues: Mapped[list[str]] = mapped_column(ARRAY(String(64)))
    concurrency: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    dispatch_mode: Mapped[str] = mapped_column(String(16), server_default="redis")
    started_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    last_heartbeat_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    stopped_at: Mapped[datetime | None] = mapped_column(TZ)
    running_jobs: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    processed_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    rss_bytes: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("state IN ('running', 'draining', 'stopped', 'lost')", name="state_valid"),
        Index("ix_workers_heartbeat", "last_heartbeat_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (Index("ix_audit_events_project_at", "project_id", text("at DESC")),)
