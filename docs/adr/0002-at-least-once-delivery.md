# ADR 0002: At-least-once execution with fenced completion

- Status: accepted
- Date: 2026-10-26

## Context

Exactly-once execution of arbitrary side effects is not achievable without the
side effect participating in the same transaction as the queue. QueueForge
handlers call external systems (email providers, APIs), so it can't promise
that.

## Decision

- Execution is **at-least-once**. A job runs again when its worker's lease
  expires (crash, partition, event-loop stall) or when its outcome could not be
  persisted.
- Leases default to 30 s and are renewed every 10 s by a heartbeat loop.
- Outcome writes are fenced by `(worker_id, attempts)`. A stale worker cannot
  overwrite a newer attempt's outcome; its result is discarded and logged as
  `lease_lost`.
- Handlers receive `ctx.job_id` and `ctx.attempt` so that they can
  deduplicate downstream (for example an idempotency header to an email API).
- Submission deduplication is exactly-once per `(project_id, idempotency_key)`
  through a unique index. Re-submitting the same key with the same payload
  returns the original job (`200`); with a different payload it returns `409`.

## Consequences

- Handler authors must make side effects idempotent. The bundled handlers
  document how they do so.
- Timeouts are enforced cooperatively with `asyncio.wait_for`. A handler that
  blocks the event loop cannot be pre-empted; its heartbeat stops, its lease
  expires and the job is re-run elsewhere. That is why bundled handlers yield
  regularly and CPU-heavy steps are chunked.
