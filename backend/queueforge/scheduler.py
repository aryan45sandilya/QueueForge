"""Scheduler process: promotion, recurring schedules, recovery, reconciliation, retention.

Several schedulers may run at once (rows are claimed with SKIP LOCKED; Redis writes are
idempotent), but one is enough. Run with `queueforge scheduler`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from queueforge.config import Settings, get_settings
from queueforge.db import Database, make_engine
from queueforge.dispatch import Dispatcher, make_redis
from queueforge.logs import configure_logging, get_logger
from queueforge.services import maintenance
from queueforge.worker import install_signal_handlers

log = get_logger("queueforge.scheduler")


class Scheduler:
    def __init__(self, settings: Settings, db: Database, dispatcher: Dispatcher) -> None:
        self.settings = settings
        self.db = db
        self.dispatcher = dispatcher
        self.stop_event = asyncio.Event()

    async def _guard(self, name: str, fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await fn()
        except Exception as exc:
            log.warning(
                "scheduler_task_failed", task=name, error=type(exc).__name__, detail=str(exc)[:200]
            )
            return None

    async def tick(self) -> None:
        """Frequent work: due jobs and schedules."""
        while (
            await self._guard("promote", lambda: maintenance.promote_due(self.db, self.dispatcher))
            == 500
        ):
            pass  # keep draining large backlogs within one tick
        await self._guard(
            "schedules", lambda: maintenance.materialize_schedules(self.db, self.dispatcher)
        )

    async def reconcile(self) -> None:
        await self._guard(
            "leases", lambda: maintenance.recover_expired_leases(self.db, self.dispatcher)
        )
        await self._guard(
            "workers",
            lambda: maintenance.mark_lost_workers(self.db, self.settings.heartbeat_seconds),
        )
        await self._guard(
            "dispatch",
            lambda: maintenance.reconcile_dispatch(
                self.db, self.dispatcher, self.settings.reconcile_grace_seconds
            ),
        )

    async def retention(self) -> None:
        await self._guard("retention", lambda: maintenance.enforce_retention(self.db))

    async def run(self) -> None:
        log.info(
            "scheduler_started",
            tick=self.settings.scheduler_tick_seconds,
            reconcile_every=self.settings.reconcile_interval_seconds,
        )
        last_reconcile = 0.0
        last_retention = 0.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            await self.tick()
            if started - last_reconcile >= self.settings.reconcile_interval_seconds:
                last_reconcile = started
                await self.reconcile()
            if started - last_retention >= self.settings.retention_interval_seconds:
                last_retention = started
                await self.retention()
            remaining = self.settings.scheduler_tick_seconds - (time.monotonic() - started)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.05, remaining))
        log.info("scheduler_stopped")


async def run_scheduler(settings: Settings) -> None:
    db = Database(make_engine(settings, pool_size=5))
    dispatcher = Dispatcher(make_redis(settings.redis_url))
    scheduler = Scheduler(settings, db, dispatcher)
    install_signal_handlers(None, scheduler.stop_event)
    try:
        await scheduler.run()
    finally:
        await dispatcher.redis.aclose()
        await db.dispose()


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    asyncio.run(run_scheduler(settings))


if __name__ == "__main__":
    main()
