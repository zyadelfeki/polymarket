from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


@pytest.fixture
def strategy() -> LatencyArbitrageEngine:
    binance_ws = MagicMock()
    binance_ws.get_current_price.return_value = None
    binance_ws.get_price = AsyncMock(return_value=None)
    binance_ws.get_price_data = AsyncMock(return_value=None)
    return LatencyArbitrageEngine(
        binance_ws=binance_ws,
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
    strategy._get_asset_price = AsyncMock(return_value=Decimal("96000"))
    strategy._fetch_orderbook_safe = AsyncMock(
        return_value={
            "bids": [["0.49", "100"]],
            "asks": [["0.50", "100"]],
        }
    )
    strategy._get_active_markets = AsyncMock(
        return_value=[
            {"id": "1", "question": "Will BTC 15 min be above $96,000?", "yes_token_id": "yes-1", "no_token_id": "no-1"},
            {"id": "2", "question": "Will BTC 15 min be above $97,000?", "yes_token_id": "yes-2", "no_token_id": "no-2"},
        ]
    )

    async def fake_check(market, btc_price, **kwargs):
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


def test_build_opportunity_carries_explicit_execution_contract(strategy: LatencyArbitrageEngine) -> None:
    opportunity = strategy._build_opportunity(
        market={"id": "m1", "question": "Will BTC be above $96,000 in 15 minutes?"},
        yes_token_id="yes-id",
        no_token_id="no-id",
        yes_price=Decimal("0.44"),
        no_price=Decimal("0.56"),
        true_prob=Decimal("0.60"),
        yes_edge=Decimal("0.16"),
        no_edge=Decimal("0.04"),
        charlie_confidence=Decimal("0.80"),
        btc_price=Decimal("97000"),
        threshold=Decimal("96000"),
        direction="UP",
        time_left=300,
    )

    assert opportunity is not None
    assert opportunity["selected_side"] == "YES"
    assert opportunity["token_id"] == "yes-id"
    assert opportunity["yes_token_id"] == "yes-id"
    assert opportunity["no_token_id"] == "no-id"
    assert opportunity["yes_price"] == Decimal("0.44")
    assert opportunity["no_price"] == Decimal("0.56")
    assert opportunity["market_price"] == Decimal("0.44")
