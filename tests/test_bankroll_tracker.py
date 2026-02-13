from decimal import Decimal

from risk.bankroll_tracker import BankrollTracker


def test_initialization_and_empty_stats() -> None:
    tracker = BankrollTracker(initial_capital=Decimal("100"))
    stats = tracker.get_stats()
    assert stats["current_capital"] == Decimal("100")
    assert stats["peak_capital"] == Decimal("100")
    assert stats["total_trades"] == 0
    assert stats["win_rate_pct"] == Decimal("0")


def test_record_trade_profit_and_loss_updates() -> None:
    tracker = BankrollTracker(initial_capital=Decimal("100"))
    tracker.record_trade(Decimal("10"), {"id": "t1"})
    tracker.record_trade(Decimal("-5"), {"id": "t2"})

    assert tracker.current_capital == Decimal("105")
    assert tracker.peak_capital == Decimal("110")
    assert tracker.total_profit == Decimal("5")
    assert tracker.total_trades == 2
    assert tracker.winning_trades == 1
    assert tracker.losing_trades == 1


def test_available_capital_total_return_and_win_rate() -> None:
    tracker = BankrollTracker(initial_capital=Decimal("200"))
    tracker.record_trade(Decimal("20"), {})
    tracker.record_trade(Decimal("20"), {})
    tracker.record_trade(Decimal("-10"), {})

    assert tracker.get_available_capital() == Decimal("115")
    assert tracker.get_total_return() == Decimal("15")
    assert tracker.get_win_rate().quantize(Decimal("0.01")) == Decimal("66.67")


def test_sharpe_ratio_and_stats_bundle() -> None:
    tracker = BankrollTracker(initial_capital=Decimal("100"))
    tracker.record_trade(Decimal("5"), {})
    tracker.record_trade(Decimal("-3"), {})
    tracker.record_trade(Decimal("8"), {})

    sharpe = tracker.get_sharpe_ratio()
    assert isinstance(sharpe, Decimal)
    assert sharpe != Decimal("0")

    stats = tracker.get_stats()
    assert stats["sharpe_ratio"] == sharpe
    assert stats["available_capital"] == tracker.get_available_capital()


def test_print_summary_smoke() -> None:
    tracker = BankrollTracker(initial_capital=Decimal("50"))
    tracker.record_trade(Decimal("5"), {})
    tracker.print_summary()
