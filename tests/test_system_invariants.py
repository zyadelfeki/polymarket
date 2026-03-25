"""
tests/test_system_invariants.py

Pytest suite for infra.system_invariants.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on the path so `infra` is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from infra.system_invariants import (
    check_approvals_vs_submissions,
    check_ofi_feed_staleness,
    check_binance_feed_health,
    check_balance_vs_equity,
    evaluate_all,
)


# ---------------------------------------------------------------------------
# check_approvals_vs_submissions
# ---------------------------------------------------------------------------


class TestApprovalsVsSubmissions:
    def test_approved_no_submitted_no_blocked__fails(self):
        """approved > 0, submitted = 0, blocked = 0 → violation."""
        ok, msg = check_approvals_vs_submissions(
            {"charlie_gate_approved": 3, "orders_submitted": 0}
        )
        assert ok is False
        assert msg is not None
        assert "charlie_gate_approved=3" in msg
        assert "orders_submitted=0" in msg

    def test_approved_with_submitted__passes(self):
        """approved > 0, submitted > 0 → no violation."""
        ok, msg = check_approvals_vs_submissions(
            {"charlie_gate_approved": 5, "orders_submitted": 3}
        )
        assert ok is True
        assert msg is None

    def test_approved_no_submitted_but_blocked__passes(self):
        """approved > 0, submitted = 0, but blocked > 0 → no violation (blocked explains it)."""
        ok, msg = check_approvals_vs_submissions(
            {
                "charlie_gate_approved": 2,
                "orders_submitted": 0,
                "blocked_risk_budget": 2,
            }
        )
        assert ok is True
        assert msg is None

    def test_zero_approved_zero_submitted__passes(self):
        """Nothing happened → no violation."""
        ok, msg = check_approvals_vs_submissions(
            {"charlie_gate_approved": 0, "orders_submitted": 0}
        )
        assert ok is True
        assert msg is None

    def test_missing_keys_treated_as_zero(self):
        """Sparse dict (no keys) must not raise KeyError."""
        ok, msg = check_approvals_vs_submissions({})
        assert ok is True
        assert msg is None


# ---------------------------------------------------------------------------
# check_ofi_feed_staleness
# ---------------------------------------------------------------------------


class TestOFIFeedStaleness:
    def test_below_threshold__passes(self):
        ok, msg = check_ofi_feed_staleness(4)
        assert ok is True
        assert msg is None

    def test_at_threshold__fails(self):
        ok, msg = check_ofi_feed_staleness(5)
        assert ok is False
        assert msg is not None
        assert "5" in msg

    def test_above_threshold__fails(self):
        ok, msg = check_ofi_feed_staleness(10)
        assert ok is False
        assert "10" in msg

    def test_zero_misses__passes(self):
        ok, msg = check_ofi_feed_staleness(0)
        assert ok is True

    def test_custom_threshold(self):
        ok, msg = check_ofi_feed_staleness(3, threshold=3)
        assert ok is False
        ok2, _ = check_ofi_feed_staleness(2, threshold=3)
        assert ok2 is True


# ---------------------------------------------------------------------------
# check_binance_feed_health
# ---------------------------------------------------------------------------


class TestBinanceFeedHealth:
    def test_healthy__passes(self):
        ok, msg = check_binance_feed_health(True, 5, 5)
        assert ok is True
        assert msg is None

    def test_unhealthy__fails(self):
        ok, msg = check_binance_feed_health(False, 3, 5)
        assert ok is False
        assert "3/5" in msg

    def test_healthy_partial_symbols__still_healthy_if_flag_true(self):
        """health flag determines result, not partial-symbol count."""
        ok, msg = check_binance_feed_health(True, 3, 5)
        assert ok is True


# ---------------------------------------------------------------------------
# check_balance_vs_equity
# ---------------------------------------------------------------------------


class TestBalanceVsEquity:
    def test_within_epsilon__passes(self):
        ok, msg = check_balance_vs_equity(100.00, 100.30)
        assert ok is True
        assert msg is None

    def test_exceeds_epsilon__fails(self):
        ok, msg = check_balance_vs_equity(100.00, 100.60)
        assert ok is False
        assert msg is not None
        assert "diff=" in msg

    def test_exactly_at_epsilon__passes(self):
        """Boundary: diff == epsilon should pass (threshold is strictly >)."""
        ok, msg = check_balance_vs_equity(0.0, 0.50)
        assert ok is True

    def test_custom_epsilon(self):
        ok, msg = check_balance_vs_equity(0.0, 0.10, epsilon=0.05)
        assert ok is False
        ok2, _ = check_balance_vs_equity(0.0, 0.04, epsilon=0.05)
        assert ok2 is True


# ---------------------------------------------------------------------------
# evaluate_all
# ---------------------------------------------------------------------------


class TestEvaluateAll:
    def test_returns_required_keys(self):
        result = evaluate_all(stats={})
        assert "approvals_vs_submissions" in result
        assert "ofi_feed_staleness" in result
        assert "binance_feed_health" in result

    def test_balance_key_absent_when_none(self):
        result = evaluate_all(stats={}, api_balance=None, ledger_equity=None)
        assert "balance_vs_equity" not in result

    def test_balance_key_present_when_both_given(self):
        result = evaluate_all(stats={}, api_balance=100.0, ledger_equity=100.0)
        assert "balance_vs_equity" in result

    def test_all_clear(self):
        stats = {"charlie_gate_approved": 2, "orders_submitted": 1}
        result = evaluate_all(stats=stats, ofi_miss_count=0, binance_healthy=True)
        for name, (ok, msg) in result.items():
            assert ok is True, f"Expected {name} to pass, got: {msg}"

    def test_violation_propagates(self):
        stats = {"charlie_gate_approved": 5, "orders_submitted": 0}
        result = evaluate_all(stats=stats, ofi_miss_count=0, binance_healthy=True)
        ok, msg = result["approvals_vs_submissions"]
        assert ok is False
        assert msg is not None
