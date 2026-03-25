"""Tier 3: supervised_task — async task wrapper that emits a structured
'task_died' event when a background coroutine exits unexpectedly.

Usage in main.py::

    from infra.supervised_task import supervised_task

    asyncio.get_running_loop().create_task(
        supervised_task("binance_trade_feed", binance_feed.run(), logger)
    )

The wrapper:
- Awaits the coroutine.
- On any unhandled exception emits structlog event 'task_died' with
  task_name, error, error_type so check_session.py can surface it as CRITICAL.
- On clean exit (return) also emits 'task_exited_cleanly' at INFO level.
- Never swallows the exception silently.

This turns the current pattern
    asyncio.get_running_loop().create_task(some_coro.run())
into one that is visible to your entire observability stack without
requiring changes to the coroutines themselves.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable

try:
    import structlog as _structlog
    _default_logger = _structlog.get_logger(__name__)
except ImportError:
    import logging as _logging
    _default_logger = _logging.getLogger(__name__)  # type: ignore[assignment]


async def supervised_task(
    task_name: str,
    coro: Awaitable[Any],
    logger: Any = None,
) -> None:
    """Run *coro* and emit a structured 'task_died' event if it crashes.

    Parameters
    ----------
    task_name:
        Human-readable name surfaced in logs and check_session.py.
    coro:
        An awaitable (typically a long-running coroutine like feed.run()).
    logger:
        Optional structlog/logging logger; falls back to module-level logger.
    """
    _log = logger if logger is not None else _default_logger
    try:
        await coro
        _log.info(
            "task_exited_cleanly",
            task_name=task_name,
        )
    except asyncio.CancelledError:
        # Normal shutdown via cancel() — not a crash.
        _log.info("task_cancelled", task_name=task_name)
        raise  # re-raise so the event loop handles cancellation correctly
    except Exception as exc:  # noqa: BLE001
        _log.error(
            "task_died",
            task_name=task_name,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        # Do NOT re-raise: the task is already dead; raising here would make
        # asyncio log an extra 'Task exception was never retrieved' noise on top
        # of our already-structured event. The structured event is the signal.
