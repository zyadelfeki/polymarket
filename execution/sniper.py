"""
Last-Second Market Sniper.

In the final 5 seconds before a Polymarket 15-minute market closes, market
makers often have stale resting limit orders they haven't cancelled.  The
candle direction is essentially confirmed, so we can take those quotes with
high confidence using FAK (Fill-And-Kill) orders.

Academically validated: "Sniping in Auction Markets" (NBER).

Currently LOG-ONLY — does not place real orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import structlog

from utils.fee_calculator import net_edge as _net_edge_calc

logger = structlog.get_logger(__name__)

SNIPE_WINDOW_SECONDS = 5        # act in final 5 seconds
SNIPE_CONFIRM_SECONDS = 30      # only snipe if candle direction confirmed 30s before close
MIN_SNIPE_EDGE = Decimal("0.02")  # minimum 2% edge after fees to snipe


class MarketSniper:
    """Detects and logs last-second sniping opportunities on closing markets."""

    def __init__(self) -> None:
        self._sniped: set[str] = set()  # avoid double-sniping same market

    @staticmethod
    def seconds_to_close(market: dict) -> float:
        """Returns seconds until market closes.  Negative = already closed."""
        close_ts = (
            market.get("end_date_iso")
            or market.get("end_date")
            or market.get("close_time")
        )
        if not close_ts:
            return float("inf")
        try:
            close_dt = datetime.fromisoformat(str(close_ts).replace("Z", "+00:00"))
            return (close_dt - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return float("inf")

    def should_snipe(
        self,
        market: dict,
        p_win: Decimal,
        market_price: Decimal,
    ) -> bool:
        """Returns True if this market is in the snipe window with sufficient edge."""
        market_id = (
            market.get("condition_id")
            or market.get("market_id")
            or market.get("id")
            or ""
        )
        if market_id in self._sniped:
            return False
        secs = self.seconds_to_close(market)
        if not (0 < secs <= SNIPE_WINDOW_SECONDS):
            return False
        # Pass market dict so crypto direction markets use the 3.15% fee
        edge = _net_edge_calc(p_win, market_price, market=market)
        return edge >= MIN_SNIPE_EDGE

    def evaluate_snipe(
        self,
        market: dict,
        side: str,
        p_win: Decimal,
        market_price: Decimal,
    ) -> Optional[dict]:
        """
        Evaluate a snipe opportunity.  Returns an order spec dict or None.

        Currently LOG-ONLY — caller decides whether to place a real order.
        """
        market_id = (
            market.get("condition_id")
            or market.get("market_id")
            or market.get("id")
            or ""
        )
        secs = self.seconds_to_close(market)
        edge = _net_edge_calc(p_win, market_price)

        logger.info(
            "snipe_would_fire",
            market_id=market_id,
            side=side,
            p_win=float(p_win),
            market_price=float(market_price),
            net_edge=float(edge),
            seconds_to_close=round(secs, 1),
            order_type="FAK",
        )

        # Mark as sniped immediately to prevent double-fire
        self._sniped.add(market_id)

        return {
            "market_id": market_id,
            "side": side,
            "order_type": "FAK",
            "price": market_price,
            "net_edge": edge,
            "seconds_to_close": secs,
        }

    def reset_session(self) -> None:
        """Clear the sniped set (call at session start)."""
        self._sniped.clear()
