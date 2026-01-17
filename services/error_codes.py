from enum import Enum


class ErrorCode(Enum):
    """Structured error codes for trading failures."""

    INVALID_PRICE = "invalid_price"
    INVALID_QUANTITY = "invalid_quantity"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    ORDER_SUBMISSION_FAILED = "order_submission_failed"
    MARKET_NOT_FOUND = "market_not_found"
    TIMEOUT = "timeout"
    API_UNAVAILABLE = "api_unavailable"
    NETWORK_ERROR = "network_error"
    NOT_AUTHENTICATED = "not_authenticated"
    INVALID_STATE = "invalid_state"
    UNKNOWN = "unknown"
