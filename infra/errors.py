"""
infra/errors.py

Central typed error taxonomy for the trading system.

Every failure surface (account ops, fund ops, feed ops, execution) maps to one
of the enums here.  Callers use typed raises instead of bare `except Exception`,
log the `kind` field via .log_context(), and exit with the matching non-zero
code so orchestrators and check_session.py can react correctly.

Exit code reference
-------------------
  0   = success
  10  = auth_failed           (bad key, creds derivation failed)
  11  = bad_config            (missing env var, import error)
  12  = api_unavailable       (CLOB / network down)
  13  = balance_mismatch      (API reports $0, parse error, accounting diverge)
  14  = bad_private_key       (key missing or empty)
  20  = approval_tx_failed    (on-chain approval reverted)
  21  = rpc_error             (Web3 / Polygon RPC failure)
  22  = insufficient_matic    (gas fee shortage)
  23  = proxy_mismatch        (proxy address wrong or $0 balance)
  30  = feed_degraded         (WebSocket dead, stale data, schema mismatch)
  40  = execution_failed      (order rejected, timeout, ledger write)
  99  = unknown

Usage
-----
    from infra.errors import AccountError, AccountErrorKind, exit_for_account_error

    try:
        creds = create_or_derive_api_creds()
    except SomeSpecificError as exc:
        err = AccountError(AccountErrorKind.AUTH_FAILED, str(exc), original=exc)
        logger.error("account_status_failed", **err.log_context())
        exit_for_account_error(err)
"""
from __future__ import annotations

import enum
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------
EXIT_OK                 = 0
EXIT_AUTH_FAILED        = 10
EXIT_BAD_CONFIG         = 11
EXIT_API_UNAVAILABLE    = 12
EXIT_BALANCE_MISMATCH   = 13
EXIT_BAD_PRIVATE_KEY    = 14
EXIT_APPROVAL_TX_FAILED = 20
EXIT_RPC_ERROR          = 21
EXIT_INSUFFICIENT_MATIC = 22
EXIT_PROXY_MISMATCH     = 23
EXIT_FEED_DEGRADED      = 30
EXIT_EXECUTION_FAILED   = 40
EXIT_UNKNOWN            = 99


# ---------------------------------------------------------------------------
# Error kind enums
# ---------------------------------------------------------------------------

class AccountErrorKind(enum.Enum):
    """
    Typed reasons an account-status / credential operation can fail.
    Each member carries (label, exit_code).
    """
    AUTH_FAILED      = ("auth_failed",      EXIT_AUTH_FAILED)
    BAD_CONFIG       = ("bad_config",       EXIT_BAD_CONFIG)
    API_UNAVAILABLE  = ("api_unavailable",  EXIT_API_UNAVAILABLE)
    BALANCE_MISMATCH = ("balance_mismatch", EXIT_BALANCE_MISMATCH)
    BAD_PRIVATE_KEY  = ("bad_private_key",  EXIT_BAD_PRIVATE_KEY)
    UNKNOWN          = ("unknown",          EXIT_UNKNOWN)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class FundsOpErrorKind(enum.Enum):
    """
    Typed reasons an approve_funds / unlock_funds operation can fail.
    """
    AUTH_FAILED        = ("auth_failed",        EXIT_AUTH_FAILED)
    BAD_CONFIG         = ("bad_config",         EXIT_BAD_CONFIG)
    API_UNAVAILABLE    = ("api_unavailable",    EXIT_API_UNAVAILABLE)
    BAD_PRIVATE_KEY    = ("bad_private_key",    EXIT_BAD_PRIVATE_KEY)
    APPROVAL_TX_FAILED = ("approval_tx_failed", EXIT_APPROVAL_TX_FAILED)
    RPC_ERROR          = ("rpc_error",          EXIT_RPC_ERROR)
    INSUFFICIENT_MATIC = ("insufficient_matic", EXIT_INSUFFICIENT_MATIC)
    PROXY_MISMATCH     = ("proxy_mismatch",     EXIT_PROXY_MISMATCH)
    UNKNOWN            = ("unknown",            EXIT_UNKNOWN)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class FeedErrorKind(enum.Enum):
    """Typed reasons a data feed fails its contract checks."""
    WEBSOCKET_DEAD  = ("websocket_dead",  EXIT_FEED_DEGRADED)
    STALE_DATA      = ("stale_data",      EXIT_FEED_DEGRADED)
    SCHEMA_MISMATCH = ("schema_mismatch", EXIT_FEED_DEGRADED)
    UNKNOWN         = ("unknown",         EXIT_FEED_DEGRADED)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


