"""Password hashing, session tokens and API key generation/verification (ADR 0004)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()

API_KEY_SCOPES: tuple[str, ...] = (
    "jobs:read",
    "jobs:write",
    "schedules:read",
    "schedules:write",
    "metrics:read",
)

# Used to keep verification time constant when a user doesn't exist.
_DUMMY_HASH = _hasher.hash("queueforge-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


@dataclass(frozen=True)
class GeneratedKey:
    prefix: str
    secret: str

    @property
    def full(self) -> str:
        return f"qf_{self.prefix}_{self.secret}"


def generate_api_key() -> GeneratedKey:
    # 12 base32-ish chars of prefix (lookup + display), 256 bits of secret.
    prefix = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    return GeneratedKey(prefix=prefix, secret=secret)


def hash_api_secret(secret_key: str, secret: str) -> bytes:
    return hmac.new(secret_key.encode(), secret.encode(), hashlib.sha256).digest()


def parse_api_key(raw: str) -> tuple[str, str] | None:
    """Split `qf_<prefix>_<secret>`; returns None for anything malformed."""
    if not raw.startswith("qf_") or len(raw) > 128:
        return None
    rest = raw[3:]
    prefix, sep, secret = rest.partition("_")
    if not sep or len(prefix) != 12 or not secret:
        return None
    return prefix, secret


def verify_api_secret(secret_key: str, secret: str, expected_hash: bytes) -> bool:
    return hmac.compare_digest(hash_api_secret(secret_key, secret), expected_hash)
