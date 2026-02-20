"""Performance Tracker — honest PnL accounting powered by the order ledger.

Reads exclusively from the double-entry ledger (``database/ledger_async.py``)
via ``get_all_tracked_orders()`` to compute real, on-chain-confirmed metrics.
The legacy ``state/order_store.py`` has been removed; all order tracking now
lives in the ``order_tracking`` table of ``AsyncLedger``.

No “would-have-been” estimates.  Only settled orders with stored PnL count
toward win rate, drawdown, or equity-curve calculations.

Public interface
----------------
    tracker = PerformanceTracker(ledger, ledger)
    await tracker.refresh()           # pull latest data

    dd  = tracker.get_current_drawdown()          # Decimal, 0–1
    wr  = tracker.get_rolling_win_rate(30)        # float 0–1 or None
    eq  = tracker.get_equity_curve()              # list[dict]
    snap = tracker.get_summary()                  # dict for logging
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from database.ledger_async import AsyncLedger

logger = logging.getLogger(__name__)


class PerformanceTracker:
    """
    Aggregates realized PnL, equity curve, drawdown, and win rate from the
    persistent order ledger and the double-entry accounting ledger.

    Designed to be called:
      - on startup (after reconcile) to seed the circuit breaker
      - periodically in ``_periodic_check`` for fresh metrics
      - at shutdown for the snapshot log line
    """

    def __init__(
        self,
        order_store: "AsyncLedger",
        ledger: Optional["AsyncLedger"] = None,
        initial_capital: Optional[Decimal] = None,
        model_feedback_callback: Optional[Callable[[bool], None]] = None,
    ) -> None:
        self._store = order_store
        self._ledger = ledger
        self._initial_capital = initial_capital or Decimal("0")

        # ``model_feedback_callback(was_correct: bool)`` is called once per
        # newly-settled trade so the ML ensemble can update accuracy weights.
        # Typically wired in main.py to call ``engine.update_model_performance``
        # for all constituent model names.
        self._model_feedback_callback = model_feedback_callback

        # Cached state (refreshed by ``refresh()``)
        self._settled_orders: List[Dict] = []
        self._current_equity: Decimal = Decimal("0")
        self._peak_equity: Decimal = Decimal("0")
        self._equity_curve: List[Dict] = []  # sorted by closed_at

        # Tracks order_ids that have already triggered model feedback.
        # Prevents re-firing on repeated refresh() calls.
        self._feedback_seen_ids: set = set()

    # ------------------------------------------------------------------ refresh

    async def refresh(self) -> None:
        """
        Pull the latest settled orders and current equity from both stores.
        Must be awaited before reading any metric.
        """
        # Support both AsyncLedger (order_tracking table) and legacy OrderStore.
        # AsyncLedger has get_all_tracked_orders(); OrderStore has get_all_orders().
        if hasattr(self._store, "get_all_tracked_orders"):
            # AsyncLedger path — order_tracking table uses 'order_state' column
            all_orders = await self._store.get_all_tracked_orders(limit=1000)
            self._settled_orders = [
                o for o in all_orders
                if o.get("order_state") == "SETTLED" and o.get("pnl") is not None
            ]
        else:
            # Legacy OrderStore path — uses 'state' column
            all_orders = await self._store.get_all_orders(limit=1000)
            self._settled_orders = [
                o for o in all_orders
                if o.get("state") == "SETTLED" and o.get("pnl") is not None
            ]

        # Dispatch model feedback for newly-settled orders.
        # Each new settlement triggers one call to the registered callback so
        # the ensemble engine can update accuracy weights via EWMA.
        if self._model_feedback_callback is not None:
            for order in self._settled_orders:
                oid = order.get("order_id") or order.get("id")
                if oid and oid not in self._feedback_seen_ids:
                    self._feedback_seen_ids.add(oid)
                    try:
                        pnl = Decimal(str(order["pnl"]))
                        was_correct = pnl > Decimal("0")
                        # Pass the full order dict so the callback can do
                        # per-model attribution via the stored model_votes column.
                        import inspect as _inspect
                        sig = _inspect.signature(self._model_feedback_callback)
                        if len(sig.parameters) >= 2:
                            self._model_feedback_callback(was_correct, order)
                        else:
                            self._model_feedback_callback(was_correct)
                        logger.debug(
                            "model_feedback_dispatched",
                            order_id=oid,
                            was_correct=was_correct,
                            pnl=str(pnl),
                        )
                    except Exception as exc:
                        logger.warning(
                            "model_feedback_dispatch_error",
                            order_id=oid,
                            error=str(exc),
                        )

        # Sort by closed_at for equity-curve calculation
        def _ts(o):
            try:
                return datetime.fromisoformat(o["closed_at"])
            except Exception:
                return datetime.min

        self._settled_orders.sort(key=_ts)

        # Current equity — prefer live ledger; fall back to order-store math
        if self._ledger is not None:
            try:
                eq = await self._ledger.get_equity()
                if eq is not None:
                    self._current_equity = Decimal(str(eq))
            except Exception as exc:
                logger.warning("performance_tracker_ledger_error", error=str(exc))

        if self._current_equity == Decimal("0") and self._initial_capital > Decimal("0"):
            realized = sum(
                Decimal(o["pnl"]) for o in self._settled_orders
            )
            self._current_equity = self._initial_capital + realized

        # Rebuild equity curve
        running = self._initial_capital
        self._equity_curve = []
        for o in self._settled_orders:
            pnl = Decimal(o["pnl"])
            running += pnl
            self._equity_curve.append(
                {
                    "ts": o.get("closed_at"),
                    "equity": running,
                    "pnl": pnl,
                    "market_id": o.get("market_id"),
                    "outcome": o.get("outcome"),
                }
            )

        # Update peak equity
        for point in self._equity_curve:
            if point["equity"] > self._peak_equity:
                self._peak_equity = point["equity"]

        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

    # ------------------------------------------------------------------ metrics

    def get_current_drawdown(self) -> Decimal:
        """
        Current drawdown as a fraction of peak equity, range [0, 1].

        Returns 0 if there is no history or equity is at a peak.
        """
        if self._peak_equity <= Decimal("0"):
            return Decimal("0")
        if self._current_equity >= self._peak_equity:
            return Decimal("0")
        dd = (self._peak_equity - self._current_equity) / self._peak_equity
        return dd.quantize(Decimal("0.0001"))

    def get_rolling_win_rate(self, window_trades: int = 20) -> Optional[float]:
        """
        Win rate over the last ``window_trades`` settled trades.

        Returns None if fewer than ``window_trades`` settled trades exist.
        We intentionally require the window to be full so that circuit-breaker
        decisions are based on a statistically meaningful sample.
        """
        if len(self._settled_orders) < window_trades:
            return None
        recent = self._settled_orders[-window_trades:]
        wins = sum(1 for o in recent if Decimal(o["pnl"]) > Decimal("0"))
        return wins / window_trades

    def get_realized_pnl(self) -> Decimal:
        """Total realized PnL across all settled orders."""
        return sum(
            (Decimal(o["pnl"]) for o in self._settled_orders),
            Decimal("0"),
        )

    def get_max_drawdown(self) -> Decimal:
        """
        Maximum drawdown across the entire history, range [0, 1].
        Computed by scanning the equity curve.
        """
        peak = self._initial_capital
        max_dd = Decimal("0")
        for point in self._equity_curve:
            eq = point["equity"]
            if eq > peak:
                peak = eq
            if peak > Decimal("0"):
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd.quantize(Decimal("0.0001"))

    def get_equity_curve(self) -> List[Dict]:
        """Ordered list of equity snapshots (one per settled trade)."""
        return list(self._equity_curve)

    def get_summary(self) -> Dict:
        """Full metrics dict — suitable for structured logging."""
        return {
            "current_equity": str(self._current_equity),
            "peak_equity": str(self._peak_equity),
            "realized_pnl": str(self.get_realized_pnl()),
            "current_drawdown_pct": str(
                self.get_current_drawdown() * Decimal("100")
            ),
            "max_drawdown_pct": str(
                self.get_max_drawdown() * Decimal("100")
            ),
            "settled_trades": len(self._settled_orders),
            "rolling_win_rate_20": self.get_rolling_win_rate(20),
            "rolling_win_rate_50": self.get_rolling_win_rate(50),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
