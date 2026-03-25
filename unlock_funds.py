#!/usr/bin/env python3
"""
unlock_funds.py — Revoke USDC allowance / withdraw from CLOB to wallet.

Exit codes (see infra/errors.py ExitCode):
  0   — OK
  13  — BAD_CONFIG
  21  — RPC_ERROR
  22  — BAD_PRIVATE_KEY
  20  — TX_FAILED
  99  — UNKNOWN_ERROR
"""
import sys
import os

import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger(__name__)

from infra.errors import FundsOpErrorKind, ExitCode  # noqa: E402


def _load_config() -> dict:
    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    rpc_url     = os.environ.get("POLYGON_RPC_URL", "").strip()

    missing = []
    if not private_key:
        missing.append("POLYMARKET_PRIVATE_KEY")
    if not rpc_url:
        missing.append("POLYGON_RPC_URL")
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return {
        "private_key": private_key,
        "rpc_url":     rpc_url,
        "chain_id":    int(os.environ.get("POLYMARKET_CHAIN_ID", "137")),
    }


def _revoke_approval(cfg: dict) -> str:
    """
    Set USDC allowance back to zero (revoking CLOB access).
    Returns transaction hash.
    """
    try:
        from web3 import Web3
        from web3.middleware import geth_poa_middleware  # type: ignore
    except ImportError as exc:
        raise ImportError("web3 not installed — run: pip install web3") from exc

    try:
        w3 = Web3(Web3.HTTPProvider(cfg["rpc_url"]))
    except Exception as exc:
        raise ConnectionError(f"Could not connect to RPC: {exc}") from exc

    if not w3.is_connected():
        raise ConnectionError(f"RPC not reachable: {cfg['rpc_url']}")

    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    SPENDER      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    ERC20_APPROVE_ABI = [
        {
            "name": "approve",
            "type": "function",
            "inputs": [
                {"name": "spender", "type": "address"},
                {"name": "amount",  "type": "uint256"},
            ],
            "outputs": [{"name": "", "type": "bool"}],
        }
    ]

    try:
        account = w3.eth.account.from_key(cfg["private_key"])
    except (ValueError, Exception) as exc:
        raise ValueError(f"Bad private key: {exc}") from exc

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_ADDRESS),
        abi=ERC20_APPROVE_ABI,
    )

    try:
        nonce = w3.eth.get_transaction_count(account.address)
        tx = contract.functions.approve(
            Web3.to_checksum_address(SPENDER), 0
        ).build_transaction({
            "from":     account.address,
            "nonce":    nonce,
            "gas":      100_000,
            "gasPrice": w3.eth.gas_price,
            "chainId":  cfg["chain_id"],
        })
        signed_tx = account.sign_transaction(tx)
        tx_hash   = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        receipt   = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except (ConnectionError, TimeoutError) as exc:
        raise ConnectionError(f"RPC communication error: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Transaction failed: {exc}") from exc

    if receipt.status != 1:
        raise RuntimeError(
            f"Revoke tx reverted. Hash: {tx_hash.hex()} Status: {receipt.status}"
        )

    return tx_hash.hex()


def main() -> int:
    logger.info("unlock_funds_start")

    try:
        cfg = _load_config()
    except ValueError as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.UNKNOWN,
            error=str(exc),
        )
        return ExitCode.BAD_CONFIG

    try:
        tx_hash = _revoke_approval(cfg)
    except ImportError as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.UNKNOWN,
            error=str(exc),
        )
        return ExitCode.UNKNOWN_ERROR
    except ConnectionError as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.RPC_ERROR,
            error=str(exc),
        )
        return ExitCode.RPC_ERROR
    except ValueError as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.BAD_PRIVATE_KEY,
            error=str(exc),
        )
        return ExitCode.BAD_PRIVATE_KEY
    except RuntimeError as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.APPROVAL_TX_FAILED,
            error=str(exc),
        )
        return ExitCode.APPROVAL_FAILED
    except Exception as exc:
        logger.error(
            "unlock_funds_failed",
            kind=FundsOpErrorKind.UNKNOWN,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ExitCode.UNKNOWN_ERROR

    logger.info("unlock_funds_ok", tx_hash=tx_hash)
    return ExitCode.OK


if __name__ == "__main__":
    sys.exit(main())
