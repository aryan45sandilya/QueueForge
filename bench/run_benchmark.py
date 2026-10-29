"""Reproducible QueueForge benchmark.

For each configuration this script:

1. creates an isolated project, queue and API key in the configured database;
2. starts its own API server, scheduler and N worker processes (separate OS processes);
3. phase 1 (submission): submits a controlled workload over HTTP into a PAUSED queue
   (`bench.synthetic` jobs: fixed sleep; a fixed, seeded fraction fails once and is retried),
   which measures API submission throughput on its own;
4. phase 2 (drain): unpauses the queue and waits until every job is terminal, which measures
   worker throughput against a full backlog independently of how fast the API accepts jobs;
5. computes metrics from persisted rows (drain throughput, wait and execution percentiles,
   failure and retry rates);
6. writes machine-readable JSON to bench/results/ and prints a comparison table.

The numbers are SYNTHETIC: handlers sleep instead of doing real work, everything runs on one
machine, and PostgreSQL/Redis share its resources. They show relative behaviour of
configurations on this hardware, not production capacity.

Usage (from the repository root, with QF_DATABASE_URL / QF_REDIS_URL pointing at a database
you can write to):

    python bench/run_benchmark.py --jobs 2000 --work-ms 20 --configs 1x4,1x16,2x16
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import socket
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
RESULTS = ROOT / "bench" / "results"
sys.path.insert(0, str(BACKEND))

from queueforge.config import get_settings  # noqa: E402
from queueforge.db import Database, make_engine  # noqa: E402
from queueforge.schemas import (  # noqa: E402
    ApiKeyCreate,
    ProjectCreate,
    ProjectSettingsUpdate,
    QueueCreate,
    QueueUpdate,
)
from queueforge.services import accounts  # noqa: E402
from queueforge.services.common import Actor  # noqa: E402


@dataclass
class Config:
    workers: int
    concurrency: int

    @property
    def label(self) -> str:
        return f"{self.workers}x{self.concurrency}"

    @classmethod
    def parse(cls, raw: str) -> Config:
        workers, concurrency = raw.lower().split("x")
        return cls(int(workers), int(concurrency))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("QF_LOG_LEVEL", "WARNING")
    env["QF_LOG_LEVEL"] = "WARNING"
    env["QF_SCHEDULER_TICK_SECONDS"] = "0.25"
    env["QF_QUEUE_REFRESH_SECONDS"] = "0.5"
    env["QF_RATE_LIMIT_PER_MINUTE"] = "1000000"
    return env


def spawn(args: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed argv built by this script
        [sys.executable, "-m", *args],
        cwd=BACKEND,
        env=child_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop(processes: list[subprocess.Popen[bytes]]) -> None:
    for p in processes:
        if p.poll() is None:
            p.terminate()
    for p in processes:
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()


async def setup_project(db: Database, settings: Any, run_id: str) -> tuple[str, str, str]:
    email = f"bench-{run_id}@queueforge.bench"
    user = await accounts.register_user(db, email, uuid.uuid4().hex, "Benchmark")
    project = await accounts.create_project(
        db, user, ProjectCreate(name=f"Benchmark {run_id}", slug=f"bench-{run_id}")
    )
    actor = Actor(label="bench", user_id=user.id)
    queue_name = f"bench-{run_id}"
    queue = await accounts.create_queue(
        db, project.id, QueueCreate(name=queue_name, description="benchmark"), actor
    )
    await accounts.update_queue(db, project.id, queue.id, QueueUpdate(paused=True), actor)
    await accounts.update_project(
        db, project.id, ProjectSettingsUpdate(max_queued_jobs=10_000_000), actor
    )
    _, secret = await accounts.create_key(
        db,
        settings,
        project.id,
        ApiKeyCreate(name="bench", scopes=["jobs:write", "jobs:read"]),
        actor,
    )
    return str(project.id), queue_name, secret


async def wait_ready(base: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                if (await client.get(f"{base}/readyz")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError("API did not become ready")


async def wait_workers(db: Database, queue: str, expected: int, timeout: float = 60) -> None:
    # Match on the run's unique queue filter: on Windows a venv python.exe is a launcher, so the
    # Popen PID is not the interpreter's PID.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session() as s:
            n = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM workers WHERE :q = ANY(queues) AND state = 'running'"
                    ),
                    {"q": queue},
                )
            ).scalar_one()
        if n == expected:
            return
        await asyncio.sleep(0.25)
    raise RuntimeError("workers did not register")


async def submit_all(
    base: str,
    project_id: str,
    queue: str,
    key: str,
    jobs: int,
    work_ms: int,
    fail_ratio: float,
    clients: int,
    seed: int,
) -> tuple[float, int]:
    rng = random.Random(seed)
    plan = [1 if rng.random() < fail_ratio else 0 for _ in range(jobs)]
    url = f"{base}/api/v1/projects/{project_id}/jobs"
    headers = {"Authorization": f"Bearer {key}"}
    errors = 0
    index = 0
    lock = asyncio.Lock()

    async def client_loop(client: httpx.AsyncClient) -> None:
        nonlocal index, errors
        while True:
            async with lock:
                if index >= jobs:
                    return
                i = index
                index += 1
            body = {
                "type": "bench.synthetic",
                "queue": queue,
                "priority": 5,
                "payload": {"work_ms": work_ms, "fail_attempts": plan[i], "label": f"b{i}"},
                "max_attempts": 3,
                "backoff_base_seconds": 1,
                "backoff_max_seconds": 1,
            }
            r = await client.post(url, json=body, headers=headers)
            if r.status_code != 201:
                errors += 1

    started = time.perf_counter()
    limits = httpx.Limits(max_connections=clients, max_keepalive_connections=clients)
    async with httpx.AsyncClient(limits=limits, timeout=30) as client:
        await asyncio.gather(*[client_loop(client) for _ in range(clients)])
    return time.perf_counter() - started, errors


async def wait_terminal(db: Database, project_id: str, jobs: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with db.session() as s:
            done = (
                await s.execute(
                    text(
                        "SELECT count(*) FROM jobs WHERE project_id = :p AND state IN ('succeeded','failed','cancelled')"
                    ),
                    {"p": project_id},
                )
            ).scalar_one()
        if done >= jobs:
            return
        await asyncio.sleep(0.5)
    raise RuntimeError(f"timed out waiting for jobs ({done}/{jobs} terminal)")


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 2)


async def unpause(db: Database, project_id: str, queue: str) -> datetime:
    async with db.transaction() as s:
        await s.execute(
            text("UPDATE queues SET paused = false WHERE project_id = :p AND name = :q"),
            {"p": project_id, "q": queue},
        )
        started: datetime = (await s.execute(text("SELECT now()"))).scalar_one()
    return started


async def collect(db: Database, project_id: str, drain_started: datetime) -> dict[str, Any]:
    async with db.session() as s:
        jobs = (
            await s.execute(
                text(
                    "SELECT state, attempts, created_at, finished_at FROM jobs WHERE project_id = :p"
                ),
                {"p": project_id},
            )
        ).all()
        attempts = (
            await s.execute(
                text(
                    "SELECT attempt_number, outcome, wait_ms, duration_ms FROM job_attempts WHERE project_id = :p"
                ),
                {"p": project_id},
            )
        ).all()
    last_finished = max(j.finished_at for j in jobs if j.finished_at)
    span = (last_finished - drain_started).total_seconds()
    succeeded = sum(1 for j in jobs if j.state == "succeeded")
    failed = sum(1 for j in jobs if j.state == "failed")
    waits = [float(a.wait_ms) for a in attempts if a.wait_ms is not None]
    durations = [
        float(a.duration_ms)
        for a in attempts
        if a.duration_ms is not None and a.outcome in ("succeeded", "failed")
    ]
    first_waits = [
        float(a.wait_ms) for a in attempts if a.wait_ms is not None and a.attempt_number == 1
    ]
    return {
        "jobs": len(jobs),
        "succeeded": succeeded,
        "failed": failed,
        "attempts": len(attempts),
        "retried_jobs": sum(1 for j in jobs if j.attempts > 1),
        "retry_rate": round(sum(1 for j in jobs if j.attempts > 1) / len(jobs), 4),
        "failure_rate": round(failed / len(jobs), 4),
        "drain_seconds": round(span, 3),
        "completed_per_second": round((succeeded + failed) / span, 2) if span else None,
        "wait_ms": {
            "p50": pct(waits, 0.5),
            "p95": pct(waits, 0.95),
            "p99": pct(waits, 0.99),
            "max": max(waits) if waits else None,
            "mean": round(statistics.fmean(waits), 2) if waits else None,
        },
        "first_attempt_wait_ms": {
            "p50": pct(first_waits, 0.5),
            "p95": pct(first_waits, 0.95),
            "p99": pct(first_waits, 0.99),
        },
        "execution_ms": {
            "p50": pct(durations, 0.5),
            "p95": pct(durations, 0.95),
            "p99": pct(durations, 0.99),
            "max": max(durations) if durations else None,
        },
    }


async def environment(db: Database) -> dict[str, Any]:
    from queueforge.dispatch import make_redis

    async with db.session() as s:
        pg = (await s.execute(text("SHOW server_version"))).scalar_one()
    redis = make_redis(get_settings().redis_url)
    try:
        info = await redis.info("server")
        redis_version = info.get("redis_version")
    finally:
        await redis.aclose()
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "memory_gb": round(psutil.virtual_memory().total / 1024**3, 1),
        "python": platform.python_version(),
        "postgres": pg,
        "redis": redis_version,
        "note": "API, scheduler, workers, PostgreSQL and Redis all ran on this single machine.",
    }


async def run_config(cfg: Config, args: argparse.Namespace, env: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    db = Database(make_engine(settings, pool_size=4))
    run_id = f"{datetime.now(UTC):%m%d%H%M%S}{uuid.uuid4().hex[:4]}"
    processes: list[subprocess.Popen[bytes]] = []
    try:
        project_id, queue, key = await setup_project(db, settings, run_id)
        port = free_port()
        processes.append(
            spawn(
                [
                    "uvicorn",
                    "queueforge.api.app:create_app",
                    "--factory",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                    "--no-access-log",
                ]
            )
        )
        processes.append(spawn(["queueforge.scheduler"]))
        workers = [
            spawn(["queueforge.worker", "--concurrency", str(cfg.concurrency), "--queues", queue])
            for _ in range(cfg.workers)
        ]
        processes.extend(workers)
        base = f"http://127.0.0.1:{port}"
        await wait_ready(base)
        await wait_workers(db, queue, len(workers))
        print(
            f"[{cfg.label}] submitting {args.jobs} jobs ({args.work_ms} ms each, {args.fail_ratio:.0%} fail once)…",
            flush=True,
        )
        submit_seconds, submit_errors = await submit_all(
            base,
            project_id,
            queue,
            key,
            args.jobs,
            args.work_ms,
            args.fail_ratio,
            args.clients,
            args.seed,
        )
        print(f"[{cfg.label}] draining backlog…", flush=True)
        drain_started = await unpause(db, project_id, queue)
        await wait_terminal(db, project_id, args.jobs, timeout=args.timeout)
        metrics = await collect(db, project_id, drain_started)
    finally:
        stop(processes)
        await db.dispose()
    result = {
        "kind": "synthetic-benchmark",
        "config": {"label": cfg.label, **asdict(cfg), "total_slots": cfg.workers * cfg.concurrency},
        "workload": {
            "jobs": args.jobs,
            "work_ms": args.work_ms,
            "fail_ratio": args.fail_ratio,
            "submit_clients": args.clients,
            "seed": args.seed,
            "max_attempts": 3,
            "backoff_seconds": 1,
        },
        "method": "phase 1 submits into a paused queue (API throughput); phase 2 unpauses and drains (worker throughput). "
        "Wait times therefore include time spent in the backlog.",
        "submission": {
            "seconds": round(submit_seconds, 3),
            "jobs_per_second": round(args.jobs / submit_seconds, 2),
            "errors": submit_errors,
        },
        "results": metrics,
        "environment": env,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    print(
        f"[{cfg.label}] done: {metrics['completed_per_second']} completed/s over {metrics['drain_seconds']} s",
        flush=True,
    )
    return result


def table(results: list[dict[str, Any]]) -> str:
    head = (
        "| config | slots | API submit jobs/s | drain completed jobs/s | drain s | backlog wait p50 / p95 / p99 ms "
        "| exec p50 / p95 ms | retry rate | failure rate |"
    )
    lines = [head, "|" + "---|" * 9]
    for r in results:
        m = r["results"]
        w, e = m["wait_ms"], m["execution_ms"]
        lines.append(
            f"| {r['config']['label']} | {r['config']['total_slots']} | {r['submission']['jobs_per_second']} | "
            f"{m['completed_per_second']} | {m['drain_seconds']} | {w['p50']} / {w['p95']} / {w['p99']} | "
            f"{e['p50']} / {e['p95']} | {m['retry_rate']:.1%} | {m['failure_rate']:.1%} |"
        )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--jobs", type=int, default=2000)
    parser.add_argument("--work-ms", type=int, default=20)
    parser.add_argument("--fail-ratio", type=float, default=0.05)
    parser.add_argument("--clients", type=int, default=16, help="concurrent HTTP submitters")
    parser.add_argument(
        "--configs", default="1x4,1x16,2x16", help="comma-separated WORKERSxCONCURRENCY"
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()

    configs = [Config.parse(c) for c in args.configs.split(",")]
    settings = get_settings()
    probe = Database(make_engine(settings, pool_size=1))
    try:
        env = await environment(probe)
    finally:
        await probe.dispose()

    results = [await run_config(cfg, args, env) for cfg in configs]
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = RESULTS / f"{stamp}.json"
    out.write_text(json.dumps({"runs": results}, indent=2) + "\n", encoding="utf-8")
    summary = table(results)
    (RESULTS / "latest.md").write_text(
        f"# Benchmark {stamp} (synthetic)\n\n{summary}\n\nEnvironment: {json.dumps(env)}\n",
        encoding="utf-8",
    )
    print("\n" + summary)
    print(f"\nWrote {out.relative_to(ROOT)} and bench/results/latest.md")


if __name__ == "__main__":
    asyncio.run(main())
