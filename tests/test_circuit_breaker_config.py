"""
Config invariant tests for config_production.py.

These tests exist to prevent the circuit-breaker / performance-tracker
drawdown mismatch bug from ever recurring.

Bug: CIRCUIT_BREAKER_CONFIG.max_drawdown_pct was 25.0 while
     PERFORMANCE_TRACKER_CONFIG.max_drawdown_halt was 0.15 (15%).
     The bot could lose an extra 10% of capital between the soft halt
     and the hard circuit breaker firing.

Fixed 2026-03-12. This test locks the invariant permanently.
"""
from decimal import Decimal

import pytest

from config_production import (
    CIRCUIT_BREAKER_CONFIG,
    PERFORMANCE_TRACKER_CONFIG,
    STARTING_CAPITAL,
    GLOBAL_RISK_BUDGET,
    KELLY_CONFIG,
    STRATEGY_CONFIG,
)


def test_circuit_breaker_drawdown_is_15_pct():
    """max_drawdown_pct must be 15.0 (not the old 25.0)."""
    assert CIRCUIT_BREAKER_CONFIG["max_drawdown_pct"] == Decimal("15.0"), (
        f"Got {CIRCUIT_BREAKER_CONFIG['max_drawdown_pct']} — expected 15.0. "
        "The 25% bug allowed a 10% capital bleed between soft-halt and hard-stop."
    )


def test_consecutive_losses_is_4():
    """max_consecutive_losses must be 4 (tightened from 5)."""
    assert CIRCUIT_BREAKER_CONFIG["max_consecutive_losses"] == 4


def test_performance_tracker_halt_is_15_pct():
    """Soft halt threshold must be 15%."""
    assert PERFORMANCE_TRACKER_CONFIG["max_drawdown_halt"] == Decimal("0.15")


def test_drawdown_stops_are_aligned():
    """
    CRITICAL INVARIANT: circuit breaker hard-stop and performance tracker
    soft-halt must reference the same drawdown level.

    circuit_breaker pct / 100 == performance_tracker fraction
    """
    cb_fraction = CIRCUIT_BREAKER_CONFIG["max_drawdown_pct"] / Decimal("100")
    pt_fraction = PERFORMANCE_TRACKER_CONFIG["max_drawdown_halt"]
    assert cb_fraction == pt_fraction, (
        f"MISMATCH: circuit breaker={cb_fraction}, perf tracker={pt_fraction}. "
        "Both stops must reference the same drawdown level or capital bleeds between them."
    )


def test_starting_capital_is_positive():
    """STARTING_CAPITAL must be set to a positive value."""
    assert STARTING_CAPITAL > Decimal("0"), "STARTING_CAPITAL is zero or negative!"


def test_global_exposure_not_over_75_pct():
    """Never deploy more than 75% of capital simultaneously."""
    assert GLOBAL_RISK_BUDGET["max_exposure_pct"] <= Decimal("0.75"), (
        f"max_exposure_pct={GLOBAL_RISK_BUDGET['max_exposure_pct']} — dangerously high!"
    )


def test_min_edge_is_positive():
    """Kelly min_edge_required must be positive."""
    assert KELLY_CONFIG["min_edge_required"] > Decimal("0")


def test_max_bet_pct_under_20():
    """Single bet can never exceed 20% of capital."""
    assert KELLY_CONFIG["max_bet_pct"] < Decimal("20.0")


def test_max_daily_loss_is_positive():
    assert CIRCUIT_BREAKER_CONFIG["max_daily_loss"] > Decimal("0")


def test_cooldown_after_trip_is_at_least_1_hour():
    """After circuit breaker trips, bot must cool down at least 1 hour."""
    assert CIRCUIT_BREAKER_CONFIG["cooldown_after_trip_seconds"] >= 3600
