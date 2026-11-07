<div align="center">

# QueueForge

**Self-hosted background jobs you can actually see.**

Submit work over a REST API, let workers run it, and watch every attempt, retry and failure in a live dashboard.

[![CI](https://github.com/aryan45sandilya/QueueForge/actions/workflows/ci.yml/badge.svg)](https://github.com/aryan45sandilya/QueueForge/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.12+-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-000000?logo=nextdotjs&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169e1?logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-dc382d?logo=redis&logoColor=white)

[Screenshots](#screenshots) · [Architecture](#architecture) · [Quick start](#quick-start-docker) · [API](#api) · [Tests](#tests) · [Benchmarks](#benchmarks)

<img src="docs/screenshots/overview.png" alt="QueueForge overview: job counts, failure rate, latency and throughput charts, queue health and failure groups" width="900">

</div>

---

## Contents

- [Features](#features) · [Project status](#project-status) · [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Quick start (Docker)](#quick-start-docker) · [Local setup](#local-setup-without-docker) · [PostgreSQL and Redis](#postgresql-and-redis)
- [Workers and scheduler](#workers-and-scheduler) · [Retries and failure recovery](#retries-and-failure-recovery)
- [API](#api) · [Tests](#tests) · [Benchmarks](#benchmarks)
- [Deploying](#deploying) · [Limitations](#limitations) · [Further reading](#further-reading)

## Features

| | |
|---|---|
| **Reliable** | Retries with backoff and jitter, timeouts, priorities, idempotency keys, lease-based crash recovery, a dead-letter view grouped by error |
| **Scheduled** | Delayed jobs, plus recurring jobs by cron or interval with overlap skipping and *run now* |
| **Observable** | Live dashboard over SSE: throughput, failure rate, wait and run-time percentiles, queue depth, worker health |
| **Safe** | Jobs name a registered handler with a validated payload; submitted code is never executed. Projects are isolated |
| **Multi-user** | Optional open sign-up with email verification, password reset and per-account limits |
| **Webhooks** | `http.request` calls your URL with an HMAC signature and `Idempotency-Key`; private networks are blocked |

Built-in handlers: `http.request`, `notification.send`, `data.transform` (CSV pipeline), `report.generate` and `bench.synthetic` (demos and benchmarks).

## Project status

Only what has actually been run is marked done.

| Item | Status | Evidence |
|---|---|---|
| Backend: API, workers, scheduler | ✅ Verified | 168 backend tests pass locally and in CI |
| Dashboard | ✅ Verified | 27 frontend tests, lint, typecheck and production build in CI; screenshots in this README are from a local run |
| Docker Compose stack | ✅ Verified | CI builds the images, starts the stack and checks `/readyz` and sign-in |
| Benchmarks | ✅ Recorded | One run on a laptop ([results](#benchmarks)); not a production measurement |
| Email delivery over SMTP | ⚠️ Partly | Verification and reset emails are checked through an in-memory outbox; the SMTP sending code has not been run against a real mail server |
| Deployed public demo | ❌ None yet | Runs locally only. See [Deploying](#deploying) |

## Screenshots

From a local run with the demo data (`queueforge seed` and `queueforge demo-jobs`). The machine hostname in worker IDs is replaced with `worker-host`.

| Jobs | Job detail |
|---|---|
| <img src="docs/screenshots/jobs.png" alt="Jobs list with state filters and per-job attempts and duration" width="440"> | <img src="docs/screenshots/job-detail.png" alt="A job's attempts, timeline and payload" width="440"> |
| **Failed jobs** | **Dark mode** |
| <img src="docs/screenshots/failures.png" alt="Failed jobs grouped by error fingerprint with retry-group actions" width="440"> | <img src="docs/screenshots/overview-dark.png" alt="Overview page in dark mode" width="440"> |

## Architecture

```mermaid
flowchart LR
  B[Browser] -->|session cookie + CSRF| W[Web · Next.js]
  S[Your services] -->|Bearer API key| A
  W -->|/api proxy, SSE| A[API · FastAPI]
  A -->|transactions| PG[(PostgreSQL<br/>source of truth)]
  A -->|job IDs, events,<br/>rate limits| R[(Redis<br/>dispatch index)]
  R -->|BZPOPMIN| K[Workers × N]
  K -->|claim, lease,<br/>record outcome| PG
  SC[Scheduler] -->|promote, schedules,<br/>recover, retention| PG
  SC -->|reconcile| R
```

| Component | Role |
|---|---|
| **API** (`backend/queueforge/api`) | Auth, validation, submission, queries, metrics, SSE. Stateless, so you can run several |
| **Workers** (`backend/queueforge/worker.py`) | Take job IDs from Redis, claim the job in PostgreSQL, run the handler with a timeout, record the outcome |
| **Scheduler** (`backend/queueforge/scheduler.py`) | Start due and recurring jobs, recover crashed work, repair Redis, delete old data. Safe to run more than one |
| **Web** (`frontend/`) | Next.js + TypeScript + Tailwind dashboard. No business logic; types are generated from the OpenAPI document |

```
backend/    FastAPI app, worker, scheduler, CLI, Alembic migrations, pytest suite
frontend/   Next.js dashboard and Vitest suite
bench/      benchmark script and recorded results
docs/       architecture, ADRs, operations guide, screenshots
scripts/    portable PostgreSQL/Redis runner for machines without Docker
```

## Quick start (Docker)

Copy the example config and set `QF_SECRET_KEY` to a long random value (`python -c "import secrets; print(secrets.token_urlsafe(48))"`):

```bash
cp .env.example .env
```

```bash
docker compose up --build
```

Compose starts PostgreSQL 16 and Redis 7 with health checks, runs the migrations once, then starts the API, two workers, the scheduler and the web app. The API (8000) and web (3000) ports are published on `127.0.0.1` only; set `QF_BIND_ADDRESS` to change that.

In a second terminal, add a demo account and sample jobs:

```bash
docker compose run --rm api queueforge seed
```

```bash
docker compose run --rm api queueforge demo-jobs
```

Open **http://localhost:3000** and sign in as `demo@queueforge.dev` / `queueforge-demo`. This account is for local use only; `seed` refuses to run in production. The interactive API reference is at http://localhost:8000/api/docs.

## Local setup (without Docker)

Needs Python 3.12+, Node 20+, PostgreSQL 14+ and Redis 5+. If you don't have PostgreSQL and Redis, `scripts/devservices.sh start` runs portable builds from `.devservices/` on ports 54329 and 63799 (the script header explains where to put them).

```bash
python -m venv .venv && .venv/bin/pip install -e "backend[dev]"
```

Create `backend/.env` from `.env.example` and set `QF_SECRET_KEY`, `QF_DATABASE_URL` and `QF_REDIS_URL`. Then, from `backend/`, start each process in its own terminal:

```bash
queueforge migrate && queueforge seed
```

```bash
queueforge api --port 8000
```

```bash
queueforge worker --concurrency 8
```

```bash
queueforge scheduler
```

```bash
cd frontend && npm ci && npm run dev
```

`queueforge check-config` prints the settings (secrets masked) and checks both connections. `queueforge demo-jobs` submits a fixed sample workload: successes, a flaky provider that succeeds on retry, a permanent error, a timeout, a delayed job and a report.

## PostgreSQL and Redis

| | PostgreSQL | Redis |
|---|---|---|
| **Setting** | `QF_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/queueforge` | `QF_REDIS_URL=redis://host:6379/0` |
| **Versions** | 14+ (CI and Compose use 16) | 5+ (Compose uses 7 with AOF persistence) |
| **Holds** | Everything: users, projects, keys, jobs, attempts, events, schedules, workers, audit log | Per-queue sorted sets of job IDs (priority, then FIFO), live-event pub/sub, rate-limit counters |
| **If lost** | The API returns `503` and accepts nothing | Rebuilt from PostgreSQL by the scheduler. Meanwhile jobs are still accepted, workers poll PostgreSQL with `SKIP LOCKED`, and the dashboard falls back to polling |

Schema changes are Alembic migrations, applied with `queueforge migrate` (Compose does this before anything else starts). Every other setting is listed in [.env.example](.env.example).

## Workers and scheduler

```bash
queueforge worker --concurrency 8 --queues default,emails
```

- `--concurrency` is the number of jobs one process runs at once; `--queues` limits it to some queues (all queues by default).
- A worker takes the next job ID from Redis, then **claims** it in PostgreSQL with a compare-and-set on `state = 'queued'`. Stale or duplicate IDs in Redis are simply skipped, so a job never runs twice at the same time.
- A claimed job gets a **30 s lease**, renewed every 10 s. The final result is only written if the worker still holds the lease for that attempt.
- Per-queue concurrency limits apply across all workers. Queues can be paused.
- On `SIGTERM` a worker stops claiming, waits `QF_SHUTDOWN_GRACE_SECONDS`, then returns unfinished jobs to the queue without using up an attempt.

```bash
queueforge scheduler
```

Every tick (1 s) the scheduler promotes delayed jobs that are due and creates runs for cron and interval schedules. Every 15 s it recovers jobs whose lease expired, marks silent workers as lost, puts missing job IDs back in Redis and removes stale ones. It also deletes finished jobs after the retention period. Running two schedulers is safe.

## Retries and failure recovery

- **Attempts:** each job has `max_attempts` (1–25, project default 5). The wait before attempt *n+1* is `min(cap, base × 2^(n−1))` with jitter.
- **What retries:** handler errors, timeouts and lost workers retry. A `PermanentError`, an invalid payload or an unknown job type fail immediately.
- **Dead letter:** jobs that run out of attempts end in `failed`. The **Failed jobs** page groups them by error (type plus normalised message), suggests what to check, and retries a whole group in one click. A manual retry gives a fresh attempt budget and keeps the history.
- **Crashes:** if a worker dies, its lease expires and the scheduler puts the job back in the queue. The tests kill a real worker process to check this.
- **Cancelling:** a waiting job is cancelled immediately. A running job is cancelled at the worker's next heartbeat.
- **Delivery is at-least-once.** After a crash a handler can run again, so handlers with side effects should be idempotent. They receive the job ID and attempt number for that. Submission is exactly-once per idempotency key: the same key and payload returns the original job, and a different payload returns `409`.

## API

All endpoints are under `/api/v1`. The full reference is at `/api/docs` (Swagger UI) and `/api/openapi.json`, and the dashboard's **Developer docs** page shows the examples with your own project ID. Services authenticate with an API key created under **Project & API keys**; the dashboard uses a session cookie with CSRF protection.

```bash
curl -X POST "http://localhost:8000/api/v1/projects/$PROJECT/jobs" \
  -H "Authorization: Bearer $QF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"type": "http.request", "priority": 6, "idempotency_key": "order-1042",
       "payload": {"url": "https://example.com/hook", "method": "POST", "body": {"order": 1042}}}'
```

| Endpoint | Purpose |
|---|---|
| `POST /projects/{id}/jobs` | Submit (priority, `delay_seconds` or `run_at`, `max_attempts`, idempotency key) |
| `GET /projects/{id}/jobs` | List with filters, sorting and pagination |
| `GET /projects/{id}/jobs/{job}` | A job with its attempts and events |
| `POST …/jobs/{job}/cancel` · `…/retry` · `…/jobs/bulk` | Control jobs |
| `GET /projects/{id}/failures` · `POST …/failures/{fingerprint}/retry` | Dead letter, grouped |
| `GET/POST /projects/{id}/schedules` · `POST …/{s}/trigger` | Recurring jobs |
| `GET/POST /projects/{id}/queues` · `PATCH …/{queue}` | Queues, concurrency limits, pause |
| `GET /projects/{id}/metrics?window=1h\|24h\|7d` · `GET …/workers` | Metrics and worker health |
| `GET /projects/{id}/events` | Live updates over Server-Sent Events |
| `GET /job-types` | Handlers with their JSON Schemas and examples |
| `/auth/*` · `/projects` · `…/api-keys` | Accounts, projects and API keys |
| `GET /healthz` · `GET /readyz` | Liveness and readiness |

Errors always have the same shape: `{"error": {"code": "...", "message": "...", "details": [...], "request_id": "..."}}`.

## Tests

```bash
cd backend && pytest -q
```

```bash
cd frontend && npm test && npm run lint && npm run typecheck
```

Backend tests run against a real PostgreSQL and Redis. Set `QF_TEST_DATABASE_URL` and `QF_TEST_REDIS_URL` (the defaults match CI), and note that the test database is wiped. Besides the usual API, auth and validation cases they cover:

- 20 workers racing to claim one job, and 3 workers × 60 jobs each running exactly once
- a real worker process killed mid-job and its job recovered
- a worker that lost its lease being unable to overwrite the newer attempt
- 25 concurrent submissions with the same idempotency key
- Redis data loss repaired by the reconciler, and the API and workers running with Redis down
- 5 schedulers running at once without duplicating schedule runs
- metrics checked against a known workload, SSE over a real HTTP server, and a migration upgrade → downgrade → upgrade

**Latest results**

| Suite | Result | Where |
|---|---|---|
| Backend (pytest) | ✅ 168 passed in 3 min 41 s | Local run, 8 Nov 2026: Windows 11 laptop, Python 3.13, PostgreSQL 16.8, Redis 5.0.14 |
| Frontend (Vitest) | ✅ 27 passed (9 files) in 66 s | Local run, 8 Nov 2026: Node 24 |
| CI (lint, types, migrations, OpenAPI drift, tests, Docker build and smoke test) | ✅ Passed | [GitHub Actions run for `d312344`](https://github.com/aryan45sandilya/QueueForge/actions/runs/42411571369) (Ubuntu, PostgreSQL 16, Redis 7) |

## Benchmarks

```bash
python bench/run_benchmark.py --jobs 2000 --work-ms 20 --configs 1x4,1x16,2x16
```

**Method.** For each configuration (`WORKERS x CONCURRENCY`) the script creates a fresh project and starts its own API, scheduler and worker processes. Phase 1 submits every job over HTTP into a paused queue, which measures API submission speed. Phase 2 unpauses the queue and measures how fast the workers empty it. All numbers come from the stored job records.

**Recorded run** (25 Oct 2026): 2,000 `bench.synthetic` jobs of 20 ms each, 5% failing once and retried after 1 s, 16 concurrent submitters. Everything ran on one Windows 11 laptop (2 cores / 4 threads, 7.7 GB RAM) with Python 3.13, PostgreSQL 16.8 and Redis 5.0.14. Raw data: [bench/results/20261025T224436Z.json](bench/results/20261025T224436Z.json).

| Config | Submit (jobs/s) | Drain (jobs/s) | Run time p50 / p95 | Retried | Failed |
|---|---|---|---|---|---|
| 1 worker × 4 | 50.4 | 58.2 | 32 / 53 ms | 6.0% | 0% |
| 1 worker × 16 | 43.2 | 59.9 | 33 / 49 ms | 6.0% | 0% |
| 2 workers × 16 | 42.7 | 70.9 | 41 / 85 ms | 6.0% | 0% |

- **More slots in one process didn't help.** Each worker process claims one job at a time, and each claim is a PostgreSQL transaction, which caps one process at about 60 short jobs per second here. A second process added about 18%. Claiming several jobs per transaction is the obvious next improvement.
- All 2,000 jobs succeeded in every run, and exactly the 120 jobs chosen to fail once were retried.
- This is a single laptop with sleep-based jobs, not a production measurement. Re-run it on your own hardware.

## Deploying

Run one backend image in three roles (`queueforge api`, `queueforge worker`, `queueforge scheduler`) plus the web image, against PostgreSQL and Redis, with `queueforge migrate` as a release step. Set `QF_ENV=production`; the app then refuses to start with default secrets or wildcard CORS. To let other people sign up, also configure SMTP and the account limits.

→ [Operations guide](docs/operations.md) · [Running a public instance](docs/operations.md#running-a-public-instance-open-sign-up)

## Limitations

- **Timeouts are cooperative.** A handler that blocks the event loop can't be interrupted; its lease expires and the job runs again elsewhere.
- **Handlers are compiled in.** On a shared instance, users run their own logic through `http.request`.
- **Throughput is bounded by PostgreSQL** and by one-at-a-time claiming per worker process.
- **Schedules are UTC only.**
- **Not built yet:** team invites, a CAPTCHA on sign-up, an admin UI, and a Prometheus exporter.

## Further reading

| | |
|---|---|
| [Architecture](docs/architecture.md) | Job lifecycle, consistency model, failure modes |
| [Operations](docs/operations.md) | Deployment, scaling, troubleshooting, public instances |
| [ADRs](docs/adr) | [Source of truth](docs/adr/0001-postgres-source-of-truth.md) · [At-least-once](docs/adr/0002-at-least-once-delivery.md) · [Retries](docs/adr/0003-retries-backoff-dead-letter.md) · [Auth](docs/adr/0004-authentication.md) |
