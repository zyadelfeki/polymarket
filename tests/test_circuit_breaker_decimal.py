"""
Tests for CircuitBreaker Decimal-precision correctness.

Every money-sensitive calculation in CircuitBreaker must use Decimal arithmetic.
float coercion on drawdown thresholds can cause the breaker to silently
never fire, leaving the bankroll unprotected.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from risk.circuit_breaker import CircuitBreaker


def _make_breaker(initial: str = "100") -> CircuitBreaker:
    """
    Build a CircuitBreaker with a properly-mocked AlertService.

    send_critical_alert is async, so it must be an AsyncMock.
    If it is a plain MagicMock, asyncio.run(mock()) raises:
      ValueError: a coroutine was expected, got <MagicMock ...>
    """
    mock_alert = MagicMock()
    mock_alert.send_critical_alert = AsyncMock(return_value=None)

    with patch("risk.circuit_breaker.AlertService", return_value=mock_alert):
        breaker = CircuitBreaker(
            initial_capital=Decimal(initial),
            alert_service=mock_alert,
        )
    return breaker


def test_get_current_drawdown_returns_decimal():
    """get_current_drawdown must return Decimal, never float."""
    breaker = _make_breaker("100")
    breaker.update_capital(Decimal("80"))  # 20% drawdown
    dd = breaker.get_current_drawdown()
    assert isinstance(dd, Decimal), (
        f"get_current_drawdown() must return Decimal, got {type(dd)}: {dd!r}"
    )
    assert dd == Decimal("20"), f"Expected 20% drawdown, got {dd}"


def test_drawdown_threshold_exact_decimal():
    """
    Circuit breaker must trip at EXACTLY the configured MAX_DRAWDOWN_PCT.

    The original bug: get_current_drawdown() returned float. If MAX_DRAWDOWN_PCT
    is Decimal, the comparison raises TypeError and the breaker never fires.
    If MAX_DRAWDOWN_PCT is int/float, floating-point epsilon can cause the
    threshold to be missed by tiny amounts.

    This test isolates the drawdown path by pinning daily_start_capital to
    the same value as current_capital, so the daily-loss check always reads 0%
    and cannot fire before the drawdown check is reached.
    """
    breaker = _make_breaker("100")

    mock_settings = MagicMock()
    mock_settings.CIRCUIT_BREAKER_ENABLED = True
    mock_settings.MAX_DRAWDOWN_PCT = 20
    mock_settings.DAILY_LOSS_LIMIT_PCT = 10
    mock_settings.MAX_CONSECUTIVE_LOSSES = 100
    mock_settings.MAX_DAILY_TRADES = 10000

    with patch("risk.circuit_breaker.settings", mock_settings):
        # Pin daily_start_capital to current_capital so daily-loss path
        # always reads 0% loss and cannot fire ahead of the drawdown check.
        breaker.peak_capital = Decimal("100")
        breaker.current_capital = Decimal("80.01")
        breaker.daily_start_capital = Decimal("80.01")  # isolate drawdown path

        # 19.99% drawdown — must NOT trip
        breaker._check_circuit_breaker()
        assert not breaker.breaker_triggered, "Breaker tripped too early at 19.99% drawdown"

        # Reset guard so _trigger_breaker's early-return doesn't skip the next call
        breaker.breaker_triggered = False
        breaker.breaker_reason = None

        # Exactly 20.00% drawdown — MUST trip
        breaker.current_capital = Decimal("80.00")
        breaker.daily_start_capital = Decimal("80.00")  # keep daily loss at 0%
        breaker._check_circuit_breaker()
        assert breaker.breaker_triggered, "Breaker did NOT trip at exactly 20% drawdown"
        assert "20" in breaker.breaker_reason


def test_daily_loss_limit_decimal():
    """Daily loss breaker must fire using Decimal division, not float."""
    breaker = _make_breaker("100")

    mock_settings = MagicMock()
    mock_settings.CIRCUIT_BREAKER_ENABLED = True
    mock_settings.MAX_DRAWDOWN_PCT = 50
    mock_settings.DAILY_LOSS_LIMIT_PCT = 10
    mock_settings.MAX_CONSECUTIVE_LOSSES = 100
    mock_settings.MAX_DAILY_TRADES = 10000

    with patch("risk.circuit_breaker.settings", mock_settings):
        breaker.daily_start_capital = Decimal("100")
        breaker.peak_capital = Decimal("100")

        # 9% loss — must NOT trip
        breaker.current_capital = Decimal("91")
        breaker._check_circuit_breaker()
        assert not breaker.breaker_triggered, "Daily breaker tripped too early at 9% loss"

        # Exactly 10% loss — MUST trip
        breaker.current_capital = Decimal("90")
        breaker._check_circuit_breaker()
        assert breaker.breaker_triggered, "Daily breaker did NOT trip at 10% loss"
        assert "10" in breaker.breaker_reason


def test_record_trade_capital_stays_decimal():
    """
    After record_trade(), current_capital must remain Decimal.
    A float profit arg must not silently upcast the whole capital to float.
    """
    breaker = _make_breaker("100")

    mock_settings = MagicMock()
    mock_settings.CIRCUIT_BREAKER_ENABLED = False

    with patch("risk.circuit_breaker.settings", mock_settings):
        breaker.record_trade(profit=Decimal("0"), win=True)
        assert isinstance(breaker.current_capital, Decimal)
        assert breaker.current_capital == Decimal("100")

        breaker.record_trade(profit=Decimal("5.50"), win=True)
        assert isinstance(breaker.current_capital, Decimal)
        assert breaker.current_capital == Decimal("105.50")

        breaker.record_trade(profit=Decimal("-3.25"), win=False)
        assert isinstance(breaker.current_capital, Decimal)
        assert breaker.current_capital == Decimal("102.25")


def test_get_status_no_float_values():
    """
    get_status() must not return float for current_capital, peak_capital,
    or current_drawdown. Consumers expect str(Decimal) for lossless handling.
    """
    breaker = _make_breaker("100")
    breaker.update_capital(Decimal("90"))  # 10% drawdown

    status = breaker.get_status()

    for field in ("current_capital", "peak_capital", "current_drawdown"):
        val = status[field]
        assert isinstance(val, str), (
            f"get_status()['{field}'] must be str, got {type(val)}: {val!r}"
        )
        Decimal(val)  # must parse without error

    assert Decimal(status["current_drawdown"]) == Decimal("10.0")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
