#!/usr/bin/env python3
"""Strategy health monitoring utilities."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Deque, Tuple


@dataclass
class TradeOutcome:
    win: bool
    roi: Decimal


class StrategyHealthMonitor:
    """Detect when strategy performance degrades."""

    def __init__(
        self,
        strategy_name: str,
        max_trades: int = 100,
        performance_threshold: Decimal = Decimal("0.52"),
        sharpe_threshold: Decimal = Decimal("0.5"),
        min_samples: int = 30,
    ):
        self.strategy_name = strategy_name
        self.recent_trades: Deque[TradeOutcome] = deque(maxlen=max_trades)
        self.performance_threshold = performance_threshold
        self.sharpe_threshold = sharpe_threshold
        self.min_samples = min_samples

    def record_trade(self, win: bool, roi: Decimal) -> None:
        self.recent_trades.append(TradeOutcome(win=win, roi=roi))

    def check_health(self) -> Tuple[bool, str]:
        if len(self.recent_trades) < self.min_samples:
            return True, "Insufficient data"

        wins = sum(1 for trade in self.recent_trades if trade.win)
        win_rate = Decimal(wins) / Decimal(len(self.recent_trades))

        avg_roi = sum((trade.roi for trade in self.recent_trades), Decimal("0")) / Decimal(len(self.recent_trades))
        variance = sum((trade.roi - avg_roi) ** 2 for trade in self.recent_trades) / Decimal(len(self.recent_trades))
        roi_std = variance.sqrt() if variance > 0 else Decimal("0")
        sharpe = avg_roi / roi_std if roi_std > 0 else Decimal("0")

        if win_rate < self.performance_threshold:
            return False, f"Win rate degraded to {win_rate:.1%}"
        if sharpe < self.sharpe_threshold:
            return False, f"Sharpe ratio degraded to {sharpe:.2f}"
        return True, "Healthy"