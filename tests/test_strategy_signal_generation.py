from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


@pytest.fixture
def strategy() -> LatencyArbitrageEngine:
    return LatencyArbitrageEngine(
        binance_ws=MagicMock(),
        polymarket_client=MagicMock(),
        charlie_predictor=None,
        redis_subscriber=None,
    )


def test_bullish_bearish_signal_and_no_trade(strategy: LatencyArbitrageEngine) -> None:
    bullish = strategy.determine_trade_direction(
        btc_price=Decimal("97000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.40"),
        no_odds=Decimal("0.60"),
    )
    assert bullish is not None
    assert bullish["outcome"] == "YES"
    assert bullish["side"] == "BUY"

    bearish = strategy.determine_trade_direction(
        btc_price=Decimal("95000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.80"),
        no_odds=Decimal("0.20"),
    )
    assert bearish is not None
    assert bearish["outcome"] == "NO"
    assert bearish["side"] == "BUY"

    no_trade = strategy.determine_trade_direction(
        btc_price=Decimal("96000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.50"),
        no_odds=Decimal("0.50"),
    )
    assert no_trade is None


@pytest.mark.asyncio
async def test_execute_signal_success_path(strategy: LatencyArbitrageEngine) -> None:
    execution = MagicMock()
    execution.get_real_balance = AsyncMock(return_value=Decimal("100"))
    execution.place_order = AsyncMock(return_value={"success": True, "order_id": "x1"})

    kelly = MagicMock()
    kelly.calculate_size.return_value = Decimal("10")

    polymarket = MagicMock()
    polymarket.get_orderbook = AsyncMock(return_value={"asks": [["0.52", "100"]]})

    strategy.execution = execution
    strategy.kelly_sizer = kelly
    strategy.polymarket = polymarket

    market = {
        "id": "m1",
        "tokens": [
            {"outcome": "YES", "token_id": "yes-id"},
            {"outcome": "NO", "token_id": "no-id"},
        ],
    }

    result = await strategy.execute_signal(market=market, signal="BULLISH", confidence=Decimal("0.8"))
    assert result["success"] is True
    execution.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_signal_reject_paths(strategy: LatencyArbitrageEngine) -> None:
    market = {"id": "m1", "tokens": [{"outcome": "YES", "token_id": "yes-only"}]}

    strategy.execution = None
    strategy.kelly_sizer = None
    assert await strategy.execute_signal(market=market, signal="BULLISH", confidence=Decimal("0.7")) is None

    strategy.execution = MagicMock()
    strategy.execution.get_real_balance = AsyncMock(return_value=Decimal("100"))
    strategy.execution.place_order = AsyncMock(return_value={"success": True})
    strategy.kelly_sizer = MagicMock()
    strategy.kelly_sizer.calculate_size.return_value = Decimal("1")
    strategy.polymarket = MagicMock()
    strategy.polymarket.get_orderbook = AsyncMock(return_value={"asks": []})

    assert await strategy.execute_signal(market=market, signal="BULLISH", confidence=Decimal("0.7")) is None
    assert await strategy.execute_signal(market=market, signal="INVALID", confidence=Decimal("0.7")) is None


@pytest.mark.asyncio
async def test_scan_opportunities_early_exits(strategy: LatencyArbitrageEngine) -> None:
    strategy._get_btc_price = AsyncMock(return_value=None)
    assert await strategy.scan_opportunities() is None

    strategy._get_btc_price = AsyncMock(return_value=Decimal("96000"))
    strategy._get_active_markets = AsyncMock(return_value=[])
    assert await strategy.scan_opportunities() is None


@pytest.mark.asyncio
async def test_scan_opportunities_returns_first_found(strategy: LatencyArbitrageEngine) -> None:
    strategy._get_btc_price = AsyncMock(return_value=Decimal("96000"))
    strategy._get_active_markets = AsyncMock(
        return_value=[
            {"id": "1", "question": "Will BTC 15 min be above $96,000?"},
            {"id": "2", "question": "Will BTC 15 min be above $97,000?"},
        ]
    )

    async def fake_check(market, btc_price):
        if market["id"] == "1":
            return {"market_id": "1", "side": "YES"}
        return None

    strategy._check_market_arbitrage = fake_check
    result = await strategy.scan_opportunities()
    assert result is not None
    assert result["market_id"] == "1"


@pytest.mark.asyncio
async def test_check_market_arbitrage_rejects_non_btc(strategy: LatencyArbitrageEngine) -> None:
    market = {"id": "x", "question": "Will ETH be above $2000 in 15 minutes?"}
    assert await strategy._check_market_arbitrage(market, Decimal("96000")) is None
