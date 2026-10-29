"""Unit tests: secrets handling and settings validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from queueforge.config import Settings
from queueforge.logs import redact_value
from queueforge.security import (
    generate_api_key,
    hash_api_secret,
    hash_password,
    parse_api_key,
    verify_api_secret,
    verify_password,
)


def test_password_hash_roundtrip_and_unknown_user() -> None:
    h = hash_password("hunter2hunter2")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "hunter2hunter2")
    assert not verify_password(h, "wrong-password")
    assert not verify_password(None, "hunter2hunter2")
    assert not verify_password("not-a-hash", "x")


def test_api_key_format_and_verification() -> None:
    key = generate_api_key()
    full = key.full
    assert full.startswith("qf_") and len(key.prefix) == 12
    assert parse_api_key(full) == (key.prefix, key.secret)
    digest = hash_api_secret("server-secret", key.secret)
    assert verify_api_secret("server-secret", key.secret, digest)
    assert not verify_api_secret("other-secret", key.secret, digest)
    assert key.secret not in digest.hex()


@pytest.mark.parametrize(
    "raw", ["", "qf_", "qf_short_x", "sk_abcdefabcdef_secret", "qf_abcdefabcdef", "qf_" + "a" * 200]
)
def test_malformed_keys_are_rejected(raw: str) -> None:
    assert parse_api_key(raw) is None


def test_redaction_masks_secrets_in_nested_values() -> None:
    key = generate_api_key().full
    data = {
        "message": f"call failed with {key}",
        "headers": {"Authorization": "Bearer abc.def", "X-Other": "fine"},
        "password": "p",
        "items": [{"api_key": "x"}, "Bearer sometoken"],
    }
    out = redact_value(data)
    text = repr(out)
    assert key not in text and "abc.def" not in text and "sometoken" not in text
    assert out["headers"]["Authorization"] == "[REDACTED]"
    assert out["password"] == "[REDACTED]"
    assert out["headers"]["X-Other"] == "fine"


def test_production_requires_real_secret_and_no_wildcard_cors() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, env="production", secret_key="dev-secret-change-me-dev-secret-change-me"
        )  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, env="production", secret_key="x" * 40, cors_origins=["*"])  # type: ignore[call-arg]
    ok = Settings(
        _env_file=None,
        env="production",
        secret_key="x" * 40,
        cors_origins="https://a.example, https://b.example",
    )  # type: ignore[call-arg]
    assert ok.cors_origins == ["https://a.example", "https://b.example"]


def test_settings_reject_unsafe_lease_and_non_postgres_url() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, lease_seconds=10, heartbeat_seconds=6)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="mysql://x")  # type: ignore[call-arg]
    assert Settings(_env_file=None, database_url="postgres://u:p@h/db").database_url.startswith(
        "postgresql+asyncpg://"
    )  # type: ignore[call-arg]
