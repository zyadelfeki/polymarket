"""Account status checker.

Checks Polymarket API-reported balance and signer address.
Exits with a typed, non-zero code on any failure so orchestrators
(and check_session) can react correctly instead of silently swallowing errors.

Exit codes are defined in infra/errors.py:
  0  = success
  10 = auth failure (bad key, creds could not be derived)
  11 = bad config (missing env var)
  12 = API unavailable (network / CLOB down)
  13 = balance mismatch (API sees $0 but we expected funds)
  99 = unknown error
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
    AccountError, AccountErrorKind,
    exit_for_account_error,
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

    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not key or key.strip() == "":
        err = AccountError(
            AccountErrorKind.BAD_PRIVATE_KEY,
            "POLYMARKET_PRIVATE_KEY is missing or empty in .env",
        )
        logger.error(
            "account_status_failed",
            kind=err.kind.name,
            error=str(err),
        )
        print(f"\u274c CONFIG ERROR: {err}")
        exit_for_account_error(err)

    host = "https://clob.polymarket.com"
    chain_id = 137

    print("--- \U0001f575\ufe0f ACCOUNT DETECTIVE ---")

    # --- 1. Authenticate ------------------------------------------------
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        err = AccountError(
            AccountErrorKind.BAD_CONFIG,
            f"py_clob_client not installed: {exc}",
            original=exc,
        )
        logger.error("account_status_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c IMPORT ERROR: {err}")
        exit_for_account_error(err)

    try:
        client = ClobClient(host, key=key, chain_id=chain_id)
        creds = _derive_creds(client)
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        logger.info("account_status_authenticated")
        print("\u2705 Authenticated")
    except AccountError:
        raise  # propagated to outer handler below
    except Exception as exc:
        err = AccountError(
            AccountErrorKind.AUTH_FAILED,
            f"Authentication failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("account_status_failed", kind=err.kind.name, error=str(err), error_type=type(exc).__name__)
        print(f"\u274c AUTH ERROR: {err}")
        exit_for_account_error(err)

    # --- 2. Get signer address ------------------------------------------
    try:
        signer = client.get_address()
        logger.info("account_status_signer", signer=signer)
        print(f"\U0001f511 SIGNER ADDRESS: {signer}")
    except Exception as exc:
        err = AccountError(
            AccountErrorKind.API_UNAVAILABLE,
            f"Could not retrieve signer address: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("account_status_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c ADDRESS ERROR: {err}")
        exit_for_account_error(err)

    # --- 3. Fetch balance from Polymarket API ---------------------------
    print("\n\U0001f4e1 ASKING POLYMARKET API FOR BALANCE...")
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
    except Exception as exc:
        err = AccountError(
            AccountErrorKind.API_UNAVAILABLE,
            f"get_balance_allowance failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("account_status_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c API ERROR: {err}")
        exit_for_account_error(err)

    raw_balance = resp.get("balance", "0")
    try:
        balance = float(raw_balance) / 1_000_000
    except (TypeError, ValueError) as exc:
        err = AccountError(
            AccountErrorKind.BALANCE_MISMATCH,
            f"Could not parse balance from API response: {raw_balance!r}: {exc}",
            original=exc,
        )
        logger.error("account_status_failed", kind=err.kind.name, error=str(err), raw_balance=raw_balance)
        print(f"\u274c BALANCE PARSE ERROR: {err}")
        exit_for_account_error(err)

    logger.info("account_status_balance", balance_usd=round(balance, 4), signer=signer)
    print(f"\U0001f4b0 API REPORTS BALANCE: ${balance:,.2f}")

    if balance > 1:
        print("\n\u2705 GREAT NEWS: The API sees your money!")
        print("   The bot was failing because it was checking the wrong 'Proxy Address'.")
        print("   We will switch the bot to trust the API instead of the config file.")
    else:
        err = AccountError(
            AccountErrorKind.BALANCE_MISMATCH,
            f"API reports $0.00 balance for signer {signer}. "
            "Private key may belong to a different account than expected.",
        )
        logger.error(
            "account_status_balance_mismatch",
            kind=err.kind.name,
            balance_usd=round(balance, 4),
            signer=signer,
            hint="Check that POLYMARKET_PRIVATE_KEY matches the account visible in your browser",
        )
        print("\n\u274c BAD NEWS: The API sees $0.00.")
        print("   This means your Private Key belongs to a DIFFERENT account than the one in your browser.")
        print("   Did you log in with a different email on the website?")
        exit_for_account_error(err)

    return EXIT_OK


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(result)
    except AccountError as exc:
        logger.error("account_status_failed", kind=exc.kind.name, error=str(exc))
        print(f"\u274c ACCOUNT ERROR [{exc.kind.name}]: {exc}")
        exit_for_account_error(exc)
    except SystemExit:
        raise
    except Exception as exc:
        err = AccountError(AccountErrorKind.UNKNOWN, f"Unexpected error: {type(exc).__name__}: {exc}", original=exc)
        logger.error("account_status_failed", kind=err.kind.name, error=str(err), error_type=type(exc).__name__)
        print(f"\u274c UNEXPECTED ERROR: {exc}")
        exit_for_account_error(err)
