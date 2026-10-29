"""The single authoritative job lifecycle model: states, transitions, retry policy.

Every code path that changes `jobs.state` goes through `assert_transition`, so the diagram in
docs/architecture.md and the behaviour can't drift apart silently.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from enum import StrEnum


class JobState(StrEnum):
    SCHEDULED = "scheduled"
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptOutcome(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    LOST = "lost"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ALL_STATES: tuple[str, ...] = tuple(s.value for s in JobState)
ATTEMPT_OUTCOMES: tuple[str, ...] = tuple(o.value for o in AttemptOutcome)

TERMINAL_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})
ACTIVE_STATES = frozenset(
    {JobState.SCHEDULED, JobState.QUEUED, JobState.RUNNING, JobState.RETRYING}
)
BACKLOG_STATES = frozenset({JobState.SCHEDULED, JobState.QUEUED, JobState.RETRYING})
CANCELLABLE_STATES = frozenset(
    {JobState.SCHEDULED, JobState.QUEUED, JobState.RETRYING, JobState.RUNNING}
)
RETRYABLE_BY_USER = frozenset({JobState.FAILED, JobState.CANCELLED})

TRANSITIONS: dict[JobState | None, frozenset[JobState]] = {
    None: frozenset({JobState.SCHEDULED, JobState.QUEUED}),  # creation
    JobState.SCHEDULED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.RETRYING,
            JobState.CANCELLED,
            JobState.QUEUED,  # graceful shutdown hands the job back without using an attempt
        }
    ),
    JobState.RETRYING: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.FAILED: frozenset({JobState.QUEUED}),  # manual retry from the dead-letter state
    JobState.CANCELLED: frozenset({JobState.QUEUED}),  # manual retry
    JobState.SUCCEEDED: frozenset(),
}

MAX_ATTEMPTS_CAP = 25


class InvalidTransition(Exception):
    def __init__(self, from_state: str | None, to_state: str) -> None:
        super().__init__(f"Invalid job state transition {from_state!r} -> {to_state!r}")
        self.from_state = from_state
        self.to_state = to_state


def can_transition(from_state: str | None, to_state: str) -> bool:
    source = JobState(from_state) if from_state is not None else None
    return JobState(to_state) in TRANSITIONS[source]


def assert_transition(from_state: str | None, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise InvalidTransition(from_state, to_state)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    backoff_base_seconds: int
    backoff_max_seconds: int

    def backoff_seconds(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay before the attempt after `attempt` (1-based): exponential with equal jitter."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        exponent = min(attempt - 1, 32)  # avoid huge ints; the cap applies long before this
        ceiling = min(float(self.backoff_max_seconds), self.backoff_base_seconds * (2.0**exponent))
        jitter = rng.uniform(0, ceiling / 2) if rng is not None else random.uniform(0, ceiling / 2)
        return ceiling / 2 + jitter

    def should_retry(self, attempts_made: int, retryable: bool) -> bool:
        return retryable and attempts_made < self.max_attempts


_NORMALIZE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x?"),
    (
        re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
        "<uuid>",
    ),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"'[^']{0,200}'|\"[^\"]{0,200}\""), "<str>"),
]


def error_fingerprint(error_type: str, message: str) -> str:
    """Groups failures that share a root cause even when messages embed IDs or counts."""
    normalized = message[:500]
    for pattern, replacement in _NORMALIZE:
        normalized = pattern.sub(replacement, normalized)
    digest = hashlib.sha1(f"{error_type}|{normalized}".encode(), usedforsecurity=False)
    return digest.hexdigest()[:16]
