"""
Test token direction logic.
Verifies strategy NEVER attempts to sell tokens it doesn't own.
"""
import pytest
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


def test_bullish_signal_buys_yes_token():
    """Test bullish scenario: Price above strike → BUY YES."""
    strategy = _make_strategy()

    btc_price = Decimal("96500.00")
    strike_price = Decimal("96000.00")
    yes_odds = Decimal("0.52")
    no_odds = Decimal("0.48")

    signal = strategy.determine_trade_direction(
        btc_price=btc_price,
        strike_price=strike_price,
        yes_odds=yes_odds,
        no_odds=no_odds,
    )

    assert signal is not None, "Should detect bullish opportunity"
    assert signal["outcome"] == "YES", "Bullish signal should target YES token"
    assert signal["side"] == "BUY", "Should BUY YES token (never SELL)"
    assert signal["direction"] == "BULLISH", "Should be labeled BULLISH"


def test_bearish_signal_buys_no_token():
    """Test bearish scenario: Price below strike → BUY NO (NOT sell YES)."""
    strategy = _make_strategy()

    btc_price = Decimal("95500.00")
    strike_price = Decimal("96000.00")
    yes_odds = Decimal("0.70")
    no_odds = Decimal("0.25")

    signal = strategy.determine_trade_direction(
        btc_price=btc_price,
        strike_price=strike_price,
        yes_odds=yes_odds,
        no_odds=no_odds,
    )

    assert signal is not None, "Should detect bearish opportunity"
    assert signal["outcome"] == "NO", "Bearish signal MUST target NO token"
    assert signal["side"] == "BUY", "MUST BUY NO token (NEVER sell YES)"
    assert signal["direction"] == "BEARISH", "Should be labeled BEARISH"


def test_never_generates_sell_orders():
    """Comprehensive test: Verify NO sell orders are ever generated."""
    strategy = _make_strategy()

    test_cases = [
        (Decimal("97000"), Decimal("96000"), Decimal("0.50"), Decimal("0.50"), "YES"),
        (Decimal("96100"), Decimal("96000"), Decimal("0.60"), Decimal("0.40"), "YES"),
        (Decimal("95900"), Decimal("96000"), Decimal("0.60"), Decimal("0.30"), "NO"),
        (Decimal("95000"), Decimal("96000"), Decimal("0.70"), Decimal("0.20"), "NO"),
    ]

    for btc_price, strike, yes_odds, no_odds, expected_outcome in test_cases:
        signal = strategy.determine_trade_direction(
            btc_price=btc_price,
            strike_price=strike,
            yes_odds=yes_odds,
            no_odds=no_odds,
        )

        if signal:
            assert signal["side"] == "BUY", (
                f"CRITICAL BUG: Found SELL order for {signal['outcome']} token!"
            )
            assert signal["outcome"] == expected_outcome, (
                f"Wrong token selected: expected {expected_outcome}, got {signal['outcome']}"
            )


def test_no_signal_when_no_edge():
    """Test that strategy doesn't trade when there's no edge."""
    strategy = _make_strategy()

    btc_price = Decimal("96500.00")
    strike_price = Decimal("96000.00")
    yes_odds = Decimal("0.90")
    no_odds = Decimal("0.10")

    signal = strategy.determine_trade_direction(
        btc_price=btc_price,
        strike_price=strike_price,
        yes_odds=yes_odds,
        no_odds=no_odds,
    )

    assert signal is None, "Should NOT trade when token is correctly priced"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
