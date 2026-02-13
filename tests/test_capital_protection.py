from decimal import Decimal

import pytest

from risk.capital_protection import CircuitBreaker


def test_circuit_breaker_daily_loss_limit():
    breaker = CircuitBreaker(starting_capital=Decimal("100"))
    breaker.update_capital(Decimal("80"))

    with pytest.raises(Exception) as exc:
        breaker.check_before_trade(Decimal("5"), Decimal("80"))

    assert "daily loss" in str(exc.value).lower()


def test_circuit_breaker_consecutive_losses():
    breaker = CircuitBreaker(starting_capital=Decimal("100"))
    breaker.record_trade_result(Decimal("-3"))
    breaker.record_trade_result(Decimal("-3"))
    breaker.record_trade_result(Decimal("-3"))

    with pytest.raises(Exception) as exc:
        breaker.check_before_trade(Decimal("1"), Decimal("91"))

    assert "consecutive" in str(exc.value).lower()
