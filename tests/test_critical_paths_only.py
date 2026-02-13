"""Minimal smoke tests for critical safety paths.

These tests are intentionally narrow:
- Prevent float arithmetic / Decimal leakage in sizing.
- Ensure the strategy never generates SELL orders.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest


def test_decimal_precision_in_sizing_max_cap_respected() -> None:
    """Kelly sizing returns Decimal and never exceeds max_bet_pct cap."""
    from risk.kelly_sizer import AdaptiveKellySizer

    bankroll = Decimal("13.98")
    max_bet_pct = Decimal("5.0")

    sizer = AdaptiveKellySizer(
        {
            "kelly_fraction": "0.25",
            "max_kelly_fraction": "0.25",
            "max_bet_pct": str(max_bet_pct),
            "min_bet_size": "0.01",
            "min_edge": "0.02",
        }
    )

    result = sizer.calculate_bet_size(
        bankroll=bankroll,
        win_probability=Decimal("0.90"),
        payout_odds=Decimal("2.0"),
        edge=Decimal("0.05"),
        sample_size=50,
        current_aggregate_exposure=Decimal("0"),
    )

    assert isinstance(result.size, Decimal)

    max_allowed = bankroll * (max_bet_pct / Decimal("100"))
    assert result.size <= max_allowed

    with pytest.raises(TypeError):
        float(result)


def test_token_direction_never_sells_unowned() -> None:
    """Strategy signal generation must only emit BUY orders."""
    from strategies.latency_arbitrage_btc import LatencyArbitrageEngine

    strategy = LatencyArbitrageEngine(
        binance_ws=MagicMock(),
        polymarket_client=MagicMock(),
        charlie_predictor=MagicMock(),
        config={},
        execution_service=None,
        kelly_sizer=None,
        price_history=None,
        redis_subscriber=None,
    )

    test_cases = [
        # Bullish: price above strike, YES odds attractive.
        (Decimal("97000"), Decimal("96000"), Decimal("0.50"), Decimal("0.90")),
        # Bearish: price below strike, NO odds attractive.
        (Decimal("95000"), Decimal("96000"), Decimal("0.90"), Decimal("0.25")),
    ]

    for btc_price, strike, yes_odds, no_odds in test_cases:
        signal = strategy.determine_trade_direction(btc_price, strike, yes_odds, no_odds)
        assert signal is not None
        assert signal["side"] == "BUY"
