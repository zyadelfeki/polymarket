"""
Backward-compatibility shim.

This module has been renamed to ``services/api_circuit_breaker.py`` to make its
purpose unambiguous: it is the **per-API fault isolator**, not the system-wide
trading kill-switch (which lives in ``risk/system_circuit_breaker.py``).

Existing consumers that import from ``services.circuit_breaker`` will continue to
work.  Migrate imports to ``services.api_circuit_breaker`` at your convenience.

    from services.api_circuit_breaker import ServiceCircuitBreaker, cb_clob
"""

from services.api_circuit_breaker import (  # noqa: F401, F403
    CircuitState,
    ServiceCircuitBreaker,
    cb_gamma,
    cb_clob,
    cb_binance,
    cb_charlie,
)

__all__ = [
    "CircuitState",
    "ServiceCircuitBreaker",
    "cb_gamma",
    "cb_clob",
    "cb_binance",
    "cb_charlie",
]
