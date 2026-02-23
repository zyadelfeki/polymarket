"""
Structured error taxonomy for the Polymarket bot.

Every exception in the system maps to one of these categories.
Catching bare ``Exception`` and logging "unknown error" is forbidden —
use the appropriate subclass so structured logs carry a meaningful error_type.

Usage::

    from shared.errors import ExecutionError, RiskError

    try:
        await place_order(...)
    except ExecutionError as e:
        logger.error("order_failed", error=str(e), error_type=type(e).__name__)
"""

from __future__ import annotations


class BotError(Exception):
    """Base class for all bot errors."""


class DataFeedError(BotError):
    """Market data unavailable or stale.

    Raised when:
    - Binance WebSocket is disconnected / no price update in > N seconds
    - Polymarket CLOB API returns 5xx
    - Gamma API call times out
    """


class ExecutionError(BotError):
    """Order placement or management failure.

    Raised when:
    - CLOB API rejects an order (invalid parameters, insufficient funds)
    - Post-submit fill check times out
    - Order cancellation fails
    """


class SettlementError(BotError):
    """Settlement resolution failure.

    Raised when:
    - A market result cannot be fetched for a resolved market
    - PnL computation fails due to missing payout data
    - DB write of settled result fails
    """


class RiskError(BotError):
    """Risk limit violation.

    Raised when:
    - Portfolio exposure cap would be exceeded
    - Drawdown kill switch is active
    - Circuit breaker is OPEN
    """


class ConfigError(BotError):
    """Bad or missing configuration.

    Raised when:
    - Required config keys are absent
    - Type mismatch in config values
    - Secrets unavailable
    """


class LedgerError(BotError):
    """Database write or integrity failure.

    Raised when:
    - SQLite write fails (disk full, lock timeout)
    - Double-entry validation fails (ledger unbalanced)
    - Migration fails
    """
