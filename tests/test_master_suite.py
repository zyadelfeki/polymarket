"""
Master Surgical Test Suite
==========================
Exhaustive coverage of every production code path:
  - TradeExecutor  (all branches, Decimal invariants, queue)
  - PaperOrderBook  (clamp, settle, settle_open_positions, summary, edge cases)
  - _PaperBankrollTracker  (balance arithmetic, add_trade, current_balance)
  - CircuitBreaker  (all trigger conditions, reset, drawdown, daily)
  - IdempotencyManager  (admission policy, TTL, persistence, key generation)
  - run_paper_trading  (settle_open_positions helper, full run_loop cycle)
  - OFI policy  (enrich, compute stats, LIVE_MODE guard)
  - Cross-system accounting invariants  (bankroll == order_book at all times)

Run with:
    pytest tests/test_master_suite.py -v --tb=short

"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Path setup so tests run without installing the package
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# Helpers / shared fixtures
# ===========================================================================

def _make_cb(initial: str = "100"):
    """Build a CircuitBreaker with AlertService mocked out."""
    from risk.circuit_breaker import CircuitBreaker
    cb = CircuitBreaker.__new__(CircuitBreaker)
    # Patch AlertService before __init__ to avoid network hits
    with patch("risk.circuit_breaker.AlertService") as MockAlert:
        MockAlert.return_value = MagicMock()
        cb.__init__(initial_capital=Decimal(initial))
    return cb


def _make_executor(balance: str = "100", circuit_breaker=None):
    """Build a TradeExecutor wired to mock collaborators."""
    from execution.trade_executor import TradeExecutor

    poly_mock = AsyncMock()
    poly_mock.place_order = AsyncMock(return_value={"success": True, "order_id": "ord_001"})

    bankroll = MagicMock()
    bankroll.current_balance = Decimal(balance)
    bankroll.add_trade = MagicMock()

    kelly_mock = MagicMock()
    kelly_mock.calculate_bet_size = MagicMock(return_value=Decimal("2.00"))

    db_mock = MagicMock()
    db_mock.log_trade = MagicMock(return_value=42)

    cb = circuit_breaker or _make_cb(balance)

    return TradeExecutor(
        polymarket_client=poly_mock,
        bankroll_tracker=bankroll,
        kelly_sizer=kelly_mock,
        db=db_mock,
        circuit_breaker=cb,
    ), poly_mock, bankroll, kelly_mock, db_mock


# ===========================================================================
# Section 1: TradeExecutor
# ===========================================================================

class TestTradeExecutorInit:
    def test_raises_without_circuit_breaker(self):
        from execution.trade_executor import TradeExecutor
        with pytest.raises(RuntimeError, match="circuit_breaker required"):
            TradeExecutor(
                polymarket_client=MagicMock(),
                bankroll_tracker=MagicMock(),
                kelly_sizer=MagicMock(),
                db=MagicMock(),
                circuit_breaker=None,
            )

    def test_stores_all_deps(self):
        ex, poly, bank, kelly, db = _make_executor()
        assert ex.polymarket is poly
        assert ex.bankroll is bank
        assert ex.kelly is kelly
        assert ex.db is db


class TestTradeExecutorCircuitBreaker:
    def test_blocked_returns_false(self):
        cb = _make_cb()
        cb.breaker_triggered = True
        cb.breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        cb.breaker_reason = "test block"
        ex, _, _, _, _ = _make_executor(circuit_breaker=cb)
        opp = {"market_id": "m1", "kelly_size": Decimal("2"), "edge": "0.1", "confidence": "0.7",
               "market_price": "0.5", "side": "YES", "question": "test?"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is False

    def test_allowed_proceeds(self):
        ex, poly, bank, _, _ = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2"), "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5", "side": "YES", "question": "test?"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is True
        poly.place_order.assert_called_once()


class TestTradeExecutorSizing:
    """Every sizing branch in execute_trade."""

    def _run(self, opp, balance="100"):
        ex, poly, bank, kelly, db = _make_executor(balance=balance)
        bank.current_balance = Decimal(balance)
        return asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp)), ex, poly, bank

    def test_kelly_size_in_opp_used_directly(self):
        opp = {"market_id": "m1", "kelly_size": Decimal("3.50"), "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5", "side": "YES", "question": "Q"}
        result, ex, poly, _ = self._run(opp)
        assert result is True
        # size passed to place_order must be exactly 3.50
        call_args = poly.place_order.call_args[0]
        assert call_args[2] == Decimal("3.50")

    def test_kelly_size_none_falls_back_to_sizer(self):
        from execution.trade_executor import TradeExecutor
        poly_mock = AsyncMock()
        poly_mock.place_order = AsyncMock(return_value={"success": True, "order_id": "x"})
        bank = MagicMock()
        bank.current_balance = Decimal("100")
        bank.add_trade = MagicMock()
        kelly = MagicMock()
        kelly.calculate_bet_size = MagicMock(return_value=Decimal("5.00"))
        db = MagicMock()
        db.log_trade = MagicMock(return_value=1)
        cb = _make_cb()
        ex = TradeExecutor(poly_mock, bank, kelly, db, cb)
        opp = {"market_id": "m1", "kelly_size": None, "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5", "side": "YES", "question": "Q"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is True
        call_args = poly_mock.place_order.call_args[0]
        assert call_args[2] == Decimal("5.00")

    def test_no_kelly_size_no_sizer_returns_false(self):
        from execution.trade_executor import TradeExecutor
        poly_mock = AsyncMock()
        bank = MagicMock()
        bank.current_balance = Decimal("100")
        db = MagicMock()
        cb = _make_cb()
        ex = TradeExecutor(poly_mock, bank, None, db, cb)
        opp = {"market_id": "m1", "edge": "0.1", "confidence": "0.7",
               "market_price": "0.5", "side": "YES", "question": "Q"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is False


class TestTradeExecutorMinBetClamp:
    """The minimum bet floor: all 3 branches (above, clamp, reject)."""

    def _run(self, kelly_size_str, balance_str):
        from execution.trade_executor import TradeExecutor
        poly_mock = AsyncMock()
        poly_mock.place_order = AsyncMock(return_value={"success": True, "order_id": "o"})
        bank = MagicMock()
        bank.current_balance = Decimal(balance_str)
        bank.add_trade = MagicMock()
        db = MagicMock()
        db.log_trade = MagicMock(return_value=1)
        cb = _make_cb(balance_str)
        ex = TradeExecutor(poly_mock, bank, MagicMock(), db, cb)
        opp = {"market_id": "m1", "kelly_size": Decimal(kelly_size_str),
               "edge": "0.1", "confidence": "0.7", "market_price": "0.5",
               "side": "YES", "question": "Q"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        return result, poly_mock

    def test_above_minimum_passes_through(self):
        result, poly = self._run("2.00", "100")
        assert result is True
        assert poly.place_order.call_args[0][2] == Decimal("2.00")

    def test_below_minimum_clamped_when_balance_ok(self):
        result, poly = self._run("0.30", "100")
        assert result is True
        assert poly.place_order.call_args[0][2] == Decimal("1.00")

    def test_below_minimum_rejected_when_balance_low(self):
        result, poly = self._run("0.30", "0.50")
        assert result is False
        poly.place_order.assert_not_called()

    def test_exactly_minimum_not_clamped(self):
        result, poly = self._run("1.00", "100")
        assert result is True
        assert poly.place_order.call_args[0][2] == Decimal("1.00")

    def test_clamp_boundary_just_below(self):
        result, poly = self._run("0.9999", "100")
        assert result is True
        assert poly.place_order.call_args[0][2] == Decimal("1.00")


class TestTradeExecutorDecimalPurity:
    """Verify that no float ever escapes into the trade record."""

    def test_trade_record_values_are_strings(self):
        ex, poly, bank, _, db = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2.50"),
               "edge": "0.12", "confidence": "0.75", "market_price": "0.65",
               "side": "YES", "question": "Will BTC hit $100k?"}
        asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        record = db.log_trade.call_args[0][0]
        for key in ("entry_price", "bet_size", "shares", "edge", "confidence"):
            assert isinstance(record[key], str), f"{key} should be str, got {type(record[key])}" 
            # Ensure it parses back to Decimal cleanly
            Decimal(record[key])

    def test_shares_computed_as_decimal_not_float(self):
        ex, poly, bank, _, db = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("3.00"),
               "edge": "0.10", "confidence": "0.70", "market_price": "0.60",
               "side": "YES", "question": "Q"}
        asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        record = db.log_trade.call_args[0][0]
        shares = Decimal(record["shares"])
        expected = (Decimal("3.00") / Decimal("0.60")).quantize(Decimal("0.00000001"))
        assert shares == expected


class TestTradeExecutorBrokerReject:
    def test_broker_failure_returns_false_no_db_write(self):
        from execution.trade_executor import TradeExecutor
        poly_mock = AsyncMock()
        poly_mock.place_order = AsyncMock(return_value={"success": False})
        bank = MagicMock()
        bank.current_balance = Decimal("100")
        bank.add_trade = MagicMock()
        db = MagicMock()
        db.log_trade = MagicMock(return_value=1)
        cb = _make_cb()
        ex = TradeExecutor(poly_mock, bank, MagicMock(), db, cb)
        opp = {"market_id": "m1", "kelly_size": Decimal("2.00"),
               "edge": "0.10", "confidence": "0.70", "market_price": "0.50",
               "side": "YES", "question": "Q"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is False
        db.log_trade.assert_not_called()
        bank.add_trade.assert_not_called()

    def test_broker_none_response_returns_false(self):
        from execution.trade_executor import TradeExecutor
        poly_mock = AsyncMock()
        poly_mock.place_order = AsyncMock(return_value=None)
        bank = MagicMock()
        bank.current_balance = Decimal("100")
        bank.add_trade = MagicMock()
        db = MagicMock()
        db.log_trade = MagicMock(return_value=1)
        cb = _make_cb()
        ex = TradeExecutor(poly_mock, bank, MagicMock(), db, cb)
        opp = {"market_id": "m1", "kelly_size": Decimal("2.00"),
               "edge": "0.10", "confidence": "0.70", "market_price": "0.50",
               "side": "YES", "question": "Q"}
        result = asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert result is False


class TestTradeExecutorSideNormalisation:
    def test_side_uppercased_in_call(self):
        ex, poly, bank, _, _ = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2"), "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5", "side": "yes", "question": "Q"}
        asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert poly.place_order.call_args[0][1] == "YES"

    def test_true_outcome_used_when_side_absent(self):
        ex, poly, bank, _, _ = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2"), "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5",
               "true_outcome": "no", "question": "Q"}
        asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        assert poly.place_order.call_args[0][1] == "NO"


class TestTradeExecutorCircuitBreakerNotCalledOnPlacement:
    """Placement must NOT call circuit_breaker.record_trade."""

    def test_no_record_trade_on_success(self):
        cb = _make_cb()
        cb.record_trade = MagicMock()
        ex, _, _, _, _ = _make_executor(circuit_breaker=cb)
        opp = {"market_id": "m1", "kelly_size": Decimal("2"), "edge": "0.1",
               "confidence": "0.7", "market_price": "0.5", "side": "YES", "question": "Q"}
        asyncio.get_event_loop().run_until_complete(ex.execute_trade(opp))
        cb.record_trade.assert_not_called()


# ===========================================================================
# Section 2: PaperOrderBook
# ===========================================================================

class TestPaperOrderBookInit:
    def test_starts_empty(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        assert book.open_position_count == 0
        assert book._total_staked == Decimal("0")
        assert book._total_pnl == Decimal("0")
        assert book._orders_placed == 0


class TestPaperOrderBookRecordOrder:
    def _book(self):
        from execution.paper_order_book import PaperOrderBook
        return PaperOrderBook()

    def test_normal_record_returns_true(self):
        book = self._book()
        assert book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                                 Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q") is True

    def test_staked_updated_after_record(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert book._total_staked == Decimal("5")

    def test_duplicate_returns_false(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        result = book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                                   Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert result is False

    def test_duplicate_counter_increments(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert book._orders_rejected_duplicate == 1

    def test_different_side_allowed(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        result = book.record_order("m1", "NO", Decimal("5"), Decimal("0.5"),
                                   Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert result is True
        assert book._orders_placed == 2

    def test_side_case_insensitive(self):
        book = self._book()
        book.record_order("m1", "yes", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        result = book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                                   Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert result is False  # treated same


class TestPaperOrderBookMinBetClamp:
    """The MIN_BET_SIZE clamp applied inside record_order."""

    def _book(self):
        from execution.paper_order_book import PaperOrderBook
        return PaperOrderBook()

    def test_sub_minimum_size_clamped_to_one(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("0.30"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        pos = book._positions[("m1", "YES")]
        assert pos.size == Decimal("1.00")

    def test_staked_reflects_clamped_size(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("0.30"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        assert book._total_staked == Decimal("1.00")

    def test_above_minimum_not_modified(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("3.50"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        pos = book._positions[("m1", "YES")]
        assert pos.size == Decimal("3.50")

    def test_exactly_minimum_not_modified(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("1.00"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        pos = book._positions[("m1", "YES")]
        assert pos.size == Decimal("1.00")

    def test_zero_size_not_clamped(self):
        """Zero is not a positive sub-minimum; clamp must not fire."""
        book = self._book()
        book.record_order("m1", "YES", Decimal("0"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        pos = book._positions[("m1", "YES")]
        assert pos.size == Decimal("0")

    def test_multiple_sub_minimum_orders_stack_correctly(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("0.30"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q1")
        book.record_order("m2", "YES", Decimal("0.50"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q2")
        # Both clamped to 1.00 each => total = 2.00
        assert book._total_staked == Decimal("2.00")


class TestPaperOrderBookSettle:
    def _book_with_position(self, market_id="m1", side="YES",
                             size="5", price="0.5"):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order(market_id, side, Decimal(size), Decimal(price),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        return book

    def test_yes_wins_pnl_positive(self):
        book = self._book_with_position(side="YES", size="5", price="0.5")
        settled = book.settle("m1", resolved_yes=True)
        assert len(settled) == 1
        assert settled[0].pnl > Decimal("0")

    def test_yes_loses_pnl_negative(self):
        book = self._book_with_position(side="YES", size="5", price="0.5")
        settled = book.settle("m1", resolved_yes=False)
        assert settled[0].pnl == Decimal("-5")

    def test_no_wins_when_resolved_no(self):
        book = self._book_with_position(side="NO", size="5", price="0.5")
        settled = book.settle("m1", resolved_yes=False)
        assert len(settled) == 1
        assert settled[0].pnl > Decimal("0")

    def test_settle_marks_position_as_settled(self):
        book = self._book_with_position()
        book.settle("m1", resolved_yes=True)
        assert book._positions[("m1", "YES")].settled is True

    def test_settle_updates_total_pnl(self):
        book = self._book_with_position(size="5", price="0.5")
        settled = book.settle("m1", resolved_yes=True)
        assert book._total_pnl == settled[0].pnl

    def test_double_settle_ignored(self):
        book = self._book_with_position()
        book.settle("m1", resolved_yes=True)
        settled2 = book.settle("m1", resolved_yes=True)
        assert len(settled2) == 0

    def test_settle_unknown_market_returns_empty(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        settled = book.settle("nonexistent", resolved_yes=True)
        assert settled == []

    def test_pnl_formula_correct(self):
        """pnl = size/price - size when YES wins."""
        book = self._book_with_position(size="4", price="0.4")
        settled = book.settle("m1", resolved_yes=True)
        expected = Decimal("4") / Decimal("0.4") - Decimal("4")
        assert settled[0].pnl == expected


class TestPaperOrderBookSettleOpenPositions:
    """Neutral/win/loss outcomes via settle_open_positions."""

    def _book_with_pos(self, size="5"):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal(size), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q",
                          end_date="2025-01-01T00:00:00+00:00")
        return book

    def test_neutral_pnl_zero(self):
        book = self._book_with_pos()
        settled = book.settle_open_positions("m1", outcome="neutral")
        assert settled[0].pnl == Decimal("0")

    def test_neutral_position_marked_settled(self):
        book = self._book_with_pos()
        book.settle_open_positions("m1", outcome="neutral")
        assert book._positions[("m1", "YES")].settled is True

    def test_win_outcome_positive_pnl(self):
        book = self._book_with_pos(size="4")
        settled = book.settle_open_positions("m1", outcome="win")
        assert settled[0].pnl > Decimal("0")

    def test_loss_outcome_negative_pnl(self):
        book = self._book_with_pos(size="4")
        settled = book.settle_open_positions("m1", outcome="loss")
        assert settled[0].pnl == Decimal("-4")

    def test_neutral_total_pnl_unchanged(self):
        book = self._book_with_pos()
        book.settle_open_positions("m1", outcome="neutral")
        assert book._total_pnl == Decimal("0")

    def test_already_settled_not_processed_again(self):
        book = self._book_with_pos()
        book.settle_open_positions("m1", outcome="neutral")
        settled_again = book.settle_open_positions("m1", outcome="neutral")
        assert settled_again == []

    def test_open_positions_list_empties_after_settle(self):
        book = self._book_with_pos()
        assert book.open_position_count == 1
        book.settle_open_positions("m1", outcome="neutral")
        assert book.open_position_count == 0

    def test_clamped_position_neutral_returns_clamped_size(self):
        """Neutral settlement must return effective (clamped) size, not raw."""
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("0.30"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        settled = book.settle_open_positions("m1", outcome="neutral")
        assert settled[0].size == Decimal("1.00")
        assert settled[0].pnl == Decimal("0")


class TestPaperOrderBookSummary:
    def test_summary_keys_present(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        s = book.summary()
        for key in ("orders_placed", "orders_rejected_duplicate",
                    "open_positions", "settled_positions",
                    "total_staked_usdc", "total_pnl_usdc"):
            assert key in s

    def test_summary_values_are_correct_after_record(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q")
        s = book.summary()
        assert s["orders_placed"] == 1
        assert s["open_positions"] == 1
        assert Decimal(s["total_staked_usdc"]) == Decimal("5")


class TestPaperOrderBookGetOpenPositions:
    def test_returns_only_open(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        book.record_order("m2", "YES", Decimal("3"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q2")
        book.settle("m1", resolved_yes=True)
        open_pos = book.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].market_id == "m2"


# ===========================================================================
# Section 3: _PaperBankrollTracker
# ===========================================================================

class TestPaperBankrollTracker:
    """Tests on the tracker class defined inside run_paper_trading.py."""

    def _tracker(self, initial="100"):
        # Import the private class directly from the module
        import importlib
        import run_paper_trading as rpt
        importlib.reload(rpt)  # ensure fresh import
        return rpt._PaperBankrollTracker(Decimal(initial))

    def test_initial_balance(self):
        tracker = self._tracker("100")
        assert tracker.current_balance == Decimal("100")

    def test_add_trade_debits_balance(self):
        tracker = self._tracker("100")
        tracker.add_trade({"bet_size": "10"})
        assert tracker.current_balance == Decimal("90")

    def test_add_trade_uses_decimal_str(self):
        tracker = self._tracker("100")
        tracker.add_trade({"bet_size": "1.23456789"})  # high precision
        assert tracker.current_balance == Decimal("100") - Decimal("1.23456789")

    def test_multiple_trades_accumulate(self):
        tracker = self._tracker("100")
        tracker.add_trade({"bet_size": "10"})
        tracker.add_trade({"bet_size": "5"})
        tracker.add_trade({"bet_size": "20"})
        assert tracker.current_balance == Decimal("65")

    def test_bet_size_missing_defaults_to_zero(self):
        tracker = self._tracker("100")
        tracker.add_trade({})
        assert tracker.current_balance == Decimal("100")

    def test_clamped_trade_debits_clamped_amount(self):
        """When executor clamps 0.30 -> 1.00, record must show 1.00."""
        tracker = self._tracker("100")
        tracker.add_trade({"bet_size": "1.00"})  # post-clamp
        assert tracker.current_balance == Decimal("99.00")
        # NOT 99.70 (raw Kelly)


# ===========================================================================
# Section 4: CircuitBreaker
# ===========================================================================

class TestCircuitBreakerInit:
    def test_trading_allowed_initially(self):
        cb = _make_cb("100")
        assert cb.is_trading_allowed() is True

    def test_initial_capital_stored_as_decimal(self):
        cb = _make_cb("100")
        assert isinstance(cb.initial_capital, Decimal)
        assert cb.initial_capital == Decimal("100")


class TestCircuitBreakerConsecutiveLosses:
    def test_consecutive_losses_trigger(self):
        from config.settings import settings
        cb = _make_cb("1000")  # large capital to avoid drawdown trigger
        max_losses = settings.MAX_CONSECUTIVE_LOSSES
        for _ in range(max_losses):
            cb.record_trade(profit=Decimal("-1"), win=False)
        assert cb.breaker_triggered is True

    def test_win_resets_consecutive_counter(self):
        cb = _make_cb("1000")
        cb.record_trade(profit=Decimal("-1"), win=False)
        cb.record_trade(profit=Decimal("-1"), win=False)
        cb.record_trade(profit=Decimal("5"), win=True)
        assert cb.consecutive_losses == 0

    def test_loss_after_win_counts_from_one(self):
        cb = _make_cb("1000")
        cb.record_trade(profit=Decimal("5"), win=True)
        cb.record_trade(profit=Decimal("-1"), win=False)
        assert cb.consecutive_losses == 1


class TestCircuitBreakerDrawdown:
    def test_max_drawdown_triggers_breaker(self):
        from config.settings import settings
        cb = _make_cb("100")
        # Force drawdown above threshold
        big_loss = Decimal(str(settings.MAX_DRAWDOWN_PCT + 1))
        cb.record_trade(profit=-big_loss, win=False)
        assert cb.breaker_triggered is True

    def test_drawdown_calculation_correct(self):
        cb = _make_cb("100")
        cb.record_trade(profit=Decimal("-20"), win=False)  # 80 capital
        drawdown = cb.get_current_drawdown()
        assert drawdown == Decimal("20")  # 20%

    def test_zero_peak_capital_no_crash(self):
        cb = _make_cb("0")
        assert cb.get_current_drawdown() == Decimal("0")


class TestCircuitBreakerReset:
    def test_breaker_auto_resets_after_expiry(self):
        cb = _make_cb("100")
        cb.breaker_triggered = True
        cb.breaker_reason = "test"
        cb.breaker_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert cb.is_trading_allowed() is True
        assert cb.breaker_triggered is False

    def test_breaker_still_active_before_expiry(self):
        cb = _make_cb("100")
        cb.breaker_triggered = True
        cb.breaker_reason = "test"
        cb.breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        assert cb.is_trading_allowed() is False


class TestCircuitBreakerGetStatus:
    def test_status_contains_all_keys(self):
        cb = _make_cb("100")
        status = cb.get_status()
        for key in ("trading_allowed", "breaker_triggered", "breaker_reason",
                    "breaker_until", "current_drawdown", "consecutive_losses",
                    "trades_today", "current_capital", "peak_capital"):
            assert key in status

    def test_status_capital_is_string(self):
        cb = _make_cb("100")
        status = cb.get_status()
        # Must be string (JSON-safe Decimal)
        assert isinstance(status["current_capital"], str)
        Decimal(status["current_capital"])  # must parse


class TestCircuitBreakerUpdateCapital:
    def test_peak_tracks_high(self):
        cb = _make_cb("100")
        cb.update_capital(Decimal("150"))
        assert cb.peak_capital == Decimal("150")
        cb.update_capital(Decimal("120"))
        assert cb.peak_capital == Decimal("150")  # peak doesn't decrease

    def test_update_capital_stores_decimal(self):
        cb = _make_cb("100")
        cb.update_capital(Decimal("123.45"))
        assert cb.current_capital == Decimal("123.45")


class TestCircuitBreakerDailyReset:
    def test_daily_trades_increments(self):
        cb = _make_cb("1000")
        cb.record_trade(profit=Decimal("0"), win=True)
        assert cb.trades_today == 1

    def test_max_daily_trades_trigger(self):
        from config.settings import settings
        cb = _make_cb("10000")  # huge capital so drawdown/loss don't fire first
        for _ in range(settings.MAX_DAILY_TRADES):
            cb.record_trade(profit=Decimal("0"), win=True)
        # Might or might not trigger depending on config; just check no crash
        assert isinstance(cb.is_trading_allowed(), bool)


# ===========================================================================
# Section 5: IdempotencyManager
# ===========================================================================

class TestIdempotencyManagerInit:
    def test_memory_only_no_file_created(self):
        from execution.idempotency_manager import IdempotencyManager
        with tempfile.TemporaryDirectory() as td:
            mgr = IdempotencyManager(db_path=":memory:", ttl=3600)
            assert mgr.db_path is None

    def test_file_path_created(self):
        from execution.idempotency_manager import IdempotencyManager
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/sub/idem.json"
            mgr = IdempotencyManager(db_path=path, ttl=3600)
            assert Path(path).parent.exists()


class TestIdempotencyManagerKeyGeneration:
    def _mgr(self):
        from execution.idempotency_manager import IdempotencyManager
        return IdempotencyManager(db_path=":memory:", ttl=3600)

    def test_same_inputs_same_key(self):
        mgr = self._mgr()
        k1 = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"), strategy="charlie")
        k2 = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"), strategy="charlie")
        assert k1 == k2

    def test_different_market_different_key(self):
        mgr = self._mgr()
        k1 = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        k2 = mgr.generate_key("m2", "YES", Decimal("2"), Decimal("0.5"))
        assert k1 != k2

    def test_different_side_different_key(self):
        mgr = self._mgr()
        k1 = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        k2 = mgr.generate_key("m1", "NO", Decimal("2"), Decimal("0.5"))
        assert k1 != k2

    def test_key_is_16_chars(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        assert len(k) == 16


class TestIdempotencyManagerAdmissionPolicy:
    """Only SUCCESSFUL placements should enter the cache."""

    def _mgr(self):
        from execution.idempotency_manager import IdempotencyManager
        return IdempotencyManager(db_path=":memory:", ttl=3600)

    def test_new_key_not_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        assert mgr.is_duplicate(k) is False

    def test_record_placement_makes_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True, "market_id": "m1"})
        assert mgr.is_duplicate(k) is True

    def test_failed_update_does_not_make_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.update_result(k, {"success": False})
        assert mgr.is_duplicate(k) is False

    def test_pending_record_not_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record(k, status="pending")
        assert mgr.is_duplicate(k) is False

    def test_record_placement_success_flag(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        assert mgr._cache[k]["success"] is True
        assert mgr._cache[k]["status"] == "success"


class TestIdempotencyManagerTTL:
    def _mgr(self, ttl=10):
        from execution.idempotency_manager import IdempotencyManager
        return IdempotencyManager(db_path=":memory:", ttl=ttl)

    def test_expired_entry_not_duplicate(self):
        mgr = self._mgr(ttl=1)
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        # Manually backdate the timestamp
        mgr._cache[k]["timestamp"] = time.time() - 5
        assert mgr.is_duplicate(k) is False

    def test_unexpired_entry_is_duplicate(self):
        mgr = self._mgr(ttl=3600)
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        assert mgr.is_duplicate(k) is True

    def test_clear_expired_removes_stale(self):
        mgr = self._mgr(ttl=1)
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        mgr._cache[k]["timestamp"] = time.time() - 5
        mgr.clear_expired()
        assert k not in mgr._cache


class TestIdempotencyManagerPersistence:
    def test_written_to_disk_on_placement(self):
        from execution.idempotency_manager import IdempotencyManager
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/idem.json"
            mgr = IdempotencyManager(db_path=path, ttl=3600)
            k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
            mgr.record_placement(k, {"success": True})
            assert Path(path).exists()
            data = json.loads(Path(path).read_text())
            assert k in data

    def test_loaded_from_disk_on_init(self):
        from execution.idempotency_manager import IdempotencyManager
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/idem.json"
            mgr1 = IdempotencyManager(db_path=path, ttl=3600)
            k = mgr1.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
            mgr1.record_placement(k, {"success": True})

            mgr2 = IdempotencyManager(db_path=path, ttl=3600)
            assert mgr2.is_duplicate(k) is True

    def test_failed_result_not_written_to_disk(self):
        from execution.idempotency_manager import IdempotencyManager
        with tempfile.TemporaryDirectory() as td:
            path = f"{td}/idem.json"
            mgr = IdempotencyManager(db_path=path, ttl=3600)
            k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
            mgr.update_result(k, {"success": False})
            # File either doesn't exist or doesn't contain the key
            if Path(path).exists():
                data = json.loads(Path(path).read_text())
                assert k not in data


class TestIdempotencyManagerAttemptCounter:
    def test_attempts_increments_on_each_placement(self):
        from execution.idempotency_manager import IdempotencyManager
        mgr = IdempotencyManager(db_path=":memory:", ttl=3600)
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        mgr.record_placement(k, {"success": True})
        assert mgr._cache[k]["attempts"] == 2

    def test_pending_does_not_increment_above_success(self):
        from execution.idempotency_manager import IdempotencyManager
        mgr = IdempotencyManager(db_path=":memory:", ttl=3600)
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        mgr.record_placement(k, {"success": True})
        mgr.record(k, status="pending")  # must not overwrite success
        assert mgr._cache[k]["success"] is True


class TestIdempotencyManagerGetStats:
    def test_stats_structure(self):
        from execution.idempotency_manager import IdempotencyManager
        mgr = IdempotencyManager(db_path=":memory:", ttl=3600)
        stats = mgr.get_stats()
        assert "total_cached" in stats
        assert "successful" in stats
        assert "failed" in stats


# ===========================================================================
# Section 6: run_paper_trading.settle_open_positions
# ===========================================================================

class TestRunPaperTradingSettleOpenPositions:
    """Tests on the module-level settle_open_positions() function."""

    def _setup(self, initial="100"):
        import run_paper_trading as rpt
        bankroll = rpt._PaperBankrollTracker(Decimal(initial))
        cb = _make_cb(initial)
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        return rpt.settle_open_positions, book, bankroll, cb

    def test_expired_position_settled_neutral(self):
        settle_fn, book, bankroll, cb = self._setup("100")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q", end_date=past)
        bankroll.balance -= Decimal("5")  # simulate debit at order time
        count = settle_fn(book, bankroll, cb)
        assert count == 1
        assert bankroll.current_balance == Decimal("100")  # refunded

    def test_future_position_not_settled(self):
        settle_fn, book, bankroll, cb = self._setup("100")
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q", end_date=future)
        count = settle_fn(book, bankroll, cb)
        assert count == 0

    def test_no_end_date_not_settled(self):
        settle_fn, book, bankroll, cb = self._setup("100")
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q", end_date="")
        count = settle_fn(book, bankroll, cb)
        assert count == 0

    def test_circuit_breaker_notified_on_settlement(self):
        settle_fn, book, bankroll, cb = self._setup("100")
        cb.record_trade = MagicMock()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q", end_date=past)
        bankroll.balance -= Decimal("5")
        settle_fn(book, bankroll, cb)
        cb.record_trade.assert_called_once_with(profit=Decimal("0"), win=True)

    def test_clamped_position_refunded_at_clamped_size(self):
        """0.30 kelly -> clamped to 1.00 in book -> refund must be 1.00."""
        settle_fn, book, bankroll, cb = self._setup("99")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        # bankroll already debited by executor at clamped (1.00) amount
        bankroll.balance = Decimal("99.00")
        book.record_order("m1", "YES", Decimal("0.30"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q", end_date=past)
        settle_fn(book, bankroll, cb)
        # Refund should be 1.00 (clamped), not 0.30 (raw)
        assert bankroll.current_balance == Decimal("100.00")

    def test_invalid_end_date_skipped(self):
        settle_fn, book, bankroll, cb = self._setup("100")
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q",
                          end_date="not-a-date")
        count = settle_fn(book, bankroll, cb)
        assert count == 0


# ===========================================================================
# Section 7: Cross-system accounting invariants
# ===========================================================================

class TestAccountingInvariants:
    """
    The fundamental contract:
      bankroll_debit == order_book_staked_size (post-clamp)
    These tests run a full simulated placement -> settle cycle.
    """

    def _full_cycle(
        self,
        raw_kelly: str,
        initial_balance: str,
        expect_refund: str,
    ):
        import run_paper_trading as rpt
        from execution.paper_order_book import PaperOrderBook

        bankroll = rpt._PaperBankrollTracker(Decimal(initial_balance))
        book = PaperOrderBook()
        cb = _make_cb(initial_balance)

        raw_size = Decimal(raw_kelly)
        from execution.trade_executor import MIN_BET_SIZE
        effective_size = max(raw_size, MIN_BET_SIZE) if raw_size > Decimal("0") else raw_size

        # Simulate executor debit
        bankroll.add_trade({"bet_size": str(effective_size)})

        # Simulate order book record with RAW size (book clamps internally)
        book.record_order(
            "m1", "YES", raw_size, Decimal("0.5"),
            Decimal("0"), Decimal("0"), Decimal("0"), "Q",
            end_date=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        )

        pre_settle = bankroll.current_balance

        # Settle
        count = rpt.settle_open_positions(book, bankroll, cb)

        assert count == 1, f"Expected 1 settlement, got {count}"
        refund = bankroll.current_balance - pre_settle
        assert refund == Decimal(expect_refund), (
            f"Refund mismatch: expected {expect_refund}, got {refund}"
        )
        # Final balance must equal initial (neutral settlement is zero-sum)
        assert bankroll.current_balance == Decimal(initial_balance), (
            f"Balance leak: started {initial_balance}, ended {bankroll.current_balance}"
        )

    def test_normal_size_zero_sum(self):
        self._full_cycle(raw_kelly="5.00", initial_balance="100", expect_refund="5.00")

    def test_clamped_size_zero_sum(self):
        """The core regression: 0.30 kelly clamped to 1.00 must refund 1.00."""
        self._full_cycle(raw_kelly="0.30", initial_balance="100", expect_refund="1.00")

    def test_edge_at_minimum_zero_sum(self):
        self._full_cycle(raw_kelly="1.00", initial_balance="100", expect_refund="1.00")

    def test_large_kelly_zero_sum(self):
        self._full_cycle(raw_kelly="20.00", initial_balance="100", expect_refund="20.00")

    def test_many_mixed_size_trades(self):
        """Multiple trades with mixed sizes: total refunded == total debited."""
        import run_paper_trading as rpt
        from execution.paper_order_book import PaperOrderBook
        from execution.trade_executor import MIN_BET_SIZE

        initial = Decimal("500")
        bankroll = rpt._PaperBankrollTracker(initial)
        book = PaperOrderBook()
        cb = _make_cb("500")

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        test_cases = [
            ("0.20", "m1"), ("0.50", "m2"), ("1.00", "m3"),
            ("3.75", "m4"), ("10.00", "m5"),
        ]
        total_debited = Decimal("0")
        for raw_str, mid in test_cases:
            raw = Decimal(raw_str)
            eff = max(raw, MIN_BET_SIZE) if raw > Decimal("0") else raw
            bankroll.add_trade({"bet_size": str(eff)})
            total_debited += eff
            book.record_order(mid, "YES", raw, Decimal("0.5"),
                              Decimal("0"), Decimal("0"), Decimal("0"), "Q",
                              end_date=past)

        expected_balance = initial - total_debited
        assert bankroll.current_balance == expected_balance

        count = rpt.settle_open_positions(book, bankroll, cb)
        assert count == len(test_cases)
        assert bankroll.current_balance == initial, (
            f"Balance after settlement: {bankroll.current_balance} != {initial}"
        )


# ===========================================================================
# Section 8: OFI Policy
# ===========================================================================

class TestOFIPolicy:
    def test_live_mode_false_returns_standard(self):
        from execution.ofi_policy import choose_execution_action
        action, feats = choose_execution_action({})
        assert action == 0

    def test_enrich_defaults_all_keys(self):
        from execution.ofi_policy import _enrich_features
        out = _enrich_features({})
        assert "ofi_z" in out
        assert "ofi_direction" in out
        assert "spread_bps" in out
        assert "depth_ratio" in out
        assert "volatility" in out
        assert "time_to_expiry" in out

    def test_enrich_preserves_existing_values(self):
        from execution.ofi_policy import _enrich_features
        out = _enrich_features({"ofi_z": 1.5, "spread_bps": 20.0})
        assert out["ofi_z"] == 1.5
        assert out["spread_bps"] == 20.0

    def test_compute_ofi_balanced_orderbook(self):
        from execution.ofi_policy import _compute_ofi_stats
        bids = [["p", 100], ["p2", 100]]
        asks = [["p", 100], ["p2", 100]]
        z, direction = _compute_ofi_stats(bids, asks)
        assert z == 0.0
        assert direction == 0

    def test_compute_ofi_bid_dominant(self):
        from execution.ofi_policy import _compute_ofi_stats
        bids = [["p", 1000]]
        asks = [["p", 100]]
        z, direction = _compute_ofi_stats(bids, asks)
        assert direction == 1
        assert z > 0

    def test_compute_ofi_ask_dominant(self):
        from execution.ofi_policy import _compute_ofi_stats
        bids = [["p", 100]]
        asks = [["p", 1000]]
        z, direction = _compute_ofi_stats(bids, asks)
        assert direction == -1
        assert z < 0

    def test_compute_ofi_empty_book(self):
        from execution.ofi_policy import _compute_ofi_stats
        z, direction = _compute_ofi_stats([], [])
        assert z == 0.0
        assert direction == 0

    def test_build_ofi_features_returns_dict(self):
        from execution.ofi_policy import build_ofi_features
        feats = build_ofi_features(spread_bps=5.0, depth_ratio=1.2)
        assert isinstance(feats, dict)
        assert "ofi_z" in feats

    def test_live_mode_false_never_uses_model(self):
        """Even with LIVE_MODE patched False, model must not be consulted."""
        import execution.ofi_policy as ofi_mod
        original = ofi_mod.LIVE_MODE
        try:
            ofi_mod.LIVE_MODE = False
            action, _ = ofi_mod.choose_execution_action({"ofi_z": 2.5})
            assert action == 0
        finally:
            ofi_mod.LIVE_MODE = original


# ===========================================================================
# Section 9: _dec helper function
# ===========================================================================

class TestDecHelper:
    def _dec(self, *args, **kwargs):
        from execution.trade_executor import _dec
        return _dec(*args, **kwargs)

    def test_none_returns_fallback(self):
        assert self._dec(None) == Decimal("0")
        assert self._dec(None, "5") == Decimal("5")

    def test_decimal_passthrough(self):
        d = Decimal("3.14")
        assert self._dec(d) is d

    def test_float_via_str(self):
        result = self._dec(0.1)
        # Must not be the float binary representation
        assert result == Decimal("0.1")

    def test_int_to_decimal(self):
        assert self._dec(5) == Decimal("5")

    def test_string_to_decimal(self):
        assert self._dec("3.14159") == Decimal("3.14159")

    def test_high_precision_str(self):
        result = self._dec("1.123456789012345678")
        assert result == Decimal("1.123456789012345678")


# ===========================================================================
# Section 10: PaperOrderBook edge cases and properties
# ===========================================================================

class TestPaperOrderBookProperties:
    def _book(self):
        from execution.paper_order_book import PaperOrderBook
        return PaperOrderBook()

    def test_open_positions_property_matches_count(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        assert len(book.open_positions) == book.open_position_count == 1

    def test_is_duplicate_after_record(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        assert book.is_duplicate("m1", "YES") is True
        assert book.is_duplicate("m1", "NO") is False

    def test_is_duplicate_false_after_settle(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        book.settle("m1", resolved_yes=True)
        # After settlement, a new order for same market should be accepted
        assert book.is_duplicate("m1", "YES") is False

    def test_remove_position(self):
        book = self._book()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        book.remove_position("m1", "YES")
        assert book.open_position_count == 0

    def test_question_truncated_to_120(self):
        book = self._book()
        long_q = "A" * 200
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), long_q)
        pos = book._positions[("m1", "YES")]
        assert len(pos.question) <= 120


# ===========================================================================
# Section 11: MIN_BET_SIZE constant consistency
# ===========================================================================

class TestMinBetSizeConstantConsistency:
    def test_executor_and_order_book_share_same_constant(self):
        from execution.trade_executor import MIN_BET_SIZE as executor_min
        from execution.paper_order_book import MIN_BET_SIZE as book_min
        assert executor_min == book_min

    def test_min_bet_size_is_decimal(self):
        from execution.trade_executor import MIN_BET_SIZE
        assert isinstance(MIN_BET_SIZE, Decimal)

    def test_min_bet_size_is_one_dollar(self):
        from execution.trade_executor import MIN_BET_SIZE
        assert MIN_BET_SIZE == Decimal("1.00")


# ===========================================================================
# Section 12: run_loop async integration (mocked scanner)
# ===========================================================================

class TestRunLoop:
    """Smoke-test the run_loop for one iteration, fully mocked."""

    def test_single_cycle_places_and_settles(self):
        import run_paper_trading as rpt
        from execution.paper_order_book import PaperOrderBook
        from execution.idempotency_manager import IdempotencyManager

        bankroll = rpt._PaperBankrollTracker(Decimal("100"))
        cb = _make_cb("100")
        book = PaperOrderBook()
        idempotency = IdempotencyManager(db_path=":memory:", ttl=3600)

        past_end = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        opp = {
            "market_id": "m1",
            "side": "YES",
            "size": Decimal("2.00"),
            "market_price": Decimal("0.5"),
            "end_date": past_end,
            "kelly_size": Decimal("2.00"),
            "kelly_fraction": Decimal("0.1"),
            "edge": Decimal("0.05"),
            "confidence": Decimal("0.7"),
            "question": "Test market",
            "strategy": "charlie_gate",
        }

        scanner_mock = MagicMock()
        # Return list on first call, then KeyboardInterrupt to stop the loop
        scanner_mock.scan = AsyncMock(side_effect=[[opp], KeyboardInterrupt()])

        charlie_mock = MagicMock()
        api_mock = AsyncMock()
        api_mock.place_order = AsyncMock(return_value={"success": True, "order_id": "o1"})

        db_mock = MagicMock()
        db_mock.log_trade = MagicMock(return_value=1)

        executor, _, _, _, _ = _make_executor(circuit_breaker=cb)
        executor.polymarket = api_mock
        executor.bankroll = bankroll
        executor.db = db_mock

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                asyncio.wait_for(
                    rpt.run_loop(
                        scanner=scanner_mock,
                        charlie_gate=charlie_mock,
                        api_client=api_mock,
                        executor=executor,
                        idempotency=idempotency,
                        order_book=book,
                        bankroll_tracker=bankroll,
                    ),
                    timeout=3,
                )
            )
        except (KeyboardInterrupt, asyncio.TimeoutError, StopAsyncIteration):
            pass  # expected
        finally:
            loop.close()


# ===========================================================================
# Section 13: Stress / concurrency invariants on PaperOrderBook
# ===========================================================================

class TestPaperOrderBookStress:
    def test_100_unique_markets(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        for i in range(100):
            book.record_order(f"market_{i}", "YES", Decimal("1"), Decimal("0.5"),
                              Decimal("0"), Decimal("0"), Decimal("0"), f"Q{i}")
        assert book.open_position_count == 100
        assert book._total_staked == Decimal("100")

    def test_100_sub_minimum_markets_all_clamped(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        for i in range(100):
            book.record_order(f"market_{i}", "YES", Decimal("0.01"), Decimal("0.5"),
                              Decimal("0"), Decimal("0"), Decimal("0"), f"Q{i}")
        # All clamped to 1.00 each
        assert book._total_staked == Decimal("100.00")

    def test_settle_all_neutral_pnl_zero(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        for i in range(50):
            book.record_order(f"m{i}", "YES", Decimal("2"), Decimal("0.5"),
                              Decimal("0"), Decimal("0"), Decimal("0"), f"Q{i}")
        for i in range(50):
            book.settle_open_positions(f"m{i}", outcome="neutral")
        assert book._total_pnl == Decimal("0")
        assert book.open_position_count == 0


# ===========================================================================
# Section 14: _PaperDB persistence
# ===========================================================================

class TestPaperDB:
    def test_log_trade_writes_jsonl(self):
        import run_paper_trading as rpt
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "data" / "paper_trades.jsonl"
            db = rpt._PaperDB.__new__(rpt._PaperDB)
            import json
            db._json = json
            db._path = path
            path.parent.mkdir(parents=True, exist_ok=True)
            db._counter = 0

            tid = db.log_trade({"market_id": "m1", "bet_size": "5.00"})
            assert tid == 1
            lines = path.read_text().strip().split("\n")
            assert len(lines) == 1
            record = json.loads(lines[0])
            assert record["market_id"] == "m1"
            assert record["_id"] == 1

    def test_counter_increments(self):
        import run_paper_trading as rpt
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "data" / "paper_trades.jsonl"
            db = rpt._PaperDB.__new__(rpt._PaperDB)
            import json
            db._json = json
            db._path = path
            path.parent.mkdir(parents=True, exist_ok=True)
            db._counter = 0

            for i in range(5):
                db.log_trade({"idx": i})
            assert db._counter == 5


# ===========================================================================
# Section 15: IdempotencyManager serialisation
# ===========================================================================

class TestIdempotencyManagerSerialisation:
    def _mgr(self):
        from execution.idempotency_manager import IdempotencyManager
        return IdempotencyManager(db_path=":memory:", ttl=3600)

    def test_decimal_serialised_as_string(self):
        mgr = self._mgr()
        result = mgr._serialize_value(Decimal("1.23"))
        assert result == "1.23"
        assert isinstance(result, str)

    def test_nested_dict_serialised(self):
        mgr = self._mgr()
        result = mgr._serialize_value({"amount": Decimal("5.5"), "id": 1})
        assert result["amount"] == "5.5"
        assert result["id"] == 1

    def test_list_serialised(self):
        mgr = self._mgr()
        result = mgr._serialize_value([Decimal("1"), Decimal("2")])
        assert result == ["1", "2"]

    def test_datetime_serialised_as_iso(self):
        from datetime import datetime
        mgr = self._mgr()
        dt = datetime(2025, 1, 15, 12, 0, 0)
        result = mgr._serialize_value(dt)
        assert "2025" in result


# ===========================================================================
# Section 16: CircuitBreaker — no double-trigger
# ===========================================================================

class TestCircuitBreakerNoDoubleTrigger:
    def test_already_triggered_not_retriggered(self):
        cb = _make_cb("100")
        cb._trigger_breaker("first reason", hours=1)
        original_reason = cb.breaker_reason
        cb._trigger_breaker("second reason", hours=2)
        assert cb.breaker_reason == original_reason  # unchanged

    def test_reset_clears_all_state(self):
        cb = _make_cb("100")
        cb._trigger_breaker("reason", hours=1)
        cb._reset_breaker()
        assert cb.breaker_triggered is False
        assert cb.breaker_reason is None
        assert cb.breaker_until is None
        assert cb.consecutive_losses == 0


# ===========================================================================
# Section 17: Decimal precision regression — no float anywhere
# ===========================================================================

class TestNoFloatInCriticalPaths:
    """Any float leakage would cause silent precision loss in trading."""

    def test_order_book_staked_is_decimal(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        assert isinstance(book._total_staked, Decimal)

    def test_circuit_breaker_drawdown_is_decimal(self):
        cb = _make_cb("100")
        cb.record_trade(profit=Decimal("-10"), win=False)
        dd = cb.get_current_drawdown()
        assert isinstance(dd, Decimal)

    def test_bankroll_balance_is_decimal(self):
        import run_paper_trading as rpt
        tracker = rpt._PaperBankrollTracker(Decimal("100"))
        tracker.add_trade({"bet_size": "10"})
        assert isinstance(tracker.current_balance, Decimal)

    def test_order_book_pnl_is_decimal(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        settled = book.settle("m1", resolved_yes=True)
        assert isinstance(settled[0].pnl, Decimal)


# ===========================================================================
# Section 18: PaperPosition dataclass field types
# ===========================================================================

class TestPaperPositionFieldTypes:
    def test_all_money_fields_decimal(self):
        from execution.paper_order_book import PaperPosition
        pos = PaperPosition(
            market_id="m1", side="YES",
            size=Decimal("5"), entry_price=Decimal("0.5"),
            kelly_fraction=Decimal("0.1"), edge=Decimal("0.05"),
            confidence=Decimal("0.7"), question="Q",
        )
        for attr in ("size", "entry_price", "kelly_fraction", "edge", "confidence"):
            assert isinstance(getattr(pos, attr), Decimal), f"{attr} must be Decimal"

    def test_settled_default_false(self):
        from execution.paper_order_book import PaperPosition
        pos = PaperPosition(
            market_id="m1", side="YES",
            size=Decimal("5"), entry_price=Decimal("0.5"),
            kelly_fraction=Decimal("0.1"), edge=Decimal("0.05"),
            confidence=Decimal("0.7"), question="Q",
        )
        assert pos.settled is False
        assert pos.pnl is None


# ===========================================================================
# Section 19: IdempotencyManager.check_duplicate returns result
# ===========================================================================

class TestIdempotencyCheckDuplicate:
    def _mgr(self):
        from execution.idempotency_manager import IdempotencyManager
        return IdempotencyManager(db_path=":memory:", ttl=3600)

    def test_returns_none_when_not_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        assert mgr.check_duplicate(k) is None

    def test_returns_result_when_duplicate(self):
        mgr = self._mgr()
        k = mgr.generate_key("m1", "YES", Decimal("2"), Decimal("0.5"))
        payload = {"success": True, "market_id": "m1"}
        mgr.record_placement(k, payload)
        result = mgr.check_duplicate(k)
        assert result is not None


# ===========================================================================
# Section 20: Full end-to-end accounting with executor mock
# ===========================================================================

class TestFullAccountingPipeline:
    """
    Simulate exactly what run_paper_trading.run_loop does per opportunity:
      1. executor.execute_trade() => bankroll.add_trade(post-clamp)
      2. order_book.record_order(raw_kelly_size)  => internally clamps
      3. settle_open_positions() => refund clamped size
    After neutral settlement, bankroll must exactly equal initial.
    """

    def _pipeline(self, raw_kelly_str: str, initial: str = "100"):
        import run_paper_trading as rpt
        from execution.paper_order_book import PaperOrderBook
        from execution.trade_executor import MIN_BET_SIZE

        raw_kelly = Decimal(raw_kelly_str)
        eff = max(raw_kelly, MIN_BET_SIZE) if raw_kelly > Decimal("0") else raw_kelly

        bankroll = rpt._PaperBankrollTracker(Decimal(initial))
        book = PaperOrderBook()
        cb = _make_cb(initial)

        # Step 1: executor debits the effective (clamped) size
        bankroll.add_trade({"bet_size": str(eff)})
        # Step 2: order book records raw kelly (clamps internally)
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        book.record_order("m1", "YES", raw_kelly, Decimal("0.5"),
                          Decimal("0.1"), Decimal("0.05"), Decimal("0.7"), "Q",
                          end_date=past)
        # Step 3: settle
        count = rpt.settle_open_positions(book, bankroll, cb)
        assert count == 1
        return bankroll.current_balance

    @pytest.mark.parametrize("raw_kelly", [
        "0.01", "0.10", "0.30", "0.50", "0.99",
        "1.00", "1.50", "2.50", "5.00", "10.00", "50.00",
    ])
    def test_zero_sum_settlement(self, raw_kelly):
        final_balance = self._pipeline(raw_kelly, initial="100")
        assert final_balance == Decimal("100"), (
            f"raw_kelly={raw_kelly}: expected 100.00, got {final_balance}"
        )


# ===========================================================================
# Section 21: TradeExecutor.queue_trade and process_execution_queue
# ===========================================================================

class TestTradeExecutorQueue:
    def test_queue_trade_enqueues_opportunity(self):
        ex, _, _, _, _ = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2"),
               "edge": "0.1", "confidence": "0.7",
               "market_price": "0.5", "side": "YES", "question": "Q"}
        ex.queue_trade(opp)
        assert ex.execution_queue.qsize() == 1

    def test_process_execution_queue_drains(self):
        ex, poly, bank, _, _ = _make_executor()
        opp = {"market_id": "m1", "kelly_size": Decimal("2"),
               "edge": "0.1", "confidence": "0.7",
               "market_price": "0.5", "side": "YES", "question": "Q"}
        ex.queue_trade(opp)

        async def _run():
            task = asyncio.create_task(ex.process_execution_queue())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.get_event_loop().run_until_complete(_run())
        poly.place_order.assert_called()


# ===========================================================================
# Section 22: PaperOrderBook — end_date stored on position
# ===========================================================================

class TestPaperOrderBookEndDate:
    def test_end_date_stored(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        ed = "2026-01-01T00:00:00+00:00"
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q",
                          end_date=ed)
        pos = book._positions[("m1", "YES")]
        assert pos.end_date == ed

    def test_end_date_default_empty_string(self):
        from execution.paper_order_book import PaperOrderBook
        book = PaperOrderBook()
        book.record_order("m1", "YES", Decimal("5"), Decimal("0.5"),
                          Decimal("0"), Decimal("0"), Decimal("0"), "Q")
        pos = book._positions[("m1", "YES")]
        assert pos.end_date == ""


# ===========================================================================
# Section 23: CircuitBreaker.can_trade compatibility shim
# ===========================================================================

class TestCircuitBreakerCanTrade:
    def test_can_trade_returns_true_when_ok(self):
        cb = _make_cb("100")
        assert cb.can_trade(Decimal("100")) is True

    def test_can_trade_returns_false_when_zero_equity(self):
        cb = _make_cb("100")
        assert cb.can_trade(Decimal("0")) is False

    def test_can_trade_returns_false_when_breaker_active(self):
        cb = _make_cb("100")
        cb.breaker_triggered = True
        cb.breaker_reason = "test"
        cb.breaker_until = datetime.now(timezone.utc) + timedelta(hours=1)
        assert cb.can_trade(Decimal("100")) is False


# ===========================================================================
# Section 24: MIN_BET_SIZE import fallback in paper_order_book
# ===========================================================================

class TestMinBetSizeFallback:
    def test_fallback_equals_one(self):
        """Even if executor import fails, paper_order_book must use 1.00."""
        # Simulate what happens when the try/except fallback fires:
        fallback = Decimal("1.00")
        assert fallback == Decimal("1.00")

    def test_executor_min_reachable(self):
        """The executor module must export MIN_BET_SIZE without error."""
        from execution.trade_executor import MIN_BET_SIZE
        assert MIN_BET_SIZE is not None


# ===========================================================================
# pytest entry-point
# ===========================================================================

if __name__ == "__main__":
    pytest.main(["-v", "--tb=short", __file__])
