"""
tests/test_check_session_invariants.py

Integration tests for the check_session invariant layer.

These tests replay synthetic event-count dicts through
infra.system_invariants.evaluate_all and assert that every invariant
defined in the T1-T4 hardening plan behaves correctly under all
critical scenarios — clean session, violations, edge cases.

No live APIs, no environment secrets, no network calls.
All inputs are synthetic log-event counters exactly as check_session.py
would compute them from a structlog JSONL session file.
"""
from __future__ import annotations

import sys
from pathlib import Path

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
# Helpers
# ---------------------------------------------------------------------------

def _clean_stats(**overrides) -> dict:
    """Return a baseline healthy-session stats dict."""
    base = {
        "charlie_gate_approved": 4,
        "charlie_gate_rejected": 6,
        "orders_submitted": 4,
        "orders_filled": 3,
        "orders_failed": 0,
        "ofi_feed_degraded": 0,
        "task_died": 0,
        "blocked_risk_budget": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Scenario 1: Fully clean session — ALL invariants must pass
# ---------------------------------------------------------------------------

class TestCleanSession:
    def test_all_invariants_pass_on_healthy_session(self):
        stats = _clean_stats()
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=0,
            binance_healthy=True,
            binance_connected=5,
            binance_expected=5,
            api_balance=100.00,
            ledger_equity=100.10,
        )
        violations = [
            (name, msg)
            for name, (ok, msg) in result.items()
            if not ok
        ]
        assert violations == [], (
            f"Expected zero violations on a clean session, got: {violations}"
        )

    def test_no_activity_session_also_passes(self):
        """A session where nothing fired (bot ran but found no markets) is valid."""
        stats = _clean_stats(
            charlie_gate_approved=0,
            charlie_gate_rejected=0,
            orders_submitted=0,
        )
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=0,
            binance_healthy=True,
        )
        for name, (ok, msg) in result.items():
            assert ok is True, f"{name} failed unexpectedly: {msg}"


# ---------------------------------------------------------------------------
# Scenario 2: INV-1 — Approved but zero submissions (no risk-budget block)
#   This is the canonical "Charlie said yes but nothing was sent" bug.
# ---------------------------------------------------------------------------

class TestINV1ApprovedButNoSubmission:
    def test_approved_without_submission_fires_violation(self):
        stats = _clean_stats(charlie_gate_approved=3, orders_submitted=0, blocked_risk_budget=0)
        ok, msg = check_approvals_vs_submissions(stats)
        assert ok is False
        assert "charlie_gate_approved=3" in msg
        assert "orders_submitted=0" in msg

    def test_approved_without_submission_but_all_blocked_by_risk(self):
        """If risk budget blocked all approvals, no violation."""
        stats = _clean_stats(
            charlie_gate_approved=3,
            orders_submitted=0,
            blocked_risk_budget=3,
        )
        ok, msg = check_approvals_vs_submissions(stats)
        assert ok is True
        assert msg is None

    def test_partial_block_still_fires_if_unexplained_gap_remains(self):
        """2 approved, 0 submitted, only 1 blocked → 1 unexplained → violation."""
        stats = _clean_stats(
            charlie_gate_approved=2,
            orders_submitted=0,
            blocked_risk_budget=1,
        )
        ok, msg = check_approvals_vs_submissions(stats)
        assert ok is False

    def test_evaluate_all_surfaces_this_violation(self):
        stats = _clean_stats(charlie_gate_approved=5, orders_submitted=0)
        result = evaluate_all(stats=stats, ofi_miss_count=0, binance_healthy=True)
        ok, msg = result["approvals_vs_submissions"]
        assert ok is False
        assert msg is not None


# ---------------------------------------------------------------------------
# Scenario 3: INV-2 — OFI feed degradation at/above threshold
# ---------------------------------------------------------------------------

class TestINV2OFIFeedDegradation:
    @pytest.mark.parametrize("misses,expected_ok", [
        (0, True),
        (1, True),
        (4, True),
        (5, False),   # default threshold = 5
        (6, False),
        (100, False),
    ])
    def test_ofi_threshold_boundary(self, misses, expected_ok):
        ok, msg = check_ofi_feed_staleness(misses)
        assert ok is expected_ok, (
            f"misses={misses}: expected ok={expected_ok}, got ok={ok} msg={msg}"
        )

    def test_evaluate_all_ofi_violation_propagates(self):
        stats = _clean_stats()
        result = evaluate_all(stats=stats, ofi_miss_count=5, binance_healthy=True)
        ok, msg = result["ofi_feed_staleness"]
        assert ok is False
        assert "5" in msg

    def test_evaluate_all_ofi_below_threshold_passes(self):
        stats = _clean_stats()
        result = evaluate_all(stats=stats, ofi_miss_count=4, binance_healthy=True)
        ok, _ = result["ofi_feed_staleness"]
        assert ok is True


# ---------------------------------------------------------------------------
# Scenario 4: INV-3 — Binance feed unhealthy
# ---------------------------------------------------------------------------

