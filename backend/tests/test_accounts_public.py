"""Public sign-up: email verification, password reset and change, account deletion."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from queueforge.api.app import create_app
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.services import email as email_service
from tests.conftest import PASSWORD, Api, make_settings
from tests.helpers import scalar

NEW_PASSWORD = "a brand new passphrase"


@pytest.fixture
async def public(db: Database, dispatcher: Dispatcher) -> AsyncIterator[httpx.AsyncClient]:
    """A client against an instance that requires email verification."""
    email_service.outbox.clear()
    app = create_app(
        make_settings(require_email_verification=True, public_url="https://qf.example"),
        db=db,
        dispatcher=dispatcher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def last_token(kind: str) -> str:
    assert email_service.outbox, "no email was sent"
    message = email_service.outbox[-1]
    match = re.search(rf"https://qf\.example/{kind}\?token=([\w-]+)", message.text)
    assert match, message.text
    return match.group(1)


async def register(c: httpx.AsyncClient, email: str = "ada@example.com") -> dict[str, Any]:
    r = await c.post(
        "/api/v1/auth/register", json={"email": email, "password": PASSWORD, "display_name": "Ada"}
    )
    assert r.status_code == 201, r.text
    return dict(r.json())


async def login(
    c: httpx.AsyncClient, email: str = "ada@example.com", password: str = PASSWORD
) -> httpx.Response:
    return await c.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_sign_up_requires_email_verification(public: httpx.AsyncClient) -> None:
    body = await register(public)
    assert body["verification_required"] is True and body["email_verified_at"] is None
    assert email_service.outbox[-1].to == "ada@example.com"

    blocked = await login(public)
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "email_not_verified"
    # Wrong password still says "invalid", so the verification state isn't revealed to strangers.
    wrong = await login(public, password="not the password")
    assert wrong.status_code == 401

    verified = await public.post(
        "/api/v1/auth/verify-email", json={"token": last_token("verify-email")}
    )
    assert verified.status_code == 200 and verified.json()["email_verified_at"]
    assert (await login(public)).status_code == 200


async def test_verification_tokens_are_single_use_and_expire(
    public: httpx.AsyncClient, db: Database
) -> None:
    await register(public)
    token = last_token("verify-email")
    assert (
        await public.post("/api/v1/auth/verify-email", json={"token": token})
    ).status_code == 200
    reused = await public.post("/api/v1/auth/verify-email", json={"token": token})
    assert reused.status_code == 400 and reused.json()["error"]["code"] == "invalid_token"
    forged = await public.post("/api/v1/auth/verify-email", json={"token": "x" * 43})
    assert forged.status_code == 400

    await register(public, "grace@example.com")
    async with db.transaction() as session:
        await session.execute(
            text(
                "UPDATE email_tokens SET expires_at = now() - interval '1 second' WHERE used_at IS NULL"
            )
        )
    expired = await public.post(
        "/api/v1/auth/verify-email", json={"token": last_token("verify-email")}
    )
    assert expired.status_code == 400


async def test_resend_verification_does_not_reveal_accounts(public: httpx.AsyncClient) -> None:
    await register(public)
    first = last_token("verify-email")
    unknown = await public.post(
        "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
    )
    known = await public.post("/api/v1/auth/resend-verification", json={"email": "ADA@example.com"})
    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    second = last_token("verify-email")
    assert second != first
    # Issuing a new link invalidates the old one.
    assert (
        await public.post("/api/v1/auth/verify-email", json={"token": first})
    ).status_code == 400
    assert (
        await public.post("/api/v1/auth/verify-email", json={"token": second})
    ).status_code == 200


async def test_password_reset_flow(
    public: httpx.AsyncClient, db: Database, dispatcher: Dispatcher
) -> None:
    await register(public)
    await public.post("/api/v1/auth/verify-email", json={"token": last_token("verify-email")})
    assert (await login(public)).status_code == 200
    assert (await public.get("/api/v1/auth/me")).status_code == 200

    sent_before = len(email_service.outbox)
    unknown = await public.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"})
    assert unknown.status_code == 202 and len(email_service.outbox) == sent_before
    known = await public.post("/api/v1/auth/forgot-password", json={"email": "ada@example.com"})
    assert known.status_code == 202 and known.json() == unknown.json()
    token = last_token("reset-password")

    short = await public.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "short"}
    )
    assert short.status_code == 422
    ok = await public.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )
    assert ok.status_code == 200
    # Every existing session was ended.
    assert (await public.get("/api/v1/auth/me")).status_code == 401
    assert (await login(public)).status_code == 401
    assert (await login(public, password=NEW_PASSWORD)).status_code == 200
    again = await public.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "another passphrase"}
    )
    assert again.status_code == 400


async def test_change_password_keeps_this_session_only(app: Any, owner: Api, db: Database) -> None:
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    try:
        r = await other.post(
            "/api/v1/auth/login", json={"email": "owner@example.com", "password": PASSWORD}
        )
        assert r.status_code == 200
        wrong = await owner.post(
            "/api/v1/auth/change-password",
            json={"current_password": "nope-nope-nope", "new_password": NEW_PASSWORD},
        )
        assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "wrong_password"
        same = await owner.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": PASSWORD},
        )
        assert same.status_code == 422
        ok = await owner.post(
            "/api/v1/auth/change-password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert ok.status_code == 204
        assert (await owner.get("/api/v1/auth/me")).status_code == 200
        assert (await other.get("/api/v1/auth/me")).status_code == 401
    finally:
        await other.aclose()


async def test_delete_account_removes_sole_owned_projects(
    app: Any, owner: Api, project: dict[str, Any], keyed: Api, db: Database
) -> None:
    pid = project["id"]
    job = await keyed.post(
        f"/api/v1/projects/{pid}/jobs", json={"type": "bench.synthetic", "payload": {}}
    )
    assert job.status_code == 201
    await owner.post(
        f"/api/v1/projects/{pid}/schedules",
        json={
            "name": "nightly",
            "job_type": "report.generate",
            "cron_expression": "0 3 * * *",
            "payload": {},
        },
    )
    shared = (
        await owner.post("/api/v1/projects", json={"name": "Shared", "slug": "shared"})
    ).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        await c.post(
            "/api/v1/auth/register",
            json={"email": "co@example.com", "password": PASSWORD, "display_name": "Co"},
        )
    async with db.transaction() as session:
        co = await session.execute(text("SELECT id FROM users WHERE email = 'co@example.com'"))
        await session.execute(
            text(
                "INSERT INTO project_members (project_id, user_id, role) VALUES (:p, :u, 'owner')"
            ),
            {"p": shared["id"], "u": co.scalar_one()},
        )

    wrong = await owner.post("/api/v1/auth/delete-account", json={"password": "nope-nope-nope"})
    assert wrong.status_code == 401
    r = await owner.post("/api/v1/auth/delete-account", json={"password": PASSWORD})
    assert r.status_code == 200 and r.json() == {"projects_deleted": 1}
    assert (await owner.get("/api/v1/auth/me")).status_code == 401
    assert await scalar(db, "SELECT count(*) FROM users WHERE email = 'owner@example.com'") == 0
    assert await scalar(db, "SELECT count(*) FROM projects WHERE id = :p", p=pid) == 0
    assert await scalar(db, "SELECT count(*) FROM jobs WHERE project_id = :p", p=pid) == 0
    assert await scalar(db, "SELECT count(*) FROM schedules WHERE project_id = :p", p=pid) == 0
    assert await scalar(db, "SELECT count(*) FROM projects WHERE id = :p", p=shared["id"]) == 1
    # The deleted project's API key no longer works.
    assert (await keyed.get(f"/api/v1/projects/{pid}/jobs")).status_code == 401


async def test_open_instance_needs_no_verification(client: httpx.AsyncClient) -> None:
    email_service.outbox.clear()
    body = await register(client)
    assert body["verification_required"] is False and body["email_verified_at"]
    assert email_service.outbox == []
    assert (await login(client)).status_code == 200


async def test_instance_info_is_public(db: Database, dispatcher: Dispatcher) -> None:
    app = create_app(
        make_settings(allow_registration=False, max_projects_per_user=3),
        db=db,
        dispatcher=dispatcher,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        info = (await c.get("/api/v1/instance")).json()
    assert info["registration_open"] is False
    assert info["limits"]["max_projects_per_user"] == 3
