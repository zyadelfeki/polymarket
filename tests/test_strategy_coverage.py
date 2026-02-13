"""
Additional tests to improve strategy coverage for latency_arbitrage_btc.
Focus on deterministic helper methods without external I/O.
"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


def _make_strategy() -> LatencyArbitrageEngine:
    return LatencyArbitrageEngine(
        binance_ws=MagicMock(),
        polymarket_client=MagicMock(),
        charlie_predictor=MagicMock(),
        redis_subscriber=None,
    )


def test_parse_start_price():
    strategy = _make_strategy()
    assert strategy._parse_start_price("Starting at $96,000") == Decimal("96000")
    assert strategy._parse_start_price("begins at 95000") == Decimal("95000")
    assert strategy._parse_start_price("") is None


def test_extract_threshold():
    strategy = _make_strategy()
    assert strategy._extract_threshold("Will BTC be above $96,000?") == Decimal("96000")
    assert strategy._extract_threshold("BTC above $96K") == Decimal("96000")


def test_extract_threshold_and_direction():
    strategy = _make_strategy()
    threshold, direction = strategy._extract_threshold_and_direction("Will BTC be above $96,000?")
    assert threshold == Decimal("96000")
    assert direction == "ABOVE"

    threshold, direction = strategy._extract_threshold_and_direction("Will BTC be below $95,000?")
    assert threshold == Decimal("95000")
    assert direction == "BELOW"


def test_extract_token_ids_and_prices():
    strategy = _make_strategy()
    market = {
        "tokens": [
            {"outcome": "YES", "token_id": "yes_token", "price": "0.55"},
            {"outcome": "NO", "token_id": "no_token", "price": "0.45"},
        ]
    }
    yes_id, no_id = strategy._extract_token_ids(market)
    assert yes_id == "yes_token"
    assert no_id == "no_token"

    assert strategy._extract_token_price(market, "YES") == Decimal("0.55")
    assert strategy._extract_token_price(market, "NO") == Decimal("0.45")

    prices = strategy._get_market_prices_from_tokens(market)
    assert prices is not None
    assert prices["yes"] == Decimal("0.55")
    assert prices["no"] == Decimal("0.45")


def test_extract_best_ask_and_mid_price():
    strategy = _make_strategy()
    orderbook = {
        "bids": [{"price": "0.48"}],
        "asks": [{"price": "0.52"}, {"price": "0.53"}],
    }
    assert strategy._extract_best_ask(orderbook) == Decimal("0.52")
    assert LatencyArbitrageEngine._extract_mid_price(orderbook) == Decimal("0.50")


def test_extract_time_left_seconds():
    strategy = _make_strategy()
    end_time = datetime.now(timezone.utc) + timedelta(seconds=60)
    market = {"endDate": end_time.isoformat().replace("+00:00", "Z")}
    seconds_left = strategy._extract_time_left_seconds(market)
    assert seconds_left is not None
    assert seconds_left > 0


def test_determine_trade_direction_no_edge():
    strategy = _make_strategy()
    signal = strategy.determine_trade_direction(
        btc_price=Decimal("96500"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.95"),
        no_odds=Decimal("0.05"),
    )
    assert signal is None


def test_determine_trade_direction_bull_bear():
    strategy = _make_strategy()
    bull = strategy.determine_trade_direction(
        btc_price=Decimal("97000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.50"),
        no_odds=Decimal("0.50"),
    )
    bear = strategy.determine_trade_direction(
        btc_price=Decimal("95000"),
        strike_price=Decimal("96000"),
        yes_odds=Decimal("0.70"),
        no_odds=Decimal("0.25"),
    )

    assert bull is not None and bull["direction"] == "BULLISH"
    assert bear is not None and bear["direction"] == "BEARISH"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
