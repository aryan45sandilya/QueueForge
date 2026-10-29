# Operating QueueForge

## Processes and how to scale them

| Process | Command | Stateless? | Scale by | Notes |
|---------|---------|-----------|----------|-------|
| API | `queueforge api --host 0.0.0.0 --port 8000` | Yes | More replicas behind a load balancer | Needs PostgreSQL to accept jobs; degrades without Redis. |
| Worker | `queueforge worker [--concurrency N] [--queues a,b]` | Yes | More processes, or higher `QF_WORKER_CONCURRENCY` | Handlers are asyncio-based; raise concurrency for I/O-bound work, add processes for CPU-bound work. |
| Scheduler | `queueforge scheduler` | Yes | One is enough; extra replicas are safe | Promotion, cron, lease recovery, reconciliation, retention. |
| Web | `node server.js` (Next.js standalone) | Yes | More replicas | Proxies `/api/*` to `QF_API_URL`; must not buffer SSE. |
| PostgreSQL | managed or self-hosted 14+ | — | Vertical; read replicas are not used | Source of truth. Back it up. |
| Redis | 5+ (Valkey works) | — | Single instance | Rebuildable. Use `noeviction`. |

Run migrations once per release, before new API/worker processes start:
`queueforge migrate` (the Compose file runs a one-shot `migrate` service).

## Production deployment

1. **Provision PostgreSQL and Redis.** Create a database and a role owning it. Redis needs no
   persistence for correctness; AOF only shortens recovery.
2. **Set secrets** in your platform's secret store (never in the image):
   - `QF_SECRET_KEY`: 48+ random characters. Rotating it invalidates all API keys.
   - `QF_DATABASE_URL`: `postgresql+asyncpg://user:password@host:5432/db` (add `?ssl=require` for TLS).
   - `QF_REDIS_URL`: `redis://` or `rediss://` URL.
3. **Set** `QF_ENV=production`, `QF_CORS_ORIGINS=https://your-dashboard`, `QF_COOKIE_SECURE=true`,
   `QF_LOG_FORMAT=json`, and `QF_TRUST_PROXY_HEADERS=true` only if a trusted proxy sets
   `X-Forwarded-For`. Consider `QF_ALLOW_REGISTRATION=false` and creating users with
   `queueforge create-user`.
4. **Build images:** `docker build -t queueforge-backend backend/` and
   `docker build --build-arg QF_API_URL=http://<internal-api-host>:8000 -t queueforge-web frontend/`.
5. **Release:** run `queueforge migrate` as a one-off task, then roll out API, scheduler and
   workers (same backend image, different commands), then web.
6. **Health checks:** liveness `GET /healthz`; readiness `GET /readyz` (503 without PostgreSQL;
   200 with `"status": "degraded"` when Redis is down).
7. **Shutdown:** send `SIGTERM` and allow at least `QF_SHUTDOWN_GRACE_SECONDS + 10` seconds
   (Kubernetes `terminationGracePeriodSeconds`, Compose `stop_grace_period`). Workers drain,
   then hand unfinished jobs back to the queue.
8. **Proxies:** if anything sits in front of the API or web tier, disable response buffering and
   compression for `/api/v1/projects/*/events` (SSE), and allow idle connections of at least 30 s.

## Failure handling reference

See the failure-mode table in [architecture.md](architecture.md#failure-modes). In short:

- **Redis down:** jobs are still accepted and persisted; workers switch to PostgreSQL polling
  (`dispatch_mode = postgres` on the Workers page); live updates fall back to 10 s polling;
  rate limiting is not enforced. When Redis returns, the reconciler re-pushes queued jobs.
- **PostgreSQL down:** the API returns `503` and accepts nothing; workers stop claiming; running
  jobs that cannot record their outcome are re-run after lease expiry.
- **Worker crash:** jobs are recovered after `QF_LEASE_SECONDS` and retried with backoff.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---------|-------------|------------|
| Jobs stay `queued` | No healthy worker serves that queue, the queue is paused, or the queue is at its concurrency limit | Check *Queues & workers*: worker health, the worker's `queues` filter, paused state, running/limit. |
| "Dispatch set" lower than "Queued" | Redis lost entries (restart, flush) | Wait one reconcile interval (default 15 s) or check scheduler logs for `reconcile_repaired`. Workers in `postgres` mode keep working meanwhile. |
| Jobs fail with `WorkerLost` | Workers killed (OOM, deploy without grace period) or event loop blocked longer than the lease | Check worker memory and logs; give `SIGTERM` time; keep CPU-heavy handler work chunked. |
| Jobs fail with `Timeout` | Work exceeds `timeout_seconds` | Raise the timeout for that job type or split the work. |
| `429 backlog_full` | Pending jobs reached `max_queued_jobs` | Add worker capacity or raise the cap in *Settings*. |
| `429 rate_limited` | Client exceeds `QF_RATE_LIMIT_PER_MINUTE` | Honour `Retry-After`, batch work into fewer jobs, or raise the limit. |
| Dashboard shows "Polling every 10 s" | SSE stream unavailable (Redis down or a buffering proxy) | Check `/readyz`; disable proxy buffering for the events endpoint. |
| `403 csrf_failed` from scripts | Using a session cookie without the CSRF header | Scripts should use API keys (`Authorization: Bearer`). |
| `queueforge migrate` cannot connect | Wrong `QF_DATABASE_URL` | `queueforge check-config` prints the effective settings (secrets masked) and tests connectivity. |

## Observability

- Structured logs (`QF_LOG_FORMAT=json`) carry `request_id`, `worker_id`, `job_id`, `job_type`,
  `attempt`, outcomes and durations. Payloads, results, secrets and bearer tokens are never
  logged; a redaction processor masks secret-looking keys and API-key patterns as a backstop.
- `GET /api/v1/projects/{id}/metrics?window=1h|24h|7d` returns throughput, failure rate, wait and
  execution percentiles, queue depth and worker health, with definitions in the response.
- Every API response includes `X-Request-ID`; error bodies repeat it as `request_id`.
