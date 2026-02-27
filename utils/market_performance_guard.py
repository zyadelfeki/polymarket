"""
Market performance guard — auto-blocks markets whose live trading history
shows persistently bad outcomes (high loss rate or large drawdown).

Design decisions:
  - In-process cache (lru_cache on a stable key) so repeated calls within
    a session are O(1) after the first DB read.  Cache intentionally resets
    on process restart so markets that were bad last session get a fresh
    evaluation with new data.
  - Thresholds are conservative: at least 5 settled trades before judging,
    >80% loss rate or <-$200 PnL.  This avoids blocking markets after one
    bad trade while still catching the '1403073' pattern (52 trades, 2% win).
  - No writes — read-only query, safe to call from hot path.
  - Failures are silent warnings; guard returns False (unblocked) on error
    rather than blocking trades due to DB issues.

Usage:
    from utils.market_performance_guard import is_market_blocked_by_performance
    if is_market_blocked_by_performance(market_id):
        # skip this market
"""

import sqlite3
import logging
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------
MIN_TRADES_TO_EVALUATE = 5    # need at least 5 settled trades before judging
MAX_LOSS_RATE = 0.80          # block if losing more than 80% of settled trades
MIN_PNL_THRESHOLD = -200.0    # block if total PnL on this market < -$200

_DB_PATH = "data/trading.db"

# In-process decision cache: market_id -> bool
# Using a plain dict instead of lru_cache so we can inspect it and clear it
# in tests without mock complexity.
_decision_cache: dict[str, bool] = {}


def is_market_blocked_by_performance(market_id: str) -> bool:
    """
    Return True if this market's historical performance warrants auto-blocking.

    Checks SETTLED orders only.  Returns False if there are fewer than
    MIN_TRADES_TO_EVALUATE settled trades, so new markets always get a chance.

    The result is cached per process — resets on restart (intentional: a
    market that was bad last session may have changed character).

    Args:
        market_id: Numeric or hex condition_id string.

    Returns:
        True if the market should be skipped, False otherwise.
    """
    if market_id in _decision_cache:
        return _decision_cache[market_id]

    should_block = _evaluate_market_performance(market_id)
    _decision_cache[market_id] = should_block
    return should_block


def _evaluate_market_performance(market_id: str) -> bool:
    """Query DB and return True if this market meets auto-block criteria."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                             AS trades,
                SUM(CASE WHEN outcome = 'WIN' THEN 1 ELSE 0 END)   AS wins,
                SUM(COALESCE(CAST(pnl AS REAL), 0))                 AS total_pnl
            FROM order_tracking
            WHERE market_id = ?
              AND order_state = 'SETTLED'
            """,
            (market_id,),
        ).fetchone()
        conn.close()

        if not row or row[0] is None or row[0] < MIN_TRADES_TO_EVALUATE:
            return False

        trades, wins, total_pnl = row
        trades = int(trades)
        wins = int(wins or 0)
        total_pnl = float(total_pnl or 0.0)

        loss_rate = 1.0 - (wins / trades) if trades > 0 else 1.0

        if loss_rate > MAX_LOSS_RATE or total_pnl < MIN_PNL_THRESHOLD:
            log.warning(
                "market_auto_blocked_performance",
                extra={
                    "market_id": market_id,
                    "trades": trades,
                    "win_rate": round(1.0 - loss_rate, 3),
                    "total_pnl": round(total_pnl, 2),
                    "loss_rate": round(loss_rate, 3),
                    "threshold_loss_rate": MAX_LOSS_RATE,
                    "threshold_pnl": MIN_PNL_THRESHOLD,
                },
            )
            return True

        return False

    except Exception as exc:
        log.warning(
            "performance_guard_error", extra={"market_id": market_id, "error": str(exc)}
        )
        return False  # fail-safe: do NOT block on DB errors


def clear_performance_cache() -> None:
    """Clear the in-process decision cache (useful in tests or after DB repair)."""
    _decision_cache.clear()
