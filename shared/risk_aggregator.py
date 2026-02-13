from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Tuple


@dataclass
class Position:
    bot: str  # "crypto" or "polymarket"
    asset: str  # "BTC", "ETH", etc.
    direction: str  # "LONG", "SHORT", "NEUTRAL"
    notional_value: Decimal  # USD value
    source: str  # "bitget", "polymarket"


class UnifiedRiskAggregator:
    """
    Aggregates risk across multiple bots.
    """

    def __init__(self, max_btc_exposure_usd: Decimal = Decimal("1000")):
        self.max_btc_exposure_usd = max_btc_exposure_usd
        self.positions: List[Position] = []

    def update_positions(self, positions: List[Position]) -> None:
        """Update current positions from all bots."""
        self.positions = positions

    def get_btc_exposure(self) -> Decimal:
        """Calculate total BTC exposure across all bots."""
        btc_long = sum(
            p.notional_value
            for p in self.positions
            if p.asset == "BTC" and p.direction == "LONG"
        )
        btc_short = sum(
            p.notional_value
            for p in self.positions
            if p.asset == "BTC" and p.direction == "SHORT"
        )
        return btc_long - btc_short  # Net exposure

    def can_open_btc_position(self, size_usd: Decimal, direction: str) -> Tuple[bool, str]:
        """
        Check if we can open new BTC position without exceeding limits.

        Returns: (can_open, reason_if_not)
        """
        current_exposure = self.get_btc_exposure()

        if direction == "LONG":
            new_exposure = current_exposure + size_usd
        else:
            new_exposure = current_exposure - size_usd

        if abs(new_exposure) > self.max_btc_exposure_usd:
            return (
                False,
                f"Would exceed BTC exposure limit: {new_exposure} > {self.max_btc_exposure_usd}",
            )

        return (True, "")
