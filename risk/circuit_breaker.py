from decimal import Decimal
from datetime import datetime, timedelta, timezone
import asyncio
import inspect
import logging
from typing import Optional
from config.settings import settings
from services.alert_service import AlertService

logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, initial_capital: Decimal, alert_service: Optional[AlertService] = None):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        self.daily_start_capital = initial_capital
        self.daily_reset_time = datetime.now(timezone.utc)
        
        self.consecutive_losses = 0
        self.trades_today = 0
        
        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None

        self.alert_service = alert_service or AlertService()
    
    def update_capital(self, new_capital: Decimal):
        self.current_capital = new_capital
        
        if new_capital > self.peak_capital:
            self.peak_capital = new_capital
        
        if (datetime.now(timezone.utc) - self.daily_reset_time) > timedelta(days=1):
            self._reset_daily()
    
    def record_trade(self, profit: Decimal, win: bool):
        self.trades_today += 1
        
        if win:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
        
        self.update_capital(self.current_capital + profit)
        
        self._check_circuit_breaker()
    
    def _check_circuit_breaker(self):
        if not settings.CIRCUIT_BREAKER_ENABLED:
            return
        
        current_drawdown = self.get_current_drawdown()
        if current_drawdown >= settings.MAX_DRAWDOWN_PCT:
            self._trigger_breaker(f"Max drawdown exceeded: {current_drawdown:.1f}%", hours=24)
            return
        
        daily_loss_pct = ((self.current_capital - self.daily_start_capital) / self.daily_start_capital) * Decimal("100")
        if daily_loss_pct <= -Decimal(str(settings.DAILY_LOSS_LIMIT_PCT)):
            self._trigger_breaker(f"Daily loss limit hit: {daily_loss_pct:.1f}%", hours=12)
            return
        
        if self.consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            self._trigger_breaker(f"{self.consecutive_losses} consecutive losses", hours=6)
            return
        
        if self.trades_today >= settings.MAX_DAILY_TRADES:
            self._trigger_breaker(f"Daily trade limit reached: {self.trades_today}", hours=4)
            return
    
    def _trigger_breaker(self, reason: str, hours: int):
        if self.breaker_triggered:
            return
        
        self.breaker_triggered = True
        self.breaker_reason = reason
        self.breaker_until = datetime.now(timezone.utc) + timedelta(hours=hours)
        
        logger.critical(f"CIRCUIT BREAKER TRIGGERED: {reason}")
        logger.critical(f"Trading paused until: {self.breaker_until}")

        self._dispatch_alert(
            title="Circuit Breaker Tripped",
            message=f"Reason: {reason}\nResume: {self.breaker_until.isoformat()}"
        )

    def _dispatch_alert(self, title: str, message: str) -> None:
        """Fire-and-forget alert dispatch that is safe in both sync and async
        contexts, and also safe when alert_service methods are plain mocks
        (not coroutines) — e.g. during unit tests."""
        if not self.alert_service:
            return
        try:
            coro = self.alert_service.send_critical_alert(title, message)
        except Exception:
            return
        # If the mock/stub returned a non-coroutine (e.g. MagicMock), bail out.
        if not inspect.isawaitable(coro):
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # No running loop — we are in a sync context, run it directly.
            asyncio.run(coro)
    
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
        self.initial_capital = capital
        self.current_capital = capital
        self.peak_capital = capital
        self.daily_start_capital = capital
        self.daily_reset_time = datetime.now(timezone.utc)
        logger.info(f"Circuit breaker baseline reset: ${capital}")
    
    def _reset_breaker(self):
        logger.info(f"Circuit breaker reset. Resuming trading.")
        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None
        self.consecutive_losses = 0
    
    def _reset_daily(self):
        self.daily_start_capital = self.current_capital
        self.daily_reset_time = datetime.now(timezone.utc)
        self.trades_today = 0
        logger.info(f"Daily reset. Starting capital: ${self.current_capital:.2f}")
    
    def get_current_drawdown(self) -> Decimal:
        """Returns drawdown as a Decimal percentage (0-100)."""
        if self.peak_capital == 0:
            return Decimal("0")
        return ((self.peak_capital - self.current_capital) / self.peak_capital) * Decimal("100")
    
    def get_status(self) -> dict:
        return {
            "trading_allowed": self.is_trading_allowed(),
            "breaker_triggered": self.breaker_triggered,
            "breaker_reason": self.breaker_reason,
            "breaker_until": self.breaker_until.isoformat() if self.breaker_until else None,
            "current_drawdown": str(self.get_current_drawdown()),
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
            "current_capital": str(self.current_capital),
            "peak_capital": str(self.peak_capital),
        }
