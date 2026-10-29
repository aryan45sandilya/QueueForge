"""`queueforge` command line: migrate, api, worker, scheduler, seed, demo-jobs, check-config."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from queueforge.config import Settings, get_settings
from queueforge.db import Database, make_engine
from queueforge.dispatch import Dispatcher, make_redis
from queueforge.logs import configure_logging

BACKEND_DIR = Path(__file__).resolve().parent.parent


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    command.upgrade(cfg, "head")
    print("Database migrated to head.")


def cmd_api(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "queueforge.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=get_settings().trust_proxy_headers,
        log_config=None,
        timeout_graceful_shutdown=10,
    )


def cmd_worker(args: argparse.Namespace) -> None:
    from queueforge import worker

    argv: list[str] = []
    if args.concurrency:
        argv += ["--concurrency", str(args.concurrency)]
    if args.queues:
        argv += ["--queues", args.queues]
    worker.main(argv)


def cmd_scheduler(_: argparse.Namespace) -> None:
    from queueforge import scheduler

    scheduler.main()


DEMO_QUEUES: list[dict[str, Any]] = [
    {
        "name": "emails",
        "description": "Transactional and batch notifications",
        "concurrency_limit": 4,
    },
    {"name": "reports", "description": "Report generation, one at a time", "concurrency_limit": 1},
    {"name": "etl", "description": "CSV and data transformation jobs", "concurrency_limit": None},
]

SALES_CSV = (
    "order_id,region,product,units,unit_price,status\n"
    "1001,north,widget,12,2.50,paid\n1002,south,widget,7,2.50,paid\n1003,north,gadget,3,9.99,refunded\n"
    "1004,east,gizmo,20,1.25,paid\n1005,south,gadget,5,9.99,paid\n1006,east,widget,9,2.50,paid\n"
    "1007,north,gizmo,0,1.25,cancelled\n1008,west,gadget,4,9.99,paid\n1009,west,widget,15,2.50,paid\n"
    "1010,east,gadget,2,9.99,paid\n"
)


def demo_jobs() -> list[dict[str, Any]]:
    """Deterministic sample workload covering success, retry, bounce, permanent failure and timeout."""
    people = [
        ("ada", "Ada Lovelace"),
        ("grace", "Grace Hopper"),
        ("alan", "Alan Turing"),
        ("edsger", "Edsger Dijkstra"),
    ]
    return [
        {
            "type": "notification.send",
            "queue": "emails",
            "idempotency_key": "demo-welcome-batch",
            "payload": {
                "channel": "email",
                "template": "welcome",
                "subject": "Welcome to Acme, {{name}}",
                "body": "Hi {{name}}, your workspace {{workspace}} is ready.",
                "recipients": [
                    {
                        "address": f"{u}@example.com",
                        "name": n,
                        "variables": {"workspace": "acme-prod"},
                    }
                    for u, n in people
                ]
                + [
                    {
                        "address": "ops@bounce.test",
                        "name": "Ops",
                        "variables": {"workspace": "acme-prod"},
                    }
                ],
            },
        },
        {
            "type": "notification.send",
            "queue": "emails",
            "priority": 8,
            "idempotency_key": "demo-flaky-provider",
            "payload": {
                "channel": "email",
                "template": "invoice-ready",
                "subject": "Invoice {{invoice}}",
                "body": "Invoice {{invoice}} for {{amount}} is ready.",
                "recipients": [
                    {
                        "address": "billing@flaky.test",
                        "variables": {"invoice": "INV-2041", "amount": "$310.00"},
                    },
                    {
                        "address": "ada@example.com",
                        "variables": {"invoice": "INV-2042", "amount": "$120.00"},
                    },
                ],
            },
            "backoff_base_seconds": 2,
            "backoff_max_seconds": 10,
        },
        {
            "type": "notification.send",
            "queue": "emails",
            "idempotency_key": "demo-sms-missing-var",
            "payload": {
                "channel": "sms",
                "template": "otp",
                "body": "Your code is {{code}}",
                "recipients": [
                    {"address": "+14155550101", "variables": {"code": "481920"}},
                    {"address": "+14155550102"},
                ],
            },
        },
        {
            "type": "data.transform",
            "queue": "etl",
            "idempotency_key": "demo-revenue-by-region",
            "payload": {
                "csv": SALES_CSV,
                "steps": [
                    {"op": "filter", "column": "status", "operator": "eq", "value": "paid"},
                    {"op": "cast", "column": "units", "to": "int"},
                    {"op": "cast", "column": "unit_price", "to": "float"},
                    {
                        "op": "aggregate",
                        "group_by": ["region"],
                        "metrics": [
                            {"fn": "sum", "column": "units", "as": "units"},
                            {"fn": "count", "as": "orders"},
                            {"fn": "avg", "column": "unit_price", "as": "avg_price"},
                        ],
                    },
                    {"op": "sort", "column": "units", "descending": True},
                ],
            },
        },
        {
            "type": "data.transform",
            "queue": "etl",
            "idempotency_key": "demo-bad-column",
            "payload": {
                "csv": SALES_CSV,
                "steps": [{"op": "select", "columns": ["order_id", "customer_email"]}],
            },
        },
        {
            "type": "data.transform",
            "queue": "etl",
            "idempotency_key": "demo-export-csv",
            "payload": {
                "csv": SALES_CSV,
                "output_format": "csv",
                "steps": [
                    {"op": "dedupe", "columns": ["product"]},
                    {"op": "select", "columns": ["product", "unit_price"]},
                    {"op": "rename", "mapping": {"unit_price": "list_price"}},
                ],
            },
        },
        {
            "type": "report.generate",
            "queue": "reports",
            "idempotency_key": "demo-daily-report",
            "delay_seconds": 20,
            "payload": {"title": "Demo activity report", "period_hours": 24, "group_by": "type"},
        },
        {
            "type": "bench.synthetic",
            "queue": "default",
            "idempotency_key": "demo-retry-twice",
            "payload": {"work_ms": 300, "fail_attempts": 2, "label": "retries then succeeds"},
            "backoff_base_seconds": 2,
            "backoff_max_seconds": 8,
        },
        {
            "type": "bench.synthetic",
            "queue": "default",
            "idempotency_key": "demo-timeout",
            "payload": {"work_ms": 5000, "label": "exceeds timeout"},
            "timeout_seconds": 1,
            "max_attempts": 2,
            "backoff_base_seconds": 1,
            "backoff_max_seconds": 2,
        },
        {
            "type": "bench.synthetic",
            "queue": "default",
            "idempotency_key": "demo-permanent",
            "payload": {"permanent_failure": True, "label": "permanent failure"},
        },
        {
            "type": "bench.synthetic",
            "queue": "default",
            "idempotency_key": "demo-delayed",
            "delay_seconds": 120,
            "payload": {"work_ms": 100, "label": "scheduled two minutes out"},
        },
    ]


async def _seed(settings: Settings) -> None:
    from queueforge.models import Project, ProjectMember, Queue, Schedule, User
    from queueforge.schemas import ApiKeyCreate, ProjectCreate, QueueCreate, ScheduleCreate
    from queueforge.services import accounts, schedules
    from queueforge.services.common import Actor

    if settings.env == "production":
        sys.exit("Refusing to seed demo data with QF_ENV=production.")
    db = Database(make_engine(settings, pool_size=2))
    try:
        async with db.session() as session:
            user = (
                await session.execute(
                    select(User).where(func.lower(User.email) == settings.demo_email.lower())
                )
            ).scalar_one_or_none()
        if user is None:
            user = await accounts.register_user(
                db, settings.demo_email, settings.demo_password, "Demo Operator"
            )
            print(f"Created demo user {settings.demo_email} (password from QF_DEMO_PASSWORD).")
        else:
            print(f"Demo user {settings.demo_email} already exists.")
        actor = Actor(label=f"user:{user.email}", user_id=user.id)
        async with db.session() as session:
            project = (
                await session.execute(select(Project).where(Project.slug == "acme"))
            ).scalar_one_or_none()
        if project is None:
            out = await accounts.create_project(
                db, user, ProjectCreate(name="Acme Notifications", slug="acme")
            )
            project_id = out.id
            for q in DEMO_QUEUES:
                await accounts.create_queue(db, project_id, QueueCreate(**q), actor)
            await schedules.create_schedule(
                db,
                settings,
                project_id,
                ScheduleCreate(
                    name="hourly-activity-report",
                    job_type="report.generate",
                    queue="reports",
                    cron_expression="0 * * * *",
                    payload={"title": "Hourly activity", "period_hours": 1, "group_by": "queue"},
                ),
                actor,
            )
            await schedules.create_schedule(
                db,
                settings,
                project_id,
                ScheduleCreate(
                    name="heartbeat-probe",
                    job_type="bench.synthetic",
                    queue="default",
                    interval_seconds=300,
                    payload={"work_ms": 50, "label": "5-minute probe"},
                ),
                actor,
            )
            _, secret = await accounts.create_key(
                db,
                settings,
                project_id,
                ApiKeyCreate(
                    name="demo-cli",
                    scopes=[
                        "jobs:read",
                        "jobs:write",
                        "schedules:read",
                        "schedules:write",
                        "metrics:read",
                    ],
                ),
                actor,
            )
            print(f"Created project 'acme' ({project_id}) with queues, schedules and an API key.")
            print("\nDemo API key (shown once; store it now):\n")
            print(f"  QF_API_KEY={secret}")
            print(f"  QF_PROJECT_ID={project_id}\n")
        else:
            async with db.session() as session:
                member = (
                    await session.execute(
                        select(ProjectMember).where(
                            ProjectMember.project_id == project.id, ProjectMember.user_id == user.id
                        )
                    )
                ).scalar_one_or_none()
                queues = (
                    await session.execute(
                        select(func.count())
                        .select_from(Queue)
                        .where(Queue.project_id == project.id)
                    )
                ).scalar_one()
                sched = (
                    await session.execute(
                        select(func.count())
                        .select_from(Schedule)
                        .where(Schedule.project_id == project.id)
                    )
                ).scalar_one()
            print(
                f"Project 'acme' ({project.id}) already exists: {queues} queues, {sched} schedules, "
                f"demo user is {'a member' if member else 'NOT a member'}."
            )
    finally:
        await db.dispose()


def cmd_seed(_: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging("WARNING", settings.log_format)
    asyncio.run(_seed(settings))


async def _demo_jobs(settings: Settings, slug: str) -> None:
    from queueforge.models import Project
    from queueforge.schemas import JobCreate
    from queueforge.services.common import Actor
    from queueforge.services.jobs import submit_job

    db = Database(make_engine(settings, pool_size=2))
    dispatcher = Dispatcher(make_redis(settings.redis_url))
    try:
        async with db.session() as session:
            project = (
                await session.execute(select(Project).where(Project.slug == slug))
            ).scalar_one_or_none()
        if project is None:
            sys.exit(f"Project {slug!r} not found. Run `queueforge seed` first.")
        actor = Actor(label="cli:demo-jobs")
        for spec in demo_jobs():
            job_id, created = await submit_job(
                db, dispatcher, settings, project, JobCreate(**spec), actor
            )
            label = spec["payload"].get("label") or spec.get("idempotency_key")
            print(f"{'submitted' if created else 'exists   '} {job_id}  {spec['type']:<18} {label}")
    finally:
        await dispatcher.redis.aclose()
        await db.dispose()


def cmd_demo_jobs(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging("WARNING", settings.log_format)
    asyncio.run(_demo_jobs(settings, args.project))


async def _create_user(settings: Settings, email: str, name: str, password: str) -> None:
    from queueforge.services import accounts

    db = Database(make_engine(settings, pool_size=1))
    try:
        user = await accounts.register_user(db, email, password, name)
        print(f"Created user {user.email} ({user.id}).")
    finally:
        await db.dispose()


def cmd_create_user(args: argparse.Namespace) -> None:
    password = getpass.getpass("Password (min 10 chars): ")
    if len(password) < 10:
        sys.exit("Password must be at least 10 characters.")
    asyncio.run(_create_user(get_settings(), args.email, args.name, password))


async def _check(settings: Settings) -> int:
    from sqlalchemy import text

    db = Database(make_engine(settings, pool_size=1))
    dispatcher = Dispatcher(make_redis(settings.redis_url))
    code = 0
    try:
        try:
            async with db.session() as session:
                version = (await session.execute(text("SHOW server_version"))).scalar_one()
                revision = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one_or_none()
            print(f"postgres: ok (server {version}, migration {revision or 'none'})")
        except Exception as exc:
            print(f"postgres: FAILED ({type(exc).__name__}: {str(exc)[:200]})")
            code = 1
        print(f"redis:    {'ok' if await dispatcher.ping() else 'FAILED'}")
        if not await dispatcher.ping():
            code = 1
    finally:
        await dispatcher.redis.aclose()
        await db.dispose()
    return code


def cmd_check_config(_: argparse.Namespace) -> None:
    try:
        settings = get_settings()
    except Exception as exc:
        sys.exit(f"Configuration invalid:\n{exc}")
    shown = settings.model_dump()
    shown["secret_key"] = "[set]" if settings.secret_key else "[missing]"
    shown["demo_password"] = "[set]"  # noqa: S105 - masking, not a credential
    for url_key in ("database_url", "redis_url"):
        value = shown[url_key]
        if "@" in value:
            scheme, rest = value.split("://", 1)
            shown[url_key] = f"{scheme}://***@{rest.split('@', 1)[1]}"
    for key, value in sorted(shown.items()):
        print(f"{key:<28} {value}")
    sys.exit(asyncio.run(_check(settings)))


def cmd_openapi(args: argparse.Namespace) -> None:
    import json

    from queueforge.api.app import create_app

    spec = create_app().openapi()
    text = json.dumps(spec, indent=2, sort_keys=True) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"Wrote {args.out} ({len(spec['paths'])} paths)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="queueforge", description="QueueForge job platform")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply database migrations").set_defaults(fn=cmd_migrate)
    api = sub.add_parser("api", help="Run the HTTP API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")
    api.set_defaults(fn=cmd_api)
    worker = sub.add_parser("worker", help="Run a worker process")
    worker.add_argument("--concurrency", type=int)
    worker.add_argument("--queues")
    worker.set_defaults(fn=cmd_worker)
    sub.add_parser("scheduler", help="Run the scheduler / reconciler").set_defaults(
        fn=cmd_scheduler
    )
    sub.add_parser(
        "seed", help="Create demo user, project, queues, schedules and API key (dev only)"
    ).set_defaults(fn=cmd_seed)
    demo = sub.add_parser("demo-jobs", help="Submit the deterministic demo workload")
    demo.add_argument("--project", default="acme")
    demo.set_defaults(fn=cmd_demo_jobs)
    cu = sub.add_parser("create-user", help="Create a user (password read from the terminal)")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", required=True)
    cu.set_defaults(fn=cmd_create_user)
    sub.add_parser("check-config", help="Validate settings and connectivity").set_defaults(
        fn=cmd_check_config
    )
    oa = sub.add_parser(
        "openapi", help="Write the OpenAPI document used to generate frontend types"
    )
    oa.add_argument("--out", default=str(BACKEND_DIR / "openapi.json"))
    oa.set_defaults(fn=cmd_openapi)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
