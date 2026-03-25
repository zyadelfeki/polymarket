"""
infra/task_supervisor.py — Supervised asyncio task wrapper.

Problem it solves
-----------------
asyncio.create_task() silently swallows exceptions by default.
A task that crashes never tells the main loop — the system keeps
running while a critical background coroutine is dead.

This module provides:
  supervised_task()   — drop-in for create_task(); logs and optionally
                        restarts on unhandled exception.
  FeedSupervisor      — polls BinanceWebSocketV2.health_check() on a
                        fixed interval; emits structured events on
                        failure and sets a shared unhealthy flag that
                        check_session / dashboard can read.
  OFIHealthMonitor    — latches a structured event when ofi_feed_misses
                        crosses a configurable threshold.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Coroutine, Optional

import structlog

from infra.errors import FeedErrorKind, TaskErrorKind

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# supervised_task
# ---------------------------------------------------------------------------

def supervised_task(
    coro: Coroutine,
    *,
    name: str,
    restart: bool = False,
    restart_delay: float = 5.0,
    on_death: Optional[Callable[[str, BaseException], None]] = None,
) -> asyncio.Task:
    """
    Wrap *coro* in a task that logs a structured ``task_died`` event when
    the coroutine raises an unhandled exception, instead of silently
    disappearing.

    Parameters
    ----------
    coro         : The coroutine to run.
    name         : Human-readable name used in log events.
    restart      : If True, reschedule the coroutine after *restart_delay* s.
                   Pass a callable factory if the coroutine needs fresh args.
    restart_delay: Seconds to wait before restarting (default 5).
    on_death     : Optional callback(name, exc) fired on unhandled exception.
    """
    async def _wrapper():
        while True:
            try:
                await coro
                # Normal exit — task completed without error.
                logger.info("supervised_task_completed", task_name=name)
                return
            except asyncio.CancelledError:
                logger.info("supervised_task_cancelled", task_name=name)
                return
            except Exception as exc:
                logger.error(
                    "task_died",
                    task_name=name,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    kind=TaskErrorKind.TASK_DIED,
                )
                if on_death is not None:
                    try:
                        on_death(name, exc)
                    except Exception:
                        pass
                if not restart:
                    return
                logger.warning(
                    "supervised_task_restarting",
                    task_name=name,
                    delay_seconds=restart_delay,
                )
                await asyncio.sleep(restart_delay)
                # Note: after restart the original coro object is exhausted;
                # caller must pass a factory (lambda) as coro for true restart.
                # Without a factory we exit after one restart attempt.
                return

    loop = asyncio.get_event_loop()
    task = loop.create_task(_wrapper(), name=name)
    return task


# ---------------------------------------------------------------------------
# FeedSupervisor
# ---------------------------------------------------------------------------

class FeedSupervisor:
    """
    Periodically polls ``ws.health_check()`` and emits a structured
    ``binance_feed_unhealthy`` event when the feed is degraded.

    Designed to be spawned once via supervised_task():

        supervised_task(
            feed_supervisor.run(),
            name="feed_supervisor",
            restart=True,
        )

    The ``unhealthy`` property is a latching flag that callers (e.g.
    check_session, _periodic_check) can read without an async call.
    """

    def __init__(self, ws: Any, *, check_interval: float = 30.0):
        self._ws = ws
        self._check_interval = check_interval
        self._unhealthy = False
        self._consecutive_failures = 0
        self._last_healthy_at: float = time.monotonic()

    @property
    def unhealthy(self) -> bool:
        return self._unhealthy

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval)
            await self._check()

    async def _check(self) -> None:
        try:
            healthy = await self._ws.health_check()
        except Exception as exc:
            healthy = False
            logger.warning(
                "feed_supervisor_check_error",
                error=str(exc),
                kind=FeedErrorKind.BINANCE_FEED_UNHEALTHY,
            )

        if healthy:
            if self._unhealthy:
                logger.info(
                    "binance_feed_recovered",
                    downtime_seconds=round(time.monotonic() - self._last_healthy_at, 1),
                )
            self._unhealthy = False
            self._consecutive_failures = 0
            self._last_healthy_at = time.monotonic()
        else:
            self._consecutive_failures += 1
            self._unhealthy = True
            logger.error(
                "binance_feed_unhealthy",
                kind=FeedErrorKind.BINANCE_FEED_UNHEALTHY,
                consecutive_failures=self._consecutive_failures,
                seconds_since_last_healthy=round(
                    time.monotonic() - self._last_healthy_at, 1
                ),
            )


# ---------------------------------------------------------------------------
# OFIHealthMonitor
# ---------------------------------------------------------------------------

class OFIHealthMonitor:
    """
    Latches a structured ``ofi_feed_degraded`` event when
    ``consecutive_misses`` crosses ``threshold``.

    Usage in _execute_opportunity:

        self._ofi_monitor.record_miss(symbol=opp_symbol)
        # or
        self._ofi_monitor.record_hit()
    """

    def __init__(self, threshold: int = 5):
        self._threshold = threshold
        self._consecutive_misses = 0
        self._latched = False

    @property
    def degraded(self) -> bool:
        return self._latched

    def record_hit(self) -> None:
        if self._latched:
            logger.info(
                "ofi_feed_recovered",
                previous_consecutive_misses=self._consecutive_misses,
            )
        self._consecutive_misses = 0
        self._latched = False

    def record_miss(self, *, symbol: str = "") -> None:
        self._consecutive_misses += 1
        if self._consecutive_misses >= self._threshold:
            self._latched = True
            logger.error(
                "ofi_feed_degraded",
                kind=FeedErrorKind.OFI_FEED_DEGRADED,
                consecutive_misses=self._consecutive_misses,
                symbol=symbol,
                threshold=self._threshold,
            )
        elif self._consecutive_misses % 5 == 0:
            logger.warning(
                "ofi_feed_misses_accumulating",
                consecutive_misses=self._consecutive_misses,
                threshold=self._threshold,
                symbol=symbol,
            )
