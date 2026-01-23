import os
import sys
import pytest
from decimal import Decimal
from types import SimpleNamespace

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from strategies.arb_engine_v1 import ArbitrageEngine


class StubPolymarketClient:
    async def get_active_markets(self):
        return [{"id": "market_1"}]

    async def get_market_orderbook_summary(self, market_id: str):
        return {
            "market_id": market_id,
            "bid": Decimal("0.45"),
            "ask": Decimal("0.46"),
            "bid_volume": Decimal("100"),
            "ask_volume": Decimal("100"),
        }

    async def place_order(self, **kwargs):
        return {"order_id": "poly_order_1"}


class StubKalshiClient:
    async def get_market_orderbook(self, market_id: str):
        return SimpleNamespace(
            bid=Decimal("0.50"),
            ask=Decimal("0.51"),
            bid_volume=Decimal("100"),
            ask_volume=Decimal("100"),
            market_id=market_id,
        )

    async def place_order(self, **kwargs):
        return {"order_id": "kalshi_order_1"}


@pytest.mark.asyncio
async def test_arbitrage_scanner_finds_opportunity():
    poly = StubPolymarketClient()
    kalshi = StubKalshiClient()

    engine = ArbitrageEngine(
        poly,
        kalshi,
        config={
            "min_profit_pct": 2.0,
            "max_position_pct": 10.0,
            "min_trade_size": 10.0,
            "max_trade_size": 1000.0,
        },
    )

    opportunities = await engine.scan_opportunities()
    assert len(opportunities) >= 1


@pytest.mark.asyncio
async def test_execute_arbitrage_returns_result():
    poly = StubPolymarketClient()
    kalshi = StubKalshiClient()

    engine = ArbitrageEngine(
        poly,
        kalshi,
        config={
            "min_profit_pct": 2.0,
            "max_position_pct": 10.0,
            "min_trade_size": 10.0,
            "max_trade_size": 1000.0,
        },
    )

    opportunities = await engine.scan_opportunities()
    result = await engine.execute_arbitrage(opportunities[0])

    assert result["buy_order_id"]
    assert result["sell_order_id"]
    assert result["net_profit"] is not None
