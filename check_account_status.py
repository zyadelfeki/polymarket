#!/usr/bin/env python3
"""
check_account_status.py — Verify API credentials and on-chain balance.

Exit codes (see infra/errors.py ExitCode):
  0   — OK
  10  — AUTH_FAILED
  11  — BALANCE_MISMATCH
  12  — API_UNAVAILABLE
  13  — BAD_CONFIG
  99  — UNKNOWN_ERROR
"""
import sys
import os

import structlog

# Bootstrap structlog before any other import so log output is consistent.
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

from infra.errors import AccountErrorKind, ExitCode  # noqa: E402


def _load_secrets() -> dict:
    """
    Load API credentials from environment.
    Raises ValueError with a clear message if any required key is missing.
    """
    api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()

    missing = []
    if not api_key:
        missing.append("POLYMARKET_API_KEY")
    if not private_key:
        missing.append("POLYMARKET_PRIVATE_KEY")
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return {"api_key": api_key, "private_key": private_key}


def _create_credentials(secrets: dict):
    """
    Attempt to build API credentials from *secrets*.
    Returns the credentials object or raises a descriptive exception.
    """
    # Import here so the module is loadable even without py_clob_client installed.
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        raise ImportError("py_clob_client not installed") from exc

    try:
        # derive_api_key / create_or_derive_api_creds live in different versions
        # of the SDK.  Try the newer path first, fall back to the older one.
        # We do NOT use a bare except here — only ImportError / AttributeError
        # can come from a missing method; anything else is a real credential error.
        try:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=secrets["api_key"],
                api_secret="",  # not required for read-only status check
                api_passphrase="",
            )
        except (ImportError, AttributeError):
            # Older SDK path
            creds = ClobClient(
                host=os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com"),
                key=secrets["private_key"],
                chain_id=int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
            ).create_or_derive_api_creds()
        return creds
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Credential construction failed: {exc}") from exc


def _check_balance(creds) -> dict:
    """
    Query on-chain USDC balance.  Raises on network / auth failure.
    Returns dict with keys: balance, collateral_address.
    """
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        raise ImportError("py_clob_client not installed") from exc

    host = os.environ.get("POLYMARKET_HOST", "https://clob.polymarket.com")
    chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")

    try:
        client = ClobClient(host=host, key=private_key, chain_id=chain_id)
        balance_info = client.get_balance()
        return {
            "balance": balance_info.get("balance", "unknown"),
            "collateral_address": balance_info.get("collateral_address", "unknown"),
        }
    except ConnectionError as exc:
        raise ConnectionError(f"Network error while fetching balance: {exc}") from exc


def main() -> int:
    """
    Run account status check.  Returns an ExitCode integer.
    """
    logger.info("account_status_check_start")

    # 1. Load secrets — exits 13 on bad config
    try:
        secrets = _load_secrets()
    except ValueError as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.BAD_CONFIG,
            error=str(exc),
        )
        return ExitCode.BAD_CONFIG

    # 2. Build credentials — exits 10 on auth failure
    try:
        creds = _create_credentials(secrets)
    except ImportError as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.API_UNAVAILABLE,
            error=str(exc),
        )
        return ExitCode.API_UNAVAILABLE
    except (ValueError, Exception) as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.AUTH_FAILED,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ExitCode.AUTH_FAILED

    logger.info("credentials_built_ok")

    # 3. Check balance — exits 12 on network error
    try:
        balance_info = _check_balance(creds)
    except ImportError as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.API_UNAVAILABLE,
            error=str(exc),
        )
        return ExitCode.API_UNAVAILABLE
    except ConnectionError as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.API_UNAVAILABLE,
            error=str(exc),
        )
        return ExitCode.API_UNAVAILABLE
    except Exception as exc:
        logger.error(
            "account_status_failed",
            kind=AccountErrorKind.UNKNOWN,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ExitCode.UNKNOWN_ERROR

    logger.info(
        "account_status_ok",
        balance=balance_info["balance"],
        collateral_address=balance_info["collateral_address"],
    )
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
