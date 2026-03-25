"""
infra/errors.py

Central typed error taxonomy for the trading system.

Every failure surface (account ops, fund ops, feed ops, execution) maps to one
of the enums here.  Callers catch specific exception types instead of bare
`except Exception`, log the `kind` field, and exit with the matching non-zero
code so orchestrators and check_session.py can react correctly.

Usage example
-------------
    from infra.errors import AccountError, AccountErrorKind
    try:
        creds = create_or_derive_api_creds()
    except ValueError as exc:
        raise AccountError(AccountErrorKind.BAD_CONFIG, str(exc)) from exc
"""
from __future__ import annotations

import enum
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Exit codes — keep in sync with any PowerShell / shell wrappers
# ---------------------------------------------------------------------------
EXIT_OK                    = 0
EXIT_AUTH_FAILED           = 10
EXIT_BALANCE_MISMATCH      = 11
EXIT_API_UNAVAILABLE       = 12
EXIT_BAD_CONFIG            = 13
EXIT_APPROVAL_TX_FAILED    = 20
EXIT_RPC_ERROR             = 21
EXIT_BAD_PRIVATE_KEY       = 22
EXIT_FEED_DEGRADED         = 30
EXIT_EXECUTION_FAILED      = 40
EXIT_UNKNOWN               = 99


# ---------------------------------------------------------------------------
# Error kinds
# ---------------------------------------------------------------------------

class AccountErrorKind(enum.Enum):
    AUTH_FAILED      = ("auth_failed",       EXIT_AUTH_FAILED)
    BALANCE_MISMATCH = ("balance_mismatch",   EXIT_BALANCE_MISMATCH)
    API_UNAVAILABLE  = ("api_unavailable",    EXIT_API_UNAVAILABLE)
    BAD_CONFIG       = ("bad_config",         EXIT_BAD_CONFIG)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class FundsOpErrorKind(enum.Enum):
    APPROVAL_TX_FAILED = ("approval_tx_failed", EXIT_APPROVAL_TX_FAILED)
    RPC_ERROR          = ("rpc_error",          EXIT_RPC_ERROR)
    BAD_PRIVATE_KEY    = ("bad_private_key",     EXIT_BAD_PRIVATE_KEY)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class FeedErrorKind(enum.Enum):
    WEBSOCKET_DEAD     = ("websocket_dead",     EXIT_FEED_DEGRADED)
    STALE_DATA         = ("stale_data",         EXIT_FEED_DEGRADED)
    SCHEMA_MISMATCH    = ("schema_mismatch",    EXIT_FEED_DEGRADED)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class ExecutionErrorKind(enum.Enum):
    ORDER_REJECTED     = ("order_rejected",     EXIT_EXECUTION_FAILED)
    TIMEOUT            = ("timeout",            EXIT_EXECUTION_FAILED)
    LEDGER_WRITE_FAIL  = ("ledger_write_fail",  EXIT_EXECUTION_FAILED)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class _TradingError(Exception):
    """Base class.  Always carries a typed kind and optional detail string."""

    def __init__(self, kind, detail: str = ""):
        self.kind   = kind
        self.detail = detail
        super().__init__(f"{kind.label}: {detail}" if detail else kind.label)

    @property
    def exit_code(self) -> int:
        return self.kind.exit_code

    def log_context(self) -> dict:
        """Return a dict suitable for structlog event fields."""
        return {
            "error_kind":  self.kind.label,
            "error_detail": self.detail,
            "exit_code":   self.exit_code,
        }


class AccountError(_TradingError):
    """Raised by account-status / credential operations."""
    def __init__(self, kind: AccountErrorKind, detail: str = ""):
        super().__init__(kind, detail)


class FundsOpError(_TradingError):
    """Raised by approve_funds / unlock_funds operations."""
    def __init__(self, kind: FundsOpErrorKind, detail: str = ""):
        super().__init__(kind, detail)


class FeedError(_TradingError):
    """Raised or logged when a data feed fails contract checks."""
    def __init__(self, kind: FeedErrorKind, detail: str = ""):
        super().__init__(kind, detail)


class ExecutionError(_TradingError):
    """Raised by execution-layer operations."""
    def __init__(self, kind: ExecutionErrorKind, detail: str = ""):
        super().__init__(kind, detail)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def exit_with_error(
    logger,
    event: str,
    error: _TradingError,
    extra: Optional[dict] = None,
) -> None:
    """
    Emit a structured structlog error event then sys.exit with the typed
    exit code.  Use this in top-level script entrypoints so orchestrators
    get a machine-readable exit code rather than a traceback.

    Parameters
    ----------
    logger  : structlog bound logger
    event   : log event name (e.g. "account_status_failed")
    error   : the _TradingError instance
    extra   : optional additional fields to merge into the log event
    """
    fields = {**error.log_context(), **(extra or {})}
    logger.error(event, **fields)
    sys.exit(error.exit_code)
