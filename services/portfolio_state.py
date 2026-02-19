"""
Portfolio state snapshot for intra-loop risk checks.

``PortfolioState`` wraps the ledger's position data into a fast, cached
in-memory view so that the hot trading loop can ask questions like:

  - "How much USDC is currently exposed across all open positions?"
  - "How much is exposed in market X specifically?"
  - "Would this trade push total exposure past the global risk budget?"

without hitting SQLite on every candidate opportunity.

The snapshot is **not** real-time — call ``refresh()`` once per loop
iteration (or after every filled order).  Stale reads between refreshes
are acceptable; the ledger is the source of truth.

Usage::

    state = PortfolioState(ledger=ledger, equity=Decimal("200.00"))
    await state.refresh()

    if not state.within_global_budget(proposed_size=Decimal("5.00")):
        return  # already too exposed
    if state.exposure_for_market("0xabc...") > Decimal("10.00"):
        return  # over-concentrated in this market
"""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PortfolioState:
    """
    Cached snapshot of open-position exposure, refreshed on demand.

    Parameters
    ----------
    ledger:
        ``AsyncLedger`` instance (must have ``get_positions_by_market()``).
    equity:
        Current equity as a Decimal.  Updated from outside whenever the
        caller re-reads equity from the ledger (usually once per loop).
    global_max_exposure_pct:
        Maximum fraction of equity that may be deployed at one time across
        ALL open positions.  Defaults to 0.50 (50%).  Set via
        ``config_production.GLOBAL_RISK_BUDGET["max_exposure_pct"]``.
    max_per_market_pct:
        Maximum fraction of equity allowed in a single market.
        Defaults to 0.10 (10%).
    stale_after_seconds:
        ``refresh()`` is a no-op when the snapshot is younger than this
        threshold — prevents stampeding the DB on every single opportunity
        scan.  The trading loop should call ``refresh()`` unconditionally;
        this guard handles the rate-limiting internally.
    """

    def __init__(
        self,
        ledger,
        equity: Decimal = Decimal("0"),
        global_max_exposure_pct: float = 0.50,
        max_per_market_pct: float = 0.10,
        stale_after_seconds: float = 5.0,
    ) -> None:
        self._ledger = ledger
        self.equity: Decimal = equity

        self._global_max_exposure_pct = Decimal(str(global_max_exposure_pct))
        self._max_per_market_pct = Decimal(str(max_per_market_pct))
        self._stale_after = stale_after_seconds

        # Snapshot data populated by refresh()
        self._positions: List[Dict] = []
        self._by_market: Dict[str, Decimal] = {}  # market_id → exposure (qty * avg_price)
        self._total_exposure: Decimal = Decimal("0")
        self._last_refresh_ts: float = 0.0

        # Refresh serialisation — prevent concurrent refreshes from hammering DB
        self._refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------------ public

    async def refresh(self, force: bool = False) -> None:
        """
        Reload position snapshot from the ledger.

        Skips the DB call when the snapshot is fresh (see ``stale_after_seconds``)
        unless ``force=True`` is passed (e.g. right after a filled order).
        """
        now = time.monotonic()
        if not force and (now - self._last_refresh_ts) < self._stale_after:
            return

        async with self._refresh_lock:
            # Re-check after acquiring lock in case another caller just refreshed
            now = time.monotonic()
            if not force and (now - self._last_refresh_ts) < self._stale_after:
                return

            try:
                rows = await self._ledger.get_positions_by_market()
            except Exception as exc:
                logger.warning(
                    "portfolio_state_refresh_failed",
                    error=str(exc),
                )
                return

            self._positions = rows
            by_market: Dict[str, Decimal] = {}
            total = Decimal("0")

            for row in rows:
                qty = Decimal(str(row.get("total_quantity", 0)))
                avg_price = Decimal(str(row.get("avg_entry_price", 0)))
                exposure = qty * avg_price
                by_market[row["market_id"]] = exposure
                total += exposure

            self._by_market = by_market
            self._total_exposure = total
            self._last_refresh_ts = time.monotonic()

            logger.debug(
                "portfolio_state_refreshed",
                markets=len(by_market),
                total_exposure=str(total),
                equity=str(self.equity),
            )

    def update_equity(self, equity: Decimal) -> None:
        """Caller should invoke this whenever equity is re-read from ledger."""
        self.equity = equity

    @property
    def total_exposure(self) -> Decimal:
        """Total USDC currently deployed across all open positions."""
        return self._total_exposure

    def exposure_for_market(self, market_id: str) -> Decimal:
        """USDC exposure for a single market (0 if no open position)."""
        return self._by_market.get(market_id, Decimal("0"))

    def within_global_budget(self, proposed_size: Decimal) -> bool:
        """
        Return True if adding ``proposed_size`` would keep total exposure ≤
        the global cap as a fraction of equity.

        Always returns True when equity is zero (avoid false blocks at startup
        before ledger is populated).
        """
        if self._last_refresh_ts == 0.0:
            # Snapshot has never been loaded — allow the trade but warn so we
            # can detect prolonged cold-start conditions in the logs.
            logger.warning(
                "portfolio_state_cold_start_allowing_order",
                method="within_global_budget",
                proposed_size=str(proposed_size),
            )
            return True
        if self.equity <= Decimal("0"):
            return True
        cap = self.equity * self._global_max_exposure_pct
        return (self._total_exposure + proposed_size) <= cap

    def within_market_budget(self, market_id: str, proposed_size: Decimal) -> bool:
        """
        Return True if adding ``proposed_size`` to this market would keep
        per-market exposure ≤ the per-market fraction of equity.
        """
        if self._last_refresh_ts == 0.0:
            # Snapshot has never been loaded — allow the trade but warn.
            logger.warning(
                "portfolio_state_cold_start_allowing_order",
                method="within_market_budget",
                market_id=market_id,
                proposed_size=str(proposed_size),
            )
            return True
        if self.equity <= Decimal("0"):
            return True
        cap = self.equity * self._max_per_market_pct
        current = self.exposure_for_market(market_id)
        return (current + proposed_size) <= cap

    def all_positions(self) -> List[Dict]:
        """Raw position rows from the last snapshot."""
        return list(self._positions)

    def get_summary(self) -> Dict:
        """Human-readable summary for logging."""
        return {
            "total_exposure": str(self._total_exposure),
            "equity": str(self.equity),
            "utilisation_pct": (
                f"{float(self._total_exposure / self.equity) * 100:.1f}%"
                if self.equity > Decimal("0") else "n/a"
            ),
            "open_markets": len(self._by_market),
            "snapshot_age_s": round(time.monotonic() - self._last_refresh_ts, 1),
        }
