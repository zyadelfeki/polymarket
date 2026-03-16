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


@dataclass
class PaperPosition:
    market_id: str
    side: str           # "YES" or "NO"
    size: Decimal       # USDC staked
    entry_price: Decimal
    kelly_fraction: Decimal
    edge: Decimal
    confidence: Decimal
    question: str
    opened_at: float = field(default_factory=time.monotonic)
    settled: bool = False
    pnl: Optional[Decimal] = None
    settled_at: Optional[float] = None


class PaperOrderBook:
    """
    Enforces one open position per (market_id, side) pair.
    All amounts are in USDC Decimal.
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
    ) -> bool:
        """
        Record a new paper order.  Returns True if recorded, False if duplicate.
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

        key = (market_id, side)
        self._positions[key] = PaperPosition(
            market_id=market_id,
            side=side,
            size=size,
            entry_price=entry_price,
            kelly_fraction=kelly_fraction,
            edge=edge,
            confidence=confidence,
            question=question[:120],
        )
        self._total_staked += size
        self._orders_placed += 1

        try:
            logger.info(
                "paper_order_placed",
                market_id=market_id,
                side=side,
                size=str(size),
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
                # Payout = size / entry_price * 1 USDC per share, minus stake
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
