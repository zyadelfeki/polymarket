from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, timezone
import asyncio
import logging
from typing import Optional
from config.settings import settings
from services.alert_service import AlertService

logger = logging.getLogger(__name__)

_TWO_DP = Decimal("0.01")
_ONE_DP = Decimal("0.1")


class CircuitBreaker:
    def __init__(self, initial_capital: Decimal, alert_service: Optional[AlertService] = None):
        self.initial_capital = Decimal(str(initial_capital))
        self.current_capital = Decimal(str(initial_capital))
        self.peak_capital = Decimal(str(initial_capital))

        self.daily_start_capital = Decimal(str(initial_capital))
        self.daily_reset_time = datetime.now(timezone.utc)

        self.consecutive_losses = 0
        self.trades_today = 0

        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None

        self.alert_service = alert_service or AlertService()

    def update_capital(self, new_capital: Decimal):
        self.current_capital = Decimal(str(new_capital))

        if self.current_capital > self.peak_capital:
            self.peak_capital = self.current_capital

        if (datetime.now(timezone.utc) - self.daily_reset_time) > timedelta(days=1):
            self._reset_daily()

    def record_trade(self, profit: Decimal, win: bool):
        self.trades_today += 1

        if win:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

        self.update_capital(self.current_capital + Decimal(str(profit)))

        self._check_circuit_breaker()

    def _check_circuit_breaker(self):
        if not settings.CIRCUIT_BREAKER_ENABLED:
            return

        # All comparisons in Decimal — no float coercion
        current_drawdown: Decimal = self.get_current_drawdown()
        max_drawdown = Decimal(str(settings.MAX_DRAWDOWN_PCT))
        if current_drawdown >= max_drawdown:
            self._trigger_breaker(
                f"Max drawdown exceeded: {str(current_drawdown.quantize(_ONE_DP))}%",
                hours=24,
            )
            return

        if self.daily_start_capital > Decimal("0"):
            daily_loss_pct: Decimal = (
                (self.current_capital - self.daily_start_capital)
                / self.daily_start_capital
            ) * Decimal("100")
        else:
            daily_loss_pct = Decimal("0")

        daily_limit = Decimal(str(settings.DAILY_LOSS_LIMIT_PCT))
        if daily_loss_pct <= -daily_limit:
            self._trigger_breaker(
                f"Daily loss limit hit: {str(daily_loss_pct.quantize(_ONE_DP))}%",
                hours=12,
            )
            return

        if self.consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            self._trigger_breaker(
                f"{self.consecutive_losses} consecutive losses",
                hours=6,
            )
            return

        if self.trades_today >= settings.MAX_DAILY_TRADES:
            self._trigger_breaker(
                f"Daily trade limit reached: {self.trades_today}",
                hours=4,
            )
            return

    def _trigger_breaker(self, reason: str, hours: int):
        if self.breaker_triggered:
            return

        self.breaker_triggered = True
        self.breaker_reason = reason
        self.breaker_until = datetime.now(timezone.utc) + timedelta(hours=hours)

        logger.critical("CIRCUIT BREAKER TRIGGERED: %s", reason)
        logger.critical("Trading paused until: %s", self.breaker_until)

        self._dispatch_alert(
            title="Circuit Breaker Tripped",
            message=f"Reason: {reason}\nResume: {self.breaker_until.isoformat()}"
        )

    def _dispatch_alert(self, title: str, message: str) -> None:
        if not self.alert_service:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.alert_service.send_critical_alert(title, message))
            return
        loop.create_task(self.alert_service.send_critical_alert(title, message))

    def is_trading_allowed(self) -> bool:
        if not self.breaker_triggered:
            return True

        if datetime.now(timezone.utc) >= self.breaker_until:
            self._reset_breaker()
            return True

        return False

    def can_trade(self, current_equity: Decimal) -> bool:
        """Compatibility helper for legacy callers."""
        if current_equity is None or current_equity <= 0:
            logger.warning("Circuit breaker: current equity unavailable or zero")
            return False
        self.update_capital(current_equity)
        self._check_circuit_breaker()
        return self.is_trading_allowed()

    def reset_baseline(self, capital: Decimal) -> None:
        """Reset baseline capital after ledger initialization."""
        self.initial_capital = Decimal(str(capital))
        self.current_capital = Decimal(str(capital))
        self.peak_capital = Decimal(str(capital))
        self.daily_start_capital = Decimal(str(capital))
        self.daily_reset_time = datetime.now(timezone.utc)
        logger.info("Circuit breaker baseline reset: $%s", str(capital))

    def _reset_breaker(self):
        logger.info("Circuit breaker reset. Resuming trading.")
        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None
        self.consecutive_losses = 0

    def _reset_daily(self):
        self.daily_start_capital = self.current_capital
        self.daily_reset_time = datetime.now(timezone.utc)
        self.trades_today = 0
        logger.info(
            "Daily reset. Starting capital: $%s",
            str(self.current_capital.quantize(_TWO_DP)),
        )

    def get_current_drawdown(self) -> Decimal:
        """Return drawdown as Decimal percentage. Never returns float."""
        if self.peak_capital == Decimal("0"):
            return Decimal("0")
        return (
            (self.peak_capital - self.current_capital) / self.peak_capital
        ) * Decimal("100")

    def get_status(self) -> dict:
        drawdown = self.get_current_drawdown()
        return {
            "trading_allowed": self.is_trading_allowed(),
            "breaker_triggered": self.breaker_triggered,
            "breaker_reason": self.breaker_reason,
            "breaker_until": self.breaker_until.isoformat() if self.breaker_until else None,
            # str(Decimal) — lossless, JSON-safe, consistent with trade_executor
            "current_drawdown": str(drawdown.quantize(_ONE_DP)),
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
            "current_capital": str(self.current_capital),
            "peak_capital": str(self.peak_capital),
        }
