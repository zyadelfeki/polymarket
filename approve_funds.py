"""Approve USDC funds for Polymarket trading.

Approves the Polymarket CLOB to trade from your proxy wallet.
Exits with a typed, non-zero code on any failure.

Exit codes (from infra/errors.py):
  0  = success
  10 = auth failure
  11 = bad config
  12 = API unavailable
  20 = approval transaction failed
  21 = RPC / Web3 error
  22 = insufficient MATIC for gas
  23 = proxy address mismatch
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
from web3 import Web3

from infra.errors import (
    FundsOpError, FundsOpErrorKind,
    AccountError, AccountErrorKind,
    exit_for_funds_error, exit_for_account_error,
    EXIT_OK,
)

# Minimal ERC-20 balanceOf ABI
_USDC_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]
_USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_POLYGON_RPC   = "https://polygon-rpc.com"


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

    key   = os.getenv("POLYMARKET_PRIVATE_KEY")
    proxy = os.getenv("POLYMARKET_PROXY_ADDRESS")
    host      = "https://clob.polymarket.com"
    chain_id  = 137

    # --- Config validation ----------------------------------------------
    if not key or key.strip() == "":
        err = FundsOpError(FundsOpErrorKind.BAD_PRIVATE_KEY, "POLYMARKET_PRIVATE_KEY is missing or empty in .env")
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c CONFIG ERROR: {err}")
        exit_for_funds_error(err)

    if not proxy or proxy.strip() == "":
        err = FundsOpError(
            FundsOpErrorKind.PROXY_MISMATCH,
            "POLYMARKET_PROXY_ADDRESS is missing in .env file.",
        )
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c ERROR: {err}")
        exit_for_funds_error(err)

    print(f"--- TARGETING PROXY: {proxy} ---")

    # --- 1. On-chain balance check (source of truth) --------------------
    try:
        w3 = Web3(Web3.HTTPProvider(_POLYGON_RPC))
        contract = w3.eth.contract(address=_USDC_CONTRACT, abi=_USDC_BALANCE_ABI)
        raw_balance = contract.functions.balanceOf(proxy).call()
        balance_usdc = raw_balance / 1e6
    except Exception as exc:
        err = FundsOpError(
            FundsOpErrorKind.RPC_ERROR,
            f"Web3 balanceOf call failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err), proxy=proxy)
        print(f"\u274c RPC ERROR: {err}")
        exit_for_funds_error(err)

    logger.info("approve_funds_proxy_balance", balance_usdc=round(balance_usdc, 4), proxy=proxy)
    print(f"\U0001f4b0 REAL PROXY BALANCE: ${balance_usdc:,.2f}")

    if balance_usdc < 1:
        err = FundsOpError(
            FundsOpErrorKind.PROXY_MISMATCH,
            f"Proxy balance is ${balance_usdc:.2f} (< $1). Check that funds were sent to the correct proxy address: {proxy}",
        )
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err), proxy=proxy, balance_usdc=round(balance_usdc, 4))
        print(f"\u274c ERROR: {err}")
        exit_for_funds_error(err)

    # --- 2. Authenticate ------------------------------------------------
    try:
        from py_clob_client.client import ClobClient
    except ImportError as exc:
        err = FundsOpError(FundsOpErrorKind.API_UNAVAILABLE, f"py_clob_client not installed: {exc}", original=exc)
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c IMPORT ERROR: {err}")
        exit_for_funds_error(err)

    try:
        client = ClobClient(host, key=key, chain_id=chain_id)
        creds = _derive_creds(client)
        client = ClobClient(host, key=key, chain_id=chain_id, creds=creds)
        logger.info("approve_funds_authenticated")
        print("\u2705 Authenticated")
    except AccountError as exc:
        # Re-wrap as FundsOpError so the exit path is unified
        err = FundsOpError(FundsOpErrorKind.AUTH_FAILED, str(exc), original=exc)
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c AUTH ERROR: {err}")
        exit_for_funds_error(err)
    except Exception as exc:
        err = FundsOpError(
            FundsOpErrorKind.AUTH_FAILED,
            f"Authentication failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c AUTH ERROR: {err}")
        exit_for_funds_error(err)

    # --- 3. Send approval transaction -----------------------------------
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams
        logger.info("approve_funds_sending_tx", proxy=proxy)
        print("\U0001f680 SENDING APPROVAL TRANSACTION...")
        resp = client.update_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL")
        )
        logger.info("approve_funds_tx_sent", response=str(resp), proxy=proxy)
        print(f"\u2705 APPROVAL SENT! Response: {resp}")
    except Exception as exc:
        kind = FundsOpErrorKind.INSUFFICIENT_MATIC if "gas" in str(exc).lower() else FundsOpErrorKind.APPROVAL_TX_FAILED
        err = FundsOpError(
            kind,
            f"update_balance_allowance failed: {type(exc).__name__}: {exc}",
            original=exc,
        )
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err), proxy=proxy)
        print(f"\u274c TX ERROR [{err.kind.name}]: {err}")
        if err.kind == FundsOpErrorKind.INSUFFICIENT_MATIC:
            print("\U0001f449 CAUSE: You need a tiny amount of MATIC for gas fees.")
        exit_for_funds_error(err)

    print("\u23f3 Waiting 15 seconds for blockchain confirmation...")
    await asyncio.sleep(15)
    logger.info("approve_funds_complete", proxy=proxy)
    print("\u2705 DONE. You should be ready to trade.")
    return EXIT_OK


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(result)
    except FundsOpError as exc:
        logger.error("approve_funds_failed", kind=exc.kind.name, error=str(exc))
        print(f"\u274c FUNDS ERROR [{exc.kind.name}]: {exc}")
        exit_for_funds_error(exc)
    except AccountError as exc:
        err = FundsOpError(FundsOpErrorKind.AUTH_FAILED, str(exc), original=exc)
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        exit_for_funds_error(err)
    except SystemExit:
        raise
    except Exception as exc:
        err = FundsOpError(FundsOpErrorKind.UNKNOWN, f"Unexpected error: {type(exc).__name__}: {exc}", original=exc)
        logger.error("approve_funds_failed", kind=err.kind.name, error=str(err))
        print(f"\u274c UNEXPECTED ERROR: {exc}")
        exit_for_funds_error(err)
