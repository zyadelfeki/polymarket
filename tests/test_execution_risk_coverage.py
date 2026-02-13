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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
