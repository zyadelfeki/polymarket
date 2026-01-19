import os
import sys
from decimal import Decimal

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.execution_service_v2 import ExecutionServiceV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2


class StubLedger:
    """Minimal ledger stub for testing (no DB required)."""

    async def insert_order(self, **kwargs):
        pass

    async def record_trade_entry(self, **kwargs):
        return 1


@pytest.mark.asyncio
async def test_cache_hit_avoids_duplicate_market_call():
    """
    CRITICAL: Verify that cache hit PREVENTS actual market call.

    Expected behavior:
    1. First call places order on market (count = 1)
    2. Second call with same key hits cache (count = 1, no new call)
    3. Both calls return success=True with same order_id
    """
    call_count = 0

    async def mock_place_order_with_tracking(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"success": True, "order_id": f"ORDER_{call_count}"}

    client = PolymarketClientV2(paper_trading=True)
    service = ExecutionServiceV2(client, StubLedger())

    original_place_order = service.client.place_order
    service.client.place_order = mock_place_order_with_tracking

    result1 = await service.place_order(
        strategy="test",
        market_id="0x123",
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="live_test_key"
    )

    assert result1["success"] is True
    assert result1["is_duplicate"] is False
    assert call_count == 1, f"First call should hit market once, got {call_count}"
    order_id_1 = result1["order_id"]

    result2 = await service.place_order(
        strategy="test",
        market_id="0x123",
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="live_test_key"
    )

    assert result2["success"] is True
    assert result2["is_duplicate"] is True
    assert call_count == 1, (
        "Call count should STILL be 1 (cache prevented 2nd market call), "
        f"got {call_count}"
    )
    assert result2["order_id"] == order_id_1

    service.client.place_order = original_place_order


@pytest.mark.asyncio
async def test_correlation_id_preserved_on_cache_hit():
    """
    Verify correlation_id is UPDATED on cache hit (not stale).

    Expected:
    - First call: correlation_id = "corr_123"
    - Second call: correlation_id = "corr_456" (different)
    - Result should return "corr_456" (not cached "corr_123")
    """
    service = ExecutionServiceV2(PolymarketClientV2(paper_trading=True), StubLedger())

    result1 = await service.place_order(
        strategy="test",
        market_id="0xcorr",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.60"),
        correlation_id="corr_123",
        idempotency_key="corr_key"
    )

    assert result1["correlation_id"] == "corr_123"

    result2 = await service.place_order(
        strategy="test",
        market_id="0xcorr",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.60"),
        correlation_id="corr_456",
        idempotency_key="corr_key"
    )

    assert result2["is_duplicate"] is True
    assert result2["correlation_id"] == "corr_456", (
        f"Should use new correlation_id, got {result2['correlation_id']}"
    )


@pytest.mark.asyncio
async def test_cache_metadata_includes_all_fields():
    """
    Verify cached result includes required fields for order tracking.
    """
    service = ExecutionServiceV2(PolymarketClientV2(paper_trading=True), StubLedger())

    result = await service.place_order(
        strategy="metadata_test",
        market_id="0xmeta",
        token_id="no",
        side="SELL",
        quantity=Decimal("3.5"),
        price=Decimal("0.35"),
        correlation_id="meta_123"
    )

    required_fields = [
        "success",
        "order_id",
        "is_duplicate",
        "correlation_id",
        "idempotency_key",
    ]

    for field in required_fields:
        assert field in result.__dict__, f"Result missing required field: {field}"
        assert result[field] is not None, f"Field {field} is None"