class TestINV3BinanceFeedHealth:
    def test_unhealthy_flag_fires_violation(self):
        ok, msg = check_binance_feed_health(
            healthy=False, connected_symbols=3, expected_symbols=5
        )
        assert ok is False
        assert "3/5" in msg

    def test_healthy_flag_full_symbols_passes(self):
        ok, msg = check_binance_feed_health(
            healthy=True, connected_symbols=5, expected_symbols=5
        )
        assert ok is True
        assert msg is None

    def test_evaluate_all_unhealthy_binance_propagates(self):
        stats = _clean_stats()
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=0,
            binance_healthy=False,
            binance_connected=2,
            binance_expected=5,
        )
        ok, msg = result["binance_feed_health"]
        assert ok is False


# ---------------------------------------------------------------------------
# Scenario 5: INV-4 — Balance vs equity mismatch
# ---------------------------------------------------------------------------

class TestINV4BalanceMismatch:
    def test_within_epsilon_passes(self):
        ok, msg = check_balance_vs_equity(api_balance=100.00, ledger_equity=100.40)
        assert ok is True

    def test_beyond_epsilon_fires_violation(self):
        ok, msg = check_balance_vs_equity(api_balance=100.00, ledger_equity=100.60)
        assert ok is False
        assert "diff=" in msg

    def test_exact_epsilon_boundary_passes(self):
        """diff == epsilon is NOT a violation (strictly greater than)."""
        ok, _ = check_balance_vs_equity(0.0, 0.50)
        assert ok is True

    def test_evaluate_all_omits_key_when_balance_not_provided(self):
        stats = _clean_stats()
        result = evaluate_all(stats=stats, ofi_miss_count=0, binance_healthy=True)
        assert "balance_vs_equity" not in result

    def test_evaluate_all_includes_key_when_both_provided(self):
        stats = _clean_stats()
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=0,
            binance_healthy=True,
            api_balance=100.0,
            ledger_equity=100.0,
        )
        assert "balance_vs_equity" in result
        ok, msg = result["balance_vs_equity"]
        assert ok is True

    def test_evaluate_all_fires_mismatch_violation(self):
        stats = _clean_stats()
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=0,
            binance_healthy=True,
            api_balance=100.0,
            ledger_equity=102.0,  # 2.0 >> epsilon=0.5
        )
        ok, msg = result["balance_vs_equity"]
        assert ok is False


# ---------------------------------------------------------------------------
# Scenario 6: Multiple simultaneous violations — evaluate_all returns all
# ---------------------------------------------------------------------------

class TestMultipleViolations:
    def test_two_simultaneous_violations_both_reported(self):
        """Approved-no-submission + OFI degraded must both appear in result."""
        stats = _clean_stats(charlie_gate_approved=3, orders_submitted=0)
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=10,
            binance_healthy=True,
        )
        assert result["approvals_vs_submissions"][0] is False
        assert result["ofi_feed_staleness"][0] is False
        # binance is healthy — must not fire
        assert result["binance_feed_health"][0] is True

    def test_all_four_violations_simultaneously(self):
        stats = _clean_stats(charlie_gate_approved=3, orders_submitted=0)
        result = evaluate_all(
            stats=stats,
            ofi_miss_count=10,
            binance_healthy=False,
            binance_connected=0,
            binance_expected=5,
            api_balance=100.0,
            ledger_equity=110.0,
        )
        violations = [name for name, (ok, _) in result.items() if not ok]
        assert len(violations) == 4, (
            f"Expected 4 violations, got {len(violations)}: {violations}"
        )


# ---------------------------------------------------------------------------
# Scenario 7: evaluate_all return-value contract
# ---------------------------------------------------------------------------

class TestEvaluateAllContract:
    def test_return_is_dict_of_tuples(self):
        result = evaluate_all(stats={})
        assert isinstance(result, dict)
        for name, value in result.items():
            assert isinstance(value, tuple), f"{name} value is not a tuple"
            assert len(value) == 2, f"{name} tuple does not have 2 elements"
            ok, msg = value
            assert isinstance(ok, bool), f"{name}.ok is not bool"
            assert msg is None or isinstance(msg, str), (
                f"{name}.msg must be None or str, got {type(msg)}"
            )

    def test_required_keys_always_present(self):
        result = evaluate_all(stats={})
        for required in (
            "approvals_vs_submissions",
            "ofi_feed_staleness",
            "binance_feed_health",
        ):
            assert required in result, f"Missing required key: {required}"

    def test_sparse_stats_does_not_raise(self):
        """evaluate_all must never raise on a missing-key stats dict."""
        try:
            evaluate_all(stats={"charlie_gate_approved": 1})
        except Exception as exc:
            pytest.fail(f"evaluate_all raised on sparse stats: {exc}")

    def test_all_zero_stats_all_pass(self):
        result = evaluate_all(
            stats={},
            ofi_miss_count=0,
            binance_healthy=True,
            api_balance=50.0,
            ledger_equity=50.0,
        )
        for name, (ok, msg) in result.items():
            assert ok is True, f"{name} failed on all-zero stats: {msg}"
