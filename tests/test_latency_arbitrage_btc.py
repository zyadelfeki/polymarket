from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


class StubBinance:
    def __init__(self, price: Decimal, volatility: float = 3.0):
        self.price = Decimal(str(price))
        self.volatility = volatility

    async def get_price(self, symbol: str):
        return self.price

    def get_volatility(self, symbol: str, window_seconds: int = 60):
        return self.volatility


class StubPolymarket:
    def __init__(self, market, yes_price: Decimal, no_price: Decimal):
        self.market = market
        self.yes_price = Decimal(str(yes_price))
        self.no_price = Decimal(str(no_price))

    async def get_markets(self, active: bool = True, limit: int = 200):
        return [self.market]

    async def get_market_orderbook_summary(self, market_id: str):
        return {
            "market_id": market_id,
            "ask": self.yes_price,
            "bid": self.yes_price,
            "bid_volume": Decimal("100"),
            "ask_volume": Decimal("100"),
        }

    async def get_orderbook(self, token_id: str):
        return {
            "bids": [{"price": str(self.no_price), "size": "10"}],
            "asks": [{"price": str(self.no_price), "size": "10"}],
        }


class StubCharlie:
    async def predict_15min_move(self, **_):
        return {"probability": 0.6, "confidence": 0.7}


def test_market_filter_accepts_bitcoin_and_btc_variants():
    engine = LatencyArbitrageEngine(
        binance_ws=StubBinance(Decimal("95000"), volatility=3.0),
        polymarket_client=StubPolymarket({}, yes_price=Decimal("0.50"), no_price=Decimal("0.50")),
        charlie_predictor=StubCharlie(),
        config={"min_edge": 0.03, "max_edge": 0.99, "min_volatility_pct": 0.0},
    )

    markets = [
        {
            "id": "m_bitcoin",
            "question": "Bitcoin Up or Down - 15 minute",
            "slug": "bitcoin-up-or-down-15m",
            "closed": False,
        },
        {
            "id": "m_btc",
            "question": "BTC Up or Down - 15 minute",
            "slug": "btc-up-or-down-15m",
            "closed": False,
        },
    ]

    selected = engine._select_markets_for_all_timeframes(asset="BTC", markets=markets)
    selected_ids = {item["data"].get("id") for item in selected}
    assert "m_bitcoin" in selected_ids
    assert "m_btc" in selected_ids


@pytest.mark.asyncio
async def test_latency_arbitrage_yes_signal():
    market = {
        "id": "m1",
        "question": "Bitcoin Up or Down - 15 minute",
        "startingPrice": "94000",
        "tokens": [
            {"outcome": "YES", "token_id": "yes1", "price": 0.5},
            {"outcome": "NO", "token_id": "no1", "price": 0.5},
        ],
        "end_date": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }

    engine = LatencyArbitrageEngine(
        binance_ws=StubBinance(Decimal("95000"), volatility=3.0),
        polymarket_client=StubPolymarket(market, yes_price=Decimal("0.50"), no_price=Decimal("0.50")),
        charlie_predictor=StubCharlie(),
        config={"min_edge": 0.03, "max_edge": 0.99, "min_volatility_pct": 0.0},
    )

    opportunity = await engine.scan_opportunities()

    assert opportunity is not None
    assert opportunity["side"] == "YES"
    assert opportunity["edge"] > Decimal("0.03")


@pytest.mark.asyncio
async def test_latency_arbitrage_no_signal():
    market = {
        "id": "m2",
        "question": "Bitcoin Up or Down - 15 minute",
        "startingPrice": "97000",
        "tokens": [
            {"outcome": "YES", "token_id": "yes2", "price": 0.1},
            {"outcome": "NO", "token_id": "no2", "price": 0.2},
        ],
        "end_date": (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat(),
    }

    engine = LatencyArbitrageEngine(
        binance_ws=StubBinance(Decimal("96000"), volatility=3.0),
        polymarket_client=StubPolymarket(market, yes_price=Decimal("0.10"), no_price=Decimal("0.20")),
        charlie_predictor=StubCharlie(),
        config={"min_edge": 0.03, "max_edge": 0.99, "min_volatility_pct": 0.0},
    )

    opportunity = await engine.scan_opportunities()

    assert opportunity is not None
    assert opportunity["side"] == "NO"
    assert opportunity["edge"] > Decimal("0.03")
