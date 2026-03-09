#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger
from find_open_positions import (
    build_market_cache,
    build_report,
    collect_exchange_state,
    collect_local_state,
    resolve_db_path,
    resolve_effective_paper_trading,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the existing open-order reconciliation flow and print its summary.")
    parser.add_argument("--config", default="config/production.yaml", help="Path to YAML config file")
    parser.add_argument(
        "--exchange",
        choices=("config", "live", "off"),
        default="config",
        help=(
            "Exchange lookup mode for mismatch reporting: 'config' uses trading.paper_trading from config, "
            "'live' forces a read-only live client, 'off' skips exchange calls."
        ),
    )
    return parser.parse_args(argv)


def load_config(config_path: str) -> dict[str, Any]:
    with open(Path(config_path), "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def build_api_client(config: dict[str, Any], exchange_mode: str = "config") -> PolymarketClientV2 | None:
    api_config = config.get("api", {}).get("polymarket", {})
    paper_trading = resolve_effective_paper_trading(config, exchange_mode)
    if paper_trading is None:
        return None
    return PolymarketClientV2(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        private_key=None if paper_trading else os.getenv("POLYMARKET_PRIVATE_KEY"),
        paper_trading=paper_trading,
        rate_limit=api_config.get("rate_limit", 8.0),
        timeout=api_config.get("timeout_seconds", 10.0),
        max_retries=api_config.get("max_retries", 3),
    )


async def run_reconciliation(config_path: str) -> dict[str, Any]:
    config = load_config(config_path)
    db_path = resolve_db_path(config, None)
    ledger = AsyncLedger(db_path=db_path)
    client = build_api_client(config)
    await ledger.initialize()
    try:
        return await ledger.reconcile_open_orders(client)
    finally:
        if hasattr(client, "close"):
            try:
                await client.close()
            except Exception:
                pass
        await ledger.close()


async def build_mismatch_report(config_path: str, exchange_mode: str) -> dict[str, Any] | None:
    config = load_config(config_path)
    db_path = resolve_db_path(config, None)
    configured_paper_trading = bool(config.get("trading", {}).get("paper_trading", True))
    effective_paper_trading = resolve_effective_paper_trading(config, exchange_mode)

    ledger = AsyncLedger(db_path=db_path)
    client = build_api_client(config, exchange_mode)
    await ledger.initialize()
    try:
        if not hasattr(ledger, "execute") or not hasattr(ledger, "execute_scalar"):
            return None

        local_state = await collect_local_state(ledger)
        exchange_state, exchange_gaps = await collect_exchange_state(client, effective_paper_trading)
        market_ids = [
            *[row.get("market_id", "") for row in local_state["open_orders"]],
            *[row.get("market_id", "") for row in local_state["open_positions"]],
            *[row.get("market_id", "") for row in exchange_state["open_orders"]],
            *[row.get("market_id", "") for row in exchange_state["open_positions"]],
        ]
        if effective_paper_trading is False:
            market_cache, market_gaps = await build_market_cache(client, market_ids)
        else:
            market_cache, market_gaps = {}, []
        return build_report(
            config_path=config_path,
            db_path=db_path,
            configured_paper_trading=configured_paper_trading,
            effective_paper_trading=effective_paper_trading,
            exchange_mode=exchange_mode,
            local_state=local_state,
            exchange_state=exchange_state,
            market_cache=market_cache,
            extra_gaps=[*exchange_gaps, *market_gaps],
        )
    finally:
        if hasattr(client, "close"):
            try:
                await client.close()
            except Exception:
                pass
        await ledger.close()


def render_summary(summary: dict[str, Any], mismatch_report: dict[str, Any] | None = None) -> None:
    print("RECONCILIATION SUMMARY")
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    if mismatch_report is not None:
        print("MISMATCH REPORT")
        print(json.dumps(mismatch_report, indent=2, sort_keys=True, default=str))


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = await run_reconciliation(args.config)
    mismatch_report = await build_mismatch_report(args.config, args.exchange)
    render_summary(summary, mismatch_report)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()