class ExecutionErrorKind(enum.Enum):
    """Typed reasons an execution-layer operation can fail."""
    ORDER_REJECTED    = ("order_rejected",    EXIT_EXECUTION_FAILED)
    TIMEOUT           = ("timeout",           EXIT_EXECUTION_FAILED)
    LEDGER_WRITE_FAIL = ("ledger_write_fail", EXIT_EXECUTION_FAILED)
    UNKNOWN           = ("unknown",           EXIT_EXECUTION_FAILED)

    def __init__(self, label: str, exit_code: int):
        self.label     = label
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Exception base
# ---------------------------------------------------------------------------

class _TradingError(Exception):
    """
    Base class for all typed trading errors.

    Parameters
    ----------
    kind     : one of the *ErrorKind enum members
    detail   : human-readable detail string (safe to log)
    original : the original exception for __cause__ chaining so
               tracebacks stay useful when re-raising
    """

    def __init__(
        self,
        kind,
        detail: str = "",
        original: Optional[Exception] = None,
    ):
        self.kind     = kind
        self.detail   = detail
        self.original = original
        msg = f"{kind.label}: {detail}" if detail else kind.label
        super().__init__(msg)
        if original is not None:
            self.__cause__ = original

    @property
    def exit_code(self) -> int:
        return self.kind.exit_code

    def log_context(self) -> dict:
        """
        Return a flat dict suitable for structlog event keyword args.
        All values are plain scalars (str / int) for reliable serialisation.
        """
        ctx: dict = {
            "error_kind":   self.kind.label,
            "error_detail": self.detail,
            "exit_code":    self.exit_code,
        }
        if self.original is not None:
            ctx["original_error_type"] = type(self.original).__name__
            ctx["original_error"]      = str(self.original)
        return ctx


# ---------------------------------------------------------------------------
# Typed exception classes
# ---------------------------------------------------------------------------

class AccountError(_TradingError):
    """Raised by account-status / credential operations."""
    def __init__(
        self,
        kind: AccountErrorKind,
        detail: str = "",
        original: Optional[Exception] = None,
    ):
        super().__init__(kind, detail, original)


class FundsOpError(_TradingError):
    """Raised by approve_funds / unlock_funds operations."""
    def __init__(
        self,
        kind: FundsOpErrorKind,
        detail: str = "",
        original: Optional[Exception] = None,
    ):
        super().__init__(kind, detail, original)


class FeedError(_TradingError):
    """Raised or logged when a data feed fails contract checks."""
    def __init__(
        self,
        kind: FeedErrorKind,
        detail: str = "",
        original: Optional[Exception] = None,
    ):
        super().__init__(kind, detail, original)


class ExecutionError(_TradingError):
    """Raised by execution-layer operations."""
    def __init__(
        self,
        kind: ExecutionErrorKind,
        detail: str = "",
        original: Optional[Exception] = None,
    ):
        super().__init__(kind, detail, original)


# ---------------------------------------------------------------------------
# Exit helpers
#
# Callers MUST log before calling these — these helpers only call sys.exit.
# Keeping log + exit separate lets tests assert on log output without
# triggering a real process exit.
# ---------------------------------------------------------------------------

def exit_for_account_error(error: AccountError) -> None:
    """
    Hard-exit with the typed exit code for an AccountError.
    Caller is responsible for logging before calling this.
    """
    sys.exit(error.exit_code)


def exit_for_funds_error(error: FundsOpError) -> None:
    """
    Hard-exit with the typed exit code for a FundsOpError.
    Caller is responsible for logging before calling this.
    """
    sys.exit(error.exit_code)


def exit_for_feed_error(error: FeedError) -> None:
    """Hard-exit with the typed exit code for a FeedError."""
    sys.exit(error.exit_code)


def exit_for_execution_error(error: ExecutionError) -> None:
    """Hard-exit with the typed exit code for an ExecutionError."""
    sys.exit(error.exit_code)


def exit_with_error(
    logger,
    event: str,
    error: _TradingError,
    extra: Optional[dict] = None,
) -> None:
    """
    Convenience wrapper: log via structlog then exit.
    Useful for one-liner error paths in scripts that don't need
    fine-grained control over log vs exit sequencing.

    Parameters
    ----------
    logger  : structlog bound logger
    event   : structlog event name (e.g. "account_status_failed")
    error   : the _TradingError instance
    extra   : optional extra fields merged into the log event
    """
    fields = {**error.log_context(), **(extra or {})}
    logger.error(event, **fields)
    sys.exit(error.exit_code)
