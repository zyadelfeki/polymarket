"""replay — offline order-history replay for strategy back-testing.

Quick start::

    python main.py --mode replay --replay-db data/orders_ledger.db

The replay engine loads settled orders from the order ledger and
replays them through the configured strategy logic to measure
what PnL and metrics the strategy would have produced if it had
been live — useful for validating config changes or new thresholds.
"""

from .engine import ReplayEngine

__all__ = ["ReplayEngine"]
