# QueueForge architecture

This document is the implementation plan and the reference for how the
components interact. Decisions with lasting consequences are recorded as ADRs in
[`docs/adr`](adr/).

## Components

```
                 ┌──────────────────────────┐
  browser ──────▶│ frontend (Next.js)       │  /api/* rewritten to the API
                 └────────────┬─────────────┘
                              │ HTTP + SSE (session cookie)
  API clients ───────────────▶│ (Bearer API key)
                 ┌────────────▼─────────────┐        ┌──────────────┐
                 │ api (FastAPI, uvicorn)   │──pub──▶│              │
                 │  auth, validation, SSE   │◀─sub───│    Redis     │
                 └──────┬───────────┬───────┘        │ dispatch     │
                        │ txn       │ ZADD           │ ZSETs,       │
                        ▼           └───────────────▶│ pub/sub,     │
                 ┌──────────────┐                    │ rate limits  │
                 │  PostgreSQL  │◀── claim (CAS) ────┤              │
                 │ source of    │                    └──────▲───────┘
                 │ truth        │                           │ BZPOPMIN
                 └──────▲───────┘                    ┌──────┴───────┐
                        │ promote / reconcile        │ worker(s)    │
                 ┌──────┴───────┐                    │ asyncio,     │
                 │ scheduler    │── ZADD ───────────▶│ N slots      │
                 └──────────────┘                    └──────────────┘
```

| Component  | Responsibility | Holds authoritative state? |
|------------|----------------|----------------------------|
| PostgreSQL | Users, projects, keys, jobs, attempts, events, schedules, workers, audit log. Every lifecycle transition is a transaction here. | **Yes** |
| Redis      | Per-queue dispatch sorted sets (job IDs only), job-event pub/sub for SSE, rate-limit counters. | No. Everything in Redis can be rebuilt from PostgreSQL. |
| API        | Authentication, authorization, validation, job submission, queries, metrics, SSE fan-out. Stateless; scale horizontally. | No |
| Worker     | Pops job IDs from Redis, claims jobs in PostgreSQL with a compare-and-set, runs registered handlers with timeouts, renews leases, records outcomes. | No |
| Scheduler  | Promotes due `scheduled`/`retrying` jobs, materializes recurring schedules, reconciles Redis with PostgreSQL, recovers expired leases, marks lost workers, enforces retention. Safe to run more than one (advisory locks + `SKIP LOCKED`). | No |
| Frontend   | Presentation only. All state transitions and metrics come from the API. | No |

## Job lifecycle

```
              ┌──────────┐ due  ┌────────┐ claim ┌─────────┐ ok   ┌───────────┐
 submit ─────▶│scheduled │─────▶│ queued │──────▶│ running │─────▶│ succeeded │
   │          └────┬─────┘      └──┬──▲──┘       └┬──┬──┬──┘      └───────────┘
   │ run_at<=now   │cancel         │  │ due       │  │  │ error, attempts left
   └───────────────┼──────────────▶│  │    ┌──────┘  │  └──────▶┌──────────┐
                   ▼               │  └────┤retrying │◀─────────│          │
              ┌──────────┐ cancel  │       └─────────┘  lease   │          │
              │cancelled │◀────────┘                    expired │          │
              └──────────┘◀──── cancel requested ───────────────┘          │
                   ▲                                                       │
                   │ manual retry ◀── ┌────────┐ ◀── error, no attempts left │
                   └─────────────────│ failed │ ◀── or permanent error ─────┘
                                      └────────┘   (dead letter)
```

The allowed transitions are defined once in `backend/queueforge/lifecycle.py`
and enforced by every write path. `failed` is the dead-letter state; jobs only
leave it through an explicit manual retry, which grants a fresh attempt budget.

## Dispatch, claiming and the consistency model

1. **Submission.** The API inserts the job row (`queued`, or `scheduled` when
   `run_at` is in the future) and its first event in one transaction. Only after
   the commit does it `ZADD` the job ID to `qf:q:<queue_id>`. If Redis is down
   the job is still accepted; the reconciler pushes it later.
2. **Pop.** A worker's dispatcher `BZPOPMIN`s across the queue keys it serves
   (shuffled each time for fairness). The score encodes priority, then FIFO.
3. **Claim.** The worker opens a PostgreSQL transaction, locks the job row, and
   only proceeds if the job is still `queued`, its queue is not paused, and the
   queue's concurrency limit (locked queue row + count of running jobs) allows
   it. The claim increments `attempts`, sets `worker_id` and
   `lease_expires_at`, and inserts an attempt row. A popped ID whose job is no
   longer `queued` (cancelled, already claimed through a duplicate entry) is
   discarded. **Redis can therefore never cause a job to run twice
   concurrently; only PostgreSQL decides ownership.**
