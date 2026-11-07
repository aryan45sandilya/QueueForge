"""http.request: SSRF protection, signing, retry classification, end-to-end delivery."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from queueforge.api.app import create_app
from queueforge.config import Settings
from queueforge.db import Database
from queueforge.dispatch import Dispatcher
from queueforge.handlers import JobContext, PermanentError, RetryableError, webhook
from queueforge.handlers.webhook import WebhookPayload, call_webhook
from queueforge.logs import get_logger
from queueforge.services.accounts import webhook_secret
from tests.conftest import make_key, make_settings, signup, wait_for
from tests.helpers import make_project, running_worker, scalar


class Recorder(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Recorder.requests.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )
        routes = {
            "/ok": 200,
            "/created": 201,
            "/missing": 404,
            "/boom": 503,
            "/busy": 429,
            "/moved": 302,
        }
        if self.path == "/slow":
            time.sleep(3)
        status = routes.get(self.path, 200)
        payload = json.dumps({"received": True, "path": self.path}).encode()
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "http://example.com/")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


@pytest.fixture
def server() -> Iterator[str]:
    Recorder.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


def ctx(db: Database, project_id: uuid.UUID, settings: Settings, attempt: int = 1) -> JobContext:
    return JobContext(
        job_id=uuid.UUID(int=7),
        project_id=project_id,
        attempt=attempt,
        max_attempts=3,
        db=db,
        logger=get_logger("test"),
        settings=settings,
    )


LOCAL = make_settings(webhook_allow_private_networks=True)


# --- payload validation -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "ftp://example.com/file"},
        {"url": "https://user:pass@example.com/"},
        {"url": "https://example.com:99999/"},
        {"url": "https://example.com/", "headers": {"Host": "evil.example"}},
        {"url": "https://example.com/", "headers": {"X-QueueForge-Signature": "forged"}},
        {"url": "https://example.com/", "headers": {"X-Ok": "a\r\nInjected: 1"}},
        {"url": "https://example.com/", "timeout_seconds": 0},
        {"url": "https://example.com/", "success_status": [700]},
    ],
)
def test_payload_validation_rejects(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WebhookPayload.model_validate(payload)


# --- SSRF protection ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://192.168.0.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://metadata.google.internal/",
    ],
)
async def test_private_targets_are_rejected_at_submission(
    keyed: Any, project: dict[str, Any], url: str
) -> None:
    r = await keyed.post(
        f"/api/v1/projects/{project['id']}/jobs",
        json={"type": "http.request", "payload": {"url": url}},
    )
    assert r.status_code == 422, (url, r.text)
    assert "public address" in r.json()["error"]["message"]


async def test_https_can_be_required(db: Database, dispatcher: Dispatcher) -> None:
    app = create_app(make_settings(webhook_require_https=True), db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        api = await signup(c, "https@example.com")
        project = (
            await api.post("/api/v1/projects", json={"name": "H", "slug": "https-only"})
        ).json()
        url = f"/api/v1/projects/{project['id']}/jobs"
        plain = await api.post(
            url, json={"type": "http.request", "payload": {"url": "http://example.com/"}}
        )
        assert plain.status_code == 422 and "https" in plain.json()["error"]["message"]
        secure = await api.post(
            url, json={"type": "http.request", "payload": {"url": "https://example.com/"}}
        )
        assert secure.status_code == 201


async def test_dns_answers_pointing_inside_are_refused(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = await make_project(db)

    async def fake_resolve(host: str, port: int) -> list[Any]:
        return [ipaddress.ip_address("10.0.0.5")]

    monkeypatch.setattr(webhook, "_resolve", fake_resolve)
    payload = WebhookPayload(url="https://innocent.example.com/hook")
    with pytest.raises(PermanentError, match="private or reserved"):
        await call_webhook(payload, ctx(db, project.id, make_settings()))


async def test_localhost_is_refused_at_execution_too(db: Database, server: str) -> None:
    project = await make_project(db)
    with pytest.raises(PermanentError):
        await call_webhook(WebhookPayload(url=f"{server}/ok"), ctx(db, project.id, make_settings()))
    assert Recorder.requests == []


# --- delivery -----------------------------------------------------------------------------------


async def test_successful_call_is_signed_and_pinned(db: Database, server: str) -> None:
    project = await make_project(db)
    payload = WebhookPayload(
        url=f"{server}/ok?x=1", headers={"Authorization": "Bearer t0k"}, body={"order": 42}
    )
    result = await call_webhook(payload, ctx(db, project.id, LOCAL, attempt=2))
    assert result["status_code"] == 200 and result["signed"] is True
    assert json.loads(result["response_body"]) == {"received": True, "path": "/ok?x=1"}

    req = Recorder.requests[0]
    headers = {k.lower(): v for k, v in req["headers"].items()}
    assert req["method"] == "POST" and req["path"] == "/ok?x=1"
    assert json.loads(req["body"]) == {"order": 42}
    assert headers["host"] == server.removeprefix("http://")
    assert headers["authorization"] == "Bearer t0k"
    assert headers["idempotency-key"] == str(uuid.UUID(int=7))
    assert headers["x-queueforge-attempt"] == "2"
    t, v1 = (part.split("=", 1)[1] for part in headers["x-queueforge-signature"].split(","))
    secret = await webhook_secret(db, project.id)
    expected = hmac.new(secret.encode(), f"{t}.".encode() + req["body"], hashlib.sha256).hexdigest()
    assert hmac.compare_digest(v1, expected)


@pytest.mark.parametrize(
    ("path", "error", "fragment"),
    [
        ("/boom", RetryableError, "HTTP 503"),
        ("/busy", RetryableError, "HTTP 429"),
        ("/missing", PermanentError, "HTTP 404"),
        ("/moved", PermanentError, "redirects are not followed"),
    ],
)
async def test_status_codes_map_to_retry_policy(
    db: Database, server: str, path: str, error: type[Exception], fragment: str
) -> None:
    project = await make_project(db)
    with pytest.raises(error, match=fragment):
        await call_webhook(WebhookPayload(url=f"{server}{path}"), ctx(db, project.id, LOCAL))


async def test_custom_success_codes_and_timeouts(db: Database, server: str) -> None:
    project = await make_project(db)
    accepted = await call_webhook(
        WebhookPayload(url=f"{server}/missing", method="GET", success_status=[404]),
        ctx(db, project.id, LOCAL),
    )
    assert accepted["status_code"] == 404
    with pytest.raises(RetryableError, match="timed out"):
        await call_webhook(
            WebhookPayload(url=f"{server}/slow", timeout_seconds=1), ctx(db, project.id, LOCAL)
        )


async def test_end_to_end_through_a_worker(
    db: Database, dispatcher: Dispatcher, server: str
) -> None:
    settings = make_settings(webhook_allow_private_networks=True)
    app = create_app(settings, db=db, dispatcher=dispatcher)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        api = await signup(c, "hooks@example.com")
        project = (
            await api.post("/api/v1/projects", json={"name": "Hooks", "slug": "hooks"})
        ).json()
        secret = await make_key(api, project["id"])
        r = await c.post(
            f"/api/v1/projects/{project['id']}/jobs",
            headers={"Authorization": f"Bearer {secret}"},
            json={
                "type": "http.request",
                "payload": {"url": f"{server}/created", "body": {"hello": "world"}},
            },
        )
        assert r.status_code == 201, r.text
        job_id = r.json()["job"]["id"]
    async with running_worker(settings, db, dispatcher, concurrency=1):

        async def done() -> bool:
            return await scalar(db, "SELECT state FROM jobs WHERE id = :i", i=job_id) == "succeeded"

        await wait_for(done, timeout=20)
    assert (
        await scalar(db, "SELECT result->>'status_code' FROM jobs WHERE id = :i", i=job_id) == "201"
    )
    assert len(Recorder.requests) == 1 and json.loads(Recorder.requests[0]["body"]) == {
        "hello": "world"
    }
