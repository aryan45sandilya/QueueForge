# QueueForge

QueueForge is a self-hostable platform for submitting, scheduling, executing, retrying and
observing background jobs. A service submits a job over an authenticated REST API; independent
worker processes run it through a registered handler; every attempt, error and state change is
persisted and visible in the dashboard.

**Who it is for:** teams that need a dependable job system (email and notification batches,
file and data transformations, report generation, scheduled maintenance) without adopting a
hosted queue, and who want to see exactly what happened to each job.

- [Features](#features)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Using the API](#using-the-api)
- [Consistency model and delivery guarantees](#consistency-model-and-delivery-guarantees)
- [Retries and failure handling](#retries-and-failure-handling)
- [Testing](#testing)
- [Benchmarks](#benchmarks)
- [Deployment](#deployment)
- [Known limitations](#known-limitations-and-future-work)

## Features

- **Authenticated API.** Argon2id password hashing; session cookies with CSRF protection for
  the dashboard; project-scoped API keys with scopes, revocation, rotation with a grace period,
  and copy-once secrets (only a keyed hash is stored).
- **Project isolation.** Every query is project-scoped and authorization is enforced on the
  backend. Cross-project access returns `404`.
- **Safe handlers.** Jobs name a registered handler and its payload is validated against a
  Pydantic schema. Submitted code is never executed. Four handlers ship:
  `notification.send`, `data.transform` (CSV pipeline), `report.generate`, and
  `bench.synthetic` (for demos and benchmarks).
- **Lifecycle.** `scheduled → queued → running → succeeded | retrying | failed | cancelled`.
  One state machine enforces it, and every transition is recorded with actor and message.
- **Scheduling.** Delayed jobs (`delay_seconds` or `run_at`); recurring schedules by cron or
  interval, with overlap skipping and a run-now action.
- **Reliability.** Priorities, per-job timeouts, bounded retries with exponential backoff and
  jitter, a dead-letter state grouped by error fingerprint, idempotency keys, cancellation of
  running jobs, per-queue concurrency limits across all workers, lease-based crash recovery,
  graceful shutdown, Redis/PostgreSQL reconciliation, and a fallback to PostgreSQL polling when
  Redis is down.
- **Observability.** Metrics computed from persisted records: throughput, submission rate,
  failure rate, retries, wait and execution percentiles, queue depth and worker heartbeats.
  Live dashboard updates use Server-Sent Events, with a visible polling fallback.
- **Dashboard.** Overview, Jobs (filter, sort, paginate, bulk cancel/retry, submit), Job
  details, Queues & workers, Failed jobs, Schedules, Project & API keys, Settings and Developer
  docs. Light and dark themes, responsive, reduced-motion aware.

## Architecture

```mermaid
flowchart LR
  subgraph Clients
    B[Browser] -->|cookie + CSRF| W[Web: Next.js]
    S[Your services] -->|Bearer API key| A
  end
  W -->|/api proxy, SSE| A[API: FastAPI]
  A -->|transactions| PG[(PostgreSQL<br/>source of truth)]
  A -->|ZADD job id, publish events,<br/>rate limits| R[(Redis<br/>dispatch index)]
  R -->|BZPOPMIN| K[Workers x N]
  K -->|claim CAS, leases,<br/>fenced outcomes| PG
  SC[Scheduler] -->|promote, cron, recover leases,<br/>retention| PG
  SC -->|reconcile| R
```

| Component | Responsibility |
|-----------|---------------|
| **PostgreSQL** | Authoritative state: users, projects, keys, jobs, attempts, events, schedules, workers, audit log. Every transition is a transaction. |
| **Redis** | Per-queue sorted sets of job IDs (priority, then FIFO), pub/sub for live events, rate-limit counters. Fully rebuildable from PostgreSQL. |
| **API** (`backend/queueforge/api`) | Auth, validation, submission, queries, metrics, SSE. Stateless. |
| **Workers** (`backend/queueforge/worker.py`) | Pop IDs, claim with compare-and-set in PostgreSQL, run handlers under timeouts, renew leases, record fenced outcomes. |
| **Scheduler** (`backend/queueforge/scheduler.py`) | Promote due jobs, materialize schedules, recover expired leases, mark lost workers, reconcile Redis, enforce retention. Safe to run more than one. |
| **Web** (`frontend/`) | Next.js + TypeScript + Tailwind. Presentation only: no business logic or simulated data. Types are generated from the OpenAPI document. |

Design documents: [docs/architecture.md](docs/architecture.md) (lifecycle, consistency model,
failure modes) and the ADRs in [docs/adr](docs/adr):
[source of truth](docs/adr/0001-postgres-source-of-truth.md),
[at-least-once delivery](docs/adr/0002-at-least-once-delivery.md),
[retries and dead letter](docs/adr/0003-retries-backoff-dead-letter.md),
[authentication](docs/adr/0004-authentication.md).

### Repository layout

```
backend/        FastAPI app, worker, scheduler, CLI, Alembic migrations, tests (pytest)
frontend/       Next.js dashboard, typed API client, tests (Vitest + Testing Library)
bench/          reproducible benchmark and recorded results
docs/           architecture, ADRs, operations guide
scripts/        portable PostgreSQL/Redis runner for machines without Docker
```

## Quick start

### Option A: Docker Compose (everything)

```bash
cp .env.example .env
```

Set `QF_SECRET_KEY` in `.env` to a long random value
(`python -c "import secrets; print(secrets.token_urlsafe(48))"`), then:

```bash
docker compose up --build
```

```bash
docker compose run --rm api queueforge seed
```

```bash
docker compose run --rm api queueforge demo-jobs
```

Open http://localhost:3000 and sign in as `demo@queueforge.dev` with password
`queueforge-demo` (local demo only; `seed` refuses to run with `QF_ENV=production`).
`seed` prints a demo API key once. The API reference is at http://localhost:8000/api/docs.

Compose starts PostgreSQL and Redis (with health checks), runs migrations as a one-shot
service, then starts the API, two workers, the scheduler and the web app.

### Option B: run the processes yourself

Requirements: Python 3.12+, Node 20+, PostgreSQL 14+, Redis 5+. Without Docker,
`scripts/devservices.sh start` runs portable PostgreSQL and Redis binaries placed in
`.devservices/` (see the script header). It uses ports 54329 and 63799.

```bash
python -m venv .venv && .venv/bin/pip install -e "backend[dev]"
```

Create `backend/.env` from `.env.example` (set `QF_SECRET_KEY`, `QF_DATABASE_URL`,
`QF_REDIS_URL`), then from `backend/` run each process in its own terminal:

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

`queueforge check-config` validates the settings (secrets masked) and checks connectivity.
`queueforge demo-jobs` submits a deterministic sample workload: successes, a provider that fails
once then succeeds, bounces, a permanent error, a timeout, a delayed job and a report.

## Using the API

Base path `/api/v1`. Full reference: `/api/docs` (Swagger UI) and `/api/openapi.json`. The
dashboard's **Developer docs** page shows these examples with your real project ID.

```bash
export QF_API_KEY="qf_<prefix>_<secret>"
```

```bash
export P=<project id>
```

```bash
curl -sS -X POST "http://localhost:8000/api/v1/projects/$P/jobs" -H "Authorization: Bearer $QF_API_KEY" -H "Content-Type: application/json" -d '{"type": "data.transform", "queue": "default", "priority": 6, "idempotency_key": "nightly-sales-2026-10-25", "payload": {"csv": "region,units\nnorth,12\nsouth,7\nnorth,3\n", "steps": [{"op": "cast", "column": "units", "to": "int"}, {"op": "aggregate", "group_by": ["region"], "metrics": [{"fn": "sum", "column": "units", "as": "units"}]}]}}'
```

```bash
curl -sS "http://localhost:8000/api/v1/projects/$P/jobs?state=failed&sort=finished_at&order=desc&limit=20" -H "Authorization: Bearer $QF_API_KEY"
```

```bash
curl -N "http://localhost:8000/api/v1/projects/$P/events" -H "Authorization: Bearer $QF_API_KEY"
```

Main endpoints:

| Method & path | Purpose |
|---|---|
| `POST /auth/register`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | Dashboard accounts and sessions |
| `GET/POST /projects`, `GET/PATCH /projects/{id}` | Projects and their job defaults |
| `GET/POST /projects/{id}/api-keys`, `POST …/{key}/revoke`, `POST …/{key}/rotate` | API keys (owners) |
| `GET/POST /projects/{id}/queues`, `PATCH/DELETE …/{queue}` | Queues, concurrency limits, pause |
| `POST /projects/{id}/jobs`, `GET /projects/{id}/jobs`, `GET …/jobs/{job}` | Submit, list (filter/sort/paginate), inspect |
| `POST …/jobs/{job}/cancel`, `POST …/jobs/{job}/retry`, `POST …/jobs/bulk` | Control |
| `GET /projects/{id}/failures`, `POST …/failures/{fingerprint}/retry` | Dead letter, grouped |
| `GET/POST /projects/{id}/schedules`, `PATCH/DELETE …/{s}`, `POST …/{s}/trigger`, `POST /schedules/preview` | Recurring jobs |
| `GET /projects/{id}/metrics?window=1h\|24h\|7d`, `GET /projects/{id}/workers` | Operations |
| `GET /projects/{id}/events` | Server-Sent Events |
| `GET /job-types` | Handlers with JSON Schemas and examples |
| `GET /healthz`, `GET /readyz` | Liveness and readiness |

Errors always look like
`{"error": {"code": "validation_error", "message": "…", "details": [...], "request_id": "…"}}`
with status codes 401, 403, 404, 409, 413, 422, 429 and 503 used as documented on the Docs page.

## Consistency model and delivery guarantees

- **PostgreSQL is the source of truth.** A job is acknowledged only after its row and first
  event commit. Redis holds only job IDs as a dispatch hint.
- **Claiming** is a compare-and-set on `state = 'queued'` under a row lock (plus a queue-row
  lock when the queue has a concurrency limit). Duplicate or stale Redis entries are discarded,
  so Redis can never cause concurrent execution of one job.
- **Leases** (30 s, renewed every 10 s) mark ownership. Outcome writes are fenced on
  `(worker_id, attempt)`, so a worker that lost its lease cannot overwrite a newer attempt.
- **Delivery is at-least-once.** A handler may run again after a crash, partition or failed
  outcome write. Handlers with external side effects must be idempotent; they receive the job ID
  and attempt number for downstream deduplication. QueueForge does **not** claim exactly-once
  execution.
- **Submission is exactly-once per idempotency key:** a partial unique index on
  `(project_id, idempotency_key)`. Same key and payload returns the original job (`200`);
  same key with a different payload returns `409`. This is tested with 25 concurrent requests.
- **Reconciliation** (every 15 s) re-pushes queued jobs missing from Redis, removes stale IDs,
  drops keys of deleted queues, recovers expired leases and marks silent workers as lost.
- **Redis unavailable:** jobs are still accepted; workers claim directly from PostgreSQL with
  `SKIP LOCKED`; live updates fall back to polling. **PostgreSQL unavailable:** the API returns
  `503` and accepts nothing.

## Retries and failure handling

- `max_attempts` (1–25, project default 5) bounds every job. Backoff before attempt *n+1* is
  `min(cap, base × 2^(n−1))` with equal jitter.
- Handler errors are **retryable** by default; `PermanentError`, payload validation failures at
  execution time and unknown job types fail immediately. Timeouts and lost workers are retryable.
- Exhausted or permanent failures move to `failed`, the dead letter. The **Failed jobs** page
  groups them by fingerprint (error type plus normalized message) with recovery guidance, and
  retries a whole group at once. A manual retry grants a fresh attempt budget and keeps history.
- Cancelling a pending job is immediate. Cancelling a running job is cooperative: the worker
  learns of it at its next heartbeat and cancels the handler.
- `SIGTERM` makes a worker stop claiming, wait `QF_SHUTDOWN_GRACE_SECONDS`, then return
  unfinished jobs to the queue without spending an attempt.
- Growth is bounded: request and payload size limits, result truncation, per-project backlog
  cap (`429 backlog_full`), rate limits, and retention that deletes finished jobs after
  `retention_days`.

## Testing

Backend tests run against **real PostgreSQL and Redis** and wipe the target test database.

```bash
cd backend && QF_TEST_DATABASE_URL=postgresql+asyncpg://queueforge:queueforge@127.0.0.1:5432/queueforge_test QF_TEST_REDIS_URL=redis://127.0.0.1:6379/15 pytest -q
```

They cover state transitions, backoff, handler validation and behaviour; auth, CSRF, rate
limits, CORS and error shapes; cross-project isolation, scopes, revocation and rotation; job
submission, idempotency under 25 concurrent duplicates, filters and pagination, cancel, retry,
bulk actions and backlog caps. They also cover worker execution, retries, timeouts, permanent
errors, cancellation of running jobs, graceful shutdown, priorities, paused queues and queue
concurrency limits across two workers; 20-way claim races and 3 workers × 60 jobs executing each
exactly once; fenced completion after lease loss; and **killing a real worker process** and
recovering its job. Further cases: Redis data loss repaired by the reconciler, stale and orphan
Redis entries, API and workers operating while Redis is down, readiness without PostgreSQL,
schedule materialization (including 5 concurrent schedulers), metrics against a known workload,
SSE over a real HTTP server, and a migration upgrade → downgrade → upgrade round trip.

```bash
cd frontend && npm test && npm run lint && npm run typecheck
```

Linting and types: `ruff check`, `ruff format --check` and `mypy` (backend); ESLint and
`tsc --noEmit` (frontend). GitHub Actions ([.github/workflows/ci.yml](.github/workflows/ci.yml))
runs all of these plus migrations, checks that the OpenAPI document and the generated
TypeScript types are current, builds the Docker images and smoke-tests the Compose stack.

## Benchmarks

```bash
python bench/run_benchmark.py --jobs 2000 --work-ms 20 --configs 1x4,1x16,2x16
```

For each configuration (`WORKERSxCONCURRENCY`), the script creates an isolated project and
starts its own API, scheduler and worker processes. Phase 1 submits the workload over HTTP into
a paused queue, which measures API submission throughput. Phase 2 unpauses the queue and
measures how fast workers drain the backlog. Metrics come from persisted rows; JSON goes to
`bench/results/` and a table to `bench/results/latest.md`.

**Recorded run (synthetic, not a production measurement).** 2,000 jobs of `bench.synthetic`
with 20 ms of sleep each, 5% (seeded) failing once and retried after 1 s, 16 concurrent HTTP
submitters. The machine was a Windows 11 laptop with 2 physical / 4 logical cores and 7.7 GB of
RAM, running Python 3.13, PostgreSQL 16.8 and Redis 5.0.14 (portable builds). The API,
scheduler, workers, PostgreSQL and Redis all shared that machine, and an idle development API
and scheduler were also running. Raw data:
[bench/results/20261025T224436Z.json](bench/results/20261025T224436Z.json).

| config | slots | API submit jobs/s | drain completed jobs/s | drain s | backlog wait p50 / p95 / p99 ms | exec p50 / p95 ms | retry rate | failure rate |
|---|---|---|---|---|---|---|---|---|
| 1x4 | 4 | 50.37 | 58.2 | 34.37 | 35826 / 39321 / 39395 | 32 / 53 | 6.0% | 0.0% |
| 1x16 | 16 | 43.21 | 59.9 | 33.39 | 38529 / 45469 / 46324 | 33 / 49 | 6.0% | 0.0% |
| 2x16 | 32 | 42.74 | 70.9 | 28.21 | 37247 / 45929 / 46315 | 41 / 85 | 6.0% | 0.0% |

What this shows:

- **More slots in one process didn't help (1x4 ≈ 1x16).** With 20 ms jobs, a single worker
  process is limited by its dispatch loop, which claims one job at a time. Each claim is a
  PostgreSQL transaction, so the loop manages roughly 60 claims per second here, not by
  execution slots. A second process (2x16) raised drain throughput by about 18% on this 2-core
  machine, where the processes compete with PostgreSQL for CPU. Batch claiming is the obvious
  next optimization (see limitations).
- **API submission ran at ~43–50 jobs/s** from a single uvicorn process. Each submission
  authenticates the key, rate-limits through Redis, checks the backlog cap, inserts the job and
  event, pushes to Redis, then reads the job back for the response.
- **Backlog wait is mostly the submission phase.** Jobs sat in the paused queue while the
  other jobs were submitted, so wait percentiles here describe the backlog design, not idle
  latency. Execution p95 rising at 2x16 reflects CPU contention on this machine.
- With seed 7, the 5% failure probability marks exactly 120 jobs (6.0%) to fail once. Every
  run retried exactly those 120 jobs, all 2,000 succeeded, and no submission returned an error.

Limitations: one machine, sleep-based handlers, a Windows laptop, and short runs (about 30 s of
drain). Re-run on your own hardware before drawing capacity conclusions.

## Deployment

See [docs/operations.md](docs/operations.md) for the production guide: process roles and
scaling, required secrets (`QF_SECRET_KEY`, `QF_DATABASE_URL`, `QF_REDIS_URL`), migration
order, health checks, graceful shutdown, proxy settings for SSE, failure handling and
troubleshooting. In short, run one backend image as three roles (`queueforge api`,
`queueforge worker`, `queueforge scheduler`) plus the web image, against managed PostgreSQL and
Redis, with `queueforge migrate` as a release step.

## Known limitations and future work

- **Timeouts are cooperative** (`asyncio`). A handler that blocks the event loop can't be
  pre-empted; its lease expires and the job is re-run elsewhere. Process-isolated execution for
  CPU-bound handlers is future work.
- **Handlers are built in.** Adding a handler means adding a module and redeploying workers;
  there is no plugin loading by design. Workers that don't know a job type fail it permanently,
  so deploy workers before clients submit new types.
- **Throughput is bounded by PostgreSQL**, since every claim and outcome is a transaction. Queues
  with concurrency limits also serialize claims on the queue row. Each worker process claims
  serially, so for short jobs scale with more worker processes rather than more slots per
  process. Claiming several jobs per transaction would raise per-process throughput.
- **Project membership** has owner and member roles in the data model, but there is no API or UI
  yet for inviting members; projects are created per user.
- **Schedules are UTC only.** Missed runs during scheduler downtime are coalesced into one.
- **Fairness** across queues is randomized key order for `BZPOPMIN`, not weighted fair queueing.
- **Metrics** cover the project's retention window; there is no long-term metrics store or
  Prometheus exporter yet.
- **SSE** fan-out uses one Redis subscription per open dashboard; very large audiences would
  need a shared broadcaster.
