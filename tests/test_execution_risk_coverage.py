"""
Additional tests to improve execution and risk coverage.
Exercises kelly sizing, idempotency manager, and executor math paths.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from execution.idempotency_manager import IdempotencyManager
from execution.ultra_fast_executor import UltraFastExecutor
from risk.kelly_sizer import AdaptiveKellySizer


def test_idempotency_manager_roundtrip():
    manager = IdempotencyManager(db_path=":memory:", cache_ttl=300)
    key = manager.generate_key(
        market_id="market",
        side="BUY",
        size=Decimal("1.0"),
        price=Decimal("0.50"),
        strategy="test",
        outcome="YES",
    )

    assert manager.is_duplicate(key) is False
    manager.record_attempt(key, {"success": True, "order_id": "abc"})
    assert manager.is_duplicate(key) is True
    cached = manager.get_cached_result(key)
    assert cached["order_id"] == "abc"

    stats = manager.get_stats()
    assert stats["total_cached"] == 1
    assert stats["successful"] == 1


def test_idempotency_manager_clear_expired():
    manager = IdempotencyManager(db_path=":memory:", cache_ttl=0)
    key = manager.generate_key(
        market_id="market",
        side="BUY",
        size=Decimal("1.0"),
        price=Decimal("0.50"),
        strategy="test",
        outcome="YES",
    )
    manager.record_attempt(key, {"success": True, "order_id": "abc"})
    manager.clear_expired()
    assert manager.get_stats()["total_cached"] == 0


def test_kelly_sizer_positive_and_negative():
    sizer = AdaptiveKellySizer(
        config={
            "min_edge": Decimal("0.01"),
            "min_bet_size": Decimal("0.01"),
            "max_bet_pct": Decimal("5.0"),
            "max_aggregate_exposure": Decimal("20.0"),
        }
    )

    positive = sizer.calculate_bet_size(
        bankroll=Decimal("100"),
        win_probability=Decimal("0.60"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.10"),
        sample_size=30,
        current_aggregate_exposure=Decimal("0"),
        market_price=Decimal("0.50"),
    )
    assert positive.size > Decimal("0")

    negative = sizer.calculate_bet_size(
        bankroll=Decimal("100"),
        win_probability=Decimal("0.50"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.00"),
        sample_size=30,
        current_aggregate_exposure=Decimal("0"),
        market_price=Decimal("0.50"),
    )
    assert negative.size == Decimal("0")


@pytest.mark.asyncio
async def test_executor_calculate_bet_size_and_kelly():
    mock_execution = AsyncMock()
    mock_ledger = AsyncMock()
    mock_ledger.get_open_positions.return_value = []

    executor = UltraFastExecutor(execution_service=mock_execution, ledger=mock_ledger)

    assert executor.calculate_kelly(Decimal("0.5"), Decimal("0.9")) == Decimal("0")
    assert executor.calculate_kelly(Decimal("0.6"), Decimal("2")) > Decimal("0")

    opportunity = {
        "edge": Decimal("0.10"),
        "market_price": Decimal("0.50"),
        "true_prob": Decimal("0.70"),
        "side": "YES",
    }

    bet_size = await executor.calculate_bet_size(opportunity, Decimal("100"))
    assert bet_size > Decimal("0")


# ---------------------------------------------------------------------------
# Circuit-breaker gate on TradeExecutor
# ---------------------------------------------------------------------------

def test_trade_executor_requires_circuit_breaker():
    """TradeExecutor must refuse to instantiate without a circuit_breaker."""
    from unittest.mock import MagicMock
    from execution.trade_executor import TradeExecutor

    with pytest.raises(RuntimeError, match="circuit_breaker required"):
        TradeExecutor(
            polymarket_client=MagicMock(),
            bankroll_tracker=MagicMock(),
            kelly_sizer=MagicMock(),
            db=MagicMock(),
        )


@pytest.mark.asyncio
async def test_execute_trade_blocked_when_circuit_breaker_tripped():
    """execute_trade must return False and never call place_bet when breaker is open."""
    from unittest.mock import MagicMock, AsyncMock
    from execution.trade_executor import TradeExecutor

    mock_polymarket = AsyncMock()
    mock_polymarket.place_bet = AsyncMock(return_value=True)

    mock_kelly = MagicMock()
    mock_kelly.calculate_bet_size.return_value = 1.50

    mock_bankroll = MagicMock()
    mock_db = MagicMock()
    mock_db.log_trade.return_value = 1

    mock_breaker = MagicMock()
    mock_breaker.is_trading_allowed.return_value = False
    mock_breaker.breaker_reason = "Max drawdown exceeded: 20.0%"

    executor = TradeExecutor(
        polymarket_client=mock_polymarket,
        bankroll_tracker=mock_bankroll,
        kelly_sizer=mock_kelly,
        db=mock_db,
        circuit_breaker=mock_breaker,
    )

    opportunity = {
        "market_id": "test_market",
        "side": "YES",
        "confidence": 0.75,
        "edge": 0.10,
        "market_price": 0.50,
    }

    result = await executor.execute_trade(opportunity)

    assert result is False
    mock_polymarket.place_bet.assert_not_called()
    mock_breaker.is_trading_allowed.assert_called_once()


@pytest.mark.asyncio
async def test_execute_trade_records_after_placement():
    """execute_trade must call circuit_breaker.record_trade after a successful bet."""
    from unittest.mock import MagicMock, AsyncMock, call
    from execution.trade_executor import TradeExecutor

    mock_polymarket = AsyncMock()
    mock_polymarket.place_bet = AsyncMock(return_value=True)

    mock_kelly = MagicMock()
    mock_kelly.calculate_bet_size.return_value = 2.00

    mock_bankroll = MagicMock()
    mock_db = MagicMock()
    mock_db.log_trade.return_value = 42

    mock_breaker = MagicMock()
    mock_breaker.is_trading_allowed.return_value = True

    executor = TradeExecutor(
        polymarket_client=mock_polymarket,
        bankroll_tracker=mock_bankroll,
        kelly_sizer=mock_kelly,
        db=mock_db,
        circuit_breaker=mock_breaker,
    )

    opportunity = {
        "market_id": "test_market",
        "side": "YES",
        "confidence": 0.80,
        "edge": 0.12,
        "market_price": 0.50,
    }

    result = await executor.execute_trade(opportunity)

    assert result is True
    mock_breaker.record_trade.assert_called_once()
    call_kwargs = mock_breaker.record_trade.call_args
    # profit should be negative (capital at risk)
    profit_arg = call_kwargs[1].get("profit") or call_kwargs[0][0]
    assert profit_arg < Decimal("0"), "record_trade profit must be negative on trade open"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
