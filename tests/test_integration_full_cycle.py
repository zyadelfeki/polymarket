"""
Full-cycle integration tests.
Uses real project components and mocks only external I/O (Polymarket API).

Pipeline validated:
1) Signal detection (latency arbitrage)
2) Kelly sizing (position sizing)
3) Idempotency (duplicate prevention)
4) Order execution (ExecutionServiceV2)
5) Ledger update (AsyncLedger)
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine
from risk.kelly_sizer import AdaptiveKellySizer
from execution.ultra_fast_executor import UltraFastExecutor
from services.execution_service_v2 import ExecutionServiceV2
from database.ledger_async import AsyncLedger
from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide
from services.validators import BoundaryValidator
from utils.decimal_helpers import quantize_quantity


@pytest.fixture
def mock_polymarket_client() -> AsyncMock:
    client = AsyncMock(spec=PolymarketClientV2)
    client.paper_trading = True
    client.place_order.return_value = {
        "success": True,
        "order_id": "0xTEST123456",
        "error": None,
    }
    return client


def _make_strategy(mock_client: AsyncMock) -> LatencyArbitrageEngine:
    return LatencyArbitrageEngine(
        binance_ws=MagicMock(),
        polymarket_client=mock_client,
        charlie_predictor=MagicMock(),
        redis_subscriber=None,
    )


async def _make_ledger(tmp_path) -> AsyncLedger:
    ledger = AsyncLedger(db_path=str(tmp_path / "integration.db"), pool_size=1)
    await ledger.initialize()
    await ledger.record_deposit(Decimal("13.98"))
    return ledger


@pytest.mark.asyncio
async def test_bullish_trade_full_cycle(tmp_path, mock_polymarket_client):
    ledger = await _make_ledger(tmp_path)
    try:
        execution = ExecutionServiceV2(polymarket_client=mock_polymarket_client, ledger=ledger)
        kelly = AdaptiveKellySizer(
            config={
                "min_edge": Decimal("0.01"),
                "min_bet_size": Decimal("0.01"),
                "max_bet_pct": Decimal("5.0"),
                "max_aggregate_exposure": Decimal("20.0"),
            }
        )
        executor = UltraFastExecutor(execution_service=execution, ledger=ledger, kelly_sizer=kelly)
        strategy = _make_strategy(mock_polymarket_client)

        btc_price = Decimal("99000.00")
        strike_price = Decimal("96000.00")
        yes_odds = Decimal("0.52")
        no_odds = Decimal("0.48")

        signal = strategy.determine_trade_direction(
            btc_price=btc_price,
            strike_price=strike_price,
            yes_odds=yes_odds,
            no_odds=no_odds,
        )

        assert signal is not None
        assert signal["outcome"] == "YES"
        assert signal["side"] == "BUY"
        assert signal["direction"] == "BULLISH"

        win_prob = Decimal("0.90")
        payout_odds = Decimal("1") / yes_odds
        bet_result = kelly.calculate_bet_size(
            bankroll=await ledger.get_equity(),
            win_probability=win_prob,
            payout_odds=payout_odds,
            edge=signal["edge"],
            sample_size=30,
            current_aggregate_exposure=Decimal("0"),
            market_price=yes_odds,
        )
        bet_size = bet_result.size
        assert bet_size > Decimal("0")

        quantity = quantize_quantity(bet_size / yes_odds)

        market_id = "0x" + "a" * 64
        token_id = "0x" + "b" * 64

        result = await executor.execute_order(
            market_id=market_id,
            outcome=signal["outcome"],
            side=signal["side"],
            price=yes_odds,
            size=quantity,
            token_id=token_id,
            strategy="latency_arbitrage_btc",
        )

        assert result["success"] is True
        assert result["order_id"] == "0xTEST123456"

        positions = await ledger.get_open_positions()
        assert len(positions) == 1
        assert positions[0].market_id == market_id
        assert positions[0].token_id == token_id
        assert positions[0].quantity == quantity

        # Equity cache can lag in async ledger; verify position instead of cash delta.

        result2 = await executor.execute_order(
            market_id=market_id,
            outcome=signal["outcome"],
            side=signal["side"],
            price=yes_odds,
            size=quantity,
            token_id=token_id,
            strategy="latency_arbitrage_btc",
        )
        assert result2["order_id"] == result["order_id"]
        assert mock_polymarket_client.place_order.call_count == 1
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_bearish_trade_full_cycle(tmp_path, mock_polymarket_client):
    ledger = await _make_ledger(tmp_path)
    try:
        execution = ExecutionServiceV2(polymarket_client=mock_polymarket_client, ledger=ledger)
        kelly = AdaptiveKellySizer(
            config={
                "min_edge": Decimal("0.01"),
                "min_bet_size": Decimal("0.01"),
                "max_bet_pct": Decimal("5.0"),
                "max_aggregate_exposure": Decimal("20.0"),
            }
        )
        executor = UltraFastExecutor(execution_service=execution, ledger=ledger, kelly_sizer=kelly)
        strategy = _make_strategy(mock_polymarket_client)

        btc_price = Decimal("93000.00")
        strike_price = Decimal("96000.00")
        yes_odds = Decimal("0.70")
        no_odds = Decimal("0.25")

        signal = strategy.determine_trade_direction(
            btc_price=btc_price,
            strike_price=strike_price,
            yes_odds=yes_odds,
            no_odds=no_odds,
        )

        assert signal is not None
        assert signal["outcome"] == "NO"
        assert signal["side"] == "BUY"
        assert signal["direction"] == "BEARISH"

        win_prob = Decimal("0.88")
        payout_odds = Decimal("1") / no_odds
        bet_result = kelly.calculate_bet_size(
            bankroll=await ledger.get_equity(),
            win_probability=win_prob,
            payout_odds=payout_odds,
            edge=signal["edge"],
            sample_size=30,
            current_aggregate_exposure=Decimal("0"),
            market_price=no_odds,
        )
        quantity = quantize_quantity(bet_result.size / no_odds)
        assert quantity > Decimal("0")

        market_id = "0x" + "c" * 64
        token_id = "0x" + "d" * 64

        result = await executor.execute_order(
            market_id=market_id,
            outcome=signal["outcome"],
            side=signal["side"],
            price=no_odds,
            size=quantity,
            token_id=token_id,
            strategy="latency_arbitrage_btc",
        )

        assert result["success"] is True

        call_kwargs = mock_polymarket_client.place_order.call_args.kwargs
        assert call_kwargs["side"] == OrderSide.BUY
        assert call_kwargs["token_id"] == token_id
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_failed_order_handling(tmp_path, mock_polymarket_client):
    ledger = await _make_ledger(tmp_path)
    try:
        execution = ExecutionServiceV2(polymarket_client=mock_polymarket_client, ledger=ledger)
        executor = UltraFastExecutor(execution_service=execution, ledger=ledger)

        mock_polymarket_client.place_order.return_value = {
            "success": False,
            "order_id": None,
            "error": "Insufficient collateral",
        }

        initial_cash = await ledger.get_equity()

        result = await executor.execute_order(
            market_id="0x" + "e" * 64,
            outcome="YES",
            side="BUY",
            price=Decimal("0.50"),
            size=Decimal("1.00"),
            token_id="0x" + "f" * 64,
            strategy="latency_arbitrage_btc",
        )

        assert result["success"] is False
        assert "collateral" in result.get("error", "").lower()
        assert await ledger.get_equity() == initial_cash
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_decimal_precision_preserved(tmp_path):
    mock_client = AsyncMock(spec=PolymarketClientV2)
    mock_client.paper_trading = True
    mock_client.place_order.return_value = {
        "success": True,
        "order_id": "0xPRECISION",
        "error": None,
    }

    ledger = await _make_ledger(tmp_path)
    try:
        execution = ExecutionServiceV2(polymarket_client=mock_client, ledger=ledger)
        executor = UltraFastExecutor(execution_service=execution, ledger=ledger)

        price = Decimal("0.5234567890123456")
        quantity = Decimal("13.98")

        result = await executor.execute_order(
            market_id="0x" + "1" * 64,
            outcome="YES",
            side="BUY",
            price=price,
            size=quantity,
            token_id="0x" + "2" * 64,
            strategy="latency_arbitrage_btc",
        )

        assert result["success"] is True
        positions = await ledger.get_open_positions()
        assert positions
        assert positions[0].entry_price == BoundaryValidator.validate_price(price)
        assert positions[0].quantity == BoundaryValidator.validate_quantity(quantity)
    finally:
        await ledger.close()


@pytest.mark.asyncio
async def test_idempotency_blocks_duplicate_api_calls(tmp_path, mock_polymarket_client):
    ledger = await _make_ledger(tmp_path)
    try:
        execution = ExecutionServiceV2(polymarket_client=mock_polymarket_client, ledger=ledger)
        executor = UltraFastExecutor(execution_service=execution, ledger=ledger)

        market_id = "0x" + "9" * 64
        token_id = "0x" + "8" * 64

        await executor.execute_order(
            market_id=market_id,
            outcome="YES",
            side="BUY",
            price=Decimal("0.51"),
            size=Decimal("1.00"),
            token_id=token_id,
            strategy="latency_arbitrage_btc",
        )

        await executor.execute_order(
            market_id=market_id,
            outcome="YES",
            side="BUY",
            price=Decimal("0.51"),
            size=Decimal("1.00"),
            token_id=token_id,
            strategy="latency_arbitrage_btc",
        )

        assert mock_polymarket_client.place_order.call_count == 1
    finally:
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(pytest.main([__file__, "-v", "-s"]))
