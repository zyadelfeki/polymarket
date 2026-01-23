from decimal import Decimal
from datetime import datetime, timedelta
import logging
from config.settings import settings

logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, initial_capital: Decimal):
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.peak_capital = initial_capital
        
        self.daily_start_capital = initial_capital
        self.daily_reset_time = datetime.utcnow()
        
        self.consecutive_losses = 0
        self.trades_today = 0
        
        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None
    
    def update_capital(self, new_capital: Decimal):
        self.current_capital = new_capital
        
        if new_capital > self.peak_capital:
            self.peak_capital = new_capital
        
        if (datetime.utcnow() - self.daily_reset_time) > timedelta(days=1):
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
        self.breaker_until = datetime.utcnow() + timedelta(hours=hours)
        
        logger.critical(f"CIRCUIT BREAKER TRIGGERED: {reason}")
        logger.critical(f"Trading paused until: {self.breaker_until}")
    
    def is_trading_allowed(self) -> bool:
        if not self.breaker_triggered:
            return True
        
        if datetime.utcnow() >= self.breaker_until:
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
        self.daily_reset_time = datetime.utcnow()
        logger.info(f"Circuit breaker baseline reset: ${capital}")
    
    def _reset_breaker(self):
        logger.info(f"Circuit breaker reset. Resuming trading.")
        self.breaker_triggered = False
        self.breaker_reason = None
        self.breaker_until = None
        self.consecutive_losses = 0
    
    def _reset_daily(self):
        self.daily_start_capital = self.current_capital
        self.daily_reset_time = datetime.utcnow()
        self.trades_today = 0
        logger.info(f"Daily reset. Starting capital: ${self.current_capital:.2f}")
    
    def get_current_drawdown(self) -> float:
        if self.peak_capital == 0:
            return 0.0
        drawdown = ((self.peak_capital - self.current_capital) / self.peak_capital) * Decimal("100")
        return float(drawdown)
    
    def get_status(self) -> dict:
        return {
            "trading_allowed": self.is_trading_allowed(),
            "breaker_triggered": self.breaker_triggered,
            "breaker_reason": self.breaker_reason,
            "breaker_until": self.breaker_until.isoformat() if self.breaker_until else None,
            "current_drawdown": self.get_current_drawdown(),
            "consecutive_losses": self.consecutive_losses,
            "trades_today": self.trades_today,
            "current_capital": float(self.current_capital),
            "peak_capital": float(self.peak_capital)
        }