"""Typed error taxonomy for the Polymarket bot.

Every fund-touching script and the session/account tools import from here.
This eliminates bare ``except:`` blocks that silently swallow typed errors
and makes failure kinds machine-readable for session checks and dashboards.

Usage::

    from infra.errors import FundsOpError, AccountError, FundsOpErrorKind, AccountErrorKind

    try:
        ...
    except SomeWeb3Exception as exc:
        raise FundsOpError(FundsOpErrorKind.RPC_ERROR, str(exc)) from exc
"""

from __future__ import annotations

import sys
from enum import Enum, auto
from typing import Optional


# ---------------------------------------------------------------------------
# Error kinds
# ---------------------------------------------------------------------------

class AccountErrorKind(Enum):
    """Failure categories for account / auth operations."""
    AUTH_FAILED        = auto()  # Private key rejected or creds could not be derived
    BAD_PRIVATE_KEY    = auto()  # Private key missing, None, or obviously malformed
    API_UNAVAILABLE    = auto()  # Network error or Polymarket CLOB unreachable
    BALANCE_MISMATCH   = auto()  # API balance disagrees with on-chain balance beyond tolerance
    BAD_CONFIG         = auto()  # Missing / invalid env vars (PROXY_ADDRESS, chain_id, …)
    UNKNOWN            = auto()  # Catch-all; always investigate


class FundsOpErrorKind(Enum):
    """Failure categories for fund approval / unlock operations."""
    APPROVAL_TX_FAILED = auto()  # update_balance_allowance call failed
    RPC_ERROR          = auto()  # Web3 / Polygon RPC unreachable or returned error
    BAD_PRIVATE_KEY    = auto()  # Private key missing or wrong format
    INSUFFICIENT_MATIC = auto()  # Estimated gas exceeds available MATIC balance
    PROXY_MISMATCH     = auto()  # POLYMARKET_PROXY_ADDRESS does not match derived signer
    AUTH_FAILED        = auto()  # CLOB client authentication step failed
    API_UNAVAILABLE    = auto()  # Network error to Polymarket CLOB
    UNKNOWN            = auto()  # Catch-all; always investigate


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class PolymarketBotError(RuntimeError):
    """Base class for all typed bot errors."""


class AccountError(PolymarketBotError):
    """Raised for account / authentication failures."""

    def __init__(self, kind: AccountErrorKind, message: str, *, original: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.original = original

    def __repr__(self) -> str:
        return f"AccountError(kind={self.kind.name}, message={str(self)!r})"


class FundsOpError(PolymarketBotError):
    """Raised for fund approval / unlock failures."""

    def __init__(self, kind: FundsOpErrorKind, message: str, *, original: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.original = original

    def __repr__(self) -> str:
        return f"FundsOpError(kind={self.kind.name}, message={str(self)!r})"


# ---------------------------------------------------------------------------
# Exit codes  (used by scripts that call sys.exit())
# ---------------------------------------------------------------------------

EXIT_OK                 = 0
EXIT_AUTH_FAILED        = 10
EXIT_BAD_CONFIG         = 11
EXIT_API_UNAVAILABLE    = 12
EXIT_BALANCE_MISMATCH   = 13
EXIT_TX_FAILED          = 20
EXIT_RPC_ERROR          = 21
EXIT_INSUFFICIENT_MATIC = 22
EXIT_PROXY_MISMATCH     = 23
EXIT_UNKNOWN            = 99


ACCOUNT_ERROR_EXIT_MAP: dict[AccountErrorKind, int] = {
    AccountErrorKind.AUTH_FAILED:      EXIT_AUTH_FAILED,
    AccountErrorKind.BAD_PRIVATE_KEY:  EXIT_AUTH_FAILED,
    AccountErrorKind.API_UNAVAILABLE:  EXIT_API_UNAVAILABLE,
    AccountErrorKind.BALANCE_MISMATCH: EXIT_BALANCE_MISMATCH,
    AccountErrorKind.BAD_CONFIG:       EXIT_BAD_CONFIG,
    AccountErrorKind.UNKNOWN:          EXIT_UNKNOWN,
}

FUNDS_ERROR_EXIT_MAP: dict[FundsOpErrorKind, int] = {
    FundsOpErrorKind.APPROVAL_TX_FAILED: EXIT_TX_FAILED,
    FundsOpErrorKind.RPC_ERROR:          EXIT_RPC_ERROR,
    FundsOpErrorKind.BAD_PRIVATE_KEY:    EXIT_AUTH_FAILED,
    FundsOpErrorKind.INSUFFICIENT_MATIC: EXIT_INSUFFICIENT_MATIC,
    FundsOpErrorKind.PROXY_MISMATCH:     EXIT_PROXY_MISMATCH,
    FundsOpErrorKind.AUTH_FAILED:        EXIT_AUTH_FAILED,
    FundsOpErrorKind.API_UNAVAILABLE:    EXIT_API_UNAVAILABLE,
    FundsOpErrorKind.UNKNOWN:            EXIT_UNKNOWN,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def exit_for_account_error(err: AccountError) -> None:
    """Call sys.exit() with the correct exit code for an AccountError."""
    sys.exit(ACCOUNT_ERROR_EXIT_MAP.get(err.kind, EXIT_UNKNOWN))


def exit_for_funds_error(err: FundsOpError) -> None:
    """Call sys.exit() with the correct exit code for a FundsOpError."""
    sys.exit(FUNDS_ERROR_EXIT_MAP.get(err.kind, EXIT_UNKNOWN))
