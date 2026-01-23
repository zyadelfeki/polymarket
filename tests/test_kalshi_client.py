import os
import pytest

from data_feeds.kalshi_client_v1 import KalshiClient


KALSHI_API_KEY = os.getenv("KALSHI_API_KEY")
KALSHI_API_SECRET = os.getenv("KALSHI_API_SECRET")
KALSHI_TEST_MARKET = os.getenv("KALSHI_TEST_MARKET")


@pytest.mark.asyncio
async def test_kalshi_initialize():
    """Test Kalshi client initialization (requires env credentials)."""
    if not KALSHI_API_KEY or not KALSHI_API_SECRET:
        pytest.skip("KALSHI_API_KEY/KALSHI_API_SECRET not set")

    client = KalshiClient(api_key=KALSHI_API_KEY, api_secret=KALSHI_API_SECRET, paper=True)
    await client.initialize()

    assert client.session is not None
    assert client.base_url

    await client.close()


@pytest.mark.asyncio
async def test_kalshi_orderbook_fetch():
    """Test fetching orderbook for a known market (requires env)."""
    if not KALSHI_API_KEY or not KALSHI_API_SECRET:
        pytest.skip("KALSHI_API_KEY/KALSHI_API_SECRET not set")
    if not KALSHI_TEST_MARKET:
        pytest.skip("KALSHI_TEST_MARKET not set")

    client = KalshiClient(api_key=KALSHI_API_KEY, api_secret=KALSHI_API_SECRET, paper=True)
    await client.initialize()

    orderbook = await client.get_market_orderbook(KALSHI_TEST_MARKET)
    assert orderbook.market_id == KALSHI_TEST_MARKET
    assert orderbook.bid >= 0
    assert orderbook.ask >= 0

    await client.close()
