"""Manual idempotency validation for production deployment."""
import asyncio
import tempfile
import pytest
from decimal import Decimal

from services.execution_service_v2 import ExecutionServiceV2
from data_feeds.polymarket_client_v2 import PolymarketClientV2
from database.ledger_async import AsyncLedger


@pytest.mark.asyncio
async def test_duplicate():
    """Verify duplicate order prevention in real execution environment."""
    client = PolymarketClientV2(paper_trading=True)
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_db:
        db_path = tmp_db.name

    ledger = AsyncLedger(db_path=db_path)
    service = ExecutionServiceV2(client, ledger)
    try:
        # First call (will place order)
        print("=== FIRST CALL (should place order) ===")
        result1 = await service.place_order(
            strategy="manual_test",
            market_id="0xd3460cd313aa9759ea67a966e9a499cb65964d6e2a2ff6902472aa83005383bb",
            token_id="yes",
            side="BUY",
            quantity=Decimal("5"),
            price=Decimal("0.50"),
            correlation_id="manual_test_001",
            idempotency_key="MANUAL_TEST_KEY_123",
        )

        print(
            "Result 1: success={} , is_duplicate={} , order_id={}".format(
                result1["success"], result1.get("is_duplicate"), result1["order_id"]
            )
        )
        assert result1["success"] is True, "First call should succeed"
        assert result1.get("is_duplicate") is False, "First call should NOT be duplicate"
        order_id_1 = result1["order_id"]

        # Second call (should hit cache)
        print("\n=== SECOND CALL (should hit cache) ===")
        result2 = await service.place_order(
            strategy="manual_test",
            market_id="0xd3460cd313aa9759ea67a966e9a499cb65964d6e2a2ff6902472aa83005383bb",
            token_id="yes",
            side="BUY",
            quantity=Decimal("5"),
            price=Decimal("0.50"),
            correlation_id="manual_test_002",  # DIFFERENT correlation_id
            idempotency_key="MANUAL_TEST_KEY_123",  # SAME idempotency_key
        )

        print(
            "Result 2: success={} , is_duplicate={} , order_id={}".format(
                result2["success"], result2.get("is_duplicate"), result2["order_id"]
            )
        )
        assert result2["success"] is True, "Second call should also succeed"
        assert result2.get("is_duplicate") is True, "Second call MUST be marked duplicate"
        assert (
            result2["order_id"] == order_id_1
        ), "Both calls must return same order_id: {} vs {}".format(
            order_id_1, result2["order_id"]
        )
        assert (
            result2.get("correlation_id") == "manual_test_002"
        ), "Correlation ID should be updated (not stale)"

        print("\n✅ ✅ ✅ MANUAL VALIDATION PASSED ✅ ✅ ✅")
        print("   - Duplicate order prevented")
        print("   - Same order_id returned: {}".format(order_id_1))
        print("   - Correlation ID updated correctly")
        print("   - Ready for production deployment")
    finally:
        await service.stop()
        await ledger.close()


if __name__ == "__main__":
    asyncio.run(test_duplicate())
