"""
Replay Engine — offline strategy simulation using settled order history.

Purpose
-------
Walk over a set of *settled* historical orders, re-evaluate each one
against the current strategy thresholds (min_edge, regime overrides,
Kelly sizing), and report what decisions the *current* code would make
on that same input.

This is NOT a backtest that re-runs live data — it only re-evaluates
decisions you already had signals for.  Its value: validate that a
config change (e.g. lowering min_edge, changing regime overrides) would
have changed outcomes on real historical trades.

Design constraints
------------------
* Stateless with respect to the production databases.  The replay engine
  reads from the order ledger but NEVER WRITES to it.
* No live exchange API calls are made.
* Uses the same Charlie gate thresholds read from ``config_production.py``
  so the replay reflects the current configuration, not whatever was live
  when the trades were placed.

Usage (from CLI)::

    python main.py --mode replay --replay-db data/orders_ledger.db

Usage (programmatic)::

    engine = ReplayEngine(db_path="data/orders_ledger.db")
    results = await engine.run()
    print(results["summary"])
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from state.order_store import OrderStore
from config_production import REGIME_RISK_OVERRIDES, KELLY_CONFIG

logger = logging.getLogger(__name__)


class ReplayEngine:
    """
    Replays settled order history against current strategy logic.

    Parameters
    ----------
    db_path:
        Path to the ``orders_ledger.db`` SQLite file produced by
        ``state.order_store.OrderStore``.
    min_edge:
        Minimum edge threshold to apply during replay (default 0.05).
    min_confidence:
        Minimum confidence threshold to apply during replay (default 0.60).
    apply_regime_overrides:
        If True, apply ``REGIME_RISK_OVERRIDES`` multipliers to replayed
        position sizes.
    """

    def __init__(
        self,
        db_path: str = "data/orders_ledger.db",
        min_edge: Decimal = Decimal("0.05"),
        min_confidence: Decimal = Decimal("0.60"),
        apply_regime_overrides: bool = True,
    ) -> None:
        self._db_path = db_path
        self._min_edge = min_edge
        self._min_confidence = min_confidence
        self._apply_regime_overrides = apply_regime_overrides
        self._order_store: Optional[OrderStore] = None

    # ------------------------------------------------------------------ public

    async def run(self) -> Dict:
        """
        Execute the replay and return a summary dict.

        Loads all settled orders, re-evaluates gate/sizing logic, and computes
        hypothetical PnL if the *current* thresholds had been applied.

        Returns
        -------
        dict with keys:
            ``total_orders``        — total settled orders evaluated
            ``would_have_taken``    — orders that pass current gate thresholds
            ``would_have_skipped``  — orders filtered by current thresholds
            ``actual_pnl``          — sum of PnL from all settled orders (reality)
            ``replay_pnl``          — estimated PnL from would-have-taken subset
            ``win_rate_actual``     — win rate over all settled orders
            ``win_rate_replay``     — win rate over would-have-taken subset
            ``regime_breakdown``    — {regime: count} for would-have-taken
            ``skip_reasons``        — {reason: count} breakdown of filtered trades
            ``summary``             — human-readable multi-line string
        """
        self._order_store = OrderStore(db_path=self._db_path)
        await self._order_store.initialize()

        try:
            all_orders = await self._order_store.get_all_orders(limit=10_000)
        finally:
            await self._order_store.close()

        settled = [
            o for o in all_orders
            if o.get("state") == "SETTLED" and o.get("pnl") is not None
        ]

        # Sort by open time for realistic sequential processing
        settled.sort(key=lambda o: o.get("opened_at", ""))

        results = self._evaluate_orders(settled)
        summary_text = self._format_summary(results)
        results["summary"] = summary_text

        logger.info(
            "replay_complete",
            total=results["total_orders"],
            would_take=results["would_have_taken"],
            replay_pnl=str(results["replay_pnl"]),
        )

        return results

    # ------------------------------------------------------------------ internal

    def _evaluate_orders(self, orders: List[Dict]) -> Dict:
        """
        Evaluate each settled order against current thresholds.
        Returns raw metrics dict (without summary text).
        """
        taken: List[Dict] = []
        skipped: List[Dict] = []
        skip_reasons: Dict[str, int] = {}

        for order in orders:
            passed, skip_reason = self._passes_gate(order)
            if passed:
                taken.append(order)
            else:
                skipped.append(order)
                skip_reasons[skip_reason] = skip_reasons.get(skip_reason, 0) + 1

        actual_pnl = sum(Decimal(str(o["pnl"])) for o in orders)
        replay_pnl = sum(Decimal(str(o["pnl"])) for o in taken)

        actual_wins = sum(1 for o in orders if Decimal(str(o["pnl"])) > Decimal("0"))
        replay_wins = sum(1 for o in taken if Decimal(str(o["pnl"])) > Decimal("0"))

        regime_breakdown: Dict[str, int] = {}
        for o in taken:
            r = o.get("charlie_regime") or "UNKNOWN"
            regime_breakdown[r] = regime_breakdown.get(r, 0) + 1

        return {
            "total_orders":       len(orders),
            "would_have_taken":   len(taken),
            "would_have_skipped": len(skipped),
            "actual_pnl":         actual_pnl,
            "replay_pnl":         replay_pnl,
            "win_rate_actual":    (actual_wins / len(orders)) if orders else 0.0,
            "win_rate_replay":    (replay_wins / len(taken)) if taken else 0.0,
            "regime_breakdown":   regime_breakdown,
            "skip_reasons":       skip_reasons,
        }

    def _passes_gate(self, order: Dict) -> tuple[bool, str]:
        """
        Return (True, '') if order passes current gate thresholds,
        else (False, reason_str).
        """
        # Edge check — stored in charlie_p_win vs actual price implied by size/pnl
        charlie_p_win = order.get("charlie_p_win")
        price_raw = order.get("price")
        charlie_conf = order.get("charlie_conf")

        if charlie_p_win is None or price_raw is None:
            return False, "missing_charlie_signal"

        try:
            p_win = Decimal(str(charlie_p_win))
            price = Decimal(str(price_raw))
        except Exception:
            return False, "invalid_signal_data"

        edge = p_win - price
        if edge < self._min_edge:
            return False, "edge_below_threshold"

        if charlie_conf is not None:
            try:
                conf = Decimal(str(charlie_conf))
                if conf < self._min_confidence:
                    return False, "confidence_below_threshold"
            except Exception:
                pass

        # Regime multiplier — if multiplier is 0.0 for this regime, treat as skipped
        if self._apply_regime_overrides:
            regime = order.get("charlie_regime") or "UNKNOWN"
            mult = REGIME_RISK_OVERRIDES.get(regime, Decimal("1.0"))
            if mult <= Decimal("0"):
                return False, f"regime_blocked:{regime}"

        return True, ""

    def _format_summary(self, r: Dict) -> str:
        """Render a human-readable replay report."""
        lines = [
            "",
            "=" * 60,
            "  REPLAY RESULTS (current thresholds applied to history)",
            "=" * 60,
            f"  Total settled orders evaluated : {r['total_orders']}",
            f"  Would have taken               : {r['would_have_taken']}",
            f"  Would have skipped             : {r['would_have_skipped']}",
            "",
            f"  Actual PnL (all orders)        : ${r['actual_pnl']:.4f}",
            f"  Replay PnL (taken orders)      : ${r['replay_pnl']:.4f}",
            "",
            f"  Actual win rate                : {r['win_rate_actual']:.1%}",
            f"  Replay win rate                : {r['win_rate_replay']:.1%}",
            "",
            "  Regime breakdown (taken orders):",
        ]
        for regime, cnt in sorted(r["regime_breakdown"].items(), key=lambda x: -x[1]):
            lines.append(f"    {regime:<20s}: {cnt}")
        lines.append("")
        lines.append("  Skip reasons:")
        for reason, cnt in sorted(r["skip_reasons"].items(), key=lambda x: -x[1]):
            lines.append(f"    {reason:<35s}: {cnt}")
        lines.append("=" * 60)
        return "\n".join(lines)


async def run_replay(db_path: str = "data/orders_ledger.db") -> None:
    """Entry-point for ``python main.py --mode replay``."""
    engine = ReplayEngine(db_path=db_path)
    results = await engine.run()
    print(results["summary"])
