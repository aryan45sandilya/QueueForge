# ADR 0001: PostgreSQL is the source of truth; Redis is a rebuildable dispatch index

- Status: accepted
- Date: 2026-10-26

## Context

The brief asks for Redis-based queue coordination and also says that accepted
jobs must survive process and infrastructure restarts. Redis offers low-latency
blocking pops but, depending on persistence settings, can lose writes on
restart. PostgreSQL offers durable transactions and row locks but polling it
adds latency and load.

## Decision

- Every job, attempt and state transition is written to PostgreSQL in a
  transaction. A job is acknowledged to the client only after that commit.
- Redis stores only job IDs in per-queue sorted sets (`qf:q:<queue_id>`) as a
  dispatch hint, plus pub/sub and rate-limit counters.
- Workers take ownership through a compare-and-set in PostgreSQL
  (`state = 'queued'` → `running`, under a row lock), never through Redis.
- A reconciler re-derives the Redis sets from PostgreSQL and removes stale IDs.
- If Redis is unreachable, workers claim directly from PostgreSQL with
  `FOR UPDATE SKIP LOCKED`.

## Consequences

- Losing Redis entirely loses no jobs; it only delays dispatch until the next
  reconcile pass (default 15 s) or until workers fall back to PostgreSQL polling.
- Each claim costs a PostgreSQL write transaction, which bounds throughput by
  PostgreSQL rather than Redis. That is acceptable for the target workloads
  (thousands of jobs per minute, not millions per second). The benchmark in
  `bench/` measures it.
- Queues with a concurrency limit serialize claims on the queue row. Unlimited
  queues do not take that lock.
