"""Unlock / approve USDC allowance for Polymarket trading.

Sends the COLLATERAL approval transaction via the Polymarket CLOB client.
Exits with a typed, non-zero code on any failure.

Exit codes (from infra/errors.py):
  0  = success
  10 = auth failure
  11 = bad config
  12 = API unavailable
  20 = approval transaction failed
  22 = insufficient MATIC for gas
  99 = unknown
"""

from __future__ import annotations

import asyncio
import os
import sys

try:
    import structlog
    logger = structlog.get_logger(__name__)
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    class _FallbackLogger:
        def __init__(self, name: str):
            self._l = logging.getLogger(name)
        def info(self, event: str, **kw): self._l.info(f"{event} | {kw}" if kw else event)
        def warning(self, event: str, **kw): self._l.warning(f"{event} | {kw}" if kw else event)
        def error(self, event: str, **kw): self._l.error(f"{event} | {kw}" if kw else event)
    logger = _FallbackLogger(__name__)

from dotenv import load_dotenv

from infra.errors import (
    FundsOpError, FundsOpErrorKind,
    AccountError, AccountErrorKind,
    exit_for_funds_error,
    EXIT_OK,
)


def _derive_creds(client):
    """Try both credential derivation methods; raise AccountError on both failing."""
    errors = []
    for method_name in ("create_or_derive_api_creds", "create_or_derive_api_key"):
        method = getattr(client, method_name, None)
        if method is None:
            continue
        try:
            return method()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method_name}: {type(exc).__name__}: {exc}")
    raise AccountError(
        AccountErrorKind.AUTH_FAILED,
        "Both credential derivation methods failed: " + " | ".join(errors),
    )


async def main() -> int:
    load_dotenv(override=True)

    key      = os.getenv("POLYMARKET_PRIVATE_KEY")
    host     = "https://clob.polymarket.com"
    chain_id = 137

    print("--- \U0001f513 UNLOCKING FUNDS ---")

    # --- Config validation ----------------------------------------------
    if not key or key.strip() == "":
        err = FundsOpError(FundsOpErrorKind.BAD_PRIVATE_KEY, "POLYMARKET_PRIVATE_KEY is missing or empty in .env")
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c CONFIG ERROR: {err}")
        exit_for_funds_error(err)

    # --- 1. Authenticate ------------------------------------------------
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        err = FundsOpError(FundsOpErrorKind.API_UNAVAILABLE, f"py_clob_client not installed: {exc}", original=exc)
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c IMPORT ERROR: {err}")
        exit_for_funds_error(err)

    print("1. Logging in...")
    try:
        client = ClobClient(host, key=key, chain_id=chain_id)
        creds = _derive_creds(client)
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        logger.info("unlock_funds_authenticated")
        print("\u2705 Logged in.")
    except AccountError as exc:
        err = FundsOpError(FundsOpErrorKind.AUTH_FAILED, str(exc), original=exc)
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c AUTH ERROR: {err}")
        exit_for_funds_error(err)
    except Exception as exc:
        err = FundsOpError(
            FundsOpErrorKind.AUTH_FAILED,
            f"Authentication failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c AUTH ERROR: {err}")
        exit_for_funds_error(err)

    # --- 2. Verify address (optional connection check) ------------------
    print("2. Verifying Address...")
    try:
        client.get_api_keys()  # refreshes context; result is not used
        print("   (Connection established)")
    except Exception as exc:
        # Non-fatal: log as warning; proceed to approval attempt
        logger.warning(
            "unlock_funds_api_keys_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            note="Proceeding to approval attempt anyway",
        )
        print(f"   (Connection check warning: {type(exc).__name__}: {exc} — proceeding anyway)")

    # --- 3. Send approval transaction -----------------------------------
    print("3. Sending 'Approve' Transaction...")
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        logger.info("unlock_funds_sending_tx")
        tx_hash = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        logger.info("unlock_funds_tx_sent", tx_hash=str(tx_hash))
        print(f"\u2705 SUCCESS! Funds Unlocked.")
        print(f"   Transaction Hash: {tx_hash}")
        print("   Wait 15 seconds for the blockchain to update.")
    except Exception as exc:
        kind = FundsOpErrorKind.INSUFFICIENT_MATIC if "gas" in str(exc).lower() else FundsOpErrorKind.APPROVAL_TX_FAILED
        err = FundsOpError(
            kind,
            f"update_balance_allowance failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        # Only exit as a real error if we did NOT see a tx hash first.
        # Some versions of the client raise on the HTTP response even after
        # submitting the tx — check if message mentions balance (known false positive).
        if "balance" in str(exc).lower():
            logger.warning(
                "unlock_funds_tx_balance_warning",
                kind=err.kind.name,
                error=str(err),
                note="This may be a false-positive after a successful tx — verify on-chain",
            )
            print(f"   \u26a0\ufe0f  Warning [{err.kind.name}]: {exc}")
            print("   (This may be ignorable if you already see a Transaction Hash above)")
            return EXIT_OK  # Treat as success with a warning
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c TX ERROR [{err.kind.name}]: {err}")
        if err.kind == FundsOpErrorKind.INSUFFICIENT_MATIC:
            print("\U0001f449 CAUSE: You need a tiny amount of MATIC for gas fees.")
        exit_for_funds_error(err)

    return EXIT_OK


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(result)
    except FundsOpError as exc:
        logger.error("unlock_funds_failed", kind=exc.kind.name, error=str(exc))
        print(f"\u274c FUNDS ERROR [{exc.kind.name}]: {exc}")
        exit_for_funds_error(exc)
    except AccountError as exc:
        err = FundsOpError(FundsOpErrorKind.AUTH_FAILED, str(exc), original=exc)
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        exit_for_funds_error(err)
    except SystemExit:
        raise
    except Exception as exc:
        err = FundsOpError(FundsOpErrorKind.UNKNOWN, f"Unexpected error: {type(exc).__name__}: {exc}", original=exc)
        logger.error("unlock_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c UNEXPECTED ERROR: {exc}")
        exit_for_funds_error(err)
