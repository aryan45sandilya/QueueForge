"""Redis side of the system: dispatch sorted sets, job-event pub/sub, rate-limit counters.

Nothing here is authoritative (ADR 0001). Every method that can fail because Redis is down
raises `DispatchUnavailable`, and callers decide whether to degrade or fail.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

QUEUE_KEY_PREFIX = "qf:q:"
EVENTS_CHANNEL_PREFIX = "qf:events:"
_PRIORITY_SPAN = 10**13  # > any epoch-millisecond timestamp before year 2286


class DispatchUnavailable(Exception):
    """Redis could not be reached; the caller should degrade gracefully."""


def queue_key(queue_id: uuid.UUID | str) -> str:
    return f"{QUEUE_KEY_PREFIX}{queue_id}"


def dispatch_score(priority: int, queued_at: datetime) -> float:
    """Lower scores pop first: higher priority first, then FIFO by the time the job became ready."""
    return float((9 - priority) * _PRIORITY_SPAN + int(queued_at.timestamp() * 1000))


def make_redis(url: str) -> aioredis.Redis:
    return aioredis.from_url(
        url,
        decode_responses=True,
        protocol=2,  # RESP2 works on every Redis/Valkey version; newer clients default to RESP3
        socket_connect_timeout=2,
        socket_timeout=5,
        health_check_interval=30,
        retry_on_timeout=False,
    )


class Dispatcher:
    def __init__(self, client: aioredis.Redis) -> None:
        self.redis = client

    async def ping(self) -> bool:
        try:
            return bool(await self.redis.ping())
        except (RedisError, OSError):
            return False

    async def enqueue(self, entries: Iterable[tuple[uuid.UUID, uuid.UUID, int, datetime]]) -> int:
        """Push (job_id, queue_id, priority, queued_at) entries. Idempotent (ZADD NX)."""
        by_queue: dict[str, dict[str, float]] = {}
        for job_id, queue_id, priority, queued_at in entries:
            by_queue.setdefault(queue_key(queue_id), {})[str(job_id)] = dispatch_score(
                priority, queued_at
            )
        if not by_queue:
            return 0
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for key, mapping in by_queue.items():
                    pipe.zadd(key, mapping, nx=True)
                results = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc
        return int(sum(int(r) for r in results))

    async def pop(
        self, queue_ids: Sequence[uuid.UUID | str], timeout: int = 1
    ) -> tuple[str, str] | None:
        """Blocking pop across queues. Returns (queue_id, job_id) or None on timeout."""
        if not queue_ids:
            return None
        keys = [queue_key(q) for q in queue_ids]
        try:
            item = await self.redis.bzpopmin(keys, timeout=timeout)
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc
        if item is None:
            return None
        key, member, _score = item
        return str(key).removeprefix(QUEUE_KEY_PREFIX), str(member)

    async def requeue(self, queue_id: uuid.UUID | str, job_id: str, score: float) -> None:
        try:
            await self.redis.zadd(queue_key(queue_id), {job_id: score}, nx=True)
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc

    async def remove(self, queue_id: uuid.UUID | str, job_ids: Sequence[str]) -> int:
        if not job_ids:
            return 0
        try:
            return int(await self.redis.zrem(queue_key(queue_id), *job_ids))
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc

    async def missing(self, queue_id: uuid.UUID | str, job_ids: Sequence[str]) -> list[str]:
        """Return the subset of job_ids that are not present in the queue's dispatch set."""
        if not job_ids:
            return []
        key = queue_key(queue_id)
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for job_id in job_ids:
                    pipe.zscore(key, job_id)
                scores = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc
        return [job_id for job_id, score in zip(job_ids, scores, strict=True) if score is None]

    async def head(self, queue_id: uuid.UUID | str, count: int) -> list[str]:
        try:
            return [str(m) for m in await self.redis.zrange(queue_key(queue_id), 0, count - 1)]
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc

    async def depth(self, queue_ids: Sequence[uuid.UUID | str]) -> dict[str, int]:
        if not queue_ids:
            return {}
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for q in queue_ids:
                    pipe.zcard(queue_key(q))
                counts = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc
        return {str(q): int(c) for q, c in zip(queue_ids, counts, strict=True)}

    async def delete_queue(self, queue_id: uuid.UUID | str) -> None:
        try:
            await self.redis.delete(queue_key(queue_id))
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc

    # --- events -------------------------------------------------------------------------

    async def publish(self, project_id: uuid.UUID | str, events: Sequence[dict[str, Any]]) -> None:
        if not events:
            return
        channel = f"{EVENTS_CHANNEL_PREFIX}{project_id}"
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for event in events:
                    pipe.publish(channel, json.dumps(event, default=str))
                await pipe.execute()
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc

    @asynccontextmanager
    async def subscribe(self, project_id: uuid.UUID | str) -> AsyncIterator[Any]:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(f"{EVENTS_CHANNEL_PREFIX}{project_id}")
        except (RedisError, OSError) as exc:
            await pubsub.aclose()
            raise DispatchUnavailable(str(exc)) from exc
        try:
            yield pubsub
        finally:
            try:
                await pubsub.unsubscribe()
            finally:
                await pubsub.aclose()

    # --- rate limiting ------------------------------------------------------------------

    async def hit(self, bucket: str, limit: int, window_seconds: int = 60) -> tuple[bool, int, int]:
        """Fixed-window counter. Returns (allowed, remaining, seconds_until_reset)."""
        window = int(time.time() // window_seconds)
        key = f"qf:rl:{bucket}:{window}"
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, window_seconds + 1)
                count, _ = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise DispatchUnavailable(str(exc)) from exc
        reset = window_seconds - int(time.time() % window_seconds)
        return int(count) <= limit, max(0, limit - int(count)), reset
