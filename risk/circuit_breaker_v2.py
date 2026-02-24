"""
Backward-compatibility shim.

This file has been renamed to ``risk/system_circuit_breaker.py`` to make its
purpose unambiguous: it is the system-wide trading kill-switch, not the
per-API fault isolator (which lives in ``services/api_circuit_breaker.py``).

Existing consumers that import from ``risk.circuit_breaker_v2`` will continue to
work.  Migrate imports to ``risk.system_circuit_breaker`` at your convenience.
"""
from risk.system_circuit_breaker import (  # noqa: F401, F403
    CircuitBreakerV2,
    CircuitState,
    CircuitEvent,
    TripReason,
)

__all__ = ["CircuitBreakerV2", "CircuitState", "CircuitEvent", "TripReason"]
