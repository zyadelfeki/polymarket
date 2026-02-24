"""
Service-level circuit breaker for external API calls.

DISTINCT from risk/circuit_breaker_v2.py (which handles trading risk limits).
This module wraps individual external service calls (Gamma API, CLOB API,
Binance, Charlie) and fast-fails when a service is consistently failing,
preventing cascading timeouts from blocking the main trading loop.

States:
  CLOSED    — normal operation; calls pass through
  OPEN      — consistently failing; calls are rejected immediately (returns None)
  HALF_OPEN — recovery probe; one call is allowed through to test recovery

Usage::

    breaker = ServiceCircuitBreaker("gamma_api", failure_threshold=3)

    result = await breaker.call(
        gamma_client.get_markets(),
        logger=logger,
        market_id="0xabc",
    )
    if result is None:
        # circuit is OPEN or call failed — skip this cycle
        ...
"""

from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Coroutine, Optional

import structlog

_log = structlog.get_logger(__name__)


class CircuitState(Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class ServiceCircuitBreaker:
    """
    Async circuit breaker for a single named external service.

    Parameters
    ----------
    name:
        Human-readable service name used in log messages (e.g. "gamma_api").
    failure_threshold:
        Number of consecutive failures before opening the circuit.
    recovery_timeout:
        Seconds to wait in OPEN state before allowing one probe call.
    half_open_max_calls:
        Number of probe calls allowed before declaring the service healthy.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self.name = name
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure_time: float = 0.0
        self.half_open_calls: int = 0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

    async def call(
        self,
        coro: Coroutine,
        logger=None,
        **log_ctx: Any,
    ) -> Optional[Any]:
        """
        Execute ``coro`` through the circuit breaker.

        Returns the coroutine's result on success, or ``None`` when:
        - The circuit is OPEN and recovery hasn't timed out yet.
        - The call fails (exception is caught and counted).
        """
        _logger = logger or _log
        now = time.monotonic()

        # --- State transitions -------------------------------------------
        if self.state == CircuitState.OPEN:
            if now - self.last_failure_time >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.half_open_calls = 0
                _logger.info(
                    "circuit_breaker_half_open",
                    breaker=self.name,
                    recovery_timeout=self.recovery_timeout,
                    **log_ctx,
                )
            else:
                _logger.warning(
                    "circuit_breaker_open_rejected",
                    breaker=self.name,
                    remaining_s=round(self.recovery_timeout - (now - self.last_failure_time), 1),
                    **log_ctx,
                )
                return None

        if self.state == CircuitState.HALF_OPEN:
            if self.half_open_calls >= self.half_open_max_calls:
                return None
            self.half_open_calls += 1

        # --- Execute -------------------------------------------------------
        try:
            result = await coro
            self._on_success(_logger, log_ctx)
            return result
        except asyncio.TimeoutError as exc:
            self._on_failure(exc, _logger, log_ctx)
            return None
        except Exception as exc:
            self._on_failure(exc, _logger, log_ctx)
            return None

    def _on_success(self, logger, log_ctx: dict) -> None:
        if self.state != CircuitState.CLOSED:
            logger.info(
                "circuit_breaker_recovered",
                breaker=self.name,
                prev_failures=self.failure_count,
                **log_ctx,
            )
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def _on_failure(self, exc: Exception, logger, log_ctx: dict) -> None:
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            if self.state != CircuitState.OPEN:
                logger.error(
                    "circuit_breaker_trip",
                    breaker=self.name,
                    failures=self.failure_count,
                    threshold=self.failure_threshold,
                    state=CircuitState.OPEN.value,
                    error=str(exc),
                    **log_ctx,
                )
            self.state = CircuitState.OPEN
        else:
            logger.warning(
                "circuit_breaker_failure",
                breaker=self.name,
                failures=self.failure_count,
                threshold=self.failure_threshold,
                state=self.state.value,
                error=str(exc),
                **log_ctx,
            )

    @property
    def status(self) -> str:
        return self.state.value

    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# Pre-built instances — one per external service used by the bot
# ---------------------------------------------------------------------------
cb_gamma   = ServiceCircuitBreaker("gamma_api",   failure_threshold=3, recovery_timeout=60.0)
cb_clob    = ServiceCircuitBreaker("clob_api",    failure_threshold=3, recovery_timeout=60.0)
cb_binance = ServiceCircuitBreaker("binance_api", failure_threshold=5, recovery_timeout=30.0)
cb_charlie = ServiceCircuitBreaker("charlie",     failure_threshold=3, recovery_timeout=45.0)
