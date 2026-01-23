"""Retry helper for operational errors with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Optional, TypeVar

from services.error_codes import OperationalError

T = TypeVar("T")


class RetryableOperation:
    """Execute a coroutine with retries for OperationalError only."""

    @staticmethod
    async def run(
        coro_factory: Callable[[], Awaitable[T]],
        *,
        max_retries: int = 3,
        base_delay: float = 0.25,
        max_delay: float = 5.0,
        jitter: float = 0.15,
    ) -> T:
        attempt = 0
        while True:
            try:
                return await coro_factory()
            except OperationalError:
                attempt += 1
                if attempt > max_retries:
                    raise
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
                if jitter > 0:
                    delay += random.uniform(0, delay * jitter)
                await asyncio.sleep(delay)
