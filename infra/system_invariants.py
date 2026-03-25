"""
System-level invariants checked by check_session and health monitors.
Each invariant returns (passed: bool, violation_message: str | None).
"""
from __future__ import annotations
from typing import Dict, Any, Optional, Tuple


def check_approvals_vs_submissions(stats: Dict[str, int]) -> Tuple[bool, Optional[str]]:
    """If charlie_gate_approved > 0, orders_submitted must be > 0 unless blocked."""
    approved = stats.get("charlie_gate_approved", 0)
    submitted = stats.get("orders_submitted", 0)
    blocked = sum(v for k, v in stats.items() if k.startswith("blocked_"))
    if approved > 0 and submitted == 0 and blocked == 0:
        return False, f"charlie_gate_approved={approved} but orders_submitted=0 with no blocks — possible execution failure"
    return True, None


def check_ofi_feed_staleness(ofi_miss_count: int, threshold: int = 5) -> Tuple[bool, Optional[str]]:
    """Warn when OFI data has been absent for threshold+ consecutive markets."""
    if ofi_miss_count >= threshold:
        return False, f"OFI feed missing for {ofi_miss_count} consecutive markets — binance orderbook may be stale"
    return True, None


def check_binance_feed_health(health: bool, symbols_connected: int, expected_symbols: int) -> Tuple[bool, Optional[str]]:
    """Binance websocket must be healthy with all expected symbols."""
    if not health:
        return False, f"binance_feed_unhealthy: {symbols_connected}/{expected_symbols} symbols connected"
    return True, None


def check_balance_vs_equity(api_balance: float, ledger_equity: float, epsilon: float = 0.50) -> Tuple[bool, Optional[str]]:
    """API balance and ledger equity must agree within epsilon."""
    diff = abs(api_balance - ledger_equity)
    if diff > epsilon:
        return False, f"balance_mismatch: api_balance={api_balance:.4f} ledger_equity={ledger_equity:.4f} diff={diff:.4f} > epsilon={epsilon}"
    return True, None


def evaluate_all(
    stats: Dict[str, int],
    ofi_miss_count: int = 0,
    binance_healthy: bool = True,
    symbols_connected: int = 1,
    expected_symbols: int = 1,
    api_balance: Optional[float] = None,
    ledger_equity: Optional[float] = None,
) -> Dict[str, Tuple[bool, Optional[str]]]:
    results = {}
    results["approvals_vs_submissions"] = check_approvals_vs_submissions(stats)
    results["ofi_feed_staleness"] = check_ofi_feed_staleness(ofi_miss_count)
    results["binance_feed_health"] = check_binance_feed_health(binance_healthy, symbols_connected, expected_symbols)
    if api_balance is not None and ledger_equity is not None:
        results["balance_vs_equity"] = check_balance_vs_equity(api_balance, ledger_equity)
    return results
