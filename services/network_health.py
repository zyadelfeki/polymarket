#!/usr/bin/env python3
"""
Network health monitoring for detecting partitions.

Tracks the time since the last successful API call and flags
network partitions when the threshold is exceeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import structlog
    _structlog_available = True
except ImportError:
    structlog = None
    _structlog_available = False

from services.correlation_context import inject_correlation

if _structlog_available:
    logger = structlog.get_logger(__name__)
else:
    import logging

    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._logger = logging.getLogger(name)

        def _log(self, level, event: str, **kwargs):
            exc_info = kwargs.pop("exc_info", None)
            kwargs = inject_correlation(kwargs)
            message = f"{event} | {kwargs}" if kwargs else event
            self._logger.log(level, message, exc_info=exc_info)

        def debug(self, event: str, **kwargs):
            self._log(logging.DEBUG, event, **kwargs)

        def info(self, event: str, **kwargs):
            self._log(logging.INFO, event, **kwargs)

        def warning(self, event: str, **kwargs):
            self._log(logging.WARNING, event, **kwargs)

        def error(self, event: str, **kwargs):
            self._log(logging.ERROR, event, **kwargs)

        def critical(self, event: str, **kwargs):
            self._log(logging.CRITICAL, event, **kwargs)

    logger = _FallbackLogger(__name__)


DEFAULT_STARTUP_GRACE_SECONDS = 30


@dataclass
class NetworkPartitionState:
    initialized_at: datetime
    last_successful_api_call: datetime
    partition_threshold_seconds: int
    startup_grace_seconds: int = DEFAULT_STARTUP_GRACE_SECONDS
    has_successful_api_call: bool = False
    is_partitioned: bool = False
    last_failure: Optional[str] = None


class NetworkPartitionError(RuntimeError):
    """Raised when a network partition is detected and trading is halted."""


class NetworkHealthMonitor:
    """Detect network partitions before trading on stale data."""

    def __init__(
        self,
        partition_threshold_seconds: int = 15,
        startup_grace_seconds: int = DEFAULT_STARTUP_GRACE_SECONDS,
    ):
        now = self._utc_now()
        self.state = NetworkPartitionState(
            initialized_at=now,
            last_successful_api_call=now,
            partition_threshold_seconds=partition_threshold_seconds,
            startup_grace_seconds=startup_grace_seconds,
        )
        logger.info(
            "network_health_monitor_initialized",
            threshold_seconds=partition_threshold_seconds,
            startup_grace_seconds=startup_grace_seconds,
            initialized_at=now.isoformat(),
        )

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def record_success(self) -> None:
        self.state.last_successful_api_call = self._utc_now()
        self.state.has_successful_api_call = True
        self.state.last_failure = None
        if self.state.is_partitioned:
            logger.info("network_recovered")
            self.state.is_partitioned = False

    def record_failure(self, reason: Optional[str] = None) -> None:
        self.state.last_failure = reason

    def check_partition(self) -> bool:
        now = self._utc_now()
        if not self.state.has_successful_api_call:
            normalized_last_success = self._normalize_utc(self.state.last_successful_api_call)
            initialized_at = self._normalize_utc(self.state.initialized_at)
            if normalized_last_success < initialized_at:
                elapsed = (now - normalized_last_success).total_seconds()
            else:
                startup_elapsed = (now - initialized_at).total_seconds()
                if startup_elapsed <= self.state.startup_grace_seconds:
                    return False
                elapsed = startup_elapsed
        else:
            elapsed = (now - self._normalize_utc(self.state.last_successful_api_call)).total_seconds()

        if elapsed > self.state.partition_threshold_seconds and not self.state.is_partitioned:
            logger.critical(
                "network_partition_detected",
                elapsed_seconds=elapsed,
                threshold_seconds=self.state.partition_threshold_seconds,
                startup_grace_seconds=self.state.startup_grace_seconds,
                has_successful_api_call=self.state.has_successful_api_call,
                last_failure=self.state.last_failure,
            )
            self.state.is_partitioned = True
        return self.state.is_partitioned

    def time_since_success(self) -> timedelta:
        return self._utc_now() - self._normalize_utc(self.state.last_successful_api_call)