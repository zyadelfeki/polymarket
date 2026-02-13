import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution.ultra_fast_executor import UltraFastExecutor


@pytest.fixture
def executor() -> UltraFastExecutor:
    execution = MagicMock()
    execution.place_order = AsyncMock(return_value={"success": True, "order_id": "ord-1"})
    ledger = MagicMock()
    ledger.get_open_positions = AsyncMock(return_value=[])

    return UltraFastExecutor(
        execution_service=execution,
        ledger=ledger,
        config={"order_timeout_seconds": "0.02", "limit_price_buffer": "0.01", "max_trade_pct": "0.05"},
    )


@pytest.mark.asyncio
async def test_execute_order_idempotent_cache_hit(executor: UltraFastExecutor) -> None:
    first = await executor.execute_order(
        market_id="m-1",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10"),
        token_id="tok-1",
    )
    second = await executor.execute_order(
        market_id="m-1",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10"),
        token_id="tok-1",
    )

    assert first["success"] is True
    assert second["order_id"] == first["order_id"]
    assert executor.execution.place_order.await_count == 1


@pytest.mark.asyncio
async def test_execute_order_duplicate_without_cached_result(executor: UltraFastExecutor) -> None:
    executor.idempotency.is_duplicate = MagicMock(return_value=True)
    executor.idempotency.get_cached_result = MagicMock(return_value=None)

    result = await executor.execute_order(
        market_id="m-2",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("1"),
        token_id="tok-2",
    )
    assert result["success"] is False
    assert "Duplicate order prevented" in result["error"]


@pytest.mark.asyncio
async def test_execute_order_invalid_non_dict_result(executor: UltraFastExecutor) -> None:
    executor.execution.place_order = AsyncMock(return_value="invalid")
    result = await executor.execute_order(
        market_id="m-3",
        outcome="NO",
        side="BUY",
        price=Decimal("0.40"),
        size=Decimal("2"),
        token_id="tok-3",
    )
    assert result["success"] is False
    assert result["error"] == "invalid_result"


@pytest.mark.asyncio
async def test_calculate_bet_size_charlie_multiplier_and_cap(executor: UltraFastExecutor) -> None:
    executor.kelly = MagicMock()
    executor.kelly.calculate_bet_size.return_value = SimpleNamespace(size=Decimal("50"))

    charlie = MagicMock()
    charlie.last_confidence = Decimal("0.9")
    charlie.calculate_kelly_multiplier.return_value = Decimal("2.0")
    executor.charlie = charlie

    opportunity = {
        "edge": Decimal("0.2"),
        "market_price": Decimal("0.5"),
        "true_prob": Decimal("0.7"),
        "side": "YES",
        "charlie_confidence": Decimal("0.95"),
    }
    size = await executor.calculate_bet_size(opportunity, capital=Decimal("100"))
    assert size == Decimal("5.00")


@pytest.mark.asyncio
async def test_current_aggregate_exposure_handles_exception() -> None:
    execution = MagicMock()
    execution.place_order = AsyncMock(return_value={"success": True})
    ledger = MagicMock()
    ledger.get_open_positions = AsyncMock(side_effect=RuntimeError("boom"))
    ex = UltraFastExecutor(execution_service=execution, ledger=ledger)

    assert await ex._current_aggregate_exposure() == Decimal("0")


@pytest.mark.asyncio
async def test_execute_trade_timeout_path(executor: UltraFastExecutor) -> None:
    async def slow_execute_order(**kwargs):
        await asyncio.sleep(0.2)
        return {"success": True}

    executor.execute_order = slow_execute_order

    opportunity = {
        "market_id": "m-timeout",
        "token_id": "tok-timeout",
        "market_price": Decimal("0.50"),
        "side": "YES",
        "edge": Decimal("0.2"),
        "true_prob": Decimal("0.7"),
    }

    result = await executor.execute_trade(opportunity=opportunity, capital=Decimal("100"), bet_size=Decimal("1"))
    assert result is None