4. **Execution.** Handlers run under `asyncio.wait_for(timeout)`. A heartbeat
   loop renews leases every `heartbeat_seconds` and learns about cancellation
   requests in the same query.
5. **Completion.** Outcome writes are fenced: `WHERE id = :id AND state =
   'running' AND worker_id = :me AND attempts = :n`. A worker that lost its
   lease (for example after a long GC pause or a network partition) cannot
   overwrite the result of the attempt that replaced it; its result is discarded
   and logged.

### Reconciliation (scheduler, every `reconcile_interval_seconds`)

| Divergence | Detection | Repair |
|------------|-----------|--------|
| Job `queued` in PostgreSQL, missing from Redis (push failed, Redis restarted without persistence, worker crashed between pop and claim) | `queued` jobs older than a grace period whose `ZSCORE` is nil | `ZADD NX` with the original score |
| ID in Redis whose job is not `queued` (cancelled, finished, deleted by retention) | Sample of the head of each queue key checked against PostgreSQL | `ZREM` |
| Job `running` with an expired lease (worker crashed, was killed, or partitioned) | `state = 'running' AND lease_expires_at < now()` | Attempt recorded as `lost`; job moves to `retrying` with backoff, or `failed` when the attempt budget is exhausted |
| Worker stopped heartbeating | `last_heartbeat_at` older than `3 × heartbeat` | Worker marked `lost` (its jobs are recovered by the lease rule above) |

### Failure modes

| Failure | API | Workers | Scheduler | Recovery |
|---------|-----|---------|-----------|----------|
| Redis unavailable | Accepts jobs (durable in PostgreSQL); `/readyz` reports degraded; SSE streams close and the dashboard falls back to visible polling; rate limiting fails open and logs a warning | Switch to claiming directly from PostgreSQL with `FOR UPDATE SKIP LOCKED` polling (higher latency, same correctness) | Promotions still commit; pushes are skipped | Reconciler re-populates dispatch sets once Redis returns |
| PostgreSQL unavailable | Returns `503 service_unavailable`; never acknowledges a job it could not persist; `/readyz` fails | Stop claiming (back off); running jobs continue, outcome writes are retried, and if the lease expires the job is re-run later (at-least-once) | Pauses | Automatic once PostgreSQL returns |
| Worker crash / `kill -9` | — | Jobs keep their expired lease | Reconciler moves them to `retrying` | Re-executed by another worker |
| Worker `SIGTERM` | — | Stops popping, waits `shutdown_grace_seconds`, then cancels remaining handlers and returns their jobs to `queued` without consuming an attempt | — | Picked up by other workers immediately |
| API crash after commit, before `ZADD` | Client may not get a response; retrying with the same idempotency key returns the original job | — | Reconciler pushes it | — |

## Delivery guarantee

QueueForge provides **at-least-once execution**. A handler can run more than
once for one job when a worker loses its lease while still executing, or when
the outcome write fails. Handlers with external side effects must be idempotent
(use `ctx.job_id` / `ctx.attempt` as a deduplication key downstream). See
[ADR 0002](adr/0002-at-least-once-delivery.md).

Submission is **exactly-once per idempotency key** within the job retention
period, enforced by a partial unique index on `(project_id, idempotency_key)`.

## Bounded resources

- Request bodies ≤ `QF_MAX_REQUEST_BYTES` (default 1 MiB); job payloads ≤
  `QF_MAX_PAYLOAD_BYTES` (default 64 KiB); results and error tracebacks are
  truncated before storage.
- `max_attempts` ≤ 25; backoff is exponential with jitter, capped per job.
- Per-project `max_queued_jobs` rejects new submissions with `429` once the
  backlog (queued + scheduled + retrying) reaches the cap.
- Retention deletes terminal jobs after `retention_days` (per project).
- Rate limits per API key, per session user and per login identity.

## Data model

See `backend/queueforge/models.py` and the migration in
`backend/alembic/versions`. Tables: `users`, `sessions`, `projects`,
`project_members`, `api_keys`, `queues`, `jobs`, `job_attempts`, `job_events`,
`schedules`, `workers`, `audit_events`. Indexes follow the actual queries: job
listing per project by `created_at`, partial indexes for promotable
(`scheduled`/`retrying` by `run_at`), running-by-lease, queued-by-queue, and
partial unique indexes for idempotency keys and schedule occurrences.

## Implementation stages

1. Repository audit (empty repository), this document, ADRs, local services.
2. Models, migration, auth (sessions + API keys), projects, error contract.
3. Submission, worker claim/execute/complete, events.
4. Retries, scheduling, idempotency, cancellation, reconciliation, retention.
5. Dashboard views, SSE.
6. Hardening, tests, metrics, benchmark.
7. Docker, CI, docs, verification.
