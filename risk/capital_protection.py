"""
ONE MISTAKE = ACCOUNT WIPEOUT
These safeguards are NON-NEGOTIABLE
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass
class CircuitBreaker:
    def __init__(self, starting_capital: Decimal):
        self.starting_capital = Decimal(str(starting_capital))
        self.current_capital = Decimal(str(starting_capital))
        self.daily_loss_limit = self.starting_capital * Decimal("0.15")
        self.consecutive_loss_limit = 3
        self.consecutive_losses = 0
        self.max_trade_pct = Decimal("0.20")
        self.max_total_drawdown_pct = Decimal("0.20")
        self._daily_reset_date = datetime.now(timezone.utc).date()
        self._daily_start_capital = self.starting_capital

    def check_before_trade(self, proposed_bet_size: Decimal, current_capital: Decimal) -> bool:
        self._roll_daily(current_capital)

        daily_loss = self._daily_start_capital - self.current_capital
        if daily_loss >= self.daily_loss_limit:
            raise Exception("CIRCUIT BREAKER: 15% daily loss limit hit")

        if self.consecutive_losses >= self.consecutive_loss_limit:
            raise Exception("CIRCUIT BREAKER: 3 consecutive losses - STOP TRADING")

        if proposed_bet_size > (self.current_capital * self.max_trade_pct):
            raise Exception("CIRCUIT BREAKER: Single trade > 20% of capital")

        if self.current_capital < (self.starting_capital * (Decimal("1") - self.max_total_drawdown_pct)):
            raise Exception("CIRCUIT BREAKER: 20% total drawdown - STOP ALL TRADING")

        return True

    def update_capital(self, new_capital: Decimal) -> None:
        self.current_capital = Decimal(str(new_capital))
        self._roll_daily(self.current_capital)

    def record_trade_result(self, pnl: Decimal) -> None:
        pnl = Decimal(str(pnl))
        self.current_capital += pnl

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        self._roll_daily(self.current_capital)

    def _roll_daily(self, current_capital: Decimal) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._daily_reset_date:
            self._daily_reset_date = today
            self._daily_start_capital = Decimal(str(current_capital))
            self.consecutive_losses = 0
