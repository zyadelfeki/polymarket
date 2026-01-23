import os
import sys

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from strategy.multi_outcome_arbitrage import MultiOutcomeArbitrageEngine
from strategy.complement_arbitrage import ComplementArbitrageEngine
from strategy.cross_platform_arbitrage import CrossPlatformArbitrageEngine


class AsyncSellClient:
    def __init__(self):
        self.calls = []

    async def market_sell(self, token_id, shares):
        self.calls.append((token_id, shares))
        return {"success": True}


@pytest.mark.asyncio
async def test_multi_outcome_unwind_calls_market_sell():
    client = AsyncSellClient()
    engine = MultiOutcomeArbitrageEngine()

    orders = [
        {"token_id": "t1", "shares": 1.5},
        {"token_id": "t2", "shares": 2.0},
    ]

    await engine._unwind_orders(client, orders)

    assert client.calls == [("t1", 1.5), ("t2", 2.0)]


@pytest.mark.asyncio
async def test_complement_unwind_calls_market_sell():
    client = AsyncSellClient()
    engine = ComplementArbitrageEngine()

    await engine._unwind_position(client, "yes_token", 3.0)

    assert client.calls == [("yes_token", 3.0)]


@pytest.mark.asyncio
async def test_cross_platform_unwind_calls_market_sell():
    client = AsyncSellClient()
    engine = CrossPlatformArbitrageEngine()

    await engine._unwind_polymarket(client, "poly_token", 4.0)

    assert client.calls == [("poly_token", 4.0)]
