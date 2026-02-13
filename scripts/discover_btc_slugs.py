#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
import sys
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_feeds.polymarket_client_v2 import PolymarketClientV2


def _market_text(market: Dict[str, Any]) -> str:
    return " ".join(
        [
            str(market.get("question") or ""),
            str(market.get("title") or ""),
            str(market.get("slug") or ""),
            str(market.get("ticker") or ""),
            str(market.get("description") or ""),
        ]
    ).lower()


def _timeframe_guess(slug: str, text: str) -> str:
    slug_lower = (slug or "").lower()
    text_lower = (text or "").lower()

    if "15m" in slug_lower or "15 minute" in text_lower or "15-minute" in text_lower:
        return "15min"
    if "1h" in slug_lower or "hourly" in text_lower or " 1 hour" in text_lower:
        return "hourly"
    if "4h" in slug_lower or "4 hour" in text_lower or "4-hour" in text_lower:
        return "4hour"
    if "daily" in slug_lower or "today" in text_lower or "price on" in text_lower:
        return "daily"
    return "unknown"


async def discover(asset: str, limit: int) -> int:
    client = PolymarketClientV2(private_key=None, paper_trading=True)
    markets = await client.get_active_markets(limit=limit)

    asset_terms = [asset.lower()]
    if asset.lower() == "btc":
        asset_terms.append("bitcoin")

    asset_markets: List[Dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        text = _market_text(market)
        if any(term in text for term in asset_terms):
            asset_markets.append(market)

    print(f"active_markets={len(markets)}")
    print(f"{asset.lower()}_markets={len(asset_markets)}")

    timeframe_counter = Counter()
    slug_roots = Counter()

    for market in asset_markets:
        slug = str(market.get("slug") or market.get("ticker") or "NO_SLUG")
        question = str(market.get("question") or market.get("title") or "NO_QUESTION")
        end_value = market.get("end_date_iso") or market.get("endDate") or market.get("resolution_time") or market.get("resolutionTime") or "NO_END"

        timeframe = _timeframe_guess(slug=slug, text=question)
        timeframe_counter[timeframe] += 1

        parts = slug.split("-")
        root = "-".join(parts[:4]) if len(parts) >= 4 else slug
        slug_roots[root] += 1

        print("-")
        print(f"slug: {slug}")
        print(f"timeframe_guess: {timeframe}")
        print(f"question: {question[:200]}")
        print(f"end: {end_value}")

    print("\n=== timeframe_counts ===")
    for timeframe, count in timeframe_counter.most_common():
        print(f"{timeframe}: {count}")

    print("\n=== top_slug_roots ===")
    for root, count in slug_roots.most_common(25):
        print(f"{root}: {count}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover active Polymarket slugs for BTC/crypto markets")
    parser.add_argument("--asset", default="btc", help="Asset keyword, default: btc")
    parser.add_argument("--limit", type=int, default=1200, help="Max active markets to scan")
    args = parser.parse_args()

    return asyncio.run(discover(asset=args.asset, limit=args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
