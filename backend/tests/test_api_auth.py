"""Authentication, sessions, CSRF, rate limiting, error format."""

from __future__ import annotations

from typing import Any

import httpx

from queueforge.api.app import create_app
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from tests.conftest import PASSWORD, Api, avoid_rate_window_edge, make_settings, signup


async def test_register_login_me_logout(client: httpx.AsyncClient) -> None:
    api = await signup(client, "ada@example.com")
    me = await api.get("/api/v1/auth/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "ada@example.com" and body["projects"] == []
    assert "password_hash" not in me.text
    assert client.cookies.get("qf_session") and client.cookies.get("qf_csrf") == api.csrf

    out = await api.post("/api/v1/auth/logout")
    assert out.status_code == 204
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_wrong_password_and_unknown_user_look_identical(client: httpx.AsyncClient) -> None:
    await signup(client, "ada@example.com")
    client.cookies.clear()
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": "nope-nope-nope"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_login_is_case_insensitive_and_duplicate_email_conflicts(
    client: httpx.AsyncClient,
) -> None:
    await signup(client, "Ada@Example.com")
    r = await client.post(
        "/api/v1/auth/login", json={"email": "ada@example.com", "password": PASSWORD}
    )
    assert r.status_code == 200
    dup = await client.post(
        "/api/v1/auth/register",
        json={"email": "ADA@example.com", "password": PASSWORD, "display_name": "x"},
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "conflict"


async def test_registration_validation_errors_use_standard_shape(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/register", json={"email": "bad", "password": "Zq9!x", "display_name": ""}
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["request_id"]
    fields = {d["loc"][-1] for d in err["details"]}
    assert {"email", "password", "display_name"} <= fields
    assert "Zq9!x" not in r.text  # submitted values are not echoed back


async def test_cookie_requests_need_csrf_token(owner: Api) -> None:
    r = await owner.client.post("/api/v1/projects", json={"name": "No CSRF", "slug": "no-csrf"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf_failed"
    r = await owner.client.post(
        "/api/v1/projects", json={"name": "Bad", "slug": "bad"}, headers={"X-CSRF-Token": "forged"}
    )
    assert r.status_code == 403
    assert (
        await owner.post("/api/v1/projects", json={"name": "Ok", "slug": "ok-project"})
    ).status_code == 201


async def test_unauthenticated_and_malformed_bearer(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/projects")).status_code == 401
    r = await client.get("/api/v1/projects", headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status_code == 401
    r = await client.get(
        "/api/v1/projects", headers={"Authorization": "Bearer qf_nothexnothe_secret"}
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


async def test_login_rate_limit(db: Database, dispatcher: Dispatcher) -> None:
    await avoid_rate_window_edge()
    app = create_app(make_settings(login_rate_limit_per_minute=3), db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        codes = [
            (
                await c.post(
                    "/api/v1/auth/login",
                    json={"email": "x@example.com", "password": "wrong-password"},
                )
            ).status_code
            for _ in range(5)
        ]
        assert codes[:3] == [401, 401, 401]
        assert codes[3:] == [429, 429]
        limited = await c.post(
            "/api/v1/auth/login", json={"email": "x@example.com", "password": "wrong-password"}
        )
        assert limited.headers["Retry-After"] and limited.json()["error"]["code"] == "rate_limited"


async def test_api_rate_limit_per_principal(db: Database, dispatcher: Dispatcher) -> None:
    await avoid_rate_window_edge()
    app = create_app(make_settings(rate_limit_per_minute=5), db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        api = await signup(c, "rl@example.com")
        statuses = [(await api.get("/api/v1/auth/me")).status_code for _ in range(7)]
        assert statuses.count(429) == 2
        ok = await api.get("/api/v1/auth/me", headers={})
        assert ok.status_code == 429 and ok.headers["X-RateLimit-Remaining"] == "0"


async def test_registration_can_be_disabled(db: Database, dispatcher: Dispatcher) -> None:
    app = create_app(make_settings(allow_registration=False), db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/v1/auth/register",
            json={"email": "a@example.com", "password": PASSWORD, "display_name": "a"},
        )
        assert r.status_code == 403


async def test_security_headers_and_request_id(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "trace-123"
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_cors_allows_only_configured_origins(client: httpx.AsyncClient) -> None:
    allowed = await client.options(
        "/api/v1/projects",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"
    denied = await client.options(
        "/api/v1/projects",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


async def test_unknown_route_uses_error_shape(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/nope")
    assert r.status_code == 404
    body: dict[str, Any] = r.json()
    assert body["error"]["code"] == "not_found"
