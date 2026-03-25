"""
infra/errors.py — Typed error kinds for all critical surfaces.

Every failure on an account/funds/feed surface must be classified
into one of these enums so that:
  1. Exit codes are deterministic and machine-parseable.
  2. check_session / live_dashboard can distinguish "auth failed"
     from "API temporarily down" and react differently.
  3. Bare `except:` blocks are no longer acceptable anywhere these
     operations are called.
"""
from enum import Enum


class AccountErrorKind(str, Enum):
    AUTH_FAILED          = "AUTH_FAILED"
    BALANCE_MISMATCH     = "BALANCE_MISMATCH"
    API_UNAVAILABLE      = "API_UNAVAILABLE"
    BAD_CONFIG           = "BAD_CONFIG"
    UNKNOWN              = "UNKNOWN"


class FundsOpErrorKind(str, Enum):
    APPROVAL_TX_FAILED   = "APPROVAL_TX_FAILED"
    RPC_ERROR            = "RPC_ERROR"
    BAD_PRIVATE_KEY      = "BAD_PRIVATE_KEY"
    INSUFFICIENT_FUNDS   = "INSUFFICIENT_FUNDS"
    UNKNOWN              = "UNKNOWN"


class FeedErrorKind(str, Enum):
    BINANCE_FEED_UNHEALTHY   = "BINANCE_FEED_UNHEALTHY"
    OFI_FEED_DEGRADED        = "OFI_FEED_DEGRADED"
    POLYMARKET_FEED_STALE    = "POLYMARKET_FEED_STALE"
    UNKNOWN                  = "UNKNOWN"


class TaskErrorKind(str, Enum):
    TASK_DIED            = "TASK_DIED"
    TASK_RESTART_FAILED  = "TASK_RESTART_FAILED"
    UNKNOWN              = "UNKNOWN"


# Exit codes used by CLI scripts (check_account_status, approve_funds, etc.)
class ExitCode:
    OK                   = 0
    AUTH_FAILED          = 10
    BALANCE_MISMATCH     = 11
    API_UNAVAILABLE      = 12
    BAD_CONFIG           = 13
    APPROVAL_FAILED      = 20
    RPC_ERROR            = 21
    BAD_PRIVATE_KEY      = 22
    INSUFFICIENT_FUNDS   = 23
    UNKNOWN_ERROR        = 99
