"""
Test OrderResult contract enforcement.
Verifies ALL execution paths return consistent structure.
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from data_feeds.polymarket_client_v2 import PolymarketClientV2


@pytest.mark.asyncio
async def test_successful_order_returns_orderresult():
    """Test successful order returns complete OrderResult."""
    client = PolymarketClientV2(api_key="test_key", paper_trading=False)
    client.authenticated = True
    client.client = object()

    with patch.object(
        client,
        "_execute_with_retries",
        new=AsyncMock(
            return_value={
                "orderID": "0xABCDEF123456",
                "filled": "10.5",
                "avgPrice": "0.52",
            }
        ),
    ):
        result = await client.place_order(
            market_id="test_market",
            token_id="token_yes",
            side="BUY",
            price=Decimal("0.52"),
            size=Decimal("10.00"),
        )

    assert isinstance(result, dict), "Must return dict (OrderResult)"
    assert result["success"] is True, "Success flag must be True"
    assert result["order_id"] == "0xABCDEF123456", "Must include order ID"
    assert result["error"] is None, "No error on success"
    assert isinstance(result["filled_size"], Decimal), "filled_size must be Decimal"
    assert isinstance(result["avg_price"], Decimal), "avg_price must be Decimal"
    assert result["filled_size"] == Decimal("10.5"), "Correct filled size"
    assert result["avg_price"] == Decimal("0.52"), "Correct avg price"
    assert isinstance(result["timestamp"], float), "timestamp must be float"


@pytest.mark.asyncio
async def test_failed_order_returns_orderresult():
    """Test API failure returns OrderResult (not None or exception)."""
    client = PolymarketClientV2(api_key="test_key", paper_trading=False)

    result = await client.place_order(
        market_id="test_market",
        token_id="token_yes",
        side="BUY",
        price=Decimal("0.52"),
        size=Decimal("10.00"),
    )

    assert isinstance(result, dict), "Must return dict even on failure"
    assert result["success"] is False, "Success flag must be False"
    assert result["order_id"] is None, "No order ID on failure"
    assert result["error"] is not None, "Must include error message"
    assert result["timestamp"] is not None, "Must include timestamp"


@pytest.mark.asyncio
async def test_exception_returns_orderresult():
    """Test network exception returns OrderResult (doesn't raise)."""
    client = PolymarketClientV2(api_key="test_key", paper_trading=False)
    client.authenticated = True
    client.client = object()

    with patch.object(
        client,
        "_execute_with_retries",
        new=AsyncMock(side_effect=Exception("Network timeout")),
    ):
        result = await client.place_order(
            market_id="test_market",
            token_id="token_yes",
            side="BUY",
            price=Decimal("0.52"),
            size=Decimal("10.00"),
        )

    assert isinstance(result, dict), "Must return dict even on exception"
    assert result["success"] is False, "Success must be False on exception"
    assert result["error"] is not None, "Must include error description"
    assert "timeout" in result["error"].lower(), "Error should describe issue"


@pytest.mark.asyncio
async def test_decimal_types_preserved():
    """Test that Decimal types are preserved (not converted to float)."""
    client = PolymarketClientV2(api_key="test_key", paper_trading=False)
    client.authenticated = True
    client.client = object()

    with patch.object(
        client,
        "_execute_with_retries",
        new=AsyncMock(
            return_value={
                "orderID": "0x123",
                "filled": "13.98",
                "avgPrice": "0.5234",
            }
        ),
    ):
        result = await client.place_order(
            market_id="test_market",
            token_id="token_yes",
            side="BUY",
            price=Decimal("0.5234"),
            size=Decimal("13.98"),
        )

    assert result["filled_size"] == Decimal("13.98"), "Must preserve exact value"
    assert result["avg_price"] == Decimal("0.5234"), "Must preserve exact price"
    assert not isinstance(result["filled_size"], float), "Must NOT be float"
    assert not isinstance(result["avg_price"], float), "Must NOT be float"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
