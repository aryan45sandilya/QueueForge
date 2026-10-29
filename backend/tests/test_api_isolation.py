"""Authorization: project isolation, key scopes, revocation, rotation, roles."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from sqlalchemy import text

from queueforge.db import Database
from tests.conftest import Api, make_key, signup


async def other_tenant(app: Any) -> tuple[Api, dict[str, Any], httpx.AsyncClient]:
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    api = await signup(c, "mallory@example.com")
    r = await api.post("/api/v1/projects", json={"name": "Other", "slug": "other"})
    assert r.status_code == 201
    return api, r.json(), c


async def test_users_cannot_see_other_projects_or_their_jobs(
    app: Any, owner: Api, project: dict[str, Any]
) -> None:
    pid = project["id"]
    job = await owner.post(
        f"/api/v1/projects/{pid}/jobs", json={"type": "bench.synthetic", "payload": {}}
    )
    assert job.status_code == 201
    job_id = job.json()["job"]["id"]

    mallory, theirs, c = await other_tenant(app)
    try:
        for path in (
            f"/api/v1/projects/{pid}",
            f"/api/v1/projects/{pid}/jobs",
            f"/api/v1/projects/{pid}/jobs/{job_id}",
            f"/api/v1/projects/{pid}/metrics",
            f"/api/v1/projects/{pid}/queues",
        ):
            r = await mallory.get(path)
            assert r.status_code == 404, path
        r = await mallory.post(f"/api/v1/projects/{pid}/jobs/{job_id}/cancel")
        assert r.status_code == 404
        # The job ID is not reachable through Mallory's own project either.
        r = await mallory.get(f"/api/v1/projects/{theirs['id']}/jobs/{job_id}")
        assert r.status_code == 404
        listing = await mallory.get("/api/v1/projects")
        assert [p["slug"] for p in listing.json()] == ["other"]
    finally:
        await c.aclose()


async def test_api_key_is_bound_to_its_project(
    app: Any, owner: Api, project: dict[str, Any], keyed: Api
) -> None:
    _, theirs, c = await other_tenant(app)
    try:
        r = await keyed.get(f"/api/v1/projects/{theirs['id']}/jobs")
        assert r.status_code == 404
        r = await keyed.post(
            f"/api/v1/projects/{theirs['id']}/jobs", json={"type": "bench.synthetic", "payload": {}}
        )
        assert r.status_code == 404
        assert (await keyed.get(f"/api/v1/projects/{project['id']}/jobs")).status_code == 200
    finally:
        await c.aclose()


async def test_api_keys_cannot_manage_keys_settings_or_use_session_routes(
    keyed: Api, project: dict[str, Any]
) -> None:
    pid = project["id"]
    assert (await keyed.get(f"/api/v1/projects/{pid}/api-keys")).status_code == 403
    assert (
        await keyed.patch(f"/api/v1/projects/{pid}", json={"retention_days": 1})
    ).status_code == 403
    assert (await keyed.get("/api/v1/auth/me")).status_code == 403


async def test_scopes_are_enforced(app: Any, owner: Api, project: dict[str, Any]) -> None:
    pid = project["id"]
    secret = await make_key(owner, pid, scopes=["jobs:read"], name="read-only")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        headers = {"Authorization": f"Bearer {secret}"}
        assert (await c.get(f"/api/v1/projects/{pid}/jobs", headers=headers)).status_code == 200
        r = await c.post(
            f"/api/v1/projects/{pid}/jobs",
            json={"type": "bench.synthetic", "payload": {}},
            headers=headers,
        )
        assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_scope"
        assert (await c.get(f"/api/v1/projects/{pid}/metrics", headers=headers)).status_code == 403


async def test_secret_shown_once_and_never_listed(
    owner: Api, project: dict[str, Any], db: Database
) -> None:
    pid = project["id"]
    created = await owner.post(f"/api/v1/projects/{pid}/api-keys", json={"name": "deploy"})
    secret = created.json()["secret"]
    listing = await owner.get(f"/api/v1/projects/{pid}/api-keys")
    assert secret not in listing.text
    assert secret.split("_", 2)[2] not in listing.text
    async with db.session() as session:
        stored = (await session.execute(text("SELECT secret_hash FROM api_keys"))).scalars().all()
    assert all(secret.encode() not in bytes(h) for h in stored)


async def test_revoked_and_rotated_keys(
    app: Any, owner: Api, project: dict[str, Any], db: Database
) -> None:
    pid = project["id"]
    created = (await owner.post(f"/api/v1/projects/{pid}/api-keys", json={"name": "svc"})).json()
    old_secret, old_id = created["secret"], created["key"]["id"]

    rotated = await owner.post(
        f"/api/v1/projects/{pid}/api-keys/{old_id}/rotate", json={"grace_period_hours": 1}
    )
    assert rotated.status_code == 201
    new_secret = rotated.json()["secret"]
    assert rotated.json()["key"]["rotated_from_id"] == old_id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:

        async def status(secret: str) -> int:
            r = await c.get(
                f"/api/v1/projects/{pid}/jobs", headers={"Authorization": f"Bearer {secret}"}
            )
            return r.status_code

        assert await status(old_secret) == 200  # inside the grace period
        assert await status(new_secret) == 200
        async with db.transaction() as session:
            await session.execute(
                text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"),
                {"i": old_id},
            )
        assert await status(old_secret) == 401

        new_id = rotated.json()["key"]["id"]
        assert (
            await owner.post(f"/api/v1/projects/{pid}/api-keys/{new_id}/revoke")
        ).status_code == 200
        assert await status(new_secret) == 401
        # Tampering with the secret part fails even with a valid prefix.
        prefix = new_secret.split("_")[1]
        assert await status(f"qf_{prefix}_{'A' * 43}") == 401


async def test_members_cannot_use_owner_endpoints(
    app: Any, owner: Api, project: dict[str, Any], db: Database
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        member = await signup(c, "member@example.com")
        async with db.transaction() as session:
            uid = (
                await session.execute(
                    text("SELECT id FROM users WHERE email = 'member@example.com'")
                )
            ).scalar_one()
            await session.execute(
                text(
                    "INSERT INTO project_members (project_id, user_id, role) VALUES (:p, :u, 'member')"
                ),
                {"p": project["id"], "u": uid},
            )
        pid = project["id"]
        assert (await member.get(f"/api/v1/projects/{pid}/jobs")).status_code == 200
        assert (
            await member.post(
                f"/api/v1/projects/{pid}/jobs", json={"type": "bench.synthetic", "payload": {}}
            )
        ).status_code == 201
        assert (await member.get(f"/api/v1/projects/{pid}/api-keys")).status_code == 403
        assert (
            await member.patch(f"/api/v1/projects/{pid}", json={"retention_days": 3})
        ).status_code == 403
        assert (
            await member.post(f"/api/v1/projects/{pid}/queues", json={"name": "x"})
        ).status_code == 403


async def test_random_project_id_is_404_not_403(owner: Api) -> None:
    r = await owner.get(f"/api/v1/projects/{uuid.uuid4()}/jobs")
    assert r.status_code == 404
