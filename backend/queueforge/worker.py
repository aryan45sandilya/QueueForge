"""Worker process: pops job IDs from Redis, claims them in PostgreSQL, runs handlers.

Run with `queueforge worker` (or `python -m queueforge.worker`). Workers are independent of the
API and of each other; any number can run against the same database and Redis.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import os
import random
import signal
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psutil
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from queueforge import __version__
from queueforge.config import Settings, get_settings
from queueforge.db import Database, make_engine
from queueforge.dispatch import Dispatcher, DispatchUnavailable, dispatch_score, make_redis
from queueforge.handlers import JobContext, PermanentError, get_handler
from queueforge.handlers.base import validation_details
from queueforge.lifecycle import AttemptOutcome
from queueforge.logs import configure_logging, get_logger
from queueforge.models import Queue, Worker
from queueforge.services.common import AfterCommit
from queueforge.services.execution import (
    Claim,
    ClaimResult,
    ClaimStatus,
    Outcome,
    build_error,
    claim_job,
    claim_next_from_db,
    complete_job,
    release_job,
    renew_leases,
)

log = get_logger("queueforge.worker")

DB_ERRORS = (SQLAlchemyError, OSError, ConnectionError, asyncio.TimeoutError)


@dataclass
class Running:
    claim: Claim
    task: asyncio.Task[None]
    handler_task: asyncio.Task[dict[str, Any]] | None = None
    stop_reason: str | None = None  # "cancel" | "lost" | "shutdown"


@dataclass
class WorkerStats:
    claimed: int = 0
    succeeded: int = 0
    failed: int = 0
    stale_pops: int = 0
    requeued: int = 0
    lease_lost: int = 0
    mode_switches: int = 0
    timings: list[float] = field(default_factory=list)


class WorkerProcess:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        dispatcher: Dispatcher,
        *,
        concurrency: int | None = None,
        queue_names: list[str] | None = None,
        worker_id: str | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.dispatcher = dispatcher
        self.concurrency = concurrency or settings.worker_concurrency
        self.queue_names = queue_names if queue_names is not None else list(settings.worker_queues)
        host = socket.gethostname()[:64]
        self.id = worker_id or f"{host}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.hostname = host
        self.rng = rng or random.Random()
        self.running: dict[uuid.UUID, Running] = {}
        self.slots = asyncio.Semaphore(self.concurrency)
        self.queue_ids: list[uuid.UUID] = []
        self.queue_backoff: dict[uuid.UUID, float] = {}
        self.mode = "redis"
        self.state = "running"
        self.stop_event = asyncio.Event()
        self.stats = WorkerStats()
        self._process = psutil.Process()
        self.log = log.bind(worker_id=self.id)

    # --- lifecycle --------------------------------------------------------------------------

    async def register(self) -> None:
        async with self.db.transaction() as session:
            session.add(
                Worker(
                    id=self.id,
                    hostname=self.hostname,
                    pid=os.getpid(),
                    version=__version__,
                    queues=self.queue_names,
                    concurrency=self.concurrency,
                    state="running",
                    dispatch_mode=self.mode,
                )
            )
        self.log.info(
            "worker_registered", concurrency=self.concurrency, queues=self.queue_names or "all"
        )

    async def run(self) -> None:
        await self.register()
        await self.refresh_queues()
        background = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._queue_refresh_loop(), name="queue-refresh"),
        ]
        try:
            await self._dispatch_loop()
        finally:
            await self._drain()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await self._mark_stopped()

    def request_stop(self) -> None:
        if not self.stop_event.is_set():
            self.log.info("worker_stopping", running=len(self.running))
            self.stop_event.set()

    async def _drain(self) -> None:
        self.state = "draining"
        await self._safe_heartbeat()
        if self.running:
            self.log.info(
                "worker_draining",
                running=len(self.running),
                grace_seconds=self.settings.shutdown_grace_seconds,
            )
            tasks = [r.task for r in self.running.values()]
            _, pending = await asyncio.wait(tasks, timeout=self.settings.shutdown_grace_seconds)
            if pending:
                for job_id, r in list(self.running.items()):
                    if not r.task.done():
                        self._stop_job(job_id, "shutdown")
                await asyncio.wait(pending, timeout=10)

    async def _mark_stopped(self) -> None:
        try:
            async with self.db.transaction() as session:
                await session.execute(
                    update(Worker)
                    .where(Worker.id == self.id)
                    .values(state="stopped", stopped_at=func.now(), running_jobs=0)
                )
        except DB_ERRORS as exc:
            self.log.warning("worker_stop_record_failed", error=type(exc).__name__)
        self.log.info("worker_stopped", processed=self.stats.succeeded + self.stats.failed)

    # --- queues -----------------------------------------------------------------------------

    async def refresh_queues(self) -> None:
        stmt = select(Queue.id).where(Queue.paused.is_(False))
        if self.queue_names:
            stmt = stmt.where(Queue.name.in_(self.queue_names))
        async with self.db.session() as session:
            self.queue_ids = list((await session.execute(stmt)).scalars().all())

    async def _queue_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.queue_refresh_seconds)
            try:
                await self.refresh_queues()
            except DB_ERRORS as exc:
                self.log.warning("queue_refresh_failed", error=type(exc).__name__)

    def _eligible_queues(self) -> list[uuid.UUID]:
        now = time.monotonic()
        eligible = [q for q in self.queue_ids if self.queue_backoff.get(q, 0) <= now]
        self.rng.shuffle(eligible)  # fairness across projects/queues for BZPOPMIN key order
        return eligible

    # --- dispatch ---------------------------------------------------------------------------

    async def _dispatch_loop(self) -> None:
        last_redis_probe = 0.0
        while not self.stop_event.is_set():
            if not await self._acquire_slot_or_stop():
                break
            try:
                queues = self._eligible_queues()
                if not queues:
                    self.slots.release()
                    await self._sleep(0.5)
                    continue
                if self.mode == "postgres" and time.monotonic() - last_redis_probe > 5:
                    last_redis_probe = time.monotonic()
                    if await self.dispatcher.ping():
                        self._set_mode("redis")
                started = await (
                    self._dispatch_redis(queues)
                    if self.mode == "redis"
                    else self._dispatch_postgres(queues)
                )
                if not started:
                    self.slots.release()
            except DB_ERRORS as exc:
                self.slots.release()
                self.log.warning("dispatch_db_error", error=type(exc).__name__)
                await self._sleep(1.0)
            except Exception:
                self.slots.release()
                self.log.exception("dispatch_error")
                await self._sleep(1.0)

    async def _acquire_slot_or_stop(self) -> bool:
        """Wait for a free slot, but return False promptly if shutdown is requested meanwhile."""
        acquire = asyncio.create_task(self.slots.acquire())
        stop = asyncio.create_task(self.stop_event.wait())
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not acquire.done():
            acquire.cancel()
            try:
                await acquire
            except asyncio.CancelledError:
                return False
        if self.stop_event.is_set():
            self.slots.release()
            return False
        return True

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.stats.mode_switches += 1
            self.log.warning("dispatch_mode_changed", mode=mode)
            self.mode = mode

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)

    async def _dispatch_redis(self, queues: list[uuid.UUID]) -> bool:
        try:
            popped = await self.dispatcher.pop(queues, timeout=1)
        except DispatchUnavailable:
            self._set_mode("postgres")
            return False
        if popped is None:
            return False
        queue_raw, job_raw = popped
        try:
            job_id = uuid.UUID(job_raw)
        except ValueError:
            return False
        after = AfterCommit()
        try:
            result = await claim_job(self.db, job_id, self.id, self.settings.lease_seconds, after)
        except DB_ERRORS:
            # Put it back; if that fails too, the reconciler restores it from PostgreSQL.
            await self._requeue(queue_raw, job_raw, None)
            raise
        return await self._handle_claim(result, queue_raw, job_raw, after)

    async def _dispatch_postgres(self, queues: list[uuid.UUID]) -> bool:
        after = AfterCommit()
        result = await claim_next_from_db(
            self.db, queues, self.id, self.settings.lease_seconds, after
        )
        if result is None:
            await self._sleep(0.5)
            return False
        return await self._handle_claim(result, None, None, after)

    async def _handle_claim(
        self, result: ClaimResult, queue_raw: str | None, job_raw: str | None, after: AfterCommit
    ) -> bool:
        if result.status == ClaimStatus.CLAIMED:
            assert result.claim is not None
            await after.apply(self.dispatcher if self.mode == "redis" else None)
            self._start(result.claim)
            return True
        if result.status == ClaimStatus.STALE:
            self.stats.stale_pops += 1
            return False
        # Paused or at its concurrency limit: give the ID back and stop popping this queue briefly.
        if queue_raw and job_raw:
            await self._requeue(queue_raw, job_raw, result.score_hint)
            delay = 2.0 if result.status == ClaimStatus.PAUSED else 0.25
            self.queue_backoff[uuid.UUID(queue_raw)] = time.monotonic() + delay
        return False

    async def _requeue(
        self, queue_raw: str, job_raw: str, hint: tuple[int, datetime] | None
    ) -> None:
        priority, queued_at = hint if hint else (5, datetime.now(UTC))
        score = dispatch_score(priority, queued_at)
        try:
            await self.dispatcher.requeue(queue_raw, job_raw, score)
            self.stats.requeued += 1
        except DispatchUnavailable:
            pass

    # --- execution --------------------------------------------------------------------------

    def _start(self, claim: Claim) -> None:
        self.stats.claimed += 1
        task = asyncio.create_task(self._execute(claim), name=f"job-{claim.job_id}")
        self.running[claim.job_id] = Running(claim=claim, task=task)
        task.add_done_callback(functools.partial(self._finished, claim.job_id))

    def _finished(self, job_id: uuid.UUID, _task: asyncio.Task[None] | None = None) -> None:
        self.running.pop(job_id, None)
        self.slots.release()

    def _stop_job(self, job_id: uuid.UUID, reason: str) -> None:
        running = self.running.get(job_id)
        if running is None or running.stop_reason is not None:
            return
        running.stop_reason = reason
        if running.handler_task is not None and not running.handler_task.done():
            running.handler_task.cancel()

    async def _execute(self, claim: Claim) -> None:
        jlog = self.log.bind(job_id=str(claim.job_id), job_type=claim.type, attempt=claim.attempt)
        running = self.running.get(claim.job_id)
        started = time.perf_counter()
        spec = get_handler(claim.type)
        outcome: Outcome
        disabled = claim.type in self.settings.disabled_job_types
        if spec is None or disabled:
            outcome = Outcome(
                AttemptOutcome.FAILED,
                error=build_error(
                    None,
                    error_type="UnknownJobType",
                    message=(
                        f"Job type {claim.type!r} is disabled on this instance"
                        if disabled
                        else f"No handler registered for {claim.type!r} on this worker"
                    ),
                    retryable=False,
                    attempt=claim.attempt,
                ),
            )
        else:
            try:
                payload = spec.payload_model.model_validate(claim.payload)
            except ValidationError as exc:
                outcome = Outcome(
                    AttemptOutcome.FAILED,
                    error={
                        **build_error(
                            exc,
                            error_type="PayloadValidationError",
                            message=f"Payload no longer valid: {validation_details(exc)[:3]}",
                            retryable=False,
                            attempt=claim.attempt,
                            include_traceback=False,
                        )
                    },
                )
            else:
                ctx = JobContext(
                    job_id=claim.job_id,
                    project_id=claim.project_id,
                    attempt=claim.attempt,
                    max_attempts=claim.max_attempts,
                    db=self.db,
                    logger=jlog,
                    settings=self.settings,
                )
                handler_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(
                    spec.run(payload, ctx)
                )
                if running is not None:
                    running.handler_task = handler_task
                    if running.stop_reason is not None:  # cancelled before the handler started
                        handler_task.cancel()
                jlog.info("job_started")
                outcome_or_none = await self._await_handler(claim, handler_task, running)
                if outcome_or_none is None:
                    return
                outcome = outcome_or_none
        await self._record(claim, outcome, jlog, time.perf_counter() - started)

    async def _await_handler(
        self,
        claim: Claim,
        handler_task: asyncio.Task[dict],
        running: Running | None,
    ) -> Outcome | None:
        try:
            async with asyncio.timeout(claim.timeout_seconds):
                result = await handler_task
            return Outcome(
                AttemptOutcome.SUCCEEDED,
                result=result if isinstance(result, dict) else {"value": result},
            )
        except TimeoutError:
            if not handler_task.done():
                handler_task.cancel()
            return Outcome(
                AttemptOutcome.TIMED_OUT,
                error=build_error(
                    None,
                    error_type="Timeout",
                    message=f"Handler exceeded its {claim.timeout_seconds}s timeout",
                    retryable=True,
                    attempt=claim.attempt,
                ),
            )
        except asyncio.CancelledError:
            reason = running.stop_reason if running else None
            if reason is None:
                raise  # this task itself was cancelled (process exiting)
            if reason == "cancel":
                return Outcome(
                    AttemptOutcome.CANCELLED,
                    error={
                        "type": "Cancelled",
                        "message": "Cancelled by request while running",
                        "retryable": False,
                        "attempt": claim.attempt,
                    },
                )
            if reason == "shutdown":
                after = AfterCommit()
                try:
                    await release_job(self.db, claim, self.id, after)
                    await after.apply(self.dispatcher)
                    self.log.info("job_released", job_id=str(claim.job_id))
                except DB_ERRORS as exc:
                    self.log.warning(
                        "job_release_failed", job_id=str(claim.job_id), error=type(exc).__name__
                    )
                return None
            # lost: another worker may own it now; never write an outcome.
            self.stats.lease_lost += 1
            return None
        except PermanentError as exc:
            return Outcome(
                AttemptOutcome.FAILED,
                error=build_error(exc, retryable=False, attempt=claim.attempt),
            )
        except Exception as exc:
            return Outcome(
                AttemptOutcome.FAILED, error=build_error(exc, retryable=True, attempt=claim.attempt)
            )

    async def _record(self, claim: Claim, outcome: Outcome, jlog: Any, elapsed: float) -> None:
        deadline = time.monotonic() + self.settings.lease_seconds
        delay = 0.25
        while True:
            after = AfterCommit()
            try:
                state = await complete_job(
                    self.db,
                    claim,
                    self.id,
                    outcome,
                    max_result_bytes=self.settings.max_result_bytes,
                    rng=self.rng,
                    after=after,
                )
                break
            except DB_ERRORS as exc:
                if time.monotonic() > deadline:
                    # Give up; the lease expires and the reconciler re-runs the job (at-least-once).
                    jlog.error("outcome_write_abandoned", error=type(exc).__name__)
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5)
        await after.apply(self.dispatcher)
        if state is None:
            self.stats.lease_lost += 1
            jlog.warning("lease_lost_result_discarded", outcome=outcome.kind)
            return
        if outcome.kind == AttemptOutcome.SUCCEEDED:
            self.stats.succeeded += 1
        else:
            self.stats.failed += 1
        self.stats.timings.append(elapsed)
        if len(self.stats.timings) > 1000:
            del self.stats.timings[:500]
        jlog.info(
            "job_finished",
            outcome=outcome.kind,
            new_state=state,
            duration_ms=round(elapsed * 1000, 1),
            error_type=(outcome.error or {}).get("type"),
        )

    # --- heartbeat --------------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self._safe_heartbeat()

    async def _safe_heartbeat(self) -> None:
        attempts = {job_id: r.claim.attempt for job_id, r in self.running.items()}
        try:
            rss = self._process.memory_info().rss
        except psutil.Error:
            rss = None
        try:
            status = await renew_leases(
                self.db,
                self.id,
                attempts,
                self.settings.lease_seconds,
                heartbeat={
                    "running_jobs": len(self.running),
                    "state": self.state,
                    "dispatch_mode": self.mode,
                    "rss_bytes": rss,
                },
            )
        except DB_ERRORS as exc:
            self.log.warning("heartbeat_failed", error=type(exc).__name__)
            return
        for job_id in status.cancel_requested:
            self.log.info("job_cancel_requested", job_id=str(job_id))
            self._stop_job(job_id, "cancel")
        for job_id in status.lost:
            if job_id in self.running:
                self.log.warning("job_lease_lost", job_id=str(job_id))
                self._stop_job(job_id, "lost")


def install_signal_handlers(worker: WorkerProcess | None, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def handle(*_: object) -> None:
        loop.call_soon_threadsafe(stop.set)
        if worker is not None:
            loop.call_soon_threadsafe(worker.request_stop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, handle)  # Windows


async def run_worker(settings: Settings, concurrency: int | None, queues: list[str] | None) -> None:
    db = Database(
        make_engine(
            settings, pool_size=max(5, min(30, (concurrency or settings.worker_concurrency) + 4))
        )
    )
    dispatcher = Dispatcher(make_redis(settings.redis_url))
    worker = WorkerProcess(settings, db, dispatcher, concurrency=concurrency, queue_names=queues)
    install_signal_handlers(worker, worker.stop_event)
    try:
        await worker.run()
    finally:
        await dispatcher.redis.aclose()
        await db.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="queueforge worker", description="Run a QueueForge worker process"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Concurrent jobs (default QF_WORKER_CONCURRENCY)",
    )
    parser.add_argument(
        "--queues", default=None, help="Comma-separated queue names (default: all queues)"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    queues = [q.strip() for q in args.queues.split(",") if q.strip()] if args.queues else None
    asyncio.run(run_worker(settings, args.concurrency, queues))


if __name__ == "__main__":
    main()
