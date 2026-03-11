from decimal import Decimal

import pytest

from risk.kelly_sizer import AdaptiveKellySizer, KellySizer


def _make_sizer() -> AdaptiveKellySizer:
    return AdaptiveKellySizer(
        config={
            "kelly_fraction": "0.25",
            "max_kelly_fraction": "0.25",
            "max_bet_pct": "5.0",
            "min_bet_size": "0.01",
            "min_edge": "0.02",
            "micro_capital_threshold": "50.0",
            "min_sample_size": 20,
            "max_aggregate_exposure": "20.0",
        }
    )


def test_invalid_bankroll_and_probability_paths() -> None:
    sizer = _make_sizer()
    bad_bankroll = sizer.calculate_bet_size(bankroll=Decimal("0"), win_probability=Decimal("0.6"), payout_odds=Decimal("2"), edge=Decimal("0.1"))
    assert bad_bankroll.size == Decimal("0")
    assert bad_bankroll.capped_reason == "invalid_bankroll"

    bad_prob = sizer.calculate_bet_size(bankroll=Decimal("100"), win_probability=Decimal("1"), payout_odds=Decimal("2"), edge=Decimal("0.1"))
    assert bad_prob.size == Decimal("0")
    assert bad_prob.capped_reason == "invalid_probability"


def test_insufficient_edge_and_negative_kelly() -> None:
    sizer = _make_sizer()
    low_edge = sizer.calculate_bet_size(
        bankroll=Decimal("100"),
        win_probability=Decimal("0.55"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.01"),
    )
    assert low_edge.size == Decimal("0")
    assert low_edge.capped_reason == "insufficient_edge"

    neg = sizer.calculate_bet_size(
        bankroll=Decimal("100"),
        win_probability=Decimal("0.2"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.2"),
    )
    assert neg.size == Decimal("0")
    assert neg.capped_reason == "negative_kelly"


def test_low_sample_loss_streak_and_exposure_cap() -> None:
    sizer = _make_sizer()
    sizer.consecutive_losses = 4

    result = sizer.calculate_bet_size(
        bankroll=Decimal("100"),
        win_probability=Decimal("0.7"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.1"),
        sample_size=5,
        current_aggregate_exposure=Decimal("19.8"),
    )
    assert result.size <= Decimal("0.20")
    assert result.warnings is not None


def test_win_streak_bonus_and_rounding() -> None:
    sizer = _make_sizer()
    sizer.consecutive_wins = 6
    result = sizer.calculate_bet_size(
        bankroll=Decimal("123.45"),
        win_probability=Decimal("0.8"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.2"),
        sample_size=50,
    )
    assert result.size > Decimal("0")
    assert result.size.as_tuple().exponent <= -2


def test_calculate_real_edge_and_type_guards() -> None:
    sizer = _make_sizer()
    edge = sizer.calculate_real_edge(
        market_price=Decimal("0.5"),
        true_probability=Decimal("0.7"),
        orderbook_spread=Decimal("0.02"),
        latency_advantage_seconds=Decimal("1"),
    )
    assert edge >= Decimal("0")

    with pytest.raises(TypeError):
        sizer.calculate_real_edge(
            market_price=0.5,
            true_probability=Decimal("0.7"),
            orderbook_spread=Decimal("0.02"),
            latency_advantage_seconds=Decimal("1"),
        )


def test_record_trade_result_and_win_rate_stats() -> None:
    sizer = _make_sizer()
    for _ in range(8):
        sizer.record_trade_result(win=True, profit=Decimal("1"), bet_size=Decimal("2"), strategy="s1")
    for _ in range(4):
        sizer.record_trade_result(win=False, profit=Decimal("-1"), bet_size=Decimal("2"), strategy="s1")

    wr = sizer.get_win_rate(strategy="s1", min_samples=10)
    assert wr is not None
    assert wr == Decimal("8") / Decimal("12")

    sizer.reset_streak()
    stats = sizer.get_stats()
    assert stats["consecutive_wins"] == 0
    assert stats["consecutive_losses"] == 0


def test_compat_kelly_sizer_wrapper_paths() -> None:
    compat = KellySizer(
        config={
            "kelly_fraction": "0.25",
            "max_kelly_fraction": "0.25",
            "max_bet_pct": "5.0",
            "min_bet_size": "0.01",
            "min_edge": "0.02",
            "min_sample_size": 20,
            "max_aggregate_exposure": "20.0",
        }
    )

    assert compat.calculate_bet_size(
        bankroll=Decimal("100"),
        edge=Decimal("0.1"),
        market_price=Decimal("0.5"),
        sample_size=0,
    ) == Decimal("0")

    size = compat.calculate_bet_size(
        bankroll=Decimal("100"),
        edge=Decimal("0.35"),
        market_price=Decimal("0.5"),
        sample_size=30,
        current_exposure=Decimal("0"),
    )
    assert size > Decimal("0")
    assert size <= Decimal("5.00")


def test_micro_capital_mode_blocks_sub_dollar_expected_bets() -> None:
    sizer = _make_sizer()

    result = sizer.calculate_bet_size(
        bankroll=Decimal("25"),
        win_probability=Decimal("0.56"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.03"),
        sample_size=30,
    )

    assert result.size == Decimal("0")
    assert result.is_micro_capital_mode is True
    assert result.capped_reason == "below_minimum"
    assert result.risk_warning is not None


def test_micro_capital_mode_allows_large_enough_expected_bets() -> None:
    sizer = _make_sizer()

    result = sizer.calculate_bet_size(
        bankroll=Decimal("40"),
        win_probability=Decimal("0.80"),
        payout_odds=Decimal("2"),
        edge=Decimal("0.12"),
        sample_size=30,
    )

    assert result.is_micro_capital_mode is True
    assert result.size > Decimal("0")


def test_compat_wrapper_honors_micro_capital_gate() -> None:
    compat = KellySizer(
        config={
            "kelly_fraction": "0.25",
            "max_kelly_fraction": "0.25",
            "max_bet_pct": "5.0",
            "min_bet_size": "0.01",
            "min_edge": "0.02",
            "micro_capital_threshold": "50.0",
            "min_sample_size": 20,
            "max_aggregate_exposure": "20.0",
        }
    )

    blocked = compat.calculate_bet_size(
        bankroll=Decimal("20"),
        edge=Decimal("0.05"),
        market_price=Decimal("0.5"),
        sample_size=30,
    )
    assert blocked == Decimal("0")

    allowed = compat.calculate_bet_size(
        bankroll=Decimal("40"),
        edge=Decimal("0.15"),
        market_price=Decimal("0.5"),
        sample_size=30,
    )
    assert allowed > Decimal("0")
