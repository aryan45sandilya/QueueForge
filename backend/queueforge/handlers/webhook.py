"""http.request: call a URL you own, with retries, timeouts and a signature.

This is how users of a shared instance run their own logic: QueueForge schedules, retries and
records the call, and their endpoint does the work.

Server-side request forgery (SSRF) protections, unless QF_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true:
- the hostname is resolved by the worker and every address must be globally routable
  (no loopback, private, link-local, cloud metadata, multicast or reserved ranges);
- the connection goes to the exact IP that was checked (TLS still verifies the hostname), so a
  DNS answer can't change between the check and the request;
- redirects are never followed, URLs with embedded credentials are rejected, the timeout is
  capped by QF_WEBHOOK_MAX_TIMEOUT_SECONDS and at most 64 KiB of the response is read.

Every request carries `X-QueueForge-Job-Id`, `X-QueueForge-Attempt`, `Idempotency-Key` (the job
ID, stable across retries) and `X-QueueForge-Signature: t=<unix>,v1=<hex>` where v1 is
HMAC-SHA256(project webhook secret, f"{t}." + raw body).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from queueforge import __version__
from queueforge.errors import ValidationFailed
from queueforge.handlers.base import (
    HandlerSpec,
    JobContext,
    PermanentError,
    RetryableError,
    register,
)

MAX_RESPONSE_BYTES = 65_536
PREVIEW_CHARS = 4_000
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_RESERVED_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "upgrade",
    "te",
    "trailer",
    "proxy-authorization",
    "proxy-connection",
    "idempotency-key",
}
_RETRYABLE_STATUS = {408, 425, 429}


class WebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(max_length=2048, description="http(s) URL to call")
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    body: Any = Field(default=None, description="JSON body (object, array, string, number) or null")
    timeout_seconds: int = Field(default=10, ge=1, le=300)
    success_status: list[int] | None = Field(
        default=None,
        max_length=20,
        description="Status codes that count as success (default: any 2xx)",
    )

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https"):
            raise ValueError("url must start with http:// or https://")
        if not parts.hostname:
            raise ValueError("url must include a host")
        if parts.username or parts.password:
            raise ValueError("credentials in the URL are not allowed; send them in headers instead")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError("url has an invalid port") from exc
        return value.strip()

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if not _HEADER_NAME.match(name):
                raise ValueError(f"invalid header name {name[:64]!r}")
            lowered = name.lower()
            if lowered in _RESERVED_HEADERS or lowered.startswith("x-queueforge-"):
                raise ValueError(f"header {name!r} is set by QueueForge and can't be overridden")
            if len(header_value) > 2048 or "\r" in header_value or "\n" in header_value:
                raise ValueError(f"header {name!r} has an invalid value")
        return value

    @field_validator("success_status")
    @classmethod
    def _statuses(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(not 100 <= code <= 599 for code in value):
            raise ValueError("status codes must be between 100 and 599")
        return value


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def submit_check(payload: WebhookPayload, settings: Any) -> None:
    """Fast feedback at submission. The authoritative check runs on the worker before each call."""
    parts = urlsplit(payload.url)
    if settings.webhook_require_https and parts.scheme != "https":
        raise ValidationFailed(
            "Only https:// webhook URLs are allowed on this instance",
            details=[{"loc": ["payload", "url"], "msg": "https required", "type": "value_error"}],
        )
    if settings.webhook_allow_private_networks:
        return
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValidationFailed(
            "Webhook URLs must point at a public address",
            details=[{"loc": ["payload", "url"], "msg": "private host", "type": "value_error"}],
        )
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return
    if _blocked(literal):
        raise ValidationFailed(
            "Webhook URLs must point at a public address",
            details=[{"loc": ["payload", "url"], "msg": "private address", "type": "value_error"}],
        )


async def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(host)
        return [literal]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RetryableError(f"Could not resolve {host}: {exc.strerror or exc}") from exc
    seen: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        ip = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if ip not in seen:
            seen.append(ip)
    return seen


def sign(secret: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


async def _project_secret(ctx: JobContext) -> str | None:
    if ctx.db is None:
        return None
    from queueforge.services.accounts import webhook_secret  # local import: avoids a cycle

    return await webhook_secret(ctx.db, ctx.project_id)


def _encode_body(payload: WebhookPayload, headers: dict[str, str]) -> bytes:
    if payload.body is None:
        return b""
    has_type = any(k.lower() == "content-type" for k in headers)
    if isinstance(payload.body, str) and has_type:
        return payload.body.encode()
    if not has_type:
        headers["Content-Type"] = "application/json"
    return json.dumps(payload.body, separators=(",", ":"), ensure_ascii=False).encode()


async def call_webhook(payload: WebhookPayload, ctx: JobContext) -> dict[str, Any]:
    settings = ctx.settings
    allow_private = bool(settings and settings.webhook_allow_private_networks)
    max_timeout = settings.webhook_max_timeout_seconds if settings else 30
    if settings is not None:
        # Policy may have changed since submission; a refusal can't succeed on retry.
        try:
            submit_check(payload, settings)
        except ValidationFailed as exc:
            raise PermanentError(exc.message) from exc

    parts = urlsplit(payload.url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = await _resolve(host, port)
    if not addresses:
        raise RetryableError(f"{host} did not resolve to any address")
    if not allow_private and any(_blocked(ip) for ip in addresses):
        raise PermanentError(
            f"{host} resolves to a private or reserved address; refusing to call it"
        )
    ip = addresses[0]

    # Connect to the vetted IP; keep the original Host header and TLS server name.
    ip_text = f"[{ip}]" if ip.version == 6 else str(ip)
    pinned = urlunsplit((parts.scheme, f"{ip_text}:{port}", parts.path or "/", parts.query, ""))
    default_port = (parts.scheme == "https" and port == 443) or (
        parts.scheme == "http" and port == 80
    )
    headers = dict(payload.headers)
    headers["Host"] = host if default_port else f"{host}:{port}"
    headers.setdefault("User-Agent", f"QueueForge-Webhook/{__version__}")
    body = _encode_body(payload, headers)
    timestamp = int(time.time())
    secret = await _project_secret(ctx)
    headers["X-QueueForge-Job-Id"] = str(ctx.job_id)
    headers["X-QueueForge-Attempt"] = str(ctx.attempt)
    headers["Idempotency-Key"] = str(ctx.job_id)
    if secret:
        headers["X-QueueForge-Signature"] = sign(secret, timestamp, body)

    timeout = float(min(payload.timeout_seconds, max_timeout))
    extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False, verify=True
        ) as client:
            request = client.build_request(
                payload.method, pinned, headers=headers, content=body or None, extensions=extensions
            )
            response = await client.send(request, stream=True)
            chunks: list[bytes] = []
            size = 0
            truncated = False
            try:
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= MAX_RESPONSE_BYTES:
                        truncated = True
                        break
            finally:
                await response.aclose()
    except httpx.TimeoutException as exc:
        raise RetryableError(f"{payload.method} {host} timed out after {timeout:.0f}s") from exc
    except httpx.TransportError as exc:
        raise RetryableError(
            f"{payload.method} {host} failed: {type(exc).__name__}: {exc}"
        ) from exc

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    raw = b"".join(chunks)[:MAX_RESPONSE_BYTES]
    text = raw.decode(response.encoding or "utf-8", errors="replace")
    status = response.status_code
    ok = status in payload.success_status if payload.success_status else 200 <= status < 300
    ctx.logger.info("webhook_called", host=host, status=status, duration_ms=elapsed_ms)
    if not ok:
        detail = f"{payload.method} {host} returned HTTP {status}: {text[:300]}"
        if status in _RETRYABLE_STATUS or status >= 500:
            raise RetryableError(detail)
        if 300 <= status < 400:
            raise PermanentError(f"{detail} (redirects are not followed; use the final URL)")
        raise PermanentError(detail)
    return {
        "status_code": status,
        "method": payload.method,
        "url": payload.url,
        "duration_ms": elapsed_ms,
        "content_type": response.headers.get("content-type"),
        "response_body": text[:PREVIEW_CHARS],
        "response_truncated": truncated or len(text) > PREVIEW_CHARS,
        "signed": bool(secret),
    }


SPEC = register(
    HandlerSpec(
        name="http.request",
        title="Call a webhook",
        description=(
            "Calls a URL you own with a JSON body, retrying on network errors, timeouts, 429 and 5xx. "
            "Requests are signed so your endpoint can verify they came from QueueForge."
        ),
        side_effects=(
            "Whatever your endpoint does. Use the Idempotency-Key header (the job ID) to ignore "
            "repeated deliveries: execution is at-least-once."
        ),
        payload_model=WebhookPayload,
        run=call_webhook,
        default_timeout_seconds=60,
        submit_check=submit_check,
        example={
            "url": "https://example.com/hooks/nightly-sync",
            "method": "POST",
            "headers": {"Authorization": "Bearer <your token>"},
            "body": {"task": "sync-orders", "since": "2026-01-01"},
            "timeout_seconds": 15,
        },
    )
)
