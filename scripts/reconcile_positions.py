#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import yaml

from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the existing open-order reconciliation flow and print its summary.")
    parser.add_argument("--config", default="config/production.yaml", help="Path to YAML config file")
    return parser.parse_args(argv)


def load_config(config_path: str) -> dict[str, Any]:
    with open(Path(config_path), "r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file) or {}


def build_api_client(config: dict[str, Any]) -> PolymarketClientV2:
    api_config = config.get("api", {}).get("polymarket", {})
    paper_trading = bool(config.get("trading", {}).get("paper_trading", True))
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
    db_path = str(config.get("database", {}).get("path", "data/trading.db"))
    ledger = AsyncLedger(db_path=db_path)
    client = build_api_client(config)
    await ledger.initialize()
    try:
        return await ledger.reconcile_open_orders(client)
    finally:
        await ledger.close()


def render_summary(summary: dict[str, Any]) -> None:
    print("RECONCILIATION SUMMARY")
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = await run_reconciliation(args.config)
    render_summary(summary)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()