"""
services/task_supervisor.py

Lightweight async task supervisor.

Every long-running asyncio Task registered here is monitored.  If a task dies
(raises an unhandled exception) the supervisor:
  1. Emits a structured ``task_died`` log event (picked up by INV-5 in
     check_session.py as a CRITICAL invariant violation).
  2. Optionally restarts the task after a configurable back-off delay.
  3. After max_restarts attempts it emits ``task_restart_exhausted`` and stops
     trying, leaving the system in a degraded state rather than looping forever.

The BinanceWebSocketV2 health check is also polled on a separate cadence.  If
``health_check()`` returns False for ``binance_unhealthy_threshold`` consecutive
checks the supervisor emits ``binance_feed_unhealthy`` (picked up by INV-7).

Usage
-----
    supervisor = TaskSupervisor(logger=structlog.get_logger())

    # Register critical background tasks
    supervisor.register("market_resolution_monitor",
                        lambda: trading_system._market_resolution_monitor())
    supervisor.register("periodic_maintenance",
                        lambda: trading_system._periodic_maintenance())

    # Start the supervisor itself (blocks until cancelled)
    asyncio.create_task(supervisor.run())

    # Wire Binance ws feed for health polling
    supervisor.set_binance_ws(trading_system.websocket)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


_DEFAULT_RESTART_DELAY_SECONDS = 5.0
_DEFAULT_MAX_RESTARTS          = 5
_DEFAULT_HEALTH_POLL_SECONDS   = 30.0
_DEFAULT_BINANCE_UNHEALTHY_THRESHOLD = 3   # consecutive failed health checks


class _ManagedTask:
    __slots__ = (
        "name", "factory", "restart_delay", "max_restarts",
        "restart_count", "task", "last_died_at",
    )

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        restart_delay: float,
        max_restarts: int,
    ):
        self.name          = name
        self.factory       = factory
        self.restart_delay = restart_delay
        self.max_restarts  = max_restarts
        self.restart_count = 0
        self.task: Optional[asyncio.Task] = None
        self.last_died_at: float = 0.0


class TaskSupervisor:
    """
    Monitor and optionally auto-restart registered asyncio tasks.

    Parameters
    ----------
    logger            : structlog (or stdlib logging) logger instance
    poll_interval     : how often to check task liveness (seconds)
    health_poll_interval : how often to call binance_ws.health_check (seconds)
    binance_unhealthy_threshold : consecutive unhealthy polls before emitting
                                  ``binance_feed_unhealthy`` event
    """

    def __init__(
        self,
        logger,
        poll_interval: float = 5.0,
        health_poll_interval: float = _DEFAULT_HEALTH_POLL_SECONDS,
        binance_unhealthy_threshold: int = _DEFAULT_BINANCE_UNHEALTHY_THRESHOLD,
    ):
        self._log                        = logger
        self._poll_interval              = poll_interval
        self._health_poll_interval       = health_poll_interval
        self._binance_unhealthy_threshold = binance_unhealthy_threshold
        self._managed: List[_ManagedTask] = []
        self._binance_ws: Optional[Any]  = None
        self._binance_consecutive_fails  = 0
        self._running                    = False
        self._last_health_poll_at        = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        factory: Callable[[], Any],
        restart_delay: float = _DEFAULT_RESTART_DELAY_SECONDS,
        max_restarts: int    = _DEFAULT_MAX_RESTARTS,
    ) -> None:
        """
        Register a task factory.  ``factory`` is called with no arguments
        and must return an awaitable (coroutine or Task).  It will be called
        again on each restart.
        """
        managed = _ManagedTask(
            name=name,
            factory=factory,
            restart_delay=restart_delay,
            max_restarts=max_restarts,
        )
        managed.task = asyncio.ensure_future(factory())
        self._managed.append(managed)
        self._log.info(
            "task_supervisor_registered",
            task_name=name,
            max_restarts=max_restarts,
            restart_delay=restart_delay,
        )

    def set_binance_ws(self, ws) -> None:
        """Wire the BinanceWebSocketV2 instance for health polling."""
        self._binance_ws = ws

    async def run(self) -> None:
        """Main supervisor loop.  Run as a background asyncio Task."""
        self._running = True
        self._log.info("task_supervisor_started", tasks=len(self._managed))
        try:
            while self._running:
                await asyncio.sleep(self._poll_interval)
                await self._check_tasks()
                await self._check_binance_health()
        except asyncio.CancelledError:
            self._log.info("task_supervisor_stopped")
            raise

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _check_tasks(self) -> None:
        for managed in self._managed:
            task = managed.task
            if task is None or not task.done():
                continue  # still running — good

            exc = task.exception() if not task.cancelled() else None
            managed.last_died_at = time.monotonic()

            self._log.error(
                "task_died",
                task_name=managed.name,
                error=str(exc) if exc else "cancelled",
                error_type=type(exc).__name__ if exc else "CancelledError",
                restart_count=managed.restart_count,
                max_restarts=managed.max_restarts,
            )

            if managed.restart_count >= managed.max_restarts:
                self._log.error(
                    "task_restart_exhausted",
                    task_name=managed.name,
                    restart_count=managed.restart_count,
                )
                managed.task = None  # stop re-checking this task
                continue

            # Back-off and restart
            await asyncio.sleep(managed.restart_delay)
            managed.restart_count += 1
            try:
                managed.task = asyncio.ensure_future(managed.factory())
                self._log.warning(
                    "task_restarted",
                    task_name=managed.name,
                    attempt=managed.restart_count,
                    max_restarts=managed.max_restarts,
                )
            except Exception as restart_exc:
                self._log.error(
                    "task_restart_failed",
                    task_name=managed.name,
                    error=str(restart_exc),
                    error_type=type(restart_exc).__name__,
                )
                managed.task = None

    async def _check_binance_health(self) -> None:
        if self._binance_ws is None:
            return
        now = time.monotonic()
        if (now - self._last_health_poll_at) < self._health_poll_interval:
            return
        self._last_health_poll_at = now

        try:
            healthy = self._binance_ws.health_check()
        except Exception as exc:
            healthy = False
            self._log.warning(
                "task_supervisor_health_check_error",
                component="binance_ws",
                error=str(exc),
            )

        if not healthy:
            self._binance_consecutive_fails += 1
            if self._binance_consecutive_fails >= self._binance_unhealthy_threshold:
                # Emit the structured event that INV-7 tracks
                self._log.error(
                    "binance_feed_unhealthy",
                    consecutive_fails=self._binance_consecutive_fails,
                    threshold=self._binance_unhealthy_threshold,
                    action="supervisor_detected_feed_failure",
                )
        else:
            if self._binance_consecutive_fails > 0:
                self._log.info(
                    "binance_feed_recovered",
                    after_consecutive_fails=self._binance_consecutive_fails,
                )
            self._binance_consecutive_fails = 0
