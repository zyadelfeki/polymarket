from enum import Enum
from typing import Optional, Dict, Any


class ErrorCode(Enum):
    """Structured error codes for trading failures."""

    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_ORDER = "invalid_order"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INSUFFICIENT_CAPITAL = "insufficient_capital"
    ORDER_SUBMISSION_FAILED = "order_submission_failed"
    MARKET_NOT_FOUND = "market_not_found"
    NETWORK_TIMEOUT = "network_timeout"
    TIMEOUT = "timeout"
    API_UNAVAILABLE = "api_unavailable"
    API_5XX = "api_5xx"
    NETWORK_ERROR = "network_error"
    NOT_AUTHENTICATED = "not_authenticated"
    CIRCUIT_BREAKER_TRIPPED = "circuit_breaker_tripped"
    INVALID_STATE = "invalid_state"
    UNKNOWN = "unknown"


RETRYABLE_CODES = {
    ErrorCode.TIMEOUT,
    ErrorCode.NETWORK_TIMEOUT,
    ErrorCode.NETWORK_ERROR,
    ErrorCode.API_UNAVAILABLE,
    ErrorCode.API_5XX,
    ErrorCode.ORDER_SUBMISSION_FAILED,
}


class TradingException(Exception):
    """Base trading exception with structured error code."""

    def __init__(self, code: ErrorCode, message: str, metadata: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.metadata = metadata or {}

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "error_code": self.code.value,
            "metadata": self.metadata,
            "retryable": self.retryable,
        }


class ValidationError(TradingException):
    """Non-retryable validation error."""

    @property
    def retryable(self) -> bool:
        return False


class OperationalError(TradingException):
    """Retryable operational error (network, API, etc)."""

    @property
    def retryable(self) -> bool:
        return True


class CircuitBreakerTripped(TradingException):
    """Non-retryable circuit breaker error."""

    @property
    def retryable(self) -> bool:
        return False


class InsufficientCapital(TradingException):
    """Non-retryable insufficient capital error."""

    @property
    def retryable(self) -> bool:
        return False


class TradeError(OperationalError):
    """Backward-compatible trading exception alias."""

    def __init__(self, code: ErrorCode, message: str, retryable: Optional[bool] = None):
        super().__init__(code, message)
        if retryable is not None:
            self._override_retryable = retryable

    @property
    def retryable(self) -> bool:
        if hasattr(self, "_override_retryable"):
            return self._override_retryable
        return super().retryable
