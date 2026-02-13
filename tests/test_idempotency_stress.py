"""
Idempotency stress test.
Simulates network retry scenario to verify duplicate prevention.
"""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from execution.ultra_fast_executor import UltraFastExecutor


@pytest.mark.asyncio
async def test_duplicate_prevention_basic():
    """Test that duplicate order is rejected or cached."""
    mock_execution = AsyncMock()
    mock_execution.place_order.return_value = {
        "success": True,
        "order_id": "0xABCD1234",
        "error": None,
        "filled_size": Decimal("10.00"),
        "avg_price": Decimal("0.52"),
        "timestamp": 1234567890.0,
    }

    mock_ledger = AsyncMock()
    executor = UltraFastExecutor(mock_execution, mock_ledger)

    market_id = "test_market"
    outcome = "YES"
    side = "BUY"
    price = Decimal("0.52")
    size = Decimal("10.00")

    result1 = await executor.execute_order(
        market_id=market_id,
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        token_id="token_yes",
    )

    assert result1["success"] is True
    assert result1["order_id"] == "0xABCD1234"

    result2 = await executor.execute_order(
        market_id=market_id,
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        token_id="token_yes",
    )

    if result2.get("success"):
        assert result2["order_id"] == "0xABCD1234"
    else:
        assert "duplicate" in result2.get("error", "").lower()

    assert mock_execution.place_order.call_count == 1


@pytest.mark.asyncio
async def test_stress_10_retries():
    """Stress test: 10 rapid retry attempts should result in 1 API call."""
    mock_execution = AsyncMock()
    mock_execution.place_order.return_value = {
        "success": True,
        "order_id": "0xSTRESS123",
        "error": None,
        "filled_size": Decimal("5.00"),
        "avg_price": Decimal("0.60"),
        "timestamp": 1234567890.0,
    }

    mock_ledger = AsyncMock()
    executor = UltraFastExecutor(mock_execution, mock_ledger)

    market_id = "stress_test_market"
    outcome = "NO"
    side = "BUY"
    price = Decimal("0.60")
    size = Decimal("5.00")

    results = []
    for _ in range(10):
        result = await executor.execute_order(
            market_id=market_id,
            outcome=outcome,
            side=side,
            price=price,
            size=size,
            token_id="token_no",
        )
        results.append(result)
        await asyncio.sleep(0.01)

    unique_order_ids = {r.get("order_id") for r in results if r.get("success") and r.get("order_id")}

    assert len(unique_order_ids) == 1
    assert "0xSTRESS123" in unique_order_ids
    assert mock_execution.place_order.call_count == 1


@pytest.mark.asyncio
async def test_different_params_not_duplicate():
    """Verify different order parameters are NOT treated as duplicates."""
    mock_execution = AsyncMock()
    mock_execution.place_order.return_value = {
        "success": True,
        "order_id": "0xDIFF123",
        "error": None,
        "filled_size": Decimal("10.00"),
        "avg_price": Decimal("0.50"),
        "timestamp": 1234567890.0,
    }

    mock_ledger = AsyncMock()
    executor = UltraFastExecutor(mock_execution, mock_ledger)

    result1 = await executor.execute_order(
        market_id="market_A",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10.00"),
        token_id="token_yes_A",
    )

    result2 = await executor.execute_order(
        market_id="market_B",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10.00"),
        token_id="token_yes_B",
    )

    assert result1["success"] is True
    assert result2["success"] is True
    assert mock_execution.place_order.call_count == 2


@pytest.mark.asyncio
async def test_cache_expiration():
    """Test that cache expires after TTL."""
    mock_execution = AsyncMock()
    mock_execution.place_order.return_value = {
        "success": True,
        "order_id": "0xEXPIRE123",
        "error": None,
        "filled_size": Decimal("10.00"),
        "avg_price": Decimal("0.50"),
        "timestamp": 1234567890.0,
    }

    mock_ledger = AsyncMock()
    executor = UltraFastExecutor(mock_execution, mock_ledger)
    executor.idempotency.cache_ttl = 2

    result1 = await executor.execute_order(
        market_id="expire_test",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10.00"),
        token_id="token_yes",
    )

    assert result1["success"] is True
    assert mock_execution.place_order.call_count == 1

    result2 = await executor.execute_order(
        market_id="expire_test",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10.00"),
        token_id="token_yes",
    )

    assert mock_execution.place_order.call_count == 1

    await asyncio.sleep(3)

    result3 = await executor.execute_order(
        market_id="expire_test",
        outcome="YES",
        side="BUY",
        price=Decimal("0.50"),
        size=Decimal("10.00"),
        token_id="token_yes",
    )

    assert result3["success"] is True
    assert mock_execution.place_order.call_count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
