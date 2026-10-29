"""Runtime configuration, loaded from QF_* environment variables (and an optional .env file).

Validation happens at import time of `get_settings()` so that a misconfigured process
fails fast at startup instead of on the first request.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_INSECURE_SECRETS = {
    "",
    "change-me",
    "changeme",
    "secret",
    "dev-secret-change-me-dev-secret-change-me",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="QF_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://queueforge:queueforge@localhost:5432/queueforge"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = Field(default="dev-secret-change-me-dev-secret-change-me", min_length=32)

    # HTTP
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    cookie_secure: bool = False
    session_ttl_hours: int = Field(default=72, ge=1, le=24 * 30)
    allow_registration: bool = True
    max_request_bytes: int = Field(default=1_048_576, ge=1024)
    max_payload_bytes: int = Field(default=65_536, ge=256)
    max_result_bytes: int = Field(default=65_536, ge=256)
    rate_limit_per_minute: int = Field(default=600, ge=1)
    login_rate_limit_per_minute: int = Field(default=10, ge=1)
    trust_proxy_headers: bool = False

    # Worker
    worker_concurrency: int = Field(default=8, ge=1, le=256)
    worker_queues: Annotated[list[str], NoDecode] = []  # empty = every queue of every project
    lease_seconds: int = Field(default=30, ge=2)
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    shutdown_grace_seconds: float = Field(default=20.0, ge=0)
    queue_refresh_seconds: float = Field(default=5.0, gt=0)

    # Scheduler
    scheduler_tick_seconds: float = Field(default=1.0, gt=0)
    reconcile_interval_seconds: float = Field(default=15.0, gt=0)
    reconcile_grace_seconds: float = Field(default=10.0, ge=0)
    retention_interval_seconds: float = Field(default=300.0, gt=0)

    # Logging
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    # Demo seed (development only)
    demo_email: str = "demo@queueforge.dev"
    demo_password: str = "queueforge-demo"  # noqa: S105 - local demo seed only; seed refuses production

    @field_validator("cors_origins", "worker_queues", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg(cls, value: str) -> str:
        if value.startswith("postgres://"):
            value = "postgresql://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value.removeprefix("postgresql://")
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("QF_DATABASE_URL must be a PostgreSQL URL")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            if self.secret_key in _INSECURE_SECRETS:
                raise ValueError("QF_SECRET_KEY must be set to a unique random value in production")
            if "*" in self.cors_origins:
                raise ValueError("QF_CORS_ORIGINS may not contain '*' in production")
        if self.heartbeat_seconds * 2 >= self.lease_seconds:
            raise ValueError("QF_HEARTBEAT_SECONDS must be less than half of QF_LEASE_SECONDS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
