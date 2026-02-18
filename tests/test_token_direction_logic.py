import random
from decimal import Decimal
from unittest.mock import MagicMock

from strategies.latency_arbitrage_btc import LatencyArbitrageEngine


def _make_strategy() -> LatencyArbitrageEngine:
    return LatencyArbitrageEngine(
        binance_ws=MagicMock(),
        polymarket_client=MagicMock(),
        charlie_predictor=MagicMock(),
        redis_subscriber=None,
    )


def test_bullish_signal_buys_yes():
    strategy = _make_strategy()

    signal = strategy.determine_trade_direction(
        market_id="123",
        start_price=Decimal("69000"),
        current_price=Decimal("70000"),
        yes_price=Decimal("0.40"),
        no_price=Decimal("0.60"),
        min_edge=Decimal("0.03"),
    )

    assert signal is not None
    assert signal["outcome"] == "YES"
    assert signal["side"] == "BUY"
    assert signal["expected_outcome"] == "UP"


def test_bearish_signal_buys_no():
    strategy = _make_strategy()

    signal = strategy.determine_trade_direction(
        market_id="123",
        start_price=Decimal("69000"),
        current_price=Decimal("68000"),
        yes_price=Decimal("0.60"),
        no_price=Decimal("0.40"),
        min_edge=Decimal("0.03"),
    )

    assert signal is not None
    assert signal["outcome"] == "NO"
    assert signal["side"] == "BUY"
    assert signal["expected_outcome"] == "DOWN"


def test_never_sells_tokens():
    strategy = _make_strategy()

    for _ in range(100):
        start = Decimal(str(random.randint(60000, 80000)))
        current = Decimal(str(random.randint(60000, 80000)))
        yes_price = Decimal(str(random.uniform(0.1, 0.9))).quantize(Decimal("0.0001"))
        no_price = (Decimal("1.0") - yes_price).quantize(Decimal("0.0001"))

        signal = strategy.determine_trade_direction(
            market_id="test",
            start_price=start,
            current_price=current,
            yes_price=yes_price,
            no_price=no_price,
            min_edge=Decimal("0.01"),
        )

        if signal is not None:
            assert signal["side"] == "BUY", f"CRITICAL: Found non-BUY signal: {signal}"


def test_no_signal_when_edge_too_small():
    strategy = _make_strategy()

    signal = strategy.determine_trade_direction(
        market_id="123",
        start_price=Decimal("69000"),
        current_price=Decimal("69000"),
        yes_price=Decimal("0.50"),
        no_price=Decimal("0.50"),
        min_edge=Decimal("0.03"),
    )

    assert signal is None
