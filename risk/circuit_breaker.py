"""
Circuit Breaker
Emergency trading halt on excessive drawdown
"""
from typing import Dict
from decimal import Decimal
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

class CircuitBreaker:
    """Drawdown protection system"""
    
    def __init__(self, max_drawdown_pct: float = 15.0, daily_loss_limit_pct: float = 10.0):
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_loss_limit_pct = daily_loss_limit_pct
        
        self.is_halted = False
        self.halt_reason = None
        self.halt_time = None
        
        self.peak_capital = Decimal("0")
        self.daily_start_capital = Decimal("0")
        self.last_reset = datetime.utcnow().date()
    
    def check(self, current_capital: Decimal, initial_capital: Decimal) -> Dict[str, bool]:
        """
        Check if circuit breaker should trigger
        
        Returns:
            {
                "should_halt": bool,
                "can_trade": bool,
                "reason": str
            }
        """
        # Reset daily tracking
        today = datetime.utcnow().date()
        if today != self.last_reset:
            self.daily_start_capital = current_capital
            self.last_reset = today
        
        # Update peak
        if current_capital > self.peak_capital:
            self.peak_capital = current_capital
        
        # Check drawdown from peak
        if self.peak_capital > 0:
            drawdown = ((self.peak_capital - current_capital) / self.peak_capital) * 100
        else:
            drawdown = 0.0
        
        # Check daily loss
        if self.daily_start_capital > 0:
            daily_loss = ((self.daily_start_capital - current_capital) / self.daily_start_capital) * 100
        else:
            daily_loss = 0.0
        
        # Check absolute loss from initial
        if initial_capital > 0:
            total_loss = ((initial_capital - current_capital) / initial_capital) * 100
        else:
            total_loss = 0.0
        
        # Trigger conditions
        if drawdown >= self.max_drawdown_pct:
            self.trigger_halt(f"Max drawdown reached: {drawdown:.1f}%")
            return {"should_halt": True, "can_trade": False, "reason": self.halt_reason}
        
        if daily_loss >= self.daily_loss_limit_pct:
            self.trigger_halt(f"Daily loss limit: {daily_loss:.1f}%")
            return {"should_halt": True, "can_trade": False, "reason": self.halt_reason}
        
        if total_loss >= 20.0:  # Hard stop at 20% total loss
            self.trigger_halt(f"Total loss exceeds 20%: {total_loss:.1f}%")
            return {"should_halt": True, "can_trade": False, "reason": self.halt_reason}
        
        return {"should_halt": False, "can_trade": not self.is_halted, "reason": None}
    
    def trigger_halt(self, reason: str):
        """Activate circuit breaker"""
        if not self.is_halted:
            self.is_halted = True
            self.halt_reason = reason
            self.halt_time = datetime.utcnow()
            logger.critical(f"⛔ CIRCUIT BREAKER TRIGGERED: {reason}")
    
    def reset(self):
        """Manual reset (use with caution)"""
        self.is_halted = False
        self.halt_reason = None
        self.halt_time = None
        logger.warning("♻️ Circuit breaker RESET - trading resumed")
    
    def get_status(self) -> Dict:
        return {
            "is_halted": self.is_halted,
            "reason": self.halt_reason,
            "halt_time": self.halt_time.isoformat() if self.halt_time else None
        }