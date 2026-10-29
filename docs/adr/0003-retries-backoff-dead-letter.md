# ADR 0003: Bounded retries, exponential backoff with jitter, dead-letter state

- Status: accepted
- Date: 2026-10-26

## Decision

- Each job carries `max_attempts` (1–25), `backoff_base_seconds` and
  `backoff_max_seconds`, defaulted from project settings.
- The delay before attempt `n + 1` is
  `d = min(max, base × 2^(n-1))`, then "equal jitter": `d/2 + uniform(0, d/2)`.
  Jitter spreads retry storms after a shared dependency recovers.
- Errors are classified: `PermanentError` (and payload validation failures at
  execution time) go straight to `failed`; any other exception, a timeout or a
  lost lease is retryable.
- `failed` is the dead-letter state. Failed jobs are grouped by an error
  fingerprint (exception type plus message with digits and hex IDs
  normalized) so that one root cause shows up as one group.
- A manual retry moves a failed or cancelled job back to `queued` and grants a
  fresh budget (`max_attempts += original budget`). The full attempt history
  is kept.
- A graceful worker shutdown returns in-flight jobs to `queued` without
  consuming an attempt.

## Consequences

Retries are bounded per job and the backlog is bounded per project
(`max_queued_jobs`), so a failing dependency can't produce unbounded queue
growth.
