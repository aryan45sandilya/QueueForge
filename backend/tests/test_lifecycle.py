"""Unit tests: state machine, retry policy, error fingerprints."""

from __future__ import annotations

import random

import pytest

from queueforge.lifecycle import (
    ALL_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    JobState,
    RetryPolicy,
    assert_transition,
    can_transition,
    error_fingerprint,
)


def test_every_state_has_a_transition_entry() -> None:
    assert set(TRANSITIONS) - {None} == set(JobState)
    assert set(ALL_STATES) == {s.value for s in JobState}


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (None, "queued"),
        (None, "scheduled"),
        ("scheduled", "queued"),
        ("queued", "running"),
        ("running", "succeeded"),
        ("running", "retrying"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "queued"),
        ("retrying", "queued"),
        ("failed", "queued"),
        ("cancelled", "queued"),
    ],
)
def test_allowed_transitions(src: str | None, dst: str) -> None:
    assert can_transition(src, dst)
    assert_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (None, "running"),
        ("queued", "succeeded"),  # must run first
        ("scheduled", "running"),  # must be promoted first
        ("succeeded", "queued"),  # succeeded is final
        ("succeeded", "failed"),
        ("failed", "running"),
        ("retrying", "running"),
        ("cancelled", "running"),
    ],
)
def test_forbidden_transitions(src: str | None, dst: str) -> None:
    assert not can_transition(src, dst)
    with pytest.raises(InvalidTransition):
        assert_transition(src, dst)


def test_succeeded_is_the_only_absorbing_state() -> None:
    absorbing = {s for s in JobState if not TRANSITIONS[s]}
    assert absorbing == {JobState.SUCCEEDED}
    assert JobState.SUCCEEDED in TERMINAL_STATES


def test_backoff_grows_exponentially_within_jitter_bounds() -> None:
    policy = RetryPolicy(max_attempts=10, backoff_base_seconds=2, backoff_max_seconds=1000)
    rng = random.Random(42)
    for attempt in range(1, 8):
        ceiling = 2 * 2 ** (attempt - 1)
        for _ in range(50):
            delay = policy.backoff_seconds(attempt, rng)
            assert ceiling / 2 <= delay <= ceiling


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(max_attempts=25, backoff_base_seconds=10, backoff_max_seconds=60)
    rng = random.Random(1)
    assert all(30 <= policy.backoff_seconds(20, rng) <= 60 for _ in range(100))
    # Very large attempt numbers must not overflow.
    assert policy.backoff_seconds(10_000, rng) <= 60


def test_backoff_is_deterministic_with_seeded_rng() -> None:
    policy = RetryPolicy(5, 3, 300)
    a = [policy.backoff_seconds(n, random.Random(7)) for n in range(1, 5)]
    b = [policy.backoff_seconds(n, random.Random(7)) for n in range(1, 5)]
    assert a == b


def test_backoff_rejects_zero_attempt() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(3, 1, 10).backoff_seconds(0)


def test_should_retry_respects_budget_and_retryability() -> None:
    policy = RetryPolicy(max_attempts=3, backoff_base_seconds=1, backoff_max_seconds=10)
    assert policy.should_retry(1, retryable=True)
    assert policy.should_retry(2, retryable=True)
    assert not policy.should_retry(3, retryable=True)
    assert not policy.should_retry(1, retryable=False)


def test_fingerprint_groups_messages_differing_only_in_ids() -> None:
    a = error_fingerprint(
        "TimeoutError",
        "upstream 10.0.0.12 timed out after 3012 ms for job 1b4e28ba-2fa1-11d2-883f-0016d3cca427",
    )
    b = error_fingerprint(
        "TimeoutError",
        "upstream 10.0.0.99 timed out after 15 ms for job 9f0e28ba-2fa1-11d2-883f-0016d3cca000",
    )
    c = error_fingerprint("ValueError", "upstream 10.0.0.12 timed out after 3012 ms")
    assert a == b
    assert a != c
    assert len(a) == 16
