"""
Tests for Issue #121: no float arithmetic in trade_executor active path.

Every money-sensitive value (bet_size, entry_price, shares, edge, confidence)
must be a str-encoded Decimal in the persisted trade_record, not a float.
"""
from decimal import Decimal, ROUND_DOWN
from unittest.mock import AsyncMock, MagicMock
import pytest

from execution.trade_executor import TradeExecutor, MIN_BET_SIZE


def _make_executor(placed_records, balance="100", kelly_raw=None):
    """Build a TradeExecutor with mocks that capture the trade_record."""
    mock_polymarket = AsyncMock()
    mock_polymarket.place_order = AsyncMock(
        return_value={"success": True, "order_id": "ord_test"}
    )

    mock_bankroll = MagicMock()
    mock_bankroll.current_balance = Decimal(balance)

    mock_db = MagicMock()
    mock_db.log_trade.side_effect = lambda rec: (placed_records.append(rec), 1)[1]

    mock_breaker = MagicMock()
    mock_breaker.is_trading_allowed.return_value = True

    mock_kelly = None
    if kelly_raw is not None:
        mock_kelly = MagicMock()
        mock_kelly.calculate_bet_size.return_value = kelly_raw

    return TradeExecutor(
        polymarket_client=mock_polymarket,
        bankroll_tracker=mock_bankroll,
        kelly_sizer=mock_kelly,
        db=mock_db,
        circuit_breaker=mock_breaker,
    )


@pytest.mark.asyncio
async def test_execute_trade_no_float_in_trade_record():
    """
    bet_size, entry_price, shares, edge, confidence in trade_record must all
    be str, not float.  float values cause silent precision loss in JSON
    serialisation and downstream PnL arithmetic.
    """
    records = []
    executor = _make_executor(records)

    result = await executor.execute_trade({
        "market_id": "mkt_1",
        "side": "YES",
        "confidence": "0.75",
        "edge": "0.10",
        "market_price": "0.65",
        "kelly_size": "2.50",
        "token_id": "tok_1",
        "question": "Test market",
    })

    assert result is True
    assert len(records) == 1
    rec = records[0]

    for field in ("bet_size", "entry_price", "shares", "edge", "confidence"):
        assert isinstance(rec[field], str), (
            f"trade_record['{field}'] must be str(Decimal), "
            f"got {type(rec[field])}: {rec[field]!r}"
        )
        # Must round-trip through Decimal without error
        Decimal(rec[field])


@pytest.mark.asyncio
async def test_shares_calculation_decimal_precision():
    """
    shares = bet_size / market_price must use ROUND_DOWN (shares must never
    be overstated) and be stored as str(Decimal) to 8 decimal places.

    Concrete case: Decimal('2.50') / Decimal('0.65') = 3.846153846153...
    With ROUND_DOWN to 8dp -> 3.84615384  (truncate, never round up)
    With ROUND_HALF_EVEN  -> 3.84615385  (rounds up the last digit)
    The executor must use ROUND_DOWN.
    """
    records = []
    executor = _make_executor(records)

    await executor.execute_trade({
        "market_id": "mkt_shares",
        "side": "YES",
        "confidence": "0.70",
        "edge": "0.08",
        "market_price": "0.65",
        "kelly_size": "2.50",
        "token_id": "tok_shares",
    })

    rec = records[0]
    shares = Decimal(rec["shares"])

    # Expected: ROUND_DOWN (truncate) — never overstate shares
    expected = (Decimal("2.50") / Decimal("0.65")).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    assert shares == expected, f"Shares mismatch: {shares} != {expected}"
    assert isinstance(rec["shares"], str)


@pytest.mark.asyncio
async def test_payout_odds_decimal_no_float():
    """
    When kelly_size is absent, executor computes payout_odds = 1/market_price.
    That division must be Decimal/Decimal, not float/float.
    """
    records = []
    captured_args = []

    class CapturingKelly:
        def calculate_bet_size(self, confidence, payout_odds, edge, strategy=None):
            captured_args.append((confidence, payout_odds, edge))
            return Decimal("1.50")

    mock_polymarket = AsyncMock()
    mock_polymarket.place_order = AsyncMock(
        return_value={"success": True, "order_id": "ord_k"}
    )
    mock_bankroll = MagicMock()
    mock_bankroll.current_balance = Decimal("100")
    mock_db = MagicMock()
    mock_db.log_trade.side_effect = lambda rec: (records.append(rec), 1)[1]
    mock_breaker = MagicMock()
    mock_breaker.is_trading_allowed.return_value = True

    executor = TradeExecutor(
        polymarket_client=mock_polymarket,
        bankroll_tracker=mock_bankroll,
        kelly_sizer=CapturingKelly(),
        db=mock_db,
        circuit_breaker=mock_breaker,
    )

    await executor.execute_trade({
        "market_id": "mkt_kelly",
        "side": "YES",
        "confidence": "0.70",
        "edge": "0.10",
        "market_price": "0.50",
        "token_id": "tok_k",
    })

    assert len(captured_args) == 1
    confidence, payout_odds, edge = captured_args[0]

    assert isinstance(confidence, Decimal), f"confidence must be Decimal, got {type(confidence)}"
    assert isinstance(payout_odds, Decimal), f"payout_odds must be Decimal, got {type(payout_odds)}"
    assert isinstance(edge, Decimal), f"edge must be Decimal, got {type(edge)}"
    assert payout_odds == Decimal("2"), f"payout_odds wrong: {payout_odds}"


@pytest.mark.asyncio
async def test_market_price_zero_guard():
    """market_price=0 must not raise ZeroDivisionError; shares must be '0'."""
    records = []
    executor = _make_executor(records)

    result = await executor.execute_trade({
        "market_id": "mkt_zero",
        "side": "YES",
        "confidence": "0.70",
        "edge": "0.10",
        "market_price": "0",
        "kelly_size": "1.00",
        "token_id": "tok_zero",
    })

    assert result is True
    assert records[0]["shares"] == "0"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
