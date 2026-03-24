"""
In-memory paper order book.

Tracks paper positions per (market_id, side) pair and enforces a single
open position per pair.  This is the deduplication guard that prevents the
scan loop from filing 30+ orders for the same market in one cycle.

Design
------
- One entry per (market_id, side).  A second approval for the same pair is
  silently rejected with reason="duplicate_position_open".
- ``settle(market_id, resolved_yes)`` closes all positions for a market and
  computes PnL.  Call this when you receive a resolution event.
- ``summary()`` returns serializable stats for the monitoring dashboard.
- Thread-safe within asyncio (single-threaded event loop); no locks needed.

Accounting invariant
--------------------
``TradeExecutor.execute_trade`` clamps the on-chain bet to MIN_BET_SIZE
($1.00) when Kelly sizing falls below the Polymarket floor.  This module
applies the SAME clamp so that PaperPosition.size, _total_staked, and
settle() PnL always agree with what was actually debited from the bankroll.
Without this clamp the bankroll loses $1.00 but the order book records
~$0.30, and neutral-resolution refunds only return $0.30, creating
permanent, compounding leakage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

try:
    import structlog
    logger = structlog.get_logger(__name__)
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# Import the authoritative minimum from trade_executor so both modules share
# the same constant.  If the import ever fails (e.g. in an isolated unit-test
# environment without the full package), fall back to the hard-coded value.
try:
    from execution.trade_executor import MIN_BET_SIZE as _EXECUTOR_MIN_BET_SIZE
except Exception:  # pragma: no cover
    _EXECUTOR_MIN_BET_SIZE = Decimal("1.00")

MIN_BET_SIZE: Decimal = _EXECUTOR_MIN_BET_SIZE


@dataclass
class PaperPosition:
    market_id: str
    side: str           # "YES" or "NO"
    size: Decimal       # USDC staked (post-clamp — matches what bankroll debited)
    entry_price: Decimal
    kelly_fraction: Decimal
    edge: Decimal
    confidence: Decimal
    question: str
    end_date: str = ""  # ISO-8601 string; used by settlement loop
    opened_at: float = field(default_factory=time.monotonic)
    settled: bool = False
    pnl: Optional[Decimal] = None
    settled_at: Optional[float] = None


class PaperOrderBook:
    """
    Enforces one open position per (market_id, side) pair.
    All amounts are in USDC Decimal.

    Size invariant
    --------------
    ``record_order`` receives the raw Kelly size from the caller.  Before
    storing it, we apply the same MIN_BET_SIZE clamp that TradeExecutor uses
    so PaperPosition.size always reflects the true on-chain debit.  The
    caller's raw size is NOT stored — only the effective (clamped) size is.
    """

    def __init__(self) -> None:
        self._positions: Dict[Tuple[str, str], PaperPosition] = {}
        self._settled: List[PaperPosition] = []
        self._total_staked: Decimal = Decimal("0")
        self._total_pnl: Decimal = Decimal("0")
        self._orders_placed: int = 0
        self._orders_rejected_duplicate: int = 0

    def is_duplicate(self, market_id: str, side: str) -> bool:
        key = (market_id, side.upper())
        pos = self._positions.get(key)
        return pos is not None and not pos.settled

    def get_open_positions(self) -> List[PaperPosition]:
        """Return all unsettled positions."""
        return [p for p in self._positions.values() if not p.settled]

    def record_order(
        self,
        market_id: str,
        side: str,
        size: Decimal,
        entry_price: Decimal,
        kelly_fraction: Decimal,
        edge: Decimal,
        confidence: Decimal,
        question: str,
        end_date: str = "",
    ) -> bool:
        """
        Record a new paper order.  Returns True if recorded, False if duplicate.

        ``size`` is the raw Kelly size from the caller.  The effective size
        stored and debited is ``max(size, MIN_BET_SIZE)`` when ``size`` is
        positive but below the Polymarket floor, mirroring the clamp applied
        by ``TradeExecutor.execute_trade``.
        """
        side = side.upper()
        if self.is_duplicate(market_id, side):
            self._orders_rejected_duplicate += 1
            try:
                logger.warning(
                    "paper_order_duplicate_rejected",
                    market_id=market_id,
                    side=side,
                    question=question[:80],
                )
            except Exception:
                pass
            return False

        # Apply the same minimum-bet clamp as TradeExecutor so all three
        # accounting systems (executor, bankroll tracker, order book) agree
        # on the true USDC amount for every paper order.
        effective_size = size
        clamped = False
        if Decimal("0") < size < MIN_BET_SIZE:
            effective_size = MIN_BET_SIZE
            clamped = True

        key = (market_id, side)
        self._positions[key] = PaperPosition(
            market_id=market_id,
            side=side,
            size=effective_size,
            entry_price=entry_price,
            kelly_fraction=kelly_fraction,
            edge=edge,
            confidence=confidence,
            question=question[:120],
            end_date=end_date,
        )
        self._total_staked += effective_size
        self._orders_placed += 1

        try:
            logger.info(
                "paper_order_placed",
                market_id=market_id,
                side=side,
                size=str(effective_size),
                raw_kelly_size=str(size) if clamped else None,
                clamped_to_minimum=clamped,
                entry_price=str(entry_price),
                edge=str(edge),
                confidence=str(confidence),
                question=question[:80],
                total_open_positions=self.open_position_count,
            )
        except Exception:
            pass
        return True

    def settle(self, market_id: str, resolved_yes: bool) -> List[PaperPosition]:
        """
        Settle all open positions for a market.  resolved_yes=True means YES wins.
        Returns all affected positions with pnl populated.
        """
        settled_positions = []
        for side in ("YES", "NO"):
            key = (market_id, side)
            pos = self._positions.get(key)
            if pos is None or pos.settled:
                continue

            won = (side == "YES" and resolved_yes) or (side == "NO" and not resolved_yes)
            if won:
                payout = pos.size / pos.entry_price if pos.entry_price > 0 else Decimal("0")
                pos.pnl = payout - pos.size
            else:
                pos.pnl = -pos.size

            pos.settled = True
            pos.settled_at = time.monotonic()
            self._total_pnl += pos.pnl
            self._settled.append(pos)
            settled_positions.append(pos)

            try:
                logger.info(
                    "paper_position_settled",
                    market_id=market_id,
                    side=side,
                    won=won,
                    size=str(pos.size),
                    pnl=str(pos.pnl),
                    question=pos.question[:80],
                )
            except Exception:
                pass

        return settled_positions

    def settle_open_positions(
        self,
        market_id: str,
        *,
        outcome: str = "neutral",
    ) -> List[PaperPosition]:
        """
        Expire open positions for a market that didn't resolve cleanly.

        outcome="neutral"  → full stake refunded (pnl = 0)
        outcome="win"      → treat as won
        outcome="loss"     → treat as lost

        This is distinct from ``settle`` (binary YES/NO resolution).  It is
        called by the settlement loop when a position ages out or the market
        closes without a clean resolution signal.
        """
        settled_positions = []
        for side in ("YES", "NO"):
            key = (market_id, side)
            pos = self._positions.get(key)
            if pos is None or pos.settled:
                continue

            if outcome == "neutral":
                pos.pnl = Decimal("0")
            elif outcome == "win":
                payout = pos.size / pos.entry_price if pos.entry_price > 0 else Decimal("0")
                pos.pnl = payout - pos.size
            else:  # loss
                pos.pnl = -pos.size

            pos.settled = True
            pos.settled_at = time.monotonic()
            self._total_pnl += pos.pnl
            self._settled.append(pos)
            settled_positions.append(pos)

            try:
                logger.info(
                    "paper_position_expired",
                    market_id=market_id,
                    side=side,
                    outcome=outcome,
                    size=str(pos.size),
                    pnl=str(pos.pnl),
                    question=pos.question[:80],
                )
            except Exception:
                pass

        return settled_positions

    def remove_position(self, market_id: str, side: str) -> None:
        self._positions.pop((market_id, side.upper()), None)

    @property
    def open_position_count(self) -> int:
        return sum(1 for p in self._positions.values() if not p.settled)

    @property
    def open_positions(self) -> List[PaperPosition]:
        return [p for p in self._positions.values() if not p.settled]

    def summary(self) -> dict:
        return {
            "orders_placed": self._orders_placed,
            "orders_rejected_duplicate": self._orders_rejected_duplicate,
            "open_positions": self.open_position_count,
            "settled_positions": len(self._settled),
            "total_staked_usdc": str(self._total_staked),
            "total_pnl_usdc": str(self._total_pnl),
        }
