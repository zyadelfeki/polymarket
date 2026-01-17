import os
import sys
import asyncio
from decimal import Decimal

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from services.execution_service_v2 import ExecutionServiceV2, OrderResult
from services.idempotency import IdempotencyCache
from data_feeds.polymarket_client_v2 import PolymarketClientV2, OrderSide
from services.error_codes import ErrorCode


class StubLedger:
    async def record_trade_entry(self, **kwargs):
        return "position_1"


@pytest.mark.asyncio
async def test_place_order_returns_order_result():
    """ExecutionServiceV2.place_order() must return OrderResult with correct fields."""
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()

    service = ExecutionServiceV2(client, ledger)
    result = await service.place_order(
        strategy="test",
        market_id="0xtest",
        token_id="yes_token",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
    )

    assert isinstance(result, OrderResult), f"Expected OrderResult, got {type(result)}"
    assert hasattr(result, "success"), "Missing 'success' attribute"
    assert hasattr(result, "order_id"), "Missing 'order_id' attribute"
    assert isinstance(result.success, bool), f"success should be bool, got {type(result.success)}"


@pytest.mark.asyncio
async def test_decimal_precision_enforced():
    """All monetary values must be Decimal, not float."""
    from services.execution_service_v2 import OrderRequest

    req = OrderRequest(
        strategy="test",
        market_id="0xtest",
        token_id="yes",
        side=OrderSide.BUY,
        quantity=Decimal("10.5678"),
        price=Decimal("0.50"),
    )

    assert isinstance(req.quantity, Decimal), "Quantity must be Decimal"
    assert isinstance(req.price, Decimal), "Price must be Decimal"
    assert req.quantity == Decimal("10.5678"), "Precision lost"


@pytest.mark.asyncio
async def test_error_result_is_structured():
    """OrderResult with success=False must have error and error_code."""
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()

    service = ExecutionServiceV2(client, ledger)

    result = await service.place_order(
        strategy="test",
        market_id="0xtest",
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("1.50"),
    )

    assert result.success is False, "Should reject invalid price"
    assert result.error is not None, "Must have error message"
    assert result.error_code == ErrorCode.INVALID_PRICE.value


@pytest.mark.asyncio
async def test_idempotency_prevents_duplicates():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()
    service = ExecutionServiceV2(client, ledger)

    result1 = await service.place_order(
        strategy="test",
        market_id="0xtest",
        token_id="yes_token",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="test_key_123",
    )

    assert result1["success"] is True
    assert result1["is_duplicate"] is False
    order_id_1 = result1["order_id"]

    result2 = await service.place_order(
        strategy="test",
        market_id="0xtest",
        token_id="yes_token",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        idempotency_key="test_key_123",
    )

    assert result2["success"] is True
    assert result2["is_duplicate"] is True
    assert result2["order_id"] == order_id_1


@pytest.mark.asyncio
async def test_correlation_id_flows_through_service():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()

    service = ExecutionServiceV2(client, ledger)

    correlation_id = "corr_test_123"
    result = await service.place_order(
        strategy="test",
        market_id="0xtest",
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.50"),
        correlation_id=correlation_id,
    )

    assert result.success is True
    assert result.order_id in service.orders
    order_state = service.orders[result.order_id]
    assert order_state.request.metadata.get("correlation_id") == correlation_id


@pytest.mark.asyncio
async def test_idempotency_auto_key_generation():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()
    service = ExecutionServiceV2(client, ledger)

    result1 = await service.place_order(
        strategy="arb",
        market_id="0x123",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.60"),
    )
    order_id_1 = result1["order_id"]

    result2 = await service.place_order(
        strategy="arb",
        market_id="0x123",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.60"),
    )

    assert result2["is_duplicate"] is True
    assert result2["order_id"] == order_id_1

    result3 = await service.place_order(
        strategy="arb",
        market_id="0x123",
        token_id="yes",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("0.60"),
    )

    assert result3["is_duplicate"] is False
    assert result3["order_id"] != order_id_1


@pytest.mark.asyncio
async def test_idempotency_cache_ttl_expiry():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()
    short_ttl_cache = IdempotencyCache(ttl_seconds=1)
    service = ExecutionServiceV2(client, ledger, idempotency_cache=short_ttl_cache)

    result1 = await service.place_order(
        strategy="test",
        market_id="0xttl",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.50"),
        idempotency_key="ttl_key",
    )
    order_id_1 = result1["order_id"]

    result2 = await service.place_order(
        strategy="test",
        market_id="0xttl",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.50"),
        idempotency_key="ttl_key",
    )

    assert result2["is_duplicate"] is True

    await asyncio.sleep(1.5)

    result3 = await service.place_order(
        strategy="test",
        market_id="0xttl",
        token_id="yes",
        side="BUY",
        quantity=Decimal("5"),
        price=Decimal("0.50"),
        idempotency_key="ttl_key",
    )

    assert result3["is_duplicate"] is False
    assert result3["order_id"] != order_id_1


@pytest.mark.asyncio
async def test_idempotency_cache_logging():
    client = PolymarketClientV2(paper_trading=True)
    ledger = StubLedger()
    service = ExecutionServiceV2(client, ledger)

    await service.place_order(
        strategy="log_test",
        market_id="0xlog",
        token_id="yes",
        side="BUY",
        quantity=Decimal("3"),
        price=Decimal("0.45"),
        correlation_id="corr_123",
    )

    result2 = await service.place_order(
        strategy="log_test",
        market_id="0xlog",
        token_id="yes",
        side="BUY",
        quantity=Decimal("3"),
        price=Decimal("0.45"),
        correlation_id="corr_456",
    )

    assert result2["correlation_id"] == "corr_456"
    assert result2["idempotency_key"] is not None
    assert result2["is_duplicate"] is True
