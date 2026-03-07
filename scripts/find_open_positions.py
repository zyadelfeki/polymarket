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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Print open orders and positions using the configured Polymarket client.")
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


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


async def collect_open_state(client: Any) -> dict[str, Any]:
    open_orders = []
    if hasattr(client, "get_open_orders"):
        open_orders = await _maybe_await(client.get_open_orders()) or []
    elif getattr(client, "client", None) is not None and hasattr(client.client, "get_orders"):
        open_orders = client.client.get_orders() or []

    open_positions = []
    if hasattr(client, "get_open_positions"):
        open_positions = await _maybe_await(client.get_open_positions()) or []

    return {
        "open_orders": open_orders,
        "open_positions": open_positions,
    }


def render_report(report: dict[str, Any]) -> None:
    print("OPEN ORDERS")
    print(json.dumps(report["open_orders"], indent=2, sort_keys=True, default=str))
    print("OPEN POSITIONS")
    print(json.dumps(report["open_positions"], indent=2, sort_keys=True, default=str))


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    client = build_api_client(config)
    report = await collect_open_state(client)
    render_report(report)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